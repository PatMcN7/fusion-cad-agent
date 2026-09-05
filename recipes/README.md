# Project recipes

Keep project-specific modeling code here instead of changing shared generators
for each design. This directory is a convention; there is no general recipe
runner yet. A module needs explicit integration with a registered handler and
must use the same execution, preflight, and verification contracts.

`edit_lattice` currently stores its own small recipe on the generated native cut
feature. That implementation does not make arbitrary Python files executable or
provide general dependency-aware revisions.
