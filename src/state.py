"""PrintState: the retained network<->UI state contract for the Bambu cloud stream.

Bambu P1/A1 printers send *partial* report deltas: a single MQTT message
carries only the fields that changed, not a full snapshot. We therefore keep a
retained PrintState and merge each delta over it. A *missing* field means
"unchanged", NOT zero -- treating missing as zero would make the display flicker
to idle/0%. This is the pybambu `models.py` idiom (`data.get(field, previous)`),
verified against the live source on 2026-06-20.

mc_remaining_time is an integer count of MINUTES (verified against pybambu
`utils.py`, which renders it via `timedelta(minutes=remaining_time)`; the
OpenBambuAPI doc claiming "seconds" is wrong). It is None-guarded because
pybambu only assigns it when present -- an explicit None must not clobber a
prior good value.

This module is pure, has no network/keyring dependency, and is the only object
the future Phase 2 tray thread reads.
"""

import os
import time
from dataclasses import dataclass


@dataclass
class PrintState:
    """Retained, delta-merged view of the active print.

    Fields mirror the verified paths under the report's top-level ``print``
    object (RESEARCH Pattern 5).

    The v2 panel (Phase 9) reads additional layer and file fields:
    ``layer_num`` / ``total_layer_num`` (current/total layer) and
    ``gcode_file`` / ``subtask_name`` (the print file name, stored as the
    BASENAME for display -- directory components are stripped, the extension is
    kept). All follow the SAME None-guarded "missing delta = unchanged" rule as
    ``mc_remaining_time`` so partial P1/A1 reports never reset them to 0/"".
    """

    gcode_state: str = "unknown"
    mc_percent: int = 0
    mc_remaining_time: int = 0  # MINUTES (verified vs pybambu utils.py timedelta(minutes=...))
    layer_num: int = 0
    total_layer_num: int = 0
    gcode_file: str = ""  # basename only (path stripped, extension kept)
    subtask_name: str = ""  # basename only (path stripped, extension kept)
    last_update_monotonic: float = 0.0

    def merge(self, print_obj: dict) -> None:
        """Merge a partial report ``print`` object over the retained state.

        Missing keys leave the current value untouched. ``mc_remaining_time``
        and every layer/file field are only overwritten when present and not
        None. File fields are stored as the basename for display.
        """
        self.gcode_state = print_obj.get("gcode_state", self.gcode_state)
        self.mc_percent = print_obj.get("mc_percent", self.mc_percent)
        if print_obj.get("mc_remaining_time") is not None:
            self.mc_remaining_time = print_obj["mc_remaining_time"]
        v = print_obj.get("layer_num")
        if v is not None:
            self.layer_num = v
        v = print_obj.get("total_layer_num")
        if v is not None:
            self.total_layer_num = v
        v = print_obj.get("gcode_file")
        if v is not None:
            self.gcode_file = _basename(v)
        v = print_obj.get("subtask_name")
        if v is not None:
            self.subtask_name = _basename(v)
        self.last_update_monotonic = time.monotonic()


def _basename(path: str) -> str:
    """Strip directory components from a report file name for display.

    Keeps the file extension (the UI shows "3DBenchy.gcode" WITH extension).
    The result is display-only and is never used as a filesystem path, so a
    crafted "../" name is rendered harmlessly (T-05-01).
    """
    return os.path.basename(path) if path else path


def hmm(minutes: int) -> str:
    """Format an integer count of MINUTES as ``h:mm`` (e.g. 83 -> "1:23")."""
    return f"{minutes // 60}:{minutes % 60:02d}"
