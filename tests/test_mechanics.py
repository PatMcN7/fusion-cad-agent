"""Tests for the closed-form mechanics layer.

Closed-form results are checked against hand-worked values and against each
other. The validity-domain tests matter most: a beam formula applied outside
its assumptions gives a confident wrong number, which is the vacuous-assertion
trap in different clothes.
"""

import math
import unittest

import mechanics as mech


class SectionProperties(unittest.TestCase):
    def test_solid_rectangle_matches_bh3_over_12(self):
        section = mech.rect(1.0, 2.0)
        self.assertAlmostEqual(section.area, 2.0)
        self.assertAlmostEqual(section.Ix, 1.0 * 8.0 / 12, places=9)
        self.assertAlmostEqual(section.Iy, 2.0 * 1.0 / 12, places=9)

    def test_tube_is_hollow_rectangle(self):
        tube = mech.rect_tube(1.0, 2.0, 0.125)
        solid = mech.rect(1.0, 2.0)
        inner = mech.rect(0.75, 1.75)
        self.assertAlmostEqual(tube.area, solid.area - inner.area, places=9)
        self.assertAlmostEqual(tube.Ix, solid.Ix - inner.Ix, places=9)

    def test_tube_beats_solid_on_stiffness_per_mass(self):
        tube = mech.rect_tube(1.0, 2.0, 0.125)
        solid = mech.rect(1.0, 2.0)
        self.assertGreater(tube.Ix / tube.area, solid.Ix / solid.area)

    def test_section_modulus_and_gyration(self):
        section = mech.rect(1.0, 2.0)
        self.assertAlmostEqual(section.Sx, section.Ix / 1.0, places=9)
        self.assertAlmostEqual(section.rx, math.sqrt(section.Ix / section.area), places=9)

    def test_round_tube_thin_wall_approximation(self):
        # For a thin wall, I ~= pi * r^3 * t
        tube = mech.round_tube(2.0, 0.05)
        approx = math.pi * (0.975 ** 3) * 0.05
        self.assertAlmostEqual(tube.Ix, approx, delta=approx * 0.05)

    def test_wall_consuming_section_raises(self):
        with self.assertRaises(ValueError):
            mech.rect_tube(1.0, 2.0, 0.5)


class BeamCases(unittest.TestCase):
    def setUp(self):
        self.mat = mech.material("6061-T6")
        self.section = mech.rect_tube(1.0, 2.0, 0.125)

    def test_simply_supported_matches_hand_calculation(self):
        result = mech.simply_supported_point(50.0, 25.0, self.mat, self.section)
        expected = 50.0 * 25.0**3 / (48 * self.mat.E * self.section.Ix)
        self.assertAlmostEqual(result.deflection, expected, places=9)
        self.assertAlmostEqual(result.max_moment, 50.0 * 25.0 / 4, places=9)

    def test_fixed_ends_are_four_times_stiffer_than_pinned(self):
        pinned = mech.simply_supported_point(50.0, 25.0, self.mat, self.section)
        fixed = mech.fixed_fixed_point(50.0, 25.0, self.mat, self.section)
        self.assertAlmostEqual(pinned.deflection / fixed.deflection, 4.0, places=6)

    def test_cantilever_is_softer_than_simply_supported(self):
        cantilever = mech.cantilever_point(50.0, 25.0, self.mat, self.section)
        pinned = mech.simply_supported_point(50.0, 25.0, self.mat, self.section)
        self.assertGreater(cantilever.deflection, pinned.deflection)

    def test_bending_stress_is_moment_over_section_modulus(self):
        result = mech.simply_supported_point(50.0, 25.0, self.mat, self.section)
        self.assertAlmostEqual(
            result.bending_stress, result.max_moment / self.section.Sx, places=6)


class ValidityDomains(unittest.TestCase):
    """The part that keeps the layer honest."""

    def setUp(self):
        self.mat = mech.material("6061-T6")

    def test_short_deep_beam_is_flagged(self):
        stubby = mech.rect_tube(1.0, 2.0, 0.125)
        result = mech.simply_supported_point(50.0, 8.0, self.mat, stubby)
        self.assertTrue(result.notes, "span/depth 4 must be flagged")
        self.assertIn("shear", result.notes[0])

    def test_slender_beam_is_not_flagged(self):
        section = mech.rect_tube(1.0, 2.0, 0.125)
        result = mech.simply_supported_point(50.0, 40.0, self.mat, section)
        self.assertEqual(result.notes, [])

    def test_short_column_flags_yield_not_euler(self):
        section = mech.rect_tube(1.0, 2.0, 0.125)
        stubby = mech.euler_buckling(section, 4.0, self.mat)
        self.assertTrue(stubby["notes"])
        self.assertIn("yield governs", stubby["notes"][0])

    def test_long_column_uses_euler_cleanly(self):
        section = mech.rect_tube(1.0, 2.0, 0.125)
        slender = mech.euler_buckling(section, 120.0, self.mat)
        self.assertEqual(slender["notes"], [])

    def test_large_plate_deflection_is_flagged(self):
        floppy = mech.flat_plate_udl_simply_supported(
            5.0, 30.0, 30.0, 0.060, self.mat)
        self.assertTrue(floppy["notes"])
        self.assertIn("small-deflection", floppy["notes"][0])


class Rules(unittest.TestCase):
    def test_edge_distance_violation_is_detected(self):
        # What the built frame actually has: 0.167 in margin on a 0.125 hole.
        rules = mech.hole_rules(0.125, 0.167, 1 / 3, 0.125)
        edge = next(r for r in rules if r.name == "hole_edge_distance")
        self.assertFalse(edge.ok)
        self.assertAlmostEqual(edge.value, 1.336, places=2)

    def test_generous_edge_distance_passes(self):
        rules = mech.hole_rules(0.125, 0.375, 0.5, 0.125)
        self.assertTrue(next(r for r in rules if r.name == "hole_edge_distance").ok)

    def test_thread_engagement_depends_on_material(self):
        into_aluminium = mech.thread_rules(0.125, 0.125, "6061-T6")[0]
        into_steel = mech.thread_rules(0.125, 0.125, "1018")[0]
        self.assertFalse(into_aluminium.ok)
        self.assertTrue(into_steel.ok)

    def test_skeletonised_rib_aspect_is_flagged(self):
        rules = mech.machining_rules(2.0, 0.08, 0.025)
        self.assertFalse(rules[0].ok)
        self.assertAlmostEqual(rules[0].value, 25.0, places=6)


class StressConcentration(unittest.TestCase):
    def test_small_hole_in_wide_plate_approaches_three(self):
        self.assertAlmostEqual(mech.kt_hole_in_plate(0.01, 100.0), 3.0, places=2)

    def test_kt_falls_as_the_hole_fills_the_plate(self):
        wide = mech.kt_hole_in_plate(0.125, 4.0)
        narrow = mech.kt_hole_in_plate(0.125, 0.5)
        self.assertLess(narrow, wide)


class ReportShape(unittest.TestCase):
    def test_report_carries_numbers_not_just_verdicts(self):
        out = mech.report(
            mech.rect_tube(1.0, 2.0, 0.125), mech.material("6061-T6"),
            span=25.0, load=50.0,
            rules=mech.hole_rules(0.125, 0.167, 1 / 3, 0.125))
        self.assertIn("Ix_in4", out["section"])
        self.assertIn("deflection_in", out["beam"])
        self.assertGreater(len(out["violations"]), 0)
        for entry in out["rules"]:
            self.assertIn("source", entry)


if __name__ == "__main__":
    unittest.main()
