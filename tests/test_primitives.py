"""Tests for the composable layer's pure logic.

pick and ops import adsk lazily, so everything that does not touch live
geometry is testable without Fusion.
"""

import unittest

import fasteners
import mechanics
import ops
import patterns
import pick
from errors import PatternError, SelectionError


class DirectionParsing(unittest.TestCase):
    def test_signed_and_unsigned(self):
        self.assertEqual(pick.parse_direction("+z"), (2, 1))
        self.assertEqual(pick.parse_direction("-y"), (1, -1))
        self.assertEqual(pick.parse_direction("x"), (0, 0))

    def test_rejects_nonsense(self):
        for bad in ("", "q", "+w", None):
            with self.assertRaises(SelectionError):
                pick.parse_direction(bad)


class ThreadDesignation(unittest.TestCase):
    """The bug that shipped: M3.5x0.6 recommended for a 0.125 in hole."""

    def test_numbered_inch(self):
        self.assertAlmostEqual(
            fasteners.designation_major_diameter_cm("5-40 UNC"), 0.125 * 2.54, places=6)
        self.assertAlmostEqual(
            fasteners.designation_major_diameter_cm("10-32 UNF"), 0.190 * 2.54, places=6)

    def test_metric_is_distinguishable(self):
        metric = fasteners.designation_major_diameter_cm("M3.5x0.6")
        self.assertAlmostEqual(metric, 0.35, places=6)
        self.assertGreater(abs(metric - 0.125 * 2.54), 1e-4)

    def test_fractional_inch(self):
        self.assertAlmostEqual(
            fasteners.designation_major_diameter_cm("1/4-20 UNC"), 0.25 * 2.54, places=6)

    def test_unparseable_returns_none(self):
        self.assertIsNone(fasteners.designation_major_diameter_cm("G 1/16-28"))


class LinearPositions(unittest.TestCase):
    def test_density_and_centring(self):
        positions = patterns.linear_positions(25 * 2.54, 3)
        self.assertEqual(len(positions), 75)
        self.assertAlmostEqual(positions[0], -positions[-1], places=9)

    def test_sparse_density(self):
        self.assertEqual(len(patterns.linear_positions(25 * 2.54, 1)), 25)

    def test_zero_density_disables_that_face_pattern(self):
        self.assertEqual(patterns.linear_positions(25 * 2.54, 0), [])

    def test_negative_density_is_rejected(self):
        with self.assertRaises(PatternError):
            patterns.linear_positions(25 * 2.54, -1)

    def test_too_sparse_raises(self):
        with self.assertRaises(PatternError):
            patterns.linear_positions(0.5 * 2.54, 1)


class PlateRules(unittest.TestCase):
    def test_named_unsupported_overhang_argument(self):
        rules = mechanics.plate_rules(30.0, 0.0625, unsupported_overhang=1.0)
        self.assertEqual([rule.name for rule in rules], [
            "plate_span_ratio", "cantilever_ratio"
        ])


class NoStdlibShadowing(unittest.TestCase):
    """pick.py is named that way because select.py shadows the stdlib module
    http.server depends on, which would break the bridge's own socket."""

    def test_module_names_are_safe(self):
        import os, sysconfig
        stdlib_dir = sysconfig.get_paths()["stdlib"]
        std = {f[:-3] for f in os.listdir(stdlib_dir) if f.endswith(".py")}
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mine = {f[:-3] for f in os.listdir(here) if f.endswith(".py")}
        self.assertEqual(mine & std, set())


class SelectorHonesty(unittest.TestCase):
    """normal_along() used to accept a sign and silently ignore it, so
    '+y' and '-y' returned the same faces."""

    def test_normal_axis_rejects_a_sign(self):
        selection = pick.Selection([], "face", "test")
        with self.assertRaises(SelectionError) as caught:
            selection.normal_axis("+y")
        self.assertIn("facing", str(caught.exception))

    def test_normal_axis_accepts_bare_axis(self):
        selection = pick.Selection([], "face", "test")
        self.assertEqual(len(selection.normal_axis("y")), 0)

    def test_facing_requires_a_sign(self):
        selection = pick.Selection([], "face", "test")
        with self.assertRaises(SelectionError):
            selection.facing("y")

    def test_extreme_requires_a_sign(self):
        selection = pick.Selection([], "face", "test")
        with self.assertRaises(SelectionError):
            selection.extreme("z")


class ErrorIdentity(unittest.TestCase):
    """Exceptions live in a module the reloader never touches, so the shell
    can catch one base class instead of comparing class names as strings."""

    def test_all_errors_share_a_base_and_carry_a_code(self):
        import errors
        for name in ("ProtocolError", "ArgumentError", "PatternError",
                     "SelectionError", "OpError", "ToolFailure", "CheckFailed"):
            cls = getattr(errors, name)
            self.assertTrue(issubclass(cls, errors.BridgeError), name)
            self.assertIsInstance(cls.code, str)

    def test_errors_is_not_hot_reloaded(self):
        import re
        shell = open("FusionCadAgent.py").read()
        reloadable = re.search(r"RELOADABLE = \(([^)]*)\)", shell, re.S).group(1)
        self.assertNotIn('"errors"', reloadable)


if __name__ == "__main__":
    unittest.main()
