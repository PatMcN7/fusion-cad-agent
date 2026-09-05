"""Failures must report what was recovered without masking the original error."""

import unittest
from types import SimpleNamespace

from checkpoints import Checkpoint, operation, tool_scope


class Timeline:
    def __init__(self):
        self.items = []

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]

    def add(self, name, *, fails=False, stuck=False):
        def delete():
            if fails:
                raise RuntimeError("kernel refused deletion")
            if not stuck:
                self.items.pop()
            return True
        self.items.append(SimpleNamespace(name=name, deleteMe=delete))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.timeline = Timeline()
        self.timeline.add("original")
        self.design = SimpleNamespace(timeline=self.timeline)

    def test_cleanup_does_not_claim_geometry_was_restored(self):
        cp = Checkpoint(self.design, "cut")
        self.timeline.add("sketch")
        self.timeline.add("cut")
        self.assertEqual(cp.rollback(), ["cut", "sketch"])
        self.assertTrue(cp.clean)
        self.assertEqual(cp.recovery["status"], "partial")
        self.assertFalse(cp.recovery["restoration_verified"])

    def test_restored_requires_independent_evidence(self):
        for verified in (True, False):
            cp = Checkpoint(self.design, "edit", verify_restored=lambda: verified)
            self.timeline.add("edit")
            cp.rollback()
            self.assertEqual(cp.recovery["status"], "restored" if verified else "partial")

    def test_deletion_is_not_recovered_by_matching_counts(self):
        cp = Checkpoint(self.design, "destructive")
        self.timeline.items.pop()
        self.timeline.add("replacement")
        cp.rollback()
        self.assertNotEqual(cp.recovery["status"], "restored")

    def test_removed_preexisting_entry_is_reported(self):
        cp = Checkpoint(self.design, "delete")
        self.timeline.items.pop()
        cp.rollback()
        self.assertEqual(cp.recovery["status"], "failed")

    def test_cleanup_failure_preserves_original_exception(self):
        original = ValueError("bad cut")
        with self.assertRaises(ValueError) as caught:
            with operation(self.design, "cut"):
                self.timeline.add("bad", fails=True)
                raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(original.recovery["status"], "failed")
        self.assertIn("kernel refused", original.recovery["reason"])

    def test_partial_cleanup_stops_at_first_failure(self):
        cp = Checkpoint(self.design, "cut")
        self.timeline.add("bad", fails=True)
        self.timeline.add("good")
        self.assertEqual(cp.rollback(), ["good"])
        self.assertEqual(cp.recovery["status"], "partial")
        self.assertEqual(self.timeline.count, 2)

    def test_successful_delete_without_progress_does_not_loop(self):
        cp = Checkpoint(self.design, "cut")
        self.timeline.add("stuck", stuck=True)
        cp.rollback()
        self.assertIn("did not reduce", cp.recovery["reason"])

    def test_unsupported_recovery_does_not_delete_anything(self):
        cp = Checkpoint(self.design, "delete", cleanup_supported=False)
        self.timeline.add("unrelated")
        self.assertEqual(cp.rollback(), [])
        self.assertEqual(self.timeline.count, 2)
        self.assertEqual(cp.recovery["status"], "not_attempted")

    def test_tool_reports_earlier_successful_stages(self):
        with self.assertRaises(ValueError) as caught:
            with tool_scope("pan"):
                with operation(self.design, "plate"):
                    self.timeline.add("plate")
                with operation(self.design, "pockets"):
                    self.timeline.add("pockets")
                    raise ValueError("bad pockets")
        report = caught.exception.recovery
        self.assertEqual(report["completed_stages"], ["plate"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.timeline.count, 2)

    def test_scope_does_not_leak_to_next_tool(self):
        with tool_scope("first"):
            with operation(self.design, "first stage"):
                pass
        with self.assertRaises(ValueError) as caught:
            with tool_scope("next"):
                raise ValueError("no checkpoint")
        self.assertEqual(caught.exception.recovery["completed_stages"], [])
        self.assertEqual(caught.exception.recovery["status"], "not_attempted")
