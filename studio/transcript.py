"""Record every session to JSONL so it can be taken apart afterwards.

This exists because of an asymmetry. When a design session went badly it was
possible to work out *why* -- 104 tool calls, a quarter of them grepping the
source, fourteen captures accumulating in context -- purely because that
agent happened to write a rollout log. The studio kept nothing. Its sessions
could be judged only by whether the part looked right at the end, which is
the same blindness the whole project is about.

So: one file per session, one JSON object per line, appended as events
happen. Not a debug log -- a record structured for counting. Every question
worth asking afterwards is a question about frequency or cost:

    how many calls were real geometry, and how many were the agent orienting?
    what did this session cost, and where did the tokens actually go?
    how often did a check fail and roll an operation back?
    did the small model call tools as well as the big one?

Two things are deliberately not recorded. Image bytes stay out -- a capture
is referenced by path and by what the vision model said about it, because a
transcript that carries base64 is one nobody will ever open. And nothing
reads the environment, so no key can reach the file.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from common import agent_home

# Events whose payload is large and whose content is already captured
# elsewhere. `text` and `thinking` arrive as one record per token; storing
# them raw would make the file mostly punctuation, so they are accumulated
# and written once per turn instead.
STREAMED = {"text", "thinking"}
SKIP = {"thinking_start"}


def transcript_dir() -> Path:
    directory = Path(os.environ.get("STUDIO_TRANSCRIPTS")
                     or agent_home() / "transcripts")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class Recorder:
    """One session, appended to as it happens.

    Writes are flushed per record. A session that crashes or is killed is
    exactly the one worth reading, so buffering it away would lose the case
    that matters most.
    """

    def __init__(self, session_id: str | None = None, **context: Any) -> None:
        self.id = session_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.path = transcript_dir() / f"session-{self.id}.jsonl"
        self.started = time.time()
        self._lock = threading.Lock()
        self._pending: dict[str, list[str]] = {}
        self._write("session_start", {"version": 1, **context})

    # ---- writing ---------------------------------------------------------

    def _write(self, kind: str, data: Any) -> None:
        record = {"t": round(time.time() - self.started, 3), "kind": kind,
                  "data": data}
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # A transcript is an observation of the session, never a
                # participant in it. If it cannot be written, the session
                # still runs.
                pass

    def event(self, name: str, data: Any) -> None:
        """Tee one emitted event. Safe to call from the agent thread."""
        if name in SKIP:
            return
        if name in STREAMED:
            text = (data or {}).get("text", "")
            if text:
                self._pending.setdefault(name, []).append(text)
            return
        # Any non-streamed event closes the run of streamed text before it,
        # so ordering in the file matches ordering in the session.
        self._flush_streamed()
        if name == "tool_end":
            data = self._summarize_tool_end(data)
        self._write(name, data)

    def _flush_streamed(self) -> None:
        for name, parts in list(self._pending.items()):
            joined = "".join(parts)
            if joined.strip():
                self._write(name, {"text": joined, "chars": len(joined)})
            del self._pending[name]

    @staticmethod
    def _summarize_tool_end(data: Any) -> Any:
        """Keep the verification verdicts; drop the bulk around them.

        A `verification` block is the most valuable thing in a tool result
        and among the largest. Reduce it to what a later count needs: which
        checks ran, which failed, and how.
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        result = out.get("result")
        if isinstance(result, dict):
            trimmed = {k: v for k, v in result.items() if k != "verification"}
            checks = []
            reports = result.get("verification") or []
            if isinstance(reports, dict): reports = [reports]
            for report in reports:
                for check in (report.get("checks") or []):
                    checks.append({"name": check.get("name"),
                                   "ok": check.get("ok"),
                                   "severity": check.get("severity"),
                                   "actual": check.get("actual")})
            if checks:
                trimmed["checks"] = checks
                # Only an error rolls an operation back. A failed warning is
                # a finding that stands -- counting the two together would
                # inflate the rollback rate with millability notes.
                trimmed["checks_failed"] = [
                    c["name"] for c in checks
                    if not c["ok"] and c.get("severity") != "warning"]
            out["result"] = trimmed
        return out

    def user_message(self, text: str, image_count: int = 0, faces: list | None = None) -> None:
        data = {"text": text, "images": image_count}
        if faces:
            data["faces"] = faces
        self._write("user", data)

    def close(self, **summary: Any) -> None:
        self._flush_streamed()
        self._write("session_end", {"wall_seconds": round(time.time() - self.started, 2),
                                    **summary})


def tee(recorder: "Recorder | None", emit):
    """Wrap an emit callable so everything it carries is also recorded.

    The agent loop already funnels every observable thing through `emit`, so
    recording needs no new instrumentation inside it -- and cannot fall out
    of step with what the UI shows, because it is the same stream.
    """
    if recorder is None:
        return emit

    def wrapped(event: str, data: Any) -> None:
        try:
            recorder.event(event, data)
        except Exception:
            pass          # never let recording break a session
        emit(event, data)

    return wrapped
