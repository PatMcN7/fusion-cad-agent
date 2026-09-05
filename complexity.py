"""Bound geometry work before allocating a large field or touching Fusion.

Defaults are provisional ceilings, not predictions of kernel running time.
Operators can tune the named environment variables after measuring live cases.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from errors import ArgumentError


@dataclass(frozen=True)
class Limits:
    cells: int = 250
    sketch_curves: int = 6000
    fillet_edges: int = 1000
    candidate_rows: int = 20000

    @classmethod
    def configured(cls):
        values = {}
        for field in cls.__dataclass_fields__:
            key = "FUSION_CAD_MAX_" + field.upper()
            raw = os.environ.get(key)
            if raw is not None:
                try:
                    value = int(raw)
                except ValueError as exc:
                    raise ArgumentError(f"{key} must be a positive integer") from exc
                if value <= 0:
                    raise ArgumentError(f"{key} must be a positive integer")
                values[field] = value
        return cls(**values)

    def require(self, *, cells=0, sketch_curves=0, fillet_edges=0, candidate_rows=0):
        counts = dict(cells=cells, sketch_curves=sketch_curves,
                      fillet_edges=fillet_edges, candidate_rows=candidate_rows)
        exceeded = {key: {"requested": value, "limit": getattr(self, key)}
                    for key, value in counts.items() if value > getattr(self, key)}
        if exceeded:
            detail = ", ".join(f"{key}={v['requested']} (limit {v['limit']})"
                               for key, v in exceeded.items())
            raise ArgumentError(
                f"Geometry complexity limit exceeded: {detail}. "
                "Increase cell_size or row_pitch, reduce the patterned area, "
                "or use fewer corner_segments/fillet edges. No geometry was "
                "created by this guarded stage.",
                complexity={"counts": counts, "exceeded": exceeded})
        return counts


def _slots(span, size, step):
    return max(0, math.floor((span - size + 1e-7) / step) + 1)


def lattice_counts(kind, length, width, cell_size, rib_width, *, margin=None,
                   row_stagger=0.5, row_pitch=1.0, include_inverted=True,
                   corner_segments=None, limits=None):
    """Count the grid without constructing polygons, in the generator's units.

    Centering/rotation do not affect counts. Rounded corners emit segments+1
    vertices; their count is an upper bound when corners are degenerate.
    """
    budget = limits or Limits.configured()
    numbers = (length, width, cell_size, rib_width, row_stagger, row_pitch)
    if not all(math.isfinite(v) for v in numbers) or (margin is not None and
                                                     not math.isfinite(margin)):
        raise ArgumentError("Lattice dimensions and spacing must be finite")
    if min(length, width, cell_size, rib_width, row_pitch) <= 0:
        raise ArgumentError("Lattice dimensions and row_pitch must be positive")
    edge = rib_width if margin is None else margin
    if edge < rib_width:
        raise ArgumentError("margin cannot be smaller than rib_width")
    available_x, available_y = length - 2 * edge, width - 2 * edge
    if kind == "isogrid":
        height = cell_size * math.sqrt(3) / 2
        rows = _slots(available_y, height, height * row_pitch)
        budget.require(candidate_rows=rows)
        cells = 0
        for row in range(rows):
            span = available_x - ((row * row_stagger) % 1.0) * cell_size
            cells += _slots(span, cell_size, cell_size)
            if include_inverted:
                cells += _slots(span, 1.5 * cell_size, cell_size)
        sides = 3
    elif kind == "hex":
        radius = cell_size / 2
        height = radius * math.sqrt(3)
        columns = _slots(available_x, cell_size, radius * 1.5)
        budget.require(candidate_rows=columns)
        even = _slots(available_y, height, height)
        odd = _slots(available_y - height / 2, height, height)
        cells = ((columns + 1) // 2) * even + (columns // 2) * odd
        sides = 6
    else:
        raise ArgumentError(f"Unknown lattice pattern: {kind}")
    if corner_segments is not None:
        if (isinstance(corner_segments, bool) or not isinstance(corner_segments, int)
                or corner_segments < 2):
            raise ArgumentError("corner_segments must be an integer >= 2")
        sides *= corner_segments + 1
    return budget.require(cells=cells, sketch_curves=cells * sides)


def polygon_counts(polygons, *, limits=None):
    return (limits or Limits.configured()).require(
        cells=len(polygons), sketch_curves=sum(len(polygon) for polygon in polygons))
