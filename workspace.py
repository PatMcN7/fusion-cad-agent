"""Local files and the network, fenced.

The gap this closes: an agent asked to mate a swerve module to a tube had no
way to find the STEP file, open the vendor drawing, or read a dimension off
it. In a logged session another agent did exactly that -- fetched the WCP
drawings, rendered the PDF to PNG, and read the mounting pattern off the
page -- and that was the difference between real geometry and a guess.

Three rules hold everything here together.

**Containment.** Every path is resolved through symlinks *before* it is
checked, then tested for containment in an allowed root. Checking the string
first and resolving later is the classic way this goes wrong.

**A denylist inside the fence.** Being in an allowed directory is not enough.
`studio/.env` sits in this repo and holds live API keys; a model that can
read it can put it in a transcript, or in a request to a third-party
endpoint. Secrets are refused by name wherever they are found.

**No inward requests.** Fetching is for vendor drawings, not for reaching
back into this machine. Loopback, private ranges, and cloud metadata are
refused -- the bridge itself listens on localhost, and link-local
169.254.169.254 is where credentials live on a cloud host.

Nothing here writes to the repository. Reading is the whole surface.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import pathlib
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from common import agent_home
from errors import ArgumentError, ToolFailure

MAX_READ_BYTES = 400_000
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
FETCH_TIMEOUT = 25
MAX_PDF_PAGES = 8

# Extensions Fusion can bring in, beyond the STEP-only tool that came before.
IMPORTABLE = {".step", ".stp", ".iges", ".igs", ".sat", ".smt",
              ".f3d", ".f3z", ".dxf", ".svg", ".stl", ".obj", ".3mf"}
# Fusion archives import into the current design like any other format;
# there is no verified API for opening a local .f3d as a *document*, so the
# distinction that used to exist here has been dropped rather than faked.

# Refused wherever they appear, including inside an allowed root. A model that
# reads a key can leak it to any endpoint it is allowed to reach.
SECRET_NAMES = {".env", ".netrc", ".htpasswd", "auth.json", "credentials",
                "credentials.json", "id_rsa", "id_ed25519", "token", "token.json",
                ".connection.json"}
SECRET_PATTERNS = ("*.pem", "*.key", "*.p12", "*.pfx", ".env.*", "*_rsa",
                   "*secret*", "*credential*", "*token*")

# Never worth walking into, and expensive when you do.
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
             "Library", ".Trash", ".ssh", ".aws", ".config", ".codex", ".claude"}


def allowed_roots() -> list[pathlib.Path]:
    """Directories the agent may read from.

    STUDIO_FILE_ROOTS overrides, colon separated. Anything that does not
    exist is dropped rather than failing the whole set.
    """
    override = os.environ.get("STUDIO_FILE_ROOTS")
    if override:
        candidates = [pathlib.Path(p).expanduser() for p in override.split(":") if p]
    else:
        home = pathlib.Path.home()
        candidates = [pathlib.Path(__file__).resolve().parent,
                      home / "Downloads", home / "Documents",
                      agent_home()]
    roots = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.append(resolved)
    return roots


def scratch_dir() -> pathlib.Path:
    directory = agent_home() / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_secret(path: pathlib.Path) -> bool:
    name = path.name.lower()
    if name in SECRET_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


def resolve(raw: str, *, must_exist: bool = True) -> pathlib.Path:
    """A path inside the fence, or an error that says why not."""
    if not raw or not isinstance(raw, str):
        raise ArgumentError("path must be a non-empty string")
    try:
        # Resolve first: a symlink out of the fence must be caught by where it
        # points, not by how it is spelled.
        path = pathlib.Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ArgumentError(f"cannot resolve {raw!r}: {exc}") from exc

    roots = allowed_roots()
    if not any(path == root or root in path.parents for root in roots):
        raise ArgumentError(
            f"{path} is outside the readable directories. Allowed: "
            + ", ".join(str(r) for r in roots)
            + ". Set STUDIO_FILE_ROOTS to change this.")
    if is_secret(path):
        raise ArgumentError(
            f"{path.name} looks like a credentials file and is never readable")
    if must_exist and not path.exists():
        raise ArgumentError(f"{path} does not exist")
    return path


def list_dir(raw: str, pattern: str = "*", limit: int = 200,
             recursive: bool = False) -> dict:
    directory = resolve(raw)
    if not directory.is_dir():
        raise ArgumentError(f"{directory} is not a directory")

    entries: list[dict] = []
    truncated = False
    walker = directory.rglob(pattern) if recursive else directory.glob(pattern)
    for item in sorted(walker):
        if len(entries) >= limit:
            truncated = True
            break
        if any(part in SKIP_DIRS or part.startswith(".") for part in
               item.relative_to(directory).parts[:-1]):
            continue
        if item.is_dir():
            if item.name in SKIP_DIRS:
                continue
            entries.append({"name": item.name + "/", "kind": "dir"})
            continue
        if is_secret(item):
            continue          # present but never listed, so it is never asked for
        try:
            size = item.stat().st_size
        except OSError:
            continue
        entries.append({
            "name": str(item.relative_to(directory)),
            "kind": "file",
            "bytes": size,
            "importable": item.suffix.lower() in IMPORTABLE,
        })
    return {"directory": str(directory), "entries": entries,
            "count": len(entries), "truncated": truncated}


def read_text(raw: str, max_bytes: int = MAX_READ_BYTES) -> dict:
    path = resolve(raw)
    if not path.is_file():
        raise ArgumentError(f"{path} is not a file")
    size = path.stat().st_size
    try:
        data = path.read_bytes()[:max_bytes]
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArgumentError(
            f"{path.name} is not text. If it is a drawing, fetch_url or "
            "pdf_to_images can turn a PDF into pictures the vision model reads.")
    return {"path": str(path), "bytes": size, "truncated": size > max_bytes,
            "text": text}


# ---- network ---------------------------------------------------------------

def _refuse_inward(host: str) -> None:
    """Refuse anything that resolves to this machine or a private network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ArgumentError(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast):
            raise ArgumentError(
                f"{host} resolves to {address}, which is this machine or a "
                "private network. Fetching is for public documents only.")


def fetch(url: str, *, timeout: int = FETCH_TIMEOUT,
          max_bytes: int = MAX_DOWNLOAD_BYTES) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ArgumentError("url must be http or https")
    if not parsed.hostname:
        raise ArgumentError("url has no host")
    _refuse_inward(parsed.hostname)

    name = pathlib.Path(urllib.parse.unquote(parsed.path)).name or "download"
    destination = scratch_dir() / name
    request = urllib.request.Request(
        url, headers={"User-Agent": "FusionCadAgent/1.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # A redirect to a private address would defeat the check above,
            # so re-check whatever host actually answered.
            final = urllib.parse.urlparse(response.geturl())
            if final.hostname and final.hostname != parsed.hostname:
                _refuse_inward(final.hostname)
            content_type = response.headers.get("Content-Type", "")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ToolFailure(f"HTTP {exc.code} fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ToolFailure(f"could not fetch {url}: {exc}") from exc

    if len(data) > max_bytes:
        raise ToolFailure(
            f"{url} is larger than {max_bytes // 1024 // 1024} MB")
    if not destination.suffix and "pdf" in content_type:
        destination = destination.with_suffix(".pdf")
    destination.write_bytes(data)
    return {"url": url, "path": str(destination), "bytes": len(data),
            "content_type": content_type}


# Fusion is launched from Finder, so its process inherits a minimal PATH with
# no /opt/homebrew/bin. `shutil.which` therefore reports poppler missing on a
# machine where it is plainly installed. Look in the usual places instead.
POPPLER_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/opt/local/bin")


def find_binary(name: str) -> str | None:
    import shutil

    found = shutil.which(name)
    if found:
        return found
    for directory in POPPLER_DIRS:
        candidate = pathlib.Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def pdf_to_images(raw: str, pages: int = 4, dpi: int = 200) -> dict:
    """Render a PDF to PNGs so the vision model can actually read it.

    A dimensioned vendor drawing is the densest source of truth available for
    a mating part, and it is unreadable as bytes. Rasterising it is what turns
    it into something the loop can use.
    """
    path = resolve(raw)
    if path.suffix.lower() != ".pdf":
        raise ArgumentError("pdf_to_images needs a .pdf file")
    pages = max(1, min(int(pages), MAX_PDF_PAGES))

    binary = find_binary("pdftoppm")
    if not binary:
        raise ToolFailure(
            "pdftoppm was not found in PATH or in "
            + ", ".join(POPPLER_DIRS)
            + "; `brew install poppler` provides it")

    output = scratch_dir() / f"{path.stem}-pages"
    try:
        subprocess.run(
            [binary, "-png", "-r", str(int(dpi)), "-f", "1", "-l", str(pages),
             str(path), str(output)],
            check=True, capture_output=True, timeout=90)
    except FileNotFoundError as exc:
        raise ToolFailure(f"{binary} could not be executed") from exc
    except subprocess.CalledProcessError as exc:
        raise ToolFailure(
            f"pdftoppm failed: {exc.stderr.decode('utf-8', 'replace')[:200]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolFailure("pdftoppm timed out") from exc

    rendered = sorted(output.parent.glob(f"{output.name}-*.png"))
    if not rendered:
        raise ToolFailure("pdftoppm produced no pages")
    return {"pdf": str(path), "pages": len(rendered),
            "images": [str(p) for p in rendered]}


# ---- writing ---------------------------------------------------------------
#
# Reading let an agent diagnose why a tool could not do what was asked. It
# could not then fix it, so it spent eleven minutes looking for another way
# round. Writing closes that loop: if the tool does not exist, make one.
#
# The fence is tighter than for reading. Reading spans Downloads and
# Documents because that is where CAD files live; writing is only ever for
# this repository, because the only reason to write is to change how the
# agent itself works.

REPO = pathlib.Path(__file__).resolve().parent

# Never writable, for reasons that are not about permission:
#   FusionCadAgent.py  the add-in shell. A syntax error here kills the bridge
#                      and only a Fusion restart brings it back -- the agent
#                      would destroy the very channel it needs to fix itself.
#   errors.py          deliberately never hot-reloaded. Rewriting it rebinds
#                      the exception classes and `isinstance` silently stops
#                      matching, so every error becomes an internal error.
UNWRITABLE = {"FusionCadAgent.py", "errors.py", "FusionCadAgent.manifest"}

BACKUP_KEEP = 20


def backup_dir() -> pathlib.Path:
    directory = agent_home() / "source-backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _snapshot(path: pathlib.Path) -> str | None:
    """Keep the previous contents before overwriting.

    This repository has no commits, so a bad write would otherwise be
    unrecoverable. Version control is the better answer; this is the one
    that works without it.
    """
    if not path.exists():
        return None
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = backup_dir() / f"{path.name}.{stamp}.bak"
    copy.write_bytes(path.read_bytes())
    existing = sorted(backup_dir().glob(f"{path.name}.*.bak"))
    for stale in existing[:-BACKUP_KEEP]:
        stale.unlink(missing_ok=True)
    return str(copy)


def resolve_writable(raw: str) -> pathlib.Path:
    if not raw or not isinstance(raw, str):
        raise ArgumentError("path must be a non-empty string")
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO / path
    try:
        path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ArgumentError(f"cannot resolve {raw!r}: {exc}") from exc

    if REPO not in path.parents and path.parent != REPO:
        raise ArgumentError(
            f"writing is limited to {REPO} and its subdirectories; {path} is outside")
    if is_secret(path):
        raise ArgumentError(f"{path.name} looks like a credentials file")
    if path.name in UNWRITABLE:
        raise ArgumentError(
            f"{path.name} is not writable. "
            + ("It is the add-in shell: a mistake here kills the bridge and "
               "needs a Fusion restart to recover."
               if path.name.startswith("FusionCadAgent")
               else "It is never hot-reloaded, so rewriting it breaks "
                    "exception handling everywhere."))
    return path


def write_text(raw: str, content: str) -> dict:
    """Write a file in this repository, refusing anything that will not parse.

    A Python module that does not compile is worse than no change at all:
    every subsequent tool call fails on the import, including the calls the
    agent would use to fix it. Checking first turns that into an error
    message instead of a dead bridge.
    """
    if not isinstance(content, str):
        raise ArgumentError("content must be a string")
    path = resolve_writable(raw)

    if path.suffix == ".py":
        import ast

        try:
            ast.parse(content, filename=path.name)
        except SyntaxError as exc:
            raise ArgumentError(
                f"refusing to write {path.name}: {exc.msg} at line {exc.lineno}. "
                "A module that does not parse would break every following call."
            ) from exc

    backup = _snapshot(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "bytes": len(content.encode("utf-8")),
            "created": backup is None, "backup": backup}


def _clean_env() -> dict:
    """An environment a system Python can actually start in.

    Fusion exports PYTHONHOME and PYTHONPATH pointing at its own embedded
    interpreter. Inherit those and /usr/bin/python3 dies before it runs a
    line -- "failed to get the Python codec of the filesystem encoding" --
    because it is looking for its standard library inside Fusion.app.
    """
    keep = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PATH"] = env.get("PATH", "") + ":" + ":".join(POPPLER_DIRS)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_tests(pattern: str = "test*.py", timeout: int = 180) -> dict:
    """Run the suite. The point of writing is being able to check the write."""
    # NOT sys.executable. Inside Fusion that is Fusion's own embedded
    # interpreter, and spawning it as a subprocess hangs rather than running
    # a test suite -- which locks the main thread the bridge needs. Find a
    # real system Python instead.
    python = find_binary("python3") or find_binary("python")
    if not python:
        raise ToolFailure(
            "no system python3 found to run the tests with; looked in PATH and "
            + ", ".join(POPPLER_DIRS))

    result = subprocess.run(
        [python, "-m", "unittest", "discover", "-s", "tests",
         "-p", pattern, "-v"],
        cwd=str(REPO), capture_output=True, timeout=timeout,
        env=_clean_env(), stdin=subprocess.DEVNULL)
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    tail = output[-6000:]
    passed = result.returncode == 0
    ran = 0
    for line in output.splitlines():
        if line.startswith("Ran ") and " test" in line:
            try:
                ran = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return {"passed": passed, "tests": ran,
            "failures": [l for l in output.splitlines()
                         if l.startswith(("FAIL:", "ERROR:"))][:20],
            "output": tail}
