"""
# filament_force - Pause the print on filament runout or jam
#
# Copyright (C) 2026 Charlie Mayall
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""

# filament_force Klippy extra package.
#
# Symlink this directory to <klipper>/klippy/extras/filament_force so
# [filament_force] resolves via load_config.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from klippy.configfile import ConfigWrapper

from .filament_force import FilamentForce, load_config

__all__ = [
    "FilamentForce",
    "load_config",
]
