# Jev native workflow harness (experimental)

## Scope

Jev is the only model used by this mode. It chooses between bounded workflows,
not arbitrary tool calls. Python constructs arguments from exact user-selected
face references, literal dimensions, and quoted names; Studio validates and
executes through the existing API bridge.
No Codex/Astra fallback, generated Python, screen control, or vision model.

Supported workflows:

| Request | Native operations after routing |
| --- | --- |
| Inspect selected faces | Reuse the native face report already obtained |
| Closest distance between two faces | `measure_geometry(mode="minimum")` |
| Perpendicular separation between two planar faces | `measure_geometry(mode="plane_separation")` |
| New body from a full start-face section to another face | Revalidate references, then `extrude_between_faces` |
| Design overview / feature tree | `design_summary` / `feature_tree` |
| Assembly overview / interference | `list_joint_candidates` / `check_interference` |
| List documents / new empty document | `list_documents` / `create_document` |
| Dimensioned box/plate in a new document | `create_test_box` with all three dimensions explicit |
| Dimensioned box/plate in the current document | Code constructs an XY rectangle; `extrude_polygons` |
| Named body or nested occurrence inspection | `inspect_geometry` with an exact quoted name/path |
| Assign 6061-T6 to one named root body | Resolve exact body, then `set_6061_material` |
| Convert named or all root bodies to components | `bodies_to_components` (named target resolved first) |
| Scoped additive-feature undo | `undo_last`, not general Fusion UI Undo |

This version does not invent dimensions, infer mounting surfaces, create arbitrary
sketches, traverse entire nested assemblies autonomously, switch documents, cut,
join, or write source. Multi-action and unsupported requests must map to
`unsupported` (a box in a new document is one supported workflow). Requests must
be self-contained, not “do the same again.”

Examples:

- `Create a 100 x 60 x 5 mm plate named "Plate" in this design.`
- `Create a 2 x 3 x 1/8 in box in a new document.`
- `Assign 6061-T6 aluminum to body "Plate".`
- `Inspect occurrence "Frame:1/Module:2".`
- `Convert all root bodies into components.`

`studio/providers/jev_inputs.py` parses explicit lengths in mm/cm/m/in, including
fractions and triples with a shared trailing unit. Jev selects candidates for
X, Y, and Z; code performs unit conversion and geometry construction. No default
box dimensions are accepted. A cube can reuse its explicit side length. Width
and height refer to X/Y, depth/thickness to Z. Placement is explicitly reported:
centered XY at Z=0, extruded in +Z. Other placements/extra features are unsupported.
Lengths must be positive and <=100 m. There are at most 12 dimension candidates
and 12 exact quoted name candidates. Named targets are resolved natively before
material assignment or conversion, rejecting missing or ambiguous root bodies.

## Setup

1. Obtain TypeSafe API access at https://console.typesafe.ai/keys.
2. Add `TYPESAFE_API_KEY=...` to the gitignored `studio/.env` (never commit it).
3. Restart Studio. Choose **Jev · native CAD workflows (experimental)**.
   Alternatively start with:

   ```sh
   STUDIO_PROVIDER=jev STUDIO_MODEL=jev-1.13.0 ./studio/run.sh
   ```

4. Run the existing Fusion add-in with an active design. Most workflows do not
   require selected faces. For face workflows, Studio's **Faces** button attaches
   exact, labeled references. Explicit attachments override the current Fusion
   selection. Test mutations in a disposable design first.

There is no additional Python dependency. Transport uses the standard-library
HTTP client and `POST https://api.typesafe.ai/v1/systemone`. A configured key is
not a connectivity test. The normal model picker can switch back to Astra.

## Execution

`studio/providers/jev_provider.py` implements the existing provider protocol as
a bounded state machine rather than a generative chat adapter:

1. Read current selection, or revalidate attached references. The native report
   also establishes the bridge's observed document. An empty selection does not
   prevent non-face workflows from running.
2. Send the request, compact face identifiers/labels, and literal candidates to
   Jev. Independent Choice questions run in one request: workflow, extrusion
   direction, and applicable dimension/name slots. No arithmetic is delegated to
   Jev. Missing/uncertain required arguments stop before mutation.
3. Validate returned options, probability distributions, and confidence locally.
4. Build an allowlisted workflow. `load_tools` steps expose its schemas locally;
   subsequent provider turns do **not** call a model.
5. Execute one native operation at a time through Studio's normal schema
   validation, document binding, mutation confirmation, operation journaling,
   evidence invalidation, and recovery path.
6. Return native data and verification/recovery, without claiming visual review.

Extrusions require exactly two faces and a clear start/end mapping. Labels and
user words establish roles; raw selection order is not sufficient. The native
extrusion implementation enforces distinct parallel planar faces, timeline
position, and new solid-body creation. It creates one new body, preserving the
start face's holes. It does not implement wall thickness, offsets, cuts, or joins.

## Latency and observability

- One Jev request per supported task; no repeated model planning between tools.
- No conversation replay, full geometry dump, or system prompt sent to Jev.
- No automatic screenshots, final captures, or vision descriptions in this mode.
  Image attachments are rejected instead of silently invoking another model.
- Existing Studio tools show immediate progress. `jev_progress` and
  `jev_decision` events show decision progress and elapsed time.
- Transcripts retain question version, resolved model ID, typed answers,
  probabilities, usage, and decision latency. Every `tool_end` now records
  `seconds` (client-observed elapsed time, **not isolated kernel time**).
- Network timeout is 15 seconds, without automatic retries. Cancellation is
  checked through Studio events; an in-flight HTTP request may finish first.
- Native geometry work still takes its normal kernel time.

The Jev preset does not change Astra's MCP screenshot behavior. That is a
separate optimization, not needed by this direct Studio execution path.

## Safety and limitations

The workflow and all applicable argument/direction choices require confidence >= 0.85 and chosen
probability >= 0.90. These are conservative experimental defaults, **not measured
CAD calibration or safety guarantees**. Jev can misclassify a request confidently.
Test in disposable designs until evaluated on representative tasks. Schema-valid
output does not prove semantic correctness or user authorization.

- Face references are revalidated again after the Jev decision before extrusion.
  Stale/deleted faces or document mismatches stop the workflow without substitution.
- A failed native call stops the sequence and reports recovery. No automatic
  repairs, rollbacks beyond the existing native behavior, or mutation retries.
- An unknown/timed-out mutation gets one `operation_status` check. If unresolved,
  another user message checks status only, never repeats the mutation. Terminal
  outcomes are reported and require a separate task to continue. The shared
  bridge also maintains its own unresolved-operation guard.
- One tool result is consumed per dispatched call, with matching call ID.
- Workflow dispatch is capped at 12 tools. No model-generated tools or arguments
  are accepted.
- The current user request, literal dimension/name candidates, and compact face
  labels/body/occurrence names are sent to TypeSafe's cloud. Native tokens, viewport images, prior conversations,
  and unrelated design state are not sent. Do not enable this mode for designs
  whose relevant metadata cannot be shared with that service.
- Output is native evidence, not certification of manufacturability, structural
  suitability, interference freedom, or satisfaction of broader design intent.

## Validation

Offline tests exercise the actual Studio loop with a mocked TypeSafe response
and bridge: dispatch, exact selectors, attachment precedence/order, confidence,
role ambiguity, missing faces, stale references, cancellation, partial recovery,
timeout polling, literal dimension parsing, unit conversion, rectangular geometry,
argument ambiguity, named-target preflight, global workflows with no selected faces,
and no screenshot/vision calls. Transport tests check endpoint,
request shape, timeout, malformed responses, and errors without leaking secrets.

```sh
python3 -m unittest discover -s studio/tests -p 'test_jev*.py' -v
studio/.venv/bin/python -m unittest discover -s studio/tests -v
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Offline mocks do not establish real Jev classification quality or live Fusion
success. Before broader use, collect labeled requests (including negations,
compound tasks, unsupported offsets/cuts, ambiguous roles, and adversarial text)
and evaluate unsafe dispatch rate, false refusals, and end-to-end latency.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[model specifications](https://docs.typesafe.ai/models),
[known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
