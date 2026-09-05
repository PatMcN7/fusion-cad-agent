"""Regression tests for the three isogrid defects that reached Fusion.

Each one shipped in a real session. Two were caught only by an in-Fusion
assertion after the fact, and one was caught by a human looking at the screen.
All three are now cheap offline checks that run before the kernel is touched.
"""

import math
import unittest

import assertions
import geom
import patterns
from errors import CheckFailed


def buggy_tiling(length, width, cell_size, rib_width, center=True):
    """The original tiling, reproduced exactly.

    The inverted row starts half a cell early, which mirrors each inverted
    triangle onto an upright one instead of nesting between them.
    """
    triangle_height = cell_size * math.sqrt(3) / 2
    left, right = -length / 2 + rib_width, length / 2 - rib_width
    bottom, top = -width / 2 + rib_width, width / 2 - rib_width
    inset = rib_width / 2
    tol = 1e-7
    triangles, row, y0 = [], 0, bottom
    while y0 + triangle_height <= top + tol:
        x0 = left
        while x0 + cell_size <= right + tol:
            triangles.append(patterns.inset_triangle(
                [(x0, y0), (x0 + cell_size, y0), (x0 + cell_size / 2, y0 + triangle_height)],
                inset))
            x0 += cell_size
        x0 = left - cell_size / 2          # <-- the defect
        while x0 + 1.5 * cell_size <= right + tol:
            full = [(x0 + cell_size / 2, y0 + triangle_height),
                    (x0 + 1.5 * cell_size, y0 + triangle_height),
                    (x0 + cell_size, y0)]
            if min(p[0] for p in full) >= left - tol:
                triangles.append(patterns.inset_triangle(full, inset))
            x0 += cell_size
        row += 1
        y0 = bottom + row * triangle_height
    if center:
        triangles = patterns.center_polygons(triangles)
    return triangles


class OverlapRegression(unittest.TestCase):
    """Bug 1: 28 triangles produced 43 sketch profiles."""

    def test_buggy_tiling_is_caught(self):
        triangles = buggy_tiling(12, 4, 1.5, 0.25)
        check = assertions.polygons_disjoint("pockets_disjoint", triangles)
        self.assertFalse(check.ok, "overlapping tiling must fail the check")
        self.assertIn("overlapping pairs", str(check.actual))

    def test_counting_alone_would_have_missed_it(self):
        # The original pre-flight only counted triangles, which is why it
        # passed. A plausible count alone says nothing about topology.
        self.assertEqual(len(buggy_tiling(12, 4, 1.5, 0.25)), 28)
        self.assertEqual(len(patterns.isogrid_triangles(12, 4, 1.5, 0.25)), 27)

    def test_fixed_tiling_passes(self):
        triangles = patterns.isogrid_triangles(12, 4, 1.5, 0.25)
        self.assertTrue(assertions.polygons_disjoint("d", triangles).ok)

    def test_successive_bands_are_half_cell_shifted(self):
        cell = 1.0
        height = cell * math.sqrt(3) / 2
        triangles = patterns.isogrid_triangles(
            12, 4, cell, 0.1, center=False)
        centroids = [
            (sum(x for x, _ in triangle) / 3,
             sum(y for _, y in triangle) / 3)
            for triangle in triangles
        ]
        first_band = min(y for _, y in centroids)
        second_band = min(y for _, y in centroids if y > first_band + height / 2)
        first_left = min(x for x, y in centroids if abs(y - first_band) < 1e-9)
        second_left = min(x for x, y in centroids if abs(y - second_band) < 1e-9)
        self.assertAlmostEqual(second_left - first_left, cell / 2, places=9)


class CenteringRegression(unittest.TestCase):
    """Bug 3: the lattice piled its remainder against the top and right."""

    def test_uncentered_pattern_is_caught(self):
        triangles = patterns.isogrid_triangles(12, 4, 1.5, 0.25, center=False)
        check = assertions.pattern_centered("pattern_centered", triangles, (0.0, 0.0), 1e-9)
        self.assertFalse(check.ok)

    def test_centered_pattern_passes(self):
        triangles = patterns.isogrid_triangles(12, 4, 1.5, 0.25)
        self.assertTrue(assertions.pattern_centered("c", triangles, (0.0, 0.0), 1e-9).ok)

    def test_margins_are_symmetric(self):
        triangles = patterns.isogrid_triangles(12, 4, 0.8, 0.08)
        min_x, max_x, min_y, max_y = patterns.bounds(triangles)
        self.assertAlmostEqual(min_x + 6, 6 - max_x, places=9)
        self.assertAlmostEqual(min_y + 2, 2 - max_y, places=9)


class ProfileCountRegression(unittest.TestCase):
    """Bug 2: sketching on a face adds a background profile.

    Feeding every profile to the cut would have machined the bar away and left
    the pockets standing. The count check is what stopped it.
    """

    def test_background_profile_is_a_failure(self):
        check = assertions.count_equals("triangular_profiles", 29, 28)
        self.assertFalse(check.ok)
        self.assertEqual(check.actual, 29)

    def test_exact_match_passes(self):
        self.assertTrue(assertions.count_equals("triangular_profiles", 28, 28).ok)


class ManufacturabilityChecks(unittest.TestCase):
    """The 25:1 rib the agent noticed only by chance."""

    def test_skeletonized_thin_rib_warns(self):
        # 0.08 in rib, 2 in deep -- what was actually built.
        check = assertions.rib_aspect_ratio("rib_aspect", 0.08, 2.0, 8.0)
        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "warning")
        self.assertIn("25.0:1", check.actual)

    def test_pocketed_rib_passes(self):
        self.assertTrue(assertions.rib_aspect_ratio("rib_aspect", 0.25, 1.75, 8.0).ok)

    def test_fillet_at_half_rib_is_rejected(self):
        self.assertFalse(assertions.fillet_fits_rib("f", 0.04, 0.08).ok)
        self.assertTrue(assertions.fillet_fits_rib("f", 0.025, 0.08).ok)


class GapChecks(unittest.TestCase):
    def test_rib_width_is_held_between_pockets(self):
        triangles = patterns.isogrid_triangles(12, 4, 1.0, 0.1)
        check = assertions.minimum_gap("rib", triangles, 0.1 * 0.99)
        self.assertTrue(check.ok, check.describe())

    def test_pockets_stay_inside_the_border(self):
        triangles = patterns.isogrid_triangles(12, 4, 1.0, 0.1)
        check = assertions.polygons_within("inside", triangles, -5.9, 5.9, -1.9, 1.9)
        self.assertTrue(check.ok, check.describe())


class SuiteBehaviour(unittest.TestCase):
    def test_warnings_do_not_fail_a_suite(self):
        suite = assertions.CheckSuite("demo")
        suite.add(assertions.rib_aspect_ratio("aspect", 0.08, 2.0, 8.0))
        self.assertTrue(suite.ok)
        self.assertEqual(len(suite.warnings), 1)
        suite.require()

    def test_errors_raise_with_detail(self):
        suite = assertions.CheckSuite("demo")
        suite.add(assertions.count_equals("profiles", 43, 28))
        self.assertFalse(suite.ok)
        with self.assertRaises(CheckFailed) as caught:
            suite.require()
        self.assertIn("profiles", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class TilingTopology(unittest.TestCase):
    """The bug every other check missed.

    An "isogrid" shipped with row_stagger=0.0: independent rows of triangles
    with a horizontal rib band between them. It passed polygons_disjoint,
    minimum_gap, pattern_centered and looked right in a render. The defect was
    topological -- no vertex had six triangles around it -- so nothing that
    measured overlap, spacing, or position could see it.
    """

    def test_default_isogrid_tessellates(self):
        field = patterns.isogrid_triangles(12, 6, 1.5, 0.1)
        self.assertTrue(assertions.tiling_valence("t", field).ok)

    def test_unstaggered_rows_are_caught(self):
        banded = patterns.isogrid_triangles(12, 6, 1.5, 0.1, row_stagger=0.0)
        check = assertions.tiling_valence("t", banded)
        self.assertFalse(check.ok, "stacked rows must not pass as a tiling")
        self.assertIn("do not tessellate", check.detail)

    def test_the_old_checks_all_passed_on_the_broken_field(self):
        # Precisely why this needed a new check rather than a better threshold.
        banded = patterns.isogrid_triangles(12, 6, 1.5, 0.1, row_stagger=0.0)
        self.assertTrue(assertions.polygons_disjoint("d", banded).ok)
        self.assertTrue(assertions.minimum_gap("g", banded, 0.1 * 0.99).ok)
        self.assertTrue(assertions.pattern_centered("c", banded, (0.0, 0.0), 1e-9).ok)

    def test_half_cell_shift_is_the_default(self):
        import inspect

        signature = inspect.signature(patterns.isogrid_triangles)
        self.assertEqual(signature.parameters["row_stagger"].default, 0.5)

    def test_hex_field_also_tessellates(self):
        field = patterns.hex_cells(12, 8, 1.5, 0.1)
        self.assertTrue(assertions.tiling_valence("t", field, expected=3).ok)


class AgentBrief(unittest.TestCase):
    """AGENTS.md is the single source of agent guidance.

    It previously existed as three overlapping copies -- the studio's system
    prompt, the MCP server's instructions, and nothing at all for Codex, which
    reads AGENTS.md automatically. Copies drift; this asserts they cannot.
    """

    def setUp(self):
        import pathlib

        import catalog

        self.root = pathlib.Path(__file__).resolve().parent.parent
        # The brief is AGENTS.md plus the generated source map. Both surfaces
        # get the whole thing; asserting against AGENTS.md alone would let the
        # source map silently reach one client and not the other.
        self.brief = catalog.brief()

    def test_brief_exists_where_codex_looks_for_it(self):
        self.assertTrue((self.root / "AGENTS.md").is_file())

    def test_mcp_instructions_are_the_brief(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mcp_probe", self.root / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.INSTRUCTIONS, self.brief)
        self.assertIn("## Source map", module.INSTRUCTIONS)

    def test_studio_system_prompt_is_the_brief(self):
        import re

        source = (self.root / "studio" / "server.py").read_text(encoding="utf-8")
        # Assert by construction rather than by importing the studio, which
        # would pull in optional vendor SDKs the test env need not have.
        self.assertIn("SYSTEM_PROMPT = _load_brief()", source)
        self.assertIn("catalog.brief()", source)

    def test_brief_documents_the_source_editing_escape_hatch(self):
        # The capability that solved a real request and was undocumented:
        # the geometry code hot-reloads, so an agent may fix a generator.
        self.assertIn("hot-reload", self.brief.lower())
        self.assertIn("patterns.py", self.brief)

    def test_brief_warns_that_green_checks_are_not_correctness(self):
        lowered = self.brief.lower()
        self.assertIn("does **not** mean the", lowered.replace("\n", " "))

    def test_brief_names_the_arbitrary_geometry_tools(self):
        for tool in ("cut_polygons", "extrude_polygons", "lattice_pockets"):
            self.assertIn(tool, self.brief)
