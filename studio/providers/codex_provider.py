"""Codex adapter -- drives the local Codex CLI as a backend.

This one is different in kind from the others. Anthropic, OpenAI and Gemini are
*model* backends: they propose tool calls and the harness executes them. Codex
is an *agent*: it runs its own loop and executes tools itself through the
fusion-cad MCP server. So this adapter runs a whole turn and reports what
happened, rather than handing back tool calls to be run.

That distinction is what makes it work at all. Codex authenticates with the
user's ChatGPT subscription via OAuth (`auth_mode = "chatgpt"`, no API key), so
routing through the CLI uses that entitlement through OpenAI's own sanctioned
client. No credential is read, copied, or forwarded by this code.

Consequences worth knowing:

  * Tools come from Codex's MCP configuration, not from the `tools` argument.
    Register the bridge with `codex mcp add fusion-cad -- python3 mcp_server.py`.
  * The studio's system prompt is passed as context on the first turn, but
    Codex has its own instructions and its own idea of how to work.
  * Conversation continuity uses Codex's own session (`exec resume`), so
    history lives in Codex rather than in this process.
  * Approval mode is set by STUDIO_CODEX_APPROVAL:
        full-auto (default) -- no approvals, no sandbox. Codex's shell and
                               file tools run unrestricted.
        auto                -- approvals auto-reviewed inside the
                               workspace-write sandbox.
        manual              -- no approval flag; destructive tools fail, since
                               Codex cannot prompt non-interactively.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import Emit, Provider, ProviderError, ToolResult, ToolSpec, Turn

# Codex turns on a real modelling task can run for minutes.
TURN_TIMEOUT = 900

# Rejected by `codex exec resume`. Guarded rather than merely documented,
# because the failure surfaces only on a conversation's second turn.
EXEC_ONLY_FLAGS = frozenset({
    "--approve-for-me", "--sandbox", "--cd", "--add-dir", "--profile",
    "--oss", "--local-provider", "--color",
})


class CodexProvider(Provider):
    name = "codex"
    supports_images = True
    tool_result_images = False   # Codex owns tool execution; it sees its own
    supports_thinking = True
    is_agent = True              # runs its own loop; harness must not execute tools

    def __init__(self, model: str = "gpt-5.6-sol", **kwargs: Any) -> None:
        self.binary = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
        if not Path(self.binary).exists():
            raise ProviderError(
                "the `codex` CLI was not found on PATH. Install Codex, or pick "
                "a different backend.")
        # Run from the repo so Codex's cwd is predictable and its sandbox has a
        # sensible root; the CAD work happens in Fusion, not on the filesystem.
        self.workdir = str(Path(__file__).resolve().parent.parent.parent)
        super().__init__(model, **kwargs)

    def reset(self) -> None:
        self.session_id: str | None = None
        self._pending_text: list[str] = []
        self._pending_images: list[str] = []

    def add_user(self, blocks: list[dict[str, Any]]) -> None:
        for block in blocks:
            if block["type"] == "text":
                self._pending_text.append(block["text"])
            elif block["type"] == "image":
                self._pending_images.append(block["data"])

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # Unreachable in normal operation: Codex executes its own tools, so the
        # harness never has results to hand back. Guard rather than silently
        # dropping them, which would look like the model ignoring a result.
        raise ProviderError(
            "CodexProvider executes tools itself; the harness should not "
            "return tool results to it")

    # ---- turn -----------------------------------------------------------
    def _command(self, prompt: str, images: list[str]) -> tuple[list[str], list[str]]:
        """Build the argv for one turn.

        `codex exec` and `codex exec resume` do NOT accept the same flags.
        Verified against `--help` for both on codex-cli 0.149.1:

            both:      --json --skip-git-repo-check --model --image --config
                       --ephemeral --output-last-message --strict-config
            exec only: --approve-for-me --sandbox --cd --add-dir --profile
            resume:    --last --all

        Passing an exec-only flag to resume exits 2 with "unexpected argument",
        which only shows up on the *second* turn of a conversation. Keeping the
        two sets explicit is what stops that being rediscovered one flag at a
        time.

        Approval mode does not need repeating: a resumed session inherits it
        from the session that created it (verified -- a mutating tool succeeds
        on resume with no approval flag), so --approve-for-me is first-turn
        only. The working directory is set on the subprocess, which both honour.
        """
        temporary: list[str] = []
        for index, data in enumerate(images):
            handle = tempfile.NamedTemporaryFile(
                suffix=f"_studio_{index}.png", delete=False)
            handle.write(base64.b64decode(data))
            handle.close()
            temporary.append(handle.name)

        shared = ["--json", "--skip-git-repo-check"]
        # Approval mode. Unlike --approve-for-me (exec-only), the bypass flag
        # is accepted by both subcommands, so it needs no first-turn special
        # case. It also removes the sandbox: Codex's own shell and file tools
        # get unrestricted filesystem and network access, which is a wider
        # grant than letting the Fusion tools through.
        mode = os.environ.get("STUDIO_CODEX_APPROVAL", "full-auto")
        if mode == "full-auto":
            shared.append("--dangerously-bypass-approvals-and-sandbox")
        elif mode != "manual":
            # Sandboxed auto-approval; exec-only, so first turn carries it and
            # the resumed session inherits the mode.
            if not self.session_id:
                shared.append("--approve-for-me")

        if self.model:
            shared += ["--model", self.model]
        for path in temporary:
            shared += ["--image", path]

        # "--" ends option parsing. Without it a variadic flag swallows the
        # positional prompt: `--image a --image b <prompt>` makes the second
        # --image treat the prompt as another filename, and Codex then falls
        # back to reading stdin and exits with "No prompt provided".
        if self.session_id:
            argv = [self.binary, "exec", "resume", self.session_id, *shared, "--", prompt]
        else:
            argv = [self.binary, "exec", *shared, "--", prompt]

        for flag in argv:
            if self.session_id and flag in EXEC_ONLY_FLAGS:
                raise ProviderError(
                    f"internal error: {flag} is not accepted by `codex exec resume`")
        return argv, temporary

    def stream(self, system: str, tools: list[ToolSpec], emit: Emit) -> Turn:
        prompt = "\n\n".join(self._pending_text).strip()
        images = list(self._pending_images)
        self._pending_text, self._pending_images = [], []
        if not prompt:
            prompt = "(see attached image)"
        # Codex carries its own instructions; give it the studio's framing once.
        if self.session_id is None:
            prompt = f"{system}\n\n---\n\n{prompt}"

        argv, temporary = self._command(prompt, images)
        turn = Turn()
        text_parts: list[str] = []
        open_calls: dict[str, str] = {}

        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=self.workdir)
        except OSError as exc:
            self._cleanup(temporary)
            raise ProviderError(f"could not launch codex: {exc}") from exc

        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle(event, emit, text_parts, open_calls, turn)

            process.wait(timeout=TURN_TIMEOUT)
            if process.returncode != 0:
                detail = (process.stderr.read() or "").strip()[:400]
                if not text_parts:
                    raise ProviderError(
                        f"codex exited {process.returncode}: {detail or 'no output'}")
                emit("error", {"message": f"codex exited {process.returncode}: {detail}"})
        except subprocess.TimeoutExpired:
            process.kill()
            raise ProviderError(f"codex turn exceeded {TURN_TIMEOUT}s") from None
        finally:
            self._cleanup(temporary)

        turn.text = "\n\n".join(text_parts)
        turn.stop_reason = "end"      # the agent loop is already finished
        return turn

    def _handle(self, event: dict, emit: Emit, text_parts: list[str],
                open_calls: dict[str, str], turn: Turn) -> None:
        kind = event.get("type")

        if kind == "thread.started":
            # Captured so the next turn resumes rather than starting over.
            self.session_id = event.get("thread_id") or self.session_id
            return

        if kind == "turn.completed":
            usage = event.get("usage") or {}
            turn.usage = {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cache_read": usage.get("cached_input_tokens", 0),
            }
            return

        if kind == "turn.failed":
            emit("error", {"message": str(event.get("error") or "codex turn failed")})
            return

        if kind not in ("item.started", "item.completed"):
            return

        item = event.get("item") or {}
        item_type = item.get("type")
        item_id = item.get("id", "")

        if item_type == "agent_message":
            if kind == "item.completed":
                text = item.get("text") or ""
                if text:
                    text_parts.append(text)
                    emit("text", {"text": text})
            return

        if item_type == "reasoning":
            summary = item.get("text") or item.get("summary") or ""
            if summary and kind == "item.completed":
                emit("thinking_start", {})
                emit("thinking", {"text": summary})
            return

        if item_type in ("mcp_tool_call", "command_execution", "function_call"):
            name = item.get("tool") or item.get("command") or item_type
            if kind == "item.started":
                open_calls[item_id] = name
                emit("tool_start", {
                    "id": item_id, "name": name,
                    "arguments": item.get("arguments") or {},
                    "mutating": bool(item.get("server") == "fusion-cad"
                                     and name not in _READ_ONLY),
                })
            else:
                result = item.get("result")
                payload, image = _split_result(result)
                emit("tool_end", {"id": item_id, "name": open_calls.get(item_id, name),
                                  "ok": not item.get("is_error"), "result": payload})
                if image:
                    emit("capture", {"inline": image})
                open_calls.pop(item_id, None)

    @staticmethod
    def _cleanup(paths: list[str]) -> None:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    def check(self) -> str:
        try:
            out = subprocess.run([self.binary, "--version"], capture_output=True,
                                 text=True, timeout=20)
        except Exception as exc:
            raise ProviderError(f"codex not runnable: {exc}") from exc
        version = (out.stdout or out.stderr).strip().splitlines()[0]

        # Without the bridge registered, Codex runs with no Fusion tools at all
        # and will confidently claim it cannot see the model.
        try:
            listing = subprocess.run([self.binary, "mcp", "list"], capture_output=True,
                                     text=True, timeout=30)
            if "fusion-cad" not in (listing.stdout or ""):
                raise ProviderError(
                    f"{version} is available but the fusion-cad MCP server is not "
                    "registered. Run: codex mcp add fusion-cad -- "
                    "python3 <repo>/mcp_server.py")
        except ProviderError:
            raise
        except Exception:
            pass
        return f"codex / {self.model} (subscription auth, tools via MCP)"


_READ_ONLY = {
    "health", "active_document", "design_summary", "check_interference",
    "verify_spec", "capture_viewport", "inspect_threads", "inspect_commands",
    "mechanics_report",
}


def _split_result(result: Any) -> tuple[Any, str | None]:
    """Pull an image out of an MCP tool result, if it carried one."""
    if not isinstance(result, dict):
        return result, None
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return result, None
    text_parts, image = [], None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "image" and block.get("data"):
            image = f"data:{block.get('mimeType','image/png')};base64,{block['data']}"
    payload: Any = "\n".join(text_parts)
    try:
        payload = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        pass
    return payload, image
