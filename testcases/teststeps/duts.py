"""Which physical units exist, and which stands can run them.

One catalogue rather than a list per DUT package, so a serial is spelled
in exactly one place. A unit that moves between stands - ZDRIVE2IN runs
on both the zdrive and the ydrive stand - is one entry naming both,
rather than the same string typed into two files that can drift apart.

Each entry names the DUT packages under testcases/ that can run it, and
serials_for() is what a stand's serial prompt offers. That filtering is
the point: the prompt is a dropdown so the answer is *checked*, and a
dropdown listing every serial in the building checks almost nothing. A
zdrive run filed against YDRIVE1 is a misattributed run that looks
correctly filed, which is worse than no answer at all.
"""
from __future__ import annotations

from typing import Dict, Tuple

DUT_SERIAL_NUMBERS: Dict[str, Tuple[str, ...]] = {
    "YDRIVE1": ("ydrive",),
    "YDRIVE2": ("ydrive",),
    "ZDRIVE2IN": ("ydrive", "zdrive"),
}
"""Every unit these tests can run on, mapped to the DUT packages that can run it.

The keys are what lands in a stored run's `dut_serial_number`, so they are the
spelling every later query has to match. Add a unit here and it appears in the
prompt of every stand listed against it."""


def serials_for(dut: str) -> Tuple[str, ...]:
    """The serials a `dut` package's prompt offers, in a stable order.

    Sorted rather than in catalogue order, so the dropdown does not silently
    reorder itself when an unrelated unit is added above it - an operator
    picking by position would otherwise start picking a different unit."""
    return tuple(sorted(
        serial for serial, duts in DUT_SERIAL_NUMBERS.items() if dut in duts
    ))
