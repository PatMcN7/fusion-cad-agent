#!/usr/bin/env python3
"""CLI client for the local Fusion CAD Agent bridge."""

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
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=310) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(detail)
        except json.JSONDecodeError:
            raise RuntimeError(f"Bridge returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Fusion CAD Agent at {url}: {exc.reason}") from exc


def _call(tool: str, arguments: dict[str, Any] | None = None, *, confirm: bool = False):
    return _request(
        "POST", "/v1/call", {"tool": tool, "arguments": arguments or {}, "confirm": confirm}
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="store_true", help="Print the full JSON response.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("tools")
    sub.add_parser("active-document")
    sub.add_parser("summary")
    sub.add_parser("interference", help="Interference and flipped-joint check.")

    call = sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("--arguments", default="{}")
    call.add_argument("--confirm", action="store_true")

    capture = sub.add_parser("capture", help="Render the viewport to PNG.")
    capture.add_argument("--views", nargs="+", default=["iso"])
    capture.add_argument("--width", type=int, default=1600)
    capture.add_argument("--height", type=int, default=1200)
    capture.add_argument("--visual-style", default="shaded_edges")
    capture.add_argument("--prefix", default="capture")
    capture.add_argument("--no-fit", action="store_true")

    verify = sub.add_parser("verify", help="Run a design spec as a test suite.")
    verify.add_argument("--spec", help="Path to a JSON spec file.")
    verify.add_argument("--envelope", nargs=3, metavar=("L", "W", "H"))
    verify.add_argument("--dimensions", nargs=3, metavar=("L", "W", "H"))
    verify.add_argument("--max-mass-kg", type=float)
    verify.add_argument("--no-interference", action="store_true")

    box = sub.add_parser("create-test-box")
    box.add_argument("--width", default="2 in")
    box.add_argument("--height", default="1 in")
    box.add_argument("--depth", default="0.5 in")
    box.add_argument("--name", default="Agent Test Box")
    box.add_argument("--confirm", action="store_true", required=True)

    undo = sub.add_parser("undo")
    undo.add_argument("--confirm", action="store_true", required=True)

    iso = sub.add_parser("create-isogrid-bar")
    iso.add_argument("--length", default="12 in")
    iso.add_argument("--width", default="4 in")
    iso.add_argument("--thickness", default="2 in")
    iso.add_argument("--cell-size", default="1.0 in")
    iso.add_argument("--rib-width", default="0.1 in")
    iso.add_argument("--skin-thickness", default="0.25 in")
    iso.add_argument("--fillet-radius", default="0.03 in")
    iso.add_argument("--name", default="6061 Aluminum Isogrid Bar")
    iso.add_argument("--confirm", action="store_true", required=True)
    return parser


def _print_checks(report: dict[str, Any], indent: str = "  ") -> None:
    label = report.get("label", "checks")
    status = "PASS" if report.get("ok") else "FAIL"
    print(f"{indent}[{status}] {label}: {report.get('passed', 0)} passed, "
          f"{report.get('failed', 0)} failed, {report.get('warned', 0)} warned")
    for check in report.get("checks", []):
        if check["ok"]:
            mark = "  ok  "
        elif check.get("severity") == "warning":
            mark = " warn "
        else:
            mark = " FAIL "
        line = f"{indent}  [{mark}] {check['name']}"
        if not check["ok"]:
            line += f": got {check['actual']}, expected {check['expected']}"
            if check.get("detail"):
                line += f" ({check['detail']})"
        print(line)


def _summarize(result: dict[str, Any]) -> None:
    verification = result.get("verification")
    if isinstance(verification, dict):
        verification = [verification]
    if isinstance(verification, list):
        print("verification:")
        for report in verification:
            _print_checks(report)


def main() -> int:
    arguments = _parser().parse_args()
    command = arguments.command

    if command == "health":
        response = _request("GET", "/v1/health")
    elif command == "tools":
        response = _request("GET", "/v1/tools")
    elif command == "active-document":
        response = _call("active_document")
    elif command == "summary":
        response = _call("design_summary")
    elif command == "interference":
        response = _call("check_interference")
    elif command == "capture":
        response = _call(
            "capture_viewport",
            {
                "views": arguments.views,
                "width": arguments.width,
                "height": arguments.height,
                "visual_style": arguments.visual_style,
                "fit": not arguments.no_fit,
                "prefix": arguments.prefix,
            },
        )
    elif command == "call":
        decoded = json.loads(arguments.arguments)
        if not isinstance(decoded, dict):
            raise RuntimeError("--arguments must decode to a JSON object")
        response = _call(arguments.tool, decoded, confirm=arguments.confirm)
    elif command == "verify":
        spec: dict[str, Any] = {}
        if arguments.spec:
            spec = json.loads(open(arguments.spec, encoding="utf-8").read())
        if arguments.envelope:
            spec["envelope"] = arguments.envelope
        if arguments.dimensions:
            spec["dimensions"] = arguments.dimensions
        if arguments.max_mass_kg is not None:
            spec["max_mass_kg"] = arguments.max_mass_kg
        if arguments.no_interference:
            spec["require_no_interference"] = True
        response = _call("verify_spec", spec)
    elif command == "create-test-box":
        response = _call(
            "create_test_box",
            {
                "width": arguments.width,
                "height": arguments.height,
                "depth": arguments.depth,
                "name": arguments.name,
            },
            confirm=True,
        )
    elif command == "undo":
        response = _call("undo_last", confirm=True)
    elif command == "create-isogrid-bar":
        response = _call(
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
        raise AssertionError(command)

    if arguments.raw:
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0 if response.get("ok") else 1

    if response.get("reloaded"):
        print(f"hot-reloaded: {', '.join(response['reloaded'])}")

    if not response.get("ok"):
        error = response.get("error", {})
        print(f"error [{error.get('code')}]: {error.get('message')}", file=sys.stderr)
        if error.get("operation"):
            print(f"  failed operation: {error['operation']}", file=sys.stderr)
        if error.get("rolled_back"):
            print(f"  rolled back: {', '.join(error['rolled_back'])}", file=sys.stderr)
        for check in error.get("failed_checks", []):
            print(f"  FAIL {check['name']}: got {check['actual']}, "
                  f"expected {check['expected']}", file=sys.stderr)
        return 1

    result = response.get("result", response)
    if response.get("tool") in {"check_interference", "verify_spec"} or "label" in result:
        _print_checks(result, indent="")
        return 0 if result.get("ok", True) else 1

    if isinstance(result, dict) and "verification" in result:
        printable = {k: v for k, v in result.items() if k != "verification"}
        print(json.dumps(printable, indent=2, sort_keys=True))
        _summarize(result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
