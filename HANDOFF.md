# Handoff

Original handoff written 2026-09-05. The historical sections below describe the
starting point; this update and `PLAN.md` supersede their status claims.

## Current implementation update

- Fusion Studio now has a native docked palette (`fusion_ui.py`) and a panel-first
  UI: full-width chat, a fixed composer, separate history/settings views, and a
  dedicated capture/scratchpad editor. The UI uses local assets with no CDN.
  Drawing drafts and message attachments persist in IndexedDB per chat.
- The redesigned page is served from `studio/static/index.html`. Browser checks
  in `studio/tests/ui_smoke.cjs` cover 320–1000 px widths, capture, drawing,
  resizing, draft reload, history, and flattened image submission with mocked
  APIs. Run with `FUSION_PLAYWRIGHT_MODULE` pointing to an installed Playwright
  package; optional `FUSION_UI_BROWSER` selects an existing Chromium executable.
  Native palette visibility and URL were checked live; screenshots were reviewed
  in Chromium, so Fusion's embedded browser still needs user visual feedback.
- Run activity is now separate from collapsed transcript details: current phase,
  elapsed time, tool count, last activity, repeated-call and quiet-period warnings.
  Reopened busy chats poll and render progress before completion. Browser checks
  cover that path and stop acknowledgement. Older runs without a stored start
  time show "Watching" time rather than inventing total elapsed time.
  The 2026-09-05 sidebar drivebase run `0e8505aa7f59` was stopped after repeated
  successful document/file listings without geometry construction. The UI flags
  repetition. The runtime now also warns after two unchanged successful reads
  and stops after three; intervening mutations reset those observations and
  operation-status polling is exempt.
- Submitted prompts and flattened images now have an independent IndexedDB copy
  (`sent:<chat>:<timestamp>`) before the composer draft is cleared. This applies
  to future submissions. The older run's three sent sketches were not retained:
  its external IndexedDB blob files were already gone; browser records recovered
  only partial drawing drafts. The exact 2095-character prompt was placed in a
  fresh draft `1e7a77784785`, without sending, awaiting reattachment of the images.
- The file-search loop's concrete cause was a bad enum: `list_files.pattern`
  and `run_tests.pattern` incorrectly accepted only `isogrid`/`hex`. Both now
  accept filename globs; lattice-pattern enums remain restricted. The fixed
  schema was loaded live, and GLM found the exact WCP-1702 STEP file and ended
  its read-only test. See `docs/file-search-loop-fix.md` for validation.
- Studio was restarted to activate the repetition guard and reasoning-history
  preservation. Earlier chat logs were exported to the agent transcript folder
  `runtime-restart-20260905/`. Fresh draft `4e971bbf6cb0` contains the exact latest
  2095-character request and three retained images; hashes match the submitted
  images. The full drivebase build has not been rerun after this fix.

- Baseline and protocol-v4 changes are committed (`021096f`, `2441dd9`,
  `9724291`). There are now bounded complexity checks and durable operation IDs.
- The next interface iteration fixes explicit argument types, introduces native
  feature references and managed lattice edits, replaces asynchronous global
  undo with synchronous scoped deletion, and reduces Studio context and captures.
- Studio uses `studio/agent_runtime.py` as its compact prompt; MCP uses
  `catalog.brief()` and this repository's `AGENTS.md`. Registered tools publish
  explicit schemas; prose inference is only a legacy client fallback.
- Studio now starts with 11 general document/inspection/discovery/control tools,
  including `create_document` for an empty unsaved design. Modeling
  and specialized workflows load by group; see `docs/initial-tool-surface.md`.
- Read `docs/agent-interface-validation.md` for replay evidence, current test
  counts, and limitations. `docs/runtime.md` describes the execution contract.
- Restart Studio with `./studio/run.sh` to load client changes. Native geometry
  hot reload is separate. API configuration remains in ignored `studio/.env`.
- The original saved assembly was not changed by the disposable validation runs.
  General saved-assembly revision, reopen identity, and recipe execution remain
  future work; do not interpret the plate replay as completing those milestones.

## The thesis

> "The codex/claude code for CAD."

Two problems this project exists to solve, in the owner's words:

**(a) The kernel knows, the model doesn't.** A premade motor was mated to a
gearbox; the mate looked right but the primary axis was flipped. *"This sort of
error is immediately obvious to a human but the AI had no clue that it had
happened."* The failure mode that matters is not one bad operation — it is ten
minutes of agentic CAD stacked on top of a silent one.

**(b) The feedback loop is loose.** *"The feedback loop for code is very tight...
Things in CAD can fail in weird ways, and the context window you need to design
stuff effectively is just like sooo much wider."*

Everything in the design follows from those two. The geometry kernel already
computes the answer — two bodies where one was expected, a cut that removed
nothing, 4.2 cm³ of interference — and nobody was reading it. This repo reads it
after every operation and hands it back.

## Layout

```
FusionCadAgent.py      Fusion add-in shell: HTTP server, custom event, auth token
FusionCadAgent.manifest
errors.py              exception hierarchy — NEVER hot-reloaded (see below)
common.py              wire protocol, agent_home(); no Fusion imports
bridge_client.py       client side of the bridge protocol
fusion_cli.py          command-line client

  hot-reloaded on every request, keyed on file mtime:
geom.py                pure: 2D computational geometry, polygon fillets
fasteners.py           pure: what a thread designation means
patterns.py            pure: lattice + hole-position math
assertions.py          check framework and the checks themselves
pick.py                geometry selection — a query language over Fusion entities
ops.py                 modelling primitives
checkpoints.py         timeline checkpoint / rollback
tools.py               the 35 agent tools, composed from pick + ops
workspace.py           local files + network, fenced; repo writes; test runner
catalog.py             AST source map, embedded in the system prompt

mcp_server.py          MCP front end (used by Codex)
AGENTS.md              the shared brief; MCP + studio both load it
studio/                the chat/sketch UI and the agent loop
tests/                 176 tests, no Fusion required
```

**`errors.py` is excluded from hot reload on purpose.** Reloading a module
rebinds its classes, so an exception raised by a freshly reloaded module is a
*different class object* than the one the shell imported, and `isinstance`
silently stops matching. A never-reloaded module keeps identity stable.

Hot reload is why editing a pure module does not need a Fusion restart. This was
a direct response to *"restarting the client thing is intensely annoying."*
`workspace` was added to the reload set after a fix to it could not reach a
running Fusion.

## The three layers of verification

1. **Pre-flight** — pure Python, before the kernel is touched. Calls the *same*
   `patterns.py` functions the build uses, so the simulation cannot drift from
   the build.
2. **Per-operation** — every mutating operation is checkpointed against the
   timeline and verified on completion. A failure rewinds *that operation and
   nothing else*. This is the layer that earns its keep: a pre-flight only tests
   what someone thought to test.
3. **Spec** — `verify_spec` runs a design spec as a test suite, delegating to the
   same `assertions` the build path uses.

Assertion design rule, learned the hard way: an assertion whose expected value is
`True` is a receipt, not a check. If it cannot fail on well-formed-but-wrong
output it is testing nothing — that is how `M3.5x0.6` shipped into a 0.125 in
hole with 17 checks green.

Manufacturability checks (`corners_machinable`) report as **warnings**. They never
block a printed part and never pass silently. The analyzer counts them separately
from rollbacks; conflating them inflates the failure rate.

## The 35 tools

Registered in `tools.py`; schemas are generated from that registry, never hand
written, so a tool added there appears in the studio on the next page load.

| group | tools |
|---|---|
| orient | `health`, `active_document`, `design_summary`, `list_documents`, `switch_document` |
| verify | `check_interference`, `verify_spec`, `mechanics_report`, `inspect_threads`, `inspect_commands` |
| see | `capture_viewport` |
| build | `create_test_box`, `create_tube_frame`, `add_bellypan`, `add_fasteners`, `replace_with_isogrid_bar`, `extrude_polygons`, `cut_polygons`, `lattice_pockets`, `router_fillet_pocket_corners`, `set_6061_material`, `delete_named_features`, `save_document`, `undo_last` |
| mate | `list_joint_candidates`, `create_joint`, `bodies_to_components` |
| import | `import_geometry`, `import_step_component` |
| read the world | `list_files`, `read_file`, `fetch_url`, `pdf_to_images` |
| edit itself | `write_file`, `run_tests` |

`cut_polygons` / `extrude_polygons` are the escape hatch from the parametric
tools: compute any 2D field in Python and cut it.

## Why a selection layer (`pick.py`)

Fusion's commands take entities, not descriptions. In the GUI your cursor
resolves "that face" for free; through the API it is a search. On the bespoke
first generation that search was **7 of every 10 lines** — the command itself
was 3.

```python
face  = pick.faces(body).planar().normal_axis("y").extreme("+y").one()
bolts = pick.faces(tube).cylindrical().axis_along("z").extreme("-z").centers()
```

`.one()` and `.expect(n)` raise if a query does not resolve to what the caller
claimed, so an ambiguous selection fails loudly instead of grabbing `item(0)`.

## What was built in the last two sessions

The owner's complaint that drove all of it: *"what are you going to do to fix the
token usage/overall time from prompt to usable cad"* and later, after watching a
four-operation task take half an hour, *"the model seems to have a tendency to
just be so scared, having to confirm everything will work before it makes a
change. I wish it would just kinda send it and then like if it fucks up fix it."*

### Token and latency work

- **`catalog.py`** — AST-walks the pure modules and emits a signature index
  embedded in the system prompt. Kills ~25 grep round trips per session.
  `catalog.brief()` is `AGENTS.md` + the generated source map, and is the single
  source both `mcp_server.py` and `studio/server.py` load.
- **State echo** — `_echo_state` wraps every mutating handler and appends a
  `design` digest to the result. The state after an operation arrives *with* the
  operation. In one logged session this killed ten `design_summary` calls.
- **Compressed check reports** — `assertions.report()` returns failures in full
  and passes as a list of names. 1,735 bytes → 387.
- **Face sampling** — `FACE_SAMPLE = 400` in `list_joint_candidates`, which used
  to touch every face of every imported body. **79 s → 5.7 s.**
- **The vision role** (`studio/vision.py`) — see below.
- **`STUDIO_EFFORT` default high → low.** One session was 96% reasoning tokens
  (49k thinking against 1.9k visible) at 41 s per call.
- **AGENTS.md "Just build it"** — the brief was rewritten to tell the model that
  every operation is checkpointed, a wrong guess costs one call, and nothing
  compounds. Explicitly: do not inspect to reassure yourself, do not confirm a
  plan before executing it, retry immediately when something fails.

### The vision role

After every mutating tool the harness captures the viewport. That picture is the
only signal that says whether the part *looks* like what was asked for — and it
is also a permanent tenant of the transcript, re-sent on every following turn.
One logged session ran **1.93M input tokens against 7K of output**, largely
because fourteen captures accumulated.

So by default the capture goes to a vision model **on its own, with no history
attached**, and what enters the conversation is a few lines of text. The call is
stateless and therefore cheap even on a frontier model — which frees the loop
model to be something small and fast.

Images *you* attach and mark up are never summarized. You are pointing at
something, and a paraphrase would discard the arrow.

### Mates

`create_joint` creates the joint, measures interference, and if the parts now
overlap, **flips it and keeps whichever direction the geometry says is right**.
Rolls back if neither works. This is problem (a) from the thesis, closed:

```python
if auto_flip and after_interference > before_interference + 1e-6:
    joint.isFlipped = not joint.isFlipped
    ...
    verdict = assertions.resolve_flip(before, after, flipped)
```

`bodies_to_components` exists because of an architectural mismatch that killed a
drivebase task: **`create_tube_frame` produces root bodies, and Fusion joints act
on occurrences.** The builders and the mating layer could not talk to each other.

### Files and the network (`workspace.py`)

Three rules, documented in the module docstring:

- **Containment.** Every path is resolved through symlinks *before* the
  containment check. Checking the string first and resolving later is the classic
  way this goes wrong.
- **A denylist inside the fence.** Being in an allowed root is not enough.
  `studio/.env` sits in this repo and holds live keys; secrets are refused by
  name wherever found (`.env`, `.netrc`, `auth.json`, `id_rsa`, …).
- **No inward requests.** Loopback, private ranges and link-local
  (169.254.169.254, cloud credentials) are refused. Fetching is for vendor
  drawings, not for reaching back into this machine.

Readable roots, chosen by the owner: **repo + Downloads + Documents**, read-only.
Writes are fenced to the repo alone, and `FusionCadAgent.py`, `errors.py` and the
manifest are never writable.

`_clean_env()` matters more than it looks: Fusion exports `PYTHONHOME` pointing at
its own embedded interpreter, and a subprocess that inherits it dies before
running a line.

### Self-editing

`write_file` + `run_tests` let the agent add a tool that does not exist yet.
AGENTS.md rung 3 says this is *"a normal thing to do, not a last resort."*
Proved end to end: the agent wrote a new module, appended a new tool to
`tools.py`, and **called the tool it had just written** on the next request —
33 tools → 36 → probe removed → 35.

`run_tests` uses `find_binary("python3")`, **not `sys.executable`**: inside Fusion
that is Fusion's embedded interpreter, and spawning it hangs and locks the main
thread the bridge needs. 176 tests in 0.4 s through Fusion.

### The studio (`studio/`)

- **Providers** (`providers/`) — a neutral protocol (`base.py`) that never
  branches on vendor. Adapters: `anthropic`, `openai` (and any OpenAI-compatible
  server), `gemini`, `openrouter`, `codex`. `openrouter_provider.py` subclasses
  the OpenAI one through three seams (`_create`, `_explain`, `_usage`) and does a
  capability pre-flight against the live catalogue, so you are told the model
  cannot see images *before* a session rather than twenty turns in. It reports
  **real spend per turn** from OpenRouter, not estimated from a price table.
- **`Cancelled(BaseException)`** — the stop button. Deriving from `BaseException`
  means `except Exception` in every provider cannot swallow it, so zero adapter
  edits were needed. Measured at 0.02 s mid-stream.
- **Multi-session** (`Session`, `SessionStore`) — chats are separate for the same
  reason branches are: a CAD session builds state in the model, and interleaving
  two lines of work makes both worse. Each chat carries its own model, which is
  what makes a real A/B possible.
- **`transcript.py` / `analyze.py`** — JSONL recording hung off the agent loop's
  existing `emit` seam, so it cannot drift from what the UI shows. Image bytes
  are never written; a capture is a path plus what the vision model said.
  `analyze.py --compare` groups by worker model. Two distinctions it is careful
  about: a warning is not a rollback, and `session_cost_usd` is cumulative so the
  total is the last value, not the sum.
- **`envfile.py`** — named that way so it cannot shadow the installed `dotenv`
  package. Same trap as `select.py` → `pick.py`; there is a test that checks
  module names against the stdlib.
- One `renderEvent` serves both the live stream and replay, so history cannot
  show something subtly different from what you watched happen.

## Model choice

Checked 2026-08-26 — Artificial Analysis Intelligence Index v4.1.1 against
OpenRouter's live price and weekly-usage rank:

| model | index | $/Mtok in/out | OR rank | vision |
|---|---|---|---|---|
| `z-ai/glm-5.3-flash` | **57** | 0.07 / 0.25 | #2 | yes |
| `google/gemini-3.7-flash` | 56 | 0.38 / 1.88 | #12 | yes |
| `qwen/qwen3.8-flash` | — | 0.16 / 0.47 | **#1** | yes |
| `openai/gpt-5.6-luna` | 52 | 0.20 / 1.20 | — | yes |
| `~deepseek/deepseek-v4-flash-latest` | 50–52 | 0.03 / 0.10 | #27 | no |

Currently configured: **GLM-5.3 Flash for both roles**, effort `low`.

Caveats worth carrying forward, because they were mistakes made here:
several of these models postdate the assistant's training cutoff, so the index
column is the only real signal — recommendations made from familiarity were wrong
twice (`gemini-3.1-pro-preview`, index 48, is worse than `gemini-3.7-flash` at 56
and 5× the price). Where a benchmark did not exist, OpenRouter's
`?order=top-weekly` adoption rank was used as the substitute.

## Current state

- **176 tests pass** (`/usr/bin/python3 -m unittest discover -s tests`) plus
  **90 studio tests** (`./studio/.venv/bin/python -m unittest discover -s studio/tests -t .`).
  Note the studio tests need the venv interpreter; the SDKs are installed there.
- **The studio server is not running.** Start it with `./studio/run.sh`, or
  detached: `nohup ./studio/run.sh > /tmp/studio.log 2>&1 &`. It binds
  `127.0.0.1:8765` and needs the Fusion add-in running.
- Two `FusionCadAgent` entries may appear in Fusion's add-in list — one is stale.

## Open items

1. **Git has zero commits.** 56 files staged, nothing committed. The agent can now
   write to its own source, and `source-backups/` is the only undo. This has been
   raised repeatedly and never approved; it is the single highest-value pending
   action.
2. **Rotate both API keys.** `OPENAI_API_KEY` and `OPENROUTER_API_KEY` were
   surfaced into an assistant transcript. `studio/.env` is `0600` and gitignored,
   and is verified excluded from staging, but the keys themselves are exposed.
3. **No cell-count guard on lattice operations.** `add_bellypan` with
   `cell_size: "1.5 in"` on a 30×30 pan is 825 triangles / 2,475 sketch lines and
   ground Fusion to a halt for ~40 minutes. Not a deadlock — it was working. A
   pre-flight cell count that refuses or warns above a threshold is the fix, and
   it is not written.
4. **Re-run the drivebase task** and compare with `studio/analyze.py --compare`.
   The blockers it hit are all removed (`bodies_to_components`, effort `low`,
   compressed checks, the rewritten brief, runtime tool authoring) but the
   improvement is unmeasured.
5. **`import_geometry` ran 4× at 35 s each** in one session. Worth a cache or at
   least an idempotence check.
6. **Captures are fit-to-whole**, so a 0.5 in screw on a 30 in assembly is
   invisible. Fine visual detail cannot be reviewed. Zoom-to-entity is the fix.
7. **Selectors are positional.** `.extreme("+z")` means "whichever face is highest
   right now"; regenerate and that may be a different entity. This is why the
   tools rebuild rather than edit. Stable entity tokens are the next real problem.

## Standing constraints

- Owner's instruction: **`STUDIO_CODEX_APPROVAL=full-auto`** — *"just let it do
  whatever it wants forever please no authorization."*
- Write access is fenced to the repo; `FusionCadAgent.py` and `errors.py` never.
- `fetch_url` refuses loopback, private, link-local and non-http(s) schemes.
- The studio binds `127.0.0.1` only and has no auth of its own. Do not expose the
  port.
- Codex's OAuth token in `~/.codex/auth.json` was never read or forwarded; the
  Codex adapter shells out to the CLI and lets it authenticate itself.

## Working notes for whoever picks this up

- **Single source of truth, applied repeatedly.** `catalog.brief()` shared by MCP
  and studio; `envfile.py` shared by server and bench; one `renderEvent` for live
  and replay; pre-flight calling the same `patterns.py` the build calls. When two
  copies of a fact exist here, that is a bug not yet found.
- **Verify against a running Fusion, not against the tests.** Several defects the
  tests could never have caught — `design_summary` crashing on a vendor STEP body
  (`ASMInterface::getEntityVolume`), `pdftoppm` "not installed" when it is
  installed but Fusion launches from Finder without Homebrew on `PATH`,
  `sys.executable` hanging — were only visible through the real add-in.
- **The owner reads the output critically and is usually right.** Two model
  recommendations and one session classification made here were wrong and were
  corrected after being challenged. Check claims against data before stating them.

## September 6: design dependencies, evidence and general spatial tools

Implemented the user's requested system-thinking changes. Current details and
limits: `docs/design-dependency-interface.md`.

- Compact general dependency guidance in Studio prompt and MCP AGENTS brief;
  targeted intermediate captures allowed. No FRC workflow is hardcoded.
- Shared `design_state.py` and client-local `update_design_state`; evidence refs,
  dependency validation, stale/blocked statuses, pinned Studio prompt, bounded
  disk snapshots. Known chat URLs restore their design records with fresh model
  context after restart; provider-native history is not restored. MCP state is
  per process, and existing MCP processes need reconnecting.
- New `spatial.py` / `spatial_contracts.py`: inspect_geometry, measure_geometry,
  verify_relationship, place_component, extrude_between_faces. Native schemas
  are explicit. Placement is top-level only, copies share geometry; existing
  grounding requires explicit release_grounding. Referenced extrusion accepts
  parallel planar face sections. No general-purpose tube construction added.
- Sidebar design card shows requirements/unknowns/assumptions and current action;
  correction button focuses a draft correction. Existing drawing flow retained.
- Fixed construction-plane feature inspection (`component`, not parentComponent).
- Validation: 262 core + 113 Studio tests; browser smoke passes; native acceptance
  script passes including rotated/transformed face measurements. Original active
  document restored. Separate unsaved validation documents remain open.
- Six fresh GLM simulated trials (three simple fixtures, two prompt variants)
  succeeded in both variants; no proven efficiency/correctness improvement.
  New variant used more output tokens (1165 vs 876). Do not claim full drivebase
  competence based on these trials. Artifacts: benchmarks/runs/2026-09-06-design-dependencies/.
- Studio restarted with these changes. Idle public chat histories archived under
  agent_home()/transcripts/design-update-20260906/. Fresh sidebar draft:
  79efbe484067, restored from saved submission 1e7a77784785 with 2095 text characters
  and three images. Native browser blob verified matching hashes 5b0b553467,
  9dceba6432, a1399b52e5. No automatic send or full drivebase attempt.
- Native panel refresh hook was temporary and removed from tools.py after use.
  Most recent Studio exec session: 39689. No commit made.

## September 6: exact user-selected face attachments

- Select faces with Fusion's normal selection, click **Faces** in Studio, add
  optional labels, then send. Highlight buttons reselect the exact faces. Multiple
  captures accumulate up to 12 faces; selecting a body never guesses its faces.
- `face_selection.py` retains document-bound handles with native face tokens and
  exact occurrence identity. Handles survive tool-module hot reload, but add-in
  restart, deleted geometry or ambiguous token resolution requires reselection.
- `get_selected_faces` is the 13th initial Studio tool. The `selection` group also
  exposes `resolve_selected_faces` and `highlight_selected_faces`; MCP exposes all
  three. Exact `face_ref` selectors work in the general spatial tools and joint
  face arguments. This does not add arbitrary sketch/cut-on-face modeling.
- Studio resolves every attachment before invoking the model, rejects stale or
  wrong-document references, and uses fresh native metadata. Labels carry user
  intent, not verified geometry. Drafts and submitted history retain face labels
  and references; rejected submissions restore the draft.
- Validation: 269 core + 118 Studio tests pass; browser smoke covers capture,
  labels, deduplication, recovery, rejection, highlighting and 12 faces in narrow
  panels. Live Fusion acceptance selected existing faces, resolved/measured exact
  instances, highlighted them, ignored whole-body selection, and restored the
  user's original selection without changing geometry. Helper:
  `scripts/validate_face_selection.py`; result `/tmp/fusion-picked-faces-native.json`.
- Studio restarted only after the prior run completed; completed public histories
  archived under agent_home()/transcripts/face-selection-update-20260906/.
  Current server exec session: 32150. Sidebar remains on chat 79efbe484067 with
  fresh model context after restart; no prompt automatically sent. Native health
  confirms the panel is visible without UI installation errors. Browser layout
  was checked in the harness, not through embedded Fusion pixel inspection.
- Live HTTP stale-attachment test rejected an expired handle before recording a
  message or calling the model. Temporary validation/refresh hooks removed from
  tools.py. No commit made.

- User requested a fresh rerun after face selection. Started sidebar chat
  `dd515b5a65d8` with GLM 5.3 Flash and the exact latest 2332-character prompt
  from session-20260906-185750-fe28b70d.jsonl, including the modules-first
  instruction, plus all three sketches restored from native IndexedDB submission
  history for 79efbe484067. No face attachments existed in that submission.
  Server confirms a fresh model context, three image blocks, 13 initial tools
  and an active run. Do not confuse launch verification with a finished CAD test.
  Temporary one-shot replay/navigation hooks were removed immediately afterward.
  Input provenance: /tmp/fusion-face-rerun-source.json.

## September 6: repair after the stalled module inspection

- Removed Studio's 2048/4096 response budgets and 80-step ceiling. Successful
  repeated reads and consecutive failures warn without terminating the model.
  Repeated identical failed calls still return corrective errors without running
  the backend again; the model remains able to change its approach. User Stop,
  genuine provider refusal and transport errors remain distinct.
- OpenAI/OpenRouter and Gemini requests omit output-token caps. Anthropic's
  required max_tokens uses its model metadata maximum. Provider truncation
  automatically continues with history; calls from an incomplete response are
  explicitly marked unexecuted before continuation. No blind mutation replay.
- inspect_geometry pages through exact child occurrences and direct bodies
  together. Empty wrapper components expose descendants and a next-step hint;
  body rows include face counts. Existing body selectors return exact faces.
- Images go directly to vision-capable workers, including every requested view
  (previously one image field could overwrite earlier views). Named image blocks
  round-trip through OpenAI, Anthropic and Gemini. Descriptions are fallback for
  text-only workers. OpenRouter uses catalogue image capability when available.
- Studio-local request_face_selection is always discoverable (14 initial tools).
  It asks a specific question/reason/surface list, emits a visible attachment
  card, and yields for user input. Later calls in the same batch are returned as
  unexecuted. User replies use existing exact face attachment validation.
- Pinned instructions require actual face measurements; an unresolved critical
  interface should trigger a concrete user-selection question, not speculation.
- Streaming log records copy emitted data and coalesce reasoning fragments,
  preserving readable history instead of evicting user messages per token.
- Tests: 271 core and 126 Studio tests pass;
  browser smoke passes including face-request rendering and attachment action.
  Native validation followed the imported wrapper into its 53 child components,
  then a body with 267 faces, retrieving exact occurrence-bound face selectors.
  No geometry changed. Evidence: /tmp/fusion-harness-navigation-validation.json.

- Activated the fixes in Studio server exec session 35089 after verifying every
  prior chat was idle and archiving public logs under
  agent_home()/transcripts/harness-repair-20260906/. Native panel refreshed to
  ba0a001790eb with fresh model context and restored design-state record.
  Live bootstrap confirms connected bridge, ready GLM worker, image capability,
  and effective vision mode direct. Temporary refresh hook removed. No full
  drivebase rerun or automatic prompt submission during this repair.

- Added `codex:gpt-6-astra` / **Codex · Astra (subscription)** to the picker.
  Uses the existing Codex CLI backend with `--model gpt-6-astra` for both new
  and resumed sessions. No API adapter or credential handling added. Verified
  command construction, 126 Studio tests and live bootstrap preset visibility.
  Sidebar refreshed; server exec session 60015. No model run or default switch.

- At user request, preloaded Codex Astra into fresh sidebar chat 7af31e2d9d63.
  Backend readiness confirms gpt-6-astra via Codex subscription/MCP. Restored
  latest full drivebase prompt and three sketches using retry_session=dd515b5a65d8.
  Draft only: no send, no model execution. Temporary handoff hook removed.
