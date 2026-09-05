"""Durable operation identities and outcomes; no Fusion API access.

This module is owned by the add-in shell and is deliberately not hot-reloaded.
A request is recorded before dispatch. A restart marks unfinished requests as
unknown, never as safe to replay. Payloads are hashed, not stored: a generated
recipe or tool argument should not become a second copy of private source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from errors import ProtocolError

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TERMINAL = {"succeeded", "failed", "outcome_unknown"}


def operation_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProtocolError("operation_id must contain 1-128 letters, digits, underscores or hyphens")
    return value


def fingerprint(payload):
    # Correlation/request IDs may change on a transport retry. Everything that
    # can affect the requested mutation, including future binding fields, stays.
    identity = {key: value for key, value in payload.items()
                if key not in {"request_id", "operation_id"}}
    try:
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Operation payload must be finite JSON") from exc
    return hashlib.sha256(canonical.encode()).hexdigest()


class Journal:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        # Only the owning bridge initializes this directory. Status readers use
        # that same instance rather than opening a second journal owner.
        for path in self.directory.glob("*.json"):
            record = self._read_path(path)
            if record["state"] in {"queued", "running"}:
                record.update(state="outcome_unknown", updated=time.time(),
                              reason="Bridge restarted before the outcome was recorded")
                self._write(record)

    def _path(self, identifier):
        return self.directory / (operation_id(identifier) + ".json")

    def _read_path(self, path):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != 1 or
                record.get("operation_id") != path.stem or
                record.get("state") not in TERMINAL | {"queued", "running"} or
                not isinstance(record.get("fingerprint"), str)):
            raise RuntimeError(f"Invalid operation journal record: {path.name}")
        return record

    def _write(self, record):
        path = self._path(record["operation_id"])
        temporary = path.with_suffix(".tmp")
        data = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def get(self, identifier):
        with self._lock:
            path = self._path(identifier)
            return self._read_path(path) if path.exists() else None

    def submit(self, payload):
        identifier = operation_id(payload.get("operation_id"))
        digest = fingerprint(payload)
        with self._lock:
            existing = self.get(identifier)
            if existing is not None:
                if existing["fingerprint"] != digest:
                    raise ProtocolError("operation_id already belongs to a different payload")
                return existing, False
            now = time.time()
            record = {"version": 1, "operation_id": identifier, "fingerprint": digest,
                      "tool": payload.get("tool"), "document_id": payload.get("document_id"),
                      "state": "queued",
                      "created": now, "updated": now}
            self._write(record)
            return record, True

    def start(self, identifier):
        with self._lock:
            record = self.get(identifier)
            if record is None or record["state"] != "queued":
                return False
            record.update(state="running", updated=time.time())
            self._write(record)
            return True

    def finish(self, identifier, response):
        with self._lock:
            record = self.get(identifier)
            if record is None or record["state"] != "running":
                raise RuntimeError("Only a running operation can record a result")
            record.update(state="succeeded" if response.get("ok") else "failed",
                          response=response, updated=time.time())
            self._write(record)
            return record

    def unknown(self, identifier, reason):
        with self._lock:
            record = self.get(identifier)
            if record is not None and record["state"] not in TERMINAL:
                record.update(state="outcome_unknown", reason=reason, updated=time.time())
                self._write(record)
            return record

    def cancel(self, identifier):
        with self._lock:
            record = self.get(identifier)
            if record is None or record["state"] != "queued":
                return False
            response = {"ok": False, "operation_id": identifier,
                        "error": {"code": "cancelled", "message": "Cancelled before execution"}}
            record.update(state="failed", response=response, updated=time.time())
            self._write(record)
            return True


def status_response(record):
    if record is None:
        return {"ok": False, "error": {"code": "not_found", "message": "Unknown operation_id"}}
    # Internal payload hashes are not needed by clients.
    return {"ok": True, "operation": {key: value for key, value in record.items()
                                      if key not in {"fingerprint", "version"}}}


def pending_response(record):
    if record.get("response") is not None:
        return record["response"]
    identifier, state = record["operation_id"], record["state"]
    return {"ok": False, "operation_id": identifier,
            "error": {"code": "operation_pending" if state in {"queued", "running"}
                      else "outcome_unknown", "operation_id": identifier, "state": state,
                      "message": "Poll this operation's status; do not submit a replacement mutation",
                      "status_path": f"/v1/operations/{identifier}"}}


class DocumentBindings:
    """Opaque identities for open documents during one add-in lifetime.

    Names are not addresses, and unsaved documents have no cloud file identity.
    This intentionally issues new IDs after a close/reopen or add-in restart.
    Called only on Fusion's main thread. It is not a document revision counter.
    """

    def __init__(self):
        import uuid
        self.session = uuid.uuid4().hex
        self.documents = []

    def identify(self, document):
        if document is None:
            return self.session + "_none"
        valid = []
        found = None
        for existing, identifier in self.documents:
            try:
                if not getattr(existing, "isValid", True):
                    continue
                valid.append((existing, identifier))
                if existing == document:
                    found = identifier
            except Exception:
                continue
        self.documents = valid
        if found is not None:
            return found
        import uuid
        identifier = self.session + "_" + uuid.uuid4().hex
        self.documents.append((document, identifier))
        return identifier

    def require(self, document, expected):
        actual = self.identify(document)
        if expected != actual:
            raise ProtocolError(
                "Active document changed before execution. Read design_summary or "
                "active_document and reconcile the target before issuing a new operation.",
                expected_document_id=expected, actual_document_id=actual)
        return actual
