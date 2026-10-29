#!/usr/bin/env python3
"""Compare OpenRouter models on this repo's actual work, not on a leaderboard.

Two things decide whether a cheap model can drive this harness, and neither
shows up in a benchmark score:

  worker  -- can it pick the right tool and fill in nested arguments (a list
             of polygons, a unit expression) without mangling them?
  vision  -- looking at a render, can it tell a true isogrid from rows of
             separate triangles? That distinction is the one that went
             undetected through an entire session.

So ask them exactly that, with the real system prompt and the real tool
registry, and print what each answer cost. No model is called unless you name
it, and nothing here touches Fusion -- the worker probe stops at the tool
call and never executes it.

    python3 studio/bench.py --worker qwen/qwen3.7-flash z-ai/glm-5.3-flash
    python3 studio/bench.py --vision google/gemini-3.7-flash --image shot.png
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import envfile  # noqa: E402

# The keys live in studio/.env, same as the server reads. Without this the
# bench reports "no key" while the app it is meant to be probing works fine.
envfile.load(HERE / ".env")

import providers  # noqa: E402
from providers import ProviderError, ToolSpec  # noqa: E402

# A request that is unambiguous to a competent model and has exactly one
# right tool call, including a nested-array argument and unit expressions.
WORKER_TASK = (
    "Pocket the body named 'Bellypan' with a hexagonal lattice: 3 inch cells, "
    "quarter inch ribs, cut all the way through, and round the pocket corners "
    "for a 1/8 inch end mill. Make the call; do not explain first."
)
EXPECTED_TOOL = "lattice_pockets"
EXPECTED_ARGS = {
    "pattern": "hex",
    "cell_size": ("3 in", "3in", "3.0 in"),
    "rib_width": ("0.25 in", ".25 in", "1/4 in", "0.25in"),
    "cutter_diameter": ("0.125 in", ".125 in", "1/8 in", "0.125in"),
}


def _noop(event, data):
    pass


def _registry() -> list[ToolSpec]:
    """Real tool specs from the live bridge, or the static registry if Fusion
    is not running -- the probe should not require an open CAD session."""
    try:
        from fusion_cad import bridge_client
        registry = bridge_client.Bridge().registry()
    except Exception:
        from fusion_cad import tools
        registry = tools.TOOLS
    specs = []
    for name, spec in sorted(registry.items()):
        properties = {
            key: (dict(text) if isinstance(text, dict)
                  else {"type": "string", "description": str(text)})
            for key, text in (spec.get("arguments") or {}).items()
        }
        specs.append(ToolSpec(name=name, description=spec.get("description", ""),
                              properties=properties))
    return specs


def _brief() -> str:
    from fusion_cad import catalog
    return catalog.brief()


def _score_worker(turn) -> tuple[str, str]:
    if not turn.tool_calls:
        return "FAIL", "made no tool call"
    call = turn.tool_calls[0]
    if call.name != EXPECTED_TOOL:
        return "FAIL", f"called {call.name}"
    misses = []
    for key, accepted in EXPECTED_ARGS.items():
        got = str(call.arguments.get(key, "")).strip().lower()
        options = (accepted,) if isinstance(accepted, str) else accepted
        if not any(got == o.lower() for o in options):
            misses.append(f"{key}={call.arguments.get(key)!r}")
    if misses:
        return "PARTIAL", "; ".join(misses)
    return "PASS", f"{len(turn.tool_calls)} call(s), arguments exact"


def run_worker(model: str, provider: str, effort: str) -> None:
    specs, brief = _registry(), _brief()
    try:
        backend = providers.build(provider, model, effort=effort)
        backend.add_user([{"type": "text", "text": WORKER_TASK}])
        started = time.time()
        turn = backend.stream(brief, specs, _noop)
    except ProviderError as exc:
        print(f"  {model:44} ERROR  {exc}")
        return
    verdict, detail = _score_worker(turn)
    usage = turn.usage or {}
    cost = usage.get("cost_usd")
    print(f"  {model:44} {verdict:7} {time.time() - started:5.1f}s "
          f"{('$%.5f' % cost) if cost is not None else '     ?'}  {detail}")
    if turn.tool_calls:
        print(f"    -> {json.dumps(turn.tool_calls[0].arguments, sort_keys=True)[:150]}")


def run_vision(model: str, provider: str, image: pathlib.Path, effort: str) -> None:
    import vision

    describer = vision.Describer(provider, model, effort=effort)
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    started = time.time()
    text = describer.describe(encoded, "This is the model after a lattice was cut.")
    elapsed = time.time() - started
    backend = describer._provider
    cost = getattr(backend, "last_cost", None)
    print(f"\n  {model}  ({elapsed:.1f}s"
          + (f", ${cost:.5f}" if cost is not None else "") + ")")
    for line in text.splitlines():
        print(f"    {line}")


def survey(limit: int, worker_only: bool) -> None:
    """What OpenRouter is actually serving right now, ranked by usage.

    Published benchmarks lag model releases badly -- three of the candidates
    worth considering here had no findable eval numbers days after launch.
    Weekly usage is not a quality score, but it is a real signal that is
    always current, and it is the one that says which ids are alive.
    """
    import urllib.request

    url = "https://openrouter.ai/api/v1/models?order=top-weekly"
    with urllib.request.urlopen(url, timeout=20) as response:
        models = json.load(response)["data"]

    def rate(model, key):
        try:
            return float(model["pricing"].get(key) or 0) * 1e6
        except (TypeError, ValueError):
            return 0.0

    print(f"\n  {'#':>3}  {'model':44} {'in':>7} {'out':>7} {'ctx':>7}  caps")
    shown = 0
    for rank, model in enumerate(models, 1):
        supported = set(model.get("supported_parameters", []))
        modalities = model["architecture"].get("input_modalities", [])
        if worker_only and "tools" not in supported:
            continue
        if rate(model, "prompt") <= 0:
            continue                      # free tiers distort the ranking
        caps = ("tools " if "tools" in supported else "      ") + \
               ("vision " if "image" in modalities else "       ") + \
               ("reason" if "reasoning" in supported else "")
        print(f"  {rank:3d}  {model['id'][:44]:44} "
              f"{rate(model, 'prompt'):7.2f} {rate(model, 'completion'):7.2f} "
              f"{model['context_length'] // 1000:6d}K  {caps}")
        shown += 1
        if shown >= limit:
            break
    print("\n  Usage rank is adoption, not quality. Probe the shortlist with"
          "\n  --worker / --vision before trusting any of it.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", nargs="*", default=[],
                        help="model ids to probe for tool calling")
    parser.add_argument("--vision", nargs="*", default=[],
                        help="model ids to probe for reading a capture")
    parser.add_argument("--image", type=pathlib.Path,
                        help="screenshot for the vision probe")
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--survey", type=int, nargs="?", const=25, default=None,
                        metavar="N",
                        help="list the N most-used OpenRouter models with "
                             "price and capabilities")
    parser.add_argument("--all", action="store_true",
                        help="with --survey, include models that cannot call tools")
    args = parser.parse_args()

    if args.survey:
        survey(args.survey, worker_only=not args.all)
        if not args.worker and not args.vision:
            return 0

    if not args.worker and not args.vision:
        parser.print_help()
        return 2

    if args.worker:
        print(f"\nWorker probe -- {len(_registry())} tools, "
              f"{len(_brief())} char brief\n  task: {WORKER_TASK[:70]}...\n")
        for model in args.worker:
            run_worker(model, args.provider, args.effort)

    if args.vision:
        if not args.image or not args.image.is_file():
            print("\n--vision needs --image pointing at a capture PNG")
            return 2
        print(f"\nVision probe -- {args.image.name}")
        for model in args.vision:
            run_vision(model, args.provider, args.image, args.effort)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
