"""Pure, bounded plan language. Data references and arithmetic, never Python eval."""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass

from fusion_cad.errors import ArgumentError
from fusion_cad.tool_contracts import validate


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
STEP_SCHEMA = obj({
    "id": TEXT, "kind": {"type": "string", "enum": ["tool", "compute", "assert", "choose"]},
    "tool": TEXT, "payload_json": TEXT, "purpose": TEXT, "critical": {"type": "boolean"},
})
PLAN_SCHEMA = obj({
    "goal": TEXT, "assumptions": {"type": "array", "items": TEXT, "maxItems": 6},
    "intent_json": TEXT,
    "steps": {"type": "array", "items": STEP_SCHEMA, "maxItems": 12},
    "completion_checks": {"type": "array", "items": TEXT, "maxItems": 12},
    "next": {"type": "string", "enum": ["finish", "replan", "ask", "blocked"]}, "message": TEXT,
})
IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}\Z")
VERIFIERS = {"verify_relationship", "verify_spec", "check_interference"}


class PlanError(ValueError):
    pass


def parse_json(text):
    if not isinstance(text, str) or len(text) > 32000:
        raise PlanError("Plan JSON field is not a bounded string")
    try:
        return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, RecursionError):
        raise PlanError("Invalid or non-finite JSON in plan") from None


def references(value):
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
                raise PlanError("A reference must contain only a string $ref")
            yield value["$ref"].split(".", 1)[0]
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def validate_plan(plan, tools, existing, *, relaxed=False):
    try:
        if len(json.dumps(plan, allow_nan=False)) > 100000:
            raise PlanError("Plan exceeds 100 KB")
        validate(plan, PLAN_SCHEMA)
    except (ArgumentError, TypeError, ValueError, RecursionError) as exc:
        raise PlanError(f"Invalid plan contract: {exc}") from None
    if not plan["goal"].strip() or len(plan["goal"]) > 500 or len(plan["message"]) > 1500:
        raise PlanError("Goal/message must be bounded and the goal nonempty")
    if any(len(s) > 500 for s in plan["assumptions"]):
        raise PlanError("Keep assumptions concise")
    intent = parse_json(plan["intent_json"])
    if not isinstance(intent, dict):
        raise PlanError("intent_json must contain design-state tool arguments")
    seen, checks = set(existing), set()
    parsed = []
    for step in plan["steps"]:
        name = step["id"]
        if not IDENTIFIER.fullmatch(name) or name in seen:
            raise PlanError(f"Duplicate or invalid step ID: {name}")
        if not step["purpose"].strip() or len(step["purpose"]) > 500:
            raise PlanError("Each step needs a concise purpose")
        payload = parse_json(step["payload_json"])
        refs = set(references(payload))
        if not refs <= seen:
            raise PlanError(f"Step {name} has unknown or forward references: {refs - seen}")
        kind = step["kind"]
        if kind == "tool":
            deferred = relaxed and any(s["kind"] == "tool" and s["tool"] in {"write_file", "patch_file"} for s in parsed)
            if (step["tool"] not in tools and not deferred) or not isinstance(payload, dict):
                raise PlanError(f"Unavailable tool or invalid arguments: {step['tool']}")
            if any(key in payload for key in ("document_id", "operation_id")):
                raise PlanError("The executor, not the planner, owns document and operation IDs")
            if step["tool"] in VERIFIERS:
                checks.add(name)
        elif step["tool"]:
            raise PlanError("Only tool steps may name a tool")
        if step["critical"] and kind != "choose":
            raise PlanError("critical applies only to choices")
        if kind == "assert":
            if not isinstance(payload, dict) or set(payload) != {"op", "left", "right"} or payload["op"] not in {"eq", "ne", "lt", "lte", "gt", "gte"}:
                raise PlanError("Assertion needs op, left, right")
            if not refs or payload["left"] == payload["right"]:
                raise PlanError("An assertion must compare native observations with a distinct expectation")
            checks.add(name)
        if kind == "choose":
            if not isinstance(payload, dict) or set(payload) != {"question", "candidates"} or not isinstance(payload["question"], str) or not 1 <= len(payload["question"]) <= 1000:
                raise PlanError("Choice needs a bounded question and candidate data")
        seen.add(name)
        parsed.append({**step, "payload": payload})
    if not set(plan["completion_checks"]) <= checks:
        raise PlanError("Completion checks must name this section's assertions or native verification tools")
    writes = [s for s in parsed if s["kind"] == "tool" and s["tool"] in {"write_file", "patch_file"}]
    geometry = [s for s in parsed if s["kind"] == "tool" and tools.get(s["tool"], {"mutating": True}).get("mutating")
                and s["tool"] not in {"write_file", "patch_file"}]
    if writes and geometry and not relaxed:
        raise PlanError("Separate source development from CAD mutations; test and refresh the catalogue first")
    if plan["next"] in {"ask", "blocked"} and not plan["message"].strip():
        raise PlanError("An ask/blocked plan must explain the question or blocker")
    if (plan["next"] == "finish" and not plan["completion_checks"] and geometry and not relaxed):
        raise PlanError("A mutation section needs explicit completion checks before execution")
    if plan["next"] == "replan" and not parsed:
        raise PlanError("An empty replan section makes no progress")
    return {**plan, "steps": parsed, "intent": intent}


@dataclass
class Record:
    value: object
    epoch: int | None
    native: bool = False


class Values:
    def __init__(self):
        self.epoch = 0
        self.records = {}

    def put(self, name, value, *, stable=False, native=False):
        self.records[name] = Record(copy.deepcopy(value), None if stable else self.epoch, native)

    def mutate(self):
        self.epoch += 1

    def resolve(self, value, depth=0):
        if depth > 20:
            raise PlanError("Expression nesting exceeds 20 levels")
        resolve = lambda item: self.resolve(item, depth + 1)
        if isinstance(value, list):
            if len(value) > 4096:
                raise PlanError("Expression array is too large")
            return [resolve(v) for v in value]
        if isinstance(value, dict):
            special = [k for k in value if k.startswith("$")]
            if "$ref" in value:
                if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
                    raise PlanError("Invalid reference")
                root, *path = value["$ref"].split(".")
                record = self.records.get(root)
                if record is None or record.epoch not in {None, self.epoch}:
                    raise PlanError(f"Missing or stale observation: {root}; remeasure/reinspect")
                out = record.value
                try:
                    for key in path:
                        out = out[int(key)] if isinstance(out, list) and key.isdigit() else out[key]
                except (KeyError, IndexError, TypeError):
                    raise PlanError(f"No value at {value['$ref']}") from None
                return copy.deepcopy(out)
            if "$calc" in value:
                if set(value) != {"$calc", "args"} or not isinstance(value["args"], list):
                    raise PlanError("Calculation needs $calc and args")
                args = [number(resolve(v)) for v in value["args"]]
                op = value["$calc"]
                if op in {"add", "min", "max"} and 1 <= len(args) <= 16:
                    result = {"add": sum, "min": min, "max": max}[op](args)
                elif op in {"subtract", "multiply", "divide"} and len(args) == 2:
                    a, b = args
                    if op == "divide" and b == 0:
                        raise PlanError("Division by zero")
                    result = a-b if op == "subtract" else a*b if op == "multiply" else a/b
                elif op == "abs" and len(args) == 1:
                    result = abs(args[0])
                else:
                    raise PlanError("Unsupported calculation or arity")
                return number(result)
            if "$length" in value:
                item = value["$length"]
                if set(value) != {"$length"} or not isinstance(item, dict) or set(item) != {"value", "unit"} or item["unit"] not in {"mm", "cm", "m", "in"}:
                    raise PlanError("Length needs a numeric value and explicit unit")
                return f"{number(resolve(item['value'])):.12g} {item['unit']}"
            if special:
                raise PlanError("Unknown expression operator")
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return number(value)
        return value

    def assert_condition(self, payload):
        left, right = self.resolve(payload["left"]), self.resolve(payload["right"])
        op = payload["op"]
        if op in {"eq", "ne"}:
            return left == right if op == "eq" else left != right
        left, right = number(left), number(right)
        return {"lt": left < right, "lte": left <= right, "gt": left > right, "gte": left >= right}[op]

    def stable_dependency(self, payload):
        return all(name in self.records and self.records[name].epoch is None for name in references(payload))

    def native_dependency(self, payload):
        return any(self.records[name].native for name in references(payload) if name in self.records)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e12 or not math.isfinite(value):
        raise PlanError("Expected a finite, bounded numeric value")
    return value


def failed_checks(payload):
    """Do not conflate an integer passed count with a boolean verdict."""
    if not isinstance(payload, dict):
        return False
    if payload.get("passed") is False or payload.get("ok") is False:
        return True
    if isinstance(payload.get("failed"), int) and payload["failed"] > 0:
        return True
    if any(c.get("ok") is False and c.get("severity", "error") == "error"
           for c in (payload.get("checks") or []) if isinstance(c, dict)):
        return True
    verification = payload.get("verification", [])
    if isinstance(verification, dict):
        verification = [verification]
    return any(failed_checks(v) for v in verification) if isinstance(verification, list) else False
