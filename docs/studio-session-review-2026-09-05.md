# Studio session review — 2026-09-05

Reviewed `session-20260905-165430-eda2b4d0.jsonl`, the captured top and isometric
views, current source, and the source at `9724291`. Worker and vision roles both
used `z-ai/glm-5.3-flash`; worker effort was already `low`. This review does not
change the running app or CAD document. The session's uncommitted change to
`bridge_client.py` is preserved.

## What the user actually asked

1. Build an 8 × 6 × 0.25 inch plate with a rounded isogrid.
2. Replace rounding in the sketch with a separate native fillet feature.
3. Correct the leftover rounded sketch after the agent said the revision was done.

The geometry primitives for sharp lattice cuts and native pocket fillets already
existed. The difficulty was expressing and verifying the revision of an existing
feature chain, compounded by a broken argument schema and ambiguous undo results.

## Measurements

| Measurement | Observed |
|---|---:|
| Initial creation, user message to done | 82.249 s |
| Requested native-fillet revision | 95.697 s |
| User correction to final done | 260.634 s |
| Total active response time | 438.580 s (7 min 19 s) |
| Worker completions | 31 |
| Explicit tool attempts | 28 |
| Recorded tool completions | 26; two capture completions missing |
| Recorded tool execution intervals | 11.348 s |
| Capture/vision intervals inferred from adjacent events | 63.903 s |
| Remaining active time, largely worker requests | 363.329 s |
| Worker cumulative input tokens | 727,659; 673,472 cached (92.6%) |
| Worker cumulative output tokens | 11,472 |
| Per-request input, first → last | 12,861 → 40,424 tokens |
| Worker-reported final cumulative cost | $0.018063; vision cost absent |
| Visible assistant text | 8,514 characters |
| Recorded reasoning text | 26,738 characters |

Time attribution is approximate: the transcript does not separately instrument
provider latency, first token, capture duration, vision duration, or kernel time.
The two explicit capture intervals above include their tool execution; automatic
capture/vision intervals begin at the preceding mutation's completion. The
remaining time must not be labeled pure model inference.

The existing analyzer reports 8.9 minutes and 98% "in the model." That includes
about 94 seconds of startup/user idle time and assigns capture/vision overhead to
the worker. It also labels file reads, source writes, and deletion attempts as
BUILD. Those numbers are unsuitable for evaluating efficiency without correction.

## Findings and changes in priority order

### 1. Replace prose-derived argument types with explicit schemas

All six `delete_named_features` attempts failed with `feature_names must be a
non-empty list`. The exposed schema called the argument a string because its
description begins "Non-empty list", while the parser recognized only certain
other phrasings. The same defect affected `sketch_names` and `body_names`.

The transcript shows strings such as `"[\"Isogrid Lattice Cut\"]"` arriving at
the handler. This was not a list lost in transit or rejected before dispatch, as
the worker claimed. The advertised schema encouraged the wrong representation.

Use explicit JSON Schema types, required fields, item types, bounds, and enums
for every public argument. Test model-facing schemas against representative
handler inputs, including arrays. Validate inputs before a Fusion request and
report the offending path, expected type, received type, and a valid example.

The worker broadened the inference heuristic, but that is only a partial fix.
Studio imports `bridge_client` once; a file write does not reload the live
module. Tool specs are also captured once per user turn. Introduce a capability
version and a supported refresh boundary, and report restart requirements
accurately for modules that cannot refresh in place.

### 2. Make edits address sketches/features directly

The delete tool cannot delete only a sketch: it requires a nonempty feature
list even when sketch names are supplied. The worker consequently guessed sketch
names in a feature-name field and fell back to global undo four times.

Provide a compact feature-tree query returning entity references, type, owning
component, dependencies, visibility, suppression, and health. Add targeted
sketch inspection with line/arc counts and dependent features. Deletion should
accept explicitly identified sketches or features and report affected dependents;
dependency removal must not silently expand to unrelated geometry.

Creation results should return handles for the sketch, cut, and fillet they
created. A revision can then address the previous result without five broad
`design_summary` calls or guessing suffixes such as `Cut` and `(1)`.

Build a staged replacement/edit operation on these primitives. For this task,
support `corner_mode: sharp | sketch_rounding | feature_fillet` and preserve the
association between generated sketches and features. Treat this as a revision,
with recovery evidence, rather than generating another cut over the previous one.

### 3. Make completion results describe completed work

`undo_last` calls `UndoCommand.execute()` and immediately returns
`undo_requested: true`; the wrapper immediately captures a design digest. Twice
the digest still showed the old volume and timeline depth, while a later summary
showed the cut was gone. That invited another undo before the first result was
understood.

The operation protocol currently treats the handler return as completion. UI
commands that complete later need an explicit pending state and subsequent
completion observation. Do not present a queued undo's digest as final state.
Prefer targeted feature operations for edits and recovery.

The final response also overstated cleanliness. The last result had nine
timeline entries; the worker claimed a clean three-feature sequence without
querying the final feature tree. The last feature list before rebuilding still
contained older cut and fillet entries. The exact final health was not recorded.
Verification must cover native editability and sketch contents, not just volume
and face counts.

### 4. Reduce unnecessary and misleading vision feedback

Eight vision descriptions consumed about 64 seconds including capture overhead.
The describer called a correct intermediate blank plate "wrong" because it had
no lattice yet. It repeatedly inferred off-centering from perspective, and
described visible sketch/vertex markers as spherical geometry. The worker then
invented an explanation that the markers were toroidal fillet patches.

Give the reviewer the expected result of the current stage, the chosen view, and
which later features are intentionally absent. Distinguish visible sketch and
selection overlays from bodies. Capture an appropriate orthographic view for
planar layout and a close-up when corner shape matters; offer a presentation
capture that temporarily hides overlays and restores the user's visibility state.

Run vision at meaningful modeling boundaries or when specifically requested,
not automatically after every successful mutation. Return compact observations,
uncertainties, and suggested measurement targets. Determine centering, sketch
curve types, feature existence, and radius from native measurements. Vision should
trigger a targeted check when it disagrees, not start a speculative argument.

### 5. Give source extension a small, usable interface

The agent read a 124,925-character `tools.py`, but the provider truncates tool
results to 24,000 serialized characters. It cannot request a line range or a
symbol. Relative `read_file("tools.py")` resolved to `/tools.py` and failed, while
the write tool supports repository-relative paths.

The only edit operation requires whole-file replacement. A tiny schema change
therefore became a complete 13 KB rewrite of `bridge_client.py`. That response
used 3,132 output tokens. Later, the model spent roughly 60 seconds and 2,351
output tokens considering how to rewrite the 125 KB tool module, then abandoned
the edit. These two completions account for 47.8% of all worker output tokens.

Add repository-relative reads, symbol/range reads, bounded text search, and a
patch operation with expected-content/hash checks. Preserve syntax validation,
backups, and relevant tests. Keep project recipes separate from the shared
runtime and make shared-module activation requirements explicit. A supported
CAD revision should not require repairing the application first.

### 6. Shorten the normal loop and measure the right thing

Load task-relevant tool groups on demand; there were 37 tool definitions in this
session and a 12,861-token first request. The full engineering brief and source
index are useful for extension work but need not accompany every ordinary edit.

Keep recent results and current design facts in context; compact resolved errors,
old captures/descriptions, and source dumps while preserving operation/entity
references and unresolved issues. Truncate by structured fields with a retrieval
handle, rather than cutting a serialized JSON string in the middle.

Use a short response contract: a brief action statement only when useful, tool
execution, and a concise outcome with unresolved findings. Effort was already
low; lowering it alone does not repair the interface. Add a repeated-error check
so an unchanged type failure triggers one focused diagnostic instead of six
near-identical calls. Keep exploratory reasoning capacity for genuinely novel
geometry, with separate limits for ordinary action selection and vision output.

Fix instrumentation before comparing models. The recorder assumes verification
is a list, but captures return a dictionary; the resulting AttributeError is
silently swallowed and drops both explicit capture completions. Record active
turn spans, provider/vision usage separately, first-token and completion timings,
operation completion state, and meaningful activity categories. Claims that code
was patched should come from file-change evidence: this session's final answer
claimed a `tools.py` patch, but only `bridge_client.py` was written.

## Acceptance target for the next iteration

Replay these same three requests in a new disposable document using the same
worker configuration. Require no source edits, no guessed entity names, no
repeated type errors, and no blind undo retries. Verify a sharp lattice sketch,
one intended cut, one native fillet, no orphaned rounded sketch, and healthy
dependencies. A revision should need approximately one focused inspection,
one staged edit, and one verification/capture boundary; this is a design target,
not a measured speedup. Compare active latency, completions, tokens, and human
corrections against this recorded session.
