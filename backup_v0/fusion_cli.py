#!/usr/bin/env python3
"""CLI client for the local Fusion CAD Agent add-in."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from common import load_or_create_token, read_connection_info


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    connection = read_connection_info()
    token = load_or_create_token()
    host = connection.get("host", "127.0.0.1")
    port = int(connection["port"])
    url = f"http://{host}:{port}{path}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=310) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bridge returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Fusion CAD Agent at {url}: {exc.reason}") from exc


def _call(
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    return _request(
        "POST",
        "/v1/call",
        {"tool": tool, "arguments": arguments or {}, "confirm": confirm},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check the bridge and Fusion main thread.")
    subcommands.add_parser("tools", help="List available bridge tools.")
    subcommands.add_parser("active-document", help="Inspect the active document.")
    subcommands.add_parser("summary", help="Inspect the active Fusion design.")

    call = subcommands.add_parser("call", help="Call a bridge tool by name.")
    call.add_argument("tool")
    call.add_argument("--arguments", default="{}", help="JSON object of tool arguments.")
    call.add_argument(
        "--confirm",
        action="store_true",
        help="Required by the bridge when calling a mutating tool.",
    )

    box = subcommands.add_parser("create-test-box", help="Create an unsaved smoke-test design.")
    box.add_argument("--width", default="2 in")
    box.add_argument("--height", default="1 in")
    box.add_argument("--depth", default="0.5 in")
    box.add_argument("--name", default="Agent Test Box")
    box.add_argument(
        "--confirm",
        action="store_true",
        help="Required because this command creates geometry in Fusion.",
    )

    undo = subcommands.add_parser("undo", help="Run Fusion Undo once.")
    undo.add_argument(
        "--confirm",
        action="store_true",
        help="Required because this command changes the active design.",
    )

    isogrid = subcommands.add_parser(
        "create-isogrid-bar",
        help="Replace the active root geometry with a filleted aluminum isogrid bar.",
    )
    isogrid.add_argument("--length", default="12 in")
    isogrid.add_argument("--width", default="4 in")
    isogrid.add_argument("--thickness", default="2 in")
    isogrid.add_argument("--cell-size", default="1.5 in")
    isogrid.add_argument("--rib-width", default="0.25 in")
    isogrid.add_argument("--skin-thickness", default="0.25 in")
    isogrid.add_argument("--fillet-radius", default="0.04 in")
    isogrid.add_argument("--name", default="6061 Aluminum Isogrid Bar")
    isogrid.add_argument("--confirm", action="store_true", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "health":
        result = _request("GET", "/v1/health")
    elif arguments.command == "tools":
        result = _request("GET", "/v1/tools")
    elif arguments.command == "active-document":
        result = _call("active_document")
    elif arguments.command == "summary":
        result = _call("design_summary")
    elif arguments.command == "call":
        decoded = json.loads(arguments.arguments)
        if not isinstance(decoded, dict):
            raise RuntimeError("--arguments must decode to a JSON object")
        result = _call(arguments.tool, decoded, confirm=arguments.confirm)
    elif arguments.command == "create-test-box":
        if not arguments.confirm:
            raise RuntimeError("Refusing to create geometry without --confirm")
        result = _call(
            "create_test_box",
            {
                "width": arguments.width,
                "height": arguments.height,
                "depth": arguments.depth,
                "name": arguments.name,
            },
            confirm=True,
        )
    elif arguments.command == "undo":
        if not arguments.confirm:
            raise RuntimeError("Refusing to modify the design without --confirm")
        result = _call("undo_last", confirm=True)
    elif arguments.command == "create-isogrid-bar":
        result = _call(
            "replace_with_isogrid_bar",
            {
                "length": arguments.length,
                "width": arguments.width,
                "thickness": arguments.thickness,
                "cell_size": arguments.cell_size,
                "rib_width": arguments.rib_width,
                "skin_thickness": arguments.skin_thickness,
                "fillet_radius": arguments.fillet_radius,
                "name": arguments.name,
            },
            confirm=True,
        )
    else:
        raise AssertionError(arguments.command)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
