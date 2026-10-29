# File-search loop fix — 2026-09-05

The live drivebase run `1e7a77784785` made 20 `list_files` calls. Its last
requests searched Downloads for the literal filename `isogrid`, receiving
empty lists. The explicit schema incorrectly limited `list_files.pattern`
to `isogrid` and `hex`; `run_tests.pattern` had the same mistake. The native
filesystem functions actually support arbitrary filename globs.

Removed those two enums, leaving the legitimate lattice enums intact. Added
a contract regression covering STEP/PDF/test globs. The corrected registry was
hot-reloaded into the running Fusion add-in.

The Studio runtime also now warns after two identical successful reads with
unchanged results, then stops after three. It detects alternating query loops,
ignores generated result handles, resets after attempted mutations (including
potential partial failures), and exempts operation-status polling. Unexecuted
calls remaining in a stalled batch receive paired tool errors.

The OpenAI-compatible adapter now retains returned reasoning strings or the
ordered structured reasoning sequence between tool calls, as described by
[OpenRouter's reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
This repairs a separate adapter omission; its effect on the drivebase task was
not isolated. A live file-search probe still selected `isogrid` after that
adapter fix, before correcting the schema.

Validation:

- 248 core tests and 110 Studio tests pass, including loop detection, changed
  results, mutation invalidation, tool-call pairing, and reasoning round trips.
- Browser checks pass, including restart recovery of prompts and images.
- Fresh live GLM probe `fcc2e05c7205` used the unchanged request to find the
  named WCP-1702 STEP file. It loaded file tools, recovered from an invalid
  relative directory by listing readable roots, searched `*WCP-1702*`, and
  reported the correct path. Four tool calls; no geometry modifications.
- Studio was restarted with runtime fixes. The native schemas were verified
  live. The complete drivebase build remains untested with these corrections.
- Sidebar draft `4e971bbf6cb0` restores the original 2095-character request
  and three submitted images. Inspection of the browser's current stored draft
  confirms all three hashes match its independent submitted-message record.

Earlier session logs were saved under the agent transcript directory's
`runtime-restart-20260905/` folder before restarting Studio.
