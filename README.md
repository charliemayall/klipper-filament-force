# filament_force

Pauses the print on filament runout or a jam, using the toolhead load cell.

Needs `[load_cell_probe]` or `[load_cell]` (Kalico or Klipper).

## Install

```sh
cd ~
git clone https://github.com/charliemayall/klipper-filament-force.git
cd klipper-filament-force
./install.sh [~/klipper]
```

Pass a Kalico tree if that is not `~/klipper`. The script can copy
`filament_force.cfg` into `printer_data/config`. Then add:

```ini
[include filament_force.cfg]
```

Restart Klipper.

Moonraker update manager:

```ini
[update_manager filament_force]
type: git_repo
path: ~/klipper-filament-force
origin: https://github.com/charliemayall/klipper-filament-force.git
primary_branch: main
managed_services: klipper
is_system_service: False
post_update_script: install.sh
```

## First use

1. Load filament.
2. `FILAMENT_FORCE_CAL_OH_SHIT` - cools the hotend, then pushes into the
   cold nozzle to set the jam stop. Do not run this during a print.
3. `SAVE_CONFIG`
4. Optional: `FILAMENT_FORCE_TEST_RUNOUT RETRACT=30 TEMP=220` should report
   a runout in the console.

Detection is on. `FILAMENT_FORCE_SET ENABLE=0` turns it off.

On a trip: fix the filament, then `RESUME`. If nothing was wrong:
`FILAMENT_FORCE_RESET` then `RESUME`.

## Toolchanger

Call two hooks from your park and pickup macros. The extra does not patch
them.

- `FILAMENT_FORCE_SUPPRESS_BEGIN` before park or pickup motion (stops scoring)
- `FILAMENT_FORCE_SUPPRESS_END TOOL={t}` after a successful pickup (records the tool, scoring on)
- `FILAMENT_FORCE_SUPPRESS_END` with no `TOOL` after a park that leaves the head empty

A single-tool printer can ignore this.

### INDX

In `CHANGE_TOOL`, inside `{% if act != t %}`, before `PARK_TOOL` /
`_PICKUP_TOOL`:

```gcode
FILAMENT_FORCE_SUPPRESS_BEGIN
```

Last line of `_RECORD_TOOLCHANGE`:

```gcode
FILAMENT_FORCE_SUPPRESS_END TOOL={t}
```

Standalone `PARK_TOOL` (calibration, `TC_CYCLE_ALL`) also needs a begin and
an end. Put `FILAMENT_FORCE_SUPPRESS_BEGIN` at the start of `PARK_TOOL` (nested begin from
`CHANGE_TOOL` is fine). Do not put `FILAMENT_FORCE_SUPPRESS_END` inside `PARK_TOOL` -
`CHANGE_TOOL` would then score the pickup latch. After those standalone
parks:

```gcode
PARK_TOOL
FILAMENT_FORCE_SUPPRESS_END
```

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
- `FILAMENT_FORCE_CAL_OH_SHIT` - set the jam stop from a cold extrude; `SAVE_CONFIG` to keep it
- `FILAMENT_FORCE_TEST_RUNOUT RETRACT=<mm> TEMP=<c>` - should report a runout
- `FILAMENT_FORCE_CHECK_SPIKE` - one-shot presence check
- `FILAMENT_FORCE_QUERY` - current state
- `FILAMENT_FORCE_SET_TOOL TOOL=n`
- `FILAMENT_FORCE_RESET [TOOL=n]` - clear history after a false trip
- `FILAMENT_FORCE_RESUME [VELOCITY=v]`
- `FILAMENT_FORCE_SUPPRESS ENABLE=0|1`
- `FILAMENT_FORCE_SUPPRESS_BEGIN` - call before park or pickup
- `FILAMENT_FORCE_SUPPRESS_END [TOOL=n]` - call after pickup, or after a park with no TOOL

## Development

```sh
uv sync
uv run pytest
uv run ty check src tests
```
