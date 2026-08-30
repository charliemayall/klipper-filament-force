# filament_force

Pauses the print on filament runout or a jam, using the toolhead load cell.

## Install

```sh
./install.sh [~/klipper]
```

Symlinks `src/filament_force` into `<klipper>/klippy/extras/filament_force`
and offers to restart klipper. Pass a Kalico tree if that is not `~/klipper`.

If an older `tool_state` install left `klippy/extras/filament_force.py` as a
shim, this install removes that file so the package directory can own the
section.

Copy `filament_force.cfg` into `printer_data/config` and add:

```ini
[include filament_force.cfg]
```

Needs `[load_cell_probe]` or `[load_cell]`.

Calibrate the jam threshold with `FILAMENT_FORCE_CAL_OH_SHIT`, then
`SAVE_CONFIG`. `FILAMENT_FORCE_SET ENABLE=0` turns detection off.

## Toolchanger

```ini
[filament_force]
#continuous_detection: True
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
#oh_shit_force: 4000
#   Absolute |F-ref| grams. After jam_dwell_s above this, pause as jam
#   even if the speed bin is unlearned.
#   Cal: FILAMENT_FORCE_CAL_OH_SHIT then SAVE_CONFIG.
#jam_dwell_s: 0.15
#baseline_time: 0.15
```

Default `continuous_detection` is on.

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

On INDX that is the start of `CHANGE_TOOL` / `PARK_TOOL` and the end of
`_RECORD_TOOLCHANGE`.

## Resume

If your `RESUME` wrapper does extra work:

```gcode
{% if printer.filament_force.pending_recheck|int == 1 %}
    FILAMENT_FORCE_RESUME
{% endif %}
```

Optional presence check before purge or load:

```gcode
FILAMENT_FORCE_CHECK_SPIKE
{% if printer.filament_force.last_probe_spike|int == 0 %}
    {action_respond_info("No filament spike - abort purge")}
{% endif %}
```

## Commands

- `FILAMENT_FORCE_SET ENABLE=0|1` - turn detection on or off
- `FILAMENT_FORCE_CAL_OH_SHIT` - set the jam threshold from a cold extrude; `SAVE_CONFIG` to persist
- `FILAMENT_FORCE_CHECK_SPIKE` - one-shot presence check; sets `last_probe_spike`
- `FILAMENT_FORCE_QUERY` - print current state
- `FILAMENT_FORCE_SET_TOOL TOOL=n`
- `FILAMENT_FORCE_RESET [TOOL=n]`
- `FILAMENT_FORCE_RESUME [VELOCITY=v]`
- `FILAMENT_FORCE_SUPPRESS ENABLE=0|1`
- `FILAMENT_FORCE_TEST_RUNOUT RETRACT=<mm> TEMP=<c>` - heat, retract, extrude; should pause as runout (`ENABLE=1`, not suppressed)

## Development

```sh
uv sync
uv run pytest
uv run ty check src tests
```
