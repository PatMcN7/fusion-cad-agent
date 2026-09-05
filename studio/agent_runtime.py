"""Small task-specific toolsets and bounded, retrievable model context."""
import json

# The initial surface should help with any document, not choose its geometry.
GROUPS = {
    "core": {"design_summary", "feature_tree", "inspect_entity", "capture_viewport",
             "create_document", "list_documents", "switch_document", "operation_status", "cancel_operation"},
    "geometry": {"create_test_box", "extrude_polygons", "cut_polygons"},
    "editing": {"delete_entities", "delete_named_features", "undo_last"},
    "patterns": {"lattice_pockets", "edit_lattice", "router_fillet_pocket_corners"},
    "assembly": {"import_geometry", "bodies_to_components", "list_joint_candidates", "create_joint", "check_interference"},
    "generators": {"create_tube_frame", "add_bellypan", "add_fasteners", "replace_with_isogrid_bar"},
    "engineering": {"mechanics_report", "inspect_threads", "set_6061_material"},
    "validation": {"verify_spec", "check_interference"},
    "files": {"list_files", "read_file", "fetch_url", "pdf_to_images", "import_geometry", "import_step_component"},
    "source": {"read_file", "search_files", "patch_file", "write_file", "run_tests"},
    "documents": {"active_document", "save_document"},
    "diagnostics": {"health", "inspect_commands"},
    "all": set(),
}

GROUP_DESCRIPTIONS = {
    "core": "design and feature inspection, viewport, empty document creation/selection, operation status",
    "geometry": "new rectangular body/document, planar polygon extrusion and cuts",
    "editing": "explicit trailing-feature deletion and scoped undo; not arbitrary parameter editing",
    "patterns": "isogrid/hex lattices, managed lattice revisions and router pocket fillets",
    "assembly": "import, bodies to components, joint candidates, mating and interference",
    "generators": "specialized tube frames, bellypans, fasteners and replacement isogrid bars",
    "engineering": "section and beam calculations, thread inspection, 6061 material assignment",
    "validation": "verify dimensions, envelope, counts and interference",
    "files": "find/read local files, vendor downloads, PDF images and CAD imports",
    "source": "search/read/patch repository code and run tests for missing capabilities",
    "documents": "document metadata and saving to an existing target",
    "diagnostics": "bridge health and Fusion command inspection",
    "all": "full registry, including newly registered tools not yet assigned a group",
}

GROUP_GUIDANCE = {
    "geometry": "Geometry: create_test_box creates a new document with width=X, height=Y, depth=Z thickness. Polygon extrusion and cuts use XY coordinates; they are not general 3D sketch operations.",
    "editing": "Editing: deletion requires an explicit complete trailing timeline chain. undo_last covers only supported additive agent changes, not arbitrary UI undo or parameter edits.",
    "patterns": "Patterns: use returned lattice_ref with edit_lattice to revise its managed latest chain. corner_mode=feature_fillet creates straight sketch triangles plus a separate fillet; sketch_rounding uses chord segments. Zero arcs alone does not prove sharp triangles: inspect non-reference lines and the returned sketch representation.",
}

PROMPT = """You operate live Autodesk Fusion. Execute the user's CAD work using tools.
The initial tools cover orientation, inspection, observation, and capability loading.
create_document creates and activates an empty unsaved design; use it before
modeling or importing into a new document. It creates no placeholder geometry.
For existing geometry, use design_summary for bodies/components and dimensions,
feature_tree for native features, or inspect_entity for a returned feature reference.
Use the returned state instead of repeating inspection after every operation.
Choose the intended open document with list_documents and switch_document when
needed; same-named documents require the returned index. Use returned references;
never invent feature names or IDs. Native inspection establishes feature contents.
Load the relevant tool group with load_tools before modeling, editing, assembly,
validation or file work. Its group directory describes actual available capabilities.
Do not substitute a specialized generator for a different requested operation.
If no supported tool fits, inspect available capabilities before loading source.
Do not claim generic feature editing or modeling operations that are not exposed.
Use unit expression strings for dimensions and respect each tool's axes and types.
The client binds edits to the observed document. A timeout is unknown: poll the
same operation ID before retrying. Recovery can be partial; inspect what remains.
Do not infer full restoration from matching counts or repeat unchanged errors.
Most actions need no narration. Keep final answers under 80 words stating the
result and unresolved issues. Never claim a check ran or a file changed without
evidence. Use focused source reads and patches, not whole-file rewrites for small
fixes. Source writes report activation requirements; recipes require explicit integration.
After the requested modeling sequence, call capture_viewport once with a suitable
view, clean=true, and expected_stage stating what should be present. Avoid captures
of unfinished stages unless diagnosing. Vision is uncertain: use native measurements
for counts, radii, placement and dependencies. Do not invent explanations for disagreement.
Large tool results have a result_ref; get_result retrieves selected fields.
"""

LOCAL = {
    "load_tools": {"description": "Load a capability group or refresh its tools. Multiple group loads can be requested in one turn. Groups: " + "; ".join(f"{name}: {description}" for name, description in GROUP_DESCRIPTIONS.items()), "mutating": False,
        "input_schema": {"type": "object", "properties": {"group": {"type": "string", "enum": list(GROUPS)}}, "required": ["group"], "additionalProperties": False}},
    "get_result": {"description": "Retrieve a field from a prior tool result without repeating the native operation.", "mutating": False,
        "input_schema": {"type": "object", "properties": {"result_ref": {"type": "string"}, "field": {"type": "string", "description": "Optional dot-separated field, e.g. created_entities.0"}}, "required": ["result_ref"], "additionalProperties": False}},
}


def guidance(groups):
    """Teach specialized conventions only after those capabilities are loaded."""
    selected = GROUPS if "all" in groups else groups
    parts = [GROUP_GUIDANCE[name] for name in GROUP_GUIDANCE if name in selected]
    if "source" in selected: parts.append(source_brief())
    return "\n".join(parts)


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
