# E-gated per-tool filament health via the toolhead load cell.
#
# Config section: [filament_force]
#
# Continuous detection: sustained forward-E force drop (global runout) and
# high-force jam against lerped expected force from per e-speed-bin
# history. Soft-pauses via pause.MonitorActions.
#
# Presence probe (retract -> deretract spike) is on-demand only via
# FILAMENT_FORCE_CHECK_SPIKE - not part of continuous detection.
#
# FILAMENT_FORCE_CAL_OH_SHIT cold-extrudes to measure a jam peak and set
# oh_shit_force to a fraction of that peak.
#
# Default continuous_detection: True.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from . import force_signal
from .force_signal import BandResult, TripKind, is_short_bead, oh_shit_from_jam_peak
from .pause import MonitorActions, MonitorTemplate

if TYPE_CHECKING:
    from klippy.configfile import ConfigWrapper
    from klippy.extras.load_cell import LoadCell
    from klippy.gcode import GCodeCommand, GCodeDispatch
    from klippy.klippy import Printer
    from klippy.reactor import SelectReactor


def _resolve_load_cell(sensor: object) -> LoadCell:
    """Return the LoadCell that streams force via add_client.

    Accepts a [load_cell] object directly, or a [load_cell_probe] wrapper
    that owns ``_load_cell`` (Kalico LoadCellPrinterProbe).
    """
    if hasattr(sensor, "add_client"):
        return cast(Any, sensor)
    inner = getattr(sensor, "_load_cell", None)
    if inner is not None and hasattr(inner, "add_client"):
        return cast(Any, inner)
    raise ValueError(
        "sensor does not provide load-cell force streaming"
        " (expected [load_cell] or [load_cell_probe])"
    )


def _ff_line(ev: str, fields: dict[str, object]) -> str:
    """One parsable klippy.log line: filament_force ev=... k=v ..."""
    parts = [f"filament_force ev={ev}"]
    for key, value in fields.items():
        parts.append(f"{key}={'-' if value is None else value}")
    return " ".join(parts)


@dataclass
class SpikeCheck:
    """On-demand retract/deretract spike state (CHECK_SPIKE)."""

    active: bool = False
    tracking: bool = False
    force1: float = 0.0
    peak: float = 0.0
    n_samples: int = 0
    pending_detail: str = ""
    last_spike: int = 0
    last_delta_g: float = 0.0
    last_f1_g: float = 0.0
    last_peak_g: float = 0.0
    last_f2_g: float = 0.0
    last_n_samples: int = 0
    last_skip_reason: str = ""

    def abort(self) -> None:
        self.active = False
        self.tracking = False

    def note_force(self, force: float) -> None:
        if not self.tracking:
            return
        self.n_samples += 1
        if abs(force - self.force1) > abs(self.peak - self.force1):
            self.peak = force

    def begin(self, detail: str) -> None:
        self.active = True
        self.tracking = False
        self.pending_detail = detail
        self.n_samples = 0

    def on_f1(self, force: float) -> None:
        self.force1 = force
        self.peak = force
        self.tracking = True
        self.n_samples = 0

    def on_done(self, force2: float, spike_g: float) -> bool:
        self.tracking = False
        spike = force_signal.deretract_has_spike(
            self.force1, self.peak, force2, spike_g=spike_g
        )
        delta = max(abs(self.peak - self.force1), abs(force2 - self.force1))
        self.last_spike = 1 if spike else 0
        self.last_delta_g = delta
        self.last_f1_g = self.force1
        self.last_peak_g = self.peak
        self.last_f2_g = force2
        self.last_n_samples = self.n_samples
        self.active = False
        self.pending_detail = ""
        return spike


@dataclass
class OhShitCal:
    """Cold-extrude jam peak for oh_shit_force."""

    active: bool = False
    tracking: bool = False
    margin: float = 0.85
    apply: bool = True
    ref: float = 0.0
    ref_sum: float = 0.0
    ref_n: int = 0
    peak_g: float = 0.0
    n_samples: int = 0
    last_peak_g: float = 0.0
    outcome: str = ""

    def abort(self) -> None:
        self.active = False
        self.tracking = False

    def begin(self, *, margin: float, apply: bool) -> None:
        self.active = True
        self.tracking = False
        self.margin = margin
        self.apply = apply
        self.ref = 0.0
        self.ref_sum = 0.0
        self.ref_n = 0
        self.peak_g = 0.0
        self.n_samples = 0
        self.outcome = ""

    def note(self, force: float) -> None:
        if not self.active:
            return
        if not self.tracking:
            self.ref_sum += force
            self.ref_n += 1
            return
        self.n_samples += 1
        raw = abs(force - self.ref)
        if raw > self.peak_g:
            self.peak_g = raw

    def start_extrude(self, fallback_force: float) -> None:
        if self.ref_n > 0:
            self.ref = self.ref_sum / self.ref_n
        else:
            self.ref = fallback_force
        self.peak_g = 0.0
        self.n_samples = 0
        self.tracking = True

    def finish(self) -> float:
        self.last_peak_g = self.peak_g
        self.active = False
        self.tracking = False
        return self.peak_g


def allow_cold_extrude(heater: object | None) -> Any:
    """Enable G1 E below min_extrude_temp. Returns a restore callback.

    CAL_OH_SHIT is a cold jam by design. Without this, G1 E dies with
    'Extrude below minimum temp' after TEMPERATURE_WAIT.
    """
    if heater is None:
        return lambda: None
    h = cast(Any, heater)
    setter = getattr(h, "set_cold_extrude", None)
    if setter is not None:
        prev = bool(getattr(h, "cold_extrude", False))
        setter(True, None)
        return lambda: setter(prev, None)
    # Upstream Klipper: can_extrude is derived from min_extrude_temp.
    prev_min = float(getattr(h, "min_extrude_temp", 0.0))
    h.min_extrude_temp = 0.0
    h.can_extrude = True

    def restore() -> None:
        h.min_extrude_temp = prev_min
        h.can_extrude = float(getattr(h, "smoothed_temp", 0.0)) >= prev_min

    return restore


class FilamentForce:
    def __init__(self, config: ConfigWrapper) -> None:
        self.printer: Printer = config.get_printer()
        self.reactor: SelectReactor = self.printer.get_reactor()
        self.gcode: GCodeDispatch = self.printer.lookup_object("gcode")
        self.actions = MonitorActions(
            config,
            template_keys={
                MonitorTemplate.RUNOUT: "runout_gcode",
                MonitorTemplate.JAM: "jam_gcode",
                MonitorTemplate.RECOVER: "recover_gcode",
                MonitorTemplate.RESUME: "resume_gcode",
            },
        )

        self.sensor_name: str = config.get("sensor", "load_cell_probe")
        self.extruder_name: str = config.get("extruder", "extruder")
        self.continuous_detection: bool = config.getboolean(
            "continuous_detection", True
        )
        self.detection_length: float = config.getfloat(
            "detection_length", 7.0, above=0.0
        )
        self.min_e_speed: float = config.getfloat("min_e_speed", 0.5, above=0.0)
        self.rolling_window: float = config.getfloat("rolling_window", 0.05, above=0.0)
        self.baseline_time: float = config.getfloat("baseline_time", 0.15, above=0.0)
        self.min_delta_g: float = config.getfloat("min_delta_g", 15.0, above=0.0)
        self.high_sigma: float = config.getfloat("high_sigma", 4.0, above=0.0)
        self.drop_ratio: float = config.getfloat(
            "drop_ratio", 0.35, minval=0.0, maxval=1.0
        )
        self.runout_max_level_g: float = config.getfloat(
            "runout_max_level_g", 80.0, minval=0.0
        )
        self.confirm_windows: int = config.getint("confirm_windows", 3, minval=1)
        self.recover_windows: int = config.getint("recover_windows", 2, minval=1)
        self.history_n: int = config.getint("history_n", 16, minval=5)
        self.min_learn_windows: int = config.getint("min_learn_windows", 5, minval=2)
        self.oh_shit_force: float = config.getfloat("oh_shit_force", 4000.0, above=0.0)
        self.jam_dwell_s: float = config.getfloat("jam_dwell_s", 0.15, above=0.0)
        self.poll_interval: float = (
            config.getint("poll_interval_ms", 250, minval=50, maxval=2000) / 1000.0
        )
        self.probe_retract_mm: float = config.getfloat(
            "probe_retract_mm", 3.0, above=0.0
        )
        self.probe_extra_prime_mm: float = config.getfloat(
            "probe_extra_prime_mm", 0.2, minval=0.0
        )
        self.probe_spike_g: float = config.getfloat("probe_spike_g", 50.0, above=0.0)
        self.probe_feedrate: float = config.getfloat("probe_feedrate", 30.0, above=0.0)
        self.quiet: bool = config.getboolean("quiet", False)
        self.idle_reset_s: float = config.getfloat("idle_reset_s", 0.3, above=0.0)
        self.min_learn_s: float = config.getfloat("min_learn_s", 1.5, minval=0.0)
        self.debug_log: bool = config.getboolean("debug_log", False)
        try:
            self.speed_bin_edges: tuple[float, ...] = (
                force_signal.normalise_speed_bin_edges(
                    config.getfloatlist(
                        "speed_bins", force_signal.DEFAULT_SPEED_BIN_EDGES
                    )
                )
            )
        except ValueError as exc:
            raise config.error(f"filament_force: {exc}")

        self.enabled: bool = self.continuous_detection
        self.active_tool: int = -1
        self.books: dict[int, force_signal.ToolForceBook] = {}
        self.suppress: bool = False
        self._armed: bool = False
        self._load_cell: LoadCell | None = None
        self._extruder: Any | None = None
        self._estimated_print_time: Any | None = None
        self._last_e_pos: float | None = None
        self._last_e_time: float | None = None
        self._window = force_signal.EWindowAccumulator(
            detection_length=self.detection_length,
            rolling_window_s=self.rolling_window,
        )
        self._force_ref: float = 0.0
        self._force_ref_locked: bool = False
        self._ref_sum: float = 0.0
        self._ref_n: int = 0
        self._ref_started: float | None = None
        self._seen_forward_e: bool = False
        self._idle_confirmed: bool = False
        self._last_force: float = 0.0
        self._e_idle_since: float | None = None
        self._last_idle_s: float = 0.0
        self._after_idle: bool = False
        self._unretract_remaining: float = 0.0
        self._jam_above_since: float | None = None
        self._triggering: bool = False
        self._poll_timer: Any | None = None
        self._client_attached: bool = False
        self._last_level_g: float = 0.0
        self._last_e_speed: float = 0.0
        self._last_bin: int = 0
        self.last_msg: str = ""
        self.last_trip: TripKind | None = None
        self._spike = SpikeCheck()
        self._cal = OhShitCal()

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "idle_timeout:printing", self._handle_printing
        )
        self.printer.register_event_handler(
            "idle_timeout:ready", self._handle_not_printing
        )
        self.printer.register_event_handler(
            "idle_timeout:idle", self._handle_not_printing
        )

        self.gcode.register_command(
            "FILAMENT_FORCE_SET",
            self.cmd_FILAMENT_FORCE_SET,
            desc=self.cmd_FILAMENT_FORCE_SET_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_QUERY",
            self.cmd_FILAMENT_FORCE_QUERY,
            desc=self.cmd_FILAMENT_FORCE_QUERY_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_SET_TOOL",
            self.cmd_FILAMENT_FORCE_SET_TOOL,
            desc=self.cmd_FILAMENT_FORCE_SET_TOOL_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_RESET",
            self.cmd_FILAMENT_FORCE_RESET,
            desc=self.cmd_FILAMENT_FORCE_RESET_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_RESUME",
            self.cmd_FILAMENT_FORCE_RESUME,
            desc=self.cmd_FILAMENT_FORCE_RESUME_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_SUPPRESS",
            self.cmd_FILAMENT_FORCE_SUPPRESS,
            desc=self.cmd_FILAMENT_FORCE_SUPPRESS_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_CHECK_SPIKE",
            self.cmd_FILAMENT_FORCE_CHECK_SPIKE,
            desc=self.cmd_FILAMENT_FORCE_CHECK_SPIKE_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_TEST_RUNOUT",
            self.cmd_FILAMENT_FORCE_TEST_RUNOUT,
            desc=self.cmd_FILAMENT_FORCE_TEST_RUNOUT_help,
        )
        self.gcode.register_command(
            "FILAMENT_FORCE_CAL_OH_SHIT",
            self.cmd_FILAMENT_FORCE_CAL_OH_SHIT,
            desc=self.cmd_FILAMENT_FORCE_CAL_OH_SHIT_help,
        )
        self.gcode.register_command(
            "_FILAMENT_FORCE_PROBE_F1",
            self.cmd__FILAMENT_FORCE_PROBE_F1,
        )
        self.gcode.register_command(
            "_FILAMENT_FORCE_PROBE_DONE",
            self.cmd__FILAMENT_FORCE_PROBE_DONE,
        )
        self.gcode.register_command(
            "_FILAMENT_FORCE_CAL_OH_SHIT_GO",
            self.cmd__FILAMENT_FORCE_CAL_OH_SHIT_GO,
        )
        self.gcode.register_command(
            "_FILAMENT_FORCE_CAL_OH_SHIT_DONE",
            self.cmd__FILAMENT_FORCE_CAL_OH_SHIT_DONE,
        )

    def _ff_log(self, ev: str, fields: dict[str, object]) -> None:
        # klippy.log only. Never respond_info: window/skip fire every
        # detection_length mm and flood Moonraker/Mainsail (Timer too close).
        if not self.debug_log:
            return
        logging.info(_ff_line(ev, fields))

    def _log_info(self, msg: str, *, ok: bool = False) -> None:
        """Console log. In quiet mode, skip ok / routine messages."""
        self.last_msg = msg
        if self.quiet and ok:
            return
        self.gcode.respond_info(msg)

    def _band(self, hist: force_signal.ToolForceHistory) -> BandResult:
        stats = hist.mean_stdev()
        return force_signal.score_level(
            hist.last_level,
            stats.mean,
            stats.stdev,
            high_sigma=self.high_sigma,
            min_delta_g=self.min_delta_g,
            drop_ratio=self.drop_ratio,
            runout_max_level_g=self.runout_max_level_g,
        )

    def _book_for(self, tool: int) -> force_signal.ToolForceBook:
        key = tool if tool >= 0 else -1
        book = self.books.get(key)
        if book is None:
            book = force_signal.ToolForceBook(
                n_bins=force_signal.speed_bin_count(self.speed_bin_edges),
                history_n=self.history_n,
                min_learn_windows=self.min_learn_windows,
            )
            self.books[key] = book
        return book

    def _rebuild_books(self) -> None:
        self.books = {}

    def _handle_ready(self) -> None:
        try:
            sensor = self.printer.lookup_object(self.sensor_name)
            self._load_cell = _resolve_load_cell(sensor)
        except Exception as exc:
            self.last_msg = f"load cell resolve failed: {exc}"
            self._load_cell = None
        self._extruder = self.printer.lookup_object(self.extruder_name, None)
        mcu = self.printer.lookup_object("mcu", None)
        if mcu is not None:
            self._estimated_print_time = mcu.estimated_print_time
        self._poll_timer = self.reactor.register_timer(
            self._poll_event, self.reactor.NEVER
        )

    def _handle_printing(self, print_time: float) -> None:
        del print_time
        if not self.enabled or self._load_cell is None:
            return
        self._arm()

    def _handle_not_printing(self, print_time: float) -> None:
        del print_time
        self._disarm()
        self._clear_pending_unless_paused()

    def _clear_pending_unless_paused(self) -> None:
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is None:
            self.actions.clear_pending()
            return
        state = print_stats.get_status(self.reactor.monotonic()).get("state", "")
        if state != "paused":
            self.actions.clear_pending()

    def _arm(self) -> None:
        # G1 E from CHECK_SPIKE / CAL_OH_SHIT trips idle_timeout:printing.
        # Arming here would abort the routine that caused the motion.
        if self._cal.active or self._spike.active:
            return
        if self._armed or self._load_cell is None or self._poll_timer is None:
            return
        self._armed = True
        self._last_e_pos = None
        self._last_e_time = None
        self._window.reset()
        self._unlock_baseline()
        self._last_force = 0.0
        self._e_idle_since = None
        self._last_idle_s = 0.0
        self._after_idle = False
        self._unretract_remaining = 0.0
        self._spike.abort()
        self._cal.abort()
        self.actions.clear_pending()
        if not self._client_attached:
            self._load_cell.add_client(self._force_client)
            self._client_attached = True
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)

    def _unlock_baseline(self) -> None:
        """Drop the locked tare so the next 0.15s of samples re-lock.

        Tool pickup changes seat preload. Scoring |F-ref| against the
        previous tool's tare looks like a jam.
        """
        self._force_ref = 0.0
        self._force_ref_locked = False
        self._ref_sum = 0.0
        self._ref_n = 0
        self._ref_started = None
        self._seen_forward_e = False
        self._idle_confirmed = False
        self._jam_above_since = None

    def _disarm(self) -> None:
        self._armed = False
        if self._poll_timer is not None:
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)

    def _force_client(self, msg: object) -> bool:
        in_probe = self._spike.active or self._cal.active
        if not self._armed and not in_probe:
            return True
        if self.suppress and not in_probe:
            return True
        if self._triggering and not in_probe:
            return True
        for sample in force_signal.iter_load_cell_samples(msg):
            self._last_force = sample.force
            if self._cal.active:
                self._cal.note(sample.force)
                continue
            self._spike.note_force(sample.force)
            if (
                not self._spike.active
                and not self._force_ref_locked
                and not self._seen_forward_e
                and self._idle_confirmed
            ):
                if self._ref_started is None:
                    self._ref_started = sample.timestamp
                self._ref_sum += sample.force
                self._ref_n += 1
                if sample.timestamp - self._ref_started >= self.baseline_time:
                    ref = force_signal.force_ref_from_idle_samples(
                        self._ref_n, self._ref_sum
                    )
                    if ref is not None:
                        self._force_ref = ref
                        self._force_ref_locked = True
            # NB: poll_interval_ms lag (default 250ms) before idle is
            # visible here. Hops shorter than that still mix in. Do not
            # include samples once poll has marked E idle - time-average
            # of travel/accel would look like runout or jam.
            if (
                not self._spike.active
                and self._unretract_remaining <= 0.0
                and self._e_idle_since is None
            ):
                self._window.add_force(sample)
            oh_shit_ok = (
                self._force_ref_locked
                and self._seen_forward_e
                and not self.actions.pending_recheck
            )
            if oh_shit_ok and abs(sample.force - self._force_ref) >= self.oh_shit_force:
                if self._jam_above_since is None:
                    self._jam_above_since = sample.timestamp
                elif sample.timestamp - self._jam_above_since >= self.jam_dwell_s:
                    if not self._spike.active:
                        logging.info(
                            _ff_line(
                                "trip",
                                {
                                    "kind": TripKind.JAM.value,
                                    "tool": self.active_tool,
                                    "bin": self._last_bin,
                                    "raw_g": (
                                        f"{abs(sample.force - self._force_ref):.1f}"
                                    ),
                                    "mean_g": "-",
                                    "z": "-",
                                },
                            )
                        )
                        self.reactor.register_callback(
                            lambda et: self._trip(
                                TripKind.JAM, "absolute oh_shit_force dwell"
                            )
                        )
                    self._jam_above_since = None
            else:
                self._jam_above_since = None
        return True

    def _extruder_pos(self, eventtime: float) -> float | None:
        if self._extruder is None or self._estimated_print_time is None:
            return None
        print_time = self._estimated_print_time(eventtime)
        return float(self._extruder.find_past_position(print_time))

    def _note_idle(self, eventtime: float) -> None:
        self._idle_confirmed = True
        if self._e_idle_since is None:
            self._e_idle_since = eventtime
        idle_s = eventtime - self._e_idle_since
        self._last_idle_s = idle_s
        if idle_s > self.idle_reset_s:
            self._after_idle = True
        self._window.note_idle(idle_s, self.idle_reset_s)

    def _poll_event(self, eventtime: float) -> float:
        next_time = eventtime + self.poll_interval
        if (
            not self._armed
            or not self.enabled
            or self.suppress
            or self._triggering
            or self._spike.active
            or self._cal.active
            or self.actions.pending_recheck
        ):
            return next_time
        e_pos = self._extruder_pos(eventtime)
        if e_pos is None:
            return next_time
        if self._last_e_pos is None or self._last_e_time is None:
            self._last_e_pos = e_pos
            self._last_e_time = eventtime
            return next_time
        de = e_pos - self._last_e_pos
        dt = max(eventtime - self._last_e_time, 1e-6)
        self._last_e_pos = e_pos
        self._last_e_time = eventtime

        de, self._unretract_remaining, reset_win = force_signal.gate_unretract(
            de, self._unretract_remaining
        )
        if reset_win:
            self._window.reset()
            self._idle_confirmed = True
            if self._e_idle_since is None:
                self._e_idle_since = eventtime
            return next_time
        if de <= 0.0:
            self._note_idle(eventtime)
            return next_time

        e_speed = de / dt
        if e_speed < self.min_e_speed:
            self._note_idle(eventtime)
            return next_time

        if self._e_idle_since is not None:
            self._last_idle_s = eventtime - self._e_idle_since
            if self._last_idle_s > self.idle_reset_s:
                self._after_idle = True
            self._window.note_idle(self._last_idle_s, self.idle_reset_s)
        self._e_idle_since = None
        self._idle_confirmed = False
        if not self._force_ref_locked:
            # Do not lock ref=0 (or a partial motion average). |F-0| is
            # the cell preload (~2kg) and trips HIGH against a learned
            # filament band. Wait for E-idle samples in _force_client.
            self._ref_sum = 0.0
            self._ref_n = 0
            self._ref_started = None
            return next_time
        self._seen_forward_e = True

        completed = self._window.add_forward_e(
            de,
            self._force_ref,
            e_speed=e_speed,
            eventtime=eventtime,
        )
        if completed is None:
            return next_time
        raw_g = completed.level
        self._last_level_g = raw_g
        self._last_e_speed = completed.e_speed
        bin_i = force_signal.speed_bin_index(
            completed.e_speed, self.speed_bin_edges
        )
        self._last_bin = bin_i
        short_bead = is_short_bead(
            completed.t_win,
            after_idle=self._after_idle,
            min_learn_s=self.min_learn_s,
        )
        self._after_idle = False

        book = self._book_for(self.active_tool)
        hist = book.bin_hist(bin_i)
        trip = book.observe(
            raw_g,
            completed.e_speed,
            self.speed_bin_edges,
            short_bead=short_bead,
            high_sigma=self.high_sigma,
            min_delta_g=self.min_delta_g,
            confirm_windows=self.confirm_windows,
            recover_windows=self.recover_windows,
            drop_ratio=self.drop_ratio,
            runout_max_level_g=self.runout_max_level_g,
        )
        stats = hist.mean_stdev()
        skip_s = "short_bead" if short_bead else "-"
        if short_bead:
            self._ff_log(
                "skip",
                {
                    "tool": self.active_tool,
                    "reason": "short_bead",
                    "e_mm/s": f"{completed.e_speed:.2f}",
                    "t_win": f"{completed.t_win:.2f}",
                    "idle_s": f"{self._last_idle_s:.2f}",
                },
            )
        high_band = book.last_high_band
        if high_band is not None:
            kind_s = high_band.kind.value
            z_s = high_band.z_score
            mean_g = high_band.mean
            stdev_g = high_band.stdev
        else:
            kind_s = hist.last_kind.value
            z_s = hist.last_z_score
            mean_g = stats.mean
            stdev_g = stats.stdev
        streak = book.jam_streak
        learned_i = 1 if hist.learned else 0
        if trip is force_signal.AnomalyKind.LOW:
            kind_s = "low"
            run_stats = book.runout.mean_stdev()
            mean_g = run_stats.mean
            stdev_g = run_stats.stdev
            z_s = book.runout.last_z_score
            streak = book.runout.anomalous_streak
            learned_i = 1 if book.runout.learned else 0
        self._ff_log(
            "window",
            {
                "tool": self.active_tool,
                "bin": bin_i,
                "e_mm/s": f"{completed.e_speed:.2f}",
                "raw_g": f"{raw_g:.1f}",
                "mean_g": f"{mean_g:.1f}",
                "stdev_g": f"{stdev_g:.1f}",
                "z": f"{z_s:.2f}",
                "kind": kind_s,
                "learned": learned_i,
                "streak": streak,
                "skip": skip_s,
            },
        )
        trip_kind = TripKind.from_anomaly(trip) if trip is not None else None
        if trip_kind is None:
            return next_time
        self._ff_log(
            "trip",
            {
                "kind": trip_kind.value,
                "tool": self.active_tool,
                "bin": bin_i,
                "raw_g": f"{raw_g:.1f}",
                "mean_g": f"{mean_g:.1f}",
                "z": f"{z_s:.2f}",
            },
        )
        detail = (
            f"{'low' if trip_kind is TripKind.RUNOUT else 'high'}"
            f" raw={raw_g:.1f}g"
            f" e_speed={completed.e_speed:.2f}mm/s"
            f" bin={bin_i}"
            f" z={z_s:.2f}"
        )
        self.reactor.register_callback(
            lambda et, k=trip_kind, d=detail: self._trip(k, d)
        )
        return next_time

    def _begin_spike_check(
        self,
        detail: str,
        *,
        retract_mm: float | None = None,
        extra_prime_mm: float | None = None,
        feedrate: float | None = None,
        spike_g: float | None = None,
    ) -> None:
        if self._spike.active or self._triggering or self._cal.active:
            self._spike.last_skip_reason = "probe_busy"
            self.gcode.respond_info(
                f"filament_force: CHECK_SPIKE skipped ({self._spike.last_skip_reason})"
            )
            return
        if spike_g is not None:
            self.probe_spike_g = spike_g
        retract = self.probe_retract_mm if retract_mm is None else retract_mm
        extra = (
            self.probe_extra_prime_mm if extra_prime_mm is None else extra_prime_mm
        )
        feed_mms = self.probe_feedrate if feedrate is None else feedrate
        prime = retract + extra
        feed = max(1.0, feed_mms) * 60.0
        self._spike.begin(detail)
        self._window.reset()
        if self._load_cell is not None and not self._client_attached:
            self._load_cell.add_client(self._force_client)
            self._client_attached = True
        self._log_info(
            f"filament_force: CHECK_SPIKE"
            f" retract={retract:.2f}mm deretract={prime:.2f}mm"
            f" spike_g={self.probe_spike_g:.1f}",
            ok=True,
        )
        script = (
            "M400\n"
            "SAVE_GCODE_STATE NAME=_FILAMENT_FORCE_PROBE\n"
            "M83\n"
            f"G1 E-{retract:.3f} F{feed:.1f}\n"
            "M400\n"
            "_FILAMENT_FORCE_PROBE_F1\n"
            f"G1 E{prime:.3f} F{feed:.1f}\n"
            "M400\n"
            "_FILAMENT_FORCE_PROBE_DONE\n"
            "RESTORE_GCODE_STATE NAME=_FILAMENT_FORCE_PROBE\n"
        )
        self.gcode.run_script_from_command(script)

    def cmd__FILAMENT_FORCE_PROBE_F1(self, gcmd: GCodeCommand) -> None:
        del gcmd
        if not self._spike.active:
            return
        self._spike.on_f1(self._last_force)

    def cmd__FILAMENT_FORCE_PROBE_DONE(self, gcmd: GCodeCommand) -> None:
        del gcmd
        if not self._spike.active:
            return
        spike = self._spike.on_done(self._last_force, self.probe_spike_g)
        delta = self._spike.last_delta_g
        if spike:
            self._log_info(
                f"filament_force CHECK_SPIKE: spike delta={delta:.1f}g"
                f" (f1={self._spike.last_f1_g:.1f} peak={self._spike.last_peak_g:.1f}"
                f" f2={self._spike.last_f2_g:.1f}) - filament present",
                ok=True,
            )
        else:
            self._log_info(
                f"filament_force CHECK_SPIKE: no spike delta={delta:.1f}g"
                f" (f1={self._spike.last_f1_g:.1f} peak={self._spike.last_peak_g:.1f}"
                f" f2={self._spike.last_f2_g:.1f} spike_g={self.probe_spike_g:.1f})"
            )

    cmd_FILAMENT_FORCE_CHECK_SPIKE_help = (
        "On-demand presence probe: retract/deretract and score force spike. "
        "FILAMENT_FORCE_CHECK_SPIKE [SPIKE_G=] [RETRACT=] [EXTRA_PRIME=] "
        "[FEEDRATE=]. Sets last_probe_spike; does not trip runout."
    )

    def cmd_FILAMENT_FORCE_CHECK_SPIKE(self, gcmd: GCodeCommand) -> None:
        spike = gcmd.get_float("SPIKE_G", None, above=0.0)
        retract = gcmd.get_float("RETRACT", None, above=0.0)
        extra = gcmd.get_float("EXTRA_PRIME", None, minval=0.0)
        feed = gcmd.get_float("FEEDRATE", None, above=0.0)
        self._begin_spike_check(
            "manual CHECK_SPIKE",
            retract_mm=retract,
            extra_prime_mm=extra,
            feedrate=feed,
            spike_g=spike,
        )

    cmd_FILAMENT_FORCE_TEST_RUNOUT_help = (
        "Heat, retract, then extrude enough forward E for global runout. "
        "FILAMENT_FORCE_TEST_RUNOUT RETRACT=<mm> TEMP=<c> [SPEED=<mm/s>] "
        "[RETRACT_SPEED=<mm/s>]"
    )

    def cmd_FILAMENT_FORCE_TEST_RUNOUT(self, gcmd: GCodeCommand) -> None:
        retract = gcmd.get_float("RETRACT", above=0.0)
        temp = gcmd.get_float("TEMP", above=0.0)
        speed = gcmd.get_float("SPEED", 4.0, above=0.0)
        retract_speed = gcmd.get_float("RETRACT_SPEED", 30.0, above=0.0)
        if not self.enabled:
            raise gcmd.error("filament_force: TEST_RUNOUT needs ENABLE=1")
        if self.suppress:
            raise gcmd.error("filament_force: TEST_RUNOUT refused (suppress on)")
        if self._spike.active or self._triggering or self.actions.pending_recheck or self._cal.active:
            raise gcmd.error("filament_force: TEST_RUNOUT refused (busy)")
        if self._load_cell is None:
            raise gcmd.error("filament_force: TEST_RUNOUT refused (no load cell)")
        if not self._armed:
            self._arm()
        forward = force_signal.forward_e_for_test_runout(
            retract, self.detection_length, self.confirm_windows
        )
        retract_f = retract_speed * 60.0
        forward_f = speed * 60.0
        self._ff_log(
            "test_runout",
            {
                "tool": self.active_tool,
                "retract": f"{retract:.2f}",
                "forward_mm": f"{forward:.2f}",
                "speed": f"{speed:.2f}",
                "retract_speed": f"{retract_speed:.2f}",
                "temp": f"{temp:.0f}",
            },
        )
        sensor = self.extruder_name
        script = (
            f"M104 S{temp:.0f}\n"
            f"TEMPERATURE_WAIT SENSOR={sensor} MINIMUM={temp:.0f}\n"
            "SAVE_GCODE_STATE NAME=_FILAMENT_FORCE_TEST_RUNOUT\n"
            "M83\n"
            f"G1 E-{retract:.3f} F{retract_f:.1f}\n"
            "M400\n"
            f"G1 E{forward:.3f} F{forward_f:.1f}\n"
            "M400\n"
            "RESTORE_GCODE_STATE NAME=_FILAMENT_FORCE_TEST_RUNOUT\n"
        )
        self.gcode.run_script_from_command(script)

    cmd_FILAMENT_FORCE_CAL_OH_SHIT_help = (
        "Cool the hotend, then extrude into the cold melt zone and set "
        "oh_shit_force from the jam peak. "
        "FILAMENT_FORCE_CAL_OH_SHIT [MAX_TEMP=50] [EXTRUDE=8] [SPEED=2] "
        "[MARGIN=0.85] [APPLY=0|1]"
    )

    def cmd_FILAMENT_FORCE_CAL_OH_SHIT(self, gcmd: GCodeCommand) -> None:
        max_temp = gcmd.get_float("MAX_TEMP", 50.0, above=0.0)
        extrude = gcmd.get_float("EXTRUDE", 8.0, above=0.0)
        speed = gcmd.get_float("SPEED", 2.0, above=0.0)
        margin = gcmd.get_float("MARGIN", 0.85, above=0.0, maxval=1.0)
        apply = bool(gcmd.get_int("APPLY", 1, minval=0, maxval=1))
        if self.suppress:
            raise gcmd.error("filament_force: CAL_OH_SHIT refused (suppress on)")
        if self._spike.active or self._triggering or self.actions.pending_recheck:
            raise gcmd.error("filament_force: CAL_OH_SHIT refused (busy)")
        if self._cal.active:
            raise gcmd.error("filament_force: CAL_OH_SHIT refused (already running)")
        if self._load_cell is None:
            raise gcmd.error("filament_force: CAL_OH_SHIT refused (no load cell)")
        if not self._client_attached:
            self._load_cell.add_client(self._force_client)
            self._client_attached = True
        self._cal.begin(margin=margin, apply=apply)
        self._window.reset()
        dwell_ms = max(400, int(self.baseline_time * 1000.0) + 250)
        feed = max(1.0, speed) * 60.0
        relieve = min(2.0, extrude)
        sensor = self.extruder_name
        gcmd.respond_info(
            f"filament_force: cooling to {max_temp:.0f}C, then cold-extrude "
            f"{extrude:.1f}mm at {speed:.1f}mm/s"
        )
        script = (
            "M104 S0\n"
            f"TEMPERATURE_WAIT SENSOR={sensor} MAXIMUM={max_temp:.0f}\n"
            "M400\n"
            "SAVE_GCODE_STATE NAME=_FILAMENT_FORCE_CAL_OH_SHIT\n"
            "M83\n"
            f"G4 P{dwell_ms}\n"
            "_FILAMENT_FORCE_CAL_OH_SHIT_GO\n"
            f"G1 E{extrude:.3f} F{feed:.1f}\n"
            "M400\n"
            "_FILAMENT_FORCE_CAL_OH_SHIT_DONE\n"
            f"G1 E-{relieve:.3f} F{feed:.1f}\n"
            "M400\n"
            "RESTORE_GCODE_STATE NAME=_FILAMENT_FORCE_CAL_OH_SHIT\n"
        )
        restore_cold = allow_cold_extrude(
            getattr(self._extruder, "get_heater", lambda: None)()
            if self._extruder
            else None
        )
        try:
            # Nested under this command. run_script re-locks the gcode mutex
            # and hangs the printer.
            self.gcode.run_script_from_command(script)
        except Exception:
            self._cal.abort()
            raise
        finally:
            restore_cold()
        if self._cal.active:
            self._cal.abort()
            self._cal.outcome = "filament_force: cal did not finish"
        msg = self._cal.outcome or "filament_force: cal did not finish"
        self.last_msg = msg
        gcmd.respond_info(msg)

    def cmd__FILAMENT_FORCE_CAL_OH_SHIT_GO(self, gcmd: GCodeCommand) -> None:
        if not self._cal.active or self._cal.tracking:
            return
        self._cal.start_extrude(self._last_force)
        gcmd.respond_info("filament_force: extruding")

    def cmd__FILAMENT_FORCE_CAL_OH_SHIT_DONE(self, gcmd: GCodeCommand) -> None:
        del gcmd
        if not self._cal.active:
            self._cal.outcome = (
                "filament_force: cal was cancelled during the extrude"
            )
            return
        apply = self._cal.apply
        margin = self._cal.margin
        n_samples = self._cal.n_samples
        peak = self._cal.finish()
        if n_samples <= 0:
            self._cal.outcome = (
                "filament_force: no load-cell samples during the extrude"
            )
            return
        suggested = oh_shit_from_jam_peak(
            peak, self.oh_shit_force, margin=margin
        )
        if suggested is None:
            self._cal.outcome = (
                f"filament_force: peak {peak:.0f}g did not beat the current "
                f"{self.oh_shit_force:.0f}g threshold. Not changed."
            )
            return
        if not apply:
            self._cal.outcome = (
                f"filament_force: peak {peak:.0f}g suggests {suggested:.0f}g "
                f"(APPLY=0). FILAMENT_FORCE_SET OH_SHIT_FORCE={suggested:.0f}"
            )
            return
        self.oh_shit_force = suggested
        configfile = self.printer.lookup_object("configfile", None)
        if configfile is not None:
            configfile.set("filament_force", "oh_shit_force", f"{suggested:.1f}")
        self._cal.outcome = (
            f"filament_force: jam threshold {suggested:.0f}g "
            f"(peak {peak:.0f}g). SAVE_CONFIG to keep it."
        )

    def _trip(self, kind: TripKind, detail: str) -> None:
        if self._triggering or self.actions.pending_recheck:
            return
        if not self.actions.is_printing():
            return
        self._triggering = True
        self._spike.abort()
        try:
            tool = self.active_tool
            target = self.actions.extruder_target()
            self.last_trip = kind
            self.last_msg = f"filament_force {kind.value}: {detail}"
            template_key = (
                MonitorTemplate.RUNOUT
                if kind is TripKind.RUNOUT
                else MonitorTemplate.JAM
            )
            self.actions.run_template_if_set(
                template_key,
                {
                    "TOOL": tool,
                    "REASON": kind.value,
                    "MSG": self.last_msg,
                    "TARGET": target,
                },
            )
            self.actions.soft_pause(
                f"{self.last_msg}. Pausing - fix filament, then RESUME.",
                tool=tool,
                target=target,
                reason=kind.value,
                from_command=False,
            )
        finally:
            self._triggering = False

    def get_status(self, eventtime: float) -> dict[str, Any]:
        del eventtime
        book = self._book_for(self.active_tool)
        hist = book.bin_hist(self._last_bin)
        stats = hist.mean_stdev()
        high_band = book.last_high_band
        if high_band is not None:
            mean_g = high_band.mean
            stdev_g = high_band.stdev
            low_bound = high_band.low_bound
            high_bound = high_band.high_bound
            last_z = high_band.z_score
        else:
            band = self._band(hist)
            mean_g = stats.mean
            stdev_g = stats.stdev
            low_bound = band.low_bound
            high_bound = band.high_bound
            last_z = hist.last_z_score
        return {
            "enabled": 1 if self.enabled else 0,
            "armed": 1 if self._armed else 0,
            "suppress": 1 if self.suppress else 0,
            "active_tool": self.active_tool,
            "learned": 1 if hist.learned else 0,
            "window_count": hist.window_count,
            "last_bin": self._last_bin,
            "mean_level_g": mean_g,
            "stdev_level_g": stdev_g,
            "low_bound_g": low_bound,
            "high_bound_g": high_bound,
            "force_ref_g": self._force_ref,
            "last_force_g": self._last_force,
            "last_level_g": self._last_level_g,
            "last_e_speed": self._last_e_speed,
            "last_z_score": last_z,
            "suspect": 1 if book.jam_suspect else 0,
            "anomalous_streak": book.jam_streak,
            "healthy_streak": book.jam_healthy_streak,
            "last_probe_spike": self._spike.last_spike,
            "last_probe_delta_g": self._spike.last_delta_g,
            "quiet": 1 if self.quiet else 0,
            "debug_log": 1 if self.debug_log else 0,
            "drop_ratio": self.drop_ratio,
            "runout_max_level_g": self.runout_max_level_g,
            "oh_shit_force": self.oh_shit_force,
            "last_oh_shit_cal_peak_g": self._cal.last_peak_g,
            "pending_recheck": self.actions.pending_recheck,
            "pending_tool": self.actions.pending_tool,
            "last_trip": self.last_trip.value if self.last_trip is not None else "",
            "last_msg": self.last_msg,
        }

    cmd_FILAMENT_FORCE_SET_help = (
        "Set filament_force options: "
        "FILAMENT_FORCE_SET [ENABLE=0|1] "
        "[PROBE_SPIKE_G=] [PROBE_RETRACT_MM=] [PROBE_EXTRA_PRIME_MM=] "
        "[PROBE_FEEDRATE=] [QUIET=0|1] [DEBUG_LOG=0|1] "
        "[MIN_DELTA_G=] [DROP_RATIO=] [RUNOUT_MAX_LEVEL_G=] "
        "[HIGH_SIGMA=] [CONFIRM_WINDOWS=] [RECOVER_WINDOWS=] "
        "[DETECTION_LENGTH=] [MIN_E_SPEED=] [OH_SHIT_FORCE=] "
        "[IDLE_RESET_S=] [MIN_LEARN_S=] [SPEED_BINS=] "
        "[HISTORY_N=] [MIN_LEARN_WINDOWS=]"
    )

    def cmd_FILAMENT_FORCE_SET(self, gcmd: GCodeCommand) -> None:
        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        if enable is not None:
            self.enabled = bool(enable)
            if self.enabled and self.actions.in_print_job():
                self._arm()
            elif not self.enabled:
                self._disarm()

        quiet = gcmd.get_int("QUIET", None, minval=0, maxval=1)
        if quiet is not None:
            self.quiet = bool(quiet)
        debug_log = gcmd.get_int("DEBUG_LOG", None, minval=0, maxval=1)
        if debug_log is not None:
            self.debug_log = bool(debug_log)

        spike = gcmd.get_float("PROBE_SPIKE_G", None, above=0.0)
        if spike is not None:
            self.probe_spike_g = spike
        retract = gcmd.get_float("PROBE_RETRACT_MM", None, above=0.0)
        if retract is not None:
            self.probe_retract_mm = retract
        extra = gcmd.get_float("PROBE_EXTRA_PRIME_MM", None, minval=0.0)
        if extra is not None:
            self.probe_extra_prime_mm = extra
        feed = gcmd.get_float("PROBE_FEEDRATE", None, above=0.0)
        if feed is not None:
            self.probe_feedrate = feed

        min_delta = gcmd.get_float("MIN_DELTA_G", None, above=0.0)
        if min_delta is not None:
            self.min_delta_g = min_delta
        drop_ratio = gcmd.get_float("DROP_RATIO", None, minval=0.0, maxval=1.0)
        if drop_ratio is not None:
            self.drop_ratio = drop_ratio
        runout_max = gcmd.get_float("RUNOUT_MAX_LEVEL_G", None, minval=0.0)
        if runout_max is not None:
            self.runout_max_level_g = runout_max
        high_sigma = gcmd.get_float("HIGH_SIGMA", None, above=0.0)
        if high_sigma is not None:
            self.high_sigma = high_sigma
        confirm = gcmd.get_int("CONFIRM_WINDOWS", None, minval=1)
        if confirm is not None:
            self.confirm_windows = confirm
        recover = gcmd.get_int("RECOVER_WINDOWS", None, minval=1)
        if recover is not None:
            self.recover_windows = recover
        det_len = gcmd.get_float("DETECTION_LENGTH", None, above=0.0)
        if det_len is not None:
            self.detection_length = det_len
            self._window.detection_length = det_len
            self._window.reset()
        min_e = gcmd.get_float("MIN_E_SPEED", None, above=0.0)
        if min_e is not None:
            self.min_e_speed = min_e
        oh_shit = gcmd.get_float("OH_SHIT_FORCE", None, above=0.0)
        if oh_shit is not None:
            self.oh_shit_force = oh_shit
        idle_reset = gcmd.get_float("IDLE_RESET_S", None, above=0.0)
        if idle_reset is not None:
            self.idle_reset_s = idle_reset
        min_learn_s = gcmd.get_float("MIN_LEARN_S", None, minval=0.0)
        if min_learn_s is not None:
            self.min_learn_s = min_learn_s

        rebuild = False
        bins_raw = gcmd.get("SPEED_BINS", None)
        if bins_raw is not None:
            try:
                edges = tuple(
                    float(part.strip())
                    for part in bins_raw.split(",")
                    if part.strip()
                )
                self.speed_bin_edges = force_signal.normalise_speed_bin_edges(
                    edges
                )
            except ValueError as exc:
                raise gcmd.error(f"filament_force: {exc}")
            rebuild = True
        history_n = gcmd.get_int("HISTORY_N", None, minval=5)
        if history_n is not None:
            self.history_n = history_n
            rebuild = True
        min_learn_windows = gcmd.get_int("MIN_LEARN_WINDOWS", None, minval=2)
        if min_learn_windows is not None:
            self.min_learn_windows = min_learn_windows
            rebuild = True
        if rebuild:
            self._rebuild_books()

        edges_s = ",".join(f"{e:g}" for e in self.speed_bin_edges)
        state = "on" if self.enabled else "off"
        gcmd.respond_info(
            f"filament_force: {state} tool={self.active_tool}\n"
            f"  drop_ratio={self.drop_ratio:.2f}"
            f" runout_max_level_g={self.runout_max_level_g:.1f}"
            f" confirm={self.confirm_windows}"
            f" recover={self.recover_windows}\n"
            f"  check_spike: spike_g={self.probe_spike_g:.1f}"
            f" retract={self.probe_retract_mm:.2f}mm"
            f" extra_prime={self.probe_extra_prime_mm:.2f}mm"
            f" feed={self.probe_feedrate:.1f}mm/s\n"
            f"  high_sigma={self.high_sigma:.1f}"
            f" detection_length={self.detection_length:.1f}mm"
            f" min_e_speed={self.min_e_speed:.2f}"
            f" oh_shit_force={self.oh_shit_force:.1f}\n"
            f"  speed_bins={edges_s}"
            f" idle_reset_s={self.idle_reset_s:.2f}"
            f" min_learn_s={self.min_learn_s:.2f}\n"
            f"  history_n={self.history_n}"
            f" min_learn_windows={self.min_learn_windows}"
            f" quiet={'on' if self.quiet else 'off'}"
            f" debug_log={'on' if self.debug_log else 'off'}"
        )

    cmd_FILAMENT_FORCE_QUERY_help = (
        "Report per-tool filament force bands and suspect state"
    )

    def cmd_FILAMENT_FORCE_QUERY(self, gcmd: GCodeCommand) -> None:
        tool = gcmd.get_int("TOOL", self.active_tool)
        book = self._book_for(tool)
        hist = book.bin_hist(self._last_bin)
        stats = hist.mean_stdev()
        high_band = book.last_high_band
        if high_band is not None:
            mean_g = high_band.mean
            stdev_g = high_band.stdev
            low_bound = high_band.low_bound
            high_bound = high_band.high_bound
            last_z = high_band.z_score
            last_kind = high_band.kind.value
        else:
            band = self._band(hist)
            mean_g = stats.mean
            stdev_g = stats.stdev
            low_bound = band.low_bound
            high_bound = band.high_bound
            last_z = hist.last_z_score
            last_kind = hist.last_kind.value
        learned = "yes" if hist.learned else "no"
        run_stats = book.runout.mean_stdev()
        run_band = self._band(book.runout)
        bin_lines = []
        for i, bhist in enumerate(book.bins):
            bstats = bhist.mean_stdev()
            bband = self._band(bhist)
            bin_lines.append(
                f"    {i} n={bhist.window_count}/{self.min_learn_windows}"
                f" learned={'yes' if bhist.learned else 'no'}"
                f" mean={bstats.mean:.1f}"
                f" high_bound={bband.high_bound:.1f}"
            )
        bins_block = "\n".join(bin_lines)
        gcmd.respond_info(
            "filament_force:\n"
            f"  enabled={'on' if self.enabled else 'off'}"
            f" armed={'yes' if self._armed else 'no'}"
            f" suppress={'yes' if self.suppress else 'no'}\n"
            f"  tool={tool} last_bin={self._last_bin}"
            f" learned={learned}"
            f" windows={hist.window_count}/{self.min_learn_windows}\n"
            f"  mean={mean_g:.1f}g stdev={stdev_g:.1f}g"
            f" band=[{low_bound:.1f},{high_bound:.1f}]g"
            f" drop_ratio={self.drop_ratio:.2f}"
            f" runout_max={self.runout_max_level_g:.1f}\n"
            f"  bins:\n{bins_block}\n"
            f"  runout n={book.runout.window_count}/{self.min_learn_windows}"
            f" learned={'yes' if book.runout.learned else 'no'}"
            f" mean={run_stats.mean:.1f}"
            f" low_bound={run_band.low_bound:.1f}\n"
            f"  ref={self._force_ref:.1f}g"
            f" locked={'yes' if self._force_ref_locked else 'no'}"
            f" last_force={self._last_force:.1f}g\n"
            f"  last_level={hist.last_level:.1f}g"
            f" e_speed={self._last_e_speed:.2f}mm/s"
            f" z={last_z:.2f} kind={last_kind}\n"
            f"  suspect={'yes' if book.jam_suspect else 'no'}"
            f" anom_streak={book.jam_streak}"
            f" healthy_streak={book.jam_healthy_streak}\n"
            f"  check_spike={'yes' if self._spike.active else 'no'}"
            f" last_spike={'yes' if self._spike.last_spike else 'no'}"
            f" spike_delta={self._spike.last_delta_g:.1f}g\n"
            f"  pending={self.actions.pending_recheck}"
            f" last_trip={self.last_trip.value if self.last_trip else '-'}"
        )

    cmd_FILAMENT_FORCE_SET_TOOL_help = (
        "Bind active tool id for per-tool history: FILAMENT_FORCE_SET_TOOL TOOL=n"
    )

    def cmd_FILAMENT_FORCE_SET_TOOL(self, gcmd: GCodeCommand) -> None:
        tool = gcmd.get_int("TOOL")
        self.active_tool = tool
        self._window.reset()
        self._unlock_baseline()
        self._unretract_remaining = 0.0
        self._after_idle = False
        book = self._book_for(tool)
        gcmd.respond_info(
            f"filament_force: active tool T{tool}"
            f" (bins={book.n_bins},"
            f" runout_learned={'yes' if book.runout.learned else 'no'},"
            f" runout_windows={book.runout.window_count})"
        )

    cmd_FILAMENT_FORCE_RESET_help = (
        "Clear per-tool history, pending trip lockout, and re-enter learn mode: "
        "FILAMENT_FORCE_RESET [TOOL=n]"
    )

    def cmd_FILAMENT_FORCE_RESET(self, gcmd: GCodeCommand) -> None:
        tool = gcmd.get_int("TOOL", self.active_tool)
        book = self._book_for(tool)
        book.reset()
        self._window.reset()
        self._spike.abort()
        self._spike.last_spike = 0
        self._spike.last_delta_g = 0.0
        self._cal.abort()
        self.actions.clear_pending()
        self.last_trip = None
        self.last_msg = ""
        self._unlock_baseline()
        self._unretract_remaining = 0.0
        self._last_bin = 0
        gcmd.respond_info(
            f"filament_force: reset history for T{tool} (pending cleared)"
        )

    cmd_FILAMENT_FORCE_RESUME_help = (
        "Resume after a filament_force soft pause (runs recover_gcode then resume)"
    )

    def cmd_FILAMENT_FORCE_RESUME(self, gcmd: GCodeCommand) -> None:
        velocity = gcmd.get("VELOCITY", None)
        if not self.actions.pending_recheck:
            if self.actions.in_print_job():
                self.actions.do_resume(velocity)
            else:
                gcmd.respond_info("filament_force: nothing pending")
            return
        tool = self.actions.pending_tool
        target = self.actions.pending_target
        reason = self.actions.pending_reason
        self.actions.clear_pending()
        book = self._book_for(tool if tool >= 0 else self.active_tool)
        book.clear_streaks()
        self._window.reset()
        self._spike.abort()
        self.actions.run_template_if_set(
            MonitorTemplate.RECOVER,
            {
                "TOOL": tool,
                "TARGET": target,
                "REASON": reason,
                "MSG": self.last_msg,
            },
        )
        if not self.actions.in_print_job():
            gcmd.respond_info(
                f"filament_force: cleared {reason or 'pending'} lockout"
                " (not printing, skip RESUME)"
            )
            return
        gcmd.respond_info(
            f"filament_force: clearing {reason or 'pending'} pause, resuming"
        )
        self.actions.do_resume(velocity)

    cmd_FILAMENT_FORCE_SUPPRESS_help = (
        "Suppress scoring during probe/toolchange: "
        "FILAMENT_FORCE_SUPPRESS ENABLE=0|1"
    )

    def cmd_FILAMENT_FORCE_SUPPRESS(self, gcmd: GCodeCommand) -> None:
        enable = gcmd.get_int("ENABLE", minval=0, maxval=1)
        self.suppress = bool(enable)
        self._window.reset()
        self._e_idle_since = self.reactor.monotonic()
        if not self.suppress:
            self._unlock_baseline()
            self._unretract_remaining = 0.0
            self._after_idle = False
        gcmd.respond_info(
            f"filament_force: suppress={'on' if self.suppress else 'off'}"
        )


def load_config(config: ConfigWrapper) -> FilamentForce:
    return FilamentForce(config)
