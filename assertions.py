"""Executable design assertions.

The premise: the geometry kernel already knows when an operation went wrong.
It knows a boolean produced two bodies, that a cut removed nothing, that two
occurrences overlap by 4.2 cm3. Nothing routes those signals back. These
helpers turn them into cheap deterministic checks that run after *every*
operation, so a mistake costs one operation instead of a session.

Errors that are obvious to a human eye are almost always cheap to compute:

    flipped mate  -> interference volume != 0
    upside down   -> centre of mass Z, or face normal
    wrong scale   -> bounding box
    off centre    -> centroid delta

Computational geometry lives in geom.py; this module is the check framework
and the checks themselves. Everything above the FUSION-BACKED banner is pure.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import geom
from errors import CheckFailed

Point = tuple[float, float]
Polygon = Sequence[Point]

CM_PER_INCH = 2.54


class Check:
    __slots__ = ("name", "ok", "actual", "expected", "detail", "severity")

    def __init__(self, name, ok, actual=None, expected=None, detail="", severity="error"):
        self.name = name
        self.ok = bool(ok)
        self.actual = actual
        self.expected = expected
        self.detail = detail
        self.severity = severity

    def describe(self) -> str:
        if self.ok:
            return f"{self.name} ok"
        parts = [f"{self.name} FAILED"]
        if self.expected is not None:
            parts.append(f"expected {self.expected}")
        if self.actual is not None:
            parts.append(f"got {self.actual}")
        if self.detail:
            parts.append(self.detail)
        return " -- ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "actual": self.actual,
            "expected": self.expected,
            "detail": self.detail,
            "severity": self.severity,
        }


class CheckSuite:
    """Collects checks for one operation and decides whether it stands."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.checks: list[Check] = []

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def extend(self, checks: Sequence[Check]) -> None:
        self.checks.extend(checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def require(self) -> None:
        if self.failures:
            raise CheckFailed(self.failures, self.label)

    def report(self) -> dict[str, Any]:
        """Only what needs attention, in full. Passes as a list of names.

        A green check carries no information a reader can act on, and there
        are often more than a dozen of them. Returning each one in full
        invites the model to reason about every one -- which it does, at
        length -- for no decision it could possibly make differently.
        """
        attention = [c for c in self.checks if not c.ok]
        return {
            "label": self.label,
            "ok": self.ok,
            "passed": sum(1 for c in self.checks if c.ok),
            "failed": len(self.failures),
            "warned": len(self.warnings),
            "passed_checks": [c.name for c in self.checks if c.ok],
            "checks": [c.as_dict() for c in attention],
        }


# ---------------------------------------------------------------- scalar ----

def count_equals(name: str, actual: int, expected: int, detail: str = "") -> Check:
    return Check(name, actual == expected, actual, expected, detail)


def value_decreased(name: str, before: float, after: float) -> Check:
    return Check(name, after < before, f"{after:.4f}", f"< {before:.4f}",
                 "operation did not remove material")


def within_tolerance(name, actual, expected, tolerance, units="") -> Check:
    delta = abs(actual - expected)
    suffix = f" {units}" if units else ""
    return Check(name, delta <= tolerance, f"{actual:.6f}{suffix}",
                 f"{expected:.6f} +/- {tolerance:g}{suffix}", f"off by {delta:.6f}{suffix}")


def at_most(name, actual, limit, units="", severity="error") -> Check:
    suffix = f" {units}" if units else ""
    return Check(name, actual <= limit, f"{actual:.4f}{suffix}",
                 f"<= {limit:.4f}{suffix}", severity=severity)


def at_least(name, actual, limit, units="", severity="error") -> Check:
    suffix = f" {units}" if units else ""
    return Check(name, actual >= limit, f"{actual:.4f}{suffix}",
                 f">= {limit:.4f}{suffix}", severity=severity)


# -------------------------------------------------------------- geometry ----

def polygons_disjoint(name: str, polygons: list[Polygon]) -> Check:
    """Catch self-intersecting pattern profiles before they reach the kernel."""
    overlapping = geom.overlapping_pairs(polygons)
    return Check(name, not overlapping, f"{len(overlapping)} overlapping pairs",
                 "0 overlapping pairs",
                 f"first few: {overlapping[:5]}" if overlapping else "")


def pattern_centered(name, polygons, center=(0.0, 0.0), tolerance=1e-6) -> Check:
    min_x, max_x, min_y, max_y = geom.bounds(polygons)
    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    delta = math.dist((cx, cy), center)
    return Check(name, delta <= tolerance, f"({cx:.6f}, {cy:.6f})",
                 f"({center[0]:.6f}, {center[1]:.6f}) +/- {tolerance:g}",
                 f"off centre by {delta:.6f}")


def polygons_within(name, polygons, min_x, max_x, min_y, max_y) -> Check:
    lo_x, hi_x, lo_y, hi_y = geom.bounds(polygons)
    inside = lo_x >= min_x and hi_x <= max_x and lo_y >= min_y and hi_y <= max_y
    return Check(name, inside, f"x {lo_x:.4f}..{hi_x:.4f}, y {lo_y:.4f}..{hi_y:.4f}",
                 f"x {min_x:.4f}..{max_x:.4f}, y {min_y:.4f}..{max_y:.4f}")


def minimum_gap(name: str, polygons: list[Polygon], required: float) -> Check:
    """Smallest wall left between neighbouring pockets -- the rib width."""
    worst = geom.minimum_separation(polygons, required * 4)
    if worst == float("inf"):
        worst = required
    return Check(name, worst >= required - 1e-6, f"{worst:.5f}", f">= {required:.5f}",
                 "thinnest wall between pockets")


def polygons_clear_of_points(name, polygons, points, required) -> Check:
    """Keep a lightening pattern away from a bolt pattern.

    An isogrid triangle clipping a mounting hole leaves a bolt with nothing to
    clamp against, which no volume or body-count check would ever notice.
    """
    worst = float("inf")
    culprit = None
    for point in points:
        for polygon in polygons:
            distance = geom.point_polygon_distance(point, polygon)
            if distance < worst:
                worst, culprit = distance, point
    if worst == float("inf"):
        worst = required
    return Check(name, worst >= required - 1e-9, f"{worst:.5f}", f">= {required:.5f}",
                 f"closest at ({culprit[0]:.3f}, {culprit[1]:.3f})"
                 if culprit and worst < required else "")


def tiling_valence(name: str, polygons: list[Polygon], expected: int = 6,
                   tolerance: float = 1e-6, minimum: int = 1,
                   severity_ok_when_deliberate: bool = False) -> Check:
    """Assert cells actually tessellate, not merely sit near each other.

    This is the check that was missing when an "isogrid" shipped as stacked
    rows of triangles with a rib band between them. Every existing check
    passed: the cells did not overlap, the rib gaps were right, the field was
    centred, and the render looked plausible. What was wrong was the topology
    -- no vertex had six triangles around it, so the ribs never ran
    continuously in three directions.

    Cells are inset by half a rib width, so they do not literally touch.
    Vertices are therefore snapped to a grid coarse enough to bridge the rib
    before counting how many cells share each one.
    """
    from collections import Counter

    if not polygons:
        return Check(name, False, "0 cells", "at least one cell")

    # Cells are inset by half a rib width, so corresponding vertices of
    # neighbouring cells sit apart rather than coincident. Cluster by distance
    # rather than snapping to a grid: two vertices a hair apart can straddle a
    # grid boundary and land in different buckets, which reads as a tiling
    # failure on a field that tessellates perfectly well.
    shortest = min(
        math.dist(p[i], p[(i + 1) % len(p)])
        for p in polygons for i in range(len(p))
    )
    radius = max(shortest * 0.55, tolerance)

    centres: list[tuple[float, float]] = []
    counts_by_cluster: list[int] = []
    for polygon in polygons:
        for point in polygon:
            for index, centre in enumerate(centres):
                if math.dist(point, centre) <= radius:
                    counts_by_cluster[index] += 1
                    break
            else:
                centres.append(point)
                counts_by_cluster.append(1)

    counts = Counter(counts_by_cluster)
    junctions = sum(n for valence, n in counts.items() if valence >= expected)

    return Check(
        name,
        junctions >= minimum,
        f"{junctions} vertices with {expected}+ cells "
        f"(valences: {dict(sorted(counts.items()))})",
        f"at least {minimum}",
        "cells are adjacent but do not tessellate -- rows may not interlock",
        # A caller who set row_stagger explicitly may want a banded field;
        # report it rather than refusing to build what was asked for.
        severity="warning" if severity_ok_when_deliberate else "error",
    )


# ------------------------------------------------------------------- dfm ----

def rib_aspect_ratio(name, rib_width, depth, max_ratio=8.0) -> Check:
    """Depth-to-width on the thinnest wall.

    Not a correctness failure, a manufacturability one. Reported as a warning
    so a printed part is never blocked, but it never passes silently either.
    """
    ratio = depth / rib_width if rib_width > 0 else float("inf")
    return Check(name, ratio <= max_ratio, f"{ratio:.1f}:1", f"<= {max_ratio:.0f}:1",
                 "milling needs an unreasonable tool reach; printing or wire EDM is fine",
                 severity="warning")


def corners_machinable(name, polygons, corner_radius, min_angle_deg=100.0) -> Check:
    """Sharp interior corners a round cutter cannot produce.

    A router cannot cut an inside corner sharper than its own radius. The
    part comes off the machine with rounded corners the model does not have,
    and on a lattice that difference lands on every cell. Warning, not error:
    the same file is correct for waterjet, wire EDM or printing.
    """
    if corner_radius > 0:
        return Check(name, True, f"{corner_radius * 2:.4f} cm cutter",
                     "corners rounded", "pocket corners pre-filleted for the cutter",
                     severity="warning")
    sharpest = min((geom.min_corner_angle(polygon) for polygon in polygons),
                   default=math.pi)
    degrees = math.degrees(sharpest)
    return Check(name, degrees >= min_angle_deg,
                 f"{degrees:.0f} deg", f">= {min_angle_deg:.0f} deg",
                 "sharp inside corners; pass cutter_diameter to round them for a router",
                 severity="warning")


def tool_reach_ratio(name, fillet_radius, depth, max_ratio=10.0) -> Check:
    diameter = fillet_radius * 2
    ratio = depth / diameter if diameter > 0 else float("inf")
    return Check(name, ratio <= max_ratio, f"{ratio:.1f}:1", f"<= {max_ratio:.0f}:1",
                 f"internal corners imply a {diameter:.4f} tool reaching {depth:.4f}",
                 severity="warning")


def fillet_fits_rib(name, fillet_radius, rib_width) -> Check:
    limit = rib_width / 2
    return Check(name, fillet_radius < limit, f"{fillet_radius:.5f}",
                 f"< {limit:.5f} (half the rib)",
                 "a radius at or above half the rib consumes the wall entirely")


# ==================================================== FUSION-BACKED ==========
# adsk is imported lazily so this module stays importable in the test suite.

def _extents(component) -> tuple[list[float], list[float], float]:
    lows, highs = [float("inf")] * 3, [float("-inf")] * 3
    volume = 0.0
    for index in range(component.bRepBodies.count):
        body = component.bRepBodies.item(index)
        volume += body.volume
        box = body.preciseBoundingBox or body.boundingBox
        for axis, (low, high) in enumerate((
            (box.minPoint.x, box.maxPoint.x),
            (box.minPoint.y, box.maxPoint.y),
            (box.minPoint.z, box.maxPoint.z),
        )):
            lows[axis] = min(lows[axis], low)
            highs[axis] = max(highs[axis], high)
    return lows, highs, volume


def single_solid_body(name: str, component: Any) -> Check:
    count = component.bRepBodies.count
    solid = count == 1 and component.bRepBodies.item(0).isSolid
    return Check(name, solid, f"{count} bodies", "exactly 1 solid body",
                 "a through cut that fragments the part shows up here")


def all_bodies_solid(name: str, component: Any) -> Check:
    count = component.bRepBodies.count
    solid = sum(1 for i in range(count) if component.bRepBodies.item(i).isSolid)
    return count_equals(name, solid, count)


def interference_total(design: Any) -> tuple[float, list[str]]:
    """Total overlap volume in cm3, and the worst offenders.

    Split out from the check because a *joint* needs the number rather than a
    verdict: creating a mate, measuring, flipping, and measuring again is how
    the flip gets resolved without anyone looking at the screen.
    """
    import adsk.core

    occurrences = design.rootComponent.allOccurrences
    if occurrences.count < 2:
        return 0.0, []
    collection = adsk.core.ObjectCollection.create()
    for index in range(occurrences.count):
        collection.add(occurrences.item(index))
    interference_input = design.createInterferenceInput(collection)
    interference_input.areCoincidentFacesIncluded = False
    results = design.analyzeInterference(interference_input)
    if not results:
        return 0.0, []
    total = 0.0
    worst = []
    for index in range(results.count):
        result = results.item(index)
        total += result.interferenceBody.volume
        worst.append(f"{result.entityOne.name} vs {result.entityTwo.name}"
                     f" = {result.interferenceBody.volume:.4f} cm3")
    return total, worst


def no_interference(name: str, design: Any) -> Check:
    """The flipped-mate check.

    A reversed primary axis drives two occurrences through each other, and the
    interference volume is a single scalar that says so. No screenshot, no
    tokenising two 800-feature assemblies.
    """
    if design.rootComponent.allOccurrences.count < 2:
        return Check(name, True, "fewer than 2 occurrences", "no interference",
                     "nothing to check")
    total, worst = interference_total(design)
    return Check(name, total <= 1e-9,
                 f"{len(worst)} pairs, {total:.4f} cm3" if worst else "none",
                 "0 cm3", "; ".join(worst[:4]))


def interference_not_worsened(name, before: float, after: float,
                              tolerance: float = 1e-6) -> Check:
    """Did this operation drive two parts into each other?

    The measurement the whole project started from. A mate whose primary axis
    points the wrong way looks plausible in the feature tree and produces a
    number here immediately -- so the error costs one operation instead of ten
    minutes of work built on top of it.
    """
    delta = after - before
    return Check(name, delta <= tolerance,
                 f"{after:.4f} cm3 (was {before:.4f})", f"<= {before:.4f} cm3",
                 f"this operation added {delta:.4f} cm3 of overlap"
                 if delta > tolerance else "no new overlap")


def resolve_flip(before: float, requested: float, flipped: float,
                 tolerance: float = 1e-6) -> str:
    """Which joint direction the geometry says is right.

    Three overlap volumes: what existed before the joint, what the requested
    direction produced, and what the opposite produced. Returns "keep",
    "flip", or "neither".

    Pure, because this is the decision the whole assembly story turns on and
    it should be checkable without a running Fusion. The rules:

      * if the requested direction added no overlap, keep it -- a tight but
        legitimate fit must not be flipped for nothing;
      * flip only when it is strictly better, so two equally bad directions
        report a real failure instead of silently picking one;
      * "neither" means the joint targets were wrong, not the direction.
    """
    if requested <= before + tolerance:
        return "keep"
    if flipped < requested - tolerance:
        return "flip" if flipped <= before + tolerance else "flip"
    return "neither"


def occurrence_moved(name, before, after, minimum: float = 1e-6) -> Check:
    """A joint that changed nothing usually means it grabbed the wrong thing.

    `before` and `after` are (x, y, z) translations of the occurrence. A
    rigid joint between two faces that were already coincident legitimately
    moves nothing, so this is a warning rather than a failure.
    """
    shift = math.dist(before, after)
    return Check(name, shift >= minimum, f"{shift:.4f} cm", f">= {minimum:g} cm",
                 "the occurrence did not move; check the joint targets were "
                 "the ones you meant",
                 severity="warning")


def bounding_box_matches(name, body, length, width, thickness, tolerance=1e-6) -> Check:
    box = body.preciseBoundingBox or body.boundingBox
    actual = (box.maxPoint.x - box.minPoint.x,
              box.maxPoint.y - box.minPoint.y,
              box.maxPoint.z - box.minPoint.z)
    expected = (length, width, thickness)
    worst = max(abs(a - e) for a, e in zip(actual, expected))
    return Check(name, worst <= tolerance, " x ".join(f"{v:.4f}" for v in actual),
                 " x ".join(f"{v:.4f}" for v in expected),
                 f"worst axis off by {worst:.6f} cm")


def assembly_dimensions(name, component, wanted, tolerance=1e-6) -> Check:
    lows, highs, _ = _extents(component)
    extents = [highs[i] - lows[i] for i in range(3)]
    worst = max(abs(a - e) for a, e in zip(sorted(extents), sorted(wanted)))
    return Check(name, worst <= tolerance, " x ".join(f"{v:.4f}" for v in extents),
                 " x ".join(f"{v:.4f}" for v in wanted),
                 f"worst axis off by {worst:.6f} cm")


def assembly_fits_envelope(name, component, allowed) -> Check:
    lows, highs, _ = _extents(component)
    extents = sorted((highs[i] - lows[i] for i in range(3)), reverse=True)
    limits = sorted(allowed, reverse=True)
    fits = all(a <= e + 1e-9 for a, e in zip(extents, limits))
    return Check(name, fits, " x ".join(f"{v:.4f}" for v in extents),
                 "within " + " x ".join(f"{v:.4f}" for v in limits),
                 "bounding box across all bodies")


def assembly_centered(name, component, axis="x", tolerance=1e-6) -> Check:
    lows, highs, _ = _extents(component)
    index = {"x": 0, "y": 1, "z": 2}[axis]
    middle = (lows[index] + highs[index]) / 2
    return within_tolerance(name, middle, 0.0, tolerance, "cm")


def assembly_mass_at_most(name, component, limit_kg, density_g_cm3=2.70) -> Check:
    _, _, volume = _extents(component)
    return at_most(name, volume * density_g_cm3 / 1000.0, limit_kg, "kg")
