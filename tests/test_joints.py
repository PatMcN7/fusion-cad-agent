"""The flipped mate.

The error this project opened with: a motor mated to a gearbox whose primary
axis pointed the wrong way, looking correct in the tree, discovered ten
minutes later. The claim is that it is one number -- so these tests pin the
arithmetic that turns that number into a decision.
"""

import unittest

import assertions

from test_registry import _install_adsk_stub

_install_adsk_stub()

import ops  # noqa: E402
import tools  # noqa: E402


class ResolveFlip(unittest.TestCase):
    """before -> requested -> flipped, and what to keep."""

    def test_requested_direction_is_clean_so_keep_it(self):
        self.assertEqual(assertions.resolve_flip(0.0, 0.0, 5.0), "keep")

    def test_a_tight_but_legitimate_fit_is_not_flipped(self):
        # Overlap that was already there before the joint is not this
        # joint's fault, and flipping for it would break a good assembly.
        self.assertEqual(assertions.resolve_flip(2.0, 2.0, 0.0), "keep")

    def test_backwards_mate_is_flipped(self):
        # The headline case: requested drove the parts through each other,
        # the opposite direction is clean.
        self.assertEqual(assertions.resolve_flip(0.0, 12.5, 0.0), "flip")

    def test_flip_is_taken_when_strictly_better(self):
        self.assertEqual(assertions.resolve_flip(0.0, 12.5, 3.0), "flip")

    def test_two_equally_bad_directions_report_failure(self):
        # Both wrong means the targets are wrong, not the flag. Silently
        # picking one would hide a real mistake.
        self.assertEqual(assertions.resolve_flip(0.0, 9.0, 9.0), "neither")

    def test_flipping_something_worse_is_refused(self):
        self.assertEqual(assertions.resolve_flip(0.0, 4.0, 8.0), "neither")

    def test_floating_point_noise_is_not_a_flip(self):
        self.assertEqual(assertions.resolve_flip(1.0, 1.0 + 1e-12, 0.0), "keep")


class InterferenceCheck(unittest.TestCase):
    def test_new_overlap_fails_and_names_the_amount(self):
        check = assertions.interference_not_worsened("x", 0.0, 4.25)
        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "error")
        self.assertIn("4.25", check.actual)

    def test_pre_existing_overlap_is_not_this_operations_fault(self):
        self.assertTrue(assertions.interference_not_worsened("x", 3.0, 3.0).ok)

    def test_reducing_overlap_passes(self):
        self.assertTrue(assertions.interference_not_worsened("x", 3.0, 1.0).ok)


class OccurrenceMoved(unittest.TestCase):
    def test_a_joint_that_moved_nothing_warns(self):
        check = assertions.occurrence_moved("m", (0, 0, 0), (0, 0, 0))
        self.assertFalse(check.ok)
        # A warning, not an error: mating two already-coincident faces is
        # legitimate and must not roll the joint back.
        self.assertEqual(check.severity, "warning")

    def test_movement_passes(self):
        self.assertTrue(assertions.occurrence_moved("m", (0, 0, 0), (0, 0, 5)).ok)


class JointRegistry(unittest.TestCase):
    def test_both_tools_are_registered(self):
        for name in ("create_joint", "list_joint_candidates"):
            self.assertIn(name, tools.TOOLS)
            self.assertIn(name, tools.HANDLERS)

    def test_only_the_mutating_one_is_mutating(self):
        self.assertTrue(tools.TOOLS["create_joint"]["mutating"])
        self.assertFalse(tools.TOOLS["list_joint_candidates"]["mutating"])

    def test_create_joint_echoes_design_state(self):
        self.assertTrue(
            tools.HANDLERS["create_joint"].__qualname__.startswith("_echo_state"))

    def test_face_queries_are_structured_schemas_not_prose(self):
        """Nested arguments cannot be described well enough in English.

        The polygon lesson: a model given prose for a structured argument
        gets it wrong the first time and burns a round trip.
        """
        for field in ("move_face", "to_face"):
            schema = tools.TOOLS["create_joint"]["arguments"][field]
            self.assertIsInstance(schema, dict)
            self.assertEqual(schema["type"], "object")
            self.assertIn("kind", schema["properties"])

    def test_auto_flip_defaults_on(self):
        # If it defaulted off, the flipped mate would still ship silently.
        self.assertIn("Default true",
                      tools.TOOLS["create_joint"]["arguments"]["auto_flip"])

    def test_motion_vocabulary_matches_the_ops_layer(self):
        described = tools.TOOLS["create_joint"]["arguments"]["motion"]
        for motion in ops.JOINT_MOTIONS:
            self.assertIn(motion, described)


if __name__ == "__main__":
    unittest.main()
