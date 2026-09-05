"""Read KEY=value lines from a .env file into the environment.

Named `envfile` rather than `dotenv` on purpose: a module called `dotenv`
here would shadow the widely-installed package of that name for anything in
this process. The same class of bug as calling a module `select` and breaking
the standard library out from under an HTTP server.

Shared by the studio server and the bench script. Both need the same keys,
and a second copy of this logic is how one of them ends up loading a file the
other does not.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(path: Path | None = None) -> list[str]:
    """Load a .env file. Returns the names of the keys it set.

    An already-set variable always wins, so an explicit `export` in the
    launching shell overrides the file rather than the file silently
    shadowing it. Values are never logged -- only names are returned.
    """
    path = path or HERE / ".env"
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded
