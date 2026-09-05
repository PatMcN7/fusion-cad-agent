"""Composable modelling primitives.

Each function here wraps exactly one Fusion feature call and takes care of the
surrounding plumbing that the API leaves to the caller: mapping model
coordinates into sketch space, filtering profiles, restricting cuts to a
participant body, and asserting the result.

The Fusion command itself was never the expensive part -- three lines out of
ten. These primitives exist so the other seven are written once.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import assertions
import fasteners
import pick
from assertions import Check, CheckSuite
from errors import OpError, SelectionError

CM_PER_INCH = 2.54

# ---- planes and sketches -------------------------------------------------

def plane_at(component, z: float, name: str = "Offset Plane"):
    """A construction plane parallel to XY at height ``z``."""
    import adsk.core

    plane_input = component.constructionPlanes.createInput()
    plane_input.setByOffset(
        component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z)
    )
    plane = component.constructionPlanes.add(plane_input)
    plane.name = name
    return plane


def sketch_on(component, target, name: str):
    sketch = component.sketches.add(target)
    sketch.name = name
    return sketch


def _to_sketch(sketch, point):
    """Model coordinates in, sketch coordinates out.

    Every sketch on a face or offset plane has its own coordinate system. Doing
    this mapping by hand at each call site was the single most repeated line in
    the bespoke code.
    """
    import adsk.core

    return sketch.modelToSketchSpace(
        adsk.core.Point3D.create(point[0], point[1], point[2] if len(point) > 2 else 0.0)
    )


def draw_rect(sketch, corner_a, corner_b):
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        _to_sketch(sketch, corner_a), _to_sketch(sketch, corner_b)
    )
    return sketch


def draw_circles(sketch, centers: Sequence[Sequence[float]], radius: float):
    circles = sketch.sketchCurves.sketchCircles
    for center in centers:
        circles.addByCenterRadius(_to_sketch(sketch, center), radius)
    return sketch


def draw_polygons(sketch, polygons: Sequence[Sequence[Sequence[float]]], z: float = 0.0):
    lines = sketch.sketchCurves.sketchLines
    for polygon in polygons:
        points = [_to_sketch(sketch, (p[0], p[1], z)) for p in polygon]
        for index in range(len(points)):
            lines.addByTwoPoints(points[index], points[(index + 1) % len(points)])
    return sketch


# ---- features ------------------------------------------------------------

def extrude(
    component,
    profiles,
    distance: float,
    *,
    operation: str = "new_body",
    participants: Sequence[Any] | None = None,
    symmetric: bool = False,
    name: str | None = None,
):
    """One extrude, in any of the four operations.

    ``participants`` restricts a cut or join to named bodies. Without it, the
    overshoot that makes a through cut robust reaches into whatever else
    happens to be in the way.
    """
    import adsk.core
    import adsk.fusion

    operations = {
        "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
        "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }
    if operation not in operations:
        raise OpError(f"unknown operation '{operation}'")

    features = component.features.extrudeFeatures
    simple = participants is None and not symmetric
    if simple:
        feature = features.addSimple(
            profiles, adsk.core.ValueInput.createByReal(distance), operations[operation]
        )
    else:
        extrude_input = features.createInput(profiles, operations[operation])
        if participants is not None:
            extrude_input.participantBodies = list(participants)
        if symmetric:
            extrude_input.setSymmetricExtent(
                adsk.core.ValueInput.createByReal(distance), True
            )
        else:
            extrude_input.setDistanceExtent(
                False, adsk.core.ValueInput.createByReal(distance)
            )
        feature = features.add(extrude_input)
    if name:
        feature.name = name
    return feature


def cut_holes(
    component,
    target_body,
    plane_or_face,
    centers: Sequence[Sequence[float]],
    radius: float,
    depth: float,
    *,
    name: str,
    suite: CheckSuite | None = None,
):
    """Drill a set of holes into one body. The most repeated operation there is.

    ``depth`` is signed and measured along the sketch's own normal: negative
    cuts into a face, positive cuts away from it. An earlier version inferred
    the sign from whether the sketch target was a face or a plane, which was
    invisible at the call site and wrong for any inward-facing face.
    """
    sketch = sketch_on(component, plane_or_face, name)
    draw_circles(sketch, centers, radius)
    selection = pick.profiles(sketch, loops=1, curves=1)
    if suite is not None:
        suite.add(
            assertions.count_equals(
                f"{name}_profiles", len(selection), len(centers),
                f"{sketch.profiles.count} total sketch profiles",
            )
        )
        suite.require()
    before = target_body.volume
    feature = extrude(
        component,
        selection.collection(),
        depth,
        operation="cut",
        participants=[target_body],
        name=f"{name} Cut",
    )
    if suite is not None:
        # A cut driven the wrong way removes nothing at all.
        suite.add(
            assertions.value_decreased(f"{name}_removed_material", before, target_body.volume)
        )
    return feature


def cut_polygons(
    component,
    target_body,
    plane_or_face,
    polygons,
    depth: float,
    z: float,
    *,
    name: str,
    suite: CheckSuite | None = None,
    sides: int = 3,
):
    """Cut polygonal pockets. ``depth`` is signed along the sketch normal."""
    sketch = sketch_on(component, plane_or_face, name)
    draw_polygons(sketch, polygons, z)
    selection = pick.profiles(sketch, loops=1, curves=sides)
    if suite is not None:
        suite.add(
            assertions.count_equals(
                f"{name}_profiles", len(selection), len(polygons),
                f"{sketch.profiles.count} total sketch profiles",
            )
        )
        suite.require()
    before = target_body.volume
    feature = extrude(
        component,
        selection.collection(),
        depth,
        operation="cut",
        participants=[target_body],
        name=f"{name} Cut",
    )
    if suite is not None:
        suite.add(
            assertions.value_decreased(f"{name}_removed_material", before, target_body.volume)
        )
    return feature


def circular_pattern(component, entities, axis, count: int, *, name: str = "Pattern"):
    import adsk.core

    collection = adsk.core.ObjectCollection.create()
    for entity in entities:
        collection.add(entity)
    pattern_input = component.features.circularPatternFeatures.createInput(collection, axis)
    pattern_input.quantity = adsk.core.ValueInput.createByReal(count)
    pattern_input.totalAngle = adsk.core.ValueInput.createByString("360 deg")
    pattern_input.isSymmetric = False
    feature = component.features.circularPatternFeatures.add(pattern_input)
    feature.name = name
    return feature


def fillet_all_edges(
    component, body, radius: float, *, fallbacks=(1.0, 0.75, 0.5, 0.25), suite=None
):
    """All-edge fillet with a retry ladder, reporting the radius that held."""
    import adsk.core

    attempted = []
    for scale in fallbacks:
        candidate = radius * scale
        attempted.append(candidate)
        try:
            fillet_input = component.features.filletFeatures.createInput()
            fillet_input.isRollingBallCorner = True
            fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
                pick.edges(body).collection(),
                adsk.core.ValueInput.createByReal(candidate),
                True,
            )
            feature = component.features.filletFeatures.add(fillet_input)
            feature.name = "All Edge Fillet"
            if suite is not None:
                suite.add(
                    Check("fillet_applied", True, f"{candidate:.5f} cm",
                          f"one of {[round(a, 5) for a in attempted]} cm")
                )
            return feature, candidate
        except Exception:
            continue
    if suite is not None:
        suite.add(
            Check("fillet_applied", False, "none",
                  f"one of {[round(a, 5) for a in attempted]} cm")
        )
        suite.require()
    return None, None


def fillet_edges(component, edges, radius: float, *, name: str = "Edge Fillet"):
    """Apply one exact-radius fillet to an explicit edge collection."""
    import adsk.core

    collection = adsk.core.ObjectCollection.create()
    for edge in edges:
        collection.add(edge)
    if collection.count == 0:
        raise ValueError("fillet_edges requires at least one edge")
    fillet_input = component.features.filletFeatures.createInput()
    fillet_input.isRollingBallCorner = True
    fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
        collection,
        adsk.core.ValueInput.createByReal(radius),
        False,
    )
    feature = component.features.filletFeatures.add(fillet_input)
    feature.name = name
    return feature


def thread(
    component,
    faces,
    diameter: float,
    *,
    internal: bool,
    family: str = "unified",
    modeled: bool = False,
    expected: str | None = None,
    label: str = "thread",
    suite: CheckSuite | None = None,
):
    """Tap or thread cylindrical faces, asserting the size is the one asked for."""
    threads = component.features.threadFeatures
    query = threads.threadDataQuery

    thread_type = None
    for candidate in query.allThreadTypes:
        if family.lower() in candidate.lower():
            thread_type = candidate
            break
    if thread_type is None:
        raise OpError(
            f"no thread family matching '{family}'. Available: "
            + ", ".join(query.allThreadTypes)
        )

    ok, designation, thread_class = query.recommendThreadData(diameter, internal, thread_type)
    if suite is not None:
        suite.add(
            Check(f"{label}_designation", bool(ok), designation if ok else "none",
                  f"a {thread_type} size for {diameter / CM_PER_INCH:.4f} in")
        )
        implied = fasteners.designation_major_diameter_cm(designation) if ok else None
        suite.add(
            Check(
                f"{label}_matches_diameter",
                implied is not None and abs(implied - diameter) <= 1e-4,
                f"{designation} = {implied / CM_PER_INCH:.4f} in" if implied else "unparsed",
                f"{diameter / CM_PER_INCH:.4f} in",
                "thread family does not match the hole size",
            )
        )
        if expected:
            suite.add(Check(f"{label}_expected", designation == expected, designation, expected))
        suite.require()
    if not ok:
        return None

    info = threads.createThreadInfo(internal, thread_type, designation, thread_class)
    thread_input = threads.createInput(faces.collection(), info)
    thread_input.isModeled = bool(modeled)
    thread_input.isFullLength = True
    threads.add(thread_input)
    return {
        "designation": designation,
        "thread_class": thread_class,
        "thread_type": thread_type,
        "modeled": bool(modeled),
        "faces": len(faces),
    }


def set_material(bodies, *, app, keyword: str = "6061"):
    libraries = app.materialLibraries
    exact, fallback = [], []
    for library_index in range(libraries.count):
        library = libraries.item(library_index)
        for material_index in range(library.materials.count):
            material = library.materials.item(material_index)
            lowered = material.name.lower()
            source = f"{library.name} / {material.name}"
            if keyword in lowered:
                exact.append((2 if "t6" in lowered else 1, material, source))
            elif "aluminum" in lowered or "aluminium" in lowered:
                fallback.append((material, source))
    if exact:
        _, material, source = max(exact, key=lambda c: c[0])
    elif fallback:
        material, source = fallback[0]
    else:
        return None
    for body in bodies:
        body.material = material
    return source


# ---- joints ---------------------------------------------------------------
#
# Joints are where the project started: a mate that looks right in the tree
# and is silently backwards. Everything here is arranged so the direction is
# decided by measurement rather than by whoever guessed the flag.

JOINT_MOTIONS = ("rigid", "revolute", "slider", "cylindrical", "planar", "ball")

# Fusion names joint axes as its own enum rather than as a vector.
JOINT_AXES = {"x": "XAxisJointDirection", "y": "YAxisJointDirection",
              "z": "ZAxisJointDirection"}

KEY_POINTS = ("center", "start", "end", "middle")


def joint_geometry(face, key_point: str = "center", edge=None):
    """A joint target on a face.

    Planar and non-planar faces take different constructors: a flat face is
    located by an edge key point, a cylinder by its own axis. Choosing between
    them here means callers name a face and a key point rather than knowing
    which Fusion factory applies.
    """
    import adsk.core
    import adsk.fusion

    lookup = {
        "center": adsk.fusion.JointKeyPointTypes.CenterKeyPoint,
        "start": adsk.fusion.JointKeyPointTypes.StartKeyPoint,
        "end": adsk.fusion.JointKeyPointTypes.EndKeyPoint,
        "middle": adsk.fusion.JointKeyPointTypes.MiddleKeyPoint,
    }
    if key_point not in lookup:
        raise SelectionError(
            f"key_point must be one of: {', '.join(KEY_POINTS)}")
    point = lookup[key_point]

    surface = face.geometry.surfaceType
    if surface == adsk.core.SurfaceTypes.PlaneSurfaceType:
        # A planar face needs an edge to resolve the key point against.
        chosen = edge or face.edges.item(0)
        geometry = adsk.fusion.JointGeometry.createByPlanarFace(face, chosen, point)
    else:
        geometry = adsk.fusion.JointGeometry.createByNonPlanarFace(face, point)
    if not geometry:
        raise SelectionError(
            "could not build joint geometry from that face; try a different "
            "face or key_point")
    return geometry


def create_joint(component, geometry_one, geometry_two, *,
                 motion: str = "rigid", axis: str = "z",
                 offset: float = 0.0, angle: float = 0.0,
                 flipped: bool = False, name: str = "Joint"):
    """Add one joint. The direction is set, not discovered -- see tools.py."""
    import adsk.core
    import adsk.fusion

    if motion not in JOINT_MOTIONS:
        raise SelectionError(f"motion must be one of: {', '.join(JOINT_MOTIONS)}")
    if axis not in JOINT_AXES:
        raise SelectionError("axis must be x, y or z")

    joints = component.joints
    joint_input = joints.createInput(geometry_one, geometry_two)
    direction = getattr(adsk.fusion.JointDirections, JOINT_AXES[axis])

    if motion == "rigid":
        joint_input.setAsRigidJointMotion()
    elif motion == "revolute":
        joint_input.setAsRevoluteJointMotion(direction)
    elif motion == "slider":
        joint_input.setAsSliderJointMotion(direction)
    elif motion == "cylindrical":
        joint_input.setAsCylindricalJointMotion(direction)
    elif motion == "planar":
        joint_input.setAsPlanarJointMotion(direction)
    else:
        joint_input.setAsBallJointMotion()

    if offset:
        joint_input.offset = adsk.core.ValueInput.createByReal(offset)
    if angle:
        joint_input.angle = adsk.core.ValueInput.createByReal(angle)
    joint_input.isFlipped = bool(flipped)

    joint = joints.add(joint_input)
    joint.name = name
    return joint


def occurrence_translation(occurrence) -> tuple[float, float, float]:
    """Where an occurrence currently sits, as (x, y, z) in cm."""
    translation = occurrence.transform2.translation
    return (translation.x, translation.y, translation.z)
