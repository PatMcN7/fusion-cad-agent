#!/usr/bin/env python3
"""Fusion Studio -- a chat + sketch interface to the Fusion CAD bridge.

The loop:

    describe intent (in words, or by drawing on a screenshot of the model)
        -> the agent asks the questions that actually change the geometry
        -> it builds through the bridge, verified operation by operation
        -> the harness captures the viewport and hands the picture back
        -> you say what is wrong, and it continues

Three design notes:

1. Tool schemas are generated from the bridge's own /v1/tools registry.
   Nothing is hand-written, so a tool added to tools.py appears here on the
   next page load and cannot drift from its implementation.

2. The design record pins requirements, dependencies and evidence outside the
   trimmed message history. Targeted visual checks resolve uncertainty, with
   a final viewport review after modeling.

3. The model backend is pluggable (see providers/). The agent loop below is
   written against the neutral protocol and never branches on vendor.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import traceback
import uuid
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))


import envfile  # noqa: E402

ENV_LOADED = envfile.load(HERE / ".env")


import providers  # noqa: E402
from providers import (Cancelled, Provider, ProviderError, ToolResult,  # noqa: E402
                       ToolSpec)

from fusion_cad.design_state import DesignState
from fusion_cad.common import agent_home, load_or_create_token, read_connection_info  # noqa: E402

# Captures are fed back on every mutating call, so keep them modest -- a
# full-resolution frame can cost several thousand tokens each time.
CAPTURE_WIDTH = 1100
CAPTURE_HEIGHT = 825
# A fetched PDF can render to many pages; looking at all of them would cost
# more than the drawing is worth.
MAX_TOOL_IMAGES = 4

def _load_brief() -> str:
    """The shared agent brief, read from AGENTS.md at the repo root.

    Codex picks this file up on its own; the other backends get it as their
    system prompt. Keeping one file means a change reaches every client.
    """
    from agent_runtime import PROMPT
    return PROMPT


SYSTEM_PROMPT = _load_brief()

# The vision role. Captures are summarized by a model chosen for looking at
# pictures, so the loop model never has to be one.
import vision  # noqa: E402

DESCRIBER, VISION_MODE = vision.from_environment()

# Every session is recorded to JSONL unless switched off. The studio kept no
# record of its own runs, which meant the only sessions that could be taken
# apart afterwards were the ones driven by some other tool that did.
import transcript  # noqa: E402

# One transcript per chat rather than per process: two chats interleaved in
# one file cannot be analysed apart, and separating them is the whole point
# of having them.
TRANSCRIPTS_OFF = os.environ.get("STUDIO_TRANSCRIPT", "").lower() in ("0", "off", "false")


# --------------------------------------------------------------- bridge ----

from fusion_cad.bridge_client import Bridge, BridgeError, tool_schemas  # noqa: E402
from fusion_cad.bridge_client import property_schema as _property_schema  # noqa: E402
import agent_runtime
import face_attachments
from fusion_cad.tool_contracts import validate as validate_arguments
from fusion_cad.errors import ArgumentError


def tool_specs(registry: dict[str, Any]) -> list[ToolSpec]:
    """Bridge registry -> neutral tool specs, using the shared schema generator."""
    return [ToolSpec(name=spec["name"], description=spec["description"],
                     properties=spec["input_schema"]["properties"],
                     required=spec["input_schema"].get("required", []),
                     additional_properties=spec["input_schema"].get("additionalProperties", True))
            for spec in tool_schemas(registry)]


# ---------------------------------------------------------------- state ----

class Session:
    """One conversation: its own provider, its own history, its own transcript.

    Chats are separate for the same reason branches are. A CAD session builds
    state in the model -- what body it just cut, which occurrence it mated --
    and interleaving two lines of work in one history makes both worse. It
    also means an experiment can be abandoned without poisoning the thread
    that was going well.

    The provider owns the native message history; `log` is the parallel
    record the browser renders, so reopening a chat shows what happened
    rather than an empty box.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.bridge = Bridge()  # Each chat retains its own observed document binding.
        self.bridge.design_state = DesignState(agent_home() / "design-states" / (self.id + ".json"))
        self.bridge.design_state.save()
        self.provider: Provider | None = None
        self.provider_name = os.environ.get("STUDIO_PROVIDER", "anthropic")
        self.model = os.environ.get("STUDIO_MODEL", "claude-opus-5")
        self.base_url = os.environ.get("STUDIO_BASE_URL") or None
        # Keep execution economical. Open-ended tasks use a bounded design
        # record and targeted evidence gathering; effort remains user-selectable.
        self.effort = os.environ.get("STUDIO_EFFORT", "low")
        self.error: str | None = None
        self.title = "New chat"
        self.created = time.time()
        self.updated = self.created
        self.log: list[dict[str, Any]] = []
        self.busy = False
        self.stop = threading.Event()
        self.cost = 0.0
        self.recorder = None if TRANSCRIPTS_OFF else transcript.Recorder(
            worker_provider=self.provider_name, worker_model=self.model,
            vision_provider=DESCRIBER.provider_name if DESCRIBER else None,
            vision_model=DESCRIBER.model if DESCRIBER else None,
            vision_mode=VISION_MODE, effort=self.effort, chat=self.id)

    # ---- history the browser renders ------------------------------------

    def record(self, kind: str, data: Any) -> None:
        """Append one renderable entry. Streamed text folds into the last one."""
        self.updated = time.time()
        if kind in {"text", "thinking"} and self.log and self.log[-1]["kind"] == kind:
            self.log[-1]["data"]["text"] += data.get("text", "")
            return
        if kind == "usage" and isinstance(data, dict):
            if data.get("session_cost_usd") is not None:
                self.cost = float(data["session_cost_usd"])
            elif data.get("cost_usd"):
                self.cost += float(data["cost_usd"])
        self.log.append({"kind": kind, "data": dict(data) if isinstance(data,dict) else data})
        # A long chat is bounded so that reopening it stays fast; the full
        # record is in the transcript, which is never trimmed.
        if len(self.log) > 800:
            del self.log[:200]

    def name_from(self, text: str) -> None:
        if self.title != "New chat" or not text.strip():
            return
        first = " ".join(text.split())
        self.title = first[:48] + ("…" if len(first) > 48 else "")

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "created": self.created,
                "updated": self.updated, "model": self.model,
                "provider": self.provider_name, "busy": self.busy,
                "messages": sum(1 for e in self.log if e["kind"] == "user"),
                "cost": round(self.cost, 5), "design_state": self.bridge.design_state.snapshot()}

    # ---- backend ---------------------------------------------------------

    def configure(self, provider: str, model: str, base_url: str | None,
                  effort: str) -> None:
        """Rebuild the backend. History does not survive: provider-native
        state (thinking blocks, call ids) cannot be transplanted."""
        with self.lock:
            self.provider_name, self.model = provider, model
            self.base_url, self.effort = base_url, effort
            try:
                self.provider = providers.build(
                    provider, model, base_url=base_url, effort=effort)
                self.error = None
            except ProviderError as exc:
                self.provider, self.error = None, str(exc)
            except Exception as exc:  # defence in depth; build() normalizes
                self.provider = None
                self.error = f"{exc.__class__.__name__}: {exc}"

    def ensure(self) -> Provider:
        with self.lock:
            if self.provider is None:
                # Retry each time rather than caching the failure: the usual
                # fix is exporting a key, and the user should not have to
                # restart the server for that to take effect.
                try:
                    self.provider = providers.build(
                        self.provider_name, self.model,
                        base_url=self.base_url, effort=self.effort)
                    self.error = None
                except ProviderError as exc:
                    self.error = str(exc)
            if self.provider is None:
                raise ProviderError(self.error or "no model backend configured")
            return self.provider


class SessionStore:
    """Every open chat, newest first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session = Session()
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> Session:
        """Resolve an id, falling back to the newest chat, creating if none.

        A browser that reloads after a server restart holds a stale id. That
        should open a working chat, not an error.
        """
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            if (session_id and len(session_id) == 12 and
                    all(c in "0123456789abcdef" for c in session_id) and
                    (agent_home() / "design-states" / (session_id + ".json")).is_file()):
                session = Session(session_id)
                if session.bridge.design_state.entries:
                    session.title = "Recovered design record"
                    session.record("text", {"text": "Recovered the design record with fresh model context. Geometry evidence needs rechecking; previous chat messages are in the transcript archive."})
                self._sessions[session_id] = session
                return session
            if self._sessions:
                return max(self._sessions.values(), key=lambda s: s.updated)
        return self.create()

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session and session.recorder:
            session.recorder.close(reason="chat deleted")
        if session and session.bridge.design_state.path:
            session.bridge.design_state.path.unlink(missing_ok=True)
        return session is not None

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.summary() for s in sorted(sessions, key=lambda s: -s.updated)]


SESSIONS = SessionStore()
BRIDGE = Bridge()


# ---------------------------------------------------------------- agent ----

class Agent:
    def __init__(self, bridge: Bridge, provider: Provider) -> None:
        self.bridge = bridge
        self.provider = provider
        self.observe = lambda event, data: None
        self.expected_stage = ""
        self.stopped: Callable[[], bool] = lambda: False

    def _capture(self, prefix: str) -> tuple[str, str] | None:
        try:
            response = self.bridge.call("capture_viewport", {
                "views": ["top"], "clean": True, "width": CAPTURE_WIDTH,
                "height": CAPTURE_HEIGHT, "prefix": prefix,
            }, confirm=False)
            if not response.get("ok"):
                return None
            path = Path(response["result"]["captures"][0]["path"])
            return str(path), base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception:
            return None

    def _look(self, result: ToolResult, encoded: str,
              requested: dict | None = None) -> str | None:
        """Show images directly to a capable worker; describe only as fallback."""
        if getattr(self.provider,"supports_images",True):
            result.images.append({"data":encoded,"media_type":"image/png"})
            return None
        note = f"This is the model immediately after `{result.name}` ran."
        if requested:
            note += ("\n\nIt was called with these arguments:\n"
                     + json.dumps(requested, indent=2, default=str)[:800]
                     + "\n\nSay whether what you see matches them. If a count, "
                       "size or placement looks different from what was asked "
                       "for, lead with that.")
        if DESCRIBER is None:
            raise ProviderError("This worker cannot see images and no vision fallback is configured.")
        description = DESCRIBER.describe(encoded, note, emit=self.observe)
        return description

    def _look_at_file(self, result: ToolResult, path: str) -> str | None:
        """Show the model an image a tool produced, however it can see it."""
        try:
            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            return None
        name = Path(path).name
        if getattr(self.provider,"supports_images",True):
            result.images.append({"data":encoded,"media_type":"image/jpeg" if Path(path).suffix.lower() in {".jpg",".jpeg"} else "image/png","label":Path(path).name})
            return None
        if DESCRIBER is None:
            raise ProviderError("This worker cannot see images and no vision fallback is configured.")
        text = DESCRIBER.describe(
            encoded,
            f"This image came back from `{result.name}` as {name}. It may be an "
            "engineering drawing rather than a CAD viewport. Report the "
            "dimensions, hole patterns and callouts you can actually read. "
            + "Expected completed stage: " + self.expected_stage, emit=self.observe)
        # Append rather than replace: one tool call can return several pages.
        result.description = ((result.description or "")
                              + f"\n\n[{name}]\n{text}").strip()
        return text

    def run(self, blocks: list[dict], emit: Callable[[str, Any], None],
            stopped: Callable[[], bool] = lambda: False) -> None:
        self.stopped, self.observe = stopped, emit
        started = time.monotonic()
        emit("turn_start", {})
        self.bridge.enabled_groups = {"core"}  # Start each user task with the small toolset.
        if not hasattr(self.bridge, "result_cache"): self.bridge.result_cache = {}
        design_state = getattr(self.bridge, "design_state", None)
        if design_state is not None:
            # Between user turns the person or another client may have edited Fusion.
            design_state.invalidate()
            emit("design_state", design_state.snapshot())
        repeated = {}
        read_loop = agent_runtime.ReadLoopGuard()
        consecutive_failures = 0
        dirty = False
        final_checked = False
        try:
            registry = self.bridge.registry()
            self.provider.add_user(blocks)
            while not self.stopped():
                prepare = getattr(self.provider, "prepare_runtime", None)
                if prepare is not None:
                    prepare(registry=registry,
                            design_state=design_state.snapshot() if design_state is not None else None,
                            document_id=getattr(self.bridge, "_document_id", None),
                            unresolved_operation=getattr(self.bridge, "_unresolved", None),
                            stopped=self.stopped)
                selected = agent_runtime.select(registry, self.bridge.enabled_groups)
                specs = tool_specs(selected)
                emit("tools", {"count": len(specs), "names": sorted(selected),
                               "capability_version": getattr(self.bridge, "capability_version", None)})
                model_start, first = time.monotonic(), None
                pending_text = []
                def observe(event, data):
                    nonlocal first
                    if first is None and event in {"text", "thinking", "tool_pending"}:
                        first = time.monotonic() - model_start
                    if event == "text": pending_text.append(dict(data))
                    else: emit(event, data)
                prompt = SYSTEM_PROMPT + "\n" + agent_runtime.guidance(self.bridge.enabled_groups)
                if design_state is not None:
                    prompt += design_state.prompt()
                turn = self.provider.stream(prompt, specs, observe)
                emit("provider_timing", {"role": getattr(self.provider, "timing_role", "worker"), "seconds": time.monotonic()-model_start,
                                         "first_token_seconds": first})
                if turn.usage: emit("usage", turn.usage)
                if getattr(self.provider, "is_agent", False):
                    for data in pending_text: emit("text", data)
                    break
                if turn.stop_reason == "length":
                    for data in pending_text: emit("text", data)
                    # A provider's own output boundary is not the end of the task.
                    # Never execute calls from a response truncated mid-generation.
                    if turn.tool_calls:
                        self.provider.add_tool_results([ToolResult(call.id,call.name,False,
                            {"code":"incomplete_response","execution_started":False,
                             "message":"Provider response truncated; this call was not executed. Reissue the complete call."}) for call in turn.tool_calls])
                    emit("progress_warning", {"message":"Provider output boundary reached; continuing automatically. No tool calls from the incomplete response were executed."})
                    self.provider.add_user([{"type":"text","text":"Your provider ended the previous response at its output boundary. Continue the same task from the available evidence. Calls in that incomplete response were not executed; reissue any needed complete calls."}])
                    continue
                if turn.stop_reason == "refusal":
                    for data in pending_text: emit("text", data)
                    if not pending_text and turn.text: emit("text", {"text":turn.text})
                    if turn.tool_calls:
                        self.provider.add_tool_results([ToolResult(call.id,call.name,False,
                            {"code":"provider_refusal","execution_started":False}) for call in turn.tool_calls])
                    emit("error", {"code":"provider_refusal","message":"The provider refused this response. See its explanation above; this is not a token limit or a user stop."})
                    break
                if not turn.tool_calls:
                    if dirty and not final_checked and not getattr(self.provider, "native_workflows", False):
                        # One fallback review at the final boundary, never after every mutation.
                        final_checked = True
                        shot_start = time.monotonic()
                        shot = self._capture("studio_final")
                        emit("capture_timing", {"seconds": time.monotonic()-shot_start})
                        if shot:
                            path, encoded = shot
                            result = ToolResult("final_review", "capture_viewport", True, {})
                            note = self._look(result, encoded, {"expected_stage": "The user's requested sequence is now complete; inspect only visible geometry, not feature-tree contents."})
                            if note: emit("vision", {"id": "final_review", "text": note})
                            emit("capture", {"path": path, "url": "/api/image?path="+path})
                            feedback = [{"type": "text", "text": "Final visual review is already completed (uncertain observation): " + (note or "Inspect the attached capture; do not infer unseen feature details.") + " Give the final outcome in at most 80 words. Do not capture again unless an unresolved issue requires a different view. Do not claim unmeasured facts."}]
                            feedback.extend({"type":"image",**img} for img in result.image_blocks())
                            self.provider.add_user(feedback)
                            continue
                        emit("error", {"message": "Final viewport capture was unavailable; visual verification is incomplete."})
                    for data in pending_text: emit("text", data)
                    break
                for data in pending_text: emit("text", data)
                results = []
                face_request = None
                for call in turn.tool_calls:
                    spec = selected.get(call.name)
                    mutating = bool(spec and spec.get("mutating"))
                    emit("tool_start", {"id": call.id, "name": call.name,
                                        "arguments": call.arguments, "mutating": mutating})
                    tool_started = time.monotonic()
                    signature = json.dumps([call.name, call.arguments], sort_keys=True)
                    try:
                        if face_request:
                            raise ArgumentError("Awaiting the user's face selection; this later call was not executed.")
                        if self.stopped(): raise ArgumentError("Stopped before execution")
                        if not spec: raise ArgumentError("Tool is not loaded; use load_tools to expose its group")
                        validate_arguments(call.arguments, next(s for s in specs if s.name == call.name).json_schema())
                        if repeated.get(signature, 0) >= 2:
                            raise ArgumentError("This exact call already failed twice. Change the arguments or inspect the reported state; another identical request was not executed.")
                        if call.name == "load_tools":
                            self.bridge.enabled_groups.add(call.arguments["group"])
                            registry = self.bridge.registry()
                            payload = {"loaded": call.arguments["group"], "available": sorted(agent_runtime.select(registry, self.bridge.enabled_groups)),
                                       "capability_version": getattr(self.bridge, "capability_version", None)}
                            outcome = {"ok": True, "result": payload}
                        elif call.name == "request_face_selection":
                            face_request = {**call.arguments,"document_id":getattr(self.bridge,"_document_id",None)}
                            outcome = {"ok": True, "result": {"status":"awaiting_user_faces",**face_request}}
                        elif call.name == "get_result":
                            payload = self.bridge.result_cache[call.arguments["result_ref"]]
                            for key in call.arguments.get("field", "").split("."):
                                if key: payload = payload[int(key)] if isinstance(payload, list) else payload[key]
                            outcome = {"ok": True, "result": payload}
                        else:
                            if mutating:
                                read_loop.reset()
                                if design_state is not None and design_state.entries:
                                    warnings = [warning for warning in design_state.warnings() if ": stale" in warning or ": blocked" in warning]
                                    if warnings:
                                        emit("progress_warning", {"message": "Design dependencies need attention: " + "; ".join(warnings[:3])})
                            outcome = self.bridge.call(call.name, call.arguments, confirm=mutating)
                    except ArgumentError as exc:
                        outcome = {"ok": False, "error": {"code": exc.code, "message": str(exc), "context": exc.context, "execution_started": False}}
                    except (KeyError, IndexError, ValueError):
                        outcome = {"ok": False, "error": {"code": "invalid_arguments", "message": "Unknown result reference or field"}}
                    except BridgeError as exc:
                        outcome = {"ok": False, "error": {"code": "bridge_unreachable", "message": str(exc)}}
                    ok = bool(outcome.get("ok"))
                    consecutive_failures = 0 if ok and mutating else consecutive_failures + (not ok)
                    payload = outcome.get("result") if ok else outcome.get("error")
                    if ok and not mutating:
                        unchanged = read_loop.observe(call.name, call.arguments, payload)
                        if unchanged >= 2:
                            warning = (f"{call.name} returned the same result {unchanged} times with the same arguments. "
                                       "Reuse the available result and choose a different action. Do not repeat this query.")
                            if call.name == "list_files" and isinstance(payload, dict) and payload.get("count") == 0:
                                warning += " No files matched. The pattern is a filename glob; use a different pattern or directory."
                            if isinstance(payload, dict):
                                payload = {**payload, "repeat_warning": warning}
                            emit("progress_warning", {"name": call.name, "repeats": unchanged, "message": warning})
                    repeated[signature] = 0 if ok else repeated.get(signature, 0) + 1
                    reference = "result_" + call.id
                    if isinstance(payload, dict): payload = {**payload, "result_ref": reference}
                    self.bridge.result_cache[reference] = payload
                    emit("tool_end", {"id": call.id, "name": call.name, "ok": ok, "result": payload,
                                      "seconds": time.monotonic() - tool_started})
                    if design_state is not None and (mutating or call.name == "update_design_state"):
                        emit("design_state", design_state.snapshot())
                    result = ToolResult(call.id, call.name, ok, payload)
                    if ok and mutating and call.name not in {"cancel_operation", "switch_document", "create_document", "write_file", "patch_file"}:
                        dirty = True
                        final_checked = False
                    if ok and call.name == "capture_viewport":
                        self.expected_stage = call.arguments.get("expected_stage", "Inspect visible geometry only; current stage is unspecified")
                        final_checked = True
                        dirty = False
                    if ok and not mutating:
                        for path in _result_images(payload)[:MAX_TOOL_IMAGES]:
                            looked = (None if getattr(self.provider, "native_workflows", False)
                                      else self._look_at_file(result, path))
                            if looked: emit("vision", {"id": call.id, "text": looked})
                            emit("capture", {"path": path, "url": "/api/image?path=" + path})
                    if ok and call.name in {"write_file", "patch_file"}:
                        registry = self.bridge.registry()
                    results.append(result)
                self.provider.add_tool_results(results)
                if face_request:
                    emit("face_selection_request", face_request)
                    break
                if consecutive_failures >= 5:
                    emit("progress_warning", {"message":"Repeated tool failures: use the returned errors to change approach or ask for the missing input."})
                if self.stopped():
                    emit("stopped", {})
                    break
        finally:
            emit("turn_end", {"active_seconds": time.monotonic()-started})
            emit("done", {})


def _result_images(payload: Any) -> list[str]:
    """Image paths anywhere in a tool result, in the order produced."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.lower().endswith((".png", ".jpg", ".jpeg")) and node.startswith("/"):
                found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    # Preserve order, drop repeats: a result often names the same file twice.
    return list(dict.fromkeys(found))


def _describe_attachments(blocks: list[dict]) -> list[dict]:
    """Replace image blocks with descriptions, for a worker that cannot see.

    The person's own words travel with each image, so the describer knows
    what it is being asked to look for rather than reporting generically.
    """
    request = " ".join(b["text"] for b in blocks if b["type"] == "text")
    note = ("The person attached this image and wrote: " + request
            if request else "The person attached this image.")
    out: list[dict] = []
    for block in blocks:
        if block["type"] != "image":
            out.append(block)
            continue
        described = DESCRIBER.describe(
            block["data"],
            note + " Describe it in detail, including anything they have drawn "
                   "or marked on it and where those marks sit on the model.",
            block.get("media_type", "image/png"))
        out.append({"type": "text",
                    "text": "[An image the person attached. You cannot see it; "
                            "this is a description of it.]\n" + described})
    return out


# --------------------------------------------------------------- server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "FusionStudio/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any,
              content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _session(self) -> "Session":
        from urllib.parse import parse_qs, urlparse

        wanted = parse_qs(urlparse(self.path).query).get("session", [None])[0]
        return SESSIONS.get(wanted)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/sessions":
            self._send(HTTPStatus.OK, {"sessions": SESSIONS.listing()})
            return
        if path.startswith("/api/session/"):
            session = SESSIONS.get(path.rsplit("/", 1)[-1])
            self._send(HTTPStatus.OK, {**session.summary(), "log": session.log})
            return
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, (HERE / "static" / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path == "/api/bootstrap":
            self._bootstrap()
        elif path == "/api/image":
            self._image()
        elif path == "/api/capture":
            self._capture_now()
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/chat":
            self._chat()
        elif path == "/api/face-selection":
            self._face_selection()
        elif path == "/api/model":
            self._set_model()
        elif path == "/api/stop":
            session = self._session()
            session.stop.set()
            self._send(HTTPStatus.OK, {"ok": True, "id": session.id})
        elif path == "/api/sessions":
            session = SESSIONS.create()
            self._send(HTTPStatus.OK, session.summary())
        elif path == "/api/reset":
            session = self._session()
            try:
                session.ensure().reset()
            except ProviderError:
                pass
            session.bridge.design_state = DesignState(session.bridge.design_state.path)
            session.bridge.design_state.entries = {}
            session.bridge.design_state.next_action = ""
            session.bridge.design_state.evidence = {}
            session.bridge.design_state.save()
            session.log.clear()
            session.cost = 0.0
            self._send(HTTPStatus.OK, {"ok": True, "id": session.id})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/session/"):
            removed = SESSIONS.delete(path.rsplit("/", 1)[-1])
            self._send(HTTPStatus.OK if removed else HTTPStatus.NOT_FOUND,
                       {"ok": removed, "sessions": SESSIONS.listing()})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _bootstrap(self) -> None:
        session = self._session()
        info: dict[str, Any] = {
            "presets": providers.PRESETS,
            "backends": providers.available(),
            "current": {"provider": session.provider_name, "model": session.model,
                        "base_url": session.base_url, "effort": session.effort},
            "session": session.summary(),
            "sessions": SESSIONS.listing(),
            "vision": {"mode": VISION_MODE,
                       "model": DESCRIBER.model if DESCRIBER else None,
                       "provider": DESCRIBER.provider_name if DESCRIBER else None},
        }
        try:
            provider = session.ensure()
            info["model_status"] = provider.check()
            info["vision"]["mode"] = ("off" if getattr(provider, "native_workflows", False)
                                      else "direct" if provider.supports_images else "describe")
            info["model_ok"] = True
            info["capabilities"] = {
                "images": provider.supports_images,
                "tool_result_images": provider.tool_result_images,
                "thinking": provider.supports_thinking,
            }
        except ProviderError as exc:
            info["model_status"] = str(exc)
            info["model_ok"] = False

        try:
            registry = BRIDGE.registry()
            info["tools"] = sorted(registry)
            info["mutating"] = sorted(n for n, s in registry.items() if s.get("mutating"))
            info["fusion"] = BRIDGE.call("health", {}, confirm=False).get("result", {})
            info["bridge"] = "connected"
        except BridgeError as exc:
            info["bridge"] = "unreachable"
            info["bridge_error"] = str(exc)
        self._send(HTTPStatus.OK, info)

    def _set_model(self) -> None:
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        model = payload.get("model", "").strip()
        provider = payload.get("provider", "").strip()
        if not model or not provider:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "provider and model required"})
            return
        session = self._session()
        session.configure(provider, model,
                          (payload.get("base_url") or "").strip() or None,
                          payload.get("effort", "high"))
        try:
            status = session.ensure().check()
            self._send(HTTPStatus.OK, {"ok": True, "status": status})
        except ProviderError as exc:
            self._send(HTTPStatus.OK, {"ok": False, "status": str(exc)})

    def _image(self) -> None:
        from urllib.parse import parse_qs, urlparse

        raw = parse_qs(urlparse(self.path).query).get("path", [""])[0]
        target = Path(raw).resolve()
        # Only ever serve files from the agent's own capture directory.
        # Captures and downloads both live under the agent's own directory;
        # a fetched drawing has to be viewable or fetching it was pointless.
        root = agent_home().resolve()
        if root not in target.parents or not target.is_file():
            self._send(HTTPStatus.FORBIDDEN, {"error": "outside capture directory"})
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, target.read_bytes(), kind)

    def _capture_now(self) -> None:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        view = query.get("view", ["current"])[0]
        # Capturing "current" must not re-zoom, or the framing the person set
        # up in Fusion is lost the moment they press the button.
        arguments = {"views": [view], "width": 1400, "height": 1050,
                     "prefix": "studio_live"}
        if view == "current":
            arguments["fit"] = False
        try:
            response = BRIDGE.call("capture_viewport", arguments, confirm=False)
        except BridgeError as exc:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        if not response.get("ok"):
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, response.get("error", {}))
            return
        path = response["result"]["captures"][0]["path"]
        self._send(HTTPStatus.OK, {"path": path, "url": "/api/image?path=" + path})

    def _face_selection(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ArgumentError("Invalid selection request")
            action = payload.get("action", "attach")
            if action == "attach":
                outcome = BRIDGE.call("get_selected_faces", {}, confirm=False)
            elif action == "highlight":
                refs = payload.get("refs")
                if (not isinstance(refs, list) or not 1 <= len(refs) <= 12 or
                        any(not isinstance(ref, str) or len(ref) > 80 for ref in refs)):
                    raise ArgumentError("Select between one and twelve attached faces to highlight")
                outcome = BRIDGE.call("highlight_selected_faces", {"refs": refs}, confirm=False)
            else:
                raise ArgumentError("Unknown face selection action")
            if not outcome.get("ok"):
                raise ArgumentError(outcome.get("error", {}).get("message", "Could not read Fusion selection"))
            result = dict(outcome["result"])
            result.setdefault("document_id", outcome.get("document_id"))
            self._send(HTTPStatus.OK, result)
        except (ArgumentError, BridgeError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _chat(self) -> None:
        session = self._session()
        try:
            provider = session.ensure()
        except ProviderError as exc:
            self._send(HTTPStatus.PRECONDITION_FAILED, {"error": str(exc)})
            return
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        # "images" is the current shape; "image" is still accepted so an older
        # client or a scripted caller does not break.
        attachments = payload.get("images")
        if attachments is None:
            attachments = payload.get("image", "")
        blocks = providers.user_blocks(payload.get("text", ""), attachments)
        try:
            selected_faces = face_attachments.resolve_attachments(session.bridge, payload.get("faces", []))
        except (ArgumentError, BridgeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if selected_faces:
            face_block = face_attachments.model_block(selected_faces)
            if getattr(provider, "native_workflows", False):
                face_block["fusion_faces"] = selected_faces
            blocks.append(face_block)
        if not blocks:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
            return
        if any(b["type"] == "image" for b in blocks) and getattr(provider, "native_workflows", False):
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"{provider.name} does not use images or a vision fallback. Attach exact faces using Faces instead."})
            return
        if any(b["type"] == "image" for b in blocks) and not provider.supports_images:
            # A text-only worker (DeepSeek V4 Flash, most code models) cannot
            # take the markup directly. Describing it is lossy -- an arrow
            # drawn on a face means something a paraphrase will blur -- but it
            # beats refusing the message outright, so long as it is said
            # plainly rather than done silently.
            if DESCRIBER is None:
                self._send(HTTPStatus.BAD_REQUEST,
                           {"error": f"{provider.name}/{provider.model} cannot accept "
                                     "images, and no vision model is configured. Set "
                                     "STUDIO_VISION_MODEL, or pick a worker that sees."})
                return
            blocks = _describe_attachments(blocks)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        channel: queue.Queue = queue.Queue()

        def emit(event: str, data: Any) -> None:
            channel.put((event, data))

        text = payload.get("text", "")
        images = sum(1 for b in blocks if b["type"] == "image")
        session.name_from(text)
        session.record("user", {"text": text, "images": images, "faces": selected_faces})
        if session.recorder is not None:
            session.recorder.user_message(text, images, faces=selected_faces)

        # Three consumers of one stream: the browser over SSE, the chat's own
        # replayable log, and the transcript on disk. Teeing here rather than
        # instrumenting the agent keeps them from ever disagreeing.
        def keep(event: str, data: Any) -> None:
            # Providers call emit for every token, which makes this the one
            # place a stop can take effect mid-generation rather than after
            # the whole response has been paid for.
            if session.stop.is_set():
                raise Cancelled()
            session.record(event, data)
            emit(event, data)

        recording = transcript.tee(session.recorder, keep)
        session.stop.clear()
        session.busy = True

        def work() -> None:
            try:
                Agent(session.bridge, provider).run(blocks, recording,
                                            stopped=session.stop.is_set)
            except Cancelled:
                # Asked for, so not an error. Say it plainly and end the turn.
                session.record("stopped", {"reason": "stopped by you"})
                emit("stopped", {"reason": "stopped by you"})
                emit("done", {})
            except ProviderError as exc:
                recording("error", {"message": str(exc)})
                recording("done", {})
            except Exception as exc:
                traceback.print_exc()
                recording("error", {"message": f"{exc.__class__.__name__}: {exc}"})
                recording("done", {})
            finally:
                session.busy = False

        threading.Thread(target=work, daemon=True).start()
        while True:
            event, data = channel.get()
            try:
                self.wfile.write(
                    f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if event == "done":
                return


def main() -> int:
    # Unbuffered: startup lines must appear even when stdout is redirected.
    say = lambda text: print(text, flush=True)
    say("\n  Fusion Studio")
    if ENV_LOADED:
        say(f"  env          loaded {', '.join(ENV_LOADED)} from studio/.env")
    try:
        say(f"  worker       {SESSIONS.get(None).ensure().check()}")
    except ProviderError as exc:
        say(f"  model        NOT READY -- {exc}")
        for name, state in providers.available().items():
            if not state["sdk_installed"]:
                say(f"               {name}: {state['install']}")
            elif not state["key_present"]:
                say(f"               {name}: export {state['env_var']}=...")
    if not TRANSCRIPTS_OFF:
        say(f"  transcript   {transcript.transcript_dir()}/ (one per chat)")
    if DESCRIBER is None:
        say("  vision       off -- captures attached as images")
    else:
        say(f"  vision       {DESCRIBER.provider_name} / {DESCRIBER.model} "
            f"({VISION_MODE})")
    try:
        BRIDGE.registry()
        say("  fusion       bridge connected")
    except BridgeError as exc:
        say(f"  fusion       NOT CONNECTED -- {exc}")

    port = int(os.environ.get("STUDIO_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    say(f"\n  ->  http://127.0.0.1:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
