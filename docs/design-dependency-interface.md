# Design dependency interface — September 6, 2026

Studio and MCP now support a compact design record plus general reference-based
geometry tools. The goal is to distinguish fixed inputs, unknown dimensions and
dependent geometry before committing features. There is no drivebase-specific
construction sequence in the prompt.

## Reasoning and state

`studio/agent_runtime.py` instructs the agent to resolve consequential unknowns,
justify critical dimensions with evidence or explicit assumptions, and verify a
relationship before building further geometry that depends on it. Targeted
intermediate captures are permitted to resolve orientation and interface selection.
Simple edits do not require a formal plan or user approval. The low effort default
remains; this change does not raise reasoning effort globally.

`update_design_state` is a client-local tool in `fusion_cad/bridge_client.py`, used by both
Studio and MCP. It records up to 16 concise entries (10000 characters total), with
id, statement, kind, status, dependencies, evidence references and source. Updates
are atomic; cyclic dependencies, unknown evidence references and established derived
facts without evidence are rejected. Explicit assumptions remain visibly labeled.
The optional `evidence_refs` field retrieves original observations.

Measurement and verification calls return `evidence_ref`. The state tracks those
observations independently of chat compaction. Native edit attempts conservatively
invalidate geometry evidence, including failed or timed-out edits. A derived fact
whose prerequisite is unresolved is marked blocked. The agent's next request gets
the current record; stale dependencies are also surfaced in the UI.

These checks establish provenance and freshness within this client, not semantic
correctness. The model can still select the wrong face or misinterpret a drawing.
Manual edits and edits through other clients are not continuously monitored. Studio
invalidates evidence between user turns, and restored records invalidate it on load.
The current policy invalidates all geometry evidence after an edit, so independent
measurements can require more rechecking than necessary.

Studio writes records under `agent_home()/design-states/<chat-id>.json`. Reopening a
known chat URL restores its record after a server restart, with an explicit fresh
model-context notice; it does not restore provider-native conversation history.
The existing chat transcript archive remains separate. MCP state lasts for its
server process. Existing MCP processes must reconnect to load the changed client.

## Tools

| Tool | Contract |
|---|---|
| `update_design_state` | Read/upsert the compact dependency record; no CAD changes. |
| `inspect_geometry` | List exact occurrence paths, bodies and paged faces. Return native selectors, root-frame bounds, face points and planar/cylindrical geometry. |
| `measure_geometry` | Closest-point distance or signed parallel-plane separation, with explicit root frame and centimeters. |
| `verify_relationship` | Check distance, minimum clearance, parallelism, coplanarity or plane separation. Return actual, expected, tolerance and passed. |
| `place_component` | Move/copy a top-level occurrence by an explicit anchor and target, optional world-axis rotation. Verify the actual transform and anchor error. |
| `extrude_between_faces` | Use a planar face section and another parallel face as a native referenced extent, creating a new solid body. |

Placement creates a snapshot, not a joint. Copies share component geometry and are
created ungrounded. Existing grounding requires `release_grounding=true`; joined
or rigid-group occurrences are refused. Nested placement is not implemented.
Selectors are exact; the tools do not infer mounting datums. Face-to-face extrusion
is not arbitrary profile generation, tube construction or automatic clearance design.
Existing `verify_spec` and interference tools remain available for broader checks.

The initial Studio surface contains 12 tools, including the new design record.
Spatial tools load through measurement, assembly, geometry and validation groups.
MCP exposes the shared client record and native registry without Studio's grouping.

The native implementation follows Autodesk's documented [minimum-distance API](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/MeasureManager_measureMinimumDistance.htm),
[assembly-context face proxies](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace_createForAssemblyContext.htm),
[occurrence transforms](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence_transform2.htm), and
[referenced extrusion extents](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ToEntityExtentDefinition_create.htm).

## Validation

- 262 core tests and 113 Studio tests pass, including dependency validation,
  stale evidence, failed-check rejection, persistence/reopening, MCP exposure,
  root-frame transform math, selector ambiguity and pinned runtime context.
- Browser smoke tests pass at widths 320–1000 px, including the expanded design
  record, short-panel layout, correction action, drafts and image restoration.
- `python3 scripts/validate_design_tools.py --live` passes in a separate unsaved
  Fusion document and restores the original active document. It checks a 49 mm
  measured span and native referenced extrusion, passing/failing relationship
  checks, 90-degree component copying, target-relative placement, 40 mm closest
  distance, and 50 mm separation between selected transformed faces. The captured
  model was visually inspected. Test documents remain unsaved for inspection.
- Live testing uncovered and fixed the existing `ConstructionPlane.parentComponent`
  inspection error using the documented component property. It also exposed
  Fusion's separate ground-to-parent setting and the fact that an extrusion's
  participating-body collection can include its source body.

`studio/evaluate_design_reasoning.py --live-model` runs a fresh-context comparison
on three deterministic simulated fixtures. Both arms have the same tools; the
intervention adds dependency guidance and pins the current record. These are small
spacer/standoff/stop tasks with the same basic extrusion structure, not an evaluation
of complex assembly competence or native modeling success.

| Across three fixtures | Execution guidance | Dependency guidance |
|---|---:|---:|
| Constructed correct simulated span | 3/3 | 3/3 |
| Construction before measurement | 0 | 0 |
| Tool calls | 15 | 14 |
| Failed calls | 1 | 1 |
| Output tokens | 876 | 1165 |
| Input tokens | 42229 | 46486 |

The new guidance does not demonstrate an efficiency or correctness advantage on
this small sample. Output tokens increased. One baseline run had a 631-second
latency outlier; these timings cannot establish a speed improvement. Full traces
and native acceptance evidence are in the ignored local directory
`benchmarks/runs/2026-09-06-design-dependencies/`. The full drivebase has not been
rerun as part of this change.
