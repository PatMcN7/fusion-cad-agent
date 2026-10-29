# Astra planner + Jev decisions (experimental)

This is a separate Studio mode, not another expansion of the Jev workflow menu.
Astra synthesizes a short plan from the goal and live native observations. Code
executes established steps. Jev is called only where the plan explicitly names a
bounded semantic choice. No model is asked to approve every tool call.

## Enable

1. Install/update Codex and authenticate with your existing `codex login` flow.
   The installed CLI must support `--ignore-user-config`, `--ignore-rules`,
   `--ephemeral`, and `--output-schema` (checked by the model readiness check).
2. Put `TYPESAFE_API_KEY=...` in the gitignored `studio/.env` for Jev decisions.
   Plans without choices do not need a Jev call/key. If a choice is required and
   the key is absent, execution stops with a configuration error, not a silent
   model fallback.
3. Restart Studio and select **Astra planner + Jev decisions (experimental)**.
   Or launch:

   ```sh
   STUDIO_PROVIDER=hybrid STUDIO_MODEL=gpt-6-astra ./studio/run.sh
   ```

The existing direct Astra/Codex and Jev-only modes remain available. The model
picker uses `hybrid:gpt-6-astra`; the planner receives the bare `gpt-6-astra` ID.
`STUDIO_HYBRID_JEV_MODEL` can pin the decision model; default `jev-1.13.0`.

Try a disposable active design first:

- `Create a centered 100 x 60 x 5 mm plate in this design; verify its dimensions.`
- Attach labeled faces, then ask for a measurement and an operation grounded in it.
- Ask Astra to inspect an assembly, identify missing interface information, and
  build only after the necessary relationships are established.

This is not a promise that a model will successfully design an arbitrary assembly.
Live planner quality and Jev calibration on CAD tasks still require evaluation.

## Optional native-tool development

Select **Astra + Jev · tool building (source edits)** (`hybrid_dev`, model
`hybrid-dev:gpt-6-astra`) to authorize source development. Standard `hybrid` still
excludes source tools. The developer preset keeps the same planning-only Codex
isolation; Astra proposes source tool calls in structured plans and the host
executes the existing guarded workspace tools.

For a missing capability the intended sequence is:

1. Check tool parameters and exact supported compositions. Do not approximate an
   exact circle with a polygon without the user's agreement.
2. Read AGENTS.md and focused neighboring geometry/registration/test source.
3. Write new recipes/tests; hash-patch existing allowed geometry files. Integrate
   imports/reload wiring and register the handler with an explicit JSON schema.
4. Optionally run tests. Writes default to `run_tests=false`; explicit testing is
   still available, and test failures are reported rather than blocking CAD.
5. Invoke the registered tool directly and optionally check actual CAD.

At the user's request, developer mode has **no mandatory test or completion-check
gate**. Source writes and CAD mutations can share one section. A tool registered
by an earlier write in that section can be invoked later in the same section;
its real runtime schema must exist and validate before dispatch. Source changes
without a passing full test run are reported honestly, but do not block CAD.
Missing or stale completion checks are not reported as passing. Existing native
argument/document validation and unknown-operation protection remain in force.
New registered handlers are exposed without adding another hardcoded workflow.

Source writes are restricted to geometry modules (`fusion_cad/tools.py`, spatial modules,
geometry/math/primitives helpers, `fusion_cad/tool_schemas.json`), `recipes/*.py`, and
`tests/test_*.py`. Existing files require SHA-guarded `patch_file`; `write_file`
is new files only. The Studio client, bridge runtime, workspace security code,
credential/hidden files, and paths outside this repository are not editable
through these planned source calls. Native tools also perform syntax checks and
keep backups. `patch_file` is now correctly registered as a mutating operation.

**This is trusted code-development permission, not a sandbox.** Tests and generated
Python execute on your machine/in Fusion. Source can still be incorrect or have
side effects; write restrictions are accident prevention, not a hostile-code
containment system. Tests do not establish live kernel behavior. Import errors
can break hot reload even after source passes syntax checks; restoring the saved
source backup may require local development outside the bridge. No automatic
full rollback of source or CAD is claimed. No Studio restart or Fusion restart is
automatically performed by a generated plan.

Developer budgets are 8 planner calls and 80 tool dispatches per user turn (4 Jev
choices, unchanged). Source changes/results/activation requirements are in the
normal tool trace. A blocked task now reports its actual explanation, rather
than displaying “Section executed” after doing no CAD work.

## Responsibilities

| Owner | Responsibility |
| --- | --- |
| Astra | Synthesize geometry/strategy, define dependencies and acceptance checks, propose a revised section when observations invalidate the plan |
| Code | Load schemas, resolve references, compute arithmetic, bind documents, execute sequentially, enforce freshness/budgets, check assertions |
| Jev | Select among 2–64 supplied candidate values at an explicit noncritical semantic choice |
| Fusion | Establish geometry, measurements, interference, feature health, and actual mutation/recovery results |
| User | Resolve consequential ambiguous mating/support/mounting interfaces |

There is no Jev call on the deterministic path. Measurement → arithmetic → native
operation → verification can run with one Astra planning call and zero Jev calls.
A read-only survey can end at a deliberate replan boundary when further synthesis
requires its results. This is different from calling Astra after every operation.

## Planning is not the existing full-auto Codex agent

`studio/providers/astra_planner.py` launches a fresh, planning-only CLI invocation
in an isolated temporary directory. It uses the existing Codex login, but ignores
user/project execution configuration/rules, supplies an empty MCP configuration,
uses a read-only sandbox with no approval escalation, and disables shell/unified
execution, apps/plugins/hooks, browser/computer control, multi-agent execution,
code-mode, and image tools. No Fusion MCP server is intentionally exposed.

The CLI receives a structured output schema, the native capability catalogue, and
bounded application state. It returns JSON, not tool calls for Codex to execute.
Observed tool-use events cause rejection of the whole returned plan. That event
check is defense-in-depth, not a substitute for CLI sandbox/configuration controls.
No full-auto bypass or resumed unrestricted Codex session is used in this mode.

The Jev API key is removed from the planner subprocess environment. The CLI still
uses its own normal authentication environment/storage; the adapter does not read,
copy, or forward credential files. Temporary state/plan files are deleted on exit.
The readiness check verifies CLI flags, not authentication or model availability.

## Executable plan language

`studio/hybrid_plan.py` defines a small declarative language; no Python `eval`,
exec, or arbitrary script runner is involved.

Each section has:

- A goal and explicit assumptions.
- `intent_json`: updates for the existing persistent design-state tool. The
  executor also records the section goal as `next_action`. Design intent and
  assumptions are claims; native evidence handles still must come from tools.
- Up to 12 ordered steps, with stable unique IDs, purposes, and typed kinds.
- Explicit completion-check IDs.
- A terminal instruction: `finish`, `replan`, `ask`, or `blocked`. Blocked/ask
  responses must explain the blocker/question. Empty finish responses display
  their explanation and explicitly state that no CAD changes were made.

Step kinds:

| Kind | Behavior |
| --- | --- |
| `tool` | Invoke a published allowlisted native tool with schema-validated arguments |
| `compute` | Resolve data or evaluate bounded arithmetic in code |
| `assert` | Compare values using eq/ne/lt/lte/gt/gte; must depend on native evidence |
| `choose` | Ask Jev about supplied candidates, or ask the user if `critical=true` |

JSON payload expressions can be nested in arrays and objects:

```json
{"$ref": "gap.distance_cm"}
```

```json
{"$calc": "subtract", "args": [{"$ref": "gap.distance_cm"}, 0.2]}
```

```json
{"$length": {"value": {"$ref": "thickness"}, "unit": "cm"}}
```

Arithmetic supports add, subtract, multiply, divide, min, max, and abs. Division
by zero, non-finite/oversized numbers, unknown operators, forward references,
excess nesting, and stale records are rejected. Units are explicit mm/cm/m/in;
the planner must use consistent units in its expressions. A typed value is not
proof that its chosen engineering formula is correct.

A choice's `candidates` can reference a native list such as
`{"$ref":"survey.entities"}`. Jev returns a candidate ID or abstains; the host
selects the original local row, preserving its exact selector. Token fields are
removed from remote candidate descriptions. No arbitrary generated selector or
argument text is accepted from Jev. Confidence >=0.85 and chosen probability
>=0.90 are experimental thresholds, not measured CAD reliability guarantees.
Astra marks critical choices; the model can still misclassify a consequential
choice as noncritical. This is not a formal authorization system.

## Execution, freshness, and recovery

`studio/providers/hybrid_provider.py` plugs into the ordinary Studio executor,
not a second Fusion tool loop. It initializes from `design_summary`, exact face
selection/revalidation, and persistent design intent. It receives the current
registry and document binding through `prepare_runtime`.

- Every native tool uses the existing argument validation, confirmation,
  operation IDs, document binding, evidence tracking, journal, and recovery path.
- The host supplies mutation document IDs. Planner-supplied document/operation
  IDs are rejected. A document switch outside the plan stops execution instead
  of automatically retargeting into another document. Explicit document creation
  updates the binding for subsequent steps.
- Every attempted mutation invalidates earlier geometry-derived records, even
  if it fails. A later argument referencing stale geometry forces fresh
  inspection/measurement or replanning. Fixed literal computations can remain
  valid. Previous-turn results are context only, not reusable geometry evidence.
- Known native failures, failed assertions, and failed verification discard the
  remaining section. Astra gets actual results/recovery; restoration is never
  inferred from a failed call. Schema/type errors also stop the affected section.
- Identical mutations are not automatically repeated within the same user turn,
  even if a replanner proposes them. Corrections must have distinct arguments and
  be grounded in known state.
- An unknown/timed-out operation gets one status check. While unresolved, further
  user messages only check that operation. No replanning, further mutations, or
  replay occurs. A terminal status is reported for review; a new task is required.
- In standard hybrid mode, completion after mutations requires positive, fresh
  native verification or assertions referencing native evidence. Developer mode
  makes completion checks optional and reports unchecked work explicitly. A check before a later mutation is
  stale and cannot certify completion. Passing these checks does not prove the
  overall engineering design is correct.

Initial budgets: 3 Astra sections, 40 native/local tool dispatches, 4 Jev choices
per user turn. The planner has a 240-second deadline; cancellation kills its
process group. Jev uses its existing 15-second request timeout; an in-flight Jev
call may return before stop is observed. Fusion kernel calls remain noninterruptible.

Unknown operation tracking is also enforced by the shared bridge. Plan execution
is not resumed automatically after a process restart. Inspect operation history
before repeating work interrupted by a crash; this mode does not add a new durable
workflow-resumption protocol on top of the existing operation journal.

## UI, images, and telemetry

Studio displays the Astra section, goals, assumptions, steps and checks. Progress
identifies planning, code execution, or Jev decisions. Transcript events include
`hybrid_plan`, `hybrid_phase`, `hybrid_step`, `hybrid_replan`, `hybrid_verification`,
and the existing Jev decision/native tool result events.

`astra_planner` timings measure planner invocations. `hybrid_controller` timings
cover provider transitions and can include model latency; do not sum them with
planner/decision time as independent durations. Native `tool_end.seconds` is
client-observed duration, not isolated Fusion kernel time.

No automatic screenshots or vision fallback are invoked. Image attachments are
rejected. A plan may explicitly request `capture_viewport` for the user; Studio
renders it without making another model interpret it. Final text explicitly says
no automated visual review occurred.

## Scope and validation

Standard hybrid mode composes published native CAD tools, not just the Jev-only
workflow menu. It excludes source writes, unrestricted file reading, saving and
shell execution. The separate `hybrid_dev` preset adds the explicitly authorized
native-tool development path described above. Native tool limitations and geometry complexity guards
still apply.

State and schemas go to Astra through Codex. The request/section goal and candidate
descriptions go to TypeSafe when a choice is needed. These may contain proprietary
design metadata; enable only where both services are permitted.

Offline tests cover real Studio orchestration with mocked model/native responses,
including zero-model execution between established steps, data references,
arithmetic, Jev choices, user escalation, stale evidence, document changes,
partial failures, unknown operation polling, duplicate mutation suppression,
completion checks, budgets, and planner process isolation/cleanup/cancellation.
They do not demonstrate actual Astra plan quality, Jev CAD calibration, or live
Fusion modeling success.

```sh
studio/.venv/bin/python -m unittest discover -s studio/tests -p test_hybrid.py -v
studio/.venv/bin/python -m unittest discover -s studio/tests -v
PYTHONPATH=. python3 -m unittest discover -s tests -v
```
