"""Small task-specific toolsets and bounded, retrievable model context."""
import json
from pathlib import Path

GROUPS = {
    "all": set(),
    "core": {"create_test_box", "lattice_pockets", "edit_lattice", "feature_tree", "inspect_entity",
             "capture_viewport", "list_documents", "switch_document", "operation_status", "cancel_operation"},
    "geometry": {"extrude_polygons", "cut_polygons", "router_fillet_pocket_corners", "delete_entities", "delete_named_features", "undo_last", "design_summary", "verify_spec", "set_6061_material", "save_document"},
    "assembly": {"create_tube_frame", "add_bellypan", "add_fasteners", "import_geometry", "bodies_to_components", "list_joint_candidates", "create_joint", "check_interference"},
    "engineering": {"mechanics_report", "inspect_threads", "verify_spec"},
    "files": {"list_files", "read_file", "fetch_url", "pdf_to_images", "import_geometry"},
    "source": {"read_file", "search_files", "patch_file", "write_file", "run_tests"},
}

PROMPT = """You operate live Autodesk Fusion. Execute the user's CAD work using tools.
Use unit strings such as '8 in' for dimensions. create_test_box creates a new
document: width=X, height=Y, depth=Z thickness. For an 8x6x0.25 plate use width=8in,
height=6in, depth=0.25in. lattice_pockets returns lattice_ref and native
entities. edit_lattice revises that chain, including corner_mode=feature_fillet
for straight sketch triangles plus a separate fillet. Use returned references;
never invent feature names or IDs. To inspect an existing design use feature_tree
or inspect_entity. Native curve counts and feature health establish editability.
The client binds edits to the observed document. A timeout is unknown: poll the
same operation ID before retrying. Recovery can be partial; inspect what remains.
Do not infer full restoration from matching counts. Undo is limited to supported
agent-created features, not arbitrary UI actions. Do not repeat unchanged errors.
Read argument types literally; arrays are JSON arrays, not strings containing JSON.
Most actions need no narration. Keep final answers under 80 words stating
the result and unresolved issues. Do not narrate debugging hypotheses. Never
claim a check ran, a file changed, or a sketch was removed without returned evidence.
Tools are grouped: load_tools exposes geometry/editing, assembly, engineering,
files/vendor data, or source editing when needed. Load source only when the
supported modeling tools cannot do the task. Project-specific code belongs in
recipes/. Use focused reads and patches, not whole-file rewrites for small fixes.
After the requested modeling sequence, call capture_viewport once with a suitable
view (top for planar layout, iso for depth), clean=true, and expected_stage stating
what should be present. Avoid captures of unfinished intermediate stages unless
diagnosing an issue. Vision observations are uncertain; use native measurements
for counts, radii, centering and dependencies. Never explain away disagreement
with invented geometry. A source write reports whether a client restart is needed.
Large tool results have a result_ref; get_result retrieves selected fields.
"""

LOCAL = {
    "load_tools": {"description": "Load an additional tool group, or refresh capabilities after a geometry/source change.", "mutating": False,
        "input_schema": {"type": "object", "properties": {"group": {"type": "string", "enum": list(GROUPS)}}, "required": ["group"], "additionalProperties": False}},
    "get_result": {"description": "Retrieve a field from a prior tool result without repeating the native operation.", "mutating": False,
        "input_schema": {"type": "object", "properties": {"result_ref": {"type": "string"}, "field": {"type": "string", "description": "Optional dot-separated field, e.g. created_entities.0"}}, "required": ["result_ref"], "additionalProperties": False}},
}


def select(registry, groups):
    if "all" in groups: return {**registry, **LOCAL}
    names = set().union(*(GROUPS[g] for g in groups))
    return {**{k: v for k, v in registry.items() if k in names}, **LOCAL}


def source_brief():
    return "Source editing: read_file supports symbol/line ranges and returns sha256. patch_file replaces unique text using that hash. Read AGENTS.md for repository conventions before shared-module changes. Source maps can be read from catalog.py. Geometry handlers refresh after writes; bridge_client and studio modules require restarting that client. Tests must cover the actual changed contract."


def compact_messages(messages, keep_recent=12):
    """Preserve protocol pairing, user intent, refs and errors; shrink old successful results.

    No native provider reasoning/signature blocks are touched. Only the OpenAI
    compatible adapter calls this function, using its plain tool/text messages.
    """
    for message in messages[:-keep_recent]:
        if message.get("role") != "tool": continue
        try: payload = json.loads(message.get("content", ""))
        except (TypeError, ValueError): continue
        if not isinstance(payload, dict) or payload.get("code") or payload.get("recovery"): continue
        if len(message["content"]) < 1200: continue
        keys = ("result_ref", "operation_id", "document_id", "lattice_ref", "created_entities",
                "design", "completion", "path", "sha256", "activation", "tests", "replaced_refs")
        summary = {k: payload[k] for k in keys if k in payload}
        if not summary.get("result_ref"): continue
        summary["compacted"] = "Older result; retrieve fields with get_result"
        # Large feature lists remain in the retrievable result, retaining their ref.
        for key in ("created_entities", "design"):
            if len(json.dumps(summary.get(key))) > 2000:
                summary[key] = {"retrieve": key, "result_ref": summary["result_ref"]}
        message["content"] = json.dumps(summary)
