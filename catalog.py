"""Generate the source map that keeps agents from grepping this repo.

A design session logged 25 shell calls -- a quarter of the whole run -- to
`rg` and `sed` its way through these modules working out what already
existed. Every one of those was a full round trip carrying the entire
conversation, and every one of them was answerable from the source without
running anything.

So answer it up front. This module walks the pure geometry modules with
`ast` and emits a signature index that ships inside the system prompt. It
costs ~1200 tokens once instead of ~25 round trips, and because it is
derived from the AST rather than written by hand it cannot go stale the way
a prose list of "available helpers" always does.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

# Only the pure modules. `tools.py` is excluded on purpose: its surface is
# already delivered as JSON Schema tool definitions, so repeating it here
# would pay for the same information twice.
MODULES = {
    "patterns.py": "lattice and hole-position math",
    "geom.py": "2D computational geometry",
    "mechanics.py": "closed-form sections, beam cases, DFM rules",
    "assertions.py": "the check framework and the checks",
    "pick.py": "geometry selection queries",
    "ops.py": "modelling primitives, one Fusion call each",
    "fasteners.py": "fastener and thread tables",
    "checkpoints.py": "timeline checkpoint / rollback",
}


def _signature(node: ast.FunctionDef) -> str:
    a = node.args
    parts = [arg.arg for arg in a.posonlyargs + a.args]
    # Defaults bind to the tail of the positional list.
    defaults = a.defaults
    if defaults:
        for i, default in enumerate(defaults):
            index = len(parts) - len(defaults) + i
            parts[index] += "=" + _literal(default)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(arg.arg + ("=" + _literal(default) if default else ""))
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return f"{node.name}({', '.join(parts)})"


def _literal(node: ast.AST) -> str:
    try:
        return repr(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError):
        return "..."


def _summary(node: ast.AST) -> str:
    """First sentence of the docstring, or nothing."""
    text = ast.get_docstring(node) or ""
    if not text:
        return ""
    first = text.strip().split("\n\n")[0].replace("\n", " ").strip()
    return first[:110]


def _module_entries(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            summary = _summary(node)
            lines.append(f"  {_signature(node)}" + (f"  -- {summary}" if summary else ""))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = [
                m.name for m in node.body
                if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")
            ]
            summary = _summary(node)
            lines.append(f"  class {node.name}" + (f"  -- {summary}" if summary else ""))
            if methods:
                lines.append("    ." + "  .".join(methods))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                # Registries and tables are the things an agent hunts for.
                if isinstance(target, ast.Name) and target.id.isupper():
                    lines.append(f"  {target.id} = {_shape(node.value)}")
    return lines


def _shape(node: ast.AST) -> str:
    """Describe a constant's shape without dumping its contents."""
    if isinstance(node, ast.Dict):
        keys = [_literal(k) for k in node.keys[:12] if k is not None]
        return "{" + ", ".join(keys) + ("...}" if len(node.keys) > 12 else "}")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return f"<{len(node.elts)} items>"
    return _literal(node)


def source_map() -> str:
    """The signature index, as markdown, for embedding in a system prompt."""
    out = ["## Source map",
           "",
           "These modules hot-reload. This index is generated from the source on",
           "every load, so it is current -- do not grep for what is listed here.",
           ""]
    for filename, blurb in MODULES.items():
        path = ROOT / filename
        if not path.exists():
            continue
        entries = _module_entries(path)
        if not entries:
            continue
        out.append(f"### {filename} -- {blurb}")
        out.append("```")
        out.extend(entries)
        out.append("```")
        out.append("")
    return "\n".join(out)


def brief() -> str:
    """AGENTS.md plus the generated source map: the whole standing brief."""
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8").strip()
    return f"{agents}\n\n{source_map()}"


if __name__ == "__main__":
    print(brief())
