"""Turn a viewport capture into a sentence, once, with a model chosen for it.

Every mutating tool feeds its result back with a picture of the model, which
is the only signal that says whether the part *looks* like what was asked
for. But an image is not a message -- it is a permanent tenant. A logged
session ran 1.93M input tokens against 7K of output, because fourteen
captures joined the transcript and every one of them was re-sent on every
turn that followed.

A description does the same job for the rest of the session at a fiftieth of
the size. So the capture goes to a vision model on its own, with no history
attached, and what enters the conversation is the answer rather than the
photograph.

Two consequences worth stating:

- The vision call is **stateless**. It carries one image and one prompt, not
  the transcript, so it is cheap even on a frontier model -- which is exactly
  where you want the frontier model, and it frees the main loop to run
  something small and fast.
- **Only automatic captures are summarized.** An image the person attached
  and drew on stays an image. They are pointing at something, and a
  paraphrase would discard the very thing the arrow was for.
"""

from __future__ import annotations

import os

from providers import Provider, ProviderError, build

# Written for a reader who cannot see the previous picture and must decide
# whether to keep building. Wrong-looking geometry is the payload; praise is
# not.
PROMPT = """You are inspecting an image from a CAD session: usually a
screenshot of a model in Autodesk Fusion, sometimes a vendor drawing or a
datasheet page that was fetched to get real dimensions.

If it is a drawing rather than a viewport, say so and read it: report the
dimensions, hole patterns, thread callouts and part numbers you can actually
make out, and say which are too small to read rather than guessing.

For a Fusion viewport:

Describe what is actually visible in at most six short lines:
- the overall shape and roughly how many distinct bodies
- any pattern or lattice: its cell shape, whether cells share vertices
  (a true isogrid) or merely sit near each other (rows of separate holes),
  and whether it is centred on the face
- anything that looks wrong: asymmetry, a feature that did not appear,
  parts intersecting, a cut in the wrong direction, an off-centre field

If the arguments the operation was called with are given below, check what
you see against them and say plainly whether they match.

Report only what you can see. Do not guess dimensions beyond comparing against
what was asked for, do not offer next steps, do not congratulate. If something
looks wrong, lead with it."""


def _noop(event: str, data) -> None:
    pass


class Describer:
    """A vision model held apart from the conversation."""

    def __init__(self, provider: str, model: str, *,
                 base_url: str | None = None, effort: str = "low") -> None:
        self.provider_name = provider
        self.model = model
        self._provider: Provider | None = None
        self._build = lambda: build(provider, model, base_url=base_url, effort=effort)

    def _ready(self) -> Provider:
        if self._provider is None:
            provider = self._build()
            if getattr(provider, "is_agent", False):
                # An agent backend runs its own tool loop; asking it to look
                # at one picture would start a whole session.
                raise ProviderError(
                    f"{provider.name} is an agent backend and cannot serve as "
                    "the vision role")
            if not provider.supports_images:
                raise ProviderError(f"{provider.name}/{provider.model} cannot see images")
            self._provider = provider
        return self._provider

    def describe(self, image_b64: str, note: str = "",
                 media_type: str = "image/png") -> str:
        """One image in, a few lines out. Never raises: this rides on a
        tool result, and a description failing must not fail the build."""
        try:
            provider = self._ready()
            # Fresh history every time. The whole point is that this call
            # never accumulates -- and independent looks do not bias each
            # other toward "same as before".
            provider.reset()
            blocks = [{"type": "image", "media_type": media_type, "data": image_b64}]
            if note:
                blocks.append({"type": "text", "text": note})
            provider.add_user(blocks)
            turn = provider.stream(PROMPT, [], _noop)
            return turn.text.strip()
        except ProviderError as exc:
            return f"[vision unavailable: {exc}]"
        except Exception as exc:  # a vendor SDK failing mid-stream
            return f"[vision failed: {exc.__class__.__name__}: {exc}]"


def from_environment() -> tuple[Describer | None, str]:
    """Build the describer the environment asks for.

    STUDIO_VISION  = off | describe | both   (default: describe)
    STUDIO_VISION_MODEL / _PROVIDER          (default: Claude Opus 5)

    `both` keeps the image *and* adds the description -- useful while you
    are still deciding whether the descriptions are good enough, and the
    only mode that costs more than doing nothing.
    """
    mode = os.environ.get("STUDIO_VISION", "describe").strip().lower()
    if mode not in ("off", "describe", "both"):
        mode = "describe"
    if mode == "off":
        return None, mode
    return Describer(
        os.environ.get("STUDIO_VISION_PROVIDER", "anthropic"),
        os.environ.get("STUDIO_VISION_MODEL", "claude-opus-5"),
        base_url=os.environ.get("STUDIO_VISION_BASE_URL") or None,
        effort=os.environ.get("STUDIO_VISION_EFFORT", "low"),
    ), mode
