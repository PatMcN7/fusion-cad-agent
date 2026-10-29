"""OpenAI-compatible adapter -- covers OpenAI and every server that speaks
its Chat Completions dialect.

Targeting Chat Completions rather than the newer Responses API is deliberate:
it is the dialect the whole ecosystem implements, so one adapter plus a
base_url reaches Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Groq,
Together, DeepSeek, and xAI. Local models are not a separate backend here;
they are this backend pointed somewhere else.

Two shape differences from Anthropic drive most of the code:

  * tool arguments arrive as a JSON *string*, streamed in fragments that must
    be concatenated per call index before parsing;
  * a `role: "tool"` message is text-only, so a viewport capture cannot ride
    along with the result it belongs to. It is appended as a following user
    turn instead, labelled so the model can still tell what it depicts.

Written from the published Chat Completions API, not from a loaded vendor
reference -- verify against a live endpoint before trusting it.
"""

from __future__ import annotations

import json
from typing import Any

from .base import Emit, Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, truncate


STOP_REASONS = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "length",
    "content_filter": "refusal",
}


def _explain(exc: Exception) -> str:
    """Turn a vendor exception into something that says what to actually do.

    The SDK raises RateLimitError for an exhausted balance as well as for a
    real rate limit. The two need opposite responses -- one is "wait", the
    other is "no amount of waiting will help" -- so separate them by the
    error code rather than the exception class.
    """
    body = getattr(exc, "body", None) or {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code = error.get("code") or error.get("type") or ""
    if code in ("credit_balance_exhausted", "insufficient_quota"):
        return ("this API account has no credits. Note that a ChatGPT "
                "subscription is billed separately and grants none -- add "
                "credits at platform.openai.com/settings/organization/billing")
    if code == "model_not_found":
        return (f"{error.get('message', 'model not available to this key')} "
                "(check the model id against /v1/models)")
    if code == "invalid_api_key":
        return "the API key was rejected; check OPENAI_API_KEY"
    return f"{exc.__class__.__name__}: {error.get('message') or exc}"


class OpenAIProvider(Provider):
    name = "openai"
    supports_images = True
    tool_result_images = False       # role:"tool" content is text-only
    supports_thinking = False

    def __init__(self, model: str = "gpt-4o", **kwargs: Any) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "The 'openai' package is not installed. "
                "pip install openai"
            ) from exc

        self._sdk = openai
        options: dict[str, Any] = {}
        if kwargs.get("api_key"):
            options["api_key"] = kwargs["api_key"]
        if kwargs.get("base_url"):
            options["base_url"] = kwargs["base_url"]
        # Local servers routinely ignore the key but the client demands one.
        options.setdefault("api_key", "not-needed")
        self.client = openai.OpenAI(**options)
        super().__init__(model, **kwargs)

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def add_user(self, blocks: list[dict[str, Any]]) -> None:
        content: list[dict[str, Any]] = []
        for block in blocks:
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                media = block.get("media_type", "image/png")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{media};base64,{block['data']}"}})
        if content:
            self.messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        images: list[tuple[str, ToolResult]] = []
        for result in results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": result.id,
                "content": result.as_text(),
            })
            if result.image_blocks():
                images.append((result.name, result))

        # Tool messages carry no images, so the captures follow as a user turn.
        if images:
            content: list[dict[str, Any]] = [{
                "type": "text",
                "text": "Viewport after "
                        + ", ".join(name for name, _ in images) + ":",
            }]
            for name, result in images:
                for img in result.image_blocks():
                    content.append({"type":"text","text":img.get("label",name)})
                    content.append({"type":"image_url","image_url":{
                        "url":f"data:{img['media_type']};base64,{img['data']}"}})
            self.messages.append({"role": "user", "content": content})

    def stream(self, system: str, tools: list[ToolSpec], emit: Emit) -> Turn:
        definitions = [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.json_schema(),
            },
        } for tool in tools]

        from agent_runtime import compact_messages
        compact_messages(self.messages)
        payload = [{"role": "system", "content": system}] + self.messages
        try:
            stream = self._create(
                model=self.model,
                messages=payload,
                tools=definitions or None,
                stream=True,
            )
        except Exception as exc:
            raise ProviderError(self._explain(exc)) from exc

        text_parts: list[str] = []
        reasoning_parts: dict[str, list[str]] = {}
        reasoning_details: list[dict[str, Any]] = []
        # Tool call fragments arrive indexed and out of order; accumulate.
        partial: dict[int, dict[str, str]] = {}
        finish = "stop"
        announced: set[int] = set()
        thinking_open = False
        usage: dict[str, Any] = {}

        try:
            for chunk in stream:
                # The usage chunk carries no choices, so read it before
                # skipping past chunks that have none.
                if getattr(chunk, "usage", None):
                    usage = self._usage(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish = choice.finish_reason

                # Tool use resumes this assistant response. Preserve the router's
                # reasoning sequence rather than only displaying it in the UI.
                for field in ("reasoning", "reasoning_content"):
                    value = getattr(delta, field, None)
                    if isinstance(value, str):
                        reasoning_parts.setdefault(field, []).append(value)
                for detail in (getattr(delta, "reasoning_details", None) or []):
                    reasoning_details.append(detail.model_dump() if hasattr(detail, "model_dump") else dict(detail))

                # Reasoning models expose their trace under a vendor-specific
                # field; surface it the same way as Anthropic thinking.
                trace = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None)
                if trace:
                    if not thinking_open:
                        emit("thinking_start", {})
                        thinking_open = True
                    emit("thinking", {"text": trace})

                if delta.content:
                    text_parts.append(delta.content)
                    emit("text", {"text": delta.content})

                for call in (delta.tool_calls or []):
                    slot = partial.setdefault(
                        call.index, {"id": "", "name": "", "arguments": ""})
                    if call.id:
                        slot["id"] = call.id
                    if call.function and call.function.name:
                        slot["name"] = call.function.name
                        if call.index not in announced:
                            announced.add(call.index)
                            emit("tool_pending", {"name": call.function.name})
                    if call.function and call.function.arguments:
                        slot["arguments"] += call.function.arguments
        except Exception as exc:
            raise ProviderError(f"stream failed: {exc}") from exc

        tool_calls: list[ToolCall] = []
        native_calls: list[dict[str, Any]] = []
        for index in sorted(partial):
            slot = partial[index]
            if not slot["name"]:
                continue
            raw = slot["arguments"] or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                # A malformed fragment must not kill the turn; hand the model
                # an error result so it can retry with valid arguments.
                arguments = {"__parse_error__": raw[:400]}
            identifier = slot["id"] or f"call_{index}"
            tool_calls.append(ToolCall(identifier, slot["name"], arguments))
            native_calls.append({
                "id": identifier, "type": "function",
                "function": {"name": slot["name"], "arguments": raw},
            })

        assistant: dict[str, Any] = {"role": "assistant",
                                     "content": "".join(text_parts) or None}
        if native_calls:
            assistant["tool_calls"] = native_calls
        if reasoning_details:
            assistant["reasoning_details"] = reasoning_details
        else:
            # Plaintext-only providers use one of these equivalent fields.
            for field, parts in reasoning_parts.items():
                assistant[field] = "".join(parts)
        self.messages.append(assistant)

        return Turn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=STOP_REASONS.get(finish, "other")
            if finish in {"length", "content_filter"} or not tool_calls else "tool_use",
            usage=usage,
        )

    # ---- overridable seams -----------------------------------------------
    #
    # Subclasses differ from plain OpenAI in how a request is issued and how
    # a failure reads, not in how a stream is consumed. Naming those two
    # points keeps an adapter like OpenRouter a few dozen lines instead of a
    # copy of this one.

    def _create(self, **payload: Any):
        return self.client.chat.completions.create(**payload)

    def _explain(self, exc: Exception) -> str:
        return _explain(exc)

    def _usage(self, raw: Any) -> dict[str, Any]:
        return {
            "input_tokens": getattr(raw, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(raw, "completion_tokens", 0) or 0,
        }

    def check(self) -> str:
        try:
            self.client.models.retrieve(self.model)
        except Exception as exc:
            # Many OpenAI-compatible servers do not implement /models/{id};
            # a listing is a good enough reachability probe.
            try:
                names = [m.id for m in self.client.models.list().data]
            except Exception:
                raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc
            if self.model not in names:
                raise ProviderError(
                    f"model '{self.model}' not offered by this endpoint; "
                    f"available: {', '.join(names[:8])}")
        return f"openai-compatible / {self.model}" + (
            f" @ {self.base_url}" if self.base_url else "")
