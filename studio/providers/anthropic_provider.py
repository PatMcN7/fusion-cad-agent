"""Anthropic adapter.

Written against the Anthropic SDK reference, and the only adapter here whose
API shapes are authoritative rather than reconstructed.

Two capabilities this backend has that the others do not, and which the agent
loop exploits when available:

  * a tool_result may contain image blocks, so a viewport capture attaches to
    the operation that produced it rather than arriving as a loose user turn;
  * adaptive thinking, surfaced as summarized text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Emit, Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, truncate


STOP_REASONS = {
    "end_turn": "end",
    "tool_use": "tool_use",
    "refusal": "refusal",
    "max_tokens": "length",
}


class AnthropicProvider(Provider):
    name = "anthropic"
    supports_images = True
    tool_result_images = True
    supports_thinking = True

    def __init__(self, model: str = "claude-opus-5", **kwargs: Any) -> None:
        import os

        import anthropic

        self._sdk = anthropic
        # The SDK also accepts an `ant auth login` profile, so only object when
        # no credential source of any kind is present.
        if not kwargs.get("api_key") and not any(
            os.environ.get(v) for v in
            ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
        ) and not (Path.home() / ".config" / "anthropic" / "credentials").exists():
            raise ProviderError(
                "no Anthropic credentials found -- set ANTHROPIC_API_KEY, "
                "or run `ant auth login`")
        options: dict[str, Any] = {}
        if kwargs.get("api_key"):
            options["api_key"] = kwargs["api_key"]
        if kwargs.get("base_url"):
            options["base_url"] = kwargs["base_url"]
        self.client = anthropic.Anthropic(**options)
        super().__init__(model, **kwargs)

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def add_user(self, blocks: list[dict[str, Any]]) -> None:
        content = []
        for block in blocks:
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                content.append({"type": "image", "source": {
                    "type": "base64",
                    "media_type": block.get("media_type", "image/png"),
                    "data": block["data"],
                }})
        if content:
            self.messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        blocks: list[dict[str, Any]] = []
        for result in results:
            content: list[dict[str, Any]] = [
                {"type": "text", "text": result.as_text()}
            ]
            for img in result.image_blocks():
                content.append({"type": "text",
                                "text": img.get("label","Image from this operation:")})
                content.append({"type": "image", "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                }})
            blocks.append({
                "type": "tool_result",
                "tool_use_id": result.id,
                "content": content,
                "is_error": not result.ok,
            })
        if blocks:
            self.messages.append({"role": "user", "content": blocks})

    def stream(self, system: str, tools: list[ToolSpec], emit: Emit) -> Turn:
        definitions = [{
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.json_schema(),
        } for tool in tools]

        # Anthropic requires max_tokens. Use the model's advertised maximum,
        # not a harness budget; the loop continues any provider truncation.
        maximum = getattr(self, "_model_output_maximum", None)
        if maximum is None:
            self.check()
            maximum = self._model_output_maximum
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": maximum,
            # Cache the system prompt: it is byte-stable across every turn,
            # and the tool list renders ahead of it.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": definitions,
            "messages": self.messages,
            "output_config": {"effort": self.effort},
            "thinking": {"type": "adaptive", "display": "summarized"},
        }

        try:
            with self.client.messages.stream(**request) as stream:
                for event in stream:
                    if event.type == "content_block_start":
                        kind = event.content_block.type
                        if kind == "thinking":
                            emit("thinking_start", {})
                        elif kind == "tool_use":
                            emit("tool_pending", {"name": event.content_block.name})
                    elif event.type == "content_block_delta":
                        delta = event.delta.type
                        if delta == "thinking_delta":
                            emit("thinking", {"text": event.delta.thinking})
                        elif delta == "text_delta":
                            emit("text", {"text": event.delta.text})
                response = stream.get_final_message()
        except self._sdk.APIStatusError as exc:
            raise ProviderError(f"Anthropic {exc.status_code}: {exc.message}") from exc
        except self._sdk.APIConnectionError as exc:
            raise ProviderError(f"Anthropic connection failed: {exc}") from exc

        # Echo assistant content back verbatim -- thinking blocks must
        # round-trip unmodified or the next turn is rejected.
        self.messages.append({
            "role": "assistant",
            "content": [block.model_dump() for block in response.content],
        })

        turn = Turn(stop_reason=STOP_REASONS.get(response.stop_reason, "other"))
        for block in response.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )
        usage = response.usage
        turn.usage = {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
        return turn

    def check(self) -> str:
        try:
            info = self.client.models.retrieve(self.model)
        except Exception as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc
        maximum = getattr(info, "max_tokens", None)
        if not isinstance(maximum, int) or maximum <= 0:
            raise ProviderError("Anthropic did not report this model's output maximum; update the SDK/model metadata before running.")
        self._model_output_maximum = maximum
        return f"anthropic / {info.id} ({info.max_input_tokens:,} ctx)"
