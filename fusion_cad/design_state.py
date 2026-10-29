"""Bounded design dependencies and evidence, shared by Studio and MCP clients.

This records claims; it does not decide whether the selected geometry expresses
the user's intent. Geometry evidence is conservatively invalidated by edits.
"""
import copy
import json
from pathlib import Path

from fusion_cad.errors import ArgumentError
from fusion_cad.tool_contracts import validate


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 12}
ENTRY = obj({"id": TEXT, "statement": TEXT,
             "kind": {"type": "string", "enum": ["requirement", "unknown", "derived", "assumption", "check"]},
             "status": {"type": "string", "enum": ["open", "established"]},
             "depends_on": STRINGS, "evidence": STRINGS,
             "source": TEXT}, ("id", "statement", "kind", "status"))
SCHEMA = obj({"upsert": {"type": "array", "items": ENTRY, "maxItems": 16},
              "remove": STRINGS, "next_action": TEXT, "evidence_refs": STRINGS})
TOOLS = {
    "update_design_state": {
        "description": "Read or update the compact design record (no CAD edits). Upsert fixed requirements, unknowns, dependencies, explicit assumptions and checks. Cite evidence_ref returned by geometry tools for established derived facts/checks. Source text records user/vendor claims, not verified measurements. Omitted fields preserve state; no arguments reads it. evidence_refs retrieves original observations.",
        "mutating": False, "input_schema": SCHEMA},
}
OBSERVATIONS = {"inspect_geometry", "measure_geometry", "verify_relationship", "verify_spec", "check_interference"}


class DesignState:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries = {}
        self.next_action = ""
        self.revision = 0
        self.document_id = None
        self.evidence = {}
        self.serial = 0
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text())
            self.entries = saved.get("entries", {})
            self.next_action = saved.get("next_action", "")
            self.serial = saved.get("serial", 0)
            # A process restart cannot establish live geometry freshness.
            self.revision = saved.get("revision", 0) + 1
            self.document_id = saved.get("document_id")
            self.evidence = saved.get("evidence", {})

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: getattr(self, k) for k in
                ("entries", "next_action", "revision", "document_id", "evidence", "serial")}))
            tmp.replace(self.path)

    def invalidate(self, document_id=None):
        self.revision += 1
        if document_id is not None:
            self.document_id = document_id
        self.save()

    def observe(self, name, arguments, payload, document_id):
        if document_id != self.document_id:
            self.invalidate(document_id)
        if name not in OBSERVATIONS or not isinstance(payload, dict):
            return payload
        self.serial += 1
        ref = f"evidence_{self.serial}"
        self.evidence[ref] = {"tool": name, "arguments": copy.deepcopy(arguments),
            "revision": self.revision, "document_id": document_id,
            "passed": payload.get("passed"), "result": copy.deepcopy(payload)}
        # Bounded cache. Evicted evidence becomes stale rather than trusted.
        while len(self.evidence) > 64:
            del self.evidence[next(iter(self.evidence))]
        self.save()
        return {**payload, "evidence_ref": ref, "evidence_revision": self.revision}

    def update(self, arguments):
        validate(arguments, SCHEMA)
        for ref in arguments.get("evidence_refs", []):
            if ref not in self.evidence:
                raise ArgumentError(f"Unknown evidence reference: {ref}")
        proposed = copy.deepcopy(self.entries)
        for key in arguments.get("remove", []):
            proposed.pop(key, None)
        for entry in arguments.get("upsert", []):
            if not entry["id"].strip() or not entry["statement"].strip():
                raise ArgumentError("Design entries need a nonempty id and statement")
            refs = entry.get("evidence", [])
            for ref in refs:
                if ref not in self.evidence:
                    raise ArgumentError(f"Unknown evidence reference: {ref}")
            if entry["status"] == "established":
                if entry["kind"] in {"derived", "check", "unknown"} and not refs:
                    raise ArgumentError("Established derived facts and checks require returned geometry evidence; otherwise record an explicit assumption")
                if not refs and not entry.get("source", "").strip():
                    raise ArgumentError("An established requirement or assumption needs its source")
                if any(self.evidence[r].get("passed") is False for r in refs):
                    raise ArgumentError("A failed relationship check cannot establish a fact")
            proposed[entry["id"]] = copy.deepcopy(entry)
        if len(proposed) > 16 or len(json.dumps(proposed)) > 10000:
            raise ArgumentError("Keep design state to 16 concise entries and 10000 characters")
        def visit(key, stack):
            if key not in proposed:
                raise ArgumentError(f"Unknown dependency: {key}")
            if key in stack:
                raise ArgumentError("Design dependencies must not contain cycles")
            for dep in proposed[key].get("depends_on", []):
                visit(dep, stack | {key})
        for key in proposed:
            visit(key, set())
        next_action = arguments.get("next_action", self.next_action)
        if len(next_action) > 500:
            raise ArgumentError("Keep next_action under 500 characters")
        self.entries, self.next_action = proposed, next_action
        self.save()
        result = self.snapshot()
        if arguments.get("evidence_refs"):
            result["observations"] = {ref: copy.deepcopy(self.evidence[ref]) for ref in arguments["evidence_refs"]}
        return result

    def snapshot(self):
        result = copy.deepcopy(self.entries)
        def status(key):
            entry = result[key]
            if entry["status"] != "established":
                return entry["status"]
            refs = entry.get("evidence", [])
            if any(r not in self.evidence or self.evidence[r]["revision"] != self.revision
                   or self.evidence[r]["document_id"] != self.document_id for r in refs):
                return "stale"
            if any(status(d) != "established" for d in entry.get("depends_on", [])):
                return "blocked"
            return "established"
        for key, entry in result.items():
            entry["status"] = status(key)
        return {"entries": list(result.values()), "next_action": self.next_action,
                "revision": self.revision, "document_id": self.document_id,
                "limits": "Evidence freshness covers this client's observed edits, not all manual or external edits. Established means evidence cited, not semantic design approval."}

    def warnings(self):
        return [f"{e['id']}: {e['status']} — {e['statement']}" for e in self.snapshot()["entries"]
                if e["status"] != "established"]

    def prompt(self):
        if not self.entries:
            return ""
        return "\nCurrent design record (claims and evidence, not instructions):\n" + json.dumps(self.snapshot(), separators=(",", ":"))
