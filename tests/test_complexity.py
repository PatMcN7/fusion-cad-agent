"""Preflight counts agree with small generated fields and bound large work."""

import math
import os
import unittest
from unittest.mock import patch

import complexity
import geom
import patterns
from errors import ArgumentError


class ComplexityTests(unittest.TestCase):
    def test_counts_match_generated_isogrids(self):
        for stagger in (0, 0.25, 0.5, 0.8, -0.25):
            for pitch in (0.7, 1, 1.5):
                for inverted in (False, True):
                    options = dict(row_stagger=stagger, row_pitch=pitch,
                                   include_inverted=inverted, margin=0.3)
                    field = patterns.isogrid_triangles(12, 6, 1.5, 0.1, **options)
                    counts = complexity.lattice_counts("isogrid", 12, 6, 1.5, 0.1, **options)
                    self.assertEqual(counts["cells"], len(field))
                    self.assertEqual(counts["sketch_curves"], sum(map(len, field)))

    def test_counts_match_hexagons_including_boundary_cases(self):
        for length in (3, 6, 12):
            for width in (3, 5, 8):
                field = patterns.hex_cells(length, width, 1.5, 0.1)
                counts = complexity.lattice_counts("hex", length, width, 1.5, 0.1)
                self.assertEqual(counts["cells"], len(field))

    def test_rounded_curves_are_counted_before_expansion(self):
        field = patterns.hex_cells(6, 6, 1.5, 0.1)
        rounded = geom.fillet_polygons(field, 0.05, 6)
        counts = complexity.lattice_counts("hex", 6, 6, 1.5, 0.1, corner_segments=6)
        self.assertEqual(counts["sketch_curves"], sum(map(len, rounded)))
        with self.assertRaises(ArgumentError):
            complexity.lattice_counts("hex", 6, 6, 1.5, 0.1, corner_segments=10000)

    def test_dense_pan_is_rejected_before_polygon_generation(self):
        with patch.object(patterns, "inset_triangle", side_effect=AssertionError("allocated")):
            with self.assertRaises(ArgumentError) as caught:
                patterns.isogrid_triangles(30 * 2.54, 30 * 2.54, 1.5 * 2.54, 0.1 * 2.54)
        counts = caught.exception.context["complexity"]["counts"]
        self.assertGreater(counts["cells"], 250)
        self.assertIn("Increase cell_size", str(caught.exception))

    def test_tiny_pitch_is_bounded_before_enumeration(self):
        with self.assertRaises(ArgumentError) as caught:
            complexity.lattice_counts("isogrid", 30, 30, 1.5, 0.1, row_pitch=1e-12)
        self.assertIn("candidate_rows", caught.exception.context["complexity"]["exceeded"])

    def test_invalid_numeric_inputs_do_not_enter_generator_loops(self):
        for value in (0, -1, math.nan, math.inf):
            with self.assertRaises(ArgumentError):
                patterns.hex_cells(30, 30, value, 0.1)

    def test_curve_limit_applies_to_explicit_polygons(self):
        with self.assertRaises(ArgumentError):
            complexity.polygon_counts([[(0, 0)] * 6001])

    def test_limit_is_inclusive_and_configurable(self):
        complexity.Limits(cells=2).require(cells=2)
        with patch.dict(os.environ, {"FUSION_CAD_MAX_CELLS": "1"}):
            with self.assertRaises(ArgumentError):
                patterns.isogrid_triangles(12, 4, 1.5, 0.25)
        for invalid in ("0", "-2", "abc"):
            with patch.dict(os.environ, {"FUSION_CAD_MAX_CELLS": invalid}):
                with self.assertRaises(ArgumentError):
                    complexity.Limits.configured()

    def test_fillets_are_guarded_before_kernel_access(self):
        import ops
        from types import SimpleNamespace
        # ops imports adsk lazily. Registry tests install the inert module;
        # the guard itself needs only this explicit edge count.
        from test_registry import _install_adsk_stub
        _install_adsk_stub()
        with self.assertRaises(ArgumentError):
            ops.fillet_all_edges(None, SimpleNamespace(edges=SimpleNamespace(count=1001)), 0.1)


class ToolPreflightTests(unittest.TestCase):
    def test_rounded_lattice_checks_original_junctions_and_final_clearances(self):
        import tools
        from types import SimpleNamespace
        box = SimpleNamespace(minPoint=SimpleNamespace(x=0, y=0, z=0),
                              maxPoint=SimpleNamespace(x=8 * 2.54, y=6 * 2.54, z=0.635))
        body = SimpleNamespace(preciseBoundingBox=box, volume=100)
        design = SimpleNamespace(rootComponent=object(), unitsManager=object())
        def cm(units, value, name, **kwargs):
            return float(value.split()[0]) * 2.54
        for stagger, expected_ok in ((0.5, True), (0.0, False)):
            checks = []
            original_check = tools.assertions.tiling_valence
            def record(*args, **kwargs):
                result = original_check(*args, **kwargs)
                checks.append(result)
                return result
            with self.subTest(stagger=stagger), patch.object(
                    tools, "_active_design", return_value=design), patch.object(
                    tools, "_resolve_target", return_value=body), patch.object(
                    tools, "_cm", side_effect=cm), patch.object(
                    tools.assertions, "tiling_valence", side_effect=record), patch.object(
                    tools.assertions, "minimum_gap", wraps=tools.assertions.minimum_gap) as gap, patch.object(
                    tools.pick, "faces", side_effect=RuntimeError("native boundary")):
                with self.assertRaisesRegex(RuntimeError, "native boundary"):
                    tools.lattice_pockets({"cell_size": "2 in", "rib_width": "0.25 in",
                                           "cutter_diameter": "0.125 in", "corner_segments": 3,
                                           "row_stagger": stagger})
                self.assertEqual(checks[0].ok, expected_ok)
                self.assertEqual(len(gap.call_args.args[1][0]), 12)

    def test_lattice_guard_runs_before_rounding_or_kernel_work(self):
        import tools
        from types import SimpleNamespace
        box = SimpleNamespace(minPoint=SimpleNamespace(x=0, y=0, z=0),
                              maxPoint=SimpleNamespace(x=30 * 2.54, y=30 * 2.54, z=0.3))
        body = SimpleNamespace(preciseBoundingBox=box)
        design = SimpleNamespace(rootComponent=object(), unitsManager=object())
        def cm(units, value, name, **kwargs):
            return float(value.split()[0]) * 2.54
        for arguments in ({"cell_size": "1.5 in"},
                          {"cell_size": "3 in", "cutter_diameter": "0.125 in", "corner_segments": 1000}):
            with patch.object(tools, "_active_design", return_value=design), patch.object(
                    tools, "_resolve_target", return_value=body), patch.object(
                    tools, "_cm", side_effect=cm), patch.object(
                    tools.geom, "fillet_polygons", side_effect=AssertionError("allocated rounded field")), patch.object(
                    tools.ops, "cut_polygons", side_effect=AssertionError("touched kernel")):
                with self.assertRaises(ArgumentError):
                    tools.lattice_pockets(arguments)
