"""Accident-prevention policy for explicitly enabled native-tool development.

This is not a sandbox: generated Python/tests execute as the user's code.
The underlying workspace tools also check syntax, hashes and create backups.
"""
from pathlib import Path
import re

from hybrid_plan import PlanError

REPO = Path(__file__).resolve().parent.parent
SOURCE_TOOLS = {"read_file", "search_files", "write_file", "patch_file", "run_tests"}
SOURCE_WRITES = {"write_file", "patch_file"}
GEOMETRY_FILES = {"tools.py", "tool_schemas.json", "spatial.py", "spatial_contracts.py",
                  "geom.py", "ops.py", "patterns.py", "pick.py", "mechanics.py",
                  "fasteners.py", "assertions.py", "complexity.py"}


def repo_path(raw, root=REPO):
    if not isinstance(raw, str) or not raw:
        raise PlanError("Development paths must be explicit repository paths")
    root = root.resolve()
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if path != root and root not in path.parents:
        raise PlanError("Tool development is limited to this repository")
    return path, path.relative_to(root)


def guard(tool, arguments, root=REPO):
    if tool in {"read_file", "write_file", "patch_file"}:
        path, relative = repo_path(arguments.get("path"), root)
        if any(part.startswith('.') for part in relative.parts) or path.name.lower() in {"auth.json", "connection.json"}:
            raise PlanError("Hidden files and credentials are not development inputs")
    elif tool == "search_files":
        repo_path(arguments.get("directory", "."), root)
        return
    else:
        return
    if tool not in SOURCE_WRITES:
        return
    allowed = (len(relative.parts) == 2 and relative.parts[0] == "fusion_cad" and path.name in GEOMETRY_FILES or
               len(relative.parts) == 2 and relative.parts[0] == "recipes" and path.suffix == ".py" or
               len(relative.parts) == 2 and relative.parts[0] == "tests" and path.name.startswith("test_") and path.suffix == ".py")
    if not allowed:
        raise PlanError("Development writes are limited to geometry modules, recipes/*.py, tests/test_*.py and tool_schemas.json; client/runtime/security files are protected")
    if tool == "write_file" and path.exists():
        raise PlanError("Use a hash-guarded patch_file for existing source; write_file is new files only")
    if tool == "patch_file" and not re.fullmatch(r"[0-9a-fA-F]{64}", arguments.get("expected_sha256", "")):
        raise PlanError("Source patches require the SHA-256 returned by a fresh read")
