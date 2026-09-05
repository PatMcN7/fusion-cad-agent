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

2. After every mutating tool the harness captures the viewport and gives the
   image to the model. It therefore *sees* what it built rather than only
   reading measurements back -- the gap that let an off-centre lattice and an
   unsupported pan edge through in earlier sessions.

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

from common import agent_home, load_or_create_token, read_connection_info  # noqa: E402

# Captures are fed back on every mutating call, so keep them modest -- a
# full-resolution frame can cost several thousand tokens each time.
CAPTURE_WIDTH = 1100
CAPTURE_HEIGHT = 825
MAX_LOOP_STEPS = 80
# A fetched PDF can render to many pages; looking at all of them would cost
# more than the drawing is worth.
MAX_TOOL_IMAGES = 4

def _load_brief() -> str:
    """The shared agent brief, read from AGENTS.md at the repo root.

    Codex picks this file up on its own; the other backends get it as their
    system prompt. Keeping one file means a change reaches every client.
    """
    try:
        import catalog
        return catalog.brief()
    except Exception:
        return ("You drive Autodesk Fusion through a local bridge. Dimensions "
                "are unit expression strings such as '12 in'. Read the "
                "verification block on every mutating call, and look at the "
                "viewport capture before saying a part is done.")


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

class BridgeError(RuntimeError):
    pass


class Bridge:
    """Thin client for the Fusion add-in's localhost API."""

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            connection = read_connection_info()
            token = load_or_create_token()
        except RuntimeError as exc:
            raise BridgeError(str(exc)) from exc
        url = f"http://{connection.get('host', '127.0.0.1')}:{int(connection['port'])}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=310) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                return json.loads(detail)
            except json.JSONDecodeError:
                raise BridgeError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise BridgeError(f"Fusion bridge unreachable at {url}: {exc.reason}") from exc

    def registry(self) -> dict[str, Any]:
        return self._request("GET", "/v1/tools").get("tools", {})

    def call(self, tool: str, arguments: dict, confirm: bool) -> dict:
        return self._request("POST", "/v1/call",
                             {"tool": tool, "arguments": arguments, "confirm": confirm})


def _property_schema(description: str) -> dict[str, Any]:
    """Infer a loose JSON type from the registry's prose description.

    The bridge documents arguments in English rather than JSON Schema, so this
    reads the conventions it uses ("default 3", "default true", "[l, w, h]")
    and otherwise leaves the type open rather than guessing wrong. A wrong type
    is worse than an absent one: it makes the model's correct value invalid.
    """
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


def tool_specs(registry: dict[str, Any]) -> list[ToolSpec]:
    """Bridge registry -> neutral tool specs. Generated, never hand-written."""
    specs = []
    for name, spec in sorted(registry.items()):
        properties = {
            key: _property_schema(str(text))
            for key, text in (spec.get("arguments") or {}).items()
        }
        description = spec.get("description", "")
        if spec.get("mutating"):
            description += " (Modifies the design; runs verified and checkpointed.)"
        specs.append(ToolSpec(name=name, description=description, properties=properties))
    return specs


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
        self.provider: Provider | None = None
        self.provider_name = os.environ.get("STUDIO_PROVIDER", "anthropic")
        self.model = os.environ.get("STUDIO_MODEL", "claude-opus-5")
        self.base_url = os.environ.get("STUDIO_BASE_URL") or None
        # Low, not high. A logged session spent 96% of everything it
        # generated on reasoning -- 49k thinking tokens against 1.9k of
        # visible output -- and 82% of its wall clock waiting for it. The
        # hard geometry lives in patterns.py and the checks; what the model
        # is doing here is choosing a tool and filling in arguments, and
        # high effort did not make it better at that. Raise it per chat when
        # a request genuinely needs planning.
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
        if kind == "text" and self.log and self.log[-1]["kind"] == "text":
            self.log[-1]["data"]["text"] += data.get("text", "")
            return
        if kind == "usage" and isinstance(data, dict):
            if data.get("session_cost_usd") is not None:
                self.cost = float(data["session_cost_usd"])
            elif data.get("cost_usd"):
                self.cost += float(data["cost_usd"])
        self.log.append({"kind": kind, "data": data})
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
                "cost": round(self.cost, 5)}

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
            if self._sessions:
                return max(self._sessions.values(), key=lambda s: s.updated)
        return self.create()

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session and session.recorder:
            session.recorder.close(reason="chat deleted")
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
        self.stopped: Callable[[], bool] = lambda: False

    def _capture(self, prefix: str) -> tuple[str, str] | None:
        try:
            response = self.bridge.call("capture_viewport", {
                "views": ["iso"], "width": CAPTURE_WIDTH,
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
        """Attach the capture to the tool result, as picture or as prose.

        An image joins the transcript permanently and is re-sent on every
        later turn; a description costs a fraction of that and answers the
        same question -- does this look like the thing that was asked for.
        The vision call itself carries no history, so the model doing the
        looking can be a frontier one while the loop runs something small.

        The *arguments* go with the image. A describer told only "this is the
        model" can narrate but cannot judge; told "3 inch hex cells were
        requested" it can say the cells look like an inch. That difference is
        the whole point -- the geometric checks already prove a result is
        self-consistent, and the only thing left for a picture to catch is a
        result that is consistent with the wrong thing.
        """
        if DESCRIBER is None:
            result.image_b64 = encoded
            return None
        note = f"This is the model immediately after `{result.name}` ran."
        if requested:
            note += ("\n\nIt was called with these arguments:\n"
                     + json.dumps(requested, indent=2, default=str)[:800]
                     + "\n\nSay whether what you see matches them. If a count, "
                       "size or placement looks different from what was asked "
                       "for, lead with that.")
        description = DESCRIBER.describe(encoded, note)
        if VISION_MODE == "both":
            result.image_b64 = encoded
        return description

    def _look_at_file(self, result: ToolResult, path: str) -> str | None:
        """Show the model an image a tool produced, however it can see it."""
        try:
            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            return None
        name = Path(path).name
        if DESCRIBER is None:
            result.image_b64 = encoded
            return None
        text = DESCRIBER.describe(
            encoded,
            f"This image came back from `{result.name}` as {name}. It may be an "
            "engineering drawing rather than a CAD viewport. Report the "
            "dimensions, hole patterns and callouts you can actually read.")
        if VISION_MODE == "both":
            result.image_b64 = encoded
        # Append rather than replace: one tool call can return several pages.
        result.description = ((result.description or "")
                              + f"\n\n[{name}]\n{text}").strip()
        return text

    def run(self, blocks: list[dict], emit: Callable[[str, Any], None],
            stopped: Callable[[], bool] = lambda: False) -> None:
        self.stopped = stopped
        registry = self.bridge.registry()
        specs = tool_specs(registry)
        mutating = {n for n, s in registry.items() if s.get("mutating")}
        emit("tools", {"count": len(specs), "names": sorted(registry)})

        self.provider.add_user(blocks)

        for _ in range(MAX_LOOP_STEPS):
            turn = self.provider.stream(SYSTEM_PROMPT, specs, emit)

            if turn.usage:
                emit("usage", turn.usage)

            # An agent backend already ran its own loop and executed its own
            # tools; running them again here would duplicate every operation.
            if getattr(self.provider, "is_agent", False):
                break
            if turn.stop_reason == "refusal":
                emit("error", {"message": "The model declined this request."})
                break
            if turn.stop_reason == "length":
                emit("error", {"message": "Response hit the output limit."})
            if not turn.tool_calls:
                break

            if self.stopped():
                # The model asked for tools and we are not going to run them.
                # Answer every call anyway: an assistant turn carrying
                # tool_calls with no matching results is a malformed history,
                # and the *next* message would be the one that fails.
                self.provider.add_tool_results([
                    ToolResult(call.id, call.name, False,
                               {"error": "stopped by the user before this ran"})
                    for call in turn.tool_calls])
                emit("stopped", {"before": [c.name for c in turn.tool_calls]})
                break

            results: list[ToolResult] = []
            for call in turn.tool_calls:
                if self.stopped():
                    results.append(ToolResult(
                        call.id, call.name, False,
                        {"error": "stopped by the user before this ran"}))
                    continue
                if "__parse_error__" in call.arguments:
                    emit("tool_end", {"id": call.id, "name": call.name, "ok": False,
                                      "result": {"error": "malformed tool arguments"}})
                    results.append(ToolResult(
                        call.id, call.name, False,
                        {"error": "Arguments were not valid JSON. Retry with "
                                  "well-formed arguments."}))
                    continue

                is_mutating = call.name in mutating
                emit("tool_start", {"id": call.id, "name": call.name,
                                    "arguments": call.arguments,
                                    "mutating": is_mutating})
                try:
                    outcome = self.bridge.call(call.name, call.arguments,
                                               confirm=is_mutating)
                except BridgeError as exc:
                    outcome = {"ok": False, "error": {"code": "bridge_unreachable",
                                                      "message": str(exc)}}

                ok = bool(outcome.get("ok"))
                payload = outcome.get("result") if ok else outcome.get("error")
                emit("tool_end", {"id": call.id, "name": call.name,
                                  "ok": ok, "result": payload})

                result = ToolResult(call.id, call.name, ok, payload)
                if ok and is_mutating and call.name != "undo_last":
                    shot = self._capture(f"studio_{call.name}")
                    if shot:
                        path, encoded = shot
                        description = self._look(result, encoded,
                                                 call.arguments)
                        if description:
                            result.description = description
                            emit("vision", {"id": call.id, "text": description})
                        emit("capture", {"path": path,
                                         "url": "/api/image?path=" + path})
                elif ok:
                    # A fetched drawing or a rendered PDF page comes back as a
                    # path. Left at that, the model is handed a filename and
                    # told nothing -- which defeats the point of fetching it.
                    for path in _result_images(payload)[:MAX_TOOL_IMAGES]:
                        looked = self._look_at_file(result, path)
                        if looked:
                            emit("vision", {"id": call.id, "text": looked})
                        emit("capture", {"path": path,
                                         "url": "/api/image?path=" + path})
                results.append(result)

            self.provider.add_tool_results(results)

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
        if not blocks:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
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
        session.record("user", {"text": text, "images": images})
        if session.recorder is not None:
            session.recorder.user_message(text, images)

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
                Agent(BRIDGE, provider).run(blocks, recording,
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
