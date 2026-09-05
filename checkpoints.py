"""Best-effort cleanup of appended timeline entries, with explicit evidence.

Deleting new features cannot restore deleted preexisting features or arbitrary
property edits. Full restoration requires an independent verifier. Successful
earlier stages of a tool are not rolled back by a later stage's checkpoint.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from assertions import CheckSuite

_completed: ContextVar[list[str] | None] = ContextVar("completed_checkpoints", default=None)


class Checkpoint:
    """A timeline cleanup boundary, not a general design transaction."""

    def __init__(self, design: Any, label: str, *,
                 verify_restored: Callable[[], bool] | None = None,
                 cleanup_supported: bool = True) -> None:
        self.design = design
        self.label = label
        self.timeline_count = design.timeline.count
        self.verify_restored = verify_restored
        self.cleanup_supported = cleanup_supported
        self.rolled_back: list[str] = []
        self.recovery: dict[str, Any] = {"status": "not_attempted", "scope": "stage"}

    def rollback(self) -> list[str]:
        """Stop on cleanup failure; preserve the original operation exception."""
        removed: list[str] = []
        report = {"status": "not_attempted", "scope": "stage", "label": self.label,
                  "strategy": "delete_appended_timeline_entries",
                  "timeline_before": self.timeline_count, "removed": removed,
                  "restoration_verified": False}
        self.recovery = report
        if not self.cleanup_supported:
            report["reason"] = "Timeline cleanup cannot restore this kind of mutation"
            return removed
        try:
            timeline = self.design.timeline
            while timeline.count > self.timeline_count:
                previous = timeline.count
                item = timeline.item(previous - 1)
                try:
                    name = item.name
                except Exception:
                    name = f"entry {previous - 1}"
                # TimelineObject is a wrapper; deletion belongs to its native
                # sketch/feature. Groups and unsupported entities need a
                # different recovery strategy, so stop rather than guessing.
                entity = item.entity
                delete = getattr(entity, "deleteMe", None)
                if not callable(delete):
                    raise RuntimeError(f"No supported entity deletion for {name}")
                if not delete():
                    raise RuntimeError(f"Could not delete {name}")
                removed.append(name)
                if timeline.count >= previous:
                    raise RuntimeError(f"Deleting {name} did not reduce timeline depth")
            report["timeline_after"] = timeline.count
            report["timeline_cleanup_complete"] = timeline.count == self.timeline_count
            if not report["timeline_cleanup_complete"]:
                raise RuntimeError("Preexisting timeline entries were removed")
            if self.verify_restored is not None:
                report["restoration_verified"] = self.verify_restored() is True
            report["status"] = "restored" if report["restoration_verified"] else "partial"
            if not report["restoration_verified"]:
                report["reason"] = ("Timeline cleanup completed; prior geometry and "
                                    "properties have not been verified as restored")
        except Exception as exc:
            report["status"] = "partial" if removed else "failed"
            report["reason"] = f"{type(exc).__name__}: {exc}"
        self.rolled_back = removed
        return removed

    @property
    def clean(self) -> bool:
        """Compatibility: timeline depth matches; does not prove restoration."""
        return self.design.timeline.count == self.timeline_count


@contextlib.contextmanager
def tool_scope(label: str):
    """Attach whole-tool context when a stage or uncheckpointed action fails."""
    completed: list[str] = []
    token = _completed.set(completed)
    try:
        yield
    except Exception as exc:
        stage = getattr(exc, "recovery", None)
        report = {"scope": "tool", "tool": label, "completed_stages": completed,
                  "status": stage["status"] if stage else "not_attempted",
                  "restoration_verified": False}
        if stage:
            report["stage"] = stage
        if completed:
            report["status"] = "partial"
            report["reason"] = "Earlier completed stages remain in the document"
        else:
            # A verified stage cannot prove uncheckpointed work was restored.
            if report["status"] == "restored":
                report["status"] = "partial"
            report["reason"] = "Full tool state has not been verified as restored"
        setattr(exc, "recovery", report)
        raise
    finally:
        _completed.reset(token)


@contextlib.contextmanager
def operation(design: Any, label: str, *,
              verify_restored: Callable[[], bool] | None = None,
              cleanup_supported: bool = True) -> Iterator[tuple[Checkpoint, CheckSuite]]:
    checkpoint = Checkpoint(design, label, verify_restored=verify_restored,
                            cleanup_supported=cleanup_supported)
    suite = CheckSuite(label)
    try:
        yield checkpoint, suite
        suite.require()
    except Exception as exc:
        removed = checkpoint.rollback()
        setattr(exc, "rolled_back", removed)
        setattr(exc, "checkpoint_label", label)
        setattr(exc, "check_report", suite.report())
        setattr(exc, "recovery", checkpoint.recovery)
        raise
    else:
        completed = _completed.get()
        if completed is not None:
            completed.append(label)
