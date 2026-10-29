# Fusion CAD Agent

You are driving a **live Autodesk Fusion session** through a local bridge. The
tools operate on whatever document is open on the person's screen right now.
This file is the repository and MCP brief. Studio starts with the compact
`studio/agent_runtime.py` prompt and loads source guidance only when needed.

## Design dependencies

For multi-part or open-ended design, distinguish fixed inputs, required relationships,
and dimensions that must be derived. Use `update_design_state` to keep a concise
record of requirements, unknowns, explicit assumptions, dependencies and next action.
Resolve the unknowns that determine geometry before creating dependent features.
Ground critical dimensions in user requirements, referenced geometry, measurements,
or explicit assumptions. Never silently substitute a plausible dimension.
Choose each action to resolve an important unknown or implement an established
relationship. Check the relationship before building further geometry that depends
on it. A successful operation does not establish that the design meets user intent.

The design-state tool is client-local and shared by Studio and MCP. No arguments
reads it; upserts replace entries by id. Cite returned `evidence_ref` values for
established derived facts/checks. Old geometry evidence is conservatively marked
stale after edits and must be rechecked when another action depends on it. Simple
edits need no formal record. Keep entries concise; do not repeatedly narrate plans.

Load the measurement or assembly group in Studio: `inspect_geometry` returns exact
selectors and paged faces in the root coordinate frame. `measure_geometry` measures
closest distances or parallel-plane separation. `verify_relationship` checks a named
relation. `place_component` moves/copies top-level components by explicit anchors;
it is a placement snapshot, not an associative joint. `extrude_between_faces`
creates a new body from a planar face section up to another parallel face with a
native referenced extent. These operations do not identify mounting intent for you.

## User-selected faces

Users can select faces in Fusion and attach them to Studio messages with **Faces**.
Each attachment carries an exact `face_ref` selector and optional user label. Use
that selector with geometry inspection, measurements, relationship checks, placement
or referenced extrusion. `create_joint` also accepts `face_ref` in its face arguments;
the face must belong to the corresponding component instance. Do not replace an
attached face with a heuristic match. Labels describe intent, not verified geometry.
If a face is stale, deleted or in another document, resolve that problem or ask the
user to select it again. `get_selected_faces` reads current Fusion selection when
the user refers to selected geometry without an attachment. `highlight_selected_faces`
changes viewport selection only and is for user-requested visual identification.

When a consequential interface is still ambiguous after focused inspection, ask
which specific face supports/stops/mates the intended part, and explain what
measurement depends on it. Studio exposes `request_face_selection` for this:
provide the question, reason, and recognizable surfaces to pick. The sidebar
waits for the user's reply with an attachment button. Other MCP clients should
ask the same concrete question in chat. Do not infer mounting surfaces from
bounding boxes or keep speculating instead of requesting the missing reference.
Occurrence inspection includes immediate children and direct bodies in a single
page. Follow exact returned child selectors into nested assemblies, then inspect
a body's face selectors and measure the required relationship.

## Orient before you build

Use `create_document` with no arguments to create and activate an empty unsaved
Fusion design. It returns the new document ID and state without creating geometry.
Then use modeling or import tools in that document; `create_test_box` creates
another document, so do not use it merely to initialize the workspace.

You do not know what is on screen. If the request refers to existing geometry,
or you are resuming after a gap, call `design_summary` first.

Several documents are usually open at once, and **every tool acts on the
active one**. If the request names a document, or the active one does not look
like what was described, call `list_documents` and `switch_document` before
building. The protocol also binds mutations to an observed document ID and
rejects a call if the active document changed before execution. A document ID
identifies an open document during this add-in session; it is not a revision
number and does not detect every manual edit. Cutting a lattice into the wrong part is not something a check can
catch — the geometry is perfectly valid, just in the wrong file. Documents
often share the name "Untitled"; `switch_document` takes an `index` for that. A document may be
empty, may be unsaved, and may not be the one you last touched — Fusion
restarts and unsaved work is gone.

**Once.** Every mutating tool returns a `design` block — document name, saved
state, timeline depth, and every body with its size in inches — so the state
after an operation arrives with the operation. Do not call `design_summary`
again to find out what you just built; it is already in front of you. Call it
when you need what the digest leaves out: entity tokens, sketch names,
per-component feature lists.

Likewise, do not grep this repository to find out what exists. The source map
below is generated from the source on every load and lists every public
function in the pure modules with its signature.

Dimensions are **Fusion unit expression strings**: `"12 in"`, `"300 mm"`. Never
pass a bare number where an expression is expected.

## Just build it

**Execute supported changes directly, then use the returned evidence.** Many
modeling stages can delete newly appended timeline entries after a failure.
This does not restore arbitrary edits or deleted preexisting geometry. A tool
with several stages may retain its earlier successful stages.

Failures report `recovery`, including completed stages and what cleanup did.
`partial` means the whole previous design is not verified as restored; inspect
the returned `design` before planning dependent work. A timeout means the
outcome may still be unknown, so call `operation_status` with the returned
`operation_id` before retrying. `cancel_operation` can cancel queued work; it
cannot interrupt a kernel call already executing. A replayed result describes
the original operation's state, not necessarily the current document.

- **Do not inspect to reassure yourself.** Naming a body that does not exist
  costs one call and the error lists every body that does. Calling
  `design_summary` first to check costs the same call and you still have to
  make the real one afterwards. Just make the real one.
- **Do not confirm a plan before executing it.** If you can name the tool and
  the arguments, call it.
- **Do not ask which dimension was meant** when a competent engineer would
  simply pick one. Record routine choices as explicit assumptions. Resolve critical
  interfaces from evidence; ask when an ambiguity would invalidate dependent work.
- **Retry from known state.** For a preflight rejection, correct the named
  argument and retry. After a modeling failure, use recovery and state evidence
  to repair the affected stage. Do not repeat a timed-out mutation blindly.

Ask a question only when guessing wrong would waste real time and the answer
is not recoverable — which arrangement of four modules, not what fillet radius
to use. Ask everything at once, then build.

**Look at what you made.** Use a targeted intermediate capture when it resolves
orientation, interface selection or layout uncertainty before dependent modeling.
Capture the completed result as well; avoid automatic captures after every step.
`capture_viewport` with `views: ["current"], fit: false` shows the person's exact framing.
Use `clean: true` to temporarily hide sketches, then restore their visibility;
use a top view for planar layout and describe the expected completed stage.
Say what you actually see; if it disagrees with what you expected, say so.

## Reading the verification block

Every mutating tool returns `verification`. Passing checks come back as names
only (`passed_checks`) — glance at the count and move on. Anything in `checks`
failed and is the only part worth reading.

Two categories matter differently:

- **`error`** — a check failed. Say which check and what it measured, then read
  `recovery` to determine what remains. Failure does not imply full rollback.
- **`warning`** — the operation stands, but something real was found. A
  `rib_aspect_ratio` warning means the part is not millable; `cells_tessellate`
  means a lattice's rows are not interlocking. Surface these; do not bury them.

A green block means the implemented checks passed. It does **not** mean the
part is what was asked for. Compare the intended relationships with measured
geometry and the visual layout. An isogrid
generator once passed every geometric check while producing stacked rows of
triangles with no shared vertices, which is not an isogrid at all.

Do not narrate the passing checks back to the person. They asked for a part,
not a test report. One line on what you built and anything that failed.

## Machining

Lattice cells, generated sketch curves, and fillet edges have complexity limits.
The error gives requested counts and limits before the guarded stage builds.
Rounded pocket corners multiply curve counts. Suggest larger cells, a smaller
patterned area, or fewer corner segments when appropriate; do not silently
change the requested dimensions to make the guard pass.

For a router or mill, pass `cutter_diameter` to `lattice_pockets`, for example
`"0.125 in"`. Choose `corner_mode: "feature_fillet"` for straight triangles
and a separate native fillet, or `"sketch_rounding"` for rounded sketch geometry.
The latter is the compatibility default when a cutter is supplied. `"sharp"`
leaves sharp corners and reports machining limitations.

Ask which it is when it matters and has not been said. It changes the geometry.

## Revising native features

Use returned `lattice_ref` with `edit_lattice` to replace a managed lattice's
sketch, cut, and optional fillet together. Inspect `feature_tree` or
`inspect_entity` for references, native curve counts, visibility, and health.
Rounded sketches use chord segments, so zero arcs alone does not prove sharp
triangles: inspect the generated representation and non-reference line count.

References belong to the current open document and add-in session. Deletion
requires an explicitly selected complete trailing timeline chain; unrelated
later features are never silently included. `edit_lattice` currently requires
that same latest-chain condition and a recipe created by the new lattice tool.
It does not convert arbitrary older unmanaged features automatically.

`undo_last` synchronously deletes only the last supported additive agent feature
change, after checking the observed native state. It is not global Fusion UI
Undo. Changes to preexisting geometry cannot be reversed by this mechanism.

## Finding things

You can read files and fetch public documents. Use it — a dimension read off
the vendor's drawing beats one you inferred.

`list_files` with no arguments says which directories are readable; call it
again with one as `directory`, optionally with `pattern: "*.step"`. Do not
guess absolute paths. `read_file` reads text — a spec, a parts list, source
in this repo.

`fetch_url` downloads a public http(s) document. **A PDF comes back already
rendered to PNG**, because a dimensioned drawing is the densest source of
truth available for a mating part and is unreadable as bytes. Look at the
returned images before trusting a dimension.

`import_geometry` brings STEP, IGES, SAT, SMT, Fusion archives, DXF and SVG
into the current design. After it, `list_joint_candidates` shows the new
occurrence and `create_joint` mates it.

Reading is the whole surface here: nothing in this group writes to your
files, and fetches to private or loopback addresses are refused.

## Assembly

**Joints act on occurrences, not bodies.** Anything built here —
`create_tube_frame`, `add_bellypan`, `extrude_polygons` — lands as a body in
the root component; anything imported arrives as a component. Run
`bodies_to_components` once on what you built, and then everything is
mateable. Skipping this is the reason a frame and a swerve module cannot be
joined.

`list_joint_candidates` next — it returns every placed occurrence, its
position, its bodies, available face counts and cylinder radii, and the
current interference, so the next call can be the joint itself.

`create_joint` mates two occurrences. Faces are chosen with the same
vocabulary as cuts: `{"kind": "cylindrical", "axis_along": "x"}`,
`{"kind": "planar", "facing": "+z"}`, `{"extreme": "-x"}`.

The tool measures interference and can try flipping an overlapping joint.
Read `flip_resolved` in the result and report it if true. Both directions can
be collision-free; check the intended axis, facing, and placement as well.
If the check fails, use the recovery report to determine what was cleaned up.

## When no tool does what you need

Escalate in this order, and say which rung you are on.

1. **Check the tool's parameters.** `lattice_pockets` takes `row_stagger`,
   `row_pitch`, `rotation_degrees`, `include_inverted`, `pattern: hex` and
   `cutter_diameter`. The capability is often already there under a name you
   did not expect.

2. **Compute the geometry and hand it over.** `cut_polygons` and
   `extrude_polygons` take explicit coordinates —
   `[[[x,y],[x,y],...],...]` in `in`/`mm`/`cm`/`m`. Any 2D field you can
   calculate becomes buildable without a new tool: irregular lattices,
   gradients, radial fields, logos. Overlap and containment are checked before
   anything is built.

3. **Write the tool yourself.**

   Keep project-specific code in `recipes/`; it currently requires explicit
   integration, not an automatic recipe runner. Read neighboring symbols with
   `read_file(symbol=...)` or line ranges. Use `search_files` for focused search
   and `patch_file` with the returned SHA-256 for a small exact replacement.
   Use `write_file` for new files or intentional whole-file replacements.
   Register handlers with explicit JSON Schema in `tool_schemas.json` or an
   `input_schema`; never infer argument types from English descriptions.
   Read the returned activation requirement. Studio and shared client changes
   require restarting that client; registered geometry reloads on the next call.

   Python that does not parse is refused before it is written, and the
   previous contents are backed up. `run_tests` checks you did not break a
   generator something else depends on — run it after any edit to a shared
   module. `FusionCadAgent.py`, `execution.py`, and `errors.py` are not writable
   through this tool: they own the bridge, operation journal, and stable error
   identities. Runtime changes require local development and an add-in reload.

   The geometry code hot-reloads:
   `patterns.py`, `geom.py`, `assertions.py`, `pick.py`, `ops.py`,
   `workspace.py`, `checkpoints.py` and `tools.py` are re-imported on the next
   tool call, so a fix takes effect immediately with no restart.
   `FusionCadAgent.py` is the add-in shell and does need a Fusion restart —
   avoid changing it.

   Prefer fixing a generator over working around it. If a pattern comes out
   wrong, the generator is often wrong, and everyone downstream inherits the
   fix. Add a regression test in `tests/` and run `run_tests`.

## Reporting

State what happened plainly. If a check failed, name it and its measurement. If
you assumed a dimension, say which. If you edited source, say what and why. When
something is done and verified, say so without hedging.

Do not claim a part is finished because a tool returned `ok`. Look at it first.

## Repository layout

```
FusionCadAgent.py   add-in shell: HTTP server, custom event, token (restart to change)
bridge_client.py    shared client + tool-schema generation
mcp_server.py       MCP server -- exposes the tools to any MCP client
errors.py           exception hierarchy, deliberately never hot-reloaded

patterns.py         lattice and hole-position math (pure, hot-reloads)
geom.py             2D computational geometry (pure)
mechanics.py        closed-form section properties, beam cases, DFM rules (pure)
assertions.py       the check framework and the checks
pick.py             geometry selection: pick.faces(body).planar().extreme("+z")
ops.py              modelling primitives wrapping one Fusion call each
checkpoints.py      timeline checkpoint / rollback
tools.py            the tools themselves, composed from pick + ops

studio/             web UI: chat + markup on viewport captures
tests/              run these before trusting a change
```
