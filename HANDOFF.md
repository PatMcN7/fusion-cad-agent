# Handoff

Original handoff written 2026-09-05. The historical sections below describe the
starting point; this update and `PLAN.md` supersede their status claims.

## Current implementation update

- Baseline and protocol-v4 changes are committed (`021096f`, `2441dd9`,
  `9724291`). There are now bounded complexity checks and durable operation IDs.
- The next interface iteration fixes explicit argument types, introduces native
  feature references and managed lattice edits, replaces asynchronous global
  undo with synchronous scoped deletion, and reduces Studio context and captures.
- Studio uses `studio/agent_runtime.py` as its compact prompt; MCP uses
  `catalog.brief()` and this repository's `AGENTS.md`. Registered tools publish
  explicit schemas; prose inference is only a legacy client fallback.
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
