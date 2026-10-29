# Fusion Studio

A chat + sketch interface to the Fusion CAD bridge. Describe a part in words or
draw on a screenshot of the model; the agent asks what it needs, builds through
the verified bridge, and shows you what it made.

```sh
./studio/run.sh                 # Claude only (default)
./studio/run.sh --with-all      # also install the OpenAI + Gemini SDKs
```

Then open <http://127.0.0.1:8765>. The Fusion add-in must be running.

## Hybrid: Astra plans, code executes, Jev decides (experimental)

Select **Astra planner + Jev decisions (experimental)** after restarting Studio.
Requires a recent authenticated Codex CLI; add `TYPESAFE_API_KEY` to `studio/.env`
for bounded Jev choices. Or launch with:

```sh
STUDIO_PROVIDER=hybrid STUDIO_MODEL=gpt-6-astra ./studio/run.sh
```

Unlike direct Codex mode, Astra only returns a structured plan. Studio executes
established native steps without another model call. Jev is invoked only for an
explicit bounded semantic choice; critical interface ambiguity goes to the user.
Native failures return actual observations to Astra for bounded replanning.
No automatic screenshots or vision fallback; no unrestricted Codex tool loop.

Try: `Create a centered 100 x 60 x 5 mm plate in this design and verify its dimensions.`
Use a disposable design first. This mode composes native CAD tools rather than
matching the request to the Jev-only workflow menu. It does not automatically
write/install new tool source.

### When a native tool is missing

Choose **Astra + Jev · tool building (source edits)** to explicitly permit Astra
to develop missing native capabilities. It can inspect repository source, create
recipes/tests, hash-patch existing geometry modules, register a new tool, and
proceed directly with CAD in the same run. Tests and completion checks are
optional in this preset; source writes default to skipping tests. Untested or
unchecked work is reported, not blocked. It does not get
an unrestricted Codex shell. Standard hybrid mode remains source-read/write-disabled.

This is **trusted code-development permission**, not a sandbox: generated Python
and tests run on your machine/in Fusion. Backups and syntax/hash checks prevent
some mistakes, but code can still be wrong. Test on disposable designs first.

```sh
STUDIO_PROVIDER=hybrid_dev STUDIO_MODEL=gpt-6-astra ./studio/run.sh
```

For example: attach the intended planar face, then ask for a true native circular
through-hole and say to implement/register/test the missing tool if necessary.
Do not substitute a many-sided polygon for an exact circular hole silently.

See [architecture, plan language, safeguards, and testing](../docs/hybrid-harness.md).

## Jev fast path (experimental)

Set `TYPESAFE_API_KEY` in `studio/.env`, restart Studio, and choose **Jev · native
CAD workflows (experimental)** in the model picker. No extra SDK is needed.
Alternatively launch with `STUDIO_PROVIDER=jev STUDIO_MODEL=jev-1.13.0 ./studio/run.sh`.

Ask one self-contained task. Most workflows no longer require face selection:

- “Inspect this design” / “Show the feature tree” / “List assembly components.”
- “Check for interference” / “List open documents” / “Create a new empty design.”
- 'Create a 100 x 60 x 5 mm plate named "Plate" in this design.'
- “Create a 2 x 3 x 1/8 in box in a new document.”
- 'Inspect body "Plate"' or 'Inspect occurrence "Frame:1/Module:2"'.
- 'Assign 6061-T6 aluminum to body "Plate".'
- 'Convert body "Plate" to a component' / “Convert all root bodies into components.”
- “Undo the last supported agent feature change.”

For face-specific workflows, select faces in Fusion or attach them with **Faces**:

- “Inspect the selected faces.”
- “Measure the closest distance between these two faces.”
- “Measure the perpendicular plane separation between these two faces.”
- Label two attached faces **Start** and **End**, then: “Create a new body by
  extruding Start's entire face section up to End.”

Jev chooses a workflow and literal argument candidates in one API request; code
normalizes units, constructs rectangular geometry, and uses the normal bridge.
Box/plate dimensions must be explicit (shared-unit triples and fractions work).
Default placement is centered XY at Z=0, extruded +Z. Named targets must be quoted. No Astra fallback, automatic screenshots, or
vision calls. Image attachments are rejected. Unsupported or uncertain requests
stop without editing; this is not an open-ended CAD/code-generating agent.

See [architecture, safety limits, and validation](../docs/jev-harness.md).

## The loop

```
   describe intent  ──────────────────────────┐
   (text, or markup on a viewport capture)    │
        │                                     │
        ▼                                     │
   agent asks the questions that actually     │
   change the geometry — once, up front       │
        │                                     │
        ▼                                     │
   builds through the bridge, verified and    │
   checkpointed operation by operation        │
        │                                     │
        ▼                                     │
   harness captures the viewport and hands    │
   the image back to the model                │
        │                                     │
        ▼                                     │
   you say what's wrong  ────────────────────┘
```

## Model backends

The agent loop is written against a neutral protocol (`providers/base.py`) and
uses provider capabilities to select execution and image handling:

| Adapter | Reaches | Auth | Runs the tool loop |
|---|---|---|---|
| `hybrid_dev` | Astra + Jev with trusted native-tool development | Codex login + `TYPESAFE_API_KEY` for choices | this harness; source edits and tests explicitly enabled |
| `hybrid` | Astra planning via Codex + explicit Jev decisions | Codex login + `TYPESAFE_API_KEY` for choices | this harness (structured plan executor) |
| `jev` | TypeSafe Jev, native CAD workflows | `TYPESAFE_API_KEY` | this harness (deterministic workflow) |
| `codex` | GPT-5.x via the Codex CLI | **your ChatGPT subscription** | Codex, via MCP |
| `anthropic` | Claude models | `ANTHROPIC_API_KEY` | this harness |
| `openai` | OpenAI **and any OpenAI-compatible server** | `OPENAI_API_KEY` | this harness |
| `gemini` | Google Gemini | `GOOGLE_API_KEY` | this harness |

### Codex: subscription auth, no API key

A ChatGPT subscription and API credits are **separate products**. The
subscription grants no API credits, which is why an `sk-proj-…` key can be
valid and still return `insufficient_quota`.

Codex sidesteps this. It authenticates with OAuth against the ChatGPT backend
(`auth_mode = "chatgpt"`, and its config literally holds `OPENAI_API_KEY =
None`), so the subscription applies. This adapter shells out to the CLI and
maps its JSONL event stream onto the studio's UI events. **No credential is
read, copied, or forwarded by this code** — Codex authenticates itself.

Prerequisite: register the bridge as an MCP server once.

```sh
codex mcp add fusion-cad -- /usr/bin/python3 /Users/patmcn/fusion-cad-agent/mcp_server.py
```

Then launch the studio on it:

```sh
STUDIO_PROVIDER=codex STUDIO_MODEL=gpt-5.6-sol ./studio/run.sh
```

Codex is an **agent**, not a model: it runs its own loop and executes tools
itself through MCP, so the harness runs one turn and stops rather than
executing tools a second time (`Provider.is_agent`). Consequences: tools come
from Codex's MCP config rather than the `tools` argument, the studio's system
prompt is passed as first-turn context alongside Codex's own instructions, and
conversation history lives in Codex's session (`exec resume`) rather than here.

### Images in tool results

| Adapter | Behaviour |
|---|---|
| `anthropic`, `codex` (MCP) | attached to the operation that produced them |
| `openai`, `gemini` | sent as a following user turn — those APIs' tool-result messages are text-only |

**Local models are not a separate backend.** They are the `openai` adapter
pointed at a different `base_url`, which covers Ollama, LM Studio, vLLM,
llama.cpp, OpenRouter, Groq, Together, and DeepSeek. Chat Completions was
chosen over the newer Responses API precisely because it is the dialect the
whole ecosystem implements.

Switch models from the header. **History does not survive a switch** — thinking
blocks and tool-call ids are provider-native and cannot be transplanted, so the
conversation resets rather than silently corrupting.

### Confidence in each adapter

The OpenAI-compatible adapter has now been exercised live with configured GLM
worker and vision models. Adapter unit tests cover Anthropic and Gemini as well;
the GLM measurements do not establish their live reliability or performance.

## Design notes

**Tool schemas are explicit.** The bridge's `/v1/tools` registry publishes
`tool_schemas.json` contracts, including required fields and array item types.
Both client and native dispatch validate them before execution. Studio begins
with 11 general inspection, document, operation-control and discovery tools,
including `create_document` to create and activate an empty unsaved design.
`load_tools` adds geometry, editing, patterns, assembly, generators, engineering,
validation, files, source, documents or diagnostics as needed. Specialist
instructions appear only with their group. See the
[initial-tool review](../docs/initial-tool-surface.md) for the selection rationale.

**The model checks the finished sequence.** It requests a clean capture with an
expected stage after related mutations; the harness supplies one fallback review
if needed before accepting a completion. Native feature inspection establishes
sketch contents and health. Vision supplies uncertain observations, with its
latency and token usage recorded separately.

The GLM/OpenAI-compatible path uses a short prompt, bounded outputs, and
retrievable older results. The live GLM revision replay and remaining limits
are documented in [the interface validation report](../docs/agent-interface-validation.md).

**A manual agent loop, not the SDK tool runner.** The server emits its own SSE
protocol to the browser and injects images into tool results mid-loop; the
runner's per-turn hooks would make that more convoluted than the loop itself.

## Markup

Press **Capture**, then **Markup** to draw on the frame. Circle the problem,
attach, and describe it. The capture and the annotation are flattened into one
image so the model sees the marks in place rather than as an overlay it has to
align itself.

## Development

```sh
cd studio && .venv/bin/python -m unittest discover -s tests -t .
```

The tests cover schema translation, registry integrity, and capability
advertisement — no SDK, key, or network required, because that is where the
bugs are. One they already caught: the Gemini schema cleaner was filtering the
`properties` map by its keyword allowlist, stripping every parameter name and
shipping tools that took no arguments.

## Environment

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | Backend credentials |
| `STUDIO_PROVIDER`, `STUDIO_MODEL` | Startup backend (default `anthropic` / `claude-opus-5`) |
| `STUDIO_BASE_URL` | Point the OpenAI adapter at a local server |
| `STUDIO_EFFORT` | `low` / `medium` / `high` / `xhigh` |
| `STUDIO_PORT` | Default 8765 |
| `STUDIO_TRANSCRIPT` | `off` to stop recording sessions |
| `STUDIO_TRANSCRIPTS` | Where transcripts go (default `<agent home>/transcripts`) |
| `OPENROUTER_API_KEY` | One key for every model on OpenRouter |
| `STUDIO_VISION` | `describe` (default), `both`, `off` — see below |
| `STUDIO_VISION_PROVIDER`, `STUDIO_VISION_MODEL` | The model that looks at captures (default `anthropic` / `claude-opus-5`) |
| `STUDIO_VISION_EFFORT` | Reasoning effort for the vision role (default `low`) |

### OpenRouter

One key, per-call billing, no subscription. Get a key at
[openrouter.ai/keys](https://openrouter.ai/keys), put it in `studio/.env`, and
pick any model id from `openrouter.ai/models`.

The adapter reads OpenRouter's public catalogue at startup and tells you what
the model you chose can actually do, before a session begins rather than
twenty turns in:

```
worker       openrouter / deepseek/deepseek-v4-flash -- $0.08/$0.16 per Mtok,
             1048K ctx  [cannot see images -- captures will be described, and
             your markup will be too]
```

#### Picking models

Checked 2026-08-26 — Artificial Analysis Intelligence Index v4.1.1 against
OpenRouter's live price and weekly-usage rank:

| model | index | $/Mtok in/out | OR rank | vision |
|---|---|---|---|---|
| `z-ai/glm-5.3-flash` | **57** | 0.07 / 0.25 | #2 | yes |
| `google/gemini-3.7-flash` | 56 | 0.38 / 1.88 | #12 | yes |
| `qwen/qwen3.8-flash` | — | 0.16 / 0.47 | **#1** | yes |
| `openai/gpt-5.6-luna` | 52 | 0.20 / 1.20 | — | yes |
| `~deepseek/deepseek-v4-flash-latest` | 50–52 | 0.03 / 0.10 | #27 | no |

GLM-5.3-Flash is the value pick: highest index of the cheap tier, five times
cheaper than the next model above it, and multimodal so it can serve both
roles. A text-only worker is fine in the default `describe` mode — the vision
role reads every capture and hands over text, so the worker never sees an
image regardless.

The index is general capability, not CAD capability. `bench.py --survey`
re-runs the live half of this table whenever models turn over, and
`--worker` / `--vision` settle it on real work.

It also reports **real spend per turn**, taken from OpenRouter rather than
estimated from a price table, so caching and per-image charges are counted
properly.

### The vision role

After every mutating tool the harness captures the viewport. That picture is
the only signal that says whether the part *looks* like what was asked for —
and it is also a permanent tenant of the transcript, re-sent on every turn
that follows. One logged session ran 1.93M input tokens against 7K of output,
largely because fourteen captures accumulated.

So by default the capture goes to a vision model **on its own, with no history
attached**, and what enters the conversation is a few lines of description
instead of the image. The call is stateless and therefore cheap even on a
frontier model, which is where you want the frontier model — and it frees the
loop model to be something small and fast:

```sh
STUDIO_PROVIDER=anthropic STUDIO_MODEL=claude-haiku-4-5 \
STUDIO_VISION_MODEL=claude-opus-5 ./studio/run.sh
```

Images **you** attach and mark up are never summarized. You are pointing at
something, and a paraphrase would discard the arrow.

Set `STUDIO_VISION=both` to keep the image as well while you judge whether the
descriptions are good enough; `off` restores the previous behaviour.

## Limits

- Captures are fit-to-whole, so a 0.5 in screw on a 30 in assembly is invisible.
  Fine detail cannot be reviewed visually yet.
- Bound to one Fusion document and one conversation; there is no project or
  session history.
- The server binds `127.0.0.1` and serves images only from the agent's capture
  directory, but it has no auth of its own — do not expose the port.


## Chats

The left rail holds separate conversations. `+ New` starts one, clicking
switches, `×` deletes. Titles come from the first message.

Chats are separate for the same reason branches are: a CAD session builds
state in the model — what body it just cut, which occurrence it mated — and
interleaving two lines of work in one history makes both worse. An
experiment can also be abandoned without poisoning the thread that was
going well.

Each chat carries **its own model**, so the picker applies to the current
chat rather than globally. That is what makes a real comparison possible:
same task, one chat on GLM-5.3 Flash and one on Gemini 3.7 Flash, then
`analyze.py --compare`. Each chat also gets its own transcript file, for the
same reason.

Reopening a chat replays it from the server, using the same renderer the
live stream uses — if those were separate, history would show something
subtly different from what you watched happen.

## Transcripts

Every session is recorded to JSONL, one file per run, appended as events
happen. This exists because of an asymmetry: when a design session went
badly it was possible to work out *why* — 104 tool calls, a quarter of them
grepping the source, fourteen captures piling up in context — only because
that agent happened to keep a rollout log. The studio kept nothing, so its
sessions could be judged only by whether the part looked right at the end.
That is the same blindness this whole project is about.

Recording hangs off the agent loop's existing `emit` seam, so it cannot
drift from what the UI shows — it is the same stream. Image bytes are never
written; a capture is stored as a path plus what the vision model said about
it. Nothing reads the environment, so no key can reach the file.

```sh
python3 studio/analyze.py             # the most recent session
python3 studio/analyze.py --all       # one line per session
python3 studio/analyze.py --compare   # grouped by worker model
```

```
  1 user turn(s), 5 tool calls, 214s wall
      68.1 s   in Fusion   ( 32%)
     145.9 s   in the model ( 68%)

  what the calls were for
    BUILD       2  40%  ###########
    inspect     1  20%  ######
    capture     1  20%  ######
    verify      1  20%  ######

  tokens  in 65,000 (88% cached)   out 1,300
  cost    $0.0068   ($0.00136 per tool call)

  rolled back: rib_width_held x1
  warnings:    corners_machinable x1
```

Two distinctions the analyzer is careful about, because both are easy to get
wrong and both would corrupt the conclusion:

- **A warning is not a rollback.** `corners_machinable` failing means the
  part is not millable, not that the operation was undone. Counting them
  together inflates the failure rate with millability notes.
- **`session_cost_usd` is cumulative**, so the total is the last value, not
  the sum of them.

`--compare` is the one to reach for when deciding whether a cheaper model is
good enough: same task, different worker, and the columns that matter are
build-percentage and rollbacks rather than price.
