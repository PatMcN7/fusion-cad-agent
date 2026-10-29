# Fresh-context drivebase evaluation — 2026-09-05

**Result: incomplete.** The harness built tube frames and triangularly lightened
bellypans, but never assembled four swerve modules at the corners. Final captures
show one imported module near the center of the frame/pan.

## Conditions

Source commit `9577865`, unmodified Studio agent loop, configured GLM-5.3 Flash
worker and vision model, low worker effort. A new Studio session
`186a2c35836b` had zero prior messages. It received one user request and no coaching,
repair hints, or intervention messages. The session remains available in Studio.

The request came from the August 27 `001703-9ffadf81` session. Two changes were
recorded: target new unsaved documents instead of existing `cadex_db` documents;
retain the written tube/module arrangement without references to colors in the
missing original sketch. The old attachment was not recoverable from the saved
transcript. The existing local WCP-1702 STEP file and original drawing URLs were used.

## Measurements

| Metric | Original attempt | Fresh attempt |
|---|---:|---:|
| Worker effort | high | low |
| Active response time | 26m 54s | 8m 50s |
| Explicit tool attempts | 41 | 35 |
| Recorded failed calls | 6 | 6 |
| Missing tool completions | 8 | 0 |
| Worker output tokens | 56,279 | 6,080 |
| Worker cumulative input, including cache | 1,678,735 | 741,401 |
| Final requested assembly | incomplete | incomplete |

The fresh run spent approximately 293 seconds in worker requests, 172 seconds
in recorded tool execution and 65 seconds in vision. It used five new documents,
three frame builds, two successful bellypan builds and two successful module
imports. There were no source edits. It selected `load_tools("all")` immediately.

This is one run, with a missing image, different starting documents and different
effort. Lower token/time figures do not establish improved task success or isolate
the impact of the interface changes. Original vision cost was not recorded.
Exact fresh usage/cost is retained in the local metrics artifact.

## Failures supported by the trace

1. **Requirements did not control construction.** The model read a 0.196-inch
   tube hole diameter, but omitted `hole_diameter` when building, producing the
   0.125-inch default. It used `wide_horizontal`: native tube sizes were
   28 × 2 × 1 inches, despite repeatedly calling them skinny-side-up. It chose
   tube length before establishing the modules' corner/socket placement.
2. **Document effects were misunderstood.** After importing a module, it said
   it would build the frame in the same document, then called `create_tube_frame`
   without its active-document option. That created another document. Three
   subsequent mutations were rejected for mismatched document bindings. The
   frame generator's active-document mode is limited to empty designs, so simply
   toggling the option would not solve composition into an existing assembly.
3. **Candidate assembly tools were not used effectively.** `import_geometry`
   produced a module assembly with roughly 90 occurrences. The agent tried to
   mate an individual module plate to a tube; Fusion returned
   `InternalValidationError`. It stopped to request manual grouping, overlooking
   the exposed `import_step_component` tool, whose grouped-import path creates a
   parent component. That alternative was not attempted or validated in this run.
4. **Recovery claims exceeded evidence.** The joint result explicitly said
   `status: partial`, `restoration_verified: false`, with no appended entries
   removed. The final answer nevertheless called it rolled back. An unchanged
   timeline count does not establish restored occurrence positions/properties.
5. **Validation has coverage problems.** The candidate tool first measured
   16.753009 cm³ of overlap among the four tubes, then reported zero after module
   import without a recorded frame repair. Zero was returned by the tool, not
   invented by the model; this inconsistent result requires investigation. The
   independent post-run `feature_tree` query failed because a `ConstructionPlane`
   lacks `parentComponent`. Full native health could therefore not be established.
6. **The response contract is still soft.** The final answer was about 1,480
   characters, used sections and offered manual options, despite the short-final
   guidance. Loading all tools also bypassed selective schema loading.

## Final evidence and preservation

Final active test document ID:
`5e2aea74298b483aa53e2ebe1e553a49_7f7ebe07eed44439882bd3db5de12c9a`.
The native digest recorded 25 timeline entries and 133 bodies across components:
frame/pan plus one imported module. The pan was 28 × 28 × 0.0625 inches, assigned
6061 material, with 131 triangular pockets and 224 bolt holes. Those partial
results do not satisfy the requested module-defined corner arrangement.

Open documents increased from 11 to 16. Comparing the original 11 documents'
names, saved/modified flags and body counts showed no changes. No preexisting
document was targeted by a recorded mutation; this is not a full geometry hash.

Transcript:
`session-20260905-183649-b221c717.jsonl` under the Studio transcript directory.
Local evidence is copied to `benchmarks/runs/2026-09-05-drivebase-fresh/`, including
the submitted request, empty-session record, manifest, metrics, transcript,
native inspection result/error and final captures. The current-view capture
`capture-20260905-184509-current.png` and the earlier isometric capture were
visually inspected.

No runtime or geometry fixes were applied during or after this evaluation.
The next work should connect requirements to measured native state, make document
targets explicit for compound generators, validate grouped module insertion and
mating, and make unsupported inspection or recovery report uncertainty reliably.
