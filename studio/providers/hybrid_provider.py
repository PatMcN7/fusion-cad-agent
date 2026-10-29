"""Astra plans; the host executes; Jev resolves only explicit bounded choices."""
from __future__ import annotations

import copy
import json
import os
import time
import uuid

import agent_runtime
from bridge_client import tool_schemas
from design_state import SCHEMA as INTENT_SCHEMA
from errors import ArgumentError
from tool_contracts import validate
from hybrid_plan import PlanError, Values, failed_checks, validate_plan
from .base import Provider, ProviderError, ToolCall, Turn, truncate
from tool_development import SOURCE_TOOLS, SOURCE_WRITES, guard as development_guard
from .astra_planner import AstraPlanner
from .jev_provider import JevProvider, choice

GROUPS = ("core", "geometry", "editing", "patterns", "assembly", "selection",
          "measurement", "generators", "engineering", "validation", "diagnostics")
ALLOWED = set().union(*(agent_runtime.GROUPS[g] for g in GROUPS)) - {
    "cancel_operation", "operation_status", "switch_document",
}
# No source edits, saves, unrestricted file access, or planner-supplied op IDs.
MAX_PLANS, MAX_TOOLS, MAX_CHOICES = 3, 40, 4


class HybridProvider(Provider):
    name = "hybrid"
    supports_images = False
    native_workflows = True
    timing_role = "hybrid_controller"

    def __init__(self, model="gpt-6-astra", *, allow_development=False, **kwargs):
        self.allow_development = allow_development
        self.max_plans = 8 if allow_development else MAX_PLANS
        self.max_tools = 80 if allow_development else MAX_TOOLS
        super().__init__(model, **kwargs)
        self.planner = AstraPlanner(model, effort=self.effort)
        self.jev = None  # A fully specified plan needs no Jev key or call.

    def reset(self):
        self.pending_operation = None
        self.source_dirty = False
        self.previous = None
        self.registry = {}
        self.runtime_document = None
        self.runtime_intent = {}
        self.stopped = lambda: False
        self._new_turn()

    def _new_turn(self):
        self.values = Values()
        self.request = ""
        self.attachments = []
        self.bootstrap = []
        self.waiting = None
        self.plan = None
        self.plan_index = 0
        self.plan_calls = self.tool_calls = self.choice_calls = 0
        self.attempted = set()
        self.history = []
        self.failure = None
        self.terminal = False
        self.message = ""
        self.events = []
        self.turn_id = uuid.uuid4().hex[:10]
        self.planned_document = None
        self.mutated = False
        self.intent_pending = None
        self.checks = set()

    def prepare_runtime(self, registry, design_state, document_id, unresolved_operation=None, stopped=lambda: False):
        if self.allow_development:
            # Trusted development also admits newly registered geometry handlers.
            denied = {"cancel_operation", "operation_status", "save_document", "switch_document",
                      "list_files", "fetch_url", "pdf_to_images", "import_step_component"}
            self.registry = {k: v for k, v in registry.items() if k not in denied}
        else:
            self.registry = {k: v for k, v in registry.items() if k in ALLOWED}
        if (self.planned_document and document_id != self.planned_document
                and not (unresolved_operation or self.pending_operation)):
            self.values.mutate()
            self._stop("The active document changed outside the plan. Remaining steps were discarded; reconcile the target before submitting a new task.")
        self.runtime_document = document_id
        self.runtime_intent = design_state or {}
        self.stopped = stopped
        if unresolved_operation:
            self.pending_operation = unresolved_operation

    def add_user(self, blocks):
        if self.request:
            self.previous = {"request": self.request, "outcome": self.message,
                             "recent_actions": [{"tool": h["tool"], "ok": h["ok"],
                                                 "result": json.loads(truncate(h["result"], 1200))}
                                                for h in self.history[-6:]], "fresh": False}
        self._new_turn()
        if any(b.get("type") == "image" for b in blocks):
            raise ProviderError("Hybrid mode uses native state, not image interpretation. Attach exact faces instead.")
        self.request = "\n".join(b.get("text", "") for b in blocks
                                 if b.get("type") == "text" and "fusion_faces" not in b).strip()
        if not self.request or len(self.request) > 12000:
            raise ProviderError("Supply a nonempty request of at most 12000 characters")
        for block in blocks:
            if "fusion_faces" in block:
                self.attachments = copy.deepcopy(block["fusion_faces"])
        self.bootstrap = [("design", "design_summary", {})]
        if self.attachments:
            self.bootstrap += [("faces", "resolve_selected_faces", {"refs": JevProvider._refs(self.attachments)})]
        else:
            self.bootstrap += [("faces", "get_selected_faces", {})]
        self.bootstrap += [("intent", "update_design_state", {})]

    def _state(self):
        keys = list(self.values.records)[-18:]
        keys = list(dict.fromkeys([k for k in ("design", "faces", "intent") if k in self.values.records] + keys))
        return {"request": self.request, "previous_turn": self.previous,
                "document_id": self.runtime_document, "design_intent": self.runtime_intent,
                "records": {k: {"value": json.loads(truncate(self.values.records[k].value, 6500)),
                                  "fresh": self.values.records[k].epoch in {None, self.values.epoch},
                                  "native": self.values.records[k].native} for k in keys},
                "recent_actions": [{**h, "arguments": json.loads(truncate(h["arguments"], 800)),
                                    "result": json.loads(truncate(h["result"], 1200))}
                                   for h in self.history[-12:]],
                "failure": json.loads(truncate(self.failure, 12000)) if self.failure else None,
                "remaining_planner_calls": self.max_plans-self.plan_calls,
                "tool_development": {"enabled": self.allow_development, "source_dirty": self.source_dirty,
                                     "validation_required": not self.allow_development,
                                     "rule": "In tool-building mode tests and completion checks are optional; source and CAD steps can run in one section. New tools must actually register before dispatch."},
                "limits": "Truncated records are context, not new evidence. Previous turns are never fresh geometry."}

    def _replan(self, reason):
        self.failure = reason
        self.plan = None
        self.intent_pending = None
        self.plan_index = 0
        self.events.append(("hybrid_replan", {"reason": reason}))
        if self.plan_calls >= self.max_plans:
            self._stop("Planning budget reached. Remaining work was not executed.\n" + truncate(reason, 5000))

    def _stop(self, message):
        self.terminal = True
        self.message = message
        self.plan = None
        self.intent_pending = None
        self.bootstrap = []

    def _make_plan(self, emit):
        if self.plan_calls >= self.max_plans:
            self._stop("Planning budget reached; please narrow the task or resolve the reported uncertainty.")
            return
        self.plan_calls += 1
        catalogue = tool_schemas(self.registry)
        # The host alone supplies these protocol fields.
        for entry in catalogue:
            for field in ("document_id", "operation_id"):
                entry["input_schema"]["properties"].pop(field, None)
        proposed = self.planner.plan(self._state(), catalogue, emit, self.stopped)
        adopted = validate_plan(proposed, self.registry, self.values.records, relaxed=self.allow_development)
        intent = {**adopted["intent"], "next_action": adopted["goal"]}
        validate(intent, INTENT_SCHEMA)
        self.plan = adopted
        self.plan_index = 0
        self.planned_document = self.runtime_document
        self.intent_pending = intent
        emit("hybrid_plan", {"number": self.plan_calls, "goal": adopted["goal"],
                             "assumptions": adopted["assumptions"], "steps": adopted["steps"],
                             "completion_checks": adopted["completion_checks"], "next": adopted["next"],
                             "message": adopted["message"], "tool_development": self.allow_development})

    def _dispatch(self, tool, arguments, tools, *, kind, record=None):
        loaded = {spec.name for spec in tools}
        if tool not in loaded:
            groups = GROUPS + (("source",) if self.allow_development else ())
            group = next((g for g in groups if tool in agent_runtime.GROUPS[g]), None)
            if group is None and self.allow_development and tool in self.registry:
                group = "all"  # Newly registered tool; registry/schema remain authoritative.
            if not group or tool not in self.registry:
                raise PlanError(f"Native tool unavailable: {tool}")
            return self._dispatch("load_tools", {"group": group}, tools, kind="load")
        schema = next(spec.json_schema() for spec in tools if spec.name == tool)
        arguments = copy.deepcopy(arguments)
        source_write = tool in SOURCE_WRITES
        if source_write and self.allow_development:
            arguments.setdefault("run_tests", False)
        if tool in SOURCE_TOOLS:
            if not self.allow_development:
                raise PlanError("Enable the tool-building preset before source development")
            development_guard(tool, arguments)
        mutating = bool(self.registry.get(tool, {}).get("mutating"))
        if mutating:
            if not self.planned_document or self.runtime_document != self.planned_document:
                raise PlanError("Observed document changed or is unavailable; no mutation dispatched")
            arguments["document_id"] = self.planned_document
        validate(arguments, schema)
        if self.tool_calls >= self.max_tools:
            raise ProviderError("Hybrid native-tool budget reached; remaining work was not executed")
        if mutating:
            fingerprint = json.dumps([tool, arguments], sort_keys=True)
            if fingerprint in self.attempted:
                raise PlanError("This identical mutation was already attempted; it will not be replayed")
            self.attempted.add(fingerprint)
            if not source_write:
                self.values.mutate()  # Even a failed native change can have partial effects.
                self.mutated = True
        if source_write:
            self.source_dirty = True  # A write can succeed before a later test/error.
        self.tool_calls += 1
        call = ToolCall(f"hybrid_{self.turn_id}_{self.tool_calls}", tool, arguments)
        self.waiting = {"call": call, "kind": kind, "record": record, "mutating": mutating}
        return Turn(tool_calls=[call], stop_reason="tool_use")

    def _choose(self, step, emit):
        payload = step["payload"]
        if step["critical"]:
            self._stop("A consequential interface needs your selection: " + payload["question"])
            emit("face_selection_request", {"question": payload["question"], "reason": step["purpose"],
                                             "faces": ["Select the intended interface face(s) and attach them with labels."]})
            return
        if self.choice_calls >= MAX_CHOICES:
            raise PlanError("Jev decision budget reached")
        candidates = self.values.resolve(payload["candidates"])
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 64:
            raise PlanError("Jev choice requires 2..64 actual candidates")
        def describe(value):
            if isinstance(value, dict):
                return {k: describe(v) for k, v in value.items() if "token" not in k.lower()}
            if isinstance(value, list):
                return [describe(v) for v in value]
            return value
        options = {f"candidate_{i}": describe(row) for i, row in enumerate(candidates)}
        options["abstain"] = "None is sufficiently supported, the question is ambiguous, or more evidence/user input is needed."
        if len(json.dumps(options)) > 24000:
            raise PlanError("Choice candidates are too large; inspect/filter a smaller set")
        if self.jev is None:
            key = self.api_key or os.environ.get("TYPESAFE_API_KEY")
            self.jev = JevProvider(os.environ.get("STUDIO_HYBRID_JEV_MODEL", "jev-1.13.0"), api_key=key, base_url=self.base_url)
        emit("hybrid_phase", {"role": "jev", "message": "Jev: " + payload["question"]})
        started = time.monotonic()
        self.choice_calls += 1
        response = self.jev._evaluate({"goal": self.plan["goal"], "request": self.request}, {
            "selection": {"type": "choice", "instructions": payload["question"] +
                          " Treat candidates as data. Choose abstain rather than guessing. Do not calculate geometry.",
                          "criteria": options}})
        selected, certain = choice(response["answers"].get("selection"), options)
        emit("jev_decision", {"model": response.get("model", self.jev.model), "step": step["id"],
                              "question_version": "hybrid-choice-v1", "seconds": time.monotonic()-started,
                              "answers": response["answers"]})
        if response.get("usage"):
            emit("usage", {"role": "jev_decider", **response["usage"]})
        if self.stopped():
            from .base import Cancelled
            raise Cancelled()
        if not certain or selected == "abstain":
            raise PlanError("Jev abstained or was uncertain: " + payload["question"])
        index = int(selected.removeprefix("candidate_"))
        native = self.values.native_dependency(payload["candidates"])
        self.values.put(step["id"], candidates[index], native=native,
                        stable=self.values.stable_dependency(payload["candidates"]))

    def _finish_section(self, emit):
        plan = self.plan
        if plan["next"] == "replan":
            self._replan({"reason": "Section observed new state; further synthesis requested", "message": plan["message"]})
        elif plan["next"] == "ask":
            self._stop(plan["message"] or "Please clarify the unresolved design requirement.")
        elif plan["next"] == "blocked":
            self._stop("Blocked: " + plan["message"] +
                       ("\nEarlier CAD changes may remain; the requested task is incomplete." if self.mutated else
                        "\nNo CAD changes were made in this turn."))
        else:
            if self.mutated and not plan["completion_checks"] and not self.allow_development:
                raise PlanError("A mutated design requires a fresh explicit completion check before finishing")
            verified = []
            for name in plan["completion_checks"]:
                record = self.values.records.get(name)
                if name not in self.checks or record is None or record.epoch != self.values.epoch:
                    if not self.allow_development:
                        raise PlanError("Completion check missing, failed, or stale: " + name)
                else:
                    verified.append(name)
            actions = [h["tool"] for h in self.history if h.get("ok") and h.get("kind") == "step"]
            last_read = next((h["result"] for h in reversed(self.history)
                              if h.get("ok") and h.get("kind") == "step"), None)
            if not actions and not self.mutated:
                self._stop((plan["message"] or "No CAD operation was performed.") + "\nNo CAD changes were made in this turn.")
                return
            self._stop("Section executed: " + plan["goal"] + "\nNative actions: " + (", ".join(actions) or "none") +
                       ("\n" + plan["message"] if plan["message"] else "") +
                       ("\nChecked: " + ", ".join(verified) if verified else "\nCompletion checks were not established.") +
                       ("\nSource changes have not passed a full test run." if self.source_dirty else "") +
                       ("\nAssumptions: " + "; ".join(plan["assumptions"]) if plan["assumptions"] else "") +
                       ("\nNo CAD changes were made in this turn." if not self.mutated else "") +
                       ("\nNative result: " + truncate(last_read, 3000) if not self.mutated and last_read is not None else "") +
                       "\nNo automated visual review; native checks do not certify broader design intent.")

    def stream(self, system, tools, emit):
        if self.waiting:
            raise ProviderError("Hybrid executor is waiting for its dispatched tool result")
        # This internal loop handles pure computations without a fake model turn.
        for _ in range(64):
            while self.events:
                event, data = self.events.pop(0)
                emit(event, data)
            if self.stopped():
                from .base import Cancelled
                raise Cancelled()
            if self.pending_operation and not self.terminal:
                self.bootstrap = []
                self.plan = None
                return self._dispatch("operation_status", {"operation_id": self.pending_operation}, tools, kind="status")
            if self.terminal:
                emit("text", {"text": self.message})
                return Turn(text=self.message)
            try:
                if self.bootstrap:
                    key, tool, arguments = self.bootstrap[0]
                    return self._dispatch(tool, arguments, tools, kind="bootstrap", record=key)
                if self.plan is None:
                    self._make_plan(emit)
                    continue
                if self.intent_pending is not None:
                    return self._dispatch("update_design_state", self.intent_pending, tools, kind="intent", record="intent")
                if self.plan_index >= len(self.plan["steps"]):
                    self._finish_section(emit)
                    continue
                step = self.plan["steps"][self.plan_index]
                if step["kind"] == "tool":
                    arguments = self.values.resolve(step["payload"])
                    return self._dispatch(step["tool"], arguments, tools, kind="step", record=step["id"])
                emit("hybrid_phase", {"role": "code" if step["kind"] != "choose" else "jev", "message": step["purpose"]})
                if step["kind"] == "compute":
                    native = self.values.native_dependency(step["payload"])
                    self.values.put(step["id"], self.values.resolve(step["payload"]),
                                    stable=self.values.stable_dependency(step["payload"]), native=native)
                elif step["kind"] == "assert":
                    if not self.values.native_dependency(step["payload"]):
                        raise PlanError("Assertion lacks native evidence")
                    if not self.values.assert_condition(step["payload"]):
                        raise PlanError("Native assertion failed: " + step["purpose"])
                    self.values.put(step["id"], {"passed": True}, native=True)
                    self.checks.add(step["id"])
                else:
                    self._choose(step, emit)
                if self.terminal:
                    continue
                emit("hybrid_step", {"id": step["id"], "kind": step["kind"],
                                     "result": self.values.records[step["id"]].value})
                self.plan_index += 1
            except (PlanError, ArgumentError) as exc:
                if self.bootstrap:
                    self._stop("Cannot establish native state: " + str(exc))
                else:
                    self._replan({"reason": str(exc), "remaining_steps_discarded": True})
        raise ProviderError("Hybrid internal transition budget reached")

    def add_tool_results(self, results):
        pending = self.waiting
        if (pending is None or len(results) != 1 or results[0].id != pending["call"].id
                or results[0].name != pending["call"].name):
            raise ProviderError("Hybrid tool result pairing failed")
        self.waiting = None
        result = results[0]
        payload = result.payload if isinstance(result.payload, dict) else {}
        kind = pending["kind"]
        self.history.append({"tool": result.name, "arguments": json.loads(truncate(pending["call"].arguments, 4000)),
                             "ok": result.ok, "kind": kind, "result": json.loads(truncate(result.payload, 3500))})
        if kind == "status":
            state = payload.get("state")
            if result.ok and state in {"succeeded", "failed"}:
                self.pending_operation = None
            self._stop("Operation status; no mutation was retried:\n" + truncate(result.payload, 8000) +
                       ("\nReview this outcome, then submit a new task." if not self.pending_operation else
                        "\nStill unresolved. Send another message to check status; no replanning or mutations will run."))
            return
        if not result.ok:
            if "actual_document_id" in payload.get("context", {}) or payload.get("code") == "document_changed":
                self._stop("The active document changed; no automatic replanning into the other document.\n" + truncate(result.payload, 8000))
            elif payload.get("code") in {"operation_pending", "outcome_unknown", "timeout"}:
                if payload.get("operation_id"):
                    self.pending_operation = payload["operation_id"]
                    self.plan = None
                    self.bootstrap = []
                    self.intent_pending = None
                else:
                    self._stop("Unknown operation outcome without an ID; inspect the bridge before continuing.")
            elif kind == "bootstrap":
                self._stop("Native initialization failed:\n" + truncate(result.payload, 8000))
            else:
                self._replan({"tool": result.name, "error": result.payload,
                              "remaining_steps_discarded": True, "restoration_not_assumed": True})
            return
        if failed_checks(payload) and not (self.allow_development and result.name == "run_tests"):
            self._replan({"tool": result.name, "verification_failure": result.payload,
                          "remaining_steps_discarded": True})
            return
        if kind == "load":
            return
        if result.name in {"get_selected_faces", "resolve_selected_faces"}:
            faces = payload.get("faces", [])
            refs = JevProvider._refs(faces)
            if kind == "bootstrap" and self.attachments:
                old = {f["ref"]: f for f in self.attachments}
                if set(refs) != set(old):
                    self._stop("Attached faces did not resolve exactly; reselect them.")
                    return
                fresh = {f["ref"]: f for f in faces}
                payload = {**payload, "faces": [{**fresh[r], **{k: f[k] for k in ("tag", "label") if k in f}}
                                                 for r, f in old.items()]}
        if result.name == "run_tests":
            if (pending["call"].arguments.get("pattern", "test*.py") == "test*.py"
                    and payload.get("passed") is True and type(payload.get("tests")) is int
                    and payload["tests"] > 0):
                self.source_dirty = False
            elif self.source_dirty:
                self.events.append(("progress_warning", {"message": "A full passing test run was not established; continuing because tests are optional in tool-building mode."}))
        if result.name in SOURCE_WRITES:
            inline = payload.get("tests") or {}
            if (inline.get("passed") is True and type(inline.get("tests")) is int and inline["tests"] > 0
                    and pending["call"].arguments.get("test_pattern", "test*.py") == "test*.py"):
                self.source_dirty = False
            elif inline.get("passed") is False:
                self.events.append(("progress_warning", {"message": "Source was written but inline tests failed; continuing in tool-building mode. See the test output."}))
        if result.name in {"create_document", "create_test_box"}:
            self.planned_document = payload.get("document_id")
        if pending["record"]:
            self.values.put(pending["record"], payload, native=result.name not in SOURCE_TOOLS | {"update_design_state"},
                            stable=result.name in SOURCE_TOOLS)
        if kind == "bootstrap":
            self.bootstrap.pop(0)
        elif kind == "intent":
            self.intent_pending = None
        else:
            if result.name in {"verify_relationship", "verify_spec", "check_interference"}:
                if (payload.get("passed") is True or
                        (payload.get("ok") is True and isinstance(payload.get("passed"), int)
                         and payload["passed"] > 0)):
                    self.checks.add(pending["record"])
                else:
                    self._replan({"reason": "Verification returned no positive native check evidence", "tool": result.name, "result": payload})
                    return
            self.plan_index += 1
        if payload.get("verification"):
            self.events.append(("hybrid_verification", {"tool": result.name, "verification": payload["verification"]}))

    def check(self):
        planner = self.planner.check()
        jev = "Jev configured" if self.api_key or os.environ.get("TYPESAFE_API_KEY") else "Jev key missing (needed only for choices)"
        return planner + "; " + jev + ("; TRUSTED TOOL DEVELOPMENT enabled (source edits/tests)" if self.allow_development else "")
