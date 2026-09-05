#!/usr/bin/env python3
"""MCP server exposing the Fusion CAD bridge.

Lets any MCP client -- Codex, Claude Desktop, Claude Code -- drive Autodesk
Fusion. The client supplies the model and the credentials; this server supplies
the tools. That is the whole point of routing through MCP rather than calling a
model API directly: a subscription-backed client keeps using its own sanctioned
auth, and nothing here ever touches a credential.

Two things are worth knowing:

  * Tool definitions are generated from the bridge's own /v1/tools registry, so
    a tool added to tools.py appears in the client on next launch and cannot
    drift from its implementation.

  * MCP tool results accept image content, so after every mutating call the
    viewport capture is attached to the result that produced it. The model sees
    what it built rather than only reading measurements back.

Speaks JSON-RPC 2.0 over stdio. Standard library only.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bridge_client import Bridge, BridgeError, tool_schemas  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fusion-cad", "version": "1.0.0"}

CAPTURE_WIDTH = 1100
CAPTURE_HEIGHT = 825

def _load_brief() -> str:
    """The shared agent brief, read from AGENTS.md.

    One file rather than a copy per surface: the MCP instructions, the studio's
    system prompt, and Codex's own AGENTS.md pickup all resolve to the same
    text, so guidance cannot drift between clients.
    """
    try:
        # AGENTS.md plus the generated source-map index. The index is what
        # stops a client shelling out to `rg` twenty-five times to discover
        # helpers that were always there.
        import catalog
        return catalog.brief()
    except Exception:
        return ("These tools drive a live Autodesk Fusion session. Dimensions "
                "are unit expression strings such as '12 in'. Read the "
                "verification block on every mutating call, and capture the "
                "viewport to confirm the result.")


INSTRUCTIONS = _load_brief()

BRIDGE = Bridge()


def _log(message: str) -> None:
    # stdout carries the protocol; diagnostics must go to stderr or the
    # client's JSON parser sees garbage and drops the connection.
    print(f"[fusion-mcp] {message}", file=sys.stderr, flush=True)


def _capture(tool: str) -> str | None:
    """Render the viewport and return it base64-encoded."""
    try:
        response = BRIDGE.call("capture_viewport", {
            "views": ["iso"], "width": CAPTURE_WIDTH,
            "height": CAPTURE_HEIGHT, "prefix": f"mcp_{tool}",
        }, confirm=False)
        if not response.get("ok"):
            return None
        path = Path(response["result"]["captures"][0]["path"])
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception as exc:
        _log(f"capture failed: {exc}")
        return None


# ------------------------------------------------------------- handlers ----

def handle_initialize(params: dict) -> dict:
    # Echo the client's protocol version when it names one; a hard-coded
    # version makes the handshake fail against clients on a different release.
    requested = params.get("protocolVersion") or PROTOCOL_VERSION
    _log(f"initialize (client protocol {requested})")
    return {
        "protocolVersion": requested,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": INSTRUCTIONS,
    }


def handle_tools_list(_: dict) -> dict:
    try:
        registry = BRIDGE.registry()
    except BridgeError as exc:
        # Advertising zero tools would look like a working-but-empty server.
        # A single failing tool makes the real problem visible in the client.
        _log(f"registry unavailable: {exc}")
        return {"tools": [{
            "name": "fusion_unavailable",
            "description": f"The Fusion bridge is not reachable: {exc}",
            "inputSchema": {"type": "object", "properties": {}},
        }]}

    tools = []
    for spec in tool_schemas(registry):
        tools.append({
            "name": spec["name"],
            "description": spec["description"],
            "inputSchema": spec["input_schema"],
            "annotations": {
                "readOnlyHint": not spec["mutating"],
                "destructiveHint": spec["mutating"],
            },
        })
    _log(f"tools/list -> {len(tools)} tools")
    return {"tools": tools}


def handle_tools_call(params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}

    if name == "fusion_unavailable":
        return {"content": [{"type": "text",
                             "text": "The Fusion bridge is not running."}],
                "isError": True}

    try:
        registry = BRIDGE.registry()
    except BridgeError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    if name not in registry:
        return {"content": [{"type": "text",
                             "text": f"Unknown tool '{name}'. Available: "
                                     + ", ".join(sorted(registry))}],
                "isError": True}

    mutating = bool(registry[name].get("mutating"))
    try:
        outcome = BRIDGE.call(name, arguments, confirm=mutating)
    except BridgeError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    ok = bool(outcome.get("ok"))
    payload = outcome.get("result") if ok else outcome.get("error")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(payload, indent=2, default=str)[:60000]}
    ]

    # Hand the model a picture of what it just did.
    if ok and mutating and name != "undo_last":
        encoded = _capture(name)
        if encoded:
            content.append({"type": "text", "text": "Viewport after this operation:"})
            content.append({"type": "image", "data": encoded, "mimeType": "image/png"})

    _log(f"tools/call {name} -> {'ok' if ok else 'failed'}")
    return {"content": content, "isError": not ok}


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "ping": lambda _: {},
}


# ---------------------------------------------------------------- loop -----

def respond(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    _log("started; waiting for client")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"unparseable line: {exc}")
            continue

        method = request.get("method", "")
        request_id = request.get("id")

        # Notifications carry no id and must never be answered -- a response
        # to one is a protocol violation some clients treat as fatal.
        if request_id is None:
            _log(f"notification: {method}")
            continue

        handler = HANDLERS.get(method)
        if handler is None:
            respond({"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32601, "message": f"Method not found: {method}"}})
            continue

        try:
            respond({"jsonrpc": "2.0", "id": request_id,
                     "result": handler(request.get("params") or {})})
        except Exception as exc:
            _log(f"handler error in {method}: {exc.__class__.__name__}: {exc}")
            respond({"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32603,
                               "message": f"{exc.__class__.__name__}: {exc}"}})
    _log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
