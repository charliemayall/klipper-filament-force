"""Unit tests for pure force-window / per-tool anomaly logic."""

from filament_force.filament_force import OhShitCal, allow_cold_extrude
from filament_force.force_signal import (
    AnomalyKind,
    DEFAULT_SPEED_BIN_EDGES,
    EWindowAccumulator,
    Sample,
    ToolForceBook,
    ToolForceHistory,
    TripKind,
    deretract_has_spike,
    expected_force_stats,
    force_ref_from_idle_samples,
    gate_unretract,
    is_short_bead,
    iter_load_cell_samples,
    oh_shit_from_jam_peak,
    score_level,
    speed_bin_count,
    speed_bin_index,
    forward_e_for_test_runout,
)


class TestIterLoadCellSamples:
    def test_kalico_dict_payload(self) -> None:
        msg = {
            "data": [
                [1.0, -120.5, 100, 50],
                [1.01, -130.0, 110, 50],
            ],
            "errors": 0,
            "overflows": 0,
        }
        samples = iter_load_cell_samples(msg)
        assert samples == [Sample(1.0, -120.5), Sample(1.01, -130.0)]

    def test_bare_list_is_not_a_payload(self) -> None:
        assert iter_load_cell_samples([[2.0, 5.0], [2.1, 6.0]]) == []

    def test_dict_keys_are_not_treated_as_rows(self) -> None:
        # Iterating the dict itself yields "data"/"errors" strings.
        assert iter_load_cell_samples({"data": [], "errors": 0}) == []
        assert iter_load_cell_samples({"errors": 0, "overflows": 0}) == []

    def test_from_raw_minimal_and_wide_rows(self) -> None:
        assert Sample.from_raw([2.0, 5.0]) == Sample(2.0, 5.0)
        assert Sample.from_raw([1.0, -120.5, 100, 50]) == Sample(1.0, -120.5)


class TestTripKind:
    def test_from_anomaly(self) -> None:
        assert TripKind.from_anomaly(AnomalyKind.HIGH) is TripKind.JAM
        assert TripKind.from_anomaly(AnomalyKind.LOW) is TripKind.RUNOUT
        assert TripKind.from_anomaly(AnomalyKind.NONE) is None


class TestDeretractSpike:
    def test_spike_from_peak(self) -> None:
        assert deretract_has_spike(-100.0, -250.0, -120.0, spike_g=50.0) is True

    def test_spike_from_force2(self) -> None:
        assert deretract_has_spike(0.0, 10.0, 80.0, spike_g=50.0) is True

    def test_no_spike(self) -> None:
        assert deretract_has_spike(-100.0, -105.0, -102.0, spike_g=50.0) is False


class TestScoreLevel:
    def test_in_band(self) -> None:
        band = score_level(
            100.0, 100.0, 10.0, high_sigma=4.0, min_delta_g=15.0
        )
        assert band.kind is AnomalyKind.NONE

    def test_low_true_empty(self) -> None:
        # mean 180, drop_ratio 0.35 → 63; abs cap 80 → low_bound 63
        band = score_level(
            20.0,
            180.0,
            25.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert band.low_bound == min(180.0 * 0.35, 80.0)
        assert band.kind is AnomalyKind.LOW

    def test_relative_low_but_above_abs_floor_is_not_runout(self) -> None:
        # Inflated mean 580: ratio cap 203, abs 80 → bound 80.
        # Level 200 is "relative low" but still above abs floor.
        band = score_level(
            200.0,
            580.0,
            50.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert band.low_bound == 80.0
        assert band.kind is AnomalyKind.NONE

    def test_high(self) -> None:
        band = score_level(
            200.0, 100.0, 10.0, high_sigma=4.0, min_delta_g=15.0
        )
        assert band.kind is AnomalyKind.HIGH

    def test_true_empty_under_abs_even_with_high_mean(self) -> None:
        band = score_level(
            50.0,
            2531.6,
            9.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert band.low_bound == 80.0
        assert band.kind is AnomalyKind.LOW

    def test_untared_preload_is_high_against_filament_band(self) -> None:
        # |F-0| ~= cell preload. Poll must not lock ref=0 or this trips.
        band = score_level(
            2171.3, 392.0, 10.0, high_sigma=4.0, min_delta_g=15.0
        )
        assert band.kind is AnomalyKind.HIGH
        assert band.z_score > 50.0


class TestForceRefFromIdleSamples:
    def test_no_samples_does_not_fake_zero(self) -> None:
        assert force_ref_from_idle_samples(0, 0.0) is None

    def test_mean_of_idle_samples(self) -> None:
        assert force_ref_from_idle_samples(2, -4800.0) == -2400.0


class TestToolForceHistory:
    def _learned(self, level: float = 180.0) -> ToolForceHistory:
        hist = ToolForceHistory(history_n=20, min_learn_windows=5)
        for _ in range(5):
            trip = hist.observe(
                level,
                high_sigma=4.0,
                min_delta_g=15.0,
                confirm_windows=3,
                recover_windows=2,
                drop_ratio=0.35,
                runout_max_level_g=80.0,
            )
            assert trip is None
        assert hist.learned
        return hist

    def test_learn_does_not_trip(self) -> None:
        hist = ToolForceHistory(history_n=10, min_learn_windows=4)
        for v in (0.0, 0.0, 0.0, 0.0):
            assert (
                hist.observe(
                    v,
                    high_sigma=4.0,
                    min_delta_g=15.0,
                    confirm_windows=1,
                    recover_windows=1,
                )
                is None
            )

    def test_confirm_windows_before_trip(self) -> None:
        hist = self._learned()
        for _ in range(2):
            assert (
                hist.observe(
                    20.0,
                    high_sigma=4.0,
                    min_delta_g=15.0,
                    confirm_windows=3,
                    recover_windows=2,
                    drop_ratio=0.35,
                    runout_max_level_g=80.0,
                )
                is None
            )
            assert hist.suspect
        trip = hist.observe(
            20.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert trip is AnomalyKind.LOW

    def test_false_relative_low_does_not_trip(self) -> None:
        hist = self._learned(level=580.0)
        for _ in range(3):
            assert (
                hist.observe(
                    200.0,
                    high_sigma=4.0,
                    min_delta_g=15.0,
                    confirm_windows=3,
                    recover_windows=2,
                    drop_ratio=0.35,
                    runout_max_level_g=80.0,
                )
                is None
            )
        assert not hist.suspect

    def test_single_drop_then_recover_no_trip(self) -> None:
        hist = self._learned()
        assert (
            hist.observe(
                20.0,
                high_sigma=4.0,
                min_delta_g=15.0,
                confirm_windows=3,
                recover_windows=2,
                drop_ratio=0.35,
                runout_max_level_g=80.0,
            )
            is None
        )
        assert hist.suspect
        hist.observe(
            180.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        hist.observe(
            180.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert not hist.suspect

    def test_recover_windows_clears_suspect(self) -> None:
        hist = self._learned()
        hist.observe(
            20.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert hist.suspect
        hist.observe(
            180.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert hist.suspect
        hist.observe(
            180.0,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
        )
        assert not hist.suspect

    def test_reset_clears_learn(self) -> None:
        hist = self._learned()
        hist.reset()
        assert not hist.learned
        assert hist.window_count == 0


class TestEWindowAccumulator:
    def test_window_spans_moves_with_mean_level(self) -> None:
        win = EWindowAccumulator(detection_length=5.0, rolling_window_s=0.05)
        # Ref 0; extruding force -600 across several tiny de steps → ~600.
        assert win.add_forward_e(1.0, force_ref=0.0) is None
        for i in range(10):
            win.add_force(Sample(0.01 * i, -600.0))
        assert win.add_forward_e(1.5, force_ref=999.0) is None  # locked; ignore
        for i in range(10):
            win.add_force(Sample(0.2 + 0.01 * i, -580.0))
        completed = win.add_forward_e(2.5, force_ref=999.0)
        assert completed is not None
        assert completed.level > 500.0

    def test_mid_window_runout_lowers_mean_not_just_end(self) -> None:
        win = EWindowAccumulator(detection_length=4.0, rolling_window_s=0.05)
        win.start(0.0)
        # Loaded half
        for i in range(20):
            win.add_force(Sample(0.01 * i, -600.0))
        win.add_forward_e(2.0)
        # Empty half
        for i in range(20):
            win.add_force(Sample(1.0 + 0.01 * i, -20.0))
        completed = win.add_forward_e(2.0)
        assert completed is not None
        # Mean should sit between empty and loaded; peak alone would stay ~600.
        assert 200.0 < completed.level < 500.0

        win2 = EWindowAccumulator(detection_length=4.0, rolling_window_s=0.05)
        win2.start(0.0)
        for i in range(20):
            win2.add_force(Sample(0.01 * i, -600.0))
        win2.add_forward_e(2.0)
        for i in range(20):
            win2.add_force(Sample(1.0 + 0.01 * i, -20.0))
        completed2 = win2.add_forward_e(2.0)
        assert completed2 is not None
        assert completed2.level < 500.0

    def test_empty_window_without_force_samples_skips(self) -> None:
        win = EWindowAccumulator(detection_length=2.0, rolling_window_s=0.05)
        assert win.add_forward_e(2.5, force_ref=0.0) is None

    def test_arm_ref_subtracts_preload(self) -> None:
        win = EWindowAccumulator(detection_length=3.0, rolling_window_s=0.05)
        win.start(force_ref=-2000.0)
        for i in range(15):
            win.add_force(Sample(0.01 * i, -2600.0))  # 600g filament on preload
        completed = win.add_forward_e(3.0)
        assert completed is not None
        assert 500.0 < completed.level < 700.0

    def test_grams_not_divided_by_e_speed(self) -> None:
        win = EWindowAccumulator(detection_length=3.0, rolling_window_s=0.05)
        win.start(0.0)
        for i in range(15):
            win.add_force(Sample(0.01 * i, -500.0))
        completed = win.add_forward_e(3.0, e_speed=5.0)
        assert completed is not None
        assert abs(completed.level - 500.0) < 1.0
        assert abs(completed.e_speed - 5.0) < 1e-9

    def test_idle_reset_drops_partial_window(self) -> None:
        win = EWindowAccumulator(detection_length=4.0, rolling_window_s=0.05)
        win.start(0.0)
        for i in range(10):
            win.add_force(Sample(0.01 * i, -400.0))
        assert win.add_forward_e(1.5, e_speed=2.0, eventtime=1.0) is None
        assert win.e_accum > 0.0
        assert win.note_idle(0.2, 0.3) is False
        assert win.e_accum > 0.0
        assert win.note_idle(0.4, 0.3) is True
        assert win.e_accum == 0.0
        assert win.active is False

    def test_t_win_is_first_to_last_de(self) -> None:
        win = EWindowAccumulator(detection_length=4.0, rolling_window_s=0.05)
        win.start(0.0)
        for i in range(10):
            win.add_force(Sample(0.01 * i, -400.0))
        assert win.add_forward_e(2.0, e_speed=2.0, eventtime=1.0) is None
        completed = win.add_forward_e(2.0, e_speed=2.0, eventtime=2.5)
        assert completed is not None
        assert abs(completed.t_win - 1.5) < 1e-9


class TestGateUnretract:
    def test_retract_then_matching_prime_ignored(self) -> None:
        # Matches mock pattern: E-1.2 then E0.6 prime then E2.0 print.
        fwd, rem, reset = gate_unretract(-1.2, 0.0)
        assert fwd == 0.0 and rem == 1.2 and reset is True
        fwd, rem, reset = gate_unretract(0.6, rem)
        assert fwd == 0.0 and rem == 0.6 and reset is False
        fwd, rem, reset = gate_unretract(2.0, rem)
        assert abs(fwd - 1.4) < 1e-9 and rem == 0.0 and reset is False

    def test_stacked_retracts(self) -> None:
        _, rem, _ = gate_unretract(-0.8, 0.0)
        _, rem, _ = gate_unretract(-0.4, rem)
        assert abs(rem - 1.2) < 1e-9
        fwd, rem, _ = gate_unretract(1.2, rem)
        assert fwd == 0.0 and abs(rem) < 1e-9

    def test_forward_without_retract_passthrough(self) -> None:
        fwd, rem, reset = gate_unretract(3.0, 0.0)
        assert fwd == 3.0 and rem == 0.0 and reset is False


class TestShortBeadGate:
    def test_continuous_fast_window_is_not_short_bead(self) -> None:
        assert (
            is_short_bead(0.25, after_idle=False, min_learn_s=1.5) is False
        )

    def test_after_idle_short_window_is_short_bead(self) -> None:
        assert is_short_bead(0.25, after_idle=True, min_learn_s=1.5) is True

    def test_after_idle_long_window_learns(self) -> None:
        assert is_short_bead(2.0, after_idle=True, min_learn_s=1.5) is False


class TestSpeedBins:
    def test_bin_index_from_e_speed(self) -> None:
        edges = DEFAULT_SPEED_BIN_EDGES
        assert speed_bin_count(edges) == 5
        assert speed_bin_index(0.50, edges) == 0
        assert speed_bin_index(0.99, edges) == 0
        assert speed_bin_index(1.0, edges) == 1
        assert speed_bin_index(1.99, edges) == 1
        assert speed_bin_index(2.0, edges) == 2
        assert speed_bin_index(3.99, edges) == 2
        assert speed_bin_index(4.0, edges) == 3
        assert speed_bin_index(6.99, edges) == 3
        assert speed_bin_index(7.0, edges) == 4
        assert speed_bin_index(12.0, edges) == 4


class TestToolForceBook:
    def _book(self) -> ToolForceBook:
        return ToolForceBook(n_bins=5, history_n=16, min_learn_windows=5)

    def _obs(
        self,
        book: ToolForceBook,
        raw_g: float,
        e_speed: float,
        *,
        short_bead: bool,
        trip_high: bool = True,
    ) -> AnomalyKind | None:
        return book.observe(
            raw_g,
            e_speed,
            DEFAULT_SPEED_BIN_EDGES,
            short_bead=short_bead,
            high_sigma=4.0,
            min_delta_g=15.0,
            confirm_windows=3,
            recover_windows=2,
            drop_ratio=0.35,
            runout_max_level_g=80.0,
            trip_high=trip_high,
        )

    def test_unlearned_faster_bin_does_not_high(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 200.0, 0.5, short_bead=False) is None
        assert book.bins[0].learned
        assert not book.bins[4].learned
        for _ in range(3):
            trip = self._obs(book, 2000.0, 8.0, short_bead=False)
            assert trip is None
        assert book.bins[4].window_count == 3
        assert not book.bins[4].learned
        assert expected_force_stats(
            book.bins, 8.0, DEFAULT_SPEED_BIN_EDGES
        ) is None

    def test_learn_gate_is_per_bin(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 0.5, short_bead=False) is None
        # 0.9 * 400 = 360; 200g would not append on bin 0.
        assert self._obs(book, 200.0, 0.5, short_bead=False) is None
        assert book.bins[0].window_count == 5
        # Same 200g still learns in empty bin 1.
        assert self._obs(book, 200.0, 1.5, short_bead=False) is None
        assert book.bins[1].window_count == 1

    def test_min_learn_s_does_not_poison_history(self) -> None:
        book = self._book()
        assert self._obs(book, 50.0, 3.0, short_bead=True) is None
        assert book.bins[2].window_count == 0
        assert book.runout.window_count == 0
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        assert book.bins[2].window_count == 5
        assert self._obs(book, 50.0, 3.0, short_bead=True) is None
        assert book.bins[2].window_count == 5
        assert book.runout.window_count == 5

    def test_short_bead_does_not_high_on_learned_bin(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        for _ in range(5):
            assert self._obs(book, 4000.0, 3.0, short_bead=True) is None
        assert book.bins[2].window_count == 5
        assert book.jam_streak == 0

    def test_high_does_not_learn_into_bin(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        assert self._obs(book, 4000.0, 3.0, short_bead=False) is None
        assert book.bins[2].window_count == 5

    def test_learned_bin_high_still_trips(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        assert self._obs(book, 4000.0, 3.0, short_bead=False) is None
        assert self._obs(book, 4000.0, 3.0, short_bead=False) is None
        assert self._obs(book, 4000.0, 3.0, short_bead=False) is AnomalyKind.HIGH

    def test_sigma_jam_off_skips_high_keeps_runout(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        for _ in range(3):
            assert (
                self._obs(book, 4000.0, 3.0, short_bead=False, trip_high=False)
                is None
            )
        assert book.jam_streak == 0
        assert book.last_high_band is not None
        assert book.last_high_band.kind is AnomalyKind.HIGH
        assert self._obs(book, 10.0, 3.0, short_bead=False, trip_high=False) is None
        assert self._obs(book, 10.0, 3.0, short_bead=False, trip_high=False) is None
        assert (
            self._obs(book, 10.0, 3.0, short_bead=False, trip_high=False)
            is AnomalyKind.LOW
        )

    def test_runout_is_global_across_unlearned_bin(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 0.5, short_bead=False) is None
        assert self._obs(book, 10.0, 8.0, short_bead=False) is None
        assert self._obs(book, 10.0, 8.0, short_bead=False) is None
        trip = self._obs(book, 10.0, 8.0, short_bead=False)
        assert trip is AnomalyKind.LOW
        assert not book.bins[4].learned

    def test_healthy_fast_bin_not_high_from_slower_neighbour(self) -> None:
        # Perimeters at 3 mm/s learn 300g; infill at 5.5 mm/s learns 700g.
        # 680g just inside the infill bin must not HIGH (lerp toward 300g
        # used to trip this as a jam).
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 300.0, 3.0, short_bead=False) is None
        for _ in range(5):
            assert self._obs(book, 700.0, 5.5, short_bead=False) is None
        stats = expected_force_stats(
            book.bins, 4.05, DEFAULT_SPEED_BIN_EDGES
        )
        assert stats is not None
        assert abs(stats.mean - 700.0) < 1e-6
        for _ in range(3):
            assert self._obs(book, 680.0, 4.05, short_bead=False) is None
        assert book.jam_streak == 0

    def test_jam_confirm_counts_across_bin_edge(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        for _ in range(5):
            assert self._obs(book, 400.0, 5.5, short_bead=False) is None
        assert self._obs(book, 4000.0, 3.99, short_bead=False) is None
        assert self._obs(book, 4000.0, 4.01, short_bead=False) is None
        trip = self._obs(book, 4000.0, 3.99, short_bead=False)
        assert trip is AnomalyKind.HIGH

    def test_unlearned_assigned_bin_does_not_high(self) -> None:
        book = self._book()
        for _ in range(5):
            assert self._obs(book, 400.0, 3.0, short_bead=False) is None
        for _ in range(5):
            assert self._obs(book, 400.0, 8.0, short_bead=False) is None
        for _ in range(3):
            assert self._obs(book, 4000.0, 5.0, short_bead=False) is None
        assert not book.bins[3].learned
        assert expected_force_stats(
            book.bins, 5.0, DEFAULT_SPEED_BIN_EDGES
        ) is None


class TestTestRunoutForward:
    def test_covers_unretract_and_confirm_plus_one(self) -> None:
        # retract 8, detection 4, confirm 3 -> 8 + 4*4 = 24
        assert forward_e_for_test_runout(8.0, 4.0, 3) == 24.0


class TestOhShitFromJamPeak:
    def test_margin_of_peak_when_above_current(self) -> None:
        assert oh_shit_from_jam_peak(3000.0, 1000.0) == 1500.0

    def test_refuses_when_suggested_would_lower(self) -> None:
        # 0.5 * 3000 = 1500, below current 1800
        assert oh_shit_from_jam_peak(3000.0, 1800.0) is None

    def test_full_margin_still_requires_peak_above_current(self) -> None:
        assert oh_shit_from_jam_peak(2000.0, 1800.0, margin=1.0) == 2000.0
        assert oh_shit_from_jam_peak(1800.0, 1800.0, margin=1.0) is None

    def test_rejects_bad_margin(self) -> None:
        try:
            oh_shit_from_jam_peak(3000.0, 1800.0, margin=0.0)
        except ValueError:
            return
        raise AssertionError("expected ValueError")


class TestOhShitCal:
    def test_idle_mean_then_peak_abs_delta(self) -> None:
        cal = OhShitCal()
        cal.begin(margin=0.85, apply=True)
        cal.note(-2000.0)
        cal.note(-2000.0)
        cal.start_extrude(0.0)
        assert cal.ref == -2000.0
        cal.note(-2000.0)
        cal.note(-4500.0)
        cal.note(-4000.0)
        peak = cal.finish()
        assert peak == 2500.0
        assert cal.last_peak_g == 2500.0
        assert cal.active is False

    def test_abort_clears_active(self) -> None:
        cal = OhShitCal()
        cal.begin(margin=0.85, apply=True)
        cal.start_extrude(0.0)
        cal.abort()
        assert cal.active is False
        assert cal.tracking is False

    def test_begin_clears_outcome(self) -> None:
        cal = OhShitCal()
        cal.outcome = "stale"
        cal.begin(margin=0.85, apply=True)
        assert cal.outcome == ""


class TestAllowColdExtrude:
    def test_none_is_noop(self) -> None:
        allow_cold_extrude(None)()

    def test_kalico_setter_restores(self) -> None:
        class Heater:
            cold_extrude = False
            can_extrude = False

            def set_cold_extrude(self, cold_extrude: object, min_extrude_temp: object) -> None:
                del min_extrude_temp
                self.cold_extrude = bool(cold_extrude)
                self.can_extrude = self.cold_extrude

        heater = Heater()
        restore = allow_cold_extrude(heater)
        assert heater.cold_extrude is True
        restore()
        assert heater.cold_extrude is False

    def test_klipper_min_temp_restores(self) -> None:
        class Heater:
            min_extrude_temp = 170.0
            can_extrude = False
            smoothed_temp = 25.0

        heater = Heater()
        restore = allow_cold_extrude(heater)
        assert heater.min_extrude_temp == 0.0
        assert heater.can_extrude is True
        restore()
        assert heater.min_extrude_temp == 170.0
        assert heater.can_extrude is False
