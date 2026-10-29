"""Small task-specific toolsets and bounded, retrievable model context."""
import json
import hashlib


class ReadLoopGuard:
    """Detect unchanged successful reads, including alternating query loops.

    Each attempted mutation invalidates the observations: even a failed native
    operation can have partial effects. Pending-operation polling is exempt.
    """
    def __init__(self):
        self.reads = {}

    def reset(self):
        self.reads.clear()

    def observe(self, name, arguments, payload):
        if name in {"operation_status", "health"}:
            return 0
        signature = json.dumps([name, arguments], sort_keys=True, default=str)
        # Result handles identify calls, not changes in the returned state.
        content = {k: v for k, v in payload.items() if k != "result_ref"} if isinstance(payload, dict) else payload
        if isinstance(content, dict):
            content = {k: v for k, v in content.items() if k not in {"evidence_ref", "evidence_revision"}}
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
        previous, count = self.reads.get(signature, (None, 0))
        count = count + 1 if previous == fingerprint else 1
        self.reads[signature] = (fingerprint, count)
        return count

# The initial surface should help with any document, not choose its geometry.
GROUPS = {
    "core": {"design_summary", "feature_tree", "inspect_entity", "capture_viewport",
             "create_document", "list_documents", "switch_document", "operation_status", "cancel_operation", "update_design_state", "get_selected_faces"},
    "geometry": {"create_test_box", "extrude_polygons", "cut_polygons", "extrude_between_faces"},
    "editing": {"delete_entities", "delete_named_features", "undo_last"},
    "patterns": {"lattice_pockets", "edit_lattice", "router_fillet_pocket_corners"},
    "assembly": {"import_geometry", "bodies_to_components", "list_joint_candidates", "create_joint", "check_interference", "inspect_geometry", "measure_geometry", "place_component", "verify_relationship"},
    "selection": {"get_selected_faces", "resolve_selected_faces", "highlight_selected_faces"},
    "measurement": {"inspect_geometry", "measure_geometry", "verify_relationship", "verify_spec"},
    "generators": {"create_tube_frame", "add_bellypan", "add_fasteners", "replace_with_isogrid_bar"},
    "engineering": {"mechanics_report", "inspect_threads", "set_6061_material"},
    "validation": {"verify_spec", "check_interference", "verify_relationship", "measure_geometry"},
    "files": {"list_files", "read_file", "fetch_url", "pdf_to_images", "import_geometry", "import_step_component"},
    "source": {"read_file", "search_files", "patch_file", "write_file", "run_tests"},
    "documents": {"active_document", "save_document"},
    "diagnostics": {"health", "inspect_commands"},
    "drivebase_recipe": {"frc_drivebase_survey", "frc_drivebase_frame", "frc_drivebase_drill_pan", "frc_drivebase_validate", "frc_drivebase_rivets", "frc_drivebase_hardware_check", "frc_arm_layout", "frc_arm_motor_survey", "frc_arm_structure", "frc_arm_fit_check", "frc_arm_repair_y_extents", "frc_arm_support_details", "frc_arm_clearance_repair", "frc_arm_pose_check", "frc_arm_joints", "frc_arm_motor_brackets", "frc_arm_elbow_outboard"},
    "all": set(),
}

GROUP_DESCRIPTIONS = {
    "selection": "read user-selected faces, revalidate attached references, highlight exact faces in the viewport",
    "core": "design state and dependencies, design and feature inspection, viewport, empty document creation/selection, operation status",
    "geometry": "new rectangular body/document, polygon extrusion/cuts, extrusion between referenced planar faces",
    "measurement": "paged geometry/face inspection, distances and parallel-plane separation in root coordinates, relationship checks",
    "editing": "explicit trailing-feature deletion and scoped undo; not arbitrary parameter editing",
    "patterns": "isogrid/hex lattices, managed lattice revisions and router pocket fillets",
    "assembly": "import, component placement/copy by reference point, geometry measurement, mating and interference",
    "generators": "specialized tube frames, bellypans, fasteners and replacement isogrid bars",
    "engineering": "section and beam calculations, thread inspection, 6061 material assignment",
    "validation": "verify dimensions, envelope, counts and interference",
    "files": "find/read local files, vendor downloads, PDF images and CAD imports",
    "source": "search/read/patch repository code and run tests for missing capabilities",
    "documents": "document metadata and saving to an existing target",
    "diagnostics": "bridge health and Fusion command inspection",
    "drivebase_recipe": "task-specific WCP-1702 butt-jointed frame, flange survey, pan mounting holes and native collision checks",
    "all": "full registry, including newly registered tools not yet assigned a group",
}

GROUP_GUIDANCE = {
    "assembly": "Assembly: resolve interfaces from placed geometry before constructing dependent parts. inspect_geometry returns exact selectors and world-space faces. measure_geometry distinguishes closest distance from parallel-plane separation. place_component moves/copies a top-level occurrence by an explicit anchor and target, with optional world-axis rotation; it does not create a joint. verify_relationship checks only the named relation, not design intent.",
    "geometry": "Geometry: create_test_box creates a new document with width=X, height=Y, depth=Z thickness. Polygon extrusion and cuts use XY coordinates; they are not general 3D sketch operations.",
    "editing": "Editing: deletion requires an explicit complete trailing timeline chain. undo_last covers only supported additive agent changes, not arbitrary UI undo or parameter edits.",
    "patterns": "Patterns: use returned lattice_ref with edit_lattice to revise its managed latest chain. corner_mode=feature_fillet creates straight sketch triangles plus a separate fillet; sketch_rounding uses chord segments. Zero arcs alone does not prove sharp triangles: inspect non-reference lines and the returned sketch representation.",
}

PROMPT = """You operate live Autodesk Fusion. Execute the user's CAD work using tools.
For multi-part or open-ended design, distinguish fixed inputs, required relationships,
and dimensions that must be derived. Use update_design_state for a concise record
of requirements, important unknowns, dependencies, explicit assumptions and next action.
Resolve the unknowns that determine geometry before creating dependent features.
Ground critical dimensions in user requirements, referenced geometry, measurements,
or explicit assumptions. Never silently replace an unknown with a plausible value.
Choose each action to resolve an important unknown or implement an established relation.
Check that relation before building further geometry that depends on it. Successful
execution does not establish that the design meets the user's intent.
Use returned evidence_ref values in the design record. Recheck stale evidence when a
next action depends on it. Record changes, not repeated plans or long narration.
Simple edits need no formal design record. Do not request plan approval for authorized
work; ask only when a consequential ambiguity cannot be resolved from the available
requirements, drawings or geometry.
User-selected face attachments identify exact locations. Prefer their face_ref selectors
for the referenced surfaces; do not substitute heuristic matches or infer replacements
from images. Revalidate an invalid reference or ask the user to select it again.
Labels express intent, not verified geometry. If the user refers to current selection
without attachments, get_selected_faces reads it. Load measurement/assembly capabilities
as needed to use those exact selectors.
For an ambiguous mounting/interface surface, inspect the relevant component and its
body faces, using the actual image for orientation. Bounding boxes are not interface
measurements. If these still do not establish which surface the user intends, call
request_face_selection with the specific surface(s) to pick and why. Do not spend
repeated turns speculating about plate gaps, change the intended interface, or build
dependent parts while waiting. Once the user replies, use the returned face_ref
selectors and measure the requested relationship before modeling from it.
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
Use targeted intermediate captures when they resolve orientation, interface selection,
or layout uncertainty before dependent modeling. Do not capture after every operation.
After the requested modeling sequence, call capture_viewport with a suitable
view, clean=true, and expected_stage stating what should be present. Vision is uncertain: use native measurements
for counts, radii, placement and dependencies. Do not invent explanations for disagreement.
Large tool results have a result_ref; get_result retrieves selected fields.
"""

LOCAL = {
    "request_face_selection": {"description":"Ask the user to identify a consequential ambiguous surface in Fusion. Supply a concrete question, why it matters, and recognizable surfaces to select. Ends this turn awaiting a reply; never guesses or selects for the user. Use after focused inspection leaves mounting intent unresolved, before dependent geometry.","mutating":False,
        "input_schema":{"type":"object","properties":{
            "question":{"type":"string","minLength":1,"maxLength":500},
            "reason":{"type":"string","minLength":1,"maxLength":500},
            "faces":{"type":"array","minItems":1,"maxItems":12,"items":{"type":"string","minLength":1,"maxLength":160}}},
            "required":["question","reason","faces"],"additionalProperties":False}},
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
