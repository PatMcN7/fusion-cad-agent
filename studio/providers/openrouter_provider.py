"""OpenRouter adapter -- one key, every model, priced per call.

OpenRouter speaks Chat Completions, so this is the OpenAI adapter pointed at
a different host. What it adds is the three things that are actually
different about a router:

1. **Capability pre-flight.** OpenRouter offers hundreds of models and they
   do not have the same abilities. Some cannot call tools. Some cannot see
   images. Picking one of those as the worker fails on turn one, or worse,
   fails silently twenty turns in when the first capture arrives. The model
   catalogue is public and unauthenticated, so `check()` reads it and says
   what the chosen model can do *before* a session starts.

2. **Real cost, reported.** OpenRouter returns the actual dollar cost of
   each call when asked. The whole reason for running here is to stop
   guessing what a design session costs, so ask for it and surface it.

3. **Reasoning effort**, passed only to models that accept it. Sending
   `reasoning` to a model without it is a 400 on some upstreams.

Attribution headers are optional but conventional; they identify this app on
the OpenRouter dashboard so spend can be traced to it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import ProviderError
from .openai_provider import OpenAIProvider

BASE_URL = "https://openrouter.ai/api/v1"
CATALOGUE = f"{BASE_URL}/models"
REFERER = "https://github.com/local/fusion-cad-agent"
TITLE = "Fusion CAD Agent"

# One fetch per process. The catalogue is ~700KB and changes on the order of
# days, not seconds; re-reading it per turn would cost more than it tells.
_CATALOGUE_CACHE: dict[str, dict[str, Any]] | None = None


def catalogue(timeout: int = 15) -> dict[str, dict[str, Any]]:
    """Model id -> metadata. Empty on any failure: this is an enhancement,
    never a precondition for running."""
    global _CATALOGUE_CACHE
    if _CATALOGUE_CACHE is not None:
        return _CATALOGUE_CACHE
    try:
        request = urllib.request.Request(
            CATALOGUE, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        _CATALOGUE_CACHE = {m["id"]: m for m in data.get("data", [])}
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        _CATALOGUE_CACHE = {}
    return _CATALOGUE_CACHE


def capabilities(model: str) -> dict[str, Any]:
    """What this model can do, from the published catalogue."""
    entry = catalogue().get(model)
    if not entry:
        return {"known": False}
    architecture = entry.get("architecture", {})
    supported = set(entry.get("supported_parameters", []))
    pricing = entry.get("pricing", {})

    def rate(key: str) -> float:
        try:
            return float(pricing.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "known": True,
        "name": entry.get("name", model),
        "tools": "tools" in supported,
        "images": "image" in architecture.get("input_modalities", []),
        "reasoning": "reasoning" in supported,
        "context": entry.get("context_length", 0),
        "input_per_m": rate("prompt") * 1e6,
        "output_per_m": rate("completion") * 1e6,
        "cache_read_per_m": rate("input_cache_read") * 1e6,
    }


def _vision_mode() -> str:
    """Whether captures reach the worker as pictures or as prose.

    Read here rather than imported so this adapter stays usable outside the
    studio -- the bench script builds providers directly.
    """
    return os.environ.get("STUDIO_VISION", "describe").strip().lower()


def _explain(exc: Exception) -> str:
    """OpenRouter's failures have their own vocabulary; translate it."""
    body = getattr(exc, "body", None) or {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = str(error.get("message") or exc)
    status = getattr(exc, "status_code", None) or error.get("code")

    if status == 402 or "insufficient" in message.lower() or "credit" in message.lower():
        return ("this OpenRouter account is out of credits -- top up at "
                "openrouter.ai/settings/credits")
    if status == 401:
        return "OpenRouter rejected the key; check OPENROUTER_API_KEY"
    if status == 404 or "no endpoints" in message.lower():
        return (f"{message}. No provider is currently serving this model id; "
                "check it at openrouter.ai/models")
    if status == 429:
        return f"rate limited by the upstream provider: {message}"
    return f"{exc.__class__.__name__}: {message}"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"

    def __init__(self, model: str = "z-ai/glm-5.3-flash", **kwargs: Any) -> None:
        kwargs.setdefault("base_url", None)
        kwargs["base_url"] = kwargs["base_url"] or BASE_URL
        if not kwargs.get("api_key"):
            kwargs["api_key"] = os.environ.get("OPENROUTER_API_KEY") or None
        if not kwargs["api_key"]:
            raise ProviderError(
                "OPENROUTER_API_KEY is not set. Create a key at "
                "openrouter.ai/keys and put it in studio/.env")
        super().__init__(model, **kwargs)
        self.last_cost: float | None = None
        self.session_cost = 0.0

    # ---- generation ------------------------------------------------------

    def _usage(self, raw: Any) -> dict[str, Any]:
        """Token counts plus what a router alone can report: real spend.

        Guessing a session's cost from token counts and a price table is how
        you end up wrong about caching, per-image charges and provider
        surcharges. OpenRouter knows the answer, so use its number.
        """
        usage = super()._usage(raw)
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        if cached:
            usage["cached_input_tokens"] = cached
        cost = getattr(raw, "cost", None)
        if cost is not None:
            self.last_cost = float(cost)
            self.session_cost += self.last_cost
            usage["cost_usd"] = round(self.last_cost, 6)
            usage["session_cost_usd"] = round(self.session_cost, 6)
        return usage

    def _explain(self, exc: Exception) -> str:
        return _explain(exc)

    def _create(self, **payload: Any):
        """Hook used by the base adapter's stream() to issue the request."""
        capability = capabilities(self.model)
        extra: dict[str, Any] = {
            # Ask the upstream to report real spend on the final chunk.
            "usage": {"include": True},
        }
        if capability.get("reasoning") and self.effort:
            extra["reasoning"] = {"effort": self.effort}
        payload["extra_body"] = extra
        payload["extra_headers"] = {"HTTP-Referer": REFERER, "X-Title": TITLE}
        return self.client.chat.completions.create(**payload)

    # ---- health ----------------------------------------------------------

    def check(self) -> str:
        capability = capabilities(self.model)
        if not capability.get("known"):
            # The catalogue may be unreachable, or the id may be wrong. Do not
            # block on it -- but do not pretend it was verified either.
            return f"openrouter / {self.model} (not found in the catalogue)"

        notes = []
        if not capability["tools"]:
            notes.append("CANNOT CALL TOOLS -- unusable as the worker model")
        if not capability["images"] and _vision_mode() != "describe":
            # In the default `describe` mode the worker never receives an
            # image anyway -- the vision role reads every capture and hands
            # over text. A text-only worker is only a limitation when that
            # role is switched off, or for the person's own marked-up
            # screenshots, which get described instead.
            notes.append("cannot see images, and the vision role is not in "
                         "'describe' mode -- captures will not be readable")
        price = (f"${capability['input_per_m']:.2f}/${capability['output_per_m']:.2f} "
                 f"per Mtok")
        line = (f"openrouter / {self.model} -- {price}, "
                f"{capability['context'] // 1000}K ctx")
        return line + ("  [" + "; ".join(notes) + "]" if notes else "")
