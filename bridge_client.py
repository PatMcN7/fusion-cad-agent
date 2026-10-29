"""Shared client for the Fusion add-in's localhost API.

Both front ends -- the studio web app and the MCP server -- talk to the bridge
through this module, so the connection handling and the schema inference exist
once rather than drifting in two places.
"""

from __future__ import annotations

import json
import copy
import uuid
import urllib.error
import urllib.request
from typing import Any

from common import load_or_create_token, read_connection_info
from execution import operation_id as validate_operation_id
from errors import ProtocolError
from errors import ArgumentError
from tool_contracts import validate, version
from design_state import DesignState, TOOLS as DESIGN_TOOLS

REQUEST_TIMEOUT = 310
CONTROL_TOOLS = {
    "operation_status": {
        "description": "Read a queued/running/completed operation without waiting for Fusion's main thread. Use after a timeout; do not repeat the mutation.",
        "mutating": False,
        "arguments": {"operation_id": "Operation ID returned by the mutation."},
    },
    "cancel_operation": {
        "description": "Cancel a queued operation. Cannot interrupt a kernel operation already running. Returns current status.",
        "mutating": True,
        "arguments": {"operation_id": "ID of the queued operation to cancel."},
    },
}


class BridgeError(RuntimeError):
    """The bridge could not be reached or returned a transport-level failure."""


class Bridge:
    def __init__(self):
        self._registry = None
        self._unresolved = None
        self._document_id = None
        self._submitted = {}
        self.capability_version = None
        self.design_state = DesignState()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            connection = read_connection_info()
            token = load_or_create_token()
        except RuntimeError as exc:
            raise BridgeError(str(exc)) from exc

        host = connection.get("host", "127.0.0.1")
        url = f"http://{host}:{int(connection['port'])}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                # The bridge reports tool failures as structured JSON with a
                # non-2xx status; those are results, not transport errors.
                return json.loads(detail)
            except json.JSONDecodeError:
                raise BridgeError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BridgeError(
                f"Fusion bridge unreachable at {url}: {getattr(exc, 'reason', exc)}. "
                "Is the FusionCadAgent add-in running?") from exc

    def registry(self) -> dict[str, Any]:
        if self._unresolved and self._registry is not None:
            return dict(self._registry)
        response = self._request("GET", "/v1/tools")
        if not response.get("ok"):
            raise BridgeError(str(response.get("error", {}).get("message", "Registry unavailable")))
        if self._document_id is None:
            self._document_id = response.get("document_id")
        self._registry = {**response.get("tools", {}), **CONTROL_TOOLS, **DESIGN_TOOLS}
        self.capability_version = version(self._registry)
        return dict(self._registry)

    def operation_status(self, identifier: str) -> dict:
        try:
            validate_operation_id(identifier)
        except ProtocolError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        response = self._request("GET", f"/v1/operations/{identifier}")
        if response.get("ok"):
            operation = response["operation"]
            if operation["state"] in {"succeeded", "failed"} and self._unresolved == identifier:
                self._unresolved = None
            return {"ok": True, "result": operation}
        return response

    def cancel_operation(self, identifier: str) -> dict:
        try:
            validate_operation_id(identifier)
        except ProtocolError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        response = self._request("POST", f"/v1/operations/{identifier}/cancel")
        if response.get("ok"):
            if response.get("cancelled") and self._unresolved == identifier:
                self._unresolved = None
            return {"ok": True, "result": {"cancelled": response.get("cancelled", False),
                                           "operation": response["operation"]}}
        return response

    def call(self, tool: str, arguments: dict, confirm: bool,
             *, operation_id: str | None = None, document_id: str | None = None) -> dict:
        if tool == "update_design_state":
            try:
                return {"ok": True, "result": self.design_state.update(arguments)}
            except ArgumentError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        if tool == "operation_status":
            return self.operation_status(arguments.get("operation_id"))
        if tool == "cancel_operation":
            if not confirm:
                return {"ok": False, "error": {"code": "bad_request", "message": "Cancellation requires confirm=true"}}
            return self.cancel_operation(arguments.get("operation_id"))
        arguments = dict(arguments)
        argument_document = arguments.pop("document_id", None) if confirm else None
        if document_id is not None and argument_document is not None and document_id != argument_document:
            return {"ok": False, "error": {"code": "bad_request", "message": "Conflicting document IDs"}}
        document_id = document_id if document_id is not None else argument_document
        argument_id = arguments.pop("operation_id", None) if confirm else None
        if operation_id is not None and argument_id is not None and operation_id != argument_id:
            return {"ok": False, "error": {"code": "bad_request", "message": "Conflicting operation IDs"}}
        identifier = operation_id if operation_id is not None else argument_id
        spec = (self._registry or {}).get(tool, {})
        if spec.get("input_schema"):
            try:
                validate(arguments, spec["input_schema"])
            except ArgumentError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": str(exc),
                        "context": exc.context, "execution_started": False}}
        if confirm:
            if identifier is None:
                identifier = uuid.uuid4().hex
            try:
                validate_operation_id(identifier)
            except ProtocolError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
            if self._unresolved and identifier != self._unresolved:
                return {"ok": False, "error": {"code": "outcome_unknown",
                        "operation_id": self._unresolved,
                        "message": "Resolve the previous operation with operation_status before another mutation"}}
        payload = {"tool": tool, "arguments": arguments, "confirm": confirm}
        if identifier:
            payload["operation_id"] = identifier
        if confirm:
            previous = self._submitted.get(identifier)
            if previous is not None:
                if (previous["tool"] != tool or previous["arguments"] != arguments or
                        (document_id is not None and document_id != previous["document_id"])):
                    return {"ok": False, "error": {"code": "bad_request",
                            "message": "operation_id already belongs to another request"}}
                payload = previous
            else:
                if document_id is None and self._document_id is None:
                    self.registry()
                payload["document_id"] = document_id if document_id is not None else self._document_id
                if not payload["document_id"]:
                    return {"ok": False, "error": {"code": "protocol_upgrade_required",
                            "message": "Restart the Fusion add-in to load protocol v4 document binding"}}
                # Freeze nested argument structures, so later caller edits cannot
                # change the meaning of a transport retry.
                try:
                    payload = json.loads(json.dumps(payload, allow_nan=False))
                except (ValueError, TypeError) as exc:
                    return {"ok": False, "error": {"code": "bad_request",
                            "message": f"Operation arguments must be finite JSON: {exc}"}}
                self._submitted[identifier] = payload
        if confirm and tool not in {"write_file", "patch_file", "save_document", "cancel_operation"}:
            # A failed mutation can leave partial geometry; invalidate conservatively.
            self.design_state.invalidate(self._document_id)
        try:
            response = self._request("POST", "/v1/call", payload)
        except BridgeError as exc:
            if not confirm:
                raise
            self._unresolved = identifier
            return {"ok": False, "operation_id": identifier,
                    "error": {"code": "outcome_unknown", "operation_id": identifier,
                              "message": str(exc) + "; poll operation_status before retrying"}}
        if response.get("document_id") and isinstance(response.get("result"), dict):
            response["result"].setdefault("document_id", response["document_id"])
        if response.get("ok") and not response.get("replayed") and (confirm or tool in {"active_document", "design_summary", "list_documents", "get_selected_faces", "resolve_selected_faces"}):
            self._document_id = response.get("document_id", self._document_id)
        if confirm:
            response["operation_id"] = identifier
            target = response.get("result") if response.get("ok") else response.get("error")
            if isinstance(target, dict):
                target["operation_id"] = identifier
                if response.get("replayed"):
                    target["replayed"] = True
            if response.get("error", {}).get("code") in {"operation_pending", "outcome_unknown", "timeout"}:
                self._unresolved = identifier
            elif self._unresolved == identifier:
                self._unresolved = None
        if response.get("ok") and isinstance(response.get("result"), dict):
            response["result"] = self.design_state.observe(tool, arguments,
                response["result"], response.get("document_id", self._document_id))
        return response

    def health(self) -> dict:
        return self._request("GET", "/v1/health")


def property_schema(description: Any) -> dict[str, Any]:
    """Infer a JSON type from the registry's prose description.

    The bridge documents arguments in English rather than JSON Schema, so the
    type is read from the conventions it uses ("default 3", "default true",
    "[l, w, h]"). Inference stays conservative and falls back to string: a
    wrong type is worse than a loose one, because it makes a correct value
    fail validation before it is ever sent.
    """
    # A tool may supply an explicit JSON Schema instead of prose when the
    # argument is structured -- nested arrays of coordinates cannot be
    # described well enough in English for a model to get right first time.
    if isinstance(description, dict):
        return dict(description)

    lowered = description.lower()
    schema: dict[str, Any] = {"description": description}
    if "default true" in lowered or "default false" in lowered:
        schema["type"] = "boolean"
    # String-list conventions. "non-empty list of ..." must be caught here
    # before the fallthrough: it does not start with "list ", so previously it
    # was inferred as a string and arrays were rejected by schema validation
    # before the tool ever saw them (delete_named_features).
    elif ("[l, w, h]" in lowered or "list of three" in lowered
          or lowered.startswith("list ")
          or "list of " in lowered):
        schema["type"] = "array"
        schema["items"] = {"type": "string"}
    elif "default '" in lowered or "unit expression" in lowered or "e.g." in lowered:
        schema["type"] = "string"
    elif "default " in lowered and any(
            c.isdigit() for c in lowered.split("default ", 1)[1][:6]):
        schema["type"] = "number"
    else:
        schema["type"] = "string"
    return schema


def tool_schemas(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Registry -> JSON Schema tool definitions, generated not hand-written."""
    tools = []
    for name, spec in sorted(registry.items()):
        schema = copy.deepcopy(spec.get("input_schema"))
        properties = schema["properties"] if schema else {
            key: property_schema(text if isinstance(text, dict) else str(text))
            for key, text in (spec.get("arguments") or {}).items()
        }
        if spec.get("mutating") and name not in CONTROL_TOOLS:
            properties["document_id"] = {
                "type": "string", "description": "Optional explicit document binding. Normally supplied from the client's last observation. Never guess this ID."
            }
            properties["operation_id"] = {
                "type": "string", "description": "Optional retry identity. Reuse only for exactly the same operation after checking its status. Normally generated by the client."
            }
        description = spec.get("description", "")
        if spec.get("mutating"):
            description += (" (Changes queued operation state.)" if name in CONTROL_TOOLS
                            else " (Modifies the design; inspect verification and recovery status.)")
        tools.append({
            "name": name,
            "description": description,
            "mutating": bool(spec.get("mutating")),
            "input_schema": {**(schema or {"type": "object"}), "properties": properties},
        })
    return tools
