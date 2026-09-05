"""Autodesk Fusion add-in: localhost-only, authenticated, hot-reloading tool bridge.

This module is the shell and nothing else. It owns the HTTP server, the custom
event registration and the auth token -- the three things that genuinely cannot
be swapped underneath a live socket. Every tool, assertion and pattern lives in
a reloadable module, so editing those and calling the CLI again picks the change
up with no Stop/Run in Fusion.
"""

from __future__ import annotations

import importlib
import json
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

import common as _common

importlib.reload(_common)

from errors import BridgeError, ProtocolError  # noqa: E402
from common import (  # noqa: E402
    API_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_REQUEST_BYTES,
    agent_home,
    load_or_create_token,
    validate_request,
    write_connection_info,
)

EVENT_ID = "FusionCadAgentExecuteEvent"

# A fully skeletonized all-edge fillet can touch thousands of edges. The worker
# thread is only waiting on an Event, so a generous ceiling costs nothing.
REQUEST_TIMEOUT_SECONDS = 300

# Reloaded in dependency order whenever any of them changes on disk.
# errors.py is deliberately NOT in this list: reloading it would rebind the
# exception classes and break isinstance in the handler below.
RELOADABLE = ("geom", "fasteners", "mechanics", "patterns", "assertions",
              "workspace", "pick", "ops", "checkpoints", "tools")

_app: adsk.core.Application | None = None
_ui: adsk.core.UserInterface | None = None
_custom_event: adsk.core.CustomEvent | None = None
_event_handler: adsk.core.CustomEventHandler | None = None
_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_token: str | None = None
_pending: dict[str, "PendingRequest"] = {}
_pending_lock = threading.Lock()
_module_mtimes: dict[str, float] = {}

STATUS_FOR_CODE = {
    "bad_request": HTTPStatus.BAD_REQUEST,
    "unauthorized": HTTPStatus.UNAUTHORIZED,
    "not_found": HTTPStatus.NOT_FOUND,
    "invalid_arguments": HTTPStatus.UNPROCESSABLE_ENTITY,
    "tool_failure": HTTPStatus.UNPROCESSABLE_ENTITY,
    "assertion_failed": HTTPStatus.UNPROCESSABLE_ENTITY,
    "reload_failed": HTTPStatus.INTERNAL_SERVER_ERROR,
    "timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "internal_error": HTTPStatus.INTERNAL_SERVER_ERROR,
}


class PendingRequest:
    def __init__(self, payload: Any):
        self.payload = payload
        self.done = threading.Event()
        self.response: dict[str, Any] | None = None


def _log(message: str) -> None:
    # Called from HTTP worker threads too, where no Fusion API call is safe.
    formatted = f"[FusionCadAgent] {message}"
    try:
        base = agent_home()
        base.mkdir(parents=True, exist_ok=True)
        with (base / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(formatted + "\n")
    except Exception:
        pass
    print(formatted)


def _json_error(message: str, code: str, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    error.update(extra)
    return {"ok": False, "error": error}


# ------------------------------------------------------------ hot reload ----

def _reload_if_changed() -> list[str]:
    """Reload tool modules whose files changed. Main thread only.

    Reloads the whole set in dependency order when any one of them moves,
    because `from x import y` bindings captured inside a sibling module would
    otherwise keep pointing at the previous module object.
    """
    changed: list[str] = []
    for name in RELOADABLE:
        path = ADDIN_DIR / f"{name}.py"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if _module_mtimes.get(name) != mtime:
            changed.append(name)
            _module_mtimes[name] = mtime

    if not changed:
        return []

    for name in RELOADABLE:
        module = sys.modules.get(name)
        if module is None:
            importlib.import_module(name)
        else:
            importlib.reload(module)

    sys.modules["tools"].bind(_app, _ui)
    _log(f"Reloaded {', '.join(changed)}")
    return changed


def _tools_module():
    module = sys.modules.get("tools")
    if module is None:
        module = importlib.import_module("tools")
        module.bind(_app, _ui)
        for name in RELOADABLE:
            try:
                _module_mtimes[name] = (ADDIN_DIR / f"{name}.py").stat().st_mtime
            except OSError:
                pass
    return module


def _dispatch(payload: Any) -> dict[str, Any]:
    """Reload, validate, run. All on Fusion's main thread."""
    reloaded = _reload_if_changed()
    tools = _tools_module()

    if isinstance(payload, dict) and payload.get("meta") == "tools":
        return {
            "ok": True,
            "api_version": API_VERSION,
            "reloaded": reloaded,
            "tools": tools.TOOLS,
        }

    request = validate_request(payload, tools.TOOLS)
    handler = tools.HANDLERS[request["tool"]]
    result = handler(request["arguments"])
    response = {
        "ok": True,
        "request_id": request.get("request_id"),
        "tool": request["tool"],
        "result": result,
    }
    if reloaded:
        response["reloaded"] = reloaded
    return response


class ExecuteEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        request_id = args.additionalInfo
        with _pending_lock:
            pending = _pending.get(request_id)
        if not pending:
            _log(f"Dropped unknown request id {request_id}")
            return

        try:
            if _ui is not None and _ui.activeCommand != "SelectCommand":
                select = _ui.commandDefinitions.itemById("SelectCommand")
                if select:
                    select.execute()
            pending.response = _dispatch(pending.payload)
        except SyntaxError as exc:
            # A typo in a hot-reloaded module. Say so precisely rather than
            # surfacing it as a mystery internal error.
            pending.response = _json_error(
                f"{exc.__class__.__name__} in {exc.filename}:{exc.lineno}: {exc.msg}",
                "reload_failed",
            )
        except BridgeError as exc:
            # One catch for every structured error. errors.py is never
            # reloaded, so isinstance stays reliable across a hot reload.
            pending.response = _json_error(
                str(exc),
                exc.code,
                failed_checks=getattr(exc, "failed_checks", None),
                rolled_back=getattr(exc, "rolled_back", None),
                operation=getattr(exc, "checkpoint_label", None),
                report=getattr(exc, "check_report", None),
            )
        except Exception as exc:
            _log(traceback.format_exc())
            pending.response = _json_error(
                f"{exc.__class__.__name__}: {exc}",
                "internal_error",
                rolled_back=getattr(exc, "rolled_back", None),
                operation=getattr(exc, "checkpoint_label", None),
            )
        finally:
            pending.done.set()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "FusionCadAgent/2.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        _log(format_string % args)

    def _authorized(self) -> bool:
        import hmac

        supplied = self.headers.get("Authorization", "")
        return bool(_token) and hmac.compare_digest(
            supplied.encode("utf-8"), f"Bearer {_token}".encode("utf-8")
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
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
        elif self.path == "/v1/tools":
            self._submit({"meta": "tools"})
        else:
            self._send(HTTPStatus.NOT_FOUND, _json_error("Not found", "not_found"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if self.path != "/v1/call":
            self._send(HTTPStatus.NOT_FOUND, _json_error("Not found", "not_found"))
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, _json_error("Invalid Content-Length", "bad_request"))
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                _json_error("Invalid request size", "bad_request"),
            )
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, _json_error(str(exc), "bad_request"))
            return

        # Validation deliberately happens on the main thread, after the reload,
        # so a tool added seconds ago is not rejected as unknown.
        self._submit(payload)

    def _submit(self, payload: Any) -> None:
        request_id = str(uuid.uuid4())
        if isinstance(payload, dict) and isinstance(payload.get("request_id"), str):
            request_id = payload["request_id"]
        elif isinstance(payload, dict):
            payload["request_id"] = request_id

        pending = PendingRequest(payload)
        with _pending_lock:
            _pending[request_id] = pending
        try:
            _app.fireCustomEvent(EVENT_ID, request_id)
            if not pending.done.wait(REQUEST_TIMEOUT_SECONDS):
                self._send(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    _json_error("Fusion did not answer before the timeout", "timeout"),
                )
                return
            response = pending.response or _json_error("Missing response", "internal_error")
            if response.get("ok"):
                status = HTTPStatus.OK
            else:
                status = STATUS_FOR_CODE.get(
                    response.get("error", {}).get("code", ""),
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            self._send(status, response)
        finally:
            with _pending_lock:
                _pending.pop(request_id, None)


def _start_server() -> None:
    global _server, _server_thread, _token
    _token = load_or_create_token()
    _server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), BridgeRequestHandler)
    selected_port = int(_server.server_address[1])
    _server.daemon_threads = True
    _server_thread = threading.Thread(
        target=_server.serve_forever, name="FusionCadAgentServer", daemon=True
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
        for name in RELOADABLE:
            sys.modules.pop(name, None)
        _module_mtimes.clear()
        # Prime the cache here so the first request does not report every
        # module as freshly reloaded.
        _tools_module()
        _start_server()
    except Exception:
        _log(traceback.format_exc())
        if _ui:
            _ui.messageBox("Fusion CAD Agent failed to start. See bridge.log for details.")


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
