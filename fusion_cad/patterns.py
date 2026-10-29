"""Pure 2D pattern generation.

This module holds no Fusion imports on purpose. The pre-flight checks and the
real modelling run call the *same* functions here, so an offline simulation
cannot silently drift from what actually gets built -- which is the failure
mode that let three separate isogrid bugs through review.
"""

from __future__ import annotations

import math
from typing import Sequence

from fusion_cad import complexity
from fusion_cad.errors import PatternError

CM_PER_INCH = 2.54

Point = tuple[float, float]
Polygon = list[Point]


def inset_triangle(points: Sequence[Point], inset: float) -> Polygon:
    """Offset every edge of an equilateral triangle inward by ``inset``.

    Scaling about the centroid by (r - inset)/r reduces the inradius by exactly
    ``inset``, so two triangles that shared an edge end up ``2 * inset`` apart.
    That is why callers pass ``rib_width / 2``.
    """
    if len(points) != 3:
        raise PatternError("inset_triangle expects exactly three points")
    centroid_x = sum(point[0] for point in points) / 3
    centroid_y = sum(point[1] for point in points) / 3
    side = math.dist(points[0], points[1])
    inradius = side * math.sqrt(3) / 6
    if inradius <= 0:
        raise PatternError("degenerate triangle")
    scale = (inradius - inset) / inradius
    if scale <= 0:
        raise PatternError(
            f"inset {inset:.4f} exceeds the triangle inradius {inradius:.4f}; "
            "rib_width is too large for cell_size"
        )
    return [
        (
            centroid_x + (x - centroid_x) * scale,
            centroid_y + (y - centroid_y) * scale,
        )
        for x, y in points
    ]


def isogrid_triangles(
    length: float,
    width: float,
    cell_size: float,
    rib_width: float,
    *,
    center: bool = True,
    margin: float | None = None,
    row_stagger: float = 0.5,
    row_pitch: float = 1.0,
    include_inverted: bool = True,
    rotation: float = 0.0,
) -> list[Polygon]:
    """Build a triangular isogrid pocket field.

    Upright and inverted triangles interleave on shared vertices. The inverted
    row starts at ``left`` -- starting half a cell earlier mirrors each
    inverted triangle onto an upright one instead of nesting between them,
    which self-intersects every profile in the sketch.

    The knobs beyond the classic isogrid:

    ``row_stagger``
        Fraction of ``cell_size`` to shift each successive row sideways.

        **0.5 is the default and is what makes this an isogrid at all.** Each
        row's base vertices then land exactly on the previous row's apexes, so
        six triangles meet at every interior vertex and the ribs run
        continuously in three directions.

        This defaulted to 0.0 in an earlier version, which stacked independent
        rows with a horizontal rib band between each -- it passed every check
        (no overlaps, correct rib gaps, centred) and looked plausible in a
        render, but had no valence-6 vertices anywhere and was not an isogrid.
        Set 0.0 deliberately if the banded variant is what you want.
    ``row_pitch``
        Multiplier on the vertical row spacing. Below 1.0 the rows overlap,
        which the caller must catch -- ``assertions.polygons_disjoint`` exists
        for exactly this.
    ``include_inverted``
        Drop the inverted triangles for a sparser field of upright cells only.
    ``rotation``
        Radians to rotate the finished field about its centre.
    """
    complexity.lattice_counts(
        "isogrid", length, width, cell_size, rib_width, margin=margin,
        row_stagger=row_stagger, row_pitch=row_pitch, include_inverted=include_inverted)
    if cell_size <= 0 or rib_width <= 0:
        raise PatternError("cell_size and rib_width must be positive")
    if cell_size >= width:
        raise PatternError("cell_size must be smaller than width")
    if row_pitch <= 0:
        raise PatternError("row_pitch must be positive")

    # ``margin`` is the solid band kept clear at every edge. It defaults to the
    # rib width, but a panel that has to carry a bolt pattern needs a wider one.
    edge = rib_width if margin is None else margin
    if edge < rib_width:
        raise PatternError("margin cannot be smaller than rib_width")

    triangle_height = cell_size * math.sqrt(3) / 2
    step = triangle_height * row_pitch
    left = -length / 2 + edge
    right = length / 2 - edge
    bottom = -width / 2 + edge
    top = width / 2 - edge
    inset = rib_width / 2
    tolerance = 1e-7

    triangles: list[Polygon] = []
    row = 0
    y0 = bottom
    while y0 + triangle_height <= top + tolerance:
        # Successive rows slide sideways by a fraction of a cell. Modulo keeps
        # the shift bounded so a large stagger does not walk the field off the
        # left edge over many rows.
        shift = ((row * row_stagger) % 1.0) * cell_size
        x0 = left + shift
        while x0 + cell_size <= right + tolerance:
            triangles.append(
                inset_triangle(
                    [
                        (x0, y0),
                        (x0 + cell_size, y0),
                        (x0 + cell_size / 2, y0 + triangle_height),
                    ],
                    inset,
                )
            )
            x0 += cell_size

        if include_inverted:
            x0 = left + shift
            while x0 + 1.5 * cell_size <= right + tolerance:
                full = [
                    (x0 + cell_size / 2, y0 + triangle_height),
                    (x0 + 1.5 * cell_size, y0 + triangle_height),
                    (x0 + cell_size, y0),
                ]
                if min(point[0] for point in full) >= left - tolerance:
                    triangles.append(inset_triangle(full, inset))
                x0 += cell_size

        row += 1
        y0 = bottom + row * step

    if not triangles:
        raise PatternError(
            f"cell_size {cell_size:.4f} is too large to fit any pocket inside "
            f"{length:.4f} x {width:.4f}"
        )

    if rotation:
        triangles = rotate_polygons(triangles, rotation)
    if center:
        triangles = center_polygons(triangles)
    return triangles


def hex_cells(
    length: float,
    width: float,
    cell_size: float,
    rib_width: float,
    *,
    center: bool = True,
    margin: float | None = None,
    rotation: float = 0.0,
) -> list[Polygon]:
    """Honeycomb: regular hexagons on a staggered grid, inset by rib_width/2.

    Distinct from isogrid -- three ribs meet at 120 degrees rather than six at
    60 -- and the pattern people usually mean when they say honeycomb.
    """
    complexity.lattice_counts("hex", length, width, cell_size, rib_width, margin=margin)
    if cell_size <= 0 or rib_width <= 0:
        raise PatternError("cell_size and rib_width must be positive")
    radius = cell_size / 2
    inradius = radius * math.sqrt(3) / 2
    scale = (inradius - rib_width / 2) / inradius
    if scale <= 0:
        raise PatternError("rib_width is too large for cell_size")

    edge = rib_width if margin is None else margin
    horizontal = radius * 1.5
    vertical = inradius * 2
    cells: list[Polygon] = []
    column = 0
    x = -length / 2 + edge + radius
    while x + radius <= length / 2 - edge + 1e-7:
        offset = inradius if column % 2 else 0.0
        y = -width / 2 + edge + inradius + offset
        while y + inradius <= width / 2 - edge + 1e-7:
            corners = [
                (x + radius * math.cos(math.pi / 3 * i),
                 y + radius * math.sin(math.pi / 3 * i))
                for i in range(6)
            ]
            cells.append([(x + (px - x) * scale, y + (py - y) * scale)
                          for px, py in corners])
            y += vertical
        x += horizontal
        column += 1

    if not cells:
        raise PatternError(
            f"cell_size {cell_size:.4f} is too large to fit any hexagon inside "
            f"{length:.4f} x {width:.4f}")
    if rotation:
        cells = rotate_polygons(cells, rotation)
    if center:
        cells = center_polygons(cells)
    return cells


def rotate_polygons(polygons: list[Polygon], radians: float) -> list[Polygon]:
    """Rotate a field about the origin."""
    cos, sin = math.cos(radians), math.sin(radians)
    return [[(x * cos - y * sin, x * sin + y * cos) for x, y in polygon]
            for polygon in polygons]


PATTERNS = {
    "isogrid": isogrid_triangles,
    "hex": hex_cells,
}


def center_polygons(polygons: list[Polygon]) -> list[Polygon]:
    """Recentre a pattern on the origin.

    A lattice fills from one corner and stops when the next cell will not fit,
    so the whole remainder piles up against the far edges. Shifting by half
    that remainder keeps every polygon inside the original border.
    """
    xs = [x for polygon in polygons for x, _ in polygon]
    ys = [y for polygon in polygons for _, y in polygon]
    shift_x = -(min(xs) + max(xs)) / 2
    shift_y = -(min(ys) + max(ys)) / 2
    return [[(x + shift_x, y + shift_y) for x, y in poly] for poly in polygons]


def bounds(polygons: list[Polygon]) -> tuple[float, float, float, float]:
    xs = [x for polygon in polygons for x, _ in polygon]
    ys = [y for polygon in polygons for _, y in polygon]
    return min(xs), max(xs), min(ys), max(ys)


def rows_in(polygons: list[Polygon], triangle_height: float) -> int:
    min_x, max_x, min_y, max_y = bounds(polygons)
    return max(1, round((max_y - min_y) / triangle_height))


def linear_positions(length: float, per_inch: float) -> list[float]:
    """A centred run of positions at a given density along a length."""
    if per_inch < 0:
        raise PatternError("feature density cannot be negative")
    if per_inch == 0:
        return []
    pitch = CM_PER_INCH / per_inch
    count = int(math.floor(length / pitch + 1e-9))
    if count < 1:
        raise PatternError(
            f"density {per_inch}/in is too low to place any feature in "
            f"{length / CM_PER_INCH:.3f} in"
        )
    span = (count - 1) * pitch
    return [-span / 2 + index * pitch for index in range(count)]
