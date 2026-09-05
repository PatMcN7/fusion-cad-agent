"""Provider-neutral agent protocol.

The agent loop needs four things from a model backend: describe tools, stream a
turn, hand back tool calls, and accept tool results. Everything else -- block
shapes, streaming event names, how a tool result is addressed -- differs per
provider and is the adapter's problem.

Two decisions shape this design:

1. **Each provider owns its own native message history.** A neutral transcript
   that every adapter re-renders would lose provider-specific state that must
   round-trip verbatim (Anthropic thinking blocks, OpenAI's tool_call ids).
   Switching provider mid-session therefore starts a fresh history, which is
   honest: thinking blocks cannot be transplanted between vendors anyway.

2. **Images attached to tool results are a capability, not a given.** Anthropic
   accepts image blocks inside a tool_result. OpenAI's `role: "tool"` message
   is text-only, and Gemini's functionResponse likewise -- those adapters must
   deliver the viewport capture as a following user message instead. The
   protocol states the intent ("show the model this picture") and lets each
   adapter choose the mechanism.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal


@dataclass
class ToolSpec:
    """A tool, described once, rendered per provider."""

    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: list[str] = field(default_factory=list)

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": self.properties}
        if self.required:
            schema["required"] = self.required
        return schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    id: str
    name: str
    ok: bool
    payload: Any
    image_b64: str | None = None
    image_media_type: str = "image/png"
    # What a vision model saw in the capture, when the harness summarized it
    # instead of attaching it. Text stays cheap on every subsequent turn;
    # an image does not.
    description: str | None = None

    def as_text(self, limit: int = 24000) -> str:
        body = truncate(self.payload, limit)
        if not self.description:
            return body
        return (f"{body}\n\nViewport after this operation, described by the "
                f"vision model:\n{self.description}")


@dataclass
class Turn:
    """One assistant response, normalized."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Literal["end", "tool_use", "refusal", "length", "other"] = "end"
    usage: dict[str, int] = field(default_factory=dict)


Emit = Callable[[str, Any], None]


class Cancelled(BaseException):
    """The person pressed stop.

    Raised out of `emit` while a turn is streaming, so a cancel takes effect
    on the next token rather than after the whole response. Every adapter
    re-raises it untouched instead of wrapping it as a provider failure --
    stopping on purpose is not an error, and reporting it as one would put a
    red message in the log for something the person just asked for.

    It derives from BaseException so a broad `except Exception` around a
    stream loop cannot swallow it by accident. That is the entire reason for
    the unusual base class.
    """


class ProviderError(RuntimeError):
    """Raised for a provider-level failure the agent should surface verbatim."""


class Provider(abc.ABC):
    """A model backend the agent loop can drive.

    Adapters are constructed with a model id and an optional base_url; nothing
    else is assumed. `supports_images` and `tool_result_images` are advertised
    rather than probed, because discovering them at runtime means a failed turn.
    """

    name: str = "provider"
    supports_images: bool = True
    tool_result_images: bool = False
    supports_thinking: bool = False
    # An agent backend runs its own tool loop and returns a finished turn. The
    # harness must not execute tools for it or it would run them twice.
    is_agent: bool = False

    def __init__(self, model: str, *, base_url: str | None = None,
                 api_key: str | None = None, effort: str = "high") -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.effort = effort
        self.reset()

    # ---- history --------------------------------------------------------
    @abc.abstractmethod
    def reset(self) -> None:
        """Clear the conversation, keeping the client."""

    @abc.abstractmethod
    def add_user(self, blocks: list[dict[str, Any]]) -> None:
        """Append a user turn.

        `blocks` are neutral: {"type": "text", "text": ...} and
        {"type": "image", "media_type": ..., "data": <base64>}.
        """

    @abc.abstractmethod
    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append tool results, and any images they carry, however this
        provider accepts them."""

    # ---- generation -----------------------------------------------------
    @abc.abstractmethod
    def stream(self, system: str, tools: list[ToolSpec], emit: Emit) -> Turn:
        """Run one assistant turn, emitting incremental events as it goes.

        Emitted event names are shared across providers so the UI never
        branches on backend: `thinking_start`, `thinking`, `text`,
        `tool_pending`.
        """

    # ---- health ---------------------------------------------------------
    def check(self) -> str:
        """Return a human-readable readiness line, or raise ProviderError."""
        return f"{self.name}: {self.model}"


def user_blocks(text: str = "",
                images: "str | list[str] | None" = None) -> list[dict[str, Any]]:
    """Build neutral user blocks from a text box and any number of data URLs.

    Images come first: every backend reads an annotated capture better when it
    arrives before the sentence that refers to it. When several are attached
    they are labelled, because "the second one" is unresolvable otherwise.
    """
    if images is None:
        images = []
    elif isinstance(images, str):
        images = [images] if images else []

    blocks: list[dict[str, Any]] = []
    for index, data_url in enumerate(images, start=1):
        if not data_url:
            continue
        header, _, data = data_url.partition(",")
        if not data:
            continue
        media = "image/png" if "png" in header else "image/jpeg"
        if len(images) > 1:
            blocks.append({"type": "text", "text": f"Image {index} of {len(images)}:"})
        blocks.append({"type": "image", "media_type": media, "data": data})
    if text.strip():
        blocks.append({"type": "text", "text": text.strip()})
    return blocks


def truncate(payload: Any, limit: int = 24000) -> str:
    import json

    text = json.dumps(payload, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text)} chars total]"
