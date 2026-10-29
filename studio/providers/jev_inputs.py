"""Literal argument candidates for Jev. No inferred units or generated numbers."""
import re
from fractions import Fraction

NUMBER = r"[+-]?(?:\d+\s+\d+/\d+|(?:\d+(?:\.\d+)?|\.\d+)(?:/\d+)?)"
UNIT = r"(?:millimeters?|centimeters?|meters?|inches|inch|mm|cm|in|m)\b"
START = r"(?<![\w./+-])"
TRIPLE = re.compile(START + rf"({NUMBER})\s*(?:x|×|by)\s*({NUMBER})\s*(?:x|×|by)\s*({NUMBER})\s*({UNIT})", re.I)
SINGLE = re.compile(START + rf"({NUMBER})\s*({UNIT})", re.I)
SCALE = {"mm": 1, "cm": 10, "m": 1000, "in": Fraction(127, 5)}


def dimension_candidates(text):
    """Read unit-bearing scalars and triples with a shared trailing unit.

    Candidates retain source spans for interpretation. Arithmetic is exact until
    conversion to the native polygon tool's numeric coordinates.
    """
    found, occupied = [], []
    for match in TRIPLE.finditer(text):
        occupied.append(match.span())
        for axis, number in zip(("X", "Y", "Z"), match.group(1, 2, 3)):
            found.append((match.start(), number, match.group(4), match.group(), axis))
    for match in SINGLE.finditer(text):
        if not any(a <= match.start() < b for a, b in occupied):
            found.append((match.start(), match.group(1), match.group(2), match.group(), None))
    if len(found) > 12:
        raise ValueError("Use at most 12 explicit dimensions in one Jev request")
    result = []
    for _, number, unit, source, axis in sorted(found, key=lambda item: item[0]):
        unit = unit.lower()
        if unit.startswith("millimeter"): unit = "mm"
        elif unit.startswith("centimeter"): unit = "cm"
        elif unit.startswith("meter"): unit = "m"
        elif unit.startswith("inch"): unit = "in"
        pieces = number.lstrip("+-").split()
        try:
            value = sum((Fraction(p) for p in pieces), Fraction(0))
            if number.startswith("-"): value = -value
            mm = value * SCALE[unit]
        except (ValueError, ZeroDivisionError):
            raise ValueError("Invalid dimension; use positive lengths with explicit units") from None
        if not 0 < mm <= 100000:
            raise ValueError("Jev dimensions must be positive and at most 100 m")
        result.append({"id": f"dim_{len(result)}", "expression": f"{float(mm):.12g} mm",
                       "mm": float(mm), "source": source, "triple_axis": axis})
    return result


def name_candidates(text):
    """Only explicit quoted strings can become native names/occurrence paths."""
    names = list(dict.fromkeys(re.findall(r'"([^"\n]{1,160})"|“([^”\n]{1,160})”', text)))
    values = list(dict.fromkeys(a or b for a, b in names))
    if len(values) > 12:
        raise ValueError("Use at most 12 quoted names in one request")
    return {f"name_{i}": value for i, value in enumerate(values)}
