# Agent interface validation — 2026-09-05

This iteration addresses the failures in
[the reviewed GLM chat](studio-session-review-2026-09-05.md). It changes the
interface and runtime around the model, using the same configured GLM-5.3 Flash
worker and vision roles with low effort. No model upgrade was needed.

## Implemented

- Explicit argument schemas, required fields, item types, enums and bounds;
  client and native validation identify the offending field before execution.
  Protocol binding fields are validated against the same schema shown to the model.
- `feature_tree`, `inspect_entity`, and returned native entity references expose
  sketch line/reference counts, feature types, ownership and native health.
- `lattice_pockets` supports sharp sketches, rounded sketches, and straight
  sketches with a separate native fillet. `edit_lattice` replaces its managed
  sketch/cut/fillet chain with preflight and recovery reporting.
- `delete_entities` accepts explicit references. `delete_named_features` accepts
  real arrays and sketches-only selections. `undo_last` completes synchronous
  native deletion of the last supported additive agent change.
- `read_file` supports repository-relative paths, symbols and line ranges;
  `search_files` bounds searches; `patch_file` requires an unchanged SHA-256 and
  unique replacement. Writes preserve backups, validate Python syntax and tool
  contract structure, and report activation requirements.
- Studio starts with 12 tools and a short prompt; `load_tools` adds groups.
  GLM/OpenAI-compatible history compacts older successful results while preserving
  references and errors. `get_result` retrieves cached fields. Default generation
  budgets are 2,048 action tokens, 4,096 source tokens and 700 vision tokens.
- Related mutations share a final visual review. Clean captures hide sketches
  temporarily. Vision receives the expected stage, reports uncertainty, and does
  not establish native feature contents. Premature final text is held until the
  fallback review completes, including image delivery without a separate describer.
- Repeated identical failures are blocked after two attempts; five failed calls
  without a successful mutation stop the loop with an incomplete-task message.
- Transcripts retain capture completions and separately measure active time,
  worker latency/usage and vision latency/usage. Interrupted runs report observed
  elapsed time as a lower bound. Document listing distinguishes same-named documents.

## Live results

The three original user requests were replayed in separate disposable documents.
These are development observations, not a statistical speed or reliability claim.

| Measurement | Original user chat | First successful replay | Final replay |
|---|---:|---:|---:|
| Active time | 438.6 s | 99.1 s | 55.0 s |
| Worker input tokens, including cache | 727,659 | 56,940 | 36,239 |
| Worker output tokens | 11,472 | 1,338 | 842 |
| Explicit tool attempts | 28 | 7 | 4 |
| Failed recorded calls | 7 | 0 | 0 |
| Missing tool completions | 2 | 0 | 0 |
| Worker reported cost | $0.018063 | $0.002673 | $0.001837 |
| Vision reported cost | unrecorded | $0.001054 | $0.000272 |

The final run used 3,175 input tokens in its first request versus 12,861 in the
original. It finished with five healthy native timeline entries: plate sketch,
plate extrusion, lattice sketch, lattice cut, and one fillet. The lattice sketch
had 51 non-reference lines for 17 triangles, four reference boundary lines, and
zero arcs/circles. Its isometric capture was inspected and showed a continuous
perimeter and alternating triangular pockets. The final native acceptance
assertions passed.

Both successful replays chose a native fillet from the first request, so their
later requests required less work than the original failed revision. The first
replay also executed `edit_lattice`; the final replay inspected the already-correct
sketch instead of needlessly rebuilding it. A separate live fixture explicitly
started with rounded sketch geometry, converted it with `edit_lattice`, and
verified the resulting straight triangle sketch plus separate fillet. A scoped
undo probe removed a newly added extrusion and sketch synchronously and returned
the original five-entry, one-body state.

Development also included a provider DNS failure before any tool executed, and
one failed replay that was stopped after 27 attempts (18 failures, at least
170.7 seconds observed). The failed replay exposed ambiguous box axis labels,
all same-named documents marked active, rejection of advertised binding fields,
and ineffective stopping when the model varied bad arguments. Those defects were
fixed before the final run and now have regression coverage. The failures remain
in the evidence; the successful runs are not an average across all attempts.

## Evidence and reproduction

Local, ignored evidence is in
`benchmarks/runs/2026-09-05-agent-interface/`: all five relevant transcripts,
captures, the final native feature tree, a manifest with metrics, and native
operation records for revision and scoped undo. Transcript suffixes:

- Original: `165430-eda2b4d0`; first success: `174451-1df5ba68`.
- Provider failure: `175734-500650ff`; interrupted failure: `175923-7d5af119`.
- Final success: `180411-d7c46418`.
- Native sketch conversion operation: `c458a87d3f1f419195761e528deac322`.
- Scoped undo operation: `ffda55785f6a49cb946c3d78a55ec8d6`.

Run offline checks with:

```sh
/usr/bin/python3 -m unittest discover -s tests
./studio/.venv/bin/python -m unittest discover -s studio/tests -t .
```

Validation: 245 core and 101 Studio tests pass. The existing unclosed-file
ResourceWarning in `test_primitives.py` remains unrelated.

For an intentional paid live replay with Fusion open:

```sh
./studio/.venv/bin/python studio/replay_revision.py --live
python3 studio/analyze.py 180411
```

Studio was restarted on `http://127.0.0.1:8765`; bootstrap confirmed configured
GLM and the connected bridge with the new tools. Existing transcripts survive
restart; in-memory conversations start fresh. The saved original assembly and
the user's prior successful CAD document were not edited by these tests.

## Limits

Entity handles identify entities in an open document for this add-in session;
they are not persistent project IDs or a complete revision system. Dependency
inspection covers profile sketches where available. Deletion and lattice
replacement conservatively require the explicitly selected complete trailing
timeline chain. Older unmanaged lattices need inspection and explicit conversion;
there is no automatic migration of arbitrary existing models.

Scoped undo covers supported appended features, not parameter edits, deletion,
or global UI actions. Its fingerprint is a guard, not a comprehensive detector
of every manual change. Failed replacement attempts try to rebuild the old recipe;
they still report partial recovery and changed entity identities.

Source writes and capability refresh do not hot-reload a running Studio process.
Geometry helpers must be imported by a registered reloaded module. `recipes/`
is a project-code convention, not a general runner. History compaction and action
output budgets were implemented and measured for the GLM/OpenAI-compatible path;
other providers need their own live acceptance runs. The 80-word response target
is prompt guidance, not hard truncation; the final replay still exceeded it once.

The saved-assembly revision milestone, arbitrary dependency repair, reopen
identity, full restoration, and broader model comparisons remain open in PLAN.md.
Native API references used for inspection/deletion: Autodesk's
[Profile.parentSketch](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Profile_parentSketch.htm),
[Timeline.markerPosition](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Timeline_markerPosition.htm),
[TimelineObject.entity](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/TimelineObject_entity.htm),
and [Sketch.deleteMe](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_deleteMe.htm).
