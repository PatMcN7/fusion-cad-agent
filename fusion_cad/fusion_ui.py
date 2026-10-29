"""Native Fusion palette lifecycle. Geometry tools do not own this module's state.

Only the readiness worker uses a thread; every UI operation runs on Fusion's
main thread. Closing the palette keeps the chat server and its sessions alive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request

import adsk.core

from fusion_cad.common import agent_home

ROOT = Path(__file__).resolve().parent.parent
COMMAND = "FusionCadAgentShowStudio"
PALETTE = "FusionCadAgentStudio"
EVENT = "FusionCadAgentStudioReady"
_app = _ui = _event = None
_handlers = []
_worker = None
_cancel = threading.Event()


def studio_url():
    # Match server.py's environment precedence without importing its providers.
    port = os.environ.get("STUDIO_PORT")
    if not port:
        env_file = ROOT / "studio" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "STUDIO_PORT":
                    port = value.strip().strip("\"'")
    return f"http://127.0.0.1:{int(port or 8765)}/?embedded=1"


def _running(url):
    # A root GET is cheap and works with Studio versions before the palette.
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return b"<title>Fusion Studio</title>" in response.read(2048)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Another service is using Studio's port.") from exc
    except (urllib.error.URLError, TimeoutError):
        return False


def _ready_worker(app, cancelled):
    try:
        url = studio_url()
        if not _running(url):
            python = ROOT / "studio" / ".venv" / "bin" / "python"
            if not python.exists():
                raise RuntimeError("Run studio/run.sh once to install Studio, then reopen this panel.")
            env = dict(os.environ)
            # Fusion's embedded Python environment must not leak into the venv.
            for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
                env.pop(key, None)
            with (agent_home() / "studio.log").open("ab") as output:
                process = subprocess.Popen(
                    [str(python), str(ROOT / "studio" / "server.py")],
                    cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                )
            deadline = time.monotonic() + 25
            while not cancelled.is_set() and not _running(url):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Studio could not start. See FusionCadAgent/studio.log in Application Support.")
                cancelled.wait(0.25)
        message = {"url": url}
    except Exception as exc:
        message = {"error": str(exc)}
    if not cancelled.is_set():
        app.fireCustomEvent(EVENT, json.dumps(message))


class ReadyHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        palette = _ui.palettes.itemById(PALETTE) if _ui else None
        if not palette:
            return
        result = json.loads(args.additionalInfo)
        if result.get("url"):
            palette.htmlFileURL = result["url"]
        else:
            _ui.messageBox(result.get("error", "Studio could not start."), "Fusion Studio")


def show():
    global _worker
    palette = _ui.palettes.itemById(PALETTE)
    if not palette:
        palette = _ui.palettes.add(
            PALETTE, "Fusion Studio", str(ROOT / "studio" / "static" / "connecting.html"),
            True, True, True, 480, 740, True,
        )
        palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
    else:
        palette.isVisible = True
    if not _worker or not _worker.is_alive():
        _worker = threading.Thread(target=_ready_worker, args=(_app, _cancel), daemon=True)
        _worker.start()


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            show()
        except Exception as exc:
            _ui.messageBox(str(exc), "Fusion Studio")


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        args.command.isExecutedWhenPreEmpted = False
        args.command.execute.add(_execute_handler)


_execute_handler = ExecuteHandler()


def install(app, ui):
    """Idempotent across geometry hot reloads; creates no document or geometry."""
    global _app, _ui, _event, _cancel
    if _ui is ui and _event:
        return
    _app, _ui = app, ui
    _cancel = threading.Event()
    try:
        _event = app.registerCustomEvent(EVENT)
        ready = ReadyHandler()
        _event.add(ready)
        definition = ui.commandDefinitions.itemById(COMMAND)
        if not definition:
            definition = ui.commandDefinitions.addButtonDefinition(
                COMMAND, "Fusion Studio", "Chat, capture the viewport, or sketch a design idea.", "")
        created = CreatedHandler()
        definition.commandCreated.add(created)
        _handlers.extend([ready, created, _execute_handler])
        panel = ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
        if panel and not panel.controls.itemById(COMMAND):
            control = panel.controls.addCommand(definition)
            control.isPromoted = True
            control.isPromotedByDefault = True
    except Exception:
        stop()
        raise


def stop():
    global _app, _ui, _event
    _cancel.set()
    if _ui:
        palette = _ui.palettes.itemById(PALETTE)
        if palette:
            palette.deleteMe()
        panel = _ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
        control = panel.controls.itemById(COMMAND) if panel else None
        if control:
            control.deleteMe()
        definition = _ui.commandDefinitions.itemById(COMMAND)
        if definition:
            definition.deleteMe()
    if _app and _event:
        _app.unregisterCustomEvent(EVENT)
    _handlers.clear()
    _app = _ui = _event = None
