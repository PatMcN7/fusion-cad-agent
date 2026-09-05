"""Google Gemini adapter (google-genai SDK).

Shape differences worth knowing:

  * turns are `contents` with roles "user" and "model" (not "assistant"), and
    every payload is a list of `parts`;
  * a tool call is a `function_call` part with `args` already decoded, so
    unlike OpenAI there is no JSON string to reassemble;
  * a tool result is a `function_response` part addressed by tool *name*, not
    by call id -- so parallel calls to the same tool in one turn cannot be
    told apart, and the agent loop must not rely on id round-tripping here;
  * function_response carries no image, so captures follow as a user turn.

Written from the published google-genai API, not from a loaded vendor
reference -- verify against a live key before trusting it.
"""

from __future__ import annotations

from typing import Any

from .base import Emit, Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, truncate

MAX_TOKENS = 16000

# Gemini rejects JSON Schema keywords it does not implement.
ALLOWED_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}


def _clean_schema(node: Any) -> Any:
    """Strip unsupported JSON Schema keywords, recursively.

    The keyword filter must never be applied to the `properties` map itself:
    its keys are parameter *names*, not schema keywords, so filtering there
    deletes every parameter and ships a tool that takes no arguments.
    """
    if not isinstance(node, dict):
        return [_clean_schema(item) for item in node] if isinstance(node, list) else node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            # Keys here are parameter names -- recurse into the values only.
            out[key] = {name: _clean_schema(sub) for name, sub in value.items()}
        elif key == "required" and isinstance(value, list):
            out[key] = list(value)
        else:
            out[key] = _clean_schema(value)

    # Gemini requires an explicit type on every node.
    if "properties" in out or out.get("type") == "object":
        out.setdefault("type", "object")
    elif "type" not in out:
        out["type"] = "string"
    return out


class GeminiProvider(Provider):
    name = "gemini"
    supports_images = True
    tool_result_images = False
    supports_thinking = True

    def __init__(self, model: str = "gemini-2.5-pro", **kwargs: Any) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "The 'google-genai' package is not installed. "
                "pip install google-genai"
            ) from exc

        self._genai = genai
        self._types = types
        options: dict[str, Any] = {}
        if kwargs.get("api_key"):
            options["api_key"] = kwargs["api_key"]
        self.client = genai.Client(**options)
        super().__init__(model, **kwargs)

    def reset(self) -> None:
        self.contents: list[Any] = []

    def _part_text(self, text: str) -> Any:
        return self._types.Part(text=text)

    def add_user(self, blocks: list[dict[str, Any]]) -> None:
        import base64

        parts = []
        for block in blocks:
            if block["type"] == "text":
                parts.append(self._part_text(block["text"]))
            elif block["type"] == "image":
                parts.append(self._types.Part.from_bytes(
                    data=base64.b64decode(block["data"]),
                    mime_type=block.get("media_type", "image/png"),
                ))
        if parts:
            self.contents.append(self._types.Content(role="user", parts=parts))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        import base64

        parts = [
            self._types.Part.from_function_response(
                name=result.name,
                response={"result": result.as_text(12000)},
            )
            for result in results
        ]
        if parts:
            self.contents.append(self._types.Content(role="user", parts=parts))

        captures = [r for r in results if r.image_b64]
        if captures:
            image_parts = [self._part_text(
                "Viewport after " + ", ".join(r.name for r in captures) + ":")]
            for result in captures:
                image_parts.append(self._types.Part.from_bytes(
                    data=base64.b64decode(result.image_b64),
                    mime_type=result.image_media_type,
                ))
            self.contents.append(
                self._types.Content(role="user", parts=image_parts))

    def stream(self, system: str, tools: list[ToolSpec], emit: Emit) -> Turn:
        declarations = [{
            "name": tool.name,
            "description": tool.description[:1000],
            "parameters": _clean_schema(tool.json_schema()),
        } for tool in tools]

        config = self._types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=MAX_TOKENS,
            tools=[self._types.Tool(function_declarations=declarations)],
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        collected: list[Any] = []
        finish = "stop"

        try:
            for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=self.contents, config=config,
            ):
                for candidate in (chunk.candidates or []):
                    if candidate.finish_reason:
                        finish = str(candidate.finish_reason)
                    content = candidate.content
                    if not content or not content.parts:
                        continue
                    for part in content.parts:
                        collected.append(part)
                        if getattr(part, "thought", None) and part.text:
                            emit("thinking", {"text": part.text})
                        elif getattr(part, "text", None):
                            text_parts.append(part.text)
                            emit("text", {"text": part.text})
                        call = getattr(part, "function_call", None)
                        if call:
                            emit("tool_pending", {"name": call.name})
                            # Gemini addresses results by name, so synthesize a
                            # stable id for the harness and never send it back.
                            tool_calls.append(ToolCall(
                                id=f"{call.name}_{len(tool_calls)}",
                                name=call.name,
                                arguments=dict(call.args or {}),
                            ))
        except Exception as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc

        if collected:
            self.contents.append(
                self._types.Content(role="model", parts=collected))

        stop = "tool_use" if tool_calls else (
            "length" if "MAX_TOKENS" in finish.upper() else
            "refusal" if "SAFETY" in finish.upper() else "end")
        return Turn(text="".join(text_parts), tool_calls=tool_calls, stop_reason=stop)

    def check(self) -> str:
        try:
            info = self.client.models.get(model=self.model)
        except Exception as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc
        return f"gemini / {getattr(info, 'name', self.model)}"
