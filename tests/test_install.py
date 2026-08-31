from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ff_install", ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
_install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_install)
copy_example_cfg = _install.copy_example_cfg


def test_copy_example_cfg_writes_missing_dest(tmp_path: Path) -> None:
    src = tmp_path / "filament_force.cfg"
    src.write_text("ok\n")
    dest = tmp_path / "config" / "filament_force.cfg"
    assert copy_example_cfg(src, dest) is True
    assert dest.read_text() == "ok\n"


def test_example_cfg_does_not_set_oh_shit_force() -> None:
    # SAVE_CONFIG cannot override an option that lives in an include.
    for line in (ROOT / "filament_force.cfg").read_text().splitlines():
        if line.split("#", 1)[0].strip().startswith("oh_shit_force"):
            raise AssertionError("oh_shit_force in the include fights SAVE_CONFIG")


def test_copy_example_cfg_does_not_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "filament_force.cfg"
    src.write_text("new\n")
    dest = tmp_path / "filament_force.cfg.dest"
    dest.write_text("mine\n")
    assert copy_example_cfg(src, dest) is False
    assert dest.read_text() == "mine\n"
