"""Pure-Python protocol helpers shared by Fusion and the CLI."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


API_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
# Ask the OS for an unused localhost port. The selected port is published in
# connection.json, so the CLI never relies on a fixed port being available.
DEFAULT_PORT = 0
MAX_REQUEST_BYTES = 1024 * 1024


TOOLS: dict[str, dict[str, Any]] = {
    "health": {
        "description": "Verify that Fusion's main thread can service agent requests.",
        "mutating": False,
        "arguments": {},
    },
    "active_document": {
        "description": "Return metadata for the active Fusion document.",
        "mutating": False,
        "arguments": {},
    },
    "design_summary": {
        "description": (
            "Summarize the active design, including components, bodies, features, "
            "sketches, bounding boxes, topology counts, volume, and material."
        ),
        "mutating": False,
        "arguments": {},
    },
    "create_test_box": {
        "description": (
            "Create a new unsaved Fusion document containing a centered rectangular "
            "sketch and one extruded test body. Intended only for bridge smoke tests."
        ),
        "mutating": True,
        "arguments": {
            "width": "Fusion unit expression such as '12 in' or '300 mm'.",
            "height": "Fusion unit expression such as '4 in' or '100 mm'.",
            "depth": "Fusion unit expression such as '1 in' or '25 mm'.",
            "name": "Optional body and feature name.",
        },
    },
    "replace_with_isogrid_bar": {
        "description": (
            "Delete geometry in the active root component and replace it with a "
            "rectangular aluminum bar containing triangular isogrid pockets and "
            "fillets on every resulting body edge."
        ),
        "mutating": True,
        "arguments": {
            "length": "Fusion unit expression; default '12 in'.",
            "width": "Fusion unit expression; default '4 in'.",
            "thickness": "Fusion unit expression; default '2 in'.",
            "cell_size": "Nominal triangular cell side; default '1.5 in'.",
            "rib_width": "Gap between adjacent pockets; default '0.25 in'.",
            "skin_thickness": (
                "Uncut bottom skin; default '0.25 in'. Use '0 in' to cut through "
                "and leave a fully skeletonized rib lattice."
            ),
            "fillet_radius": "Maximum requested all-edge fillet; default '0.04 in'.",
            "name": "Optional body name.",
        },
    },
    "undo_last": {
        "description": "Execute Fusion's native Undo command once.",
        "mutating": True,
        "arguments": {},
    },
}


class ProtocolError(ValueError):
    """Raised when a bridge request does not match the protocol."""


def agent_home() -> Path:
    override = os.environ.get("FUSION_CAD_AGENT_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FusionCadAgent"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "FusionCadAgent"
        return Path.home() / "AppData" / "Local" / "FusionCadAgent"
    return Path.home() / ".local" / "share" / "FusionCadAgent"


def ensure_agent_home(path: Path | None = None) -> Path:
    base = path or agent_home()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def load_or_create_token(path: Path | None = None) -> str:
    base = ensure_agent_home(path)
    token_path = base / "token"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeError(f"Refusing short bridge token in {token_path}")
        return token

    token = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(token_path, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token


def write_connection_info(info: dict[str, Any], path: Path | None = None) -> Path:
    base = ensure_agent_home(path)
    destination = base / "connection.json"
    temporary = base / "connection.json.tmp"
    temporary.write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def read_connection_info(path: Path | None = None) -> dict[str, Any]:
    location = (path or agent_home()) / "connection.json"
    try:
        value = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Fusion CAD Agent is not running; connection.json was not found."
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid connection metadata in {location}")
    return value


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("Request body must be a JSON object.")

    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ProtocolError("Request field 'tool' must be a non-empty string.")
    if tool not in TOOLS:
        raise ProtocolError(f"Unknown tool: {tool}")

    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ProtocolError("Request field 'arguments' must be a JSON object.")

    confirm = payload.get("confirm", False)
    if not isinstance(confirm, bool):
        raise ProtocolError("Request field 'confirm' must be a boolean when supplied.")
    if TOOLS[tool]["mutating"] and not confirm:
        raise ProtocolError(f"Mutating tool '{tool}' requires confirm=true.")

    request_id = payload.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ProtocolError("Request field 'request_id' must be a string when supplied.")

    return {
        "tool": tool,
        "arguments": arguments,
        "confirm": confirm,
        "request_id": request_id,
    }
