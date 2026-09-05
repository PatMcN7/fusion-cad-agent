"""Autodesk Fusion add-in exposing a localhost-only, authenticated tool bridge."""

from __future__ import annotations

import json
import importlib
import math
import os
import sys
import threading
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import adsk.core
import adsk.fusion


ADDIN_DIR = Path(__file__).resolve().parent
if str(ADDIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADDIN_DIR))

# Fusion keeps imported helper modules alive when an add-in is stopped and run
# again. Reload the protocol module so newly added tools are available without
# requiring a full Fusion restart during development.
import common as _common  # noqa: E402

importlib.reload(_common)

from common import (  # noqa: E402
    API_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_REQUEST_BYTES,
    TOOLS,
    ProtocolError,
    agent_home,
    load_or_create_token,
    validate_request,
    write_connection_info,
)


# Fusion's own samples use simple identifier strings for custom events. Keeping
# this alphanumeric also avoids version-specific parsing of punctuation.
EVENT_ID = "FusionCadAgentExecuteEvent"
# A fully skeletonized all-edge fillet can touch a thousand edges, which
# takes Fusion well past a few seconds. The worker thread is only waiting
# on an Event, so a long ceiling costs nothing when operations are quick.
REQUEST_TIMEOUT_SECONDS = 300

_app: adsk.core.Application | None = None
_ui: adsk.core.UserInterface | None = None
_custom_event: adsk.core.CustomEvent | None = None
_event_handler: adsk.core.CustomEventHandler | None = None
_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_token: str | None = None
_pending: dict[str, "PendingRequest"] = {}
_pending_lock = threading.Lock()


class ToolFailure(RuntimeError):
    pass


class PendingRequest:
    def __init__(self, request: dict[str, Any]):
        self.request = request
        self.done = threading.Event()
        self.response: dict[str, Any] | None = None


def _log(message: str) -> None:
    # This function is also called by HTTP worker threads, where *no* Fusion API
    # method is safe. A plain local log keeps diagnostics thread-safe.
    formatted = f"[FusionCadAgent] {message}"
    try:
        base = agent_home()
        base.mkdir(parents=True, exist_ok=True)
        with (base / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(formatted + "\n")
    except Exception:
        pass
    print(formatted)


def _json_error(message: str, code: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _safe_name(value: Any, default: str = "Agent Test Box") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProtocolError("name must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 100:
        raise ProtocolError("name must contain 1 to 100 characters")
    return cleaned


def _active_design() -> adsk.fusion.Design:
    assert _app is not None
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        raise ToolFailure("The active document is not a Fusion design.")
    return design


def _point(point: adsk.core.Point3D) -> dict[str, float]:
    return {"x_cm": point.x, "y_cm": point.y, "z_cm": point.z}


def _body_summary(body: adsk.fusion.BRepBody) -> dict[str, Any]:
    bounds = body.preciseBoundingBox or body.boundingBox
    material = None
    try:
        if body.material:
            material = body.material.name
    except Exception:
        material = None

    return {
        "name": body.name,
        "entity_token": body.entityToken,
        "revision_id": body.revisionId,
        "is_solid": bool(body.isSolid),
        "volume_cm3": body.volume,
        "area_cm2": body.area,
        "faces": body.faces.count,
        "edges": body.edges.count,
        "vertices": body.vertices.count,
        "material": material,
        "bounding_box": {
            "min": _point(bounds.minPoint),
            "max": _point(bounds.maxPoint),
        },
    }


def _document_metadata() -> dict[str, Any]:
    assert _app is not None
    document = _app.activeDocument
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
        "is_active": bool(document.isActive),
        "data_file_id": data_file_id,
    }


def _health(_: dict[str, Any]) -> dict[str, Any]:
    assert _app is not None
    return {
        "bridge": "fusion-cad-agent",
        "api_version": API_VERSION,
        "fusion_version": _app.version,
        "main_thread_responding": True,
        "active_document": _document_metadata(),
    }


def _active_document(_: dict[str, Any]) -> dict[str, Any]:
    return _document_metadata()


def _design_summary(_: dict[str, Any]) -> dict[str, Any]:
    design = _active_design()
    components: list[dict[str, Any]] = []

    all_components = design.allComponents
    for index in range(all_components.count):
        component = all_components.item(index)
        bodies = [
            _body_summary(component.bRepBodies.item(body_index))
            for body_index in range(component.bRepBodies.count)
        ]
        components.append(
            {
                "name": component.name,
                "id": component.id,
                "features": component.features.count,
                "sketches": component.sketches.count,
                "bodies": bodies,
            }
        )

    return {
        "document": _document_metadata(),
        "design_type": str(design.designType),
        "timeline_entries": design.timeline.count,
        "component_count": all_components.count,
        "occurrence_count": design.rootComponent.allOccurrences.count,
        "components": components,
    }


def _expression_cm(
    units: adsk.core.UnitsManager,
    value: Any,
    field: str,
    *,
    allow_zero: bool = False,
) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field} must be a Fusion unit expression string")
    result = units.evaluateExpression(value, "cm")
    if allow_zero:
        # A zero skin is meaningful: it asks for a fully skeletonized lattice.
        if result < 0:
            raise ProtocolError(f"{field} must evaluate to a non-negative length")
    elif result <= 0:
        raise ProtocolError(f"{field} must evaluate to a positive length")
    return result


def _create_test_box(arguments: dict[str, Any]) -> dict[str, Any]:
    assert _app is not None
    name = _safe_name(arguments.get("name"))
    width_expression = arguments.get("width", "2 in")
    height_expression = arguments.get("height", "1 in")
    depth_expression = arguments.get("depth", "0.5 in")

    document = _app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        raise ToolFailure("Fusion failed to create a design document.")

    root = design.rootComponent
    units = design.unitsManager
    width_cm = _expression_cm(units, width_expression, "width")
    height_cm = _expression_cm(units, height_expression, "height")
    _expression_cm(units, depth_expression, "depth")

    sketch = root.sketches.add(root.xYConstructionPlane)
    sketch.name = f"{name} Sketch"
    lines = sketch.sketchCurves.sketchLines
    lines.addTwoPointRectangle(
        adsk.core.Point3D.create(-width_cm / 2, -height_cm / 2, 0),
        adsk.core.Point3D.create(width_cm / 2, height_cm / 2, 0),
    )
    if sketch.profiles.count != 1:
        raise ToolFailure(
            f"Expected one closed rectangle profile; Fusion reported {sketch.profiles.count}."
        )

    distance = adsk.core.ValueInput.createByString(depth_expression)
    extrude = root.features.extrudeFeatures.addSimple(
        sketch.profiles.item(0),
        distance,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    extrude.name = f"{name} Extrude"
    if extrude.bodies.count != 1:
        raise ToolFailure(
            f"Expected one extruded body; Fusion reported {extrude.bodies.count}."
        )
    body = extrude.bodies.item(0)
    body.name = name
    _app.activeViewport.fit()

    return {
        "created_unsaved_document": document.name,
        "feature": extrude.name,
        "body": _body_summary(body),
        "requested_dimensions": {
            "width": width_expression,
            "height": height_expression,
            "depth": depth_expression,
        },
    }


def _delete_root_geometry(root: adsk.fusion.Component) -> dict[str, list[str]]:
    deleted: dict[str, list[str]] = {"features": [], "sketches": [], "bodies": []}

    # Delete parametric owners before their dependent sketches and residual
    # bodies. Iterating backward mirrors Fusion's timeline dependency order.
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


def _find_aluminum_material() -> tuple[adsk.core.Material, str]:
    assert _app is not None
    exact: list[tuple[int, adsk.core.Material, str]] = []
    fallback: list[tuple[adsk.core.Material, str]] = []
    libraries = _app.materialLibraries

    for library_index in range(libraries.count):
        library = libraries.item(library_index)
        materials = library.materials
        for material_index in range(materials.count):
            material = materials.item(material_index)
            name = material.name
            lowered = name.lower()
            source = f"{library.name} / {name}"
            if "6061" in lowered and ("alum" in lowered or "al" in lowered):
                score = 2 if "t6" in lowered else 1
                exact.append((score, material, source))
            elif "aluminum" in lowered or "aluminium" in lowered:
                fallback.append((material, source))

    if exact:
        _, material, source = max(exact, key=lambda candidate: candidate[0])
        return material, source
    if fallback:
        material, source = fallback[0]
        return material, source
    raise ToolFailure("Fusion has no aluminum physical material in its loaded libraries.")


def _inset_triangle(
    points: list[tuple[float, float]],
    inset: float,
) -> list[tuple[float, float]]:
    centroid_x = sum(point[0] for point in points) / 3
    centroid_y = sum(point[1] for point in points) / 3
    side = math.dist(points[0], points[1])
    inradius = side * math.sqrt(3) / 6
    scale = (inradius - inset) / inradius
    if scale <= 0:
        raise ProtocolError("rib_width is too large for cell_size")
    return [
        (
            centroid_x + (x - centroid_x) * scale,
            centroid_y + (y - centroid_y) * scale,
        )
        for x, y in points
    ]


def _create_isogrid_bar(arguments: dict[str, Any]) -> dict[str, Any]:
    assert _app is not None
    design = _active_design()
    document = _app.activeDocument
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
        "cell_size": arguments.get("cell_size", "1.5 in"),
        "rib_width": arguments.get("rib_width", "0.25 in"),
        "skin_thickness": arguments.get("skin_thickness", "0.25 in"),
        "fillet_radius": arguments.get("fillet_radius", "0.04 in"),
    }
    values = {
        field: _expression_cm(
            units, expression, field, allow_zero=(field == "skin_thickness")
        )
        for field, expression in expressions.items()
    }
    if values["skin_thickness"] >= values["thickness"]:
        raise ProtocolError("skin_thickness must be less than thickness")
    if values["cell_size"] >= values["width"]:
        raise ProtocolError("cell_size must be smaller than width")

    deleted = _delete_root_geometry(root)

    base_sketch = root.sketches.add(root.xYConstructionPlane)
    base_sketch.name = "Bar Profile"
    base_sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(-values["length"] / 2, -values["width"] / 2, 0),
        adsk.core.Point3D.create(values["length"] / 2, values["width"] / 2, 0),
    )
    base_feature = root.features.extrudeFeatures.addSimple(
        base_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(expressions["thickness"]),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    base_feature.name = "Bar Extrude"
    body = base_feature.bodies.item(0)
    body.name = name
    unpocketed_volume = body.volume

    # Find the horizontal face at the positive-Z extent, then transform model
    # coordinates into the attached sketch's coordinate system.
    top_face = max(
        (body.faces.item(index) for index in range(body.faces.count)),
        key=lambda face: face.boundingBox.minPoint.z + face.boundingBox.maxPoint.z,
    )
    pocket_sketch = root.sketches.add(top_face)
    pocket_sketch.name = "Isogrid Pocket Profiles"
    lines = pocket_sketch.sketchCurves.sketchLines

    side = values["cell_size"]
    triangle_height = side * math.sqrt(3) / 2
    border = values["rib_width"]
    left = -values["length"] / 2 + border
    right = values["length"] / 2 - border
    bottom = -values["width"] / 2 + border
    top = values["width"] / 2 - border
    inset = values["rib_width"] / 2
    triangles: list[list[tuple[float, float]]] = []

    row = 0
    y0 = bottom
    tolerance = 1e-7
    while y0 + triangle_height <= top + tolerance:
        x0 = left
        while x0 + side <= right + tolerance:
            triangles.append(
                _inset_triangle(
                    [(x0, y0), (x0 + side, y0), (x0 + side / 2, y0 + triangle_height)],
                    inset,
                )
            )
            x0 += side

        # Inverted triangles nest on the vertices shared by adjacent upright
        # triangles, so this row starts at `left`, not half a cell before it.
        # Offsetting by side/2 mirrors each inverted triangle onto an upright
        # one instead of between them, which self-intersects the profiles.
        x0 = left
        while x0 + 1.5 * side <= right + tolerance:
            full = [
                (x0 + side / 2, y0 + triangle_height),
                (x0 + 1.5 * side, y0 + triangle_height),
                (x0 + side, y0),
            ]
            if min(point[0] for point in full) >= left - tolerance:
                triangles.append(_inset_triangle(full, inset))
            x0 += side
        row += 1
        y0 = bottom + row * triangle_height

    if not triangles:
        raise ProtocolError("cell_size is too large to fit any pockets")

    # The lattice fills from the lower-left corner and stops once another cell
    # will not fit, so all of the unused remainder piles up against the right
    # and top edges. Recenter the finished pattern on the bar. The shift is
    # half the remainder, so every pocket stays inside the rib border.
    pattern_x = [x for triangle in triangles for x, _ in triangle]
    pattern_y = [y for triangle in triangles for _, y in triangle]
    shift_x = -(min(pattern_x) + max(pattern_x)) / 2
    shift_y = -(min(pattern_y) + max(pattern_y)) / 2
    triangles = [
        [(x + shift_x, y + shift_y) for x, y in triangle] for triangle in triangles
    ]

    for triangle in triangles:
        sketch_points = [
            pocket_sketch.modelToSketchSpace(
                adsk.core.Point3D.create(x, y, values["thickness"])
            )
            for x, y in triangle
        ]
        for index in range(3):
            lines.addByTwoPoints(sketch_points[index], sketch_points[(index + 1) % 3])

    # Sketching on the bar's top face brings that face's own boundary into
    # profile computation, so Fusion also reports one background profile: the
    # face with every triangle as an inner loop. Cutting that would remove the
    # bar and leave the triangles standing, so keep only the pocket profiles,
    # which are the ones bounded by a single three-curve loop.
    profiles = adsk.core.ObjectCollection.create()
    for profile_index in range(pocket_sketch.profiles.count):
        profile = pocket_sketch.profiles.item(profile_index)
        loops = profile.profileLoops
        if loops.count == 1 and loops.item(0).profileCurves.count == 3:
            profiles.add(profile)

    if profiles.count != len(triangles):
        raise ToolFailure(
            "Isogrid profile validation failed: expected "
            f"{len(triangles)} triangular profiles, got {profiles.count} of "
            f"{pocket_sketch.profiles.count} sketch profiles."
        )

    # A zero skin asks for a fully skeletonized lattice, so the cut has to
    # clear the underside completely. Overshoot the far face rather than
    # landing exactly on it, because a coincident cut face can leave slivers.
    skeletonized = values["skin_thickness"] <= 0
    pocket_depth_cm = values["thickness"] - values["skin_thickness"]
    cut_depth_cm = pocket_depth_cm * 1.05 if skeletonized else pocket_depth_cm
    pocket_feature = root.features.extrudeFeatures.addSimple(
        profiles,
        adsk.core.ValueInput.createByReal(-cut_depth_cm),
        adsk.fusion.FeatureOperations.CutFeatureOperation,
    )
    pocket_feature.name = "Isogrid Pocket Cut"
    pocketed_volume = root.bRepBodies.item(0).volume
    if pocketed_volume >= unpocketed_volume:
        raise ToolFailure("Pocket operation did not reduce the bar volume.")

    body = root.bRepBodies.item(0)
    original_edge_count = body.edges.count
    requested_radius_cm = values["fillet_radius"]
    attempted_radii: list[float] = []
    fillet_feature = None
    used_radius_cm = None
    for scale in (1.0, 0.75, 0.5, 0.25):
        radius_cm = requested_radius_cm * scale
        attempted_radii.append(radius_cm)
        edge_collection = adsk.core.ObjectCollection.create()
        for edge_index in range(body.edges.count):
            edge_collection.add(body.edges.item(edge_index))
        try:
            fillet_input = root.features.filletFeatures.createInput()
            fillet_input.isRollingBallCorner = True
            fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
                edge_collection,
                adsk.core.ValueInput.createByReal(radius_cm),
                True,
            )
            fillet_feature = root.features.filletFeatures.add(fillet_input)
            used_radius_cm = radius_cm
            break
        except Exception as exc:
            _log(f"All-edge fillet attempt at {radius_cm} cm failed: {exc}")

    if not fillet_feature or used_radius_cm is None:
        raise ToolFailure(
            "Fusion could not fillet every edge at any attempted radius: "
            + ", ".join(f"{radius:.4g} cm" for radius in attempted_radii)
        )
    fillet_feature.name = "All Edge Fillet"

    body = root.bRepBodies.item(0)
    material, material_source = _find_aluminum_material()
    body.material = material
    body.name = name
    _app.activeViewport.fit()

    summary = _body_summary(body)
    bounds = summary["bounding_box"]
    measured = {
        "length_cm": bounds["max"]["x_cm"] - bounds["min"]["x_cm"],
        "width_cm": bounds["max"]["y_cm"] - bounds["min"]["y_cm"],
        "thickness_cm": bounds["max"]["z_cm"] - bounds["min"]["z_cm"],
    }
    return {
        "document": document.name,
        "deleted": deleted,
        "requested_dimensions": expressions,
        "measured_dimensions": measured,
        "triangle_pockets": len(triangles),
        "skeletonized": skeletonized,
        "pocket_depth_cm": pocket_depth_cm,
        "volume_removed_cm3": unpocketed_volume - pocketed_volume,
        "original_edges_fillet_requested": original_edge_count,
        "fillet_radius_cm": used_radius_cm,
        "material_source": material_source,
        "body": summary,
    }


def _undo_last(_: dict[str, Any]) -> dict[str, Any]:
    assert _ui is not None
    command = _ui.commandDefinitions.itemById("UndoCommand")
    if not command:
        raise ToolFailure("Fusion's Undo command is unavailable.")
    command.execute()
    return {"undo_requested": True}


TOOL_HANDLERS = {
    "health": _health,
    "active_document": _active_document,
    "design_summary": _design_summary,
    "create_test_box": _create_test_box,
    "replace_with_isogrid_bar": _create_isogrid_bar,
    "undo_last": _undo_last,
}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    tool = request["tool"]
    handler = TOOL_HANDLERS[tool]
    result = handler(request["arguments"])
    return {
        "ok": True,
        "request_id": request.get("request_id"),
        "tool": tool,
        "result": result,
    }


class ExecuteEventHandler(adsk.core.CustomEventHandler):
    def __init__(self) -> None:
        super().__init__()

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        request_id = args.additionalInfo
        with _pending_lock:
            pending = _pending.get(request_id)
        if not pending:
            _log(f"Dropped unknown request id {request_id}")
            return

        try:
            assert _ui is not None
            if _ui.activeCommand != "SelectCommand":
                select = _ui.commandDefinitions.itemById("SelectCommand")
                if select:
                    select.execute()
            pending.response = _dispatch(pending.request)
        except ProtocolError as exc:
            pending.response = _json_error(str(exc), "invalid_arguments")
        except ToolFailure as exc:
            pending.response = _json_error(str(exc), "tool_failure")
        except Exception as exc:
            _log(traceback.format_exc())
            pending.response = _json_error(str(exc), "internal_error")
        finally:
            pending.done.set()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "FusionCadAgent/0.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        _log(format_string % args)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return bool(_token) and secrets_compare(supplied, f"Bearer {_token}")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, _json_error("Unauthorized", "unauthorized"))
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if self.path == "/v1/health":
            self._submit({"tool": "health", "arguments": {}})
            return
        if self.path == "/v1/tools":
            self._send(
                HTTPStatus.OK,
                {"ok": True, "api_version": API_VERSION, "tools": TOOLS},
            )
            return
        self._send(HTTPStatus.NOT_FOUND, _json_error("Not found", "not_found"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if self.path != "/v1/call":
            self._send(HTTPStatus.NOT_FOUND, _json_error("Not found", "not_found"))
            return

        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, _json_error("Invalid Content-Length", "bad_request"))
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _json_error("Invalid request size", "bad_request"))
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            request = validate_request(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, _json_error(str(exc), "bad_request"))
            return
        self._submit(request)

    def _submit(self, request: dict[str, Any]) -> None:
        assert _app is not None
        request_id = request.get("request_id") or str(uuid.uuid4())
        request["request_id"] = request_id
        pending = PendingRequest(request)
        with _pending_lock:
            _pending[request_id] = pending

        try:
            # Autodesk's Python custom-event samples intentionally do not use
            # the wrapper's return value. Some Fusion builds return None even
            # after queueing; the handler response below is authoritative.
            event_result = _app.fireCustomEvent(EVENT_ID, request_id)
            _log(f"Queued request {request_id}; fireCustomEvent={event_result!r}")
            if not pending.done.wait(REQUEST_TIMEOUT_SECONDS):
                self._send(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    _json_error("Fusion did not answer before the timeout", "timeout"),
                )
                return
            response = pending.response or _json_error("Missing response", "internal_error")
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY
            self._send(status, response)
        finally:
            with _pending_lock:
                _pending.pop(request_id, None)


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _start_server() -> None:
    global _server, _server_thread, _token
    _token = load_or_create_token()
    _server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), BridgeRequestHandler)
    selected_port = int(_server.server_address[1])
    _server.daemon_threads = True
    _server_thread = threading.Thread(
        target=_server.serve_forever,
        name="FusionCadAgentServer",
        daemon=True,
    )
    _server_thread.start()
    write_connection_info(
        {
            "api_version": API_VERSION,
            "host": DEFAULT_HOST,
            "port": selected_port,
            "pid": os.getpid(),
            "token_file": str(agent_home() / "token"),
        }
    )
    _log(f"Listening on http://{DEFAULT_HOST}:{selected_port}")


def run(context: Any) -> None:
    del context
    global _app, _ui, _custom_event, _event_handler
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        _custom_event = _app.registerCustomEvent(EVENT_ID)
        if not _custom_event:
            raise RuntimeError(f"Could not register custom event: {EVENT_ID}")
        _event_handler = ExecuteEventHandler()
        if not _custom_event.add(_event_handler):
            raise RuntimeError("Could not attach the Fusion custom-event handler")
        _start_server()
    except Exception:
        _log(traceback.format_exc())
        if _ui:
            _ui.messageBox(
                "Fusion CAD Agent failed to start. Open the Text Commands panel for details."
            )


def stop(context: Any) -> None:
    del context
    global _server, _server_thread, _custom_event, _event_handler
    try:
        if _server:
            _server.shutdown()
            _server.server_close()
        if _server_thread:
            _server_thread.join(timeout=2)
        if _custom_event and _event_handler:
            _custom_event.remove(_event_handler)
        if _app:
            _app.unregisterCustomEvent(EVENT_ID)
        try:
            (agent_home() / "connection.json").unlink()
        except FileNotFoundError:
            pass
        _log("Stopped")
    except Exception:
        _log(traceback.format_exc())
    finally:
        _server = None
        _server_thread = None
        _custom_event = None
        _event_handler = None
