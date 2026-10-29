"""Native entity inspection and deliberately scoped feature-chain edits.

References resolve native tokens in their owning open document. Deletion only
accepts an explicitly selected timeline suffix: unknown dependencies never cause
an implicit cascade through later user features.
"""
from __future__ import annotations
import hashlib
import json
import uuid
from errors import ArgumentError, ToolFailure

# Retained if geometry code imports/reloads this module during a session.
if "_refs" not in globals(): _refs = {}
if "_undo" not in globals(): _undo = []


def items(collection):
    return [collection.item(i) for i in range(collection.count)]


def ref(design, entity):
    for key, (owner, token) in list(_refs.items()):
        if owner == design.parentDocument:
            try:
                if entity in design.findEntityByToken(token): return key
            except Exception: pass
    key = "entity_" + uuid.uuid4().hex[:12]
    _refs[key] = (design.parentDocument, entity.entityToken)
    return key


def resolve(design, key):
    entry = _refs.get(key)
    if not entry or entry[0] != design.parentDocument:
        raise ArgumentError("Unknown or wrong-document entity reference; inspect feature_tree again")
    found = design.findEntityByToken(entry[1])
    if len(found) != 1 or not found[0].isValid:
        raise ArgumentError("Entity reference no longer resolves uniquely; inspect feature_tree again")
    return found[0]


def component_name(entity):
    owner = getattr(entity, "parentComponent", None) or getattr(entity, "component", None)
    return owner.name if owner is not None else None


def row(design, entity):
    timeline = getattr(entity, "timelineObject", None)
    out = {"ref": ref(design, entity), "name": entity.name,
           "type": entity.objectType.split("::")[-1],
           "component": component_name(entity),
           "timeline_index": timeline.index if timeline else None}
    for source, target in (("isVisible", "visible"), ("isSuppressed", "suppressed"),
                           ("healthState", "health"), ("errorOrWarningMessage", "health_message")):
        try:
            value = getattr(entity, source)
            out[target] = value if isinstance(value, (str, bool, int, float)) else str(value)
        except Exception: pass
    # This is a partial dependency view, not a claim to cover every Fusion type.
    try:
        profile = entity.profile
        profiles = items(profile) if hasattr(profile, "count") else [profile]
        out["profile_sketches"] = list(dict.fromkeys(ref(design, p.parentSketch) for p in profiles if hasattr(p, "parentSketch")))
    except Exception: pass
    try:
        curves = entity.sketchCurves
        out["curves"] = {"total": curves.count, "lines": curves.sketchLines.count,
                         "arcs": curves.sketchArcs.count, "circles": curves.sketchCircles.count}
        out["curves"]["reference"] = sum(bool(c.isReference) for c in items(curves))
        out["curves"]["non_reference_lines"] = sum(not c.isReference for c in items(curves.sketchLines))
        out["profiles"] = entity.profiles.count
    except Exception: pass
    return out


def tree(design, component=None):
    rows = []
    for i in range(design.timeline.count):
        entity = design.timeline.item(i).entity
        if entity is None: continue
        if component and component_name(entity) != component: continue
        rows.append(row(design, entity))
    return {"entries": rows, "timeline_count": design.timeline.count,
            "marker_position": design.timeline.markerPosition,
            "dependency_coverage": "profile_sketches where available; deletion conservatively requires an explicit timeline suffix"}


def suffix(design, references):
    if not references or len(set(references)) != len(references):
        raise ArgumentError("Pass distinct explicit entity references")
    entities = [resolve(design, key) for key in references]
    indexes = sorted(e.timelineObject.index for e in entities)
    if indexes != list(range(indexes[0], design.timeline.count)) or design.timeline.markerPosition != design.timeline.count:
        raise ArgumentError("Later or rolled-back timeline entries could depend on this selection. Inspect feature_tree; select only an explicit complete trailing chain.")
    return sorted(entities, key=lambda e: e.timelineObject.index, reverse=True)


def delete(design, references):
    entities = suffix(design, references)  # Validate everything before deleting.
    removed = []
    try:
        for entity in entities:
            name = entity.name
            before = design.timeline.count
            if not entity.deleteMe(): raise ToolFailure(f"Could not delete {name}")
            removed.append(name)
            if design.timeline.count != before - 1:
                raise ToolFailure("Unexpected cascade while deleting; inspect the remaining document")
    except Exception as exc:
        exc.recovery = {"status": "partial" if removed else "not_attempted", "removed": removed,
                        "restoration_verified": False, "reason": "Deletion is not reversible by timeline cleanup"}
        raise
    return {"completion": "completed", "deleted": removed, "timeline_count": design.timeline.count}


def fingerprint(design):
    value = tree(design)
    bodies = []
    for component in items(design.allComponents):
        for body in items(component.bRepBodies):
            box = body.boundingBox
            bodies.append([body.name, body.volume, body.faces.count, body.edges.count,
                           box.minPoint.x, box.minPoint.y, box.minPoint.z,
                           box.maxPoint.x, box.maxPoint.y, box.maxPoint.z])
    return hashlib.sha256(json.dumps([value, bodies], sort_keys=True).encode()).hexdigest()


def remember(design, references):
    _undo[:] = [(design.parentDocument, references, fingerprint(design))]


def undo_last(design):
    if not _undo or _undo[-1][0] != design.parentDocument:
        raise ArgumentError("No supported agent feature change to undo. Use feature_tree and delete_entities; Fusion UI Undo remains available to you.")
    _, references, state = _undo[-1]
    if fingerprint(design) != state:
        raise ArgumentError("Design changed since the agent operation; inspect before a targeted edit")
    result = delete(design, references)
    _undo.clear()
    result["scope"] = "last supported additive agent feature change; not global UI undo"
    return result


def invalidate_undo():
    _undo.clear()
