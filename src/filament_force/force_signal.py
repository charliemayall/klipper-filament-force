# Pure force-window / per-tool anomaly helpers (no Klippy imports).
#
# Used by [filament_force] and unit-tested without a printer.

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, NamedTuple, cast


DEFAULT_SPEED_BIN_EDGES: tuple[float, ...] = (1.0, 2.0, 4.0, 7.0)


class AnomalyKind(str, Enum):
    NONE = "none"
    LOW = "low"  # runout / slip
    HIGH = "high"  # jam / clog


class TripKind(str, Enum):
    """Filament-force pause reason (maps from AnomalyKind for band trips)."""

    RUNOUT = "runout"
    JAM = "jam"

    @classmethod
    def from_anomaly(cls, kind: AnomalyKind) -> TripKind | None:
        if kind is AnomalyKind.LOW:
            return cls.RUNOUT
        if kind is AnomalyKind.HIGH:
            return cls.JAM
        return None


class Sample(NamedTuple):
    """One load-cell force reading."""

    timestamp: float
    force: float

    @classmethod
    def from_raw(cls, row: object) -> Sample | None:
        try:
            cells = cast(Sequence[Any], row)
            return cls(float(cells[0]), float(cells[1]))
        except (TypeError, ValueError, IndexError):
            return None


def iter_load_cell_samples(msg: object) -> list[Sample]:
    """Parse a Kalico LoadCell.add_client payload into Samples.

    Wire format: ``{"data": [[time, force_g, ...], ...], ...}``. Extra
    columns after force are ignored.
    """
    if not isinstance(msg, dict):
        return []
    raw = msg.get("data")
    if not isinstance(raw, list):
        return []
    out: list[Sample] = []
    for row in raw:
        parsed = Sample.from_raw(row)
        if parsed is not None:
            out.append(parsed)
    return out


class MeanStdev(NamedTuple):
    mean: float
    stdev: float


@dataclass(frozen=True)
class BandResult:
    kind: AnomalyKind
    z_score: float
    mean: float
    stdev: float
    low_bound: float
    high_bound: float


def is_short_bead(
    t_win: float, *, after_idle: bool, min_learn_s: float
) -> bool:
    """True when a window after an idle gap is too short to learn.

    Continuous extrusion is never a short bead, even if ``t_win`` is
    below ``min_learn_s`` (4 mm at cruise often is).
    """
    return after_idle and t_win < min_learn_s


def force_ref_from_idle_samples(n: int, total: float) -> float | None:
    """Tare from E-idle samples. None means keep waiting.

    Must not be faked as 0.0: the cell sits around -2kg, and |F-0| is
    scored as a jam against a learned ~400g band.
    """
    if n <= 0:
        return None
    return total / n


def rolling_mean(samples: Sequence[Sample], t: float, window_s: float) -> float | None:
    """Mean of force samples with timestamp in [t - window_s, t]."""
    t_start = t - window_s
    vals = [s.force for s in samples if s.timestamp >= t_start]
    if not vals:
        return None
    return statistics.fmean(vals)


def gate_unretract(de: float, unretract_remaining: float) -> tuple[float, float, bool]:
    """Split extruder delta into scoreable forward E vs unretract.

    Retracts (``de < 0``) add to ``unretract_remaining``. The next matching
    forward millimetres are treated as de-retract/prime and are not scored.
    Returns ``(forward_de, new_remaining, reset_window)`` where
    ``reset_window`` is True on retract so a forward-E window does not span
    a retract/prime cycle.
    """
    if de < 0.0:
        return 0.0, unretract_remaining - de, True
    if de == 0.0:
        return 0.0, unretract_remaining, False
    if unretract_remaining <= 0.0:
        return de, 0.0, False
    skip = min(de, unretract_remaining)
    return de - skip, unretract_remaining - skip, False


def deretract_has_spike(
    force1_g: float,
    peak_force_g: float,
    force2_g: float,
    *,
    spike_g: float,
) -> bool:
    """True if deretract produced a force spike vs post-retract Force1.

    Uses the larger of peak-during-deretract and final Force2 excursion
    from Force1 (absolute grams).
    """
    peak_delta = abs(peak_force_g - force1_g)
    end_delta = abs(force2_g - force1_g)
    return max(peak_delta, end_delta) >= max(0.0, spike_g)


def normalise_speed_bin_edges(edges: Sequence[float]) -> tuple[float, ...]:
    """Interior edges; bin i is [prev, edge) with 0 / +inf sentinels."""
    out = tuple(float(e) for e in edges)
    if not out:
        raise ValueError("speed_bins needs at least one interior edge")
    prev = 0.0
    for edge in out:
        if edge <= prev:
            raise ValueError(
                "speed_bins must be strictly increasing and positive"
            )
        prev = edge
    return out


def speed_bin_count(edges: Sequence[float]) -> int:
    return len(edges) + 1


def speed_bin_index(e_speed: float, edges: Sequence[float]) -> int:
    """Hard-assign e_speed to a learn bin.

    Bin i is ``[edge[i-1], edge[i])`` with sentinels 0 and +inf. Interior
    ``edges`` are e.g. ``(1.0, 2.0, 4.0, 7.0)``. Jam HIGH scoring uses
    ``expected_force_stats`` (lerp between learned neighbours), not this.
    """
    spd = max(0.0, float(e_speed))
    for i, edge in enumerate(edges):
        if spd < edge:
            return i
    return len(edges)


def speed_bin_knot(bin_i: int, edges: Sequence[float]) -> float:
    """Representative speed for bin i (lerp knots).

    Bin 0: midpoint of ``[0, edges[0])``. Interior: interval midpoint.
    Last bin: ``edges[-1]`` (no fake centre for ``[last, inf)``).
    """
    n = speed_bin_count(edges)
    if n <= 0:
        return 0.0
    i = 0 if bin_i < 0 else n - 1 if bin_i >= n else bin_i
    if i == 0:
        return float(edges[0]) * 0.5
    if i == n - 1:
        return float(edges[-1])
    return 0.5 * (float(edges[i - 1]) + float(edges[i]))


def expected_force_stats(
    bins: Sequence[ToolForceHistory],
    e_speed: float,
    edges: Sequence[float],
) -> MeanStdev | None:
    """Healthy mean/stdev at e_speed, or None if the assigned bin is unlearned.

    Lerps mean between bracketing learned knots; stdev is the max of those
    two bins so a gap does not invent a tight band. Does not interpolate
    through an unlearned assigned bin.
    """
    if not bins:
        return None
    i = speed_bin_index(e_speed, edges)
    if i >= len(bins):
        i = len(bins) - 1
    if not bins[i].learned:
        return None
    v = max(0.0, float(e_speed))
    lo: int | None = None
    hi: int | None = None
    for j, hist in enumerate(bins):
        if not hist.learned:
            continue
        knot = speed_bin_knot(j, edges)
        if knot <= v:
            lo = j
        if knot >= v and hi is None:
            hi = j
    if lo is not None and hi is not None and lo != hi:
        k0 = speed_bin_knot(lo, edges)
        k1 = speed_bin_knot(hi, edges)
        span = k1 - k0
        t = 0.0 if span <= 1e-12 else (v - k0) / span
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        s0 = bins[lo].mean_stdev()
        s1 = bins[hi].mean_stdev()
        return MeanStdev(
            s0.mean + t * (s1.mean - s0.mean),
            max(s0.stdev, s1.stdev),
        )
    return bins[i].mean_stdev()


def oh_shit_from_jam_peak(
    peak_g: float,
    current_g: float,
    *,
    margin: float = 0.85,
) -> float | None:
    """Suggested oh_shit_force from a cold-jam peak, or None if not usable.

    ``margin`` is a fraction of peak so the trip sits below a hard jam and
    above print force. None if the peak (or the suggested value) does not
    beat ``current_g`` - applying that would lower the trip.
    """
    if not 0.0 < margin <= 1.0:
        raise ValueError("margin must be in (0, 1]")
    if peak_g <= 0.0:
        return None
    suggested = peak_g * margin
    if peak_g <= current_g or suggested <= current_g:
        return None
    return suggested


def forward_e_for_test_runout(
    retract_mm: float, detection_length: float, confirm_windows: int
) -> float:
    """Forward E mm for FILAMENT_FORCE_TEST_RUNOUT after a retract.

    Extra ``retract_mm`` covers unretract skip. ``detection_length *
    (confirm_windows + 1)`` is one window more than the trip streak.
    """
    return retract_mm + detection_length * (confirm_windows + 1)


def score_level(
    level_g: float,
    mean: float,
    stdev: float,
    *,
    high_sigma: float,
    min_delta_g: float,
    drop_ratio: float = 0.35,
    runout_max_level_g: float = 80.0,
    rel_stdev_floor: float = 0.08,
) -> BandResult:
    """Classify a window level against a tool's learned band.

    LOW (runout) is a sustained force drop: level must sit at or below
    both ``drop_ratio * mean`` and ``runout_max_level_g``. That blocks
    "relative low but still lots of force" false trips while catching
    true empties that collapse near zero.

    HIGH (jam) uses a sigma band with stdev floored by ``min_delta_g`` /
    ``rel_stdev_floor``.
    """
    stdev_eff = max(stdev, min_delta_g, abs(mean) * max(0.0, rel_stdev_floor))
    half_high = max(high_sigma * stdev_eff, min_delta_g * 0.5)
    high_bound = mean + half_high

    if mean < min_delta_g:
        low_bound = 0.0
    else:
        ratio_cap = mean * max(0.0, min(1.0, drop_ratio))
        abs_cap = max(0.0, runout_max_level_g)
        low_bound = min(ratio_cap, abs_cap)

    if stdev_eff > 1e-9:
        z_score = (level_g - mean) / stdev_eff
    elif mean > 1e-9:
        z_score = (level_g - mean) / mean
    else:
        z_score = 0.0

    if level_g <= low_bound and mean >= min_delta_g:
        kind = AnomalyKind.LOW
    elif level_g > high_bound:
        kind = AnomalyKind.HIGH
    else:
        kind = AnomalyKind.NONE
    return BandResult(kind, z_score, mean, stdev, low_bound, high_bound)


class ToolForceHistory:
    """Rolling history of healthy window levels (raw grams)."""

    def __init__(self, history_n: int, min_learn_windows: int) -> None:
        self.history_n = max(1, history_n)
        self.min_learn_windows = max(1, min_learn_windows)
        self.windows: Deque[float] = deque(maxlen=self.history_n)
        self.anomalous_streak = 0
        self.healthy_streak = 0
        self.last_level = 0.0
        self.last_z_score = 0.0
        self.last_kind = AnomalyKind.NONE
        self.suspect = False

    @property
    def window_count(self) -> int:
        return len(self.windows)

    @property
    def learned(self) -> bool:
        return self.window_count >= self.min_learn_windows

    def mean_stdev(self) -> MeanStdev:
        if not self.windows:
            return MeanStdev(0.0, 0.0)
        vals = list(self.windows)
        mean = statistics.fmean(vals)
        stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        return MeanStdev(mean, stdev)

    def reset(self) -> None:
        self.windows.clear()
        self.anomalous_streak = 0
        self.healthy_streak = 0
        self.last_level = 0.0
        self.last_z_score = 0.0
        self.last_kind = AnomalyKind.NONE
        self.suspect = False

    def observe(
        self,
        level_g: float,
        *,
        high_sigma: float,
        min_delta_g: float,
        confirm_windows: int,
        recover_windows: int,
        drop_ratio: float = 0.35,
        runout_max_level_g: float = 80.0,
        trip_low: bool = True,
        allow_learn: bool = True,
    ) -> AnomalyKind | None:
        """Ingest one scored window.

        Returns AnomalyKind.LOW when a trip should fire, else None.
        During learn mode, appends samples (if ``allow_learn``) and never
        trips. HIGH is scored by ``ToolForceBook`` lerp, not here.
        ``trip_low`` is False for per-bin histories (learn only).
        """
        self.last_level = level_g
        if not self.learned:
            if allow_learn:
                self.windows.append(level_g)
            self.last_kind = AnomalyKind.NONE
            self.last_z_score = 0.0
            return None

        stats = self.mean_stdev()
        band = score_level(
            level_g,
            stats.mean,
            stats.stdev,
            high_sigma=high_sigma,
            min_delta_g=min_delta_g,
            drop_ratio=drop_ratio,
            runout_max_level_g=runout_max_level_g,
        )
        self.last_z_score = band.z_score
        self.last_kind = band.kind

        kind = band.kind
        if kind is AnomalyKind.HIGH:
            kind = AnomalyKind.NONE
        if kind is AnomalyKind.LOW and not trip_low:
            kind = AnomalyKind.NONE

        if kind == AnomalyKind.NONE:
            self.healthy_streak += 1
            self.anomalous_streak = 0
            if self.suspect and self.healthy_streak >= recover_windows:
                self.suspect = False
                self.healthy_streak = 0
            # Do not learn downward drift or jams: only append NONE levels
            # that look like the current healthy band. HIGH still lands
            # here for trips, so band.kind (not kind) gates the append.
            if (
                allow_learn
                and band.kind is AnomalyKind.NONE
                and level_g >= stats.mean * 0.9
            ):
                self.windows.append(level_g)
            return None

        self.suspect = True
        self.healthy_streak = 0
        self.anomalous_streak += 1
        if self.anomalous_streak >= confirm_windows:
            trip = kind
            self.anomalous_streak = 0
            self.suspect = False
            return trip
        return None


class ToolForceBook:
    """Per-tool: one history per e-speed bin plus a global runout history."""

    def __init__(
        self, n_bins: int, history_n: int, min_learn_windows: int
    ) -> None:
        self.n_bins = max(1, n_bins)
        self.history_n = history_n
        self.min_learn_windows = min_learn_windows
        self.bins: list[ToolForceHistory] = [
            ToolForceHistory(history_n, min_learn_windows)
            for _ in range(self.n_bins)
        ]
        self.runout = ToolForceHistory(history_n, min_learn_windows)
        self.last_bin = 0
        self.jam_streak = 0
        self.jam_healthy_streak = 0
        self.jam_suspect = False
        self.last_high_band: BandResult | None = None

    def reset(self) -> None:
        for hist in self.bins:
            hist.reset()
        self.runout.reset()
        self.last_bin = 0
        self.clear_streaks()
        self.last_high_band = None

    def clear_streaks(self) -> None:
        """Drop jam/runout confirm state; keep learned windows."""
        self.jam_streak = 0
        self.jam_healthy_streak = 0
        self.jam_suspect = False
        self.runout.anomalous_streak = 0
        self.runout.healthy_streak = 0
        self.runout.suspect = False
        for hist in self.bins:
            hist.anomalous_streak = 0
            hist.healthy_streak = 0
            hist.suspect = False

    def bin_hist(self, bin_i: int) -> ToolForceHistory:
        return self.bins[self._clamp_bin(bin_i)]

    def _clamp_bin(self, bin_i: int) -> int:
        if bin_i < 0:
            return 0
        if bin_i >= self.n_bins:
            return self.n_bins - 1
        return bin_i

    def _note_jam(
        self, kind: AnomalyKind, confirm_windows: int, recover_windows: int
    ) -> AnomalyKind | None:
        """Book-level HIGH confirm (not per-bin). None kind is interpolated-healthy."""
        if kind is AnomalyKind.HIGH:
            self.jam_suspect = True
            self.jam_healthy_streak = 0
            self.jam_streak += 1
            if self.jam_streak >= confirm_windows:
                self.jam_streak = 0
                self.jam_suspect = False
                return AnomalyKind.HIGH
            return None
        self.jam_healthy_streak += 1
        self.jam_streak = 0
        if self.jam_suspect and self.jam_healthy_streak >= recover_windows:
            self.jam_suspect = False
            self.jam_healthy_streak = 0
        return None

    def observe(
        self,
        raw_g: float,
        e_speed: float,
        edges: Sequence[float],
        *,
        short_bead: bool,
        high_sigma: float,
        min_delta_g: float,
        confirm_windows: int,
        recover_windows: int,
        drop_ratio: float = 0.35,
        runout_max_level_g: float = 80.0,
    ) -> AnomalyKind | None:
        """Score one raw-gram window.

        Learn is per-bin. HIGH uses lerped expected force at ``e_speed``;
        unlearned assigned bins never HIGH. LOW is global. Short beads skip
        scoring entirely (no learn, no HIGH, no LOW): post-travel windows
        are dominated by toolhead accel on the load cell. HIGH confirm is
        consecutive across bins; runout confirm is also consecutive across
        bins. Short / unlearned-HIGH skips leave the jam streak alone.
        """
        bin_i = self._clamp_bin(speed_bin_index(e_speed, edges))
        self.last_bin = bin_i
        if short_bead:
            return None
        hist = self.bins[bin_i]
        hist.observe(
            raw_g,
            high_sigma=high_sigma,
            min_delta_g=min_delta_g,
            confirm_windows=confirm_windows,
            recover_windows=recover_windows,
            drop_ratio=drop_ratio,
            runout_max_level_g=runout_max_level_g,
            trip_low=False,
            allow_learn=True,
        )
        stats = expected_force_stats(self.bins, e_speed, edges)
        high_trip: AnomalyKind | None = None
        if stats is None:
            self.last_high_band = None
        else:
            band = score_level(
                raw_g,
                stats.mean,
                stats.stdev,
                high_sigma=high_sigma,
                min_delta_g=min_delta_g,
                drop_ratio=drop_ratio,
                runout_max_level_g=runout_max_level_g,
            )
            self.last_high_band = band
            high_kind = (
                AnomalyKind.HIGH if band.kind is AnomalyKind.HIGH else AnomalyKind.NONE
            )
            high_trip = self._note_jam(
                high_kind, confirm_windows, recover_windows
            )
        low_trip = self.runout.observe(
            raw_g,
            high_sigma=high_sigma,
            min_delta_g=min_delta_g,
            confirm_windows=confirm_windows,
            recover_windows=recover_windows,
            drop_ratio=drop_ratio,
            runout_max_level_g=runout_max_level_g,
            trip_low=True,
            allow_learn=True,
        )
        return high_trip or low_trip


class WindowLevel(NamedTuple):
    """Completed forward-E window score.

    ``level`` is raw grams (mean |F-ref|). ``e_speed`` selects the learn
    bin and the lerped HIGH band.
    """

    level: float
    e_speed: float
    t_win: float = 0.0


@dataclass
class EWindowAccumulator:
    """Forward-E window spanning many tiny print moves.

    Accumulates commanded forward E until ``detection_length`` mm across G1
    boundaries. Raw force is **mean |rolling_mean(force) - ref|** over
    samples in that window. ``ref`` is locked at arm / window start (tare
    or seat preload) and must not be refreshed mid-print.

    Travel idle longer than ``idle_reset_s`` (applied by the caller via
    ``note_idle``) clears a partial window so beads do not stitch.
    ``min_learn_s`` applies only to the first window after that idle.
    """

    detection_length: float
    rolling_window_s: float
    e_accum: float = 0.0
    force_ref: float = 0.0
    level_sum: float = 0.0
    level_n: int = 0
    e_speed_sum: float = 0.0
    e_speed_weight: float = 0.0
    first_e_time: float | None = None
    last_e_time: float | None = None
    samples: Deque[Sample] = field(default_factory=deque)
    active: bool = False

    def reset(self) -> None:
        self.e_accum = 0.0
        self.level_sum = 0.0
        self.level_n = 0
        self.e_speed_sum = 0.0
        self.e_speed_weight = 0.0
        self.first_e_time = None
        self.last_e_time = None
        self.samples.clear()
        self.active = False

    @property
    def mean_level(self) -> float:
        if self.level_n <= 0:
            return 0.0
        return self.level_sum / self.level_n

    @property
    def mean_e_speed(self) -> float:
        if self.e_speed_weight <= 0.0:
            return 0.0
        return self.e_speed_sum / self.e_speed_weight

    @property
    def t_win(self) -> float:
        if self.first_e_time is None or self.last_e_time is None:
            return 0.0
        return max(0.0, self.last_e_time - self.first_e_time)

    def start(self, force_ref: float = 0.0) -> None:
        self.reset()
        self.force_ref = force_ref
        self.active = True

    def note_idle(self, idle_s: float, idle_reset_s: float) -> bool:
        """Reset a partial window after a travel/idle gap.

        Returns True if a non-empty window was discarded.
        """
        if idle_s <= idle_reset_s:
            return False
        if not self.active and self.e_accum <= 0.0 and self.level_n <= 0:
            return False
        self.reset()
        return True

    def add_force(self, sample: Sample) -> None:
        if not self.active:
            return
        self.samples.append(sample)
        keep = self.rolling_window_s + 0.5
        cutoff = sample.timestamp - keep
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()
        rm = rolling_mean(self.samples, sample.timestamp, self.rolling_window_s)
        if rm is None:
            return
        self.level_sum += abs(rm - self.force_ref)
        self.level_n += 1

    def add_forward_e(
        self,
        de: float,
        force_ref: float | None = None,
        *,
        e_speed: float | None = None,
        eventtime: float | None = None,
    ) -> WindowLevel | None:
        """Add forward E mm. Returns a WindowLevel when the window completes.

        ``e_speed`` is commanded forward E speed (mm/s) for this segment
        (bin selection and lerp). ``eventtime`` tracks wall-time span for
        the short-bead gate.

        Returns None when the window completes with no force samples (avoid
        scoring a bogus 0 as runout).
        """
        if de <= 0:
            return None
        if not self.active:
            self.start(0.0 if force_ref is None else force_ref)
        if eventtime is not None:
            if self.first_e_time is None:
                self.first_e_time = eventtime
            self.last_e_time = eventtime
        self.e_accum += de
        if e_speed is not None and e_speed > 0.0:
            self.e_speed_sum += e_speed * de
            self.e_speed_weight += de
        if self.e_accum < self.detection_length:
            return None
        if self.level_n <= 0:
            self.reset()
            return None
        raw = self.mean_level
        mean_spd = self.mean_e_speed
        span = self.t_win
        self.reset()
        return WindowLevel(
            level=raw, e_speed=mean_spd, t_win=span
        )
