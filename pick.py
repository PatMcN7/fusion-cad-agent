"""Geometry selection: a query language for naming the thing you mean.

In the GUI, selection is free -- your cursor resolves "that face" instantly.
Through the API there is no cursor, so every selection becomes a search. That
search was seven of every ten lines of the bespoke part code this replaces.

Nothing here imports adsk at module scope, so the predicate logic stays
importable and testable outside Fusion.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from errors import SelectionError

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def parse_direction(text: str) -> tuple[int, int]:
    """'+z' -> (2, 1); '-y' -> (1, -1); 'z' -> (2, 0) meaning either sign."""
    if not isinstance(text, str) or not text:
        raise SelectionError("direction must be a string like '+z', '-y' or 'x'")
    body = text.strip().lower()
    sign = 0
    if body[0] in "+-":
        sign = 1 if body[0] == "+" else -1
        body = body[1:]
    if body not in AXIS_INDEX:
        raise SelectionError(f"unknown axis '{text}'")
    return AXIS_INDEX[body], sign


def _bbox_span(entity, axis: int) -> tuple[float, float]:
    box = entity.boundingBox
    low = (box.minPoint.x, box.minPoint.y, box.minPoint.z)[axis]
    high = (box.maxPoint.x, box.maxPoint.y, box.maxPoint.z)[axis]
    return low, high


def _bbox_mid(entity, axis: int) -> float:
    low, high = _bbox_span(entity, axis)
    return (low + high) / 2


class Selection:
    """A filtered, chainable set of Fusion entities."""

    def __init__(self, items: Sequence[Any], kind: str = "entity", note: str = ""):
        self.items = list(items)
        self.kind = kind
        self.note = note or kind

    # ---- plumbing -------------------------------------------------------
    def _derive(self, items, note: str) -> "Selection":
        return Selection(items, self.kind, f"{self.note}.{note}")

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def where(self, predicate: Callable[[Any], bool], note: str = "where") -> "Selection":
        return self._derive([i for i in self.items if predicate(i)], note)

    # ---- surface kind ---------------------------------------------------
    def planar(self) -> "Selection":
        def matches(face):
            import adsk.core

            return face.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType

        return self.where(matches, "planar")

    def cylindrical(self) -> "Selection":
        def matches(face):
            import adsk.core

            return face.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType

        return self.where(matches, "cylindrical")

    # ---- orientation ----------------------------------------------------
    def normal_axis(self, axis: str, tolerance: float = 1e-6) -> "Selection":
        """Planar faces whose normal lies along an axis, either facing.

        Takes an unsigned axis only. Signed direction is what facing() is for --
        an earlier version of this method accepted '+y', parsed the sign, and
        then ignored it, so '+y' and '-y' returned the same faces.
        """
        index, sign = parse_direction(axis)
        if sign != 0:
            raise SelectionError(
                f"normal_axis() takes an unsigned axis; use facing('{axis}') "
                "to select by which way the face points"
            )
        def matches(face):
            import adsk.core

            plane = adsk.core.Plane.cast(face.geometry)
            if plane is None:
                return False
            components = (plane.normal.x, plane.normal.y, plane.normal.z)
            return abs(abs(components[index]) - 1.0) <= tolerance

        return self.where(matches, f"normal_axis({axis})")

    def facing(self, direction: str, tolerance: float = 1e-6) -> "Selection":
        """Planar faces whose *outward* normal points this way.

        A face's stored surface normal is reversed relative to the solid when
        isParamReversed is set, so the raw plane normal alone is not the
        direction the face actually faces.
        """
        index, sign = parse_direction(direction)
        if sign == 0:
            raise SelectionError("facing() needs a signed direction like '+z'")
        def matches(face):
            import adsk.core

            plane = adsk.core.Plane.cast(face.geometry)
            if plane is None:
                return False
            components = (plane.normal.x, plane.normal.y, plane.normal.z)
            value = components[index]
            if abs(abs(value) - 1.0) > tolerance:
                return False
            if getattr(face, "isParamReversed", False):
                value = -value
            return (value > 0) == (sign > 0)

        return self.where(matches, f"facing({direction})")

    def axis_along(self, direction: str, tolerance: float = 1e-6) -> "Selection":
        """Cylindrical faces whose axis runs along an axis."""
        axis, _ = parse_direction(direction)

        def matches(face):
            import adsk.core

            cylinder = adsk.core.Cylinder.cast(face.geometry)
            if cylinder is None:
                return False
            components = (cylinder.axis.x, cylinder.axis.y, cylinder.axis.z)
            return abs(abs(components[axis]) - 1.0) <= tolerance

        return self.where(matches, f"axis_along({direction})")

    def radius(self, value: float, tolerance: float = 1e-5) -> "Selection":
        def matches(face):
            import adsk.core

            cylinder = adsk.core.Cylinder.cast(face.geometry)
            return cylinder is not None and abs(cylinder.radius - value) <= tolerance

        return self.where(matches, f"radius({value:.5f})")

    # ---- position -------------------------------------------------------
    def extreme(self, direction: str, tolerance: float = 1e-7) -> "Selection":
        """Keep the entities furthest along a direction.

        '+z' keeps the highest, '-z' the lowest. Ties within tolerance are all
        kept, which is what you want for a ring of coplanar faces.
        """
        axis, sign = parse_direction(direction)
        if sign == 0:
            raise SelectionError("extreme() needs a signed direction like '+z'")
        if not self.items:
            return self._derive([], f"extreme({direction})")
        values = [_bbox_mid(i, axis) for i in self.items]
        best = max(values) if sign > 0 else min(values)
        kept = [i for i, v in zip(self.items, values) if abs(v - best) <= tolerance]
        return self._derive(kept, f"extreme({direction})")

    def at(self, direction: str, value: float, tolerance: float = 1e-6) -> "Selection":
        axis, _ = parse_direction(direction)
        return self.where(
            lambda i: abs(_bbox_mid(i, axis) - value) <= tolerance,
            f"at({direction}={value:.5f})",
        )

    def spans_at_most(self, direction: str, limit: float) -> "Selection":
        axis, _ = parse_direction(direction)

        def matches(entity):
            low, high = _bbox_span(entity, axis)
            return (high - low) <= limit

        return self.where(matches, f"spans_at_most({direction},{limit:.5f})")

    def named(self, name: str) -> "Selection":
        return self.where(lambda i: getattr(i, "name", None) == name, f"named({name})")

    def name_starts(self, prefix: str) -> "Selection":
        return self.where(
            lambda i: str(getattr(i, "name", "")).startswith(prefix), f"name_starts({prefix})"
        )

    # ---- terminals ------------------------------------------------------
    def all(self) -> list:
        return list(self.items)

    def one(self):
        if len(self.items) != 1:
            raise SelectionError(
                f"{self.note} resolved to {len(self.items)} entities, expected exactly 1"
            )
        return self.items[0]

    def first(self):
        if not self.items:
            raise SelectionError(f"{self.note} resolved to nothing")
        return self.items[0]

    def expect(self, count: int) -> "Selection":
        if len(self.items) != count:
            raise SelectionError(
                f"{self.note} resolved to {len(self.items)} entities, expected {count}"
            )
        return self

    def collection(self):
        import adsk.core

        collection = adsk.core.ObjectCollection.create()
        for item in self.items:
            collection.add(item)
        return collection

    def centers(self) -> list[tuple[float, float, float]]:
        """Axis points of cylindrical faces -- a bolt pattern, as coordinates."""
        import adsk.core

        points = []
        for face in self.items:
            cylinder = adsk.core.Cylinder.cast(face.geometry)
            if cylinder is not None:
                points.append((cylinder.origin.x, cylinder.origin.y, cylinder.origin.z))
        return points


def position(entity, direction: str) -> float:
    """Where an entity sits along an axis -- the plane position of a planar face."""
    axis, _ = parse_direction(direction)
    return _bbox_mid(entity, axis)


# ---- entry points --------------------------------------------------------

def faces(body) -> Selection:
    return Selection([body.faces.item(i) for i in range(body.faces.count)], "face", "faces")


def edges(body) -> Selection:
    return Selection([body.edges.item(i) for i in range(body.edges.count)], "edge", "edges")


def bodies(component) -> Selection:
    return Selection(
        [component.bRepBodies.item(i) for i in range(component.bRepBodies.count)],
        "body",
        "bodies",
    )


def occurrences(component) -> Selection:
    """Placed components, the things joints actually move.

    A joint acts on occurrences, not bodies: two instances of the same
    component are one component and two occurrences, and mating the component
    would move both. Selecting them shares the Selection filters, so
    `.named()` and `.name_starts()` work here as they do for faces.
    """
    items = [component.allOccurrences.item(i)
             for i in range(component.allOccurrences.count)]
    return Selection(items, "occurrence", "occurrences")


def profiles(sketch, loops: int | None = None, curves: int | None = None) -> Selection:
    """Sketch profiles, optionally filtered by loop topology.

    Sketching on a face pulls that face's boundary into profile computation, so
    a sketch of N shapes reports N+1 profiles: the shapes, plus a background
    profile carrying all of them as inner loops. Feeding that background to a
    cut removes the part and leaves the shapes standing. Filtering on loop and
    curve counts is what separates them.
    """
    items = [sketch.profiles.item(i) for i in range(sketch.profiles.count)]
    selection = Selection(items, "profile", "profiles")
    if loops is not None:
        selection = selection.where(
            lambda p: p.profileLoops.count == loops, f"loops({loops})"
        )
    if curves is not None:
        selection = selection.where(
            lambda p: p.profileLoops.item(0).profileCurves.count == curves,
            f"curves({curves})",
        )
    return selection
