"""Pure fastener data. No Fusion, no geometry -- just what a designation means."""

from __future__ import annotations

import re

CM_PER_INCH = 2.54

# ANSI numbered screw sizes: major diameter = 0.060 + 0.013 * number
NUMBERED_BASE_IN = 0.060
NUMBERED_STEP_IN = 0.013


def designation_major_diameter_cm(designation: str) -> float | None:
    """Recover the nominal major diameter a thread designation implies.

    This is the check that catches a thread-family mismatch. M3.5x0.6 and
    5-40 UNC are both things Fusion will happily recommend for a 0.125 in
    request, but only one of them is actually 0.125 in.
    """
    if not isinstance(designation, str):
        return None
    text = designation.strip()

    match = re.match(r"^M([\d.]+)", text)
    if match:
        return float(match.group(1)) / 10.0
    match = re.match(r"^(\d+)\s*/\s*(\d+)\s*-", text)
    if match:
        return int(match.group(1)) / int(match.group(2)) * CM_PER_INCH
    match = re.match(r"^(\d+)-\d+", text)
    if match:
        number = int(match.group(1))
        return (NUMBERED_BASE_IN + NUMBERED_STEP_IN * number) * CM_PER_INCH
    match = re.match(r"^([\d.]+)-\d+", text)
    if match:
        return float(match.group(1)) * CM_PER_INCH
    return None


def matches_diameter(designation: str, diameter_cm: float, tolerance: float = 1e-4) -> bool:
    implied = designation_major_diameter_cm(designation)
    return implied is not None and abs(implied - diameter_cm) <= tolerance
