# Live runtime smoke test — 2026-09-05

Protocol v4 was tested through the shared CLI client against the running Fusion
add-in. Source baseline: `2441dd9`, plus the cleanup and topology-check fixes
recorded with this report. This is a smoke test, not completion of Phase 1 or any
registered benchmark in `benchmarks/cases.json`.

The original saved `cadex_db_assembly` remained unmodified, with 132 bodies. All
geometry changes used a new unsaved `Untitled` document containing one body,
`Runtime Test Plate`. It remains open for inspection. No native archive was
saved; the screenshots and operation records do not replace a native fixture.

## Observed results

| Case | Result |
|---|---|
| Native creation | Created an 8 × 6 × 0.25 in plate: one solid, six faces, two timeline entries, 12 in³. |
| Duplicate request | Repeating creation with the original operation ID, arguments, and document ID returned `replayed: true`; the document count stayed at two. |
| Stale binding | A material edit bound to the original assembly while the test plate was active was rejected before mutation. |
| Dense geometry | A 0.2 in cell / 0.02 in rib request was rejected at 2,635 cells and 7,905 curves; the plate remained 12 in³ with two timeline entries. |
| Failure before fix | A collinear three-point polygon produced no profile. Cleanup failed because TimelineObject has no `deleteMe`; the error truthfully reported failed recovery and the leftover sketch. |
| Failure after fix | Repeating the invalid sketch removed `Recovery Retry Probe Profile`. Timeline depth returned to five; the existing solid retained its dimensions, 210 faces, and 123.22204541399375 cm³ volume. Recovery remained `partial`, since complete tool restoration was not independently established. |
| Rebuild | Deleted the original test cut, its sketch, and the first failed sketch by exact names. This returned the plate to two entries and 12 in³. The corrected lattice then rebuilt successfully. |
| Final geometry | 17 rounded triangular through-pockets, 204 sketch curves, one solid, four timeline entries. Volume 7.5195 in³; material reduction 37.3377%; outer dimensions unchanged. Nine checks passed, no warnings. |
| Outcome lookup | A separate CLI invocation retrieved the final operation as `succeeded` with its stored result. This did not simulate a lost response or a restart. |
| Visual inspection | Top and isometric captures show alternating triangular pockets, continuous ribs, rounded corner approximations, an intact perimeter, and no leftover test sketch. |

Lattice arguments: `cell_size="2 in"`, `rib_width="0.25 in"`,
`cutter_diameter="0.125 in"`, `corner_segments=3`, default isogrid staggering.
The body retained its default Steel material. This was a geometry/runtime test,
not a structural or machining qualification.

## Fixes driven by native evidence

Cleanup now obtains the native sketch/feature from `TimelineObject.entity` and
calls that entity's deletion method. Missing or unsupported entities stop
cleanup with an explicit report. The old test double incorrectly put `deleteMe`
on the timeline wrapper; it now matches Fusion's object shape. See Autodesk's
[TimelineObject.entity](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/TimelineObject_entity.htm)
and [Sketch.deleteMe](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_deleteMe.htm)
references.

Rounded pocket segments had also caused `cells_tessellate` to report zero
junctions. The tool now checks junctions on the original cell polygons, while
overlap, rib gaps, and machining checks use the final rounded polygons. A
regression exercises both the valid staggered field and an invalid banded field:
rounding no longer creates a false warning or hides the genuine topology defect.

Validation: 230 core and 90 studio unit tests passed. The existing unclosed-file ResourceWarning
in `tests/test_primitives.py` remains unrelated to these changes.

## Local evidence and remaining gates

Operation records and inspected captures are copied into the ignored directory
`benchmarks/runs/2026-09-05-runtime-smoke/`. Its manifest maps each case to the
original durable journal record. Cost and kernel-only timings were not measured.

Still untested live: lost transport responses, polling while the kernel is busy,
queued cancellation, add-in restart with unfinished work, switching tabs during
queued execution, and failures after parameter edits, deletion, or compound
operations. Full geometry restoration, global reconciliation, document revision
tracking, and save/reopen continuity remain unimplemented or unverified.
