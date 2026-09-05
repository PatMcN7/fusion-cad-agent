"""Provider registry.

Adapters are imported lazily: a missing optional SDK should surface as a clear
message when that backend is selected, not as an ImportError at startup that
takes the whole app down.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (  # noqa: F401
    Cancelled, Emit, Provider, ProviderError, ToolCall, ToolResult, ToolSpec,
    Turn, truncate, user_blocks,
)

# Presets are starting points, not a whitelist -- any model id the backend
# accepts can be typed in. base_url is what turns "openai" into a local runner.
PRESETS: dict[str, dict[str, Any]] = {
    "claude-opus-5":       {"provider": "anthropic", "label": "Claude Opus 5"},
    "claude-sonnet-5":     {"provider": "anthropic", "label": "Claude Sonnet 5"},
    "claude-haiku-4-5":    {"provider": "anthropic", "label": "Claude Haiku 4.5"},
    "gpt-5.6-sol":         {"provider": "openai",    "label": "GPT-5.6 Sol"},
    "gpt-5.6-terra":       {"provider": "openai",    "label": "GPT-5.6 Terra"},
    "gpt-5.5":             {"provider": "openai",    "label": "GPT-5.5"},
    "gpt-5.4-mini":        {"provider": "openai",    "label": "GPT-5.4 mini"},
    "gemini-2.5-pro":      {"provider": "gemini",    "label": "Gemini 2.5 Pro"},
    "gemini-2.5-flash":    {"provider": "gemini",    "label": "Gemini 2.5 Flash"},
    "codex:gpt-5.6-sol":   {"provider": "codex",     "label": "Codex · GPT-5.6 Sol (subscription)"},
    "codex:gpt-5.6-terra": {"provider": "codex",     "label": "Codex · GPT-5.6 Terra (subscription)"},
    # OpenRouter: one key, per-call billing, no subscription.
    #
    # Ranked by the Artificial Analysis Intelligence Index (v4.1.1) against
    # OpenRouter's live price and weekly-usage rank, checked 2026-08-26.
    # Index is a general-capability score, not a CAD score -- studio/bench.py
    # is what settles it on this repo's actual work.
    "z-ai/glm-5.3-flash":        {"provider": "openrouter",
                                  "label": "OR · GLM-5.3 Flash — index 57, $0.07/$0.25 (best value)"},
    "google/gemini-3.7-flash":   {"provider": "openrouter",
                                  "label": "OR · Gemini 3.7 Flash — index 56, $0.38/$1.88"},
    "qwen/qwen3.8-flash":        {"provider": "openrouter",
                                  "label": "OR · Qwen3.8 Flash — most-used on OR, $0.16/$0.47"},
    "openai/gpt-5.6-luna":       {"provider": "openrouter",
                                  "label": "OR · GPT-5.6 Luna — index 52, $0.20/$1.20"},
    "~deepseek/deepseek-v4-flash-latest": {"provider": "openrouter",
                                  "label": "OR · DeepSeek V4 Flash — index 52, $0.03/$0.10, text-only"},
    "qwen2.5-coder:32b":   {"provider": "openai",    "label": "Qwen2.5 Coder (local)",
                            "base_url": "http://localhost:11434/v1"},
    "llama3.3:70b":        {"provider": "openai",    "label": "Llama 3.3 (local)",
                            "base_url": "http://localhost:11434/v1"},
}

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    # Codex authenticates itself with the user's ChatGPT subscription; there
    # is no key for this process to hold.
    "codex": "",
}


def build(provider: str, model: str, *, base_url: str | None = None,
          api_key: str | None = None, effort: str = "high") -> Provider:
    """Construct an adapter. Raises ProviderError if its SDK is absent."""
    key = api_key or os.environ.get(ENV_KEYS.get(provider, ""), "") or None
    kwargs: dict[str, Any] = {"base_url": base_url, "api_key": key, "effort": effort}

    if provider not in ENV_KEYS:
        raise ProviderError(
            f"unknown provider '{provider}'; expected one of: "
            + ", ".join(sorted(ENV_KEYS)))

    # Vendor SDKs raise their own types on a missing key or bad option. Callers
    # only catch ProviderError, so normalize everything here -- a leaked
    # TypeError surfaces as a stack trace mid-stream instead of a clear message.
    try:
        if provider == "anthropic":
            from .anthropic_provider import AnthropicProvider
            return AnthropicProvider(model, **kwargs)
        if provider == "openai":
            from .openai_provider import OpenAIProvider
            return OpenAIProvider(model, **kwargs)
        if provider == "openrouter":
            from .openrouter_provider import OpenRouterProvider
            return OpenRouterProvider(model, **kwargs)
        if provider == "codex":
            from .codex_provider import CodexProvider
            # Presets namespace the model as "codex:<model>" to keep the
            # picker unambiguous; Codex itself wants the bare id.
            return CodexProvider(model.split(":", 1)[-1], **kwargs)
        from .gemini_provider import GeminiProvider
        return GeminiProvider(model, **kwargs)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"could not start {provider}/{model}: {exc.__class__.__name__}: {exc}"
        ) from exc


def available() -> dict[str, dict[str, Any]]:
    """Which backends can actually run here, and why not when they cannot."""
    import importlib.util

    import shutil

    # OpenRouter rides on the openai SDK -- same dialect, different host.
    modules = {"anthropic": "anthropic", "openai": "openai",
               "gemini": "google.genai", "openrouter": "openai"}
    report: dict[str, dict[str, Any]] = {}
    for provider, module in modules.items():
        installed = importlib.util.find_spec(module.split(".")[0]) is not None
        env = ENV_KEYS[provider]
        report[provider] = {
            "sdk_installed": installed,
            "env_var": env,
            "key_present": bool(os.environ.get(env)),
            "install": f"pip install {'google-genai' if provider == 'gemini' else module}",
        }
    # Codex needs no SDK and no key -- only the CLI, which carries its own auth.
    found = bool(shutil.which("codex"))
    report["codex"] = {
        "sdk_installed": found,
        "env_var": "",
        "key_present": found,
        "install": "install the Codex CLI",
    }
    return report
