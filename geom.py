"""Pure 2D computational geometry.

Split out of assertions.py, which had grown into four modules in a trenchcoat.
Segment intersection and polygon distance are not assertions; they are the
primitives assertions are built from. No Fusion, no I/O, fully testable.
"""

from __future__ import annotations

import math
from typing import Sequence

Point = tuple[float, float]
Polygon = Sequence[Point]


def cross(origin: Point, a: Point, b: Point) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > point[1]) != (y2 > point[1]):
            x_at = x1 + (point[1] - y1) * (x2 - x1) / (y2 - y1)
            if point[0] < x_at:
                inside = not inside
    return inside


def polygon_bbox(polygon: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), max(xs), min(ys), max(ys)


def bboxes_apart(one: tuple, two: tuple, gap: float) -> bool:
    return (
        one[1] + gap < two[0]
        or two[1] + gap < one[0]
        or one[3] + gap < two[2]
        or two[3] + gap < one[2]
    )


def polygons_overlap(a: Polygon, b: Polygon) -> bool:
    for i in range(len(a)):
        for j in range(len(b)):
            if segments_intersect(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)]):
                return True
    return point_in_polygon(a[0], b) or point_in_polygon(b[0], a)


def point_segment_distance(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.dist(point, a)
    t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def point_polygon_distance(point: Point, polygon: Polygon) -> float:
    if point_in_polygon(point, polygon):
        return 0.0
    return min(
        point_segment_distance(point, polygon[i], polygon[(i + 1) % len(polygon)])
        for i in range(len(polygon))
    )


def polygon_distance(a: Polygon, b: Polygon) -> float:
    if polygons_overlap(a, b):
        return 0.0
    best = float("inf")
    for i in range(len(a)):
        a1, a2 = a[i], a[(i + 1) % len(a)]
        for j in range(len(b)):
            b1, b2 = b[j], b[(j + 1) % len(b)]
            best = min(
                best,
                point_segment_distance(a1, b1, b2),
                point_segment_distance(a2, b1, b2),
                point_segment_distance(b1, a1, a2),
                point_segment_distance(b2, a1, a2),
            )
    return best


def overlapping_pairs(polygons: Sequence[Polygon]) -> list[tuple[int, int]]:
    boxes = [polygon_bbox(p) for p in polygons]
    found = []
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            if bboxes_apart(boxes[i], boxes[j], 0.0):
                continue
            if polygons_overlap(polygons[i], polygons[j]):
                found.append((i, j))
    return found


def minimum_separation(polygons: Sequence[Polygon], search: float) -> float:
    boxes = [polygon_bbox(p) for p in polygons]
    worst = float("inf")
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            span = worst if worst < float("inf") else search
            if bboxes_apart(boxes[i], boxes[j], span):
                continue
            worst = min(worst, polygon_distance(polygons[i], polygons[j]))
    return worst


def bounds(polygons: Sequence[Polygon]) -> tuple[float, float, float, float]:
    xs = [x for polygon in polygons for x, _ in polygon]
    ys = [y for polygon in polygons for _, y in polygon]
    return min(xs), max(xs), min(ys), max(ys)


def fillet_polygon(polygon, radius, segments=6):
    """Round every corner of a polygon to ``radius``, as chorded arcs.

    A pocket drawn with sharp corners cannot be cut on a router: the tool is
    round, so it leaves its own radius behind and the part never matches the
    model. Filleting after the fact means a second operation on dozens of
    corners; generating the pocket already rounded means the cut is correct
    the first time and the model matches the machine.

    The tangent offset is clamped to half of the shorter adjoining edge, so a
    radius too large for a cell degrades to the largest fillet that still
    fits rather than producing a self-intersecting outline.
    """
    if radius <= 0 or len(polygon) < 3:
        return [tuple(point) for point in polygon]

    count = len(polygon)
    out = []
    for index in range(count):
        before = polygon[index - 1]
        corner = polygon[index]
        after = polygon[(index + 1) % count]

        ux, uy = before[0] - corner[0], before[1] - corner[1]
        vx, vy = after[0] - corner[0], after[1] - corner[1]
        ulen = math.hypot(ux, uy)
        vlen = math.hypot(vx, vy)
        if ulen < 1e-12 or vlen < 1e-12:
            out.append((corner[0], corner[1]))
            continue
        ux, uy = ux / ulen, uy / ulen
        vx, vy = vx / vlen, vy / vlen

        cosine = max(-1.0, min(1.0, ux * vx + uy * vy))
        interior = math.acos(cosine)
        # A straight run has nothing to round; a fully doubled-back spike has
        # no finite tangent length.
        if interior < 1e-9 or abs(interior - math.pi) < 1e-9:
            out.append((corner[0], corner[1]))
            continue

        half = interior / 2.0
        offset = radius / math.tan(half)
        offset = min(offset, ulen / 2.0, vlen / 2.0)
        effective = offset * math.tan(half)

        start = (corner[0] + ux * offset, corner[1] + uy * offset)
        end = (corner[0] + vx * offset, corner[1] + vy * offset)

        # Bisector points into the polygon interior at this corner; the arc
        # centre sits along it at the hypotenuse distance.
        bx, by = ux + vx, uy + vy
        blen = math.hypot(bx, by)
        if blen < 1e-12:
            out.append((corner[0], corner[1]))
            continue
        centre = (corner[0] + bx / blen * (effective / math.sin(half)),
                  corner[1] + by / blen * (effective / math.sin(half)))

        a0 = math.atan2(start[1] - centre[1], start[0] - centre[0])
        a1 = math.atan2(end[1] - centre[1], end[0] - centre[0])
        sweep = a1 - a0
        # Take the short way round: the fillet arc is always the minor arc.
        while sweep > math.pi:
            sweep -= 2 * math.pi
        while sweep < -math.pi:
            sweep += 2 * math.pi

        steps = max(2, int(segments))
        for step in range(steps + 1):
            angle = a0 + sweep * step / steps
            out.append((centre[0] + effective * math.cos(angle),
                        centre[1] + effective * math.sin(angle)))
    return out


def fillet_polygons(polygons, radius, segments=6):
    return [fillet_polygon(polygon, radius, segments) for polygon in polygons]


def min_corner_angle(polygon):
    """Sharpest interior angle in radians -- what decides millability."""
    sharpest = math.pi
    count = len(polygon)
    for index in range(count):
        before, corner, after = polygon[index - 1], polygon[index], polygon[(index + 1) % count]
        ux, uy = before[0] - corner[0], before[1] - corner[1]
        vx, vy = after[0] - corner[0], after[1] - corner[1]
        ulen, vlen = math.hypot(ux, uy), math.hypot(vx, vy)
        if ulen < 1e-12 or vlen < 1e-12:
            continue
        cosine = max(-1.0, min(1.0, (ux * vx + uy * vy) / (ulen * vlen)))
        sharpest = min(sharpest, math.acos(cosine))
    return sharpest
