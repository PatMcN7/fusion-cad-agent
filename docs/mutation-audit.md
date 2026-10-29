# Mutation and recovery audit

Source review dated 2026-09-05. Live proofs are pending. `mutating` currently
means CAD/session mutation in the registry; filesystem tools need a distinct
effect classification as the recipe runtime develops.

| Tool | Effects | Current recovery boundary |
|---|---|---|
| `create_test_box` | Creates a document and features | Appended features; new document can remain |
| `replace_with_isogrid_bar` | Deletes old geometry, adds features, assigns material | Separate additive stages; old geometry is not restored |
| `create_tube_frame` | Creates frame features in stages | Only failed stage's appended entries |
| `add_bellypan` | Adds plate, pockets, holes, material | Earlier stages/material effects can remain |
| `add_fasteners` | Cuts holes, adds screws and threads | Separate stages; earlier work can remain |
| `cut_polygons` | Adds sketch/cut; may create construction surface | Timeline cleanup; prior body geometry unverified |
| `extrude_polygons` | Adds plane/sketch/extrusion | Timeline cleanup; full prior state unverified |
| `lattice_pockets` | Adds sketch/cut; may rename result | Timeline cleanup; name changes outside its scope |
| `router_fillet_pocket_corners` | Adds fillet feature | Timeline cleanup; prior body geometry unverified |
| `import_step_component` | Imports bodies/components | Timeline cleanup; native import recovery unverified |
| `import_geometry` | Imports supported file formats | Format-specific effects need native proof |
| `create_joint` | Adds joint, moves occurrence, may flip | Deleting joint may not prove restoration of transforms |
| `bodies_to_components` | Creates occurrences and moves body ownership | Native ownership restoration unverified |
| `delete_named_features` | Deletes preexisting features/sketches | No automatic restoration; explicitly unsupported |
| `set_6061_material` | Changes existing body materials | No previous-value restore yet |
| `save_document` | Saves native/cloud document state | No save rollback |
| `undo_last` | Executes Fusion undo | No general redo/restore proof |
| `switch_document` | Changes active document | Updates client binding on success; no automatic switch-back |

All registered CAD mutations require operation and document IDs. These provide
delivery identity and destination checks, not whole-tool atomicity. Each wrapped
handler reports stage/recovery context on errors and attempts to include current
design evidence.

`write_file` changes repository files with backups and optional test execution.
It remains outside the CAD mutation registry flag; it is not covered by the CAD
operation journal. `fetch_url`/PDF rendering create local artifacts, and test
execution can have filesystem effects. None is a CAD transaction.

The agent write fence now also excludes `fusion_cad/execution.py`, which owns journal and
document-binding state and is not hot-reloaded while the bridge runs.

Next recovery work: implement previous-value restoration for the first native
parameter-edit workflow, then prove a snapshot/undo strategy on disposable
documents before relying on it for destructive and compound operations.
