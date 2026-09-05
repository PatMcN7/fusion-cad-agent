"""Timeline checkpoints, so a bad operation costs one operation.

The failure mode this exists to prevent: an agent models for ten minutes on
top of a mate whose primary axis was flipped in the first thirty seconds. If
every operation is checkpointed and verified, the agent cannot get one step
past a silent error, let alone ten minutes.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from assertions import CheckSuite


class Checkpoint:
    """A restore point at a timeline position."""

    def __init__(self, design: Any, label: str) -> None:
        self.design = design
        self.label = label
        self.timeline_count = design.timeline.count
        self.rolled_back: list[str] = []

    def rollback(self) -> list[str]:
        """Delete every timeline entry created since this checkpoint.

        Walks backwards, because a later feature depends on earlier ones. Stops
        at the first entry that refuses to delete rather than thrashing, and
        reports how far it got -- a partial rollback the caller knows about
        beats a silent one it does not.
        """
        timeline = self.design.timeline
        removed: list[str] = []
        while timeline.count > self.timeline_count:
            item = timeline.item(timeline.count - 1)
            try:
                name = item.name
            except Exception:
                name = f"entry {timeline.count - 1}"
            try:
                if not item.deleteMe():
                    break
            except Exception:
                break
            removed.append(name)
        self.rolled_back = removed
        return removed

    @property
    def clean(self) -> bool:
        return self.design.timeline.count == self.timeline_count


@contextlib.contextmanager
def operation(design: Any, label: str) -> Iterator[tuple[Checkpoint, CheckSuite]]:
    """Run one verified operation.

    Yields a checkpoint and an empty suite. Add checks to the suite inside the
    block; on exit the suite is required. If it fails -- or the block raises --
    the timeline is rewound to where it started and the error propagates with
    the rollback recorded on it.
    """
    checkpoint = Checkpoint(design, label)
    suite = CheckSuite(label)
    try:
        yield checkpoint, suite
        suite.require()
    except Exception as exc:
        removed = checkpoint.rollback()
        setattr(exc, "rolled_back", removed)
        setattr(exc, "checkpoint_label", label)
        setattr(exc, "check_report", suite.report())
        raise
