#!/usr/bin/env python3
"""Take a recorded session apart.

The questions worth asking after a design session are all about proportion,
and none of them can be answered by reading the chat back:

    how much of that was building, and how much was the agent orienting?
    where did the wall clock actually go -- the model, or Fusion?
    what did it cost, and did the cheap model call tools as well as the
      expensive one?
    how often did a check fail and roll something back?

    python3 studio/analyze.py                 # the most recent session
    python3 studio/analyze.py --all           # every session, one line each
    python3 studio/analyze.py --compare       # grouped by model
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from transcript import transcript_dir  # noqa: E402

# What a tool call was *for*. The distinction that matters is between
# changing the model and finding out about it -- a session heavy on the
# second is a harness that failed to explain itself.
INSPECTION = {"design_summary", "active_document", "health", "inspect_threads",
              "inspect_commands", "check_interference", "mechanics_report"}
VERIFICATION = {"verify_spec"}
CAPTURE = {"capture_viewport"}


def sessions() -> list[pathlib.Path]:
    return sorted(transcript_dir().glob("session-*.jsonl"))


def load(path: pathlib.Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a session killed mid-write ends in a partial line
    return records


def summarize(path: pathlib.Path) -> dict:
    records = load(path)
    context = next((r["data"] for r in records if r["kind"] == "session_start"), {})

    calls: list[dict] = []
    open_calls: dict[str, dict] = {}
    usage = collections.Counter()
    cost = 0.0
    failed_checks: list[str] = []
    warnings: list[str] = []
    user_turns = 0
    assistant_chars = 0
    thinking_chars = 0
    errors: list[str] = []
    tool_seconds = 0.0
    active_seconds = 0.0
    user_start = None
    measured_active = 0.0
    current_turn_measured = False
    provider_seconds = collections.Counter()
    vision_usage = collections.Counter()
    vision_cost = 0.0
    attempts = 0

    for record in records:
        kind, data, at = record["kind"], record.get("data") or {}, record.get("t", 0)
        if kind == "user":
            user_turns += 1
            user_start = at
            current_turn_measured = False
        elif kind == "turn_end":
            measured_active += data.get("active_seconds", 0)
            current_turn_measured = True
        elif kind == "done":
            if user_start is not None:
                active_seconds += at-user_start
                user_start = None
        elif kind == "provider_timing":
            provider_seconds[data.get("role", "unknown")] += data.get("seconds", 0)
        elif kind == "vision_usage":
            for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                vision_usage[key] += data.get(key, 0) or 0
            if data.get("cost_usd") is not None: vision_cost += data["cost_usd"]
        elif kind == "text":
            assistant_chars += data.get("chars", 0)
        elif kind == "thinking":
            thinking_chars += data.get("chars", 0)
        elif kind == "tool_start":
            attempts += 1
            open_calls[data.get("id", "")] = {"name": data.get("name", "?"),
                                              "at": at,
                                              "mutating": data.get("mutating")}
        elif kind == "tool_end":
            started = open_calls.pop(data.get("id", ""), None)
            elapsed = at - started["at"] if started else 0.0
            tool_seconds += elapsed
            result = data.get("result") or {}
            calls.append({"name": data.get("name", "?"), "ok": data.get("ok"),
                          "seconds": elapsed,
                          "mutating": (started or {}).get("mutating")})
            if isinstance(result, dict):
                failed_checks.extend(result.get("checks_failed") or [])
                for check in (result.get("checks") or []):
                    if not check.get("ok") and check.get("severity") == "warning":
                        warnings.append(check["name"])
        elif kind == "usage":
            for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                usage[key] += data.get(key, 0) or 0
            if data.get("session_cost_usd") is not None:
                cost = max(cost, float(data["session_cost_usd"]))
            elif data.get("cost_usd"):
                cost += float(data["cost_usd"])
        elif kind == "error":
            errors.append(str(data.get("message", ""))[:120])

    wall = next((r["data"].get("wall_seconds") for r in reversed(records)
                 if r["kind"] == "session_end"),
                records[-1]["t"] if records else 0.0)
    unfinished = user_start is not None and not current_turn_measured
    # A killed process cannot emit turn_end. Count the observed interval as a
    # lower bound, rather than reporting that its failed work took zero time.
    unfinished_seconds = max(0.0, records[-1]["t"] - user_start) if unfinished else 0.0

    def bucket(name: str) -> str:
        if name in {"read_file", "search_files", "list_files", "write_file", "patch_file", "run_tests"}: return "source"
        if name in {"delete_entities", "delete_named_features", "undo_last", "edit_lattice"}: return "edit"
        if name in {"operation_status", "cancel_operation", "load_tools", "get_result"}: return "control"
        if name in INSPECTION or name in {"feature_tree", "inspect_entity", "list_documents"}:
            return "inspect"
        if name in CAPTURE:
            return "capture"
        if name in VERIFICATION:
            return "verify"
        return "BUILD"

    return {
        "path": path, "context": context, "calls": calls,
        "active_seconds": (measured_active or active_seconds) + unfinished_seconds,
        "unfinished_turn": unfinished,
        "attempts": attempts, "missing_completions": len(open_calls),
        "provider_seconds": provider_seconds, "vision_usage": vision_usage, "vision_cost": vision_cost,
        "buckets": collections.Counter(bucket(c["name"]) for c in calls),
        "usage": usage, "cost": cost, "wall": wall or 0.0,
        "tool_seconds": tool_seconds, "user_turns": user_turns,
        "assistant_chars": assistant_chars, "thinking_chars": thinking_chars,
        "failed_checks": failed_checks, "warnings": warnings, "errors": errors,
    }


def report(summary: dict) -> None:
    context, calls = summary["context"], summary["calls"]
    total = len(calls)
    print(f"\n  {summary['path'].name}")
    print(f"  worker {context.get('worker_provider')}/{context.get('worker_model')}"
          f"   vision {context.get('vision_provider')}/{context.get('vision_model')}"
          f" ({context.get('vision_mode')})")

    wall, tool_seconds = summary["wall"], summary["tool_seconds"]
    active = summary["active_seconds"]
    model_seconds = summary["provider_seconds"].get("worker")
    span = f"{wall / 60:.1f} min" if wall >= 90 else f"{wall:.0f}s"
    print(f"\n  {summary['user_turns']} user turns, {summary['attempts']} tool attempts, {active:.1f}s active ({wall:.1f}s session span)")
    print(f"  {tool_seconds:.2f}s in recorded tools; {summary['missing_completions']} missing completions")
    if model_seconds is not None:
        print(f"  worker requests {model_seconds:.2f}s; vision requests {summary['provider_seconds'].get('vision', 0):.2f}s")
    else:
        print("  Worker/vision timing not instrumented in this older transcript; unassigned time includes capture and provider overhead")

    if total:
        print("\n  what the calls were for")
        for name, count in summary["buckets"].most_common():
            bar = "#" * round(28 * count / total)
            print(f"    {name:8} {count:4d} {100 * count / total:3.0f}%  {bar}")

        frequency = collections.Counter(c["name"] for c in calls)
        print("\n  tools")
        for name, count in frequency.most_common(10):
            failures = sum(1 for c in calls if c["name"] == name and not c["ok"])
            print(f"    {count:3d}  {name:32}" + (f"  {failures} failed" if failures else ""))
        slowest = sorted(calls, key=lambda c: -c["seconds"])[:3]
        if slowest and slowest[0]["seconds"] > 1:
            print("  slowest: " + ", ".join(
                f"{c['name']} {c['seconds']:.0f}s" for c in slowest))

    usage = summary["usage"]
    if usage.get("input_tokens"):
        cached = usage.get("cached_input_tokens", 0)
        print(f"\n  tokens  in {usage['input_tokens']:,}"
              f" ({100 * cached / usage['input_tokens']:.0f}% cached)"
              f"   out {usage['output_tokens']:,}")
    if summary["vision_usage"]:
        print(f"  vision tokens {dict(summary['vision_usage'])}, reported cost ${summary['vision_cost']:.4f}")
    if summary["cost"]:
        per_call = summary["cost"] / total if total else 0
        print(f"  worker cost ${summary['cost']:.4f}   (${per_call:.5f} per tool call)")

    if summary["failed_checks"]:
        rolled = collections.Counter(summary["failed_checks"])
        print("\n  failed checks (read recovery separately): " + ", ".join(f"{n} x{c}" for n, c in rolled.items()))
    if summary["warnings"]:
        warned = collections.Counter(summary["warnings"])
        print("  warnings:    " + ", ".join(f"{n} x{c}" for n, c in warned.items()))
    if summary["errors"]:
        print("  errors:      " + "; ".join(summary["errors"][:3]))
    print()


def one_line(summary: dict) -> str:
    context = summary["context"]
    build = summary["buckets"].get("BUILD", 0)
    total = len(summary["calls"]) or 1
    return (f"  {summary['path'].name[8:24]}  "
            f"{str(context.get('worker_model'))[:26]:26} "
            f"{len(summary['calls']):4d} calls  {100 * build / total:3.0f}% build  "
            f"{summary['active_seconds'] / 60:5.1f} min  "
            + (f"${summary['cost']:.4f}" if summary["cost"] else "     -"))


def compare(summaries: list[dict]) -> None:
    """Group by worker model -- the question the recording exists to answer."""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for summary in summaries:
        groups[str(summary["context"].get("worker_model"))].append(summary)
    print(f"\n  {'model':28} {'runs':>5} {'calls':>7} {'build%':>7} "
          f"{'active min':>10} {'worker $':>10} {'checks failed':>13}")
    for model, group in sorted(groups.items()):
        calls = sum(len(s["calls"]) for s in group)
        build = sum(s["buckets"].get("BUILD", 0) for s in group)
        print(f"  {model[:28]:28} {len(group):5d} {calls / len(group):7.1f} "
              f"{100 * build / max(calls, 1):6.0f}% "
              f"{sum(s['active_seconds'] for s in group) / len(group) / 60:7.1f} "
              f"{sum(s['cost'] for s in group) / len(group):10.4f} "
              f"{sum(len(s['failed_checks']) for s in group):10d}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", nargs="?", help="path or partial name")
    parser.add_argument("--all", action="store_true", help="one line per session")
    parser.add_argument("--compare", action="store_true", help="group by worker model")
    args = parser.parse_args()

    found = sessions()
    if not found:
        print(f"\n  No transcripts yet in {transcript_dir()}\n")
        return 1

    if args.all or args.compare:
        summaries = [summarize(p) for p in found]
        summaries = [s for s in summaries if s["calls"] or s["user_turns"]]
        if args.compare:
            compare(summaries)
        else:
            print()
            for summary in summaries:
                print(one_line(summary))
            print()
        return 0

    if args.session:
        matches = [p for p in found if args.session in p.name]
        if not matches:
            print(f"\n  No session matching {args.session!r}\n")
            return 1
        target = matches[-1]
    else:
        target = found[-1]
    report(summarize(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
