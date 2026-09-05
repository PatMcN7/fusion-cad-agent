"""Shared client for the Fusion add-in's localhost API.

Both front ends -- the studio web app and the MCP server -- talk to the bridge
through this module, so the connection handling and the schema inference exist
once rather than drifting in two places.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from common import load_or_create_token, read_connection_info

REQUEST_TIMEOUT = 310


class BridgeError(RuntimeError):
    """The bridge could not be reached or returned a transport-level failure."""


class Bridge:
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
        except urllib.error.URLError as exc:
            raise BridgeError(
                f"Fusion bridge unreachable at {url}: {exc.reason}. "
                "Is the FusionCadAgent add-in running?") from exc

    def registry(self) -> dict[str, Any]:
        return self._request("GET", "/v1/tools").get("tools", {})

    def call(self, tool: str, arguments: dict, confirm: bool) -> dict:
        return self._request("POST", "/v1/call",
                             {"tool": tool, "arguments": arguments, "confirm": confirm})

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
    elif "[l, w, h]" in lowered or "list of three" in lowered or lowered.startswith("list "):
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
        properties = {
            key: property_schema(text if isinstance(text, dict) else str(text))
            for key, text in (spec.get("arguments") or {}).items()
        }
        description = spec.get("description", "")
        if spec.get("mutating"):
            description += " (Modifies the design; verified and checkpointed.)"
        tools.append({
            "name": name,
            "description": description,
            "mutating": bool(spec.get("mutating")),
            "input_schema": {"type": "object", "properties": properties},
        })
    return tools
