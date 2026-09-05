# Runtime implementation: protocol v4

Status: implemented, covered by offline tests, and smoke-tested in live Fusion
on 2026-09-05. See [live results](live-runtime-validation.md) for the tested cases
and remaining gates. The running add-in has loaded protocol v4. Future shell or
execution-module changes require an add-in restart; geometry modules hot-reload.

## Mutation identity and document binding

HTTP paths remain under `/v1/`; the protocol version advertised by the bridge is
now 4. A mutating request requires `confirm: true`, `operation_id`, and
`document_id`, in addition to `tool` and `arguments`. Older unbound mutations are
rejected before modeling begins.

`GET /v1/tools` supplies the current open-document ID along with the registry.
Successful tool responses include the observed document ID. Shared clients retain
that binding and validate it on the Fusion main thread immediately before the
handler runs. Reading `active_document`, `design_summary`, or `list_documents`
rebinds a client to the observed active document. Successful document creation or
switching updates its binding as well. Merely refreshing tool schemas does not.

Document names and tab indices are not IDs. An unsaved document gets an opaque
ID just like a saved document. IDs change when the add-in restarts or the document
is closed and reopened. Stable binding across calls and stale binding rejection
were observed in one live session; close/reopen and restart cases remain pending.
There is no persistent project identity or comprehensive manual-edit revision
tracking yet. Two edits to the same open document can therefore share its ID.

The studio creates a separate bridge client for each conversation. MCP and CLI
use the same transport implementation. Model-visible schemas accept optional
operation/document IDs for explicit retries; ordinary calls get them from the
client. Each new CLI invocation observes the current active document unless an
explicit `--document-id` is provided.

## Outcomes and retries

The shell records an operation before enqueueing it. Records live in
`agent_home()/operations/`, with atomic replacement, file flush, and directory
flush where supported. Each record stores a payload hash, IDs, timestamps,
execution state, and eventual result. It does not copy the original arguments.
Only the owning add-in opens a journal for recovery; multiple live add-ins sharing
this directory are unsupported.

| State | Meaning |
|---|---|
| `queued` | Recorded; the handler has not started |
| `running` | The handler may be modifying Fusion |
| `succeeded` | A successful response was durably recorded |
| `failed` | A failure response was recorded; read its recovery evidence |
| `outcome_unknown` | The previous outcome cannot be established automatically |

The same operation ID and payload reuse the original execution/result. Reusing
an ID with different arguments or a different document binding is refused. A
correlation `request_id` may change between transport attempts. A replayed
response is marked `replayed`; its geometry digest describes the original result.

An HTTP timeout does not cancel the queued/running operation. Its ID remains
owned by the executor, and a second delivery does not enqueue it again. After a
restart, unfinished records become `outcome_unknown` and cannot be replayed as
new work. An error while persisting a completed result is also treated as an
unknown outcome, because geometry may already have changed.

Use `operation_status` or:

```sh
python3 fusion_cli.py operation-status OPERATION_ID
```

This reaches `GET /v1/operations/OPERATION_ID` directly; it does not wait on the
Fusion event queue. The shared client refuses a different mutation while it has
an unresolved operation. That client check is advisory across independent clients;
it is not a substitute for the planned global revision/reconciliation coordinator.

Use `cancel_operation` or:

```sh
python3 fusion_cli.py cancel-operation OPERATION_ID
```

Cancellation can prevent queued work from starting. A running synchronous kernel
call cannot be interrupted by this implementation. Cancellation does not undo a
finished operation.

An explicit transport retry can specify both original identities:

```sh
python3 fusion_cli.py call TOOL_NAME --confirm --arguments '{}' \
  --operation-id ORIGINAL_OPERATION_ID --document-id ORIGINAL_DOCUMENT_ID
```

Supply exactly the original tool arguments. Check status first. The status record
includes the original document ID. An unknown outcome requires inspecting the
actual document; issuing a new ID would create a new operation, not recover the old
one. A durable reconciliation workflow for unknown outcomes is still pending.

## Recovery evidence

`recovery.status` is `restored`, `partial`, `failed`, or `not_attempted`.
`restored` is reserved for a stage with an independent successful restoration
verifier. Existing modeling tools currently provide timeline cleanup, not such a
verifier, so whole-tool restoration is never claimed just from matching counts.

Stage reports include removed entries, timeline counts when available, whether
timeline cleanup completed, and the cleanup failure reason. Whole-tool reports
also list earlier completed stages. Errors include a best-effort design digest.
The original modeling exception is retained if cleanup itself fails.

Deletion of preexisting features and root replacement explicitly disable
unsupported timeline recovery. Parameter-value restoration, document snapshots,
and protection against intervening manual edits need separate live implementation.

## Geometry workload limits

Provisional defaults:

| Environment variable | Default |
|---|---:|
| `FUSION_CAD_MAX_CELLS` | 250 |
| `FUSION_CAD_MAX_SKETCH_CURVES` | 6000 |
| `FUSION_CAD_MAX_FILLET_EDGES` | 1000 |
| `FUSION_CAD_MAX_CANDIDATE_ROWS` | 20000 |

Values must be positive integers in the Fusion add-in process environment.
These are ceilings, not a claim that every smaller operation is fast. Live
calibration remains pending. Tool arguments do not disable the limits.

Isogrid and hex generators count cells before allocating polygons. The lattice
tool checks rounded-corner expansion before creating the expanded field. Explicit
polygon tools check curve counts before sketch work. Fillet primitives check
actual edges before attempting the kernel operation; the replacement bar also
checks a conservative edge estimate before clearing the old geometry.

The error includes requested counts and exceeded limits. It states that the
guarded stage has not built geometry; earlier stages of a compound tool may
already exist. Solid bellypans skip unused lattice generation.

## Validation still required

- A tab change while work is queued, same-name document switching, and document
  identity across close/reopen. Basic native dispatch and stale binding rejection
  passed the initial smoke test.
- Lost responses and queued cancellation against the actual add-in.
- Small lattice/rounded-corner/fillet timings to calibrate limits.
- Feature, compound, and destructive-operation recovery measurements. A failed
  sketch's cleanup was verified; this is not a full restoration proof.
- Save/reopen identity, manual edits, and undo/redo for future revision tracking.

The fake event queue tests exercise the real shell dispatch methods, journal,
and timeout lifetime logic. They do not emulate Fusion's kernel or prove geometry
restoration. Do not advance the live gates in `PLAN.md` using those tests alone.
