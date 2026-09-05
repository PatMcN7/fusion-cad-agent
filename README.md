# Fusion CAD Agent

A local, authenticated bridge that lets an agent inspect and modify the active
Autodesk Fusion design through Fusion's native in-process API.

Protocol v4 binds mutations to an observed open document and records operation
IDs for status/retry handling. After upgrading, restart the add-in and use the
updated clients. See [runtime behavior and limits](docs/runtime.md), the
[recovery audit](docs/mutation-audit.md), and [implementation progress](PLAN.md).

The September 5 interface update adds explicit argument contracts, native feature
references, managed lattice revisions, bounded source patches, and a smaller GLM
agent loop. See [validation and measured results](docs/agent-interface-validation.md).
Restart Studio after updating its Python files; geometry changes load on the
next bridge request when the registered module changes.

The premise: **the geometry kernel is already a compiler, and nobody is reading
its output.** It knows a boolean produced two bodies, that a cut removed
nothing, that two occurrences overlap by 4.2 cm3. Routing those signals back
after every operation is what separates an agent that builds from one that
stacks ten minutes of work on a silent error.

## Layout

```
FusionCadAgent.py   shell only: HTTP server, custom event, auth token
errors.py           exception hierarchy -- NEVER hot-reloaded, see below
common.py           wire protocol; no Fusion

geom.py        \    pure: computational geometry
fasteners.py    |   pure: what a thread designation means
patterns.py     |   pure: lattice and hole-position math
assertions.py   |   check framework + checks
pick.py         |   geometry selection (a query language)
ops.py          |   modelling primitives
checkpoints.py  |   timeline checkpoint / rollback
tools.py       /    parts, composed from pick + ops
```

Everything in the braced block is hot-reloaded on every request, keyed on file
mtime. Edit, save, call the CLI. No Stop/Run in Fusion.

`errors.py` is excluded on purpose. Reloading a module rebinds its classes, so
an exception raised by a freshly reloaded module is a *different class object*
than the one the shell imported, and `isinstance` silently stops matching. A
never-reloaded module keeps identity stable, so the shell catches one
`BridgeError` and reads `.code` off it.

## Why a selection layer

Fusion's commands take entities, not descriptions. In the GUI your cursor
resolves "that face" for free; through the API it is a search. Measured on the
bespoke first generation, that search was **7 of every 10 lines** -- the
command itself was 3.

```python
face  = pick.faces(body).planar().normal_axis("y").extreme("+y").one()
bolts = pick.faces(tube).cylindrical().axis_along("z").extreme("-z").centers()
profs = pick.profiles(sketch, loops=1, curves=3)
```

`.one()` and `.expect(n)` raise if a query does not resolve to what the caller
claimed, so an ambiguous selection fails loudly instead of grabbing `item(0)`.

`normal_axis()` takes an unsigned axis; `facing()` takes a signed direction and
accounts for `isParamReversed`. They are separate because an earlier combined
method accepted a sign and then ignored it.

## Three layers of verification

1. **Pre-flight** -- pure Python, before the kernel is touched. Calls the *same*
   `patterns.py` functions the build uses, so the simulation cannot drift.
2. **Per-operation** -- supported modeling stages checkpoint appended timeline
   entries and attempt cleanup on failure. Recovery reports distinguish cleanup
   from verified restoration; earlier stages of a compound tool can remain.
3. **Spec** -- `verify_spec` runs a design spec as a test suite, delegating to
   the same `assertions` the build path uses.

A pre-flight only tests what someone thought to test. Layer 2 catches the
kernel's actual behaviour, and it is the one that earns its keep.

## Assertions

| symptom | check |
|---|---|
| overlapping mate | interference volume != 0 (collision-free reversal needs an intent check) |
| upside down | centre of mass Z, face normal |
| wrong scale | bounding box |
| off centre | centroid delta |
| fragmented by a through cut | body count |
| cut driven the wrong way | volume did not decrease |
| wrong thread family | designation parsed back to a major diameter |

Manufacturability checks report as **warnings**: they never block a printed
part, and never pass silently.

An assertion whose expected value is `True` is a receipt, not a check. If it
cannot fail on well-formed-but-wrong output, it is testing nothing -- that is
how `M3.5x0.6` shipped into a 0.125 in hole with 17 checks green.

## Tools

Read-only: `health`, `active_document`, `design_summary`, `check_interference`,
`verify_spec`, `capture_viewport`, `inspect_threads`, `inspect_commands`.

Mutating (all require `confirm=true`): `create_test_box`,
`replace_with_isogrid_bar`, `create_tube_frame`, `add_bellypan`,
`add_fasteners`, `undo_last`.

## Use

```sh
./scripts/install_macos.sh          # once
# In Fusion: Utilities > Scripts and Add-Ins > Add-Ins > FusionCadAgent > Run

python3 fusion_cli.py health
python3 fusion_cli.py summary
python3 fusion_cli.py interference
python3 fusion_cli.py verify --spec spec.json
python3 fusion_cli.py capture --views iso top --prefix part

python3 fusion_cli.py call create_tube_frame --confirm --arguments '{}'
python3 fusion_cli.py call add_bellypan --confirm --arguments '{"expected_holes":100}'
python3 fusion_cli.py call add_fasteners --confirm \
  --arguments '{"expected_designation":"5-40 UNC"}'
```

`--confirm` is enforced by the bridge, not just the CLI. `--raw` goes **before**
the subcommand.

## Development

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile *.py
```

`tests/test_regressions.py` encodes defects that actually reached Fusion:
overlapping isogrid triangles (28 triangles, 43 profiles), the background
profile from sketching on a face (feeding it to the cut would have machined the
bar away and left the pockets standing), an off-centre lattice, and the metric
thread in an inch hole.

Module names are checked against the stdlib by a test: `select.py` would shadow
the module `http.server` reaches through `selectors`, which would break the
bridge's own socket. That is why the selection module is `pick.py`.

## Security boundaries

- Binds `127.0.0.1` on an OS-assigned port, published in `connection.json`.
- Random 48-byte bearer token, `0600`, compared with `hmac.compare_digest`.
- Requests capped at 1 MB; no arbitrary code execution tool.
- Mutating tools require explicit confirmation.
- No cloud save calls.

## Known limits

- **Selectors are positional.** `.extreme("+z")` means "whichever face is
  highest right now". Regenerate and that may be a different entity. This is
  why the tools rebuild rather than edit; stable entity tokens are the next
  real problem.
- **Capture cannot zoom.** Fit-to-whole at 30 in makes a 0.5 in screw
  invisible, so visual review of fine detail is not yet possible.
- No structural analysis. Mass and volume are reported; stiffness is not.
