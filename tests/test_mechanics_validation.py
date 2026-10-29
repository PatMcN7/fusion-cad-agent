"""Validation you can check without an engineering background.

Four kinds, none of which require you to trust the author:

1. COEFFICIENTS -- every beam formula's constant is stated explicitly and is
   googleable. "simply supported beam point load deflection" returns PL^3/48EI
   on any reference site. You are checking one number against the internet.

2. SCALING LAWS -- how the answer must respond to changing one input. Double
   the load, double the deflection. Double the span, eight times the
   deflection. These follow from the physics regardless of whether the
   constant is right, and they catch a wrong exponent instantly.

3. LIMIT CASES -- values that converge to famous published numbers. A small
   hole in a wide plate has Kt = 3.0 exactly (Kirsch, 1898). A thin-walled
   tube approaches pi*r^3*t.

4. CATALOGUE CROSS-CHECK -- computed mass against what a metal supplier
   publishes for the same stock. Independently verifiable in a browser.
"""

import math
import unittest

from fusion_cad import mechanics as mech


class Coefficients(unittest.TestCase):
    """Each formula's constant, isolated so it can be checked against any
    reference. Search the case name; compare the number."""

    def setUp(self):
        self.mat = mech.Material("test", E=1.0, density=1.0, yield_strength=1.0,
                                 ultimate=1.0)
        # A section with Ix exactly 1.0 so the coefficient is the whole answer.
        self.section = mech.Section("unit", area=1.0, Ix=1.0, Iy=1.0,
                                    height=2.0, width=1.0)

    def _coefficient(self, fn, load=1.0, span=1.0):
        return fn(load, span, self.mat, self.section).deflection

    def test_cantilever_point_is_one_third(self):
        # delta = P L^3 / (3 E I)
        self.assertAlmostEqual(self._coefficient(mech.cantilever_point), 1 / 3, places=12)

    def test_cantilever_udl_is_one_eighth(self):
        # delta = w L^4 / (8 E I)
        self.assertAlmostEqual(self._coefficient(mech.cantilever_udl), 1 / 8, places=12)

    def test_simply_supported_point_is_one_forty_eighth(self):
        # delta = P L^3 / (48 E I)
        self.assertAlmostEqual(
            self._coefficient(mech.simply_supported_point), 1 / 48, places=12)

    def test_simply_supported_udl_is_five_over_384(self):
        # delta = 5 w L^4 / (384 E I)
        self.assertAlmostEqual(
            self._coefficient(mech.simply_supported_udl), 5 / 384, places=12)

    def test_fixed_fixed_point_is_one_one_ninety_second(self):
        # delta = P L^3 / (192 E I)
        self.assertAlmostEqual(
            self._coefficient(mech.fixed_fixed_point), 1 / 192, places=12)

    def test_moment_coefficients(self):
        section, mat = self.section, self.mat
        self.assertAlmostEqual(
            mech.simply_supported_point(1.0, 1.0, mat, section).max_moment, 1 / 4)
        self.assertAlmostEqual(
            mech.simply_supported_udl(1.0, 1.0, mat, section).max_moment, 1 / 8)
        self.assertAlmostEqual(
            mech.cantilever_point(1.0, 1.0, mat, section).max_moment, 1.0)
        self.assertAlmostEqual(
            mech.cantilever_udl(1.0, 1.0, mat, section).max_moment, 1 / 2)


class ScalingLaws(unittest.TestCase):
    """Exponents, which follow from physics regardless of any constant.

    A wrong exponent is the most damaging kind of error -- it is right at one
    size and badly wrong at another. These catch it without any reference.
    """

    def setUp(self):
        self.mat = mech.material("6061-T6")
        self.section = mech.rect_tube(1.0, 2.0, 0.125)

    def _deflect(self, load=50.0, span=25.0, mat=None, section=None):
        return mech.simply_supported_point(
            load, span, mat or self.mat, section or self.section).deflection

    def test_deflection_is_linear_in_load(self):
        self.assertAlmostEqual(self._deflect(load=100.0) / self._deflect(load=50.0),
                               2.0, places=9)

    def test_deflection_is_cubic_in_span(self):
        self.assertAlmostEqual(self._deflect(span=50.0) / self._deflect(span=25.0),
                               8.0, places=9)

    def test_deflection_is_inverse_in_modulus(self):
        stiff = mech.Material("2x", E=self.mat.E * 2, density=1, yield_strength=1,
                              ultimate=1)
        self.assertAlmostEqual(self._deflect(mat=stiff) / self._deflect(), 0.5, places=9)

    def test_deflection_is_inverse_in_inertia(self):
        double = mech.Section("2I", area=1.0, Ix=self.section.Ix * 2,
                              Iy=1.0, height=2.0, width=1.0)
        single = mech.Section("1I", area=1.0, Ix=self.section.Ix,
                              Iy=1.0, height=2.0, width=1.0)
        self.assertAlmostEqual(
            self._deflect(section=double) / self._deflect(section=single), 0.5, places=9)

    def test_second_moment_is_cubic_in_depth(self):
        self.assertAlmostEqual(mech.rect(1.0, 2.0).Ix / mech.rect(1.0, 1.0).Ix,
                               8.0, places=9)

    def test_second_moment_is_linear_in_width(self):
        self.assertAlmostEqual(mech.rect(2.0, 1.0).Ix / mech.rect(1.0, 1.0).Ix,
                               2.0, places=9)

    def test_buckling_is_inverse_square_in_length(self):
        section = mech.rect_tube(1.0, 2.0, 0.125)
        short = mech.euler_buckling(section, 50.0, self.mat)["critical_load_lb"]
        long = mech.euler_buckling(section, 100.0, self.mat)["critical_load_lb"]
        self.assertAlmostEqual(short / long, 4.0, places=9)

    def test_plate_deflection_is_fourth_power_in_span(self):
        small = mech.flat_plate_udl_simply_supported(1.0, 10.0, 10.0, 0.125, self.mat)
        big = mech.flat_plate_udl_simply_supported(1.0, 20.0, 20.0, 0.125, self.mat)
        self.assertAlmostEqual(
            big["deflection_in"] / small["deflection_in"], 16.0, places=6)

    def test_plate_deflection_is_inverse_cubic_in_thickness(self):
        thin = mech.flat_plate_udl_simply_supported(1.0, 10.0, 10.0, 0.100, self.mat)
        thick = mech.flat_plate_udl_simply_supported(1.0, 10.0, 10.0, 0.200, self.mat)
        self.assertAlmostEqual(
            thin["deflection_in"] / thick["deflection_in"], 8.0, places=6)


class LimitCases(unittest.TestCase):
    """Convergence to published closed-form values."""

    def test_kt_of_a_small_hole_is_three(self):
        # Kirsch 1898: an infinite plate with a circular hole in tension has a
        # stress concentration of exactly 3. The most cited number in the field.
        self.assertAlmostEqual(mech.kt_hole_in_plate(1e-6, 1.0), 3.0, places=5)

    def test_thin_wall_tube_approaches_pi_r_cubed_t(self):
        outer, wall = 2.0, 0.01
        mean_radius = (outer - wall) / 2
        self.assertAlmostEqual(
            mech.round_tube(outer, wall).Ix,
            math.pi * mean_radius**3 * wall,
            delta=math.pi * mean_radius**3 * wall * 0.02)

    def test_tube_wall_going_to_full_thickness_becomes_solid(self):
        nearly_solid = mech.rect_tube(1.0, 2.0, 0.4999)
        solid = mech.rect(1.0, 2.0)
        self.assertAlmostEqual(nearly_solid.Ix, solid.Ix, delta=solid.Ix * 0.01)

    def test_hollow_equals_solid_minus_void(self):
        self.assertAlmostEqual(
            mech.rect_tube(1.0, 2.0, 0.125).Ix,
            mech.rect(1.0, 2.0).Ix - mech.rect(0.75, 1.75).Ix,
            places=12)

    def test_fixed_ends_are_exactly_four_times_stiffer(self):
        # 1/48 vs 1/192 -- a textbook relationship, checkable by inspection.
        mat, section = mech.material("6061-T6"), mech.rect_tube(1.0, 2.0, 0.125)
        pinned = mech.simply_supported_point(50, 25, mat, section).deflection
        fixed = mech.fixed_fixed_point(50, 25, mat, section).deflection
        self.assertAlmostEqual(pinned / fixed, 4.0, places=9)


class CatalogueCrossCheck(unittest.TestCase):
    """Against numbers a metal supplier publishes. Verifiable in a browser.

    Note these assume sharp corners. Real extrusions have corner radii, so a
    published area runs about 1-3% below the sharp-corner calculation. The
    tolerances here allow for that; a match tighter than that would be
    suspicious, not reassuring.
    """

    def test_1x2x125_aluminium_tube_mass_per_foot(self):
        # Suppliers list 6061 1" x 2" x 0.125" wall at roughly 0.80 lb/ft.
        tube = mech.rect_tube(1.0, 2.0, 0.125)
        mass = tube.mass_per_foot(mech.material("6061-T6"))
        self.assertAlmostEqual(mass, 0.81, delta=0.04)

    def test_aluminium_plate_mass_per_square_foot(self):
        # 0.125" 6061 sheet is about 1.76 lb/ft^2.
        mat = mech.material("6061-T6")
        self.assertAlmostEqual(144 * 0.125 * mat.density, 1.76, delta=0.05)

    def test_steel_density_ratio_to_aluminium(self):
        # Steel is very close to 2.9x aluminium by density.
        ratio = mech.material("1018").density / mech.material("6061-T6").density
        self.assertAlmostEqual(ratio, 2.9, delta=0.05)

    def test_steel_modulus_ratio_to_aluminium(self):
        # And very close to 3x the stiffness.
        ratio = mech.material("1018").E / mech.material("6061-T6").E
        self.assertAlmostEqual(ratio, 2.9, delta=0.1)

    def test_specific_stiffness_is_similar_across_metals(self):
        # E/rho is famously within ~10% for aluminium, steel and titanium --
        # which is why you cannot make a stiffer part just by changing metal.
        values = [mech.material(n).specific_stiffness
                  for n in ("6061-T6", "1018", "Ti-6Al-4V")]
        self.assertLess((max(values) - min(values)) / min(values), 0.10)


class DimensionalConsistency(unittest.TestCase):
    """Units must work out. Checked by scaling every input by a factor and
    confirming the output scales by the predicted power."""

    def test_deflection_has_dimensions_of_length(self):
        mat = mech.material("6061-T6")
        # Scale all lengths by k: P/(E) fixed, L^3 -> k^3, I -> k^4.
        # So deflection -> k^3/k^4 = k^-1 ... times load scaling k^2 for a
        # pressure-driven load. Here with fixed load, deflection scales k^-1.
        k = 2.0
        small = mech.simply_supported_point(
            100.0, 10.0, mat, mech.rect(1.0, 1.0)).deflection
        big = mech.simply_supported_point(
            100.0, 10.0 * k, mat, mech.rect(1.0 * k, 1.0 * k)).deflection
        self.assertAlmostEqual(big / small, k**3 / k**4, places=9)

    def test_stress_has_dimensions_of_force_over_area(self):
        mat = mech.material("6061-T6")
        k = 2.0
        small = mech.simply_supported_point(
            100.0, 10.0, mat, mech.rect(1.0, 1.0)).bending_stress
        big = mech.simply_supported_point(
            100.0, 10.0 * k, mat, mech.rect(1.0 * k, 1.0 * k)).bending_stress
        # moment ~ k, section modulus ~ k^3, so stress ~ k^-2
        self.assertAlmostEqual(big / small, k / k**3, places=9)


if __name__ == "__main__":
    unittest.main()
