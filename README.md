# filament_force

E-gated filament health for Klipper / Kalico: runout (sustained forward-E
force drop) and jam (high force vs a per e-speed-bin notebook), using the
toolhead load cell.

Layout:

- `filament_force.py` - `[filament_force]` extra
- `force_signal.py` - pure force-window / anomaly helpers
- `pause.py` - soft-pause / resume / gcode templates

## Install

```sh
./install.sh [~/klipper]
```

Symlinks `src/filament_force` into `<klipper>/klippy/extras/filament_force`,
adds a git exclude, and offers to restart klipper. Pass a Kalico tree if
that is not `~/klipper`.

If an older `tool_state` install left `klippy/extras/filament_force.py` as a
shim, this install removes that file so the package directory can own the
section.

Copy `filament_force.cfg` into `printer_data/config` and add:

```ini
[include filament_force.cfg]
```

## Development

```sh
uv sync
uv run pytest
uv run ty check src tests
```

## Configuration

```ini
[filament_force]
#continuous_detection: False
#sensor: load_cell_probe
#detection_length: 7.0
#min_e_speed: 0.5
#speed_bins: 1.0, 2.0, 4.0, 7.0
#   Interior edges; bin i is [prev, edge) with 0 / +inf sentinels.
#   Learn raw grams per bin. Jam HIGH lerps expected force between
#   neighbouring learned bins.
#idle_reset_s: 0.3
#   Reset the E window after this much idle so travel does not stitch beads.
#min_learn_s: 1.5
#   After idle_reset_s, a window shorter than this is not learned.
#drop_ratio: 0.35
#   Runout when window level <= drop_ratio * recent healthy mean AND ...
#runout_max_level_g: 80.0
#   ... level <= this absolute floor (blocks relative-only false lows).
#confirm_windows: 3
#recover_windows: 2
#history_n: 16
#min_learn_windows: 5
#   Per e-speed bin (and for global runout history).
#high_sigma: 4.0
#probe_retract_mm: 3.0
#probe_extra_prime_mm: 0.2
#probe_spike_g: 50.0
#probe_feedrate: 30.0
#   On-demand only via FILAMENT_FORCE_CHECK_SPIKE (not continuous).
#quiet: False
#debug_log: False
#   Parsable filament_force ev=window|skip|trip lines to klippy.log
#   only (never the Mainsail console). Enable for a capture session.
#oh_shit_force: 1800
#   Absolute |F-ref| grams. After jam_dwell_s above this, pause as jam
#   even if the speed bin is unlearned.
#   Cal: FILAMENT_FORCE_CAL_OH_SHIT then SAVE_CONFIG.
#jam_dwell_s: 0.15
#baseline_time: 0.15
```

Default `continuous_detection` is off. Enable only after empty-vs-loaded
traces show usable SNR on-bed.

Continuous runout is a **sustained forward-E force drop**: each print
window's raw grams (mean |F-ref|) are compared to a **global** healthy
mean (`drop_ratio * mean` and `runout_max_level_g`). After
`confirm_windows` of collapse it soft-pauses as runout. Jam learns a
**per e-speed-bin** raw-gram notebook, then scores HIGH against a straight
line between neighbouring learned bins at the window's e_speed; an
unlearned assigned bin does not HIGH (only `oh_shit_force` until that bin
has learned). Confirm counts consecutive highs across bins. Retracts, unretracts,
slow E, and idle longer than `idle_reset_s` are not scored. A window
shorter than `min_learn_s` is not learned only if it follows that idle
(post-dwell short bead). Continuous cruise windows learn even when 4 mm
finishes in well under 1.5 s.

`FILAMENT_FORCE_CHECK_SPIKE` runs the retract -> Force1 -> deretract spike
routine once for macros (e.g. purge / load verify). It sets
`last_probe_spike` / `last_probe_delta_g` and does **not** trip continuous
runout or clear suspect state.

On a toolchanger, bind the active tool after pickup and suppress scoring
for the park+pickup latch window so peel E does not look like a jam:

```gcode
FILAMENT_FORCE_SUPPRESS ENABLE=1
# ... park / pickup ...
FILAMENT_FORCE_SET_TOOL TOOL={t}
FILAMENT_FORCE_SUPPRESS ENABLE=0
```

## Commands

- `FILAMENT_FORCE_SET ENABLE=0|1` plus optional runtime knobs:
  `DROP_RATIO`, `RUNOUT_MAX_LEVEL_G`, `CONFIRM_WINDOWS`, `RECOVER_WINDOWS`,
  `PROBE_SPIKE_G`, `PROBE_RETRACT_MM`, `PROBE_EXTRA_PRIME_MM`,
  `PROBE_FEEDRATE`, `QUIET`, `DEBUG_LOG`, `MIN_DELTA_G`, `HIGH_SIGMA`,
  `DETECTION_LENGTH`, `MIN_E_SPEED`, `OH_SHIT_FORCE`, `IDLE_RESET_S`,
  `MIN_LEARN_S`, `SPEED_BINS`, `HISTORY_N`, `MIN_LEARN_WINDOWS`
- `FILAMENT_FORCE_CAL_OH_SHIT [MAX_TEMP=50] [EXTRUDE=8] [SPEED=2]
  [MARGIN=0.85] [APPLY=0|1]` - wait until the hotend is cold, extrude
  into the solid melt zone, set `oh_shit_force` to `MARGIN * peak |F-ref|`
  if that beats the current value. `SAVE_CONFIG` to persist. Does not
  need `ENABLE=1`.
- `FILAMENT_FORCE_CHECK_SPIKE [SPIKE_G=] [RETRACT=] [EXTRA_PRIME=]
  [FEEDRATE=]` - on-demand presence probe; sets `last_probe_spike`
- `FILAMENT_FORCE_TEST_RUNOUT RETRACT=<mm> TEMP=<c> [SPEED=<mm/s>]
  [RETRACT_SPEED=<mm/s>]` - heat, retract, then extrude enough forward E
  for global runout (`RETRACT` plus `detection_length * (confirm_windows + 1)`).
  Needs `ENABLE=1`, not suppress. Logs `ev=test_runout`. Not a calibration save.
- `FILAMENT_FORCE_QUERY [TOOL=n]`
  lists per-bin `n/learned/mean/high_bound` and global runout.
- `FILAMENT_FORCE_SET_TOOL TOOL=n`
- `FILAMENT_FORCE_RESET [TOOL=n]`
- `FILAMENT_FORCE_RESUME [VELOCITY=v]`
- `FILAMENT_FORCE_SUPPRESS ENABLE=0|1`

## Status

`printer.filament_force` - `enabled`, `armed`, `active_tool`, `learned`,
`window_count`, `last_bin`, `mean_level_g`, `stdev_level_g`,
`last_level_g`, `last_e_speed`,
`last_z_score`, `suspect`, streaks, `pending_recheck`, `quiet`,
`debug_log`, `last_trip`, `last_msg`, `last_probe_spike`, `drop_ratio`,
`runout_max_level_g`, `oh_shit_force`, `last_oh_shit_cal_peak_g`.

## Integration sketch

```gcode
# After successful pickup
FILAMENT_FORCE_SET_TOOL TOOL={t}

# Optional: enable after traces prove SNR
FILAMENT_FORCE_SET ENABLE=1

# Optional: before purge / load verify
FILAMENT_FORCE_CHECK_SPIKE
{% if printer.filament_force.last_probe_spike|int == 0 %}
    {action_respond_info("No filament spike - abort purge")}
{% endif %}

# RESUME wrapper
{% if printer.filament_force.pending_recheck|int == 1 %}
    FILAMENT_FORCE_RESUME
{% endif %}
```

## Tests

```sh
uv run pytest
```

Covers per-tool anomaly / E-window logic. Hardware behaviour needs the
checklist below.

## Manual test checklist

1. With continuous off, no pauses from force.
2. Enable, print until `FILAMENT_FORCE_QUERY` shows `learned=yes` for a tool.
3. Yank filament mid-extrude: after low-band confirm, runout pause.
4. Brief force blip that recovers within `recover_windows` must not pause.
5. Retract / travel must not score or clear a real runout suspect on clock time.
6. Toolchange latch E must not trip (suppress path).
7. `FILAMENT_FORCE_CHECK_SPIKE` sets `last_probe_spike` without tripping
   continuous runout.
8. `FILAMENT_FORCE_TEST_RUNOUT` with an empty melt zone should log
   `ev=trip kind=runout` after `confirm_windows` low windows (pause only
   if print_stats is printing).
9. Load a clog / hold the filament so |F-ref| stays above `oh_shit_force`
   for `jam_dwell_s`: pause as jam even before the bin is learned.
10. `FILAMENT_FORCE_CAL_OH_SHIT` with a cold loaded nozzle should raise
    `oh_shit_force` above 1800 if the jam peak is high enough; an already
    molten nozzle should wait or be cooled first.
