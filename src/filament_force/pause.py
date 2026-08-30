# Pause / resume / template helpers for [filament_force].
#
# Not a Klipper config section. Imported by the extras package.

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from klippy.configfile import ConfigWrapper
    from klippy.extras.gcode_macro import PrinterGCodeMacro, TemplateWrapper
    from klippy.gcode import GCodeDispatch
    from klippy.klippy import Printer
    from klippy.reactor import SelectReactor

_E = TypeVar("_E", bound=Enum)


class FailMode(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    RETRY = "retry"
    NONE = "none"


class MonitorTemplate(str, Enum):
    FAIL = "fail_gcode"
    RECOVER = "recover_gcode"
    RESUME = "resume_gcode"
    RETRY = "retry_gcode"
    RUNOUT = "runout_gcode"
    JAM = "jam_gcode"


def enum_choice_map(enum_cls: type[_E], *members: _E) -> dict[str, str]:
    chosen = members or tuple(enum_cls)
    return {m.value: m.value for m in chosen}


def get_enum_choice(
    config: ConfigWrapper,
    option: str,
    enum_cls: type[_E],
    default: _E,
    *members: _E,
) -> _E:
    # Config getchoice returns the map value; keep values as strings so the
    # default (also a string) type-checks against klippy-stubs, then wrap.
    raw = config.getchoice(option, enum_choice_map(enum_cls, *members), default.value)
    return enum_cls(raw)


class MonitorActions:
    """Shared gcode template, printing, soft-pause, and resume helpers."""

    def __init__(
        self,
        config: ConfigWrapper,
        *,
        template_keys: Mapping[MonitorTemplate, str],
    ) -> None:
        self.printer: Printer = config.get_printer()
        self.reactor: SelectReactor = self.printer.get_reactor()
        self.gcode: GCodeDispatch = self.printer.lookup_object("gcode")
        gcode_macro: PrinterGCodeMacro = self.printer.load_object(config, "gcode_macro")

        self.templates: dict[MonitorTemplate, TemplateWrapper] = {}
        self.has_template: dict[MonitorTemplate, bool] = {}
        for attr, option in template_keys.items():
            present = bool(config.get(option, "").strip())
            self.has_template[attr] = present
            self.templates[attr] = gcode_macro.load_template(config, option, "")

        self.pending_recheck: int = 0
        self.pending_tool: int = -1
        self.pending_target: float = 0.0
        self.pending_reason: str = ""

    def print_stats_state(self) -> str:
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is None:
            return ""
        eventtime = self.reactor.monotonic()
        return str(
            print_stats.get_status(eventtime).get("state", "") or ""
        )

    def is_printing(self) -> bool:
        return self.print_stats_state() == "printing"

    def in_print_job(self) -> bool:
        """True while a job is printing or paused (soft-pause is still a job)."""
        return self.print_stats_state() in ("printing", "paused")

    def run_template(self, key: MonitorTemplate, params: Mapping[str, Any]) -> None:
        template = self.templates.get(key)
        if template is None:
            return
        context = template.create_template_context()
        context["params"] = {k: str(v) for k, v in params.items()}
        template.run_gcode_from_command(context)

    def run_template_if_set(
        self, key: MonitorTemplate, params: Mapping[str, Any]
    ) -> None:
        if self.has_template.get(key):
            self.run_template(key, params)

    def do_resume(self, velocity: str | None) -> None:
        if self.has_template.get(MonitorTemplate.RESUME):
            self.run_template(
                MonitorTemplate.RESUME,
                {"VELOCITY": velocity if velocity is not None else ""},
            )
            return
        suffix = ""
        if velocity is not None:
            suffix = f" VELOCITY={velocity}"
        client_resume = self.printer.lookup_object("gcode_macro _CLIENT_RESUME", None)
        if client_resume is not None:
            self.gcode.run_script_from_command("_CLIENT_RESUME" + suffix)
        else:
            self.gcode.run_script_from_command("RESUME_BASE" + suffix)

    def set_pending(
        self,
        *,
        tool: int = -1,
        target: float = 0.0,
        reason: str = "",
    ) -> None:
        self.pending_recheck = 1
        self.pending_tool = tool
        self.pending_target = target
        self.pending_reason = reason

    def clear_pending(self) -> None:
        self.pending_recheck = 0
        self.pending_tool = -1
        self.pending_target = 0.0
        self.pending_reason = ""

    def soft_pause(
        self,
        msg: str,
        *,
        tool: int = -1,
        target: float = 0.0,
        reason: str = "",
        from_command: bool = False,
    ) -> None:
        """Record pending state and PAUSE. Prefer from_command for G-code paths."""
        self.set_pending(tool=tool, target=target, reason=reason or msg)
        self.gcode.respond_info(msg)
        if from_command:
            self.gcode.run_script_from_command("PAUSE")
        else:
            self.gcode.run_script("PAUSE\nM400")

    def extruder_target(self) -> float:
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is None:
            return 0.0
        now = self.reactor.monotonic()
        return float(extruder.get_status(now).get("target", 0.0) or 0.0)
