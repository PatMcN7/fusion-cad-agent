"""Corner rounding, and the machinability check that depends on it."""

import math
import unittest

import assertions
import catalog
import geom
import patterns


class FilletPolygon(unittest.TestCase):
    def test_rounded_square_area_matches_closed_form(self):
        # Exact area of a square with four r-radius corners: A - (4 - pi)r^2.
        square = [(0, 0), (2, 0), (2, 2), (0, 2)]
        radius = 0.25
        rounded = geom.fillet_polygon(square, radius, segments=64)
        expected = 4 - (4 - math.pi) * radius ** 2
        self.assertAlmostEqual(_area(rounded), expected, places=4)

    def test_fillet_never_leaves_the_original(self):
        square = [(0, 0), (2, 0), (2, 2), (0, 2)]
        rounded = geom.fillet_polygon(square, 0.4, segments=16)
        for x, y in rounded:
            self.assertGreaterEqual(x, -1e-9)
            self.assertGreaterEqual(y, -1e-9)
            self.assertLessEqual(x, 2 + 1e-9)
            self.assertLessEqual(y, 2 + 1e-9)

    def test_oversized_radius_degrades_instead_of_self_intersecting(self):
        # Half the shortest edge is the clamp; a huge radius must still
        # produce a simple polygon rather than a knot.
        triangle = [(0, 0), (1, 0), (0.5, math.sqrt(3) / 2)]
        rounded = geom.fillet_polygon(triangle, 50.0, segments=8)
        self.assertGreater(_area(rounded), 0.0)
        self.assertFalse(geom.overlapping_pairs([rounded]))

    def test_filleting_a_lattice_keeps_cells_disjoint(self):
        # The rib between cells must survive rounding: a fillet that grew the
        # cell outward would eat the rib and merge neighbours.
        field = patterns.isogrid_triangles(30.48, 30.48, 7.62, 0.635)
        gap_before = geom.minimum_separation(field, search=12)
        rounded = geom.fillet_polygons(field, 0.125 * 2.54 / 2, segments=6)
        self.assertFalse(geom.overlapping_pairs(rounded))
        self.assertGreaterEqual(geom.minimum_separation(rounded, search=12),
                                gap_before - 1e-9)

    def test_zero_radius_is_the_identity(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        self.assertEqual(geom.fillet_polygon(square, 0), [(0, 0), (1, 0), (1, 1), (0, 1)])


class Machinability(unittest.TestCase):
    def test_sharp_isogrid_warns(self):
        field = patterns.isogrid_triangles(30.48, 30.48, 7.62, 0.635)
        check = assertions.corners_machinable("m", field, 0.0)
        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "warning")

    def test_rounded_isogrid_passes(self):
        field = patterns.isogrid_triangles(30.48, 30.48, 7.62, 0.635)
        radius = 0.125 * 2.54 / 2
        check = assertions.corners_machinable(
            "m", geom.fillet_polygons(field, radius), radius)
        self.assertTrue(check.ok)


class Catalog(unittest.TestCase):
    """The source map is what stops an agent grepping this repo."""

    def test_brief_lists_the_helpers_agents_hunt_for(self):
        brief = catalog.brief()
        for needle in ("isogrid_triangles", "row_stagger=0.5", "fillet_polygons",
                       "cut_polygons", "tiling_valence", "class CheckSuite"):
            self.assertIn(needle, brief, f"{needle} missing from the brief")

    def test_brief_carries_the_agents_md_guidance_too(self):
        self.assertIn("Fusion unit expression strings", catalog.brief())


def _area(polygon):
    total = 0.0
    for index in range(len(polygon)):
        x0, y0 = polygon[index]
        x1, y1 = polygon[(index + 1) % len(polygon)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2


if __name__ == "__main__":
    unittest.main()


class VisionRole(unittest.TestCase):
    """Captures are summarized rather than accumulated."""

    def setUp(self):
        import sys, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for p in (str(root / "studio"), str(root / "studio" / "providers")):
            if p not in sys.path:
                sys.path.insert(0, p)

    def test_tool_result_renders_the_description(self):
        from providers.base import ToolResult

        result = ToolResult("id", "lattice_pockets", True, {"cells": 105})
        self.assertNotIn("described by the vision model", result.as_text())
        result.description = "Triangles do not share vertices."
        text = result.as_text()
        self.assertIn("cells", text)
        self.assertIn("Triangles do not share vertices.", text)

    def test_describe_mode_replaces_the_image_by_default(self):
        import os
        import vision

        previous = os.environ.pop("STUDIO_VISION", None)
        try:
            describer, mode = vision.from_environment()
            self.assertEqual(mode, "describe")
            self.assertIsNotNone(describer)
        finally:
            if previous is not None:
                os.environ["STUDIO_VISION"] = previous

    def test_vision_can_be_switched_off(self):
        import os
        import vision

        os.environ["STUDIO_VISION"] = "off"
        try:
            describer, mode = vision.from_environment()
            self.assertIsNone(describer)
            self.assertEqual(mode, "off")
        finally:
            os.environ.pop("STUDIO_VISION")

    def test_describe_never_raises_when_the_backend_is_missing(self):
        import vision

        describer = vision.Describer("anthropic", "claude-opus-5")
        describer._build = lambda: (_ for _ in ()).throw(
            __import__("providers").ProviderError("no key"))
        self.assertIn("vision unavailable", describer.describe("Zm9v"))


class OpenRouterBackend(unittest.TestCase):
    def setUp(self):
        import sys, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for p in (str(root / "studio"), str(root / "studio" / "providers")):
            if p not in sys.path:
                sys.path.insert(0, p)

    def test_registered_as_a_backend(self):
        import providers

        self.assertIn("openrouter", providers.ENV_KEYS)
        self.assertEqual(providers.ENV_KEYS["openrouter"], "OPENROUTER_API_KEY")
        self.assertIn("openrouter", providers.available())

    def test_presets_reference_only_real_openrouter_ids(self):
        import providers

        ids = [k for k, v in providers.PRESETS.items() if v["provider"] == "openrouter"]
        self.assertTrue(ids)
        for model in ids:
            # OpenRouter ids are always vendor-namespaced; a bare id here
            # would 404 at the first turn.
            self.assertIn("/", model, f"{model} is not an OpenRouter id")

    def test_missing_key_fails_with_an_actionable_message(self):
        import os
        import providers
        from providers import ProviderError

        previous = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            with self.assertRaises(ProviderError) as caught:
                providers.build("openrouter", "qwen/qwen3.7-flash")
            self.assertIn("OPENROUTER_API_KEY", str(caught.exception))
        finally:
            if previous is not None:
                os.environ["OPENROUTER_API_KEY"] = previous

    def test_catalogue_failure_is_survivable(self):
        # The catalogue is an enhancement. If openrouter.ai is unreachable the
        # adapter must still start, not refuse to run.
        from providers import openrouter_provider as orp

        orp._CATALOGUE_CACHE = {}
        self.assertEqual(orp.capabilities("anything/at-all"), {"known": False})

    def test_capabilities_reads_the_catalogue_shape(self):
        from providers import openrouter_provider as orp

        orp._CATALOGUE_CACHE = {"vendor/m": {
            "name": "M", "context_length": 262144,
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
        }}
        try:
            capability = orp.capabilities("vendor/m")
            self.assertTrue(capability["tools"])
            self.assertTrue(capability["images"])
            self.assertAlmostEqual(capability["input_per_m"], 0.1)
            self.assertAlmostEqual(capability["output_per_m"], 0.4)
        finally:
            orp._CATALOGUE_CACHE = None

    def test_base_adapter_exposes_the_seams_the_subclass_overrides(self):
        from providers.openai_provider import OpenAIProvider
        from providers.openrouter_provider import OpenRouterProvider

        for name in ("_create", "_explain", "_usage"):
            self.assertTrue(hasattr(OpenAIProvider, name), name)
        # The subclass must genuinely override, or it is silently plain OpenAI.
        for name in ("_create", "_explain", "_usage"):
            self.assertIsNot(getattr(OpenRouterProvider, name),
                             getattr(OpenAIProvider, name), name)
