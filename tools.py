"""Tool implementations. Hot-reloaded on every request.

Every part here is composed from pick (selection) and ops (primitives). The
bespoke first generation -- twelve hand-written helpers and a 178-line tube
frame -- has been deleted; what it did now lives in the shared layer where a
fix reaches every part at once.

Keep the HTTP server and event registration out of this module: those cannot
be swapped underneath a live socket.
"""

from __future__ import annotations

import importlib as _importlib
import math
from pathlib import Path
from typing import Any

import adsk.core
import adsk.fusion

import assertions
import complexity
import fasteners
import geom
import mechanics
import ops
import patterns
import pick
import workspace
import editing
import tool_contracts
from assertions import Check, CheckSuite
from checkpoints import operation, tool_scope
from errors import (ArgumentError, BridgeError, PatternError, SelectionError,
                    ToolFailure)

# workspace.py joined RELOADABLE in the add-in shell, but that file is the one
# module a Fusion restart is required to change. Reloading it here means a fix
# to the file tools lands on the next call like every other geometry fix.
# Nobody should have to restart Fusion to receive a bug fix.
_importlib.reload(workspace)
_importlib.reload(editing)

# pick/ops/geom/fasteners are imported here rather than by the shell, so
# reload them alongside this module. Order matters: ops imports pick.
for _module in (complexity, geom, fasteners, mechanics, patterns, assertions, pick, ops):
    _importlib.reload(_module)

CM_PER_INCH = 2.54

APP: Any = None
UI: Any = None


def bind(app: Any, ui: Any) -> None:
    global APP, UI
    APP, UI = app, ui


# --------------------------------------------------------------- helpers ----

def _safe_name(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ArgumentError("name must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 100:
        raise ArgumentError("name must contain 1 to 100 characters")
    return cleaned


def _active_design() -> adsk.fusion.Design:
    design = adsk.fusion.Design.cast(APP.activeProduct)
    if not design:
        raise ToolFailure("The active document is not a Fusion design.")
    return design


def _new_design(name: str = "Untitled"):
    document = APP.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = adsk.fusion.Design.cast(APP.activeProduct)
    if not design:
        raise ToolFailure("Fusion failed to create a design document.")
    return document, design


def _cm(units, value: Any, field: str, *, allow_zero: bool = False) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ArgumentError(f"{field} must be a Fusion unit expression string")
    result = units.evaluateExpression(value, "cm")
    if allow_zero:
        if result < 0:
            raise ArgumentError(f"{field} must evaluate to a non-negative length")
    elif result <= 0:
        raise ArgumentError(f"{field} must evaluate to a positive length")
    return result


def _values(units, expressions: dict[str, str], zero_ok: tuple[str, ...] = ()) -> dict[str, float]:
    return {
        field: _cm(units, expression, field, allow_zero=(field in zero_ok))
        for field, expression in expressions.items()
    }


def _point(point) -> dict[str, float]:
    return {"x_cm": point.x, "y_cm": point.y, "z_cm": point.z}


def _measure(body, attribute: str):
    """A kernel measurement, or None if the kernel refuses.

    Imported geometry is not always valid. A STEP from a vendor produced a
    body whose volume raised InternalValidationError, and because
    design_summary asked every body for its volume, one bad solid removed
    the agent's ability to see the design at all. A measurement that cannot
    be taken is missing data, not a reason to fail the call.
    """
    try:
        return getattr(body, attribute)
    except Exception:
        return None


def _body_summary(body) -> dict[str, Any]:
    try:
        bounds = body.preciseBoundingBox or body.boundingBox
    except Exception:
        bounds = None
    material = None
    try:
        if body.material:
            material = body.material.name
    except Exception:
        material = None
    return {
        "name": body.name,
        "entity_token": body.entityToken,
        "is_solid": bool(_measure(body, "isSolid")),
        "volume_cm3": _measure(body, "volume"),
        "area_cm2": _measure(body, "area"),
        "faces": body.faces.count,
        "edges": body.edges.count,
        "vertices": body.vertices.count,
        "material": material,
        "bounding_box": ({"min": _point(bounds.minPoint),
                          "max": _point(bounds.maxPoint)} if bounds else None),
        "unmeasurable": _measure(body, "volume") is None or bounds is None,
    }


def _document_metadata() -> dict[str, Any]:
    document = APP.activeDocument
    if not document:
        return {"open": False}
    data_file_id = None
    try:
        if document.dataFile:
            data_file_id = document.dataFile.id
    except Exception:
        data_file_id = None
    return {
        "open": True,
        "name": document.name,
        "is_saved": bool(document.isSaved),
        "is_modified": bool(document.isModified),
        "data_file_id": data_file_id,
    }


def _clear_root(root) -> dict[str, list[str]]:
    deleted: dict[str, list[str]] = {"features": [], "sketches": [], "bodies": []}
    while root.features.count:
        feature = root.features.item(root.features.count - 1)
        deleted["features"].append(feature.name)
        if not feature.deleteMe():
            raise ToolFailure(f"Could not delete existing feature: {feature.name}")
    while root.sketches.count:
        sketch = root.sketches.item(root.sketches.count - 1)
        deleted["sketches"].append(sketch.name)
        if not sketch.deleteMe():
            raise ToolFailure(f"Could not delete existing sketch: {sketch.name}")
    while root.bRepBodies.count:
        body = root.bRepBodies.item(root.bRepBodies.count - 1)
        deleted["bodies"].append(body.name)
        if not body.deleteMe():
            raise ToolFailure(f"Could not delete existing body: {body.name}")
    return deleted


# ------------------------------------------------------------- read-only ----

def health(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "bridge": "fusion-cad-agent",
        "fusion_version": APP.version,
        "main_thread_responding": True,
        "active_document": _document_metadata(),
    }


def active_document(_: dict[str, Any]) -> dict[str, Any]:
    return _document_metadata()


def save_document(arguments: dict[str, Any]) -> dict[str, Any]:
    """Save the already-named active Fusion document in place."""
    document = APP.activeDocument
    if not document:
        raise ToolFailure("No active Fusion document to save.")
    if not document.isSaved:
        raise ToolFailure(
            "The active document has no existing save target; use Fusion Save As first."
        )

    description = str(arguments.get("description", "Saved by Fusion CAD Agent"))
    saved = bool(document.save(description)) if document.isModified else True
    check = CheckSuite("save")
    check.add(Check(
        "document_saved",
        saved and not document.isModified,
        f"save_returned={saved}, is_modified={bool(document.isModified)}",
        "save_returned=True, is_modified=False",
    ))
    check.require()
    return {"document": _document_metadata(), "verification": check.report()}


def delete_named_features(arguments: dict[str, Any]) -> dict[str, Any]:
    """Delete explicitly named features and now-unused sketches in order."""
    design = _active_design()
    root = design.rootComponent
    feature_names = arguments.get("feature_names", [])
    sketch_names = arguments.get("sketch_names", [])
    if not isinstance(feature_names, list):
        raise ArgumentError("feature_names must be a list")
    if not feature_names and not sketch_names:
        raise ArgumentError("Supply explicit feature_names or sketch_names")
    if not isinstance(sketch_names, list):
        raise ArgumentError("sketch_names must be a list")

    references = []
    for names, collection in ((feature_names, root.features), (sketch_names, root.sketches)):
        for name in names:
            matches = [entity for entity in editing.items(collection) if entity.name == name]
            if len(matches) != 1:
                raise ArgumentError(f"Expected exactly one entity named {name!r}, found {len(matches)}; use feature_tree references")
            references.append(editing.ref(design, matches[0]))
    result = editing.delete(design, references)
    result.update(deleted_features=feature_names, deleted_sketches=sketch_names)
    return result


def set_6061_material(arguments: dict[str, Any]) -> dict[str, Any]:
    """Assign the repository's standard 6061-T6 material to named bodies."""
    design = _active_design()
    root = design.rootComponent
    names = arguments.get("body_names", [])
    if not isinstance(names, list) or not names:
        raise ArgumentError("body_names must be a non-empty list")
    bodies = []
    for name in names:
        selection = pick.bodies(root).named(str(name))
        if len(selection) != 1:
            raise ToolFailure(
                f"expected exactly one body named '{name}', found {len(selection)}"
            )
        bodies.append(selection.one())
    source = ops.set_material(bodies, app=APP)
    suite = CheckSuite("material")
    for body in bodies:
        material = body.material.name if body.material else "None"
        suite.add(Check(
            f"{body.name}_is_6061",
            "6061" in material,
            material,
            "material name containing 6061",
        ))
    suite.require()
    return {
        "body_names": names,
        "material_source": source,
        "verification": suite.report(),
    }


def router_fillet_pocket_corners(arguments: dict[str, Any]) -> dict[str, Any]:
    """Radius vertical pocket corners to the specified router cutter radius."""
    design = _active_design()
    root = design.rootComponent
    body = _resolve_target(root, arguments.get("body"))
    cutter_diameter = _cm(
        design.unitsManager,
        arguments.get("cutter_diameter", "0.125 in"),
        "cutter_diameter",
    )
    radius = cutter_diameter / 2
    box = body.preciseBoundingBox or body.boundingBox
    x_limit = max(abs(box.minPoint.x), abs(box.maxPoint.x))
    y_limit = max(abs(box.minPoint.y), abs(box.maxPoint.y))
    outer_tolerance = 1e-5

    candidates = []
    corner_angles = []
    for edge in pick.edges(body):
        line = adsk.core.Line3D.cast(edge.geometry)
        if line is None:
            continue
        direction = line.startPoint.vectorTo(line.endPoint)
        if not direction.normalize() or abs(abs(direction.z) - 1.0) > 1e-6:
            continue
        edge_box = edge.boundingBox
        midpoint_x = (edge_box.minPoint.x + edge_box.maxPoint.x) / 2
        midpoint_y = (edge_box.minPoint.y + edge_box.maxPoint.y) / 2
        if (abs(midpoint_x) >= x_limit - outer_tolerance or
                abs(midpoint_y) >= y_limit - outer_tolerance):
            continue
        faces = edge.faces
        if faces.count != 2:
            continue
        planes = [adsk.core.Plane.cast(faces.item(i).geometry) for i in range(2)]
        if any(plane is None for plane in planes):
            continue
        if any(abs(plane.normal.z) > 1e-6 for plane in planes):
            continue
        dot = max(-1.0, min(1.0, planes[0].normal.dotProduct(planes[1].normal)))
        angle = math.degrees(math.acos(dot))
        if angle <= 1e-3 or angle >= 179.999:
            continue
        candidates.append(edge)
        corner_angles.append(angle)

    preflight = CheckSuite("preflight")
    preflight.add(assertions.at_least(
        "router_corners_found", len(candidates), 1, "edges"
    ))
    preflight.add(assertions.within_tolerance(
        "fillet_matches_cutter_radius", radius, cutter_diameter / 2, 1e-9, "cm"
    ))
    preflight.require()
    reports = [preflight.report()]

    before_volume = body.volume
    before_radius_faces = len(
        pick.faces(body).cylindrical().axis_along("z").radius(radius, 1e-4)
    )
    length = box.maxPoint.x - box.minPoint.x
    width = box.maxPoint.y - box.minPoint.y
    thickness = box.maxPoint.z - box.minPoint.z
    with operation(design, "router_corner_fillet") as (_, suite):
        ops.fillet_edges(
            root,
            candidates,
            radius,
            name=arguments.get("name", "Router Pocket Corner Fillets"),
        )
        suite.add(Check(
            "target_still_solid", body.isSolid, str(body.isSolid), "True"
        ))
        suite.add(assertions.bounding_box_matches(
            "outer_dimensions_held", body, length, width, thickness
        ))
        radius_faces = len(
            pick.faces(body).cylindrical().axis_along("z").radius(radius, 1e-4)
        )
        suite.add(assertions.at_least(
            "router_radius_faces",
            radius_faces - before_radius_faces,
            len(candidates),
            "faces",
        ))
        suite.add(Check(
            "geometry_changed",
            abs(body.volume - before_volume) > 1e-6,
            f"volume delta {body.volume - before_volume:.6f} cm^3",
            "non-zero volume change",
        ))
        suite.require()
    reports.append(suite.report())

    rules = [rule.as_dict() for rule in mechanics.machining_rules(
        thickness / CM_PER_INCH,
        float(arguments.get("rib_width_in", 0.25)),
        radius / CM_PER_INCH,
    )]
    return {
        "body": _body_summary(body),
        "cutter_diameter_in": cutter_diameter / CM_PER_INCH,
        "fillet_radius_in": radius / CM_PER_INCH,
        "edges_filleted": len(candidates),
        "corner_angle_range_degrees": [min(corner_angles), max(corner_angles)],
        "machining_rules": rules,
        "verification": reports,
    }


def import_step_component(arguments: dict[str, Any]) -> dict[str, Any]:
    """Import a STEP assembly as a component or prefixed root-body group."""
    design = _active_design()
    root = design.rootComponent
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ArgumentError("path must be an absolute STEP file path")
    path = Path(raw_path).expanduser().resolve()
    if path.suffix.lower() not in {".step", ".stp"}:
        raise ArgumentError("path must end in .step or .stp")
    if not path.is_file():
        raise ToolFailure(f"STEP file does not exist: {path}")
    name = _safe_name(arguments.get("name"), path.stem)

    flatten = bool(arguments.get("flatten_into_root", False))
    reports = []
    if flatten:
        before_tokens = {body.entityToken for body in pick.bodies(root)}
        with operation(design, "import_step_bodies") as (_, suite):
            options = APP.importManager.createSTEPImportOptions(str(path))
            imported = bool(APP.importManager.importToTarget(options, root))
            suite.add(Check("step_imported", imported, str(imported), "True"))
            suite.require()
            imported_bodies = [
                body for body in pick.bodies(root)
                if body.entityToken not in before_tokens
            ]
            suite.add(assertions.at_least(
                "imported_bodies", len(imported_bodies), 1, "bodies"
            ))
            suite.require()
            for index, body in enumerate(imported_bodies, 1):
                original = body.name
                body.name = f"{name} :: {index:03d} :: {original}"[:250]
        reports.append(suite.report())

        bounds = [body.preciseBoundingBox or body.boundingBox for body in imported_bodies]
        minima = [
            min(getattr(box.minPoint, axis) for box in bounds)
            for axis in ("x", "y", "z")
        ]
        maxima = [
            max(getattr(box.maxPoint, axis) for box in bounds)
            for axis in ("x", "y", "z")
        ]
        dimensions = [high - low for low, high in zip(minima, maxima)]
        return {
            "path": str(path),
            "mode": "flattened_root_bodies",
            "body_prefix": f"{name} ::",
            "imported_bodies": len(imported_bodies),
            "body_name_sample": [body.name for body in imported_bodies[:12]],
            "bounding_box": {
                "min": {f"{axis}_cm": value for axis, value in zip(("x", "y", "z"), minima)},
                "max": {f"{axis}_cm": value for axis, value in zip(("x", "y", "z"), maxima)},
                "dimensions_cm": dimensions,
                "dimensions_in": [value / CM_PER_INCH for value in dimensions],
            },
            "verification": reports,
        }

    before_occurrences = root.allOccurrences.count
    with operation(design, "import_step_component") as (_, suite):
        occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        options = APP.importManager.createSTEPImportOptions(str(path))
        imported = bool(APP.importManager.importToTarget(options, occurrence.component))
        suite.add(Check("step_imported", imported, str(imported), "True"))
        suite.require()
        direct_bodies = occurrence.component.bRepBodies.count
        nested_bodies = sum(
            occurrence.component.allOccurrences.item(i).bRepBodies.count
            for i in range(occurrence.component.allOccurrences.count)
        )
        total_bodies = direct_bodies + nested_bodies
        suite.add(assertions.at_least("imported_bodies", total_bodies, 1, "bodies"))
        suite.add(assertions.count_equals(
            "root_occurrence_added", root.allOccurrences.count,
            before_occurrences + 1 + occurrence.component.allOccurrences.count,
        ))
        suite.require()
    reports.append(suite.report())

    box = occurrence.boundingBox
    dimensions = [
        box.maxPoint.x - box.minPoint.x,
        box.maxPoint.y - box.minPoint.y,
        box.maxPoint.z - box.minPoint.z,
    ]
    return {
        "path": str(path),
        "mode": "grouped_component",
        "occurrence": occurrence.name,
        "component": occurrence.component.name,
        "direct_bodies": direct_bodies,
        "nested_occurrences": occurrence.component.allOccurrences.count,
        "nested_bodies": nested_bodies,
        "bounding_box": {
            "min": _point(box.minPoint),
            "max": _point(box.maxPoint),
            "dimensions_cm": dimensions,
            "dimensions_in": [value / CM_PER_INCH for value in dimensions],
        },
        "verification": reports,
    }


def design_summary(_: dict[str, Any]) -> dict[str, Any]:
    design = _active_design()
    components = []
    for index in range(design.allComponents.count):
        component = design.allComponents.item(index)
        components.append({
            "name": component.name,
            "features": component.features.count,
            "feature_names": [
                component.features.item(i).name
                for i in range(component.features.count)
            ],
            "sketches": component.sketches.count,
            "sketch_names": [
                component.sketches.item(i).name
                for i in range(component.sketches.count)
            ],
            "bodies": [_body_summary(b) for b in pick.bodies(component)],
        })
    return {
        "document": _document_metadata(),
        "timeline_entries": design.timeline.count,
        "component_count": design.allComponents.count,
        "occurrence_count": design.rootComponent.allOccurrences.count,
        "components": components,
    }


def check_interference(_: dict[str, Any]) -> dict[str, Any]:
    design = _active_design()
    suite = CheckSuite("interference")
    suite.add(assertions.no_interference("no_interference", design))
    return suite.report()


def verify_spec(arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a design spec as a test suite against the active design.

    Every check delegates to the assertions module, so the spec runner and the
    build path cannot disagree about what "fits envelope" means.
    """
    design = _active_design()
    root = design.rootComponent
    units = design.unitsManager
    suite = CheckSuite("spec")

    count = root.bRepBodies.count
    if count == 0:
        suite.add(Check("has_geometry", False, "0 bodies", ">= 1 body"))
        return suite.report()

    tolerance = float(arguments.get("tolerance_cm", 1e-6))

    if arguments.get("body_count") is not None:
        suite.add(assertions.count_equals(
            "body_count", count, int(arguments["body_count"])))

    if arguments.get("require_single_solid", False):
        suite.add(assertions.single_solid_body("single_solid", root))
    else:
        suite.add(assertions.all_bodies_solid("all_bodies_solid", root))

    if arguments.get("envelope"):
        allowed = [_cm(units, e, "envelope") for e in arguments["envelope"]]
        suite.add(assertions.assembly_fits_envelope("fits_envelope", root, allowed))

    if arguments.get("dimensions"):
        wanted = [_cm(units, e, "dimensions") for e in arguments["dimensions"]]
        suite.add(assertions.assembly_dimensions("dimensions", root, wanted, tolerance))

    if arguments.get("centered_on_origin"):
        for axis in ("x", "y"):
            suite.add(assertions.assembly_centered(
                f"centred_{axis}", root, axis, tolerance))

    if arguments.get("max_mass_kg") is not None:
        suite.add(assertions.assembly_mass_at_most(
            "max_mass", root, float(arguments["max_mass_kg"]),
            float(arguments.get("density_g_cm3", 2.70))))

    if arguments.get("require_no_interference", False):
        suite.add(assertions.no_interference("no_interference", design))

    return suite.report()


# ------------------------------------------------------------- modelling ----

def create_document(_: dict[str, Any]) -> dict[str, Any]:
    """Create and activate an empty, unsaved Fusion design."""
    document, _ = _new_design()
    return {"created_unsaved_document": document.name,
            "document": _document_metadata()}


def create_test_box(arguments: dict[str, Any]) -> dict[str, Any]:
    name = _safe_name(arguments.get("name"), "Agent Test Box")
    document, design = _new_design()
    root = design.rootComponent
    units = design.unitsManager
    v = _values(units, {
        "width": arguments.get("width", "2 in"),
        "height": arguments.get("height", "1 in"),
        "depth": arguments.get("depth", "0.5 in"),
    })

    with operation(design, "create_test_box") as (_, suite):
        sketch = ops.sketch_on(root, root.xYConstructionPlane, f"{name} Sketch")
        ops.draw_rect(sketch, (-v["width"] / 2, -v["height"] / 2, 0),
                              (v["width"] / 2, v["height"] / 2, 0))
        profile = pick.profiles(sketch, loops=1).one()
        feature = ops.extrude(root, profile, v["depth"], name=f"{name} Extrude")
        body = feature.bodies.item(0)
        body.name = name
        suite.add(assertions.bounding_box_matches(
            "dimensions", body, v["width"], v["height"], v["depth"]))

    APP.activeViewport.fit()
    return {
        "created_unsaved_document": document.name,
        "created_entities": [editing.row(design, sketch), editing.row(design, feature)],
        "body": _body_summary(body),
        "verification": [suite.report()],
    }


def replace_with_isogrid_bar(arguments: dict[str, Any]) -> dict[str, Any]:
    """Replace root geometry with a filleted aluminium isogrid bar."""
    design = _active_design()
    document = APP.activeDocument
    if not document:
        raise ToolFailure("No active Fusion document.")
    if design.allComponents.count != 1:
        raise ToolFailure("Replacement currently requires a single-component design.")

    root = design.rootComponent
    units = design.unitsManager
    name = _safe_name(arguments.get("name"), "6061 Aluminum Isogrid Bar")
    expressions = {
        "length": arguments.get("length", "12 in"),
        "width": arguments.get("width", "4 in"),
        "thickness": arguments.get("thickness", "2 in"),
        "cell_size": arguments.get("cell_size", "1.0 in"),
        "rib_width": arguments.get("rib_width", "0.1 in"),
        "skin_thickness": arguments.get("skin_thickness", "0.25 in"),
        "fillet_radius": arguments.get("fillet_radius", "0.03 in"),
    }
    v = _values(units, expressions, zero_ok=("skin_thickness",))

    preflight = CheckSuite("preflight")
    if v["skin_thickness"] >= v["thickness"]:
        preflight.add(Check("skin_below_thickness", False,
                            f"{v['skin_thickness']:.4f}", f"< {v['thickness']:.4f}"))
        preflight.require()

    triangles = patterns.isogrid_triangles(
        v["length"], v["width"], v["cell_size"], v["rib_width"])
    workload = complexity.polygon_counts(triangles)
    # Conservative bound for this rectangular bar and triangular through pockets.
    # Enforce before clearing the starting design, then check actual edges again.
    complexity.Limits.configured().require(fillet_edges=12 + 9 * len(triangles))
    skeletonized = v["skin_thickness"] <= 0
    pocket_depth = v["thickness"] - v["skin_thickness"]
    border = v["rib_width"]

    preflight.add(assertions.polygons_disjoint("pockets_disjoint", triangles))
    preflight.add(assertions.tiling_valence("pockets_tessellate", triangles))
    preflight.add(assertions.pattern_centered("pattern_centered", triangles, (0.0, 0.0), 1e-9))
    preflight.add(assertions.polygons_within(
        "pockets_inside_border", triangles,
        -v["length"] / 2 + border, v["length"] / 2 - border,
        -v["width"] / 2 + border, v["width"] / 2 - border))
    preflight.add(assertions.minimum_gap("rib_width_held", triangles, v["rib_width"] * 0.99))
    preflight.add(assertions.fillet_fits_rib(
        "fillet_fits_rib", v["fillet_radius"], v["rib_width"]))
    preflight.add(assertions.rib_aspect_ratio(
        "rib_aspect_ratio", v["rib_width"], pocket_depth, 8.0))
    preflight.add(assertions.tool_reach_ratio(
        "tool_reach", v["fillet_radius"], pocket_depth, 10.0))
    preflight.require()
    reports = [preflight.report()]

    with operation(design, "clear_root", cleanup_supported=False) as (_, suite):
        deleted = _clear_root(root)
        suite.add(assertions.count_equals("root_bodies_cleared", root.bRepBodies.count, 0))
    reports.append(suite.report())

    with operation(design, "bar_extrude") as (_, suite):
        sketch = ops.sketch_on(root, root.xYConstructionPlane, "Bar Profile")
        ops.draw_rect(sketch, (-v["length"] / 2, -v["width"] / 2, 0),
                              (v["length"] / 2, v["width"] / 2, 0))
        feature = ops.extrude(root, pick.profiles(sketch, loops=1).one(),
                              v["thickness"], name="Bar Extrude")
        body = feature.bodies.item(0)
        body.name = name
        suite.add(assertions.bounding_box_matches(
            "bar_dimensions", body, v["length"], v["width"], v["thickness"]))
    reports.append(suite.report())
    unpocketed = body.volume

    with operation(design, "pocket_cut") as (_, suite):
        top = pick.faces(body).planar().normal_axis("z").extreme("+z").one()
        depth = pocket_depth * 1.05 if skeletonized else pocket_depth
        ops.cut_polygons(root, body, top, triangles, -depth, v["thickness"],
                         name="Isogrid Pocket", suite=suite, sides=3)
        suite.add(assertions.single_solid_body("still_one_solid", root))
        suite.require()
        suite.add(assertions.value_decreased(
            "material_removed", unpocketed, root.bRepBodies.item(0).volume))
    reports.append(suite.report())

    body = root.bRepBodies.item(0)
    original_edges = body.edges.count
    with operation(design, "all_edge_fillet") as (_, suite):
        feature, used_radius = ops.fillet_all_edges(
            root, body, v["fillet_radius"], suite=suite)
        suite.require()
        suite.add(assertions.single_solid_body("solid_after_fillet", root))
    reports.append(suite.report())

    body = root.bRepBodies.item(0)
    source = ops.set_material([body], app=APP)
    body.name = name

    final = CheckSuite("final")
    final.add(assertions.bounding_box_matches(
        "dimensions_held", body, v["length"], v["width"], v["thickness"]))
    final.add(assertions.single_solid_body("single_solid", root))
    final.require()
    reports.append(final.report())

    bar_section = mechanics.rect(v["width"] / CM_PER_INCH,
                                 v["thickness"] / CM_PER_INCH)
    bar_rules = mechanics.machining_rules(
        pocket_depth / CM_PER_INCH, v["rib_width"] / CM_PER_INCH,
        (used_radius or 0.0) / CM_PER_INCH)

    APP.activeViewport.fit()
    solid = v["length"] * v["width"] * v["thickness"]
    return {
        "document": document.name,
        "mechanics": _mechanics_block(body, bar_section, "6061-T6", "x", bar_rules),
        "deleted": deleted,
        "requested": expressions,
        "skeletonized": skeletonized,
        "triangle_pockets": len(triangles),
        "complexity": workload,
        "pocket_depth_cm": pocket_depth,
        "fillet_radius_cm": used_radius,
        "edges_filleted": original_edges,
        "mass_reduction_percent": 100 * (solid - body.volume) / solid,
        "material_source": source,
        "body": _body_summary(body),
        "verification": reports,
    }


def create_tube_frame(arguments: dict[str, Any]) -> dict[str, Any]:
    """Four perforated rectangular tubes arranged as a square frame."""
    name = _safe_name(arguments.get("name"), "Tube Frame")
    justify = arguments.get("justify", "outer")
    if justify not in {"outer", "center", "inner"}:
        raise ArgumentError("justify must be one of: outer, center, inner")
    orientation = arguments.get("orientation", "wide_vertical")
    if orientation not in {"wide_vertical", "wide_horizontal"}:
        raise ArgumentError("orientation must be wide_vertical or wide_horizontal")

    expressions = {
        "tube_length": arguments.get("tube_length", "25 in"),
        "tube_wide": arguments.get("tube_wide", "2 in"),
        "tube_narrow": arguments.get("tube_narrow", "1 in"),
        "wall": arguments.get("wall", "0.125 in"),
        "frame_size": arguments.get("frame_size", "30 in"),
        "hole_diameter": arguments.get("hole_diameter", "0.125 in"),
    }
    wide_density = float(arguments.get("wide_holes_per_inch", 3))
    narrow_density = float(arguments.get("narrow_holes_per_inch", 1))
    if wide_density < 0 or narrow_density < 0:
        raise ArgumentError("hole densities must be non-negative")

    use_active_document = bool(arguments.get("use_active_document", False))
    if use_active_document:
        design = _active_design()
        document = APP.activeDocument
        root = design.rootComponent
        if root.bRepBodies.count or root.features.count or root.sketches.count:
            raise ToolFailure(
                "use_active_document requires an empty active design; "
                "refusing to overwrite existing geometry"
            )
    else:
        document, design = _new_design()
        root = design.rootComponent
    units = design.unitsManager
    v = _values(units, expressions)

    if orientation == "wide_vertical":
        height, in_plane = v["tube_wide"], v["tube_narrow"]
        wide_axis, narrow_axis = "y", "z"
    else:
        height, in_plane = v["tube_narrow"], v["tube_wide"]
        wide_axis, narrow_axis = "z", "y"

    half = v["frame_size"] / 2
    offset = {"outer": half - in_plane / 2, "center": half,
              "inner": half + in_plane / 2}[justify]
    radius = v["hole_diameter"] / 2
    reports = []

    preflight = CheckSuite("preflight")
    preflight.add(assertions.at_least(
        "wall_fits_section", min(height, in_plane) / 2 - v["wall"], 1e-6, "cm"))
    wide_positions = patterns.linear_positions(v["tube_length"], wide_density)
    narrow_positions = patterns.linear_positions(v["tube_length"], narrow_density)
    active_patterns = [
        (density, positions)
        for density, positions in (
            (wide_density, wide_positions),
            (narrow_density, narrow_positions),
        )
        if positions
    ]
    # Two different quantities that are easy to confuse: edge distance is
    # measured from the hole *centre* to the end of the part, while the clear
    # margin is what is left beyond the hole's edge. Rules of thumb are quoted
    # against the first; the material check wants the second.
    if active_patterns:
        governing_density, governing_positions = max(
            active_patterns, key=lambda item: item[0]
        )
        pitch = CM_PER_INCH / governing_density
        preflight.add(assertions.at_least(
            "hole_ligament", pitch - v["hole_diameter"], 0.02, "cm"))
        end_center_distance = (
            v["tube_length"] - (len(governing_positions) - 1) * pitch
        ) / 2
        clear_margin = end_center_distance - radius
        preflight.add(assertions.at_least(
            "hole_clear_margin", clear_margin, radius, "cm"))
    else:
        pitch = 0.0
        end_center_distance = 0.0
    preflight.require()
    reports.append(preflight.report())

    with operation(design, "tube_extrude") as (_, suite):
        sketch = ops.sketch_on(root, root.yZConstructionPlane, "Tube Section")
        ops.draw_rect(sketch, (0, offset - in_plane / 2, -height / 2),
                              (0, offset + in_plane / 2, height / 2))
        ops.draw_rect(sketch, (0, offset - in_plane / 2 + v["wall"], -height / 2 + v["wall"]),
                              (0, offset + in_plane / 2 - v["wall"], height / 2 - v["wall"]))
        annulus = pick.profiles(sketch, loops=2).one()
        feature = ops.extrude(root, annulus, v["tube_length"],
                              symmetric=True, name="Tube Extrude")
        body = feature.bodies.item(0)
        body.name = f"{name} Tube"
        suite.add(assertions.bounding_box_matches(
            "tube_section", body, v["tube_length"], in_plane, height))
    reports.append(suite.report())

    if body.volume >= v["tube_length"] * in_plane * height - 1e-9:
        raise ToolFailure("Tube extrude produced a solid bar, not a hollow tube.")

    holes = 0
    with operation(design, "drill_faces") as (_, suite):
        plan = (
            (wide_axis, f"+{wide_axis}", wide_positions, "Wide A"),
            (wide_axis, f"-{wide_axis}", wide_positions, "Wide B"),
            (narrow_axis, f"+{narrow_axis}", narrow_positions, "Narrow A"),
            (narrow_axis, f"-{narrow_axis}", narrow_positions, "Narrow B"),
        )
        for axis, direction, positions, label in plan:
            if not positions:
                continue
            face = pick.faces(body).planar().normal_axis(axis).extreme(direction).one()
            at = pick.position(face, axis)
            centers = [(x, at, 0.0) if axis == "y" else (x, offset, at) for x in positions]
            ops.cut_holes(root, body, face, centers, radius, -v["wall"],
                          name=label, suite=suite)
            holes += len(positions)
    reports.append(suite.report())

    with operation(design, "circular_pattern") as (_, suite):
        ops.circular_pattern(root, [body], root.zConstructionAxis, 4, name="Frame Pattern")
        suite.add(assertions.count_equals("frame_members", root.bRepBodies.count, 4))
        suite.require()
        suite.add(assertions.no_interference("tubes_do_not_clash", design))
    reports.append(suite.report())

    members = pick.bodies(root).all()
    source = ops.set_material(members, app=APP)
    for index, member in enumerate(members):
        member.name = f"{name} {index + 1}"

    final = CheckSuite("final")
    final.add(assertions.count_equals("four_tubes", len(members), 4))
    final.add(assertions.all_bodies_solid("all_solid", root))
    final.require()
    reports.append(final.report())

    section = mechanics.rect_tube(in_plane / CM_PER_INCH, height / CM_PER_INCH,
                                  v["wall"] / CM_PER_INCH)
    mech_rules = (
        mechanics.hole_rules(
            v["hole_diameter"] / CM_PER_INCH,
            end_center_distance / CM_PER_INCH,
            pitch / CM_PER_INCH,
            v["wall"] / CM_PER_INCH,
        )
        if active_patterns else ()
    )

    APP.activeViewport.fit()
    return {
        "document": document.name,
        "created_unsaved_document": None if use_active_document else document.name,
        "mechanics": _mechanics_block(members[0], section, "6061-T6", "x", mech_rules),
        "requested": expressions,
        "justify": justify,
        "orientation": orientation,
        "holes_per_tube": holes,
        "holes_total": holes * 4,
        "wide_face_holes": len(wide_positions),
        "narrow_face_holes": len(narrow_positions),
        "tube_offset_in": offset / CM_PER_INCH,
        "material_source": source,
        "bodies": [_body_summary(b) for b in members],
        "verification": reports,
    }


def add_bellypan(arguments: dict[str, Any]) -> dict[str, Any]:
    """Add an isogridded belly pan bolted to the underside of a tube frame."""
    design = _active_design()
    document = APP.activeDocument
    root = design.rootComponent
    units = design.unitsManager
    name = _safe_name(arguments.get("name"), "Belly Pan")

    tubes = pick.bodies(root).all()
    if not tubes:
        raise ToolFailure("No existing geometry; run create_tube_frame first.")

    expressions = {
        "thickness": arguments.get("thickness", "0.125 in"),
        "pan_size": arguments.get("pan_size", "30 in"),
        "cell_size": arguments.get("cell_size", "3 in"),
        "rib_width": arguments.get("rib_width", "0.25 in"),
        "border": arguments.get("border", "2 in"),
        "hole_clearance": arguments.get("hole_clearance", "0.25 in"),
    }
    v = _values(units, expressions)
    skeletonize = bool(arguments.get("skeletonize", True))

    frame_bottom = min(
        (b.preciseBoundingBox or b.boundingBox).minPoint.z for b in tubes)

    # Read the bolt pattern out of the tubes so the pan follows the frame
    # instead of assuming it.
    holes = []
    for tube in tubes:
        holes.extend(
            pick.faces(tube).cylindrical().axis_along("z").extreme("-z").centers())
    hole_points = [(x, y) for x, y, _ in holes]
    hole_radius = 0.0
    for tube in tubes:
        found = pick.faces(tube).cylindrical().axis_along("z").extreme("-z").all()
        if found:
            hole_radius = adsk.core.Cylinder.cast(found[0].geometry).radius
            break

    preflight = CheckSuite("preflight")
    preflight.add(Check("bolt_pattern_found", len(holes) > 0, f"{len(holes)} holes",
                        "at least one downward-facing hole"))
    preflight.require()
    if arguments.get("expected_holes") is not None:
        preflight.add(assertions.count_equals(
            "bolt_count", len(holes), int(arguments["expected_holes"])))

    triangles = []
    if skeletonize:
        triangles = patterns.isogrid_triangles(
            v["pan_size"], v["pan_size"], v["cell_size"], v["rib_width"], margin=v["border"])
        preflight.add(assertions.polygons_disjoint("pockets_disjoint", triangles))
        preflight.add(assertions.pattern_centered("pattern_centered", triangles, (0.0, 0.0), 1e-9))
        preflight.add(assertions.polygons_clear_of_points(
            "pockets_clear_of_bolts", triangles, hole_points,
            hole_radius + v["hole_clearance"]))
        preflight.add(assertions.minimum_gap("rib_width_held", triangles, v["rib_width"] * 0.99))
    workload = complexity.polygon_counts(triangles)
    complexity.Limits.configured().require(sketch_curves=workload["sketch_curves"] + len(holes) + 4)
    preflight.require()
    reports = [preflight.report()]

    thickness = v["thickness"]
    half = v["pan_size"] / 2
    pan_bottom = frame_bottom - thickness

    with operation(design, "pan_plate") as (_, suite):
        plane = ops.plane_at(root, pan_bottom, "Belly Pan Plane")
        sketch = ops.sketch_on(root, plane, "Belly Pan Outline")
        ops.draw_rect(sketch, (-half, -half, pan_bottom), (half, half, pan_bottom))
        feature = ops.extrude(root, pick.profiles(sketch, loops=1).one(),
                              thickness, name="Belly Pan Extrude")
        pan = feature.bodies.item(0)
        pan.name = name
        suite.add(assertions.bounding_box_matches(
            "pan_dimensions", pan, v["pan_size"], v["pan_size"], thickness))
        suite.add(assertions.count_equals(
            "total_bodies", root.bRepBodies.count, len(tubes) + 1))
    reports.append(suite.report())

    depth = thickness * 1.05 if skeletonize else thickness * 0.5

    if skeletonize:
        with operation(design, "isogrid_cut") as (_, suite):
            ops.cut_polygons(root, pan, plane, triangles, depth, pan_bottom,
                             name="Belly Pan Isogrid", suite=suite, sides=3)
            suite.add(assertions.count_equals(
                "bodies_after_isogrid", root.bRepBodies.count, len(tubes) + 1))
        reports.append(suite.report())

    with operation(design, "bolt_holes") as (_, suite):
        centers = [(x, y, pan_bottom) for x, y in hole_points]
        ops.cut_holes(root, pan, plane, centers, hole_radius, thickness * 1.05,
                      name="Belly Pan Bolt Pattern", suite=suite)
    reports.append(suite.report())

    source = ops.set_material([pan], app=APP)

    final = CheckSuite("final")
    final.add(assertions.count_equals(
        "body_count", root.bRepBodies.count, len(tubes) + 1))
    final.add(Check("pan_is_solid", pan.isSolid, str(pan.isSolid), "True"))
    final.add(assertions.no_interference("pan_clears_tubes", design))
    final.require()
    reports.append(final.report())

    pan_section = mechanics.plate(v["pan_size"] / CM_PER_INCH,
                                  thickness / CM_PER_INCH)
    pan_rules = mechanics.plate_rules(
        v["pan_size"] / CM_PER_INCH, thickness / CM_PER_INCH,
        unsupported_overhang=float(arguments.get("unsupported_overhang_in", 0.0)))

    APP.activeViewport.fit()
    solid_plate = v["pan_size"] ** 2 * thickness
    return {
        "document": document.name,
        "mechanics": _mechanics_block(pan, pan_section, "6061-T6", "x", pan_rules),
        "requested": expressions,
        "skeletonized": skeletonize,
        "bolt_holes": len(holes),
        "isogrid_pockets": len(triangles),
        "complexity": workload,
        "pan_top_z_cm": frame_bottom,
        "mass_reduction_percent": 100 * (solid_plate - pan.volume) / solid_plate,
        "material_source": source,
        "body": _body_summary(pan),
        "verification": reports,
    }


def add_fasteners(arguments: dict[str, Any]) -> dict[str, Any]:
    """Tap the frame holes, open the pan to clearance, and place screws."""
    design = _active_design()
    document = APP.activeDocument
    root = design.rootComponent
    units = design.unitsManager

    pan_name = arguments.get("pan_name", "Belly Pan")
    pan_selection = pick.bodies(root).named(pan_name)
    if not len(pan_selection):
        raise ToolFailure(f"No body named '{pan_name}'; run add_bellypan first.")
    pan = pan_selection.one()
    tubes = [b for b in pick.bodies(root) if b.name != pan_name]
    if not tubes:
        raise ToolFailure("No tube bodies found to tap.")

    expressions = {
        "screw_diameter": arguments.get("screw_diameter", "0.125 in"),
        "clearance_diameter": arguments.get("clearance_diameter", "0.136 in"),
        "head_diameter": arguments.get("head_diameter", "0.183 in"),
        "head_height": arguments.get("head_height", "0.125 in"),
        "screw_length": arguments.get("screw_length", "0.375 in"),
    }
    v = _values(units, expressions)
    modeled = bool(arguments.get("modeled_threads", False))
    thread_screws = bool(arguments.get("thread_screws", True))
    family = arguments.get("thread_family", "unified")
    expected = arguments.get("expected_designation")

    pan_box = pan.preciseBoundingBox or pan.boundingBox
    pan_bottom = pan_box.minPoint.z
    pan_thickness = pan_box.maxPoint.z - pan_box.minPoint.z

    tap_faces: list[Any] = []
    hole_points: list[tuple[float, float]] = []
    for tube in tubes:
        selection = pick.faces(tube).cylindrical().axis_along("z").extreme("-z")
        tap_faces.extend(selection.all())
        hole_points.extend((x, y) for x, y, _ in selection.centers())

    preflight = CheckSuite("preflight")
    preflight.add(Check("tap_holes_found", len(tap_faces) > 0,
                        f"{len(tap_faces)}", "at least one"))
    preflight.add(assertions.at_least("clearance_exceeds_screw",
                                      v["clearance_diameter"] - v["screw_diameter"], 1e-4, "cm"))
    preflight.add(assertions.at_least("head_covers_clearance",
                                      v["head_diameter"] - v["clearance_diameter"], 0.02, "cm"))
    engagement = v["screw_length"] - pan_thickness
    preflight.add(assertions.at_least("thread_engagement", engagement,
                                      v["screw_diameter"], "cm"))
    preflight.require()
    reports = [preflight.report()]

    with operation(design, "pan_clearance_holes") as (_, suite):
        plane = ops.plane_at(root, pan_bottom, "Fastener Plane")
        centers = [(x, y, pan_bottom) for x, y in hole_points]
        ops.cut_holes(root, pan, plane, centers, v["clearance_diameter"] / 2,
                      pan_thickness * 1.05, name="Pan Clearance", suite=suite)
    reports.append(suite.report())

    with operation(design, "tap_frame_holes") as (_, suite):
        tap_info = ops.thread(root, pick.Selection(tap_faces, "face", "tap_faces"),
                              v["screw_diameter"], internal=True, family=family,
                              modeled=modeled, expected=expected, label="tap", suite=suite)
    reports.append(suite.report())

    before_count = root.bRepBodies.count
    head_bottom = pan_bottom - v["head_height"]

    with operation(design, "screw_bodies") as (_, suite):
        head_plane = ops.plane_at(root, head_bottom, "Screw Head Plane")
        head_sketch = ops.sketch_on(root, head_plane, "Screw Heads")
        ops.draw_circles(head_sketch, [(x, y, head_bottom) for x, y in hole_points],
                         v["head_diameter"] / 2)
        head_profiles = pick.profiles(head_sketch, loops=1, curves=1)
        suite.add(assertions.count_equals(
            "head_profiles", len(head_profiles), len(hole_points)))
        suite.require()
        head_feature = ops.extrude(root, head_profiles.collection(), v["head_height"],
                                   name="Screw Heads")
        suite.add(assertions.count_equals(
            "screw_bodies_created", head_feature.bodies.count, len(hole_points)))
        suite.require()

        # The shank spans the full head height so the join has real overlap;
        # a shank merely touching the head's top face is tangent, and tangent
        # joins are unreliable.
        shank_sketch = ops.sketch_on(root, head_plane, "Screw Shanks")
        ops.draw_circles(shank_sketch, [(x, y, head_bottom) for x, y in hole_points],
                         v["screw_diameter"] / 2)
        shank_profiles = pick.profiles(shank_sketch, loops=1, curves=1)
        suite.add(assertions.count_equals(
            "shank_profiles", len(shank_profiles), len(hole_points)))
        suite.require()
        ops.extrude(root, shank_profiles.collection(),
                    v["head_height"] + v["screw_length"], operation="join",
                    participants=[head_feature.bodies.item(i)
                                  for i in range(head_feature.bodies.count)],
                    name="Screw Shanks")
        suite.add(assertions.count_equals(
            "total_bodies", root.bRepBodies.count, before_count + len(hole_points)))
    reports.append(suite.report())

    screws = pick.bodies(root).name_starts("Body").all()
    for index, screw in enumerate(screws):
        screw.name = f"Screw {index + 1}"

    screw_thread = None
    if thread_screws and screws:
        with operation(design, "thread_screws") as (_, suite):
            shank_faces = []
            for screw in screws:
                shank_faces.extend(
                    pick.faces(screw).cylindrical()
                    .radius(v["screw_diameter"] / 2).all())
            screw_thread = ops.thread(
                root, pick.Selection(shank_faces, "face", "shank_faces"),
                v["screw_diameter"], internal=False, family=family, modeled=modeled,
                expected=expected, label="screw_thread", suite=suite)
        reports.append(suite.report())

    ops.set_material(screws, app=APP, keyword="steel")

    final = CheckSuite("final")
    final.add(assertions.count_equals("screw_count", len(screws), len(hole_points)))
    final.add(assertions.count_equals(
        "body_count", root.bRepBodies.count, before_count + len(hole_points)))
    final.add(assertions.no_interference("assembly_clear", design))
    final.require()
    reports.append(final.report())

    APP.activeViewport.fit()
    return {
        "document": document.name,
        "requested": expressions,
        "screws": len(screws),
        "tapped_holes": len(tap_faces),
        "frame_thread": tap_info,
        "screw_thread": screw_thread,
        "modeled_threads": modeled,
        "thread_engagement_in": engagement / CM_PER_INCH,
        "verification": reports,
    }


def undo_last(_: dict[str, Any]) -> dict[str, Any]:
    return editing.undo_last(_active_design())

def capture_viewport(arguments):
    if not arguments.get("clean"):
        return _capture_viewport(arguments)
    visibility = []
    viewport = APP.activeViewport
    camera, style = viewport.camera, viewport.visualStyle
    try:
        for component in editing.items(_active_design().allComponents):
            for sketch in editing.items(component.sketches):
                visibility.append((sketch, sketch.isVisible))
                sketch.isVisible = False
        result = _capture_viewport(arguments)
        result["presentation"] = "sketches hidden for this capture; original visibility and camera restored"
        return result
    finally:
        for sketch, visible in visibility:
            if sketch.isValid: sketch.isVisible = visible
        viewport.camera, viewport.visualStyle = camera, style
        viewport.refresh()


def _capture_viewport(arguments: dict[str, Any]) -> dict[str, Any]:
    """Render the active viewport to PNG files.

    This is the one signal the bridge could not produce before: every other
    tool reports measured topology, which confirms a model is self-consistent
    but never that it looks like the thing that was asked for.
    """
    import time

    views = arguments.get("views", ["iso"])
    if isinstance(views, str):
        views = [views]
    if not isinstance(views, list) or not views:
        raise ArgumentError("views must be a non-empty list of view names")
    for view in views:
        if view != "current" and view not in VIEW_ORIENTATIONS:
            raise ArgumentError(
                f"unknown view '{view}'; choose from current, "
                + ", ".join(sorted(VIEW_ORIENTATIONS))
            )

    width = int(arguments.get("width", 1600))
    height = int(arguments.get("height", 1200))
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        raise ArgumentError("width and height must be between 64 and 4096")

    style = arguments.get("visual_style", "shaded_edges")
    if style not in VISUAL_STYLES:
        raise ArgumentError(
            "unknown visual_style; choose from " + ", ".join(sorted(VISUAL_STYLES))
        )
    # "current" means "what the person is looking at right now", so fitting
    # defeats the point -- it re-zooms and discards their framing. Default
    # fit on only for a named orientation, where the camera moves anyway.
    wants_current = "current" in views
    fit = bool(arguments.get("fit", not wants_current))
    prefix = _safe_name(arguments.get("prefix"), "capture")

    viewport = APP.activeViewport
    if not viewport:
        raise ToolFailure("No active viewport.")

    try:
        viewport.visualStyle = getattr(adsk.core.VisualStyles, VISUAL_STYLES[style])
    except Exception:
        pass

    directory = _capture_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suite = CheckSuite("capture")
    captured: list[dict[str, Any]] = []

    for view in views:
        if view != "current":
            camera = viewport.camera
            camera.viewOrientation = getattr(
                adsk.core.ViewOrientations, VIEW_ORIENTATIONS[view]
            )
            camera.isSmoothTransition = False
            if fit:
                camera.isFitView = True
            # Camera is returned by value; the assignment is what applies it.
            viewport.camera = camera
        elif fit:
            viewport.fit()
        viewport.refresh()

        path = directory / f"{prefix.replace(' ', '_')}-{stamp}-{view}.png"
        ok = viewport.saveAsImageFile(str(path), width, height)
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        suite.add(
            Check(
                f"{view}_written",
                bool(ok) and exists and size > 1024,
                f"ok={ok}, {size} bytes",
                "a PNG over 1 KB",
                "a tiny file usually means the viewport was occluded",
            )
        )
        captured.append(
            {"view": view, "path": str(path), "bytes": size, "width": width, "height": height}
        )

    suite.require()
    return {
        "captures": captured,
        "directory": str(directory),
        "visual_style": style,
        "verification": suite.report(),
    }


def inspect_threads(arguments: dict[str, Any]) -> dict[str, Any]:
    """List thread types and what Fusion recommends for a diameter."""
    design = _active_design()
    units = design.unitsManager
    diameter = _cm(units, arguments.get("diameter", "0.125 in"), "diameter")
    query = design.rootComponent.features.threadFeatures.threadDataQuery
    rows = []
    for thread_type in query.allThreadTypes:
        entry: dict[str, Any] = {"thread_type": thread_type}
        for internal in (True, False):
            try:
                ok, designation, cls = query.recommendThreadData(
                    diameter, internal, thread_type)
                entry["internal" if internal else "external"] = (
                    f"{designation} / {cls}" if ok else "no match")
            except Exception as exc:
                entry["internal" if internal else "external"] = f"error: {exc}"
        try:
            sizes = list(query.allSizes(thread_type))
            entry["sizes_sample"] = sizes[:14]
            entry["size_count"] = len(sizes)
        except Exception:
            entry["sizes_sample"] = []
        rows.append(entry)
    return {"diameter_in": diameter / CM_PER_INCH, "types": rows}


def inspect_commands(arguments: dict[str, Any]) -> dict[str, Any]:
    """Enumerate Fusion's GUI command definitions.

    Answers a specific question: can a script drive the same commands the
    toolbar buttons drive, and pass them parameters?
    """
    filter_text = str(arguments.get("contains", "")).lower()
    definitions = UI.commandDefinitions
    matches = []
    for index in range(definitions.count):
        definition = definitions.item(index)
        try:
            identifier = definition.id
        except Exception:
            continue
        if filter_text and filter_text not in identifier.lower():
            continue
        entry = {"id": identifier}
        try:
            entry["name"] = definition.name
        except Exception:
            entry["name"] = None
        try:
            # Whether the command exposes typed inputs to a script, or only a
            # dialog a human fills in.
            entry["has_execute"] = hasattr(definition, "execute")
            entry["execute_takes_args"] = (
                definition.execute.__doc__ or ""
            ).strip()[:120]
        except Exception:
            entry["has_execute"] = False
        matches.append(entry)
    return {
        "total_command_definitions": definitions.count,
        "matched": len(matches),
        "sample": matches[:25],
    }


def _capture_dir():
    from common import agent_home

    base = agent_home() / "captures"
    base.mkdir(parents=True, exist_ok=True)
    return base

# ------------------------------------------------------------- registry ----

VIEW_ORIENTATIONS = {
    "top": "TopViewOrientation", "bottom": "BottomViewOrientation",
    "front": "FrontViewOrientation", "back": "BackViewOrientation",
    "left": "LeftViewOrientation", "right": "RightViewOrientation",
    "iso": "IsoTopRightViewOrientation",
    "iso_top_left": "IsoTopLeftViewOrientation",
    "iso_bottom_right": "IsoBottomRightViewOrientation",
}

VISUAL_STYLES = {
    "shaded": "ShadedVisualStyle",
    "shaded_edges": "ShadedWithVisibleEdgesOnlyVisualStyle",
    "shaded_hidden_edges": "ShadedWithHiddenEdgesVisualStyle",
    "wireframe": "WireframeVisualStyle",
    "wireframe_hidden": "WireframeWithHiddenEdgesVisualStyle",
}

TOOLS: dict[str, dict[str, Any]] = {
    "health": {"description": "Verify Fusion's main thread responds.",
               "mutating": False, "arguments": {}},
    "active_document": {"description": "Metadata for the active document.",
                        "mutating": False, "arguments": {}},
    "save_document": {
        "description": "Save the already-named active Fusion document in place.",
        "mutating": True,
        "arguments": {"description": "Optional version description."}},
    "delete_named_features": {
        "description": "Delete explicitly named features and unused sketches in order.",
        "mutating": True,
        "arguments": {
            "feature_names": "Non-empty list of exact feature names.",
            "sketch_names": "Optional list of exact unused sketch names.",
        }},
    "set_6061_material": {
        "description": "Assign 6061-T6 material to explicitly named bodies.",
        "mutating": True,
        "arguments": {"body_names": "Non-empty list of exact body names."}},
    "router_fillet_pocket_corners": {
        "description": "Fillet vertical pocket corners for a specified router cutter.",
        "mutating": True,
        "arguments": {
            "body": "Exact target body name.",
            "cutter_diameter": "Fusion expression; default '0.125 in'.",
            "rib_width_in": "Rib width in inches for the machining report; default 0.25.",
            "name": "Optional feature name.",
        }},
    "import_step_component": {
        "description": "Import a STEP as a grouped component or prefixed root-body set.",
        "mutating": True,
        "arguments": {
            "path": "Absolute path to a .step or .stp file.",
            "name": "Optional grouped component name.",
            "flatten_into_root": "Default false; use true for Part Design documents.",
        }},
    "design_summary": {"description": "Components, bodies, topology, dimensions.",
                       "mutating": False, "arguments": {}},
    "check_interference": {
        "description": "Interference volume between occurrences. The flipped-mate check.",
        "mutating": False, "arguments": {}},
    "verify_spec": {
        "description": "Run a design spec as a test suite against the active design.",
        "mutating": False,
        "arguments": {
            "envelope": "Optional [l, w, h] the assembly bounding box must fit inside.",
            "dimensions": "Optional exact [l, w, h] for the assembly bounding box.",
            "body_count": "Optional expected number of bodies.",
            "centered_on_origin": "Optional; assert the assembly is centred in X and Y.",
            "max_mass_kg": "Optional mass ceiling in kilograms.",
            "density_g_cm3": "Density for the mass estimate; default 2.70.",
            "tolerance_cm": "Tolerance for dimension and centring checks; default 1e-6.",
            "require_single_solid": "Default false.",
            "require_no_interference": "Default false.",
        }},
    "capture_viewport": {
        "description": (
            "Render the Fusion viewport to PNG. Look whenever a picture would "
            "tell you something measurements cannot: after building, before "
            "answering a question about how something appears, or when the "
            "person refers to what is on their screen. views ['current'] "
            "captures their exact framing; a named orientation inspects a "
            "specific side."
        ),
        "mutating": False,
        "arguments": {
            "views": "List of views. 'current' captures the person's screen "
                     "exactly as framed, without moving the camera or "
                     "re-zooming -- use it whenever they refer to what they "
                     "can see. Named orientations: top, bottom, front, back, "
                     "left, right, iso, iso_top_left, iso_bottom_right. "
                     "Several may be requested at once. Default ['iso'].",
            "width": "Pixels, 64-4096; default 1600.",
            "height": "Pixels, 64-4096; default 1200.",
            "visual_style": "shaded | shaded_edges | shaded_hidden_edges | "
                            "wireframe | wireframe_hidden.",
            "fit": "Zoom to fit. Defaults false for 'current' (keeps their "
                   "framing) and true for a named orientation.",
            "prefix": "Filename prefix; default 'capture'.",
        }},
    "inspect_threads": {
        "description": "List thread types and Fusion's recommendation for a diameter.",
        "mutating": False, "arguments": {"diameter": "Default '0.125 in'."}},
    "inspect_commands": {
        "description": "Enumerate Fusion GUI command definitions.",
        "mutating": False, "arguments": {"contains": "Optional substring filter."}},
    "create_test_box": {
        "description": "New unsaved document with a rectangular body: width=X, height=Y, depth=Z thickness. An 8x6x0.25 plate uses width='8 in', height='6 in', depth='0.25 in'.",
        "mutating": True,
        "arguments": {"width": "e.g. '2 in'", "height": "e.g. '1 in'",
                      "depth": "e.g. '0.5 in'", "name": "Optional."}},
    "replace_with_isogrid_bar": {
        "description": "Replace root geometry with a filleted aluminium isogrid bar.",
        "mutating": True,
        "arguments": {
            "length": "default '12 in'", "width": "default '4 in'",
            "thickness": "default '2 in'", "cell_size": "default '1.0 in'",
            "rib_width": "default '0.1 in'",
            "skin_thickness": "default '0.25 in'; '0 in' cuts through.",
            "fillet_radius": "default '0.03 in'", "name": "Optional."}},
    "create_tube_frame": {
        "description": "Four perforated rectangular tubes arranged as a square frame.",
        "mutating": True,
        "arguments": {
            "tube_length": "default '25 in'", "tube_wide": "default '2 in'",
            "tube_narrow": "default '1 in'", "wall": "default '0.125 in'",
            "frame_size": "default '30 in'",
            "justify": "outer | center | inner",
            "orientation": "wide_vertical | wide_horizontal",
            "hole_diameter": "default '0.125 in'",
            "wide_holes_per_inch": "default 3; 0 leaves the wide faces unpunched",
            "narrow_holes_per_inch": "default 1; 0 leaves the narrow faces unpunched",
            "use_active_document": (
                "default false; requires the active design to be empty"
            ),
            "name": "Optional."}},
    "add_bellypan": {
        "description": "Isogridded belly pan bolted under an existing tube frame.",
        "mutating": True,
        "arguments": {
            "thickness": "default '0.125 in'", "pan_size": "default '30 in'",
            "cell_size": "default '3 in'", "rib_width": "default '0.25 in'",
            "border": "solid band for the bolt pattern; default '2 in'",
            "hole_clearance": "material kept around each bolt; default '0.25 in'",
            "skeletonize": "Cut through; default true.",
            "expected_holes": "Optional assertion on the derived bolt count.",
            "name": "Optional."}},
    "add_fasteners": {
        "description": "Tap the frame holes, open the pan to clearance, place screws.",
        "mutating": True,
        "arguments": {
            "screw_diameter": "default '0.125 in' (#5-40)",
            "clearance_diameter": "default '0.136 in'",
            "head_diameter": "default '0.183 in'", "head_height": "default '0.125 in'",
            "screw_length": "default '0.375 in'",
            "thread_family": "default 'unified' (ANSI inch)",
            "expected_designation": "Optional, e.g. '5-40 UNC'.",
            "modeled_threads": "default false", "thread_screws": "default true",
            "pan_name": "default 'Belly Pan'"}},
    "undo_last": {"description": "Execute Fusion's native Undo once.",
                  "mutating": True, "arguments": {}},
}

HANDLERS = {
    "health": health,
    "active_document": active_document,
    "save_document": save_document,
    "delete_named_features": delete_named_features,
    "set_6061_material": set_6061_material,
    "router_fillet_pocket_corners": router_fillet_pocket_corners,
    "import_step_component": import_step_component,
    "design_summary": design_summary,
    "check_interference": check_interference,
    "verify_spec": verify_spec,
    "capture_viewport": capture_viewport,
    "inspect_threads": inspect_threads,
    "inspect_commands": inspect_commands,
    "create_test_box": create_test_box,
    "replace_with_isogrid_bar": replace_with_isogrid_bar,
    "create_tube_frame": create_tube_frame,
    "add_bellypan": add_bellypan,
    "add_fasteners": add_fasteners,
    "undo_last": undo_last,
}


# ------------------------------------------------------------ mechanics ----

SECTION_BUILDERS = {
    "rect": lambda d: mechanics.rect(d["width"], d["height"]),
    "rect_tube": lambda d: mechanics.rect_tube(d["width"], d["height"], d["wall"]),
    "round_tube": lambda d: mechanics.round_tube(d["outer_diameter"], d["wall"]),
    "plate": lambda d: mechanics.plate(d["width"], d["thickness"]),
}


def _measured_section(body, axis: str) -> dict[str, float]:
    """Effective cross-section of a prismatic body, straight from the kernel.

    area = volume / length holds exactly for any prismatic solid, including a
    perforated one -- so this is the section the part actually has, not the
    section it was drawn from.
    """
    box = body.preciseBoundingBox or body.boundingBox
    spans = {
        "x": (box.maxPoint.x - box.minPoint.x),
        "y": (box.maxPoint.y - box.minPoint.y),
        "z": (box.maxPoint.z - box.minPoint.z),
    }
    length_cm = spans[axis]
    volume_cm3 = body.volume
    length_in = length_cm / CM_PER_INCH
    volume_in3 = volume_cm3 / CM_PER_INCH ** 3
    return {
        "length_in": length_in,
        "volume_in3": volume_in3,
        "effective_area_in2": volume_in3 / length_in if length_in else 0.0,
    }


def _mechanics_block(body, section, material_name: str, axis: str,
                     rules=()) -> dict[str, Any]:
    """Section facts for a finished body. No loads, no assumptions.

    The section is built from the same variables that drove the geometry, so
    it cannot drift from the part the way a hand-typed spec can.
    """
    mat = mechanics.material(material_name)
    block = mechanics.section_facts(section, mat, rules)
    measured = _measured_section(body, axis)
    ideal = section.area
    effective = measured["effective_area_in2"]
    block["measured"] = measured
    block["section_efficiency"] = effective / ideal if ideal else 0.0
    if ideal and effective < ideal * 0.999:
        block["note"] = (
            f"as built the part has {100 * (1 - effective / ideal):.1f}% less "
            "material than the drawn section; I is reduced at perforated stations"
        )
    return block


def mechanics_report(arguments: dict[str, Any]) -> dict[str, Any]:
    """Closed-form section, beam and rule-of-thumb report for a body.

    Reports numbers, not verdicts. A boolean only lets you conform; a metric
    lets you compare and optimise.
    """
    design = _active_design()
    root = design.rootComponent

    body_name = arguments.get("body")
    bodies = pick.bodies(root)
    body = bodies.named(body_name).first() if body_name else bodies.first()

    axis = arguments.get("axis", "x")
    if axis not in {"x", "y", "z"}:
        raise ArgumentError("axis must be x, y or z")

    material_name = arguments.get("material", "6061-T6")
    try:
        mat = mechanics.material(material_name)
    except KeyError as exc:
        raise ArgumentError(str(exc)) from exc

    section_spec = arguments.get("section")
    if not isinstance(section_spec, dict) or "type" not in section_spec:
        raise ArgumentError(
            "section must be an object with a 'type' of "
            + ", ".join(sorted(SECTION_BUILDERS))
        )
    kind = section_spec["type"]
    if kind not in SECTION_BUILDERS:
        raise ArgumentError(f"unknown section type '{kind}'")
    try:
        section = SECTION_BUILDERS[kind](section_spec)
    except (KeyError, ValueError) as exc:
        raise ArgumentError(f"bad section spec: {exc}") from exc

    measured = _measured_section(body, axis)

    rules: list[Any] = []
    if arguments.get("holes"):
        h = arguments["holes"]
        rules.extend(mechanics.hole_rules(
            h["diameter"], h["edge_distance"], h["pitch"], h.get("wall", 0.0)))
    if arguments.get("threads"):
        t = arguments["threads"]
        rules.extend(mechanics.thread_rules(
            t["diameter"], t["engagement"], t.get("into", material_name)))
    if arguments.get("machining"):
        m = arguments["machining"]
        rules.extend(mechanics.machining_rules(
            m["depth"], m["wall"], m.get("internal_radius", 0.0)))
    if arguments.get("plate"):
        p = arguments["plate"]
        rules.extend(mechanics.plate_rules(
            p["span"], p["thickness"], p.get("overhang", 0.0)))

    out = mechanics.report(
        section, mat,
        span=arguments.get("span"),
        load=arguments.get("load"),
        case=arguments.get("case", "simply_supported_point"),
        rules=rules,
    )

    ideal_area = section.area
    effective = measured["effective_area_in2"]
    out["body"] = body.name
    out["measured"] = measured
    out["section_efficiency"] = effective / ideal_area if ideal_area else 0.0
    out["notes"] = []
    if ideal_area and effective < ideal_area * 0.999:
        out["notes"].append(
            f"the built part has {100 * (1 - effective / ideal_area):.1f}% less "
            "material than the drawn section -- holes and pockets reduce I "
            "roughly in proportion at the perforated stations, so the beam "
            "numbers above read stiff"
        )
    if arguments.get("buckling_length"):
        out["buckling"] = mechanics.euler_buckling(
            section, arguments["buckling_length"], mat,
            arguments.get("end_fixity", 1.0))
    return out


TOOLS["mechanics_report"] = {
    "description": (
        "Closed-form section properties, beam deflection and rule-of-thumb "
        "checks for a body. Microseconds, not FEA. Reports numbers, not verdicts."
    ),
    "mutating": False,
    "arguments": {
        "body": "Body name; defaults to the first body.",
        "axis": "Prismatic axis of the member: x, y or z. Default x.",
        "material": "One of " + ", ".join(sorted(mechanics.MATERIALS)) + ".",
        "section": "{type: rect|rect_tube|round_tube|plate, ...dimensions in inches}",
        "span": "Optional span in inches for a beam case.",
        "load": "Optional load in pounds.",
        "case": "cantilever_point | cantilever_udl | simply_supported_point | "
                "simply_supported_udl | fixed_fixed_point",
        "holes": "{diameter, edge_distance, pitch, wall} in inches.",
        "threads": "{diameter, engagement, into} in inches.",
        "machining": "{depth, wall, internal_radius} in inches.",
        "plate": "{span, thickness, overhang} in inches.",
        "buckling_length": "Optional column length in inches.",
        "end_fixity": "K factor; 1.0 pinned, 0.5 fixed, 2.0 free. Default 1.0.",
    },
}
HANDLERS["mechanics_report"] = mechanics_report


# ------------------------------------------------- arbitrary geometry ------

UNIT_SCALE = {"in": CM_PER_INCH, "mm": 0.1, "cm": 1.0, "m": 100.0}

POLYGON_SCHEMA = {
    "type": "array",
    "description": (
        "Closed polygons as [[ [x,y], [x,y], ... ], ...] in `units`. Do not "
        "repeat the first point; the loop is closed for you. Polygons must not "
        "overlap each other -- that is checked before anything is built."
    ),
    "items": {
        "type": "array",
        "items": {"type": "array", "items": {"type": "number"},
                  "minItems": 2, "maxItems": 2},
        "minItems": 3,
    },
}

CIRCLE_SCHEMA = {
    "type": "array",
    "description": "Circles as [[x, y, radius], ...] in `units`.",
    "items": {"type": "array", "items": {"type": "number"},
              "minItems": 3, "maxItems": 3},
}


def _scale(units: str) -> float:
    if units not in UNIT_SCALE:
        raise ArgumentError(f"units must be one of {', '.join(sorted(UNIT_SCALE))}")
    return UNIT_SCALE[units]


def _polygons_cm(raw: Any, units: str) -> list[list[tuple[float, float]]]:
    if not isinstance(raw, list) or not raw:
        raise ArgumentError("polygons must be a non-empty list")
    factor = _scale(units)
    out = []
    for index, polygon in enumerate(raw):
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise ArgumentError(f"polygon {index} needs at least 3 points")
        points = []
        for point in polygon:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ArgumentError(
                    f"polygon {index} has a malformed point {point!r}; "
                    "each point is [x, y]")
            points.append((float(point[0]) * factor, float(point[1]) * factor))
        # A repeated closing point makes Fusion see a zero-length edge.
        if math.dist(points[0], points[-1]) < 1e-9:
            points = points[:-1]
        out.append(points)
    return out


def _resolve_target(root, name: str | None):
    bodies = pick.bodies(root)
    if name:
        selection = bodies.named(name)
        if not len(selection):
            raise ToolFailure(
                f"no body named '{name}'; have: "
                + ", ".join(b.name for b in bodies))
        return selection.first()
    if not len(bodies):
        raise ToolFailure("the design has no bodies to cut")
    return bodies.first()


def _resolve_surface(root, body, plane: str, units: str):
    """Return (sketch target, z of that surface in cm, cut sign).

    The sign matters: a sketch on a face cuts *into* the solid along its own
    negative normal, while a sketch on a construction plane extrudes upward.
    Getting it backwards removes nothing at all, which the volume check
    downstream catches -- but it is cheaper to get right here.
    """
    if plane in ("top", "bottom"):
        direction = "+z" if plane == "top" else "-z"
        face = pick.faces(body).planar().normal_axis("z").extreme(direction).one()
        return face, pick.position(face, "z"), -1.0
    if plane == "xy":
        return root.xYConstructionPlane, 0.0, 1.0
    if plane.startswith("z="):
        z = float(plane[2:]) * _scale(units)
        return ops.plane_at(root, z, f"Sketch Plane z={plane[2:]}"), z, 1.0
    raise ArgumentError(
        "plane must be 'top', 'bottom', 'xy', or 'z=<number>' in the given units")


def cut_polygons(arguments: dict[str, Any]) -> dict[str, Any]:
    """Cut arbitrary polygons into an existing body.

    The escape hatch from the parametric tools: any 2D field the caller can
    compute -- a lattice with an unusual offset, a logo, a bespoke rib layout --
    becomes buildable without a new tool being written for it.
    """
    design = _active_design()
    root = design.rootComponent
    units = arguments.get("units", "in")
    polygons = _polygons_cm(arguments.get("polygons"), units)
    complexity.polygon_counts(polygons)
    body = _resolve_target(root, arguments.get("body"))
    plane_name = arguments.get("plane", "top")
    depth_expr = arguments.get("depth")
    if not depth_expr:
        raise ArgumentError("depth is required, e.g. '0.5 in'")
    depth = _cm(design.unitsManager, depth_expr, "depth")
    through = bool(arguments.get("through", False))

    preflight = CheckSuite("preflight")
    preflight.add(assertions.polygons_disjoint("polygons_disjoint", polygons))
    box = body.preciseBoundingBox or body.boundingBox
    preflight.add(assertions.polygons_within(
        "polygons_inside_body", polygons,
        box.minPoint.x, box.maxPoint.x, box.minPoint.y, box.maxPoint.y))
    preflight.require()
    reports = [preflight.report()]

    surface, z, sign = _resolve_surface(root, body, plane_name, units)
    if through:
        depth = (box.maxPoint.z - box.minPoint.z) * 1.05

    with operation(design, "cut_polygons") as (_, suite):
        sides = len(polygons[0])
        uniform = all(len(p) == sides for p in polygons)
        if uniform:
            ops.cut_polygons(root, body, surface, polygons, sign * depth, z,
                             name=arguments.get("name", "Custom Cut"),
                             suite=suite, sides=sides)
        else:
            # Mixed vertex counts cannot share one profile filter, so each
            # distinct shape is cut in its own pass.
            by_sides: dict[int, list] = {}
            for polygon in polygons:
                by_sides.setdefault(len(polygon), []).append(polygon)
            for count, group in sorted(by_sides.items()):
                ops.cut_polygons(root, body, surface, group, sign * depth, z,
                                 name=f"{arguments.get('name', 'Custom Cut')} {count}gon",
                                 suite=suite, sides=count)
        suite.add(Check(
            "target_still_solid", body.isSolid, str(body.isSolid), "True"
        ))
    reports.append(suite.report())

    return {
        "polygons_cut": len(polygons),
        "depth_cm": depth,
        "plane": plane_name,
        "through": through,
        "body": _body_summary(body),
        "verification": reports,
    }


def extrude_polygons(arguments: dict[str, Any]) -> dict[str, Any]:
    """Create a new body from arbitrary polygons."""
    design = _active_design()
    root = design.rootComponent
    units = arguments.get("units", "in")
    polygons = _polygons_cm(arguments.get("polygons"), units)
    complexity.polygon_counts(polygons)
    height_expr = arguments.get("height")
    if not height_expr:
        raise ArgumentError("height is required, e.g. '1 in'")
    height = _cm(design.unitsManager, height_expr, "height")
    name = _safe_name(arguments.get("name"), "Custom Body")

    preflight = CheckSuite("preflight")
    preflight.add(assertions.polygons_disjoint("polygons_disjoint", polygons))
    preflight.require()
    reports = [preflight.report()]

    base_z = _cm(design.unitsManager, arguments.get("base", "0 in"),
                 "base", allow_zero=True)
    with operation(design, "extrude_polygons") as (_, suite):
        plane = (root.xYConstructionPlane if abs(base_z) < 1e-9
                 else ops.plane_at(root, base_z, "Custom Base"))
        sketch = ops.sketch_on(root, plane, f"{name} Profile")
        ops.draw_polygons(sketch, polygons, base_z)
        sides = len(polygons[0])
        selection = pick.profiles(sketch, loops=1, curves=sides)
        suite.add(assertions.count_equals(
            "profiles", len(selection), len(polygons),
            f"{sketch.profiles.count} total sketch profiles"))
        suite.require()
        feature = ops.extrude(root, selection.collection(), height, name=f"{name} Extrude")
        suite.add(assertions.count_equals(
            "bodies_created", feature.bodies.count, len(polygons)))
    reports.append(suite.report())

    bodies = [feature.bodies.item(i) for i in range(feature.bodies.count)]
    for index, solid in enumerate(bodies):
        solid.name = name if len(bodies) == 1 else f"{name} {index + 1}"
    source = ops.set_material(bodies, app=APP)
    APP.activeViewport.fit()
    return {
        "bodies_created": len(bodies),
        "material_source": source,
        "bodies": [_body_summary(b) for b in bodies],
        "verification": reports,
    }


def lattice_pockets(arguments: dict[str, Any]) -> dict[str, Any]:
    """Cut a parametric lattice into an existing body.

    Covers the common fields without the caller computing coordinates. Anything
    this cannot express -- an irregular field, a gradient, a logo -- is what
    cut_polygons is for.
    """
    design = _active_design()
    root = design.rootComponent
    units_manager = design.unitsManager
    body = _resolve_target(root, arguments.get("body"))
    box = body.preciseBoundingBox or body.boundingBox

    kind = arguments.get("pattern", "isogrid")
    if kind not in patterns.PATTERNS:
        raise ArgumentError(
            f"pattern must be one of: {', '.join(sorted(patterns.PATTERNS))}")

    cell = _cm(units_manager, arguments.get("cell_size", "1 in"), "cell_size")
    rib = _cm(units_manager, arguments.get("rib_width", "0.1 in"), "rib_width")
    margin_expr = arguments.get("margin")
    margin = _cm(units_manager, margin_expr, "margin") if margin_expr else None
    length = box.maxPoint.x - box.minPoint.x
    width = box.maxPoint.y - box.minPoint.y

    options: dict[str, Any] = {"margin": margin}
    if kind == "isogrid":
        options.update(
            # Must mirror patterns.isogrid_triangles' own default. A 0.0 here
            # silently un-did the fix that makes the field an isogrid at all.
            row_stagger=float(arguments.get("row_stagger", 0.5)),
            row_pitch=float(arguments.get("row_pitch", 1.0)),
            include_inverted=bool(arguments.get("include_inverted", True)),
        )
    rotation_deg = float(arguments.get("rotation_degrees", 0.0))
    if rotation_deg:
        options["rotation"] = math.radians(rotation_deg)

    try:
        field = patterns.PATTERNS[kind](length, width, cell, rib, **options)
    except PatternError as exc:
        raise ArgumentError(
            f"{exc}. Lattice uses the XY footprint: X={length / CM_PER_INCH:g} in, "
            f"Y={width / CM_PER_INCH:g} in. Inspect the body orientation and "
            "requested dimensions before changing cell size or cutter.",
            footprint_cm={"x": length, "y": width}, cell_size_cm=cell,
        ) from exc

    # A router leaves its own radius in every interior corner, so a lattice
    # drawn with sharp corners is a model the machine cannot reproduce.
    # Rounding here rather than in a post-pass means the cut is right the
    # first time -- and the rib width is measured on the geometry that will
    # actually exist, not on a sharp-cornered idealisation of it.
    # Junction valence concerns the original cell corners. Arc approximation
    # vertices cannot represent how many neighbouring cells meet at a junction.
    topology_field = field
    cutter_expr = arguments.get("cutter_diameter")
    cutter = _cm(units_manager, cutter_expr, "cutter_diameter") if cutter_expr else 0.0
    mode = arguments.get("corner_mode", "sketch_rounding" if cutter else "sharp")
    if mode not in {"sharp", "sketch_rounding", "feature_fillet"}:
        raise ArgumentError("Unknown corner_mode")
    if mode != "sharp" and not cutter:
        raise ArgumentError("cutter_diameter is required for rounded corners")
    corner_radius = cutter / 2.0 if mode == "sketch_rounding" else 0.0
    if corner_radius > 0:
        if corner_radius >= cell / 2.0:
            raise ArgumentError(
                "cutter_diameter is too large for this cell_size; "
                "the pocket would close up")
        segments = arguments.get("corner_segments", 6)
        if isinstance(segments, bool) or not isinstance(segments, (int, float)) or not math.isfinite(segments) or int(segments) != segments or segments < 2:
            raise ArgumentError("corner_segments must be an integer >= 2")
        segments = int(segments)
        complexity.Limits.configured().require(
            cells=len(field), sketch_curves=sum(len(p) for p in field) * (segments + 1))
        field = geom.fillet_polygons(field, corner_radius, segments)
    workload = complexity.polygon_counts(field)

    skin_expr = arguments.get("skin_thickness", "0 in")
    skin = _cm(units_manager, skin_expr, "skin_thickness", allow_zero=True)
    thickness = box.maxPoint.z - box.minPoint.z
    if skin >= thickness:
        raise ArgumentError("skin_thickness must be less than the body thickness")
    depth = thickness - skin
    if skin <= 0:
        depth *= 1.05   # overshoot so a through cut leaves no sliver

    preflight = CheckSuite("preflight")
    preflight.add(assertions.polygons_disjoint("cells_disjoint", field))
    preflight.add(assertions.minimum_gap("rib_width_held", field, rib * 0.99))
    # Overlap and spacing checks cannot see a field whose rows fail to
    # interlock; this is the one that can.
    preflight.add(assertions.tiling_valence(
        "cells_tessellate", topology_field, expected=6 if kind == "isogrid" else 3,
        severity_ok_when_deliberate=True))
    preflight.add(assertions.rib_aspect_ratio("rib_aspect", rib, depth, 8.0))
    if mode != "feature_fillet":
        preflight.add(assertions.corners_machinable(
            "corners_machinable", field, corner_radius))
    preflight.require()
    reports = [preflight.report()]

    if arguments.get("_preflight_only"):
        return {"complexity": workload, "verification": reports}
    timeline_start = design.timeline.count
    before = body.volume
    top = pick.faces(body).planar().normal_axis("z").extreme("+z").one()
    with operation(design, "lattice_pockets") as (_, suite):
        cut_feature = ops.cut_polygons(root, body, top, field, -depth, box.maxPoint.z,
                         name=f"{kind.title()} Lattice", suite=suite,
                         sides=len(field[0]))
        suite.add(Check(
            "target_still_solid", body.isSolid, str(body.isSolid), "True"
        ))
        suite.require()
        suite.add(assertions.value_decreased(
            "material_removed", before, body.volume))
    reports.append(suite.report())

    if mode == "feature_fillet":
        fillet_result = router_fillet_pocket_corners({"body": body.name,
            "cutter_diameter": cutter_expr, "rib_width_in": rib / CM_PER_INCH})
        reports.extend(fillet_result["verification"])
    chain = [design.timeline.item(i).entity for i in range(timeline_start, design.timeline.count)]
    import json
    recipe = {"version": 1, "arguments": {k: v for k, v in arguments.items() if not k.startswith("_")},
              "tokens": [e.entityToken for e in chain]}
    recipe["arguments"]["body"] = body.name
    cut_feature.attributes.add("fusion_cad_agent", "lattice", json.dumps(recipe))
    for entity in chain:
        if hasattr(entity, "sketchCurves"): entity.isVisible = False
    solid = body
    result_name = arguments.get("result_name")
    if result_name is not None:
        solid.name = _safe_name(result_name, solid.name)
        recipe["arguments"]["body"] = solid.name
        cut_feature.attributes.add("fusion_cad_agent", "lattice", json.dumps(recipe))
    APP.activeViewport.fit()
    return {
        "lattice_ref": editing.ref(design, cut_feature),
        "corner_mode": mode,
        "created_entities": [editing.row(design, e) for e in chain],
        "pattern": kind,
        "cells": len(field),
        "complexity": workload,
        "row_stagger": options.get("row_stagger", 0.0),
        "corner_radius_cm": corner_radius,
        "native_fillet_radius_cm": cutter / 2 if mode == "feature_fillet" else 0.0,
        "sketch_representation": {"mode": mode, "pocket_count": len(field),
            "generated_lines": sum(map(len, field)), "lines_per_pocket": len(field[0]),
            "rounded_by_chord_segments": mode == "sketch_rounding"},
        "machinable": mode != "sharp",
        "depth_cm": depth,
        "skeletonized": skin <= 0,
        "mass_reduction_percent": 100 * (before - solid.volume) / before,
        "body": _body_summary(solid),
        "verification": reports,
    }


TOOLS["cut_polygons"] = {
    "description": (
        "Cut arbitrary polygons into an existing body. The escape hatch from "
        "the parametric tools: compute any 2D field yourself and cut it. "
        "Overlap and containment are checked before anything is built."
    ),
    "mutating": True,
    "arguments": {
        "polygons": POLYGON_SCHEMA,
        "units": "Coordinate units: in, mm, cm, m. Default in.",
        "depth": "Cut depth as a unit expression, e.g. '0.5 in'.",
        "through": "Cut clean through the body; default false.",
        "plane": "Where to sketch: 'top' (default), 'bottom', 'xy', or 'z=<number>'.",
        "body": "Target body name; defaults to the first body.",
        "name": "Optional feature name.",
    },
}
TOOLS["extrude_polygons"] = {
    "description": "Create a new body by extruding arbitrary polygons.",
    "mutating": True,
    "arguments": {
        "polygons": POLYGON_SCHEMA,
        "units": "Coordinate units: in, mm, cm, m. Default in.",
        "height": "Extrusion height as a unit expression, e.g. '1 in'.",
        "base": "Z height of the sketch plane; default '0 in'.",
        "name": "Optional body name.",
    },
}
TOOLS["lattice_pockets"] = {
    "description": (
        "Cut a parametric lattice into an existing body. Supports isogrid and "
        "hex, with row stagger, row pitch, and rotation."
    ),
    "mutating": True,
    "arguments": {
        "pattern": "isogrid | hex. Default isogrid.",
        "cell_size": "default '1 in'",
        "rib_width": "default '0.1 in'",
        "margin": "Solid band at the edges; defaults to rib_width.",
        "skin_thickness": "Uncut floor; '0 in' cuts through. Default '0 in'.",
        "row_stagger": "isogrid only: fraction of a cell each row shifts "
                       "sideways. Default 0.5, which is what makes the field "
                       "tessellate (six triangles per vertex). 0 gives "
                       "independent rows separated by horizontal rib bands.",
        "row_pitch": "isogrid only: multiplier on row spacing. Default 1.",
        "include_inverted": "isogrid only: include inverted triangles. Default true.",
        "rotation_degrees": "Rotate the field. Default 0.",
        "body": "Target body name; defaults to the first body.",
        "result_name": "Optional new name for the pocketed target body.",
        "cutter_diameter": "Round pocket corners for this router/end-mill, e.g. '0.125 in'. Omit for sharp corners.",
        "corner_segments": "Chord segments per rounded corner. Default 6.",
    },
}
HANDLERS["cut_polygons"] = cut_polygons
HANDLERS["extrude_polygons"] = extrude_polygons
HANDLERS["lattice_pockets"] = lattice_pockets


# --- State echo -------------------------------------------------------------
#
# A logged design session spent ten of its thirty-seven CAD calls on
# `design_summary`, every one of them immediately after a mutation, asking
# what the document now contained. That question has a known answer at the
# moment the mutation returns, and each round trip to ask it re-sent the
# entire conversation.
#
# So answer it unasked. Every mutating tool carries a compact digest of the
# resulting design. It is not the full summary -- entity tokens, sketch
# names and per-component feature lists stay behind `design_summary` -- but
# it is enough to decide what to do next, which is what those ten calls
# were for.

def _digest_body(body) -> dict[str, Any]:
    try:
        bounds = body.preciseBoundingBox or body.boundingBox
        low, high = bounds.minPoint, bounds.maxPoint
    except Exception:
        return {"name": body.name, "unmeasurable": True}
    return {
        "name": body.name,
        # Inches, because every dimension in this repo's requests is in
        # inches and a reader should not have to divide by 2.54 to sanity
        # check a bounding box.
        "size_in": [round((high.x - low.x) / 2.54, 4),
                    round((high.y - low.y) / 2.54, 4),
                    round((high.z - low.z) / 2.54, 4)],
        "volume_in3": (round(volume / 16.387064, 4)
                       if (volume := _measure(body, "volume")) is not None else None),
        "faces": body.faces.count,
    }


def design_digest() -> dict[str, Any]:
    """Cheap post-mutation state echo. Never raises: it rides on results."""
    try:
        design = _active_design()
        document = APP.activeDocument
        bodies = []
        for index in range(design.allComponents.count):
            component = design.allComponents.item(index)
            bodies.extend(_digest_body(b) for b in pick.bodies(component))
        return {
            "document": document.name if document else None,
            "saved": bool(document and document.isSaved and not document.isModified),
            "timeline_entries": design.timeline.count,
            "body_count": len(bodies),
            "bodies": bodies,
        }
    except Exception as exc:  # pragma: no cover - depends on live Fusion
        return {"unavailable": f"{exc.__class__.__name__}: {exc}"}


def _echo_state(handler):
    def wrapped(arguments: dict[str, Any]) -> dict[str, Any]:
        additive = handler.__name__ in {"lattice_pockets", "router_fillet_pocket_corners", "extrude_polygons", "cut_polygons"}
        design, start = None, None
        if additive:
            try:
                design = _active_design()
                start = design.timeline.count
                if design.timeline.markerPosition != start:
                    raise ArgumentError("Timeline is rolled back; move to the end before creating a feature")
            except ArgumentError: raise
            except Exception: design = None
        try:
            with tool_scope(handler.__name__):
                result = handler(arguments)
        except Exception as exc:
            editing.invalidate_undo()
            setattr(exc, "design", design_digest())
            raise
        if isinstance(result, dict):
            result.setdefault("design", design_digest())
            result.setdefault("completion", "completed")
            if design is not None and design.timeline.count > start:
                entities = [design.timeline.item(i).entity for i in range(start, design.timeline.count)]
                result.setdefault("created_entities", [editing.row(design, e) for e in entities if e is not None])
                try:
                    editing.remember(design, [editing.ref(design, e) for e in entities if e is not None])
                    result["undo_available"] = True
                except Exception as exc:
                    editing.invalidate_undo()
                    result["undo_available"] = False
                    result["undo_unavailable_reason"] = str(exc)
            elif handler.__name__ != "undo_last":
                editing.invalidate_undo()
        return result
    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    wrapped.__doc__ = handler.__doc__
    return wrapped


for _name, _spec in TOOLS.items():
    if _spec.get("mutating") and _name in HANDLERS:
        HANDLERS[_name] = _echo_state(HANDLERS[_name])
del _name, _spec


# --- Assembly ---------------------------------------------------------------
#
# The problem this project opened with: a motor mated to a gearbox whose
# primary axis pointed the wrong way. The tree looked right, the agent had no
# idea, and ten minutes of work got built on top of it.
#
# The fix is not a better prompt. A backwards mate drives two solids through
# each other, and that overlap is one number. So the tool creates the joint,
# measures, and if the parts now interfere, flips it and measures again --
# keeping whichever direction the geometry says is right. The agent never has
# to guess `isFlipped`, and cannot be wrong about it silently.

def _resolve_occurrence(root, name: str, field: str):
    if not name or not isinstance(name, str):
        raise ArgumentError(f"{field} must be an occurrence name")
    found = pick.occurrences(root).named(name)
    if not len(found):
        found = pick.occurrences(root).where(
            lambda o: name.lower() in o.name.lower(), "contains")
    if not len(found):
        available = [o.name for o in pick.occurrences(root)][:12]
        raise ArgumentError(
            f"no occurrence matching {name!r}. Present: "
            + (", ".join(available) if available else "none -- this design has "
               "no placed components; import or create one first"))
    if len(found) > 1:
        raise ArgumentError(
            f"{name!r} matches {len(found)} occurrences: "
            + ", ".join(o.name for o in found.all()[:8])
            + ". Use the exact name.")
    return found.one()


def _face_for_joint(occurrence, spec: dict[str, Any], field: str):
    """Resolve a face on an occurrence from a small query object.

    Deliberately the same vocabulary as the rest of the selection layer --
    `planar`, `cylindrical`, `facing`, `extreme`, `radius` -- so knowing how
    to find a face for a cut is knowing how to find one for a mate.
    """
    if not isinstance(spec, dict):
        raise ArgumentError(f"{field} must be an object describing a face")

    bodies = list(pick.bodies(occurrence.component))
    if not bodies:
        raise ArgumentError(f"{occurrence.name} has no solid bodies to mate")
    body_name = spec.get("body")
    if body_name:
        matching = [b for b in bodies if b.name == body_name]
        if not matching:
            raise ArgumentError(
                f"{field}.body {body_name!r} not in {occurrence.name}; have: "
                + ", ".join(b.name for b in bodies[:8]))
        bodies = matching

    selection = pick.faces(bodies[0])
    kind = spec.get("kind", "planar")
    if kind == "planar":
        selection = selection.planar()
    elif kind == "cylindrical":
        selection = selection.cylindrical()
    elif kind != "any":
        raise ArgumentError(f"{field}.kind must be planar, cylindrical or any")

    if spec.get("facing"):
        selection = selection.facing(str(spec["facing"]))
    if spec.get("normal_axis"):
        selection = selection.normal_axis(str(spec["normal_axis"]))
    if spec.get("axis_along"):
        selection = selection.axis_along(str(spec["axis_along"]))
    if spec.get("radius") is not None:
        selection = selection.radius(float(spec["radius"]))
    if spec.get("extreme"):
        selection = selection.extreme(str(spec["extreme"]))

    if not len(selection):
        raise ArgumentError(
            f"{field} matched no face on {occurrence.name} "
            f"({selection.note}). Call list_joint_candidates to see what is there.")
    return selection.first()


def list_joint_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
    """What is placed in this design, and what could be mated.

    Read-only, and deliberately generous: guessing a face and being told
    "matched nothing" costs a round trip, so this returns enough in one call
    that the next call can be the joint itself.
    """
    design = _active_design()
    root = design.rootComponent
    limit = int(arguments.get("max_faces", 8))
    wanted = arguments.get("occurrence")

    listing = []
    for occurrence in pick.occurrences(root):
        if wanted and wanted.lower() not in occurrence.name.lower():
            continue
        translation = ops.occurrence_translation(occurrence)
        entry: dict[str, Any] = {
            "name": occurrence.name,
            "component": occurrence.component.name,
            "grounded": bool(occurrence.isGrounded),
            "position_in": [round(v / CM_PER_INCH, 4) for v in translation],
            "bodies": [b.name for b in pick.bodies(occurrence.component)],
            "joints": occurrence.component.joints.count,
        }
        # Face classification is the expensive part: it asks every face for
        # its geometry. An imported swerve module is thousands of faces, and
        # doing this for every occurrence took 79 seconds -- long enough that
        # the agent stopped using the tool it needs to mate anything.
        # Sample instead: the counts were never the point, the radii were.
        bodies = list(pick.bodies(occurrence.component))
        entry["body_count"] = len(bodies)
        for body in bodies[:1]:
            faces = [body.faces.item(i)
                     for i in range(min(body.faces.count, FACE_SAMPLE))]
            radii = set()
            planar = cylindrical = 0
            for face in faces:
                try:
                    kind = face.geometry.surfaceType
                except Exception:
                    continue
                if kind == adsk.core.SurfaceTypes.PlaneSurfaceType:
                    planar += 1
                elif kind == adsk.core.SurfaceTypes.CylinderSurfaceType:
                    cylindrical += 1
                    if len(radii) < limit:
                        radii.add(round(face.geometry.radius / CM_PER_INCH, 4))
            entry["faces"] = {
                "body": body.name,
                "total": body.faces.count,
                "sampled": len(faces),
                "planar": planar,
                "cylindrical": cylindrical,
                "cylinder_radii_in": sorted(radii),
            }
        listing.append(entry)

    total, worst = assertions.interference_total(design)
    return {
        "occurrences": listing,
        "count": len(listing),
        "interference_cm3": round(total, 6),
        "interfering_pairs": worst[:6],
        "hint": ("Mate with create_joint. Faces are chosen with the same "
                 "vocabulary as cuts: {kind: planar|cylindrical, facing: '+z', "
                 "extreme: '-x', radius: <cm>}."),
    }


def create_joint(arguments: dict[str, Any]) -> dict[str, Any]:
    """Mate two occurrences, resolving the flip by measurement.

    Returns `flip_resolved: true` when the requested direction interfered and
    the opposite one did not -- which is the error that started this project,
    caught in the operation that caused it rather than ten minutes later.
    """
    design = _active_design()
    root = design.rootComponent
    units_manager = design.unitsManager

    moving = _resolve_occurrence(root, arguments.get("move"), "move")
    fixed = _resolve_occurrence(root, arguments.get("to"), "to")
    if moving.entityToken == fixed.entityToken:
        raise ArgumentError("move and to must be different occurrences")

    motion = str(arguments.get("motion", "rigid"))
    if motion not in ops.JOINT_MOTIONS:
        raise ArgumentError(f"motion must be one of: {', '.join(ops.JOINT_MOTIONS)}")

    offset_expr = arguments.get("offset")
    offset = _cm(units_manager, offset_expr, "offset", allow_zero=True) if offset_expr else 0.0
    angle = math.radians(float(arguments.get("angle_degrees", 0.0)))
    axis = str(arguments.get("axis", "z"))
    auto_flip = bool(arguments.get("auto_flip", True))
    requested_flip = bool(arguments.get("flipped", False))

    face_one = _face_for_joint(moving, arguments.get("move_face") or {}, "move_face")
    face_two = _face_for_joint(fixed, arguments.get("to_face") or {}, "to_face")
    key_point = str(arguments.get("key_point", "center"))

    before_interference, _ = assertions.interference_total(design)
    before_position = ops.occurrence_translation(moving)

    preflight = CheckSuite("preflight")
    preflight.add(Check("occurrences_distinct", True, moving.name, f"!= {fixed.name}"))
    preflight.add(Check(
        "target_not_grounded", not moving.isGrounded, str(moving.isGrounded), "False",
        "a grounded occurrence cannot be moved by a joint; ground the other one"))
    preflight.require()
    reports = [preflight.report()]

    attempts: list[dict[str, Any]] = []
    flip_resolved = False

    with operation(design, "create_joint") as (_, suite):
        geometry_one = ops.joint_geometry(face_one, key_point)
        geometry_two = ops.joint_geometry(face_two, key_point)
        joint = ops.create_joint(
            root, geometry_one, geometry_two, motion=motion, axis=axis,
            offset=offset, angle=angle, flipped=requested_flip,
            name=_safe_name(arguments.get("name"), f"{moving.name} to {fixed.name}"))

        after_interference, worst = assertions.interference_total(design)
        attempts.append({"flipped": requested_flip,
                         "interference_cm3": round(after_interference, 6)})

        # The whole point. If the requested direction drove the parts into
        # each other, try the other one before reporting anything -- and only
        # keep it if it is genuinely better, so a legitimately tight assembly
        # is not flipped for nothing.
        if auto_flip and after_interference > before_interference + 1e-6:
            joint.isFlipped = not joint.isFlipped
            flipped_interference, flipped_worst = assertions.interference_total(design)
            attempts.append({"flipped": bool(joint.isFlipped),
                             "interference_cm3": round(flipped_interference, 6)})
            verdict = assertions.resolve_flip(
                before_interference, after_interference, flipped_interference)
            if verdict == "flip":
                after_interference, worst = flipped_interference, flipped_worst
                flip_resolved = True
            else:
                # Neither direction works: the targets are wrong, not the
                # flag. Restore and let the check below roll it back.
                joint.isFlipped = not joint.isFlipped

        suite.add(assertions.interference_not_worsened(
            "no_new_interference", before_interference, after_interference))
        suite.require()
        suite.add(assertions.occurrence_moved(
            "occurrence_moved", before_position,
            ops.occurrence_translation(moving)))

    reports.append(suite.report())
    APP.activeViewport.refresh()
    return {
        "joint": joint.name,
        "motion": motion,
        "moved": moving.name,
        "fixed_to": fixed.name,
        "flipped": bool(joint.isFlipped),
        "flip_resolved": flip_resolved,
        "attempts": attempts,
        "interference_cm3": round(after_interference, 6),
        "interfering_pairs": worst[:4],
        "position_in": [round(v / CM_PER_INCH, 4)
                        for v in ops.occurrence_translation(moving)],
        "verification": reports,
    }


_FACE_QUERY_SCHEMA = {
    "type": "object",
    "description": "Which face to mate. Same vocabulary as cuts.",
    "properties": {
        "kind": {"type": "string", "enum": ["planar", "cylindrical", "any"],
                 "description": "Face type. Default planar."},
        "body": {"type": "string", "description": "Body name, if the component has several."},
        "facing": {"type": "string", "description": "Signed outward normal, e.g. '+z'."},
        "normal_axis": {"type": "string", "description": "Unsigned normal axis, e.g. 'z'."},
        "axis_along": {"type": "string", "description": "Cylinder axis, e.g. 'x'."},
        "radius": {"type": "number", "description": "Cylinder radius in cm."},
        "extreme": {"type": "string", "description": "Outermost in a direction, e.g. '-x'."},
    },
}

TOOLS["list_joint_candidates"] = {
    "description": ("What is placed in this design and what could be mated: "
                    "occurrences, their positions, face counts and cylinder radii, "
                    "plus current interference."),
    "mutating": False,
    "arguments": {
        "occurrence": "Optional name filter.",
        "max_faces": "Cylinder radii to sample per body. Default 8.",
    },
}

TOOLS["create_joint"] = {
    "description": ("Mate two occurrences. Creates the joint, measures interference, "
                    "and if the parts now overlap, flips it and keeps whichever "
                    "direction the geometry says is right. Rolls back if neither works."),
    "mutating": True,
    "arguments": {
        "move": "Name of the occurrence that moves.",
        "to": "Name of the occurrence it mates to.",
        "move_face": _FACE_QUERY_SCHEMA,
        "to_face": _FACE_QUERY_SCHEMA,
        "motion": "rigid, revolute, slider, cylindrical, planar or ball. Default rigid.",
        "axis": "Motion axis for non-rigid joints: x, y or z. Default z.",
        "offset": "Unit expression, e.g. '0.25 in'. Default 0.",
        "angle_degrees": "Rotation about the joint axis. Default 0.",
        "key_point": "center, start, end or middle. Default center.",
        "flipped": "Try this direction first. Default false.",
        "auto_flip": "Resolve the direction by measuring interference. Default true.",
        "name": "Optional joint name.",
    },
}

HANDLERS["list_joint_candidates"] = list_joint_candidates
HANDLERS["create_joint"] = _echo_state(create_joint)


# --- Files and the network --------------------------------------------------
#
# The gap: asked to mate a swerve module to a tube, the agent had no way to
# find the STEP file, open the vendor drawing, or read a dimension off it.
# `import_step_component` existed but wanted an absolute path it could not
# discover, so a person had to paste one.
#
# Everything here is read-only. Fencing, the secret denylist and the
# refusal to fetch inward all live in workspace.py.

def list_files(arguments: dict[str, Any]) -> dict[str, Any]:
    """Look in a readable directory. Start here, not with a guessed path."""
    directory = arguments.get("directory")
    if not directory:
        # No argument means "what can I see at all", which is the question
        # actually being asked the first time.
        return {"roots": [str(r) for r in workspace.allowed_roots()],
                "hint": "Call again with one of these as `directory`."}
    return workspace.list_dir(
        str(directory), str(arguments.get("pattern", "*")),
        int(arguments.get("limit", 200)),
        bool(arguments.get("recursive", False)))


def read_file(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read a text file: a spec, a parts list, a source file in this repo."""
    return workspace.read_text(arguments["path"], arguments.get("max_bytes", 12000),
                               start_line=arguments.get("start_line", 1),
                               end_line=arguments.get("end_line"), symbol=arguments.get("symbol"))


def fetch_url(arguments: dict[str, Any]) -> dict[str, Any]:
    """Download a public document -- a vendor drawing, a spec sheet, a STEP.

    A PDF is rendered to PNG in the same call, because a dimensioned drawing
    is the densest source of truth available for a mating part and is
    unreadable as bytes. Pass the returned image paths to capture-style
    inspection to actually read it.
    """
    result = workspace.fetch(str(arguments.get("url", "")))
    if (result["path"].lower().endswith(".pdf")
            or "pdf" in result.get("content_type", "")):
        if arguments.get("render_pdf", True):
            try:
                result["rendered"] = workspace.pdf_to_images(
                    result["path"], int(arguments.get("pages", 4)))
            except BridgeError as exc:
                result["render_error"] = str(exc)
    return result


def pdf_to_images(arguments: dict[str, Any]) -> dict[str, Any]:
    """Rasterise a PDF already on disk so it can be looked at."""
    return workspace.pdf_to_images(str(arguments.get("path", "")),
                                    int(arguments.get("pages", 4)),
                                    int(arguments.get("dpi", 200)))


IMPORT_OPTIONS = {
    ".step": "createSTEPImportOptions", ".stp": "createSTEPImportOptions",
    ".iges": "createIGESImportOptions", ".igs": "createIGESImportOptions",
    ".sat": "createSATImportOptions", ".smt": "createSMTImportOptions",
    ".f3d": "createFusionArchiveImportOptions",
    ".f3z": "createFusionArchiveImportOptions",
    ".dxf": "createDXFImportOptions", ".svg": "createSVGImportOptions",
}


def import_geometry(arguments: dict[str, Any]) -> dict[str, Any]:
    """Import a CAD file into the current design.

    Generalises the STEP-only import: the vendor file you actually have is as
    often IGES, SAT or a Fusion archive, and being told "only .step" after
    finding the right file is a wasted turn.
    """
    path = workspace.resolve(str(arguments.get("path", "")))
    suffix = path.suffix.lower()
    factory = IMPORT_OPTIONS.get(suffix)
    if not factory:
        raise ArgumentError(
            f"cannot import {suffix}; supported: "
            + ", ".join(sorted(IMPORT_OPTIONS)))

    design = _active_design()
    root = design.rootComponent
    before = {b.name for c in
              (design.allComponents.item(i) for i in range(design.allComponents.count))
              for b in pick.bodies(c)}
    before_occurrences = root.allOccurrences.count

    manager = APP.importManager
    options = getattr(manager, factory)(str(path))
    target = root
    with operation(design, "import_geometry") as (_, suite):
        if suffix in (".dxf", ".svg"):
            # 2D formats import onto a plane, not into a component.
            plane = root.xYConstructionPlane
            options = getattr(manager, factory)(str(path), plane)
            manager.importToTarget(options, root)
        elif arguments.get("flatten_into_root"):
            manager.importToTarget(options, target)
        else:
            manager.importToTarget2(options, target)
        suite.add(Check(
            "something_was_imported",
            root.allOccurrences.count > before_occurrences
            or any(b.name not in before for c in
                   (design.allComponents.item(i)
                    for i in range(design.allComponents.count))
                   for b in pick.bodies(c)),
            f"{root.allOccurrences.count} occurrences",
            f"> {before_occurrences}",
            "the file imported but produced no new geometry"))
        suite.require()

    APP.activeViewport.fit()
    return {"imported": str(path), "format": suffix,
            "occurrences": root.allOccurrences.count,
            "new_occurrences": root.allOccurrences.count - before_occurrences,
            "verification": [suite.report()],
            "next": "list_joint_candidates now shows it, and create_joint mates it.",
            }


TOOLS["list_files"] = {
    "description": ("List a readable directory. Call with no arguments to see "
                    "which directories are readable at all. Flags which files "
                    "can be imported or opened."),
    "mutating": False,
    "arguments": {
        "directory": "Directory to list. Omit to see the readable roots.",
        "pattern": "Glob, e.g. '*.step'. Default '*'.",
        "recursive": "Search subdirectories. Default false.",
        "limit": "Maximum entries. Default 200.",
    },
}
TOOLS["read_file"] = {
    "description": "Read a text file: a spec, a parts list, or source in this repo.",
    "mutating": False,
    "arguments": {"path": "Path to a text file inside a readable directory.",
                  "max_bytes": "Truncate after this many bytes. Default 400000."},
}
TOOLS["fetch_url"] = {
    "description": ("Download a public http(s) document -- a vendor drawing, "
                    "spec sheet or STEP file. PDFs are rendered to PNG so they "
                    "can be read. Refuses private and loopback addresses."),
    "mutating": False,
    "arguments": {"url": "Public http or https URL.",
                  "render_pdf": "Rasterise a PDF on arrival. Default true.",
                  "pages": "PDF pages to render. Default 4, maximum 8."},
}
TOOLS["pdf_to_images"] = {
    "description": "Rasterise a PDF already on disk so a drawing can be read.",
    "mutating": False,
    "arguments": {"path": "Path to a .pdf inside a readable directory.",
                  "pages": "Pages to render. Default 4, maximum 8.",
                  "dpi": "Resolution. Default 200."},
}
TOOLS["import_geometry"] = {
    "description": ("Import STEP, IGES, SAT, SMT, Fusion archive, DXF or SVG "
                    "into the current design. Supersedes import_step_component."),
    "mutating": True,
    "arguments": {
        "path": "Path to the CAD file inside a readable directory.",
        "flatten_into_root": "Import bodies into the root rather than as a component. Default false.",
    },
}

HANDLERS["list_files"] = list_files
HANDLERS["read_file"] = read_file
HANDLERS["fetch_url"] = fetch_url
HANDLERS["pdf_to_images"] = pdf_to_images
HANDLERS["import_geometry"] = _echo_state(import_geometry)


# --- Documents --------------------------------------------------------------
#
# Fusion can have several documents open at once, and every tool here acts on
# whichever one is active. Without a way to see and change that, an agent
# resuming work has no idea which document it is about to modify -- and the
# failure mode is silent: it cuts a lattice into the wrong part.

# Enough faces to characterise a part, few enough to stay interactive.
FACE_SAMPLE = 400


def _document_entry(document, active, index: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        # Fusion happily keeps a dozen documents all called "Untitled", so a
        # name is not an address. The index is.
        "index": index,
        "name": document.name,
        "active": document == active,
        "modified": bool(document.isModified),
        "saved": bool(document.isSaved),
    }
    try:
        # A never-saved document has no dataFile at all.
        if document.dataFile:
            entry["path"] = document.dataFile.parentFolder.name + "/" + document.dataFile.name
    except Exception:
        pass
    try:
        design = adsk.fusion.Design.cast(document.products.itemByProductType("DesignProductType"))
        if design:
            entry["bodies"] = sum(
                len(pick.bodies(design.allComponents.item(i)))
                for i in range(design.allComponents.count))
    except Exception:
        pass
    return entry


def list_documents(_: dict[str, Any]) -> dict[str, Any]:
    """Every open Fusion document, and which one the tools are acting on."""
    documents = APP.documents
    active = APP.activeDocument
    entries = [_document_entry(documents.item(i), active, i)
               for i in range(documents.count)]
    return {
        "documents": entries,
        "count": len(entries),
        "active": getattr(active, "name", None),
        "hint": ("Every other tool acts on the active document. "
                 "switch_document changes it, by name or by index."
                 if len(entries) > 1 else ""),
        "unsaved": sum(1 for e in entries if not e["saved"]),
    }


def switch_document(arguments: dict[str, Any]) -> dict[str, Any]:
    """Make a different open document active.

    Matches on exact name first, then on a unique substring -- an agent that
    read "codex_drivebase" from a summary should not have to reproduce
    Fusion's exact titling to act on it. An ambiguous match is an error
    rather than a guess: activating the wrong document then cutting into it
    is not recoverable by undo in the way a bad feature is.
    """
    documents = APP.documents
    everything = [documents.item(i) for i in range(documents.count)]
    if not everything:
        raise ToolFailure("Fusion has no open documents")

    def catalogue() -> str:
        return "; ".join(f"[{i}] {d.name}" for i, d in enumerate(everything))

    index = arguments.get("index")
    wanted = arguments.get("name")

    if index is not None:
        try:
            position = int(index)
        except (TypeError, ValueError):
            raise ArgumentError("index must be a whole number") from None
        if not 0 <= position < len(everything):
            raise ArgumentError(
                f"index {position} is out of range; open documents are "
                + catalogue())
        target = everything[position]
    else:
        if not wanted or not isinstance(wanted, str):
            raise ArgumentError(
                "pass a name or an index; see list_documents")
        matches = [d for d in everything if d.name == wanted]
        if not matches:
            matches = [d for d in everything if wanted.lower() in d.name.lower()]
        if not matches:
            raise ArgumentError(
                f"no open document matching {wanted!r}. Open: " + catalogue())
        if len(matches) > 1:
            # Repeating the same name sixteen times says nothing. Say where
            # they are instead, which is what makes them reachable.
            positions = [i for i, d in enumerate(everything) if d in matches]
            raise ArgumentError(
                f"{wanted!r} matches {len(matches)} open documents at indexes "
                + ", ".join(str(i) for i in positions)
                + ". Pass `index` instead of `name` to pick one.")
        target = matches[0]
    previous = getattr(APP.activeDocument, "name", None)
    if target == APP.activeDocument:
        return {"active": target.name, "changed": False,
                "note": "that document was already active",
                "design": design_digest()}

    if not target.activate():
        raise ToolFailure(f"Fusion declined to activate {target.name}")
    now = getattr(APP.activeDocument, "name", None)
    if now != target.name:
        raise ToolFailure(
            f"asked for {target.name} but {now} is active; the switch did not take")

    return {"active": now, "previous": previous, "changed": True,
            "warning": ("the document you switched away from has unsaved changes"
                        if any(d.name == previous and d.isModified
                               for d in everything) else None),
            "design": design_digest()}


TOOLS["list_documents"] = {
    "description": ("Every open Fusion document and which one is active. "
                    "All other tools act on the active document."),
    "mutating": False,
    "arguments": {},
}
TOOLS["switch_document"] = {
    "description": ("Make a different open document active, by exact name or "
                    "unique substring. Everything afterwards acts on it."),
    "mutating": True,
    "arguments": {
        "name": "Name of an open document, exact or a unique substring.",
        "index": "Index from list_documents. Use this when several documents share a name.",
    },
}

HANDLERS["list_documents"] = list_documents
HANDLERS["switch_document"] = _echo_state(switch_document)


def bodies_to_components(arguments: dict[str, Any]) -> dict[str, Any]:
    """Turn root bodies into components so they can be mated.

    The mismatch this fixes: `create_tube_frame` and the other builders write
    bodies into the root component, while joints act on occurrences. An
    imported STEP arrives as occurrences, a frame built here does not, and so
    the two could not be mated to each other at all. An agent hit that wall
    and spent six minutes discovering there was no way through it.
    """
    design = _active_design()
    root = design.rootComponent
    wanted = arguments.get("bodies")
    if isinstance(wanted, str):
        wanted = [wanted]

    available = [root.bRepBodies.item(i) for i in range(root.bRepBodies.count)]
    if not available:
        raise ArgumentError(
            "the root component has no bodies; everything here is already "
            "a component. list_joint_candidates shows what can be mated.")

    if wanted:
        by_name = {b.name: b for b in available}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise ArgumentError(
                f"no root body named {', '.join(missing)}. Present: "
                + ", ".join(by_name))
        targets = [by_name[n] for n in wanted]
    else:
        targets = available

    before = root.allOccurrences.count
    made = []
    with operation(design, "bodies_to_components") as (_, suite):
        for body in targets:
            name = body.name
            occurrence = root.occurrences.addNewComponent(
                adsk.core.Matrix3D.create())
            occurrence.component.name = name
            # moveToComponent takes the occurrence, and invalidates the old
            # body reference -- read the name before moving, not after.
            body.moveToComponent(occurrence)
            made.append(occurrence.name)
        suite.add(Check(
            "components_created", root.allOccurrences.count == before + len(targets),
            root.allOccurrences.count, before + len(targets)))
        suite.require()

    return {
        "components": made,
        "count": len(made),
        "remaining_root_bodies": root.bRepBodies.count,
        "verification": [suite.report()],
        "next": "These are occurrences now; create_joint can mate them.",
    }


TOOLS["bodies_to_components"] = {
    "description": ("Convert bodies in the root component into components, so "
                    "joints can act on them. Frames and pans built here are "
                    "root bodies; imported CAD arrives as components, and only "
                    "components can be mated."),
    "mutating": True,
    "arguments": {
        "bodies": "Body names to convert. Omit to convert every root body.",
    },
}
HANDLERS["bodies_to_components"] = _echo_state(bodies_to_components)


def write_file(arguments: dict[str, Any]) -> dict[str, Any]:
    """Write a file in this repository. This is how you add a missing tool.

    The geometry modules hot-reload, so a new tool registered in tools.py is
    callable on the very next request -- no restart, no redeploy. If the tool
    you need does not exist, write it and then call it.
    """
    result = workspace.write_text(str(arguments.get("path", "")),
                                   arguments.get("content", ""))
    if arguments.get("run_tests", True):
        # Writing without checking is how a broken generator ships. The
        # suite is fast; there is no reason to skip it.
        result["tests"] = workspace.run_tests()
    tool_contracts.apply(TOOLS)
    return result


def run_tests(arguments: dict[str, Any]) -> dict[str, Any]:
    return workspace.run_tests(str(arguments.get("pattern", "test*.py")))


TOOLS["write_file"] = {
    "description": ("Write a file in this repository -- typically to add a "
                    "missing tool or fix a generator. tools.py, patterns.py, "
                    "ops.py, pick.py, assertions.py, geom.py and workspace.py "
                    "hot-reload, so a new tool is callable on the next call. "
                    "Python that does not parse is refused, and the previous "
                    "contents are backed up."),
    "mutating": False,
    "arguments": {
        "path": "Path in this repository, absolute or relative to its root.",
        "content": "Full new contents of the file.",
        "run_tests": "Run the suite afterwards and report. Default true.",
    },
}
TOOLS["run_tests"] = {
    "description": "Run the repository's test suite and report failures.",
    "mutating": False,
    "arguments": {"pattern": "Test file glob. Default 'test*.py'."},
}

HANDLERS["write_file"] = write_file
HANDLERS["run_tests"] = run_tests


def feature_tree(arguments):
    return editing.tree(_active_design(), arguments.get("component"))


def inspect_entity(arguments):
    design = _active_design()
    return editing.row(design, editing.resolve(design, arguments["ref"]))


def delete_entities(arguments):
    return editing.delete(_active_design(), arguments["refs"])


def edit_lattice(arguments):
    import json
    from checkpoints import Checkpoint
    design = _active_design()
    feature = editing.resolve(design, arguments["lattice_ref"])
    attribute = feature.attributes.itemByName("fusion_cad_agent", "lattice")
    if attribute is None:
        raise ArgumentError("This feature has no managed lattice recipe; inspect and explicitly replace the old chain")
    recipe = json.loads(attribute.value)
    entities = []
    for token in recipe["tokens"]:
        found = design.findEntityByToken(token)
        if len(found) != 1: raise ArgumentError("Lattice chain changed; inspect feature_tree")
        entities.append(found[0])
    references = [editing.ref(design, e) for e in entities]
    editing.suffix(design, references)
    params = {**recipe["arguments"], **{k: v for k, v in arguments.items() if k != "lattice_ref"}}
    # Guard all pure geometry work before removing the old chain.
    lattice_pockets({**params, "_preflight_only": True})
    editing.delete(design, references)
    checkpoint = Checkpoint(design, "replacement_lattice")
    try:
        result = lattice_pockets(params)
    except Exception as exc:
        checkpoint.rollback()
        recovery = {"status": "partial", "restoration_verified": False,
                    "cleanup": checkpoint.recovery, "reason": "Old feature identities were removed"}
        if checkpoint.clean:
            try:
                restored = lattice_pockets(recipe["arguments"])
                recovery.update(previous_recipe_rebuilt=True, lattice_ref=restored["lattice_ref"])
            except Exception as restore_exc: recovery["rebuild_error"] = str(restore_exc)
        exc.recovery = recovery
        raise
    result["replaced_refs"] = references
    result["feature_tree"] = editing.tree(design)
    return result


def search_files(arguments):
    return workspace.search_text(arguments["query"], arguments.get("directory", "."),
                                 arguments.get("pattern", "*.py"), arguments.get("limit", 30))


def patch_file(arguments):
    result = workspace.patch_text(arguments["path"], arguments["old_text"],
                                  arguments["new_text"], arguments["expected_sha256"])
    if arguments.get("run_tests", True):
        result["tests"] = workspace.run_tests(arguments.get("test_pattern", "test*.py"))
    tool_contracts.apply(TOOLS)
    return result


def _register(name, handler, description, args, required=(), mutating=False):
    TOOLS[name] = {"description": description, "mutating": mutating,
                   "arguments": args, "required": list(required)}
    HANDLERS[name] = _echo_state(handler) if mutating else handler


_str = lambda description: {"type": "string", "description": description}
_register("create_document", create_document,
          "Create and activate a new empty, unsaved Fusion design document. Creates no bodies or sketches. Returns its document ID and design state; use modeling/import tools afterward.",
          {}, mutating=True)
_register("feature_tree", feature_tree, "Inspect native feature/sketch references, curve counts, health and profile dependencies.",
          {"component": _str("Optional exact component name")})
_register("inspect_entity", inspect_entity, "Inspect one native feature or sketch by its returned reference.",
          {"ref": _str("Reference from feature_tree or created_entities")}, ["ref"])
_register("delete_entities", delete_entities, "Delete explicit feature/sketch references in a complete trailing chain. Refuses implicit downstream deletion.",
          {"refs": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["refs"], True)
_register("edit_lattice", edit_lattice, "Revise a managed lattice in place: switch sharp/sketch_rounding/feature_fillet and change dimensions. Removes its old owned chain; refuses unrelated downstream features.",
          {"lattice_ref": _str("Cut feature reference returned by lattice_pockets")}, ["lattice_ref"], True)
_register("search_files", search_files, "Bounded literal source search. Returns file/line matches for focused reads.",
          {"query": _str("Literal text"), "directory": _str("Repository-relative or allowed absolute directory"),
           "pattern": _str("Filename glob, default *.py"), "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"])
_register("patch_file", patch_file, "Apply one exact source replacement guarded by the inspected file hash. Returns backup, tests, and activation requirements.",
          {"path": _str("Repository-relative file"), "old_text": _str("Unique exact text to replace"),
           "new_text": _str("Replacement text"), "expected_sha256": _str("sha256 from read_file"),
           "run_tests": {"type": "boolean"}, "test_pattern": _str("Focused test filename glob")},
          ["path", "old_text", "new_text", "expected_sha256"])

# Reload data/contracts at the capability boundary; clients refresh after source writes.
_importlib.reload(tool_contracts)
tool_contracts.apply(TOOLS)
TOOLS["undo_last"]["description"] = "Undo the last supported additive agent feature change synchronously. Refuses changed documents; never invokes global UI Undo."
for _tool_name, _spec in TOOLS.items():
    _handler = HANDLERS[_tool_name]
    def _checked(arguments, handler=_handler, name=_tool_name):
        tool_contracts.validate(arguments, TOOLS[name]["input_schema"])
        return handler(arguments)
    _checked.__name__ = _handler.__name__
    _checked.__qualname__ = _handler.__qualname__
    HANDLERS[_tool_name] = _checked
