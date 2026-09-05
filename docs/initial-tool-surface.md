# Initial tool surface review — 2026-09-05

The first 12-tool selection was biased toward the plate/lattice replay. It exposed
three shape-specific operations immediately, hid general body/component inspection,
and put benchmark dimensions and lattice revision instructions in every request.
The successful replay did not establish that this was a good default for other CAD.

## Selection principle

Start with tools that establish the document, inspect existing state, observe
results, discover capabilities and resolve pending operations. Load modeling
schemas when their task is known. There is no target count of 12: the current
selection needs 10 tools. This is a reasoned default, not an empirically proven
optimal set; operation frequency across varied real tasks has not been measured.

| Initial tool | Why it belongs initially |
|---|---|
| `design_summary` | Bodies, components, topology and dimensions; useful even on imported designs without a feature history. |
| `feature_tree` | Native feature/sketch identity, sequence and health for understanding editability. |
| `inspect_entity` | Focused inspection of a returned feature or sketch instead of repeating a broad query. |
| `capture_viewport` | Observe the current or finished model independently of its intended construction. |
| `list_documents` | Locate the correct open document. |
| `switch_document` | Select that document before working. |
| `operation_status` | Resolve pending/unknown operations before another mutation. |
| `cancel_operation` | Cancel queued work without loading another group first. |
| `load_tools` | Discover and expose task-relevant capabilities using a compact group directory. |
| `get_result` | Recover earlier evidence after context compaction without repeating native work. |

The three old constructors move out of the default: `create_test_box` is in
`geometry`, and `lattice_pockets`/`edit_lattice` are in `patterns`. The other nine
initial tools remain; `design_summary` is added.

General extrusion/cut tools are also loaded on demand. They are more reusable
than a lattice generator, but an import, assembly inspection, calculation or
existing-feature diagnosis does not need their coordinate schemas. Loading adds
a model round trip before using a new group. Multiple group loads can be emitted
in one model turn. The groups reset for each new user task, as before this change.

## Available groups

`load_tools` describes these groups directly in its schema. The definitions and
descriptions live together in `studio/agent_runtime.py`.

| Group | Operations |
|---|---|
| `geometry` | New box/document, polygon extrusion and cuts |
| `editing` | Explicit trailing-feature deletion and scoped undo |
| `patterns` | Isogrid/hex lattices, managed lattice revision, router pocket fillets |
| `assembly` | Imports, bodies to components, joint candidates, joints, interference |
| `generators` | Specialized tube frames, bellypans, fasteners, replacement isogrid bars |
| `engineering` | Mechanics calculations, thread inspection, 6061 material assignment |
| `validation` | Dimension/envelope/body-count checks and interference |
| `files` | Listing, reading, downloading, PDF rendering and CAD imports |
| `source` | Focused source reads/search/patch/write and tests |
| `documents` | Metadata and saving to an existing target |
| `diagnostics` | Bridge health and Fusion command inspection |
| `all` | Full registry, including newly registered tools not yet categorized |

All current native/control tools are assigned to at least one group. Related
groups intentionally overlap on imports and interference. The general prompt
does not include the replay plate's dimensions or favor a particular generator.
Axis and lattice-representation guidance is added only when its group is loaded.

## What this does not generalize

This is tool selection and discovery, not a rewrite of the CAD kernel interface.
`inspect_entity` currently inspects features/sketches, not arbitrary faces, edges
or assemblies. Polygon tools use planar coordinates. There is no general exposed
sketch-constraint, parameter-edit, revolve, loft, shell or arbitrary fillet tool.
`edit_lattice` is a managed latest-chain edit, not a generic feature editor.
`create_test_box` creates a rectangular body and a document together; it does not
provide an independent empty-document constructor.

These limitations are stated in loaded guidance rather than hidden behind a
generic `execute` wrapper. Extending those native primitives requires separate
implementation and live geometry validation. The MCP server still exposes the
full registry; these selection changes affect Studio.

## Verification

246 core and 103 Studio tests pass. New checks cover full registry reachability,
absence of specialized constructors from the initial surface, and group loading
for extrusion, assembly, import, mechanics, validation and lattice editing. They
also verify that specialized instructions are absent until loaded.

Read-only validation against the running Fusion registry confirms all 43
native/control tools are reachable and the initial set contains the intended 10.
No native handlers or geometry changed in this review. No paid-model speed or
quality benchmark was rerun; the earlier 55-second plate result belongs to the
previous initial selection. Broader live-task evaluation is still needed before
making performance claims for this default.
