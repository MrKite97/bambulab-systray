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

import time
from dataclasses import dataclass


@dataclass
class PrintState:
    """Retained, delta-merged view of the active print.

    Fields mirror the verified paths under the report's top-level ``print``
    object (RESEARCH Pattern 5).
    """

    gcode_state: str = "unknown"
    mc_percent: int = 0
    mc_remaining_time: int = 0  # MINUTES (verified vs pybambu utils.py timedelta(minutes=...))
    last_update_monotonic: float = 0.0

    def merge(self, print_obj: dict) -> None:
        """Merge a partial report ``print`` object over the retained state.

        Missing keys leave the current value untouched. ``mc_remaining_time`` is
        only overwritten when present and not None.
        """
        self.gcode_state = print_obj.get("gcode_state", self.gcode_state)
        self.mc_percent = print_obj.get("mc_percent", self.mc_percent)
        if print_obj.get("mc_remaining_time") is not None:
            self.mc_remaining_time = print_obj["mc_remaining_time"]
        self.last_update_monotonic = time.monotonic()


def hmm(minutes: int) -> str:
    """Format an integer count of MINUTES as ``h:mm`` (e.g. 83 -> "1:23")."""
    return f"{minutes // 60}:{minutes % 60:02d}"
