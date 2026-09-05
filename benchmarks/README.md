# Acceptance fixtures

`baseline.json` records the original source/test baseline. `cases.json` registers
the eight intended acceptance cases and the evidence needed for a run. Native
fixtures have not been built or executed yet; neither file claims a CAD pass.

Create fixtures only in disposable documents once the add-in is running. Save
accepted native artifacts under `artifacts/` and evidence under `runs/`; both are
excluded from source commits. Assign fixture versions and content hashes before
comparing runs. Record vendor sources and exact mounting requirements when the
assembly fixtures are built.

The plate case supplies independently chosen dimensions and tolerances. For
assembly cases, record the actual fixture's interface and clearance requirements
before executing a candidate edit. Do not derive the acceptance target solely
from the generator being evaluated.

Use the existing studio transcript as the execution record. Its `Recorder`
already accepts additional context; record the case, fixture, source, and recipe
versions with each benchmark session. The analyzer retains that context. The
existing `studio/bench.py` tool-choice probe does not execute CAD and cannot count
as a native acceptance run.

Include failed and interrupted runs in comparisons. Missing cost/timing evidence
is unknown, not zero. Inspect the native feature tree and affected geometry before
marking an edit accepted. The frame/reopen/manual-edit cases are gates for later
phases, not capabilities claimed by the current runtime.
