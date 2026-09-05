"""Tests for the provider-neutral layer.

Everything here runs without any vendor SDK, key, or network: the schema
translation and registry logic are pure, and that is where the bugs live.
"""

import sys
import unittest
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent))

import providers  # noqa: E402
from providers import ProviderError, ToolResult, ToolSpec, truncate, user_blocks  # noqa: E402
from providers.gemini_provider import _clean_schema  # noqa: E402


class ToolSchema(unittest.TestCase):
    def test_required_is_omitted_when_empty(self):
        # An empty `required: []` is rejected by some backends.
        schema = ToolSpec("t", "d", {"a": {"type": "string"}}).json_schema()
        self.assertNotIn("required", schema)

    def test_required_is_included_when_present(self):
        schema = ToolSpec("t", "d", {"a": {"type": "string"}}, ["a"]).json_schema()
        self.assertEqual(schema["required"], ["a"])


class GeminiSchemaCleaning(unittest.TestCase):
    """Gemini rejects JSON Schema keywords it does not implement."""

    def test_unsupported_keywords_are_stripped(self):
        cleaned = _clean_schema({
            "type": "object",
            "properties": {"n": {"type": "number", "minimum": 0, "maximum": 5}},
            "additionalProperties": False,
        })
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("minimum", cleaned["properties"]["n"])
        self.assertEqual(cleaned["properties"]["n"]["type"], "number")

    def test_every_property_gets_a_type(self):
        cleaned = _clean_schema({"type": "object",
                                 "properties": {"x": {"description": "no type"}}})
        self.assertEqual(cleaned["properties"]["x"]["type"], "string")

    def test_nested_objects_are_cleaned_recursively(self):
        cleaned = _clean_schema({
            "type": "object",
            "properties": {"o": {"type": "object", "additionalProperties": False,
                                 "properties": {"i": {"type": "string", "minLength": 2}}}},
        })
        inner = cleaned["properties"]["o"]
        self.assertNotIn("additionalProperties", inner)
        self.assertNotIn("minLength", inner["properties"]["i"])


class UserBlocks(unittest.TestCase):
    def test_text_only(self):
        blocks = user_blocks("hello")
        self.assertEqual(blocks, [{"type": "text", "text": "hello"}])

    def test_image_precedes_text(self):
        # Every backend reads an image better when it arrives before the
        # question that refers to it.
        blocks = user_blocks("what is this", "data:image/png;base64,QUJD")
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[1]["type"], "text")
        self.assertEqual(blocks[0]["data"], "QUJD")

    def test_jpeg_media_type_detected(self):
        blocks = user_blocks("", "data:image/jpeg;base64,QUJD")
        self.assertEqual(blocks[0]["media_type"], "image/jpeg")

    def test_blank_input_yields_nothing(self):
        self.assertEqual(user_blocks("   ", ""), [])


class Truncation(unittest.TestCase):
    def test_short_payload_is_untouched(self):
        self.assertEqual(truncate({"a": 1}), '{"a": 1}')

    def test_long_payload_is_marked(self):
        out = truncate({"a": "x" * 50000}, limit=200)
        self.assertLess(len(out), 400)
        self.assertIn("truncated", out)

    def test_non_serializable_does_not_raise(self):
        # default=str keeps a Path from blowing up json.dumps mid-tool-result.
        self.assertEqual(truncate({"p": Path("/tmp")}), '{"p": "/tmp"}')


class Registry(unittest.TestCase):
    def test_every_preset_names_a_known_provider(self):
        for model, spec in providers.PRESETS.items():
            self.assertIn(spec["provider"], providers.ENV_KEYS, model)
            self.assertIn("label", spec, model)

    def test_local_presets_carry_a_base_url(self):
        # A local model is the OpenAI adapter pointed elsewhere; without a
        # base_url it would silently call OpenAI's servers instead.
        for model, spec in providers.PRESETS.items():
            if "local" in spec["label"].lower():
                self.assertTrue(spec.get("base_url"), model)

    def test_unknown_provider_raises_provider_error(self):
        with self.assertRaises(ProviderError) as caught:
            providers.build("cohere", "whatever")
        self.assertIn("unknown provider", str(caught.exception))

    def test_build_never_leaks_a_vendor_exception(self):
        # Callers only catch ProviderError; a leaked vendor exception surfaces
        # as a stack trace mid-stream instead of an actionable message. Force a
        # constructor failure rather than using a bad base_url -- these clients
        # are lazy and correctly do not connect until the first request.
        import providers.openai_provider as mod

        original = mod.OpenAIProvider.__init__

        def exploding(self, model="gpt-4o", **kwargs):
            raise ValueError("simulated SDK constructor failure")

        mod.OpenAIProvider.__init__ = exploding
        try:
            with self.assertRaises(ProviderError) as caught:
                providers.build("openai", "gpt-4o")
            self.assertIn("could not start", str(caught.exception))
        finally:
            mod.OpenAIProvider.__init__ = original

    def test_lazy_client_does_not_connect_at_construction(self):
        # Constructing against an unreachable endpoint must succeed: blocking
        # on a network round-trip at construction would stall page load.
        provider = providers.build("openai", "gpt-4o", base_url="http://127.0.0.1:1")
        self.assertEqual(provider.model, "gpt-4o")

    def test_availability_reports_actionable_install_hint(self):
        # Not every backend is a pip package -- Codex is a CLI that carries
        # its own auth -- so assert an actionable hint, not a pip command.
        for name, state in providers.available().items():
            self.assertTrue(state["install"], name)
            if state["env_var"]:
                self.assertTrue(state["env_var"].endswith("_API_KEY"), name)

    def test_codex_needs_no_key_of_its_own(self):
        # Codex authenticates with the user's ChatGPT subscription; this
        # process must never expect to hold a credential for it.
        self.assertEqual(providers.ENV_KEYS["codex"], "")
        self.assertEqual(providers.available()["codex"]["env_var"], "")


class AgentBackends(unittest.TestCase):
    """An agent backend runs its own tool loop; the harness must not run one
    for it, or every operation would execute twice."""

    def test_only_codex_is_flagged_as_an_agent(self):
        from providers.anthropic_provider import AnthropicProvider
        from providers.codex_provider import CodexProvider
        from providers.openai_provider import OpenAIProvider

        self.assertTrue(CodexProvider.is_agent)
        self.assertFalse(AnthropicProvider.is_agent)
        self.assertFalse(OpenAIProvider.is_agent)

    def test_harness_breaks_after_one_turn_for_an_agent_backend(self):
        import inspect

        import server
        source = inspect.getsource(server.Agent.run)
        self.assertIn('getattr(self.provider, "is_agent", False)', source)

    def test_codex_refuses_tool_results(self):
        # Feeding results to a backend that already ran its own tools would
        # look like the model ignoring them; fail loudly instead.
        from providers import ToolResult
        from providers.codex_provider import CodexProvider

        p = CodexProvider.__new__(CodexProvider)
        p.reset()
        with self.assertRaises(ProviderError):
            p.add_tool_results([ToolResult("a", "health", True, {})])


class CodexEventMapping(unittest.TestCase):
    """Map Codex's JSONL events onto the shared UI event names."""

    def _provider(self):
        from providers.codex_provider import CodexProvider

        p = CodexProvider.__new__(CodexProvider)
        p.reset()
        return p

    def _run(self, events):
        from providers import Turn

        p = self._provider()
        emitted, text, calls, turn = [], [], {}, Turn()
        for e in events:
            p._handle(e, lambda k, d: emitted.append((k, d)), text, calls, turn)
        return p, emitted, text, turn

    def test_thread_id_is_captured_for_resume(self):
        p, *_ = self._run([{"type": "thread.started", "thread_id": "abc-123"}])
        self.assertEqual(p.session_id, "abc-123")

    def test_agent_message_becomes_text(self):
        _, emitted, text, _ = self._run([
            {"type": "item.completed",
             "item": {"id": "i0", "type": "agent_message", "text": "Done."}}])
        self.assertEqual(text, ["Done."])
        self.assertIn(("text", {"text": "Done."}), emitted)

    def test_mcp_tool_call_maps_to_start_and_end(self):
        _, emitted, _, _ = self._run([
            {"type": "item.started",
             "item": {"id": "i1", "type": "mcp_tool_call", "server": "fusion-cad",
                      "tool": "design_summary", "arguments": {}}},
            {"type": "item.completed",
             "item": {"id": "i1", "type": "mcp_tool_call", "server": "fusion-cad",
                      "tool": "design_summary",
                      "result": {"content": [{"type": "text", "text": '{"bodies": 4}'}]}}},
        ])
        kinds = [k for k, _ in emitted]
        self.assertEqual(kinds, ["tool_start", "tool_end"])
        self.assertEqual(dict(emitted)["tool_end"]["result"], {"bodies": 4})

    def test_mutating_tool_is_flagged_read_only_tools_are_not(self):
        _, emitted, _, _ = self._run([
            {"type": "item.started",
             "item": {"id": "a", "type": "mcp_tool_call", "server": "fusion-cad",
                      "tool": "design_summary", "arguments": {}}},
            {"type": "item.started",
             "item": {"id": "b", "type": "mcp_tool_call", "server": "fusion-cad",
                      "tool": "replace_with_isogrid_bar", "arguments": {}}},
        ])
        flags = [d["mutating"] for _, d in emitted]
        self.assertEqual(flags, [False, True])

    def test_image_in_tool_result_becomes_a_capture(self):
        _, emitted, _, _ = self._run([
            {"type": "item.started",
             "item": {"id": "i1", "type": "mcp_tool_call", "server": "fusion-cad",
                      "tool": "create_test_box", "arguments": {}}},
            {"type": "item.completed",
             "item": {"id": "i1", "type": "mcp_tool_call", "tool": "create_test_box",
                      "result": {"content": [
                          {"type": "text", "text": "{}"},
                          {"type": "image", "data": "QUJD", "mimeType": "image/png"}]}}},
        ])
        captures = [d for k, d in emitted if k == "capture"]
        self.assertEqual(len(captures), 1)
        self.assertTrue(captures[0]["inline"].startswith("data:image/png;base64,"))

    def test_usage_is_recorded_from_turn_completed(self):
        _, _, _, turn = self._run([
            {"type": "turn.completed",
             "usage": {"input_tokens": 100, "output_tokens": 20,
                       "cached_input_tokens": 80}}])
        self.assertEqual(turn.usage,
                         {"input": 100, "output": 20, "cache_read": 80})

    def test_turn_failed_surfaces_an_error(self):
        _, emitted, _, _ = self._run([{"type": "turn.failed", "error": "boom"}])
        self.assertEqual(emitted, [("error", {"message": "boom"})])

    def _cmd_provider(self):
        from providers.codex_provider import CodexProvider

        p = CodexProvider.__new__(CodexProvider)
        p.binary, p.model, p.workdir = "/bin/codex", "gpt-5.6-sol", "/repo"
        p.reset()
        return p

    def test_resume_rejects_every_exec_only_flag(self):
        """The general form of two bugs found in production use.

        `--cd` broke turn two, was fixed, and then `--approve-for-me` broke
        turn two again the same way. Both are exec-only. Assert the whole set
        rather than the two that happened to be found by hand.
        """
        from providers.codex_provider import EXEC_ONLY_FLAGS

        p = self._cmd_provider()
        p.session_id = "abc-123"
        argv, _ = p._command("follow up", [])
        leaked = set(argv) & EXEC_ONLY_FLAGS
        self.assertEqual(leaked, set(), f"exec-only flags sent to resume: {leaked}")

    def _with_mode(self, mode):
        import os

        previous = os.environ.get("STUDIO_CODEX_APPROVAL")
        os.environ["STUDIO_CODEX_APPROVAL"] = mode
        self.addCleanup(
            lambda: os.environ.__setitem__("STUDIO_CODEX_APPROVAL", previous)
            if previous is not None
            else os.environ.pop("STUDIO_CODEX_APPROVAL", None))
        return self._cmd_provider()

    def test_full_auto_is_the_default_and_works_on_both_subcommands(self):
        # The bypass flag is accepted by exec and resume alike, so unlike
        # --approve-for-me it needs no first-turn special case.
        p = self._cmd_provider()
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      p._command("hi", [])[0])
        p.session_id = "s1"
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      p._command("hi", [])[0])

    def test_auto_mode_keeps_the_sandbox_and_is_first_turn_only(self):
        p = self._with_mode("auto")
        self.assertIn("--approve-for-me", p._command("hi", [])[0])
        p.session_id = "s1"   # resumed sessions inherit the mode
        self.assertNotIn("--approve-for-me", p._command("hi", [])[0])

    def test_manual_mode_passes_no_approval_flag(self):
        argv = self._with_mode("manual")._command("hi", [])[0]
        self.assertNotIn("--approve-for-me", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_resume_never_passes_cd(self):
        # `codex exec` accepts --cd; `codex exec resume` rejects it. Passing it
        # made every follow-up turn fail with exit code 2 while the first
        # turn succeeded -- a failure only multi-turn use reveals.
        p = self._cmd_provider()
        p.session_id = "abc-123"
        argv, _ = p._command("follow up", [])
        self.assertNotIn("--cd", argv)

    def test_first_turn_never_passes_cd_either(self):
        # The working directory is set on the subprocess, which both honour.
        argv, _ = self._cmd_provider()._command("hello", [])
        self.assertNotIn("--cd", argv)

    def test_resume_puts_session_id_before_flags(self):
        p = self._cmd_provider()
        p.session_id = "abc-123"
        argv, _ = p._command("go", [])
        self.assertEqual(argv[1:4], ["exec", "resume", "abc-123"])

    def test_both_paths_carry_json_and_model(self):
        p = self._cmd_provider()
        fresh, _ = p._command("go", [])
        p.session_id = "s1"
        resumed, _ = p._command("go", [])
        for argv in (fresh, resumed):
            self.assertIn("--json", argv)
            self.assertIn("--model", argv)
            self.assertEqual(argv[-1], "go")

    def test_images_become_temp_files_on_both_paths(self):
        import base64
        import os

        png = base64.b64encode(b"not-really-a-png").decode()
        p = self._cmd_provider()
        argv, temporary = p._command("look", [png])
        try:
            self.assertEqual(argv.count("--image"), 1)
            self.assertTrue(os.path.exists(temporary[0]))
        finally:
            p._cleanup(temporary)
        self.assertFalse(os.path.exists(temporary[0]))

    def test_unknown_event_types_are_ignored(self):
        _, emitted, text, _ = self._run([{"type": "something.new", "x": 1}])
        self.assertEqual((emitted, text), ([], []))


class Capabilities(unittest.TestCase):
    """Image handling differs per backend and is advertised, not probed --
    discovering it at runtime means a failed turn."""

    def test_only_anthropic_takes_images_inside_tool_results(self):
        from providers.anthropic_provider import AnthropicProvider
        from providers.gemini_provider import GeminiProvider
        from providers.openai_provider import OpenAIProvider

        self.assertTrue(AnthropicProvider.tool_result_images)
        self.assertFalse(OpenAIProvider.tool_result_images)
        self.assertFalse(GeminiProvider.tool_result_images)

    def test_all_backends_accept_images_somewhere(self):
        from providers.anthropic_provider import AnthropicProvider
        from providers.gemini_provider import GeminiProvider
        from providers.openai_provider import OpenAIProvider

        for cls in (AnthropicProvider, OpenAIProvider, GeminiProvider):
            self.assertTrue(cls.supports_images, cls.__name__)


class BridgeSchemaInference(unittest.TestCase):
    """The bridge documents arguments in prose, so types are inferred. A wrong
    type is worse than an absent one -- it invalidates a correct value."""

    def setUp(self):
        import server
        self.infer = server._property_schema

    def test_unit_expression_is_a_string(self):
        self.assertEqual(self.infer("default '12 in'")["type"], "string")
        self.assertEqual(self.infer("Fusion unit expression string")["type"], "string")

    def test_boolean_default(self):
        self.assertEqual(self.infer("Cut through; default true.")["type"], "boolean")

    def test_numeric_default(self):
        self.assertEqual(self.infer("default 3")["type"], "number")

    def test_triple_is_an_array_of_strings(self):
        schema = self.infer("Optional [l, w, h] the assembly must fit inside.")
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["items"]["type"], "string")

    def test_description_is_always_preserved(self):
        text = "Body name; defaults to the first body."
        self.assertEqual(self.infer(text)["description"], text)

    def test_every_inferred_schema_has_a_type(self):
        for text in ("anything at all", "", "default '1 in'", "default 5"):
            self.assertIn("type", self.infer(text))


class ToolSpecGeneration(unittest.TestCase):
    def test_specs_are_generated_from_the_bridge_registry(self):
        import server
        specs = server.tool_specs({
            "make_thing": {"description": "Makes a thing.", "mutating": True,
                           "arguments": {"size": "default '1 in'"}},
            "read_thing": {"description": "Reads.", "mutating": False, "arguments": {}},
        })
        self.assertEqual([s.name for s in specs], ["make_thing", "read_thing"])
        # Mutating tools are labelled so the model knows the call has effects.
        self.assertIn("Modifies the design", specs[0].description)
        self.assertNotIn("Modifies the design", specs[1].description)
        self.assertEqual(specs[0].properties["size"]["type"], "string")

    def test_tool_with_no_arguments_still_yields_valid_schema(self):
        import server
        schema = server.tool_specs(
            {"health": {"description": "d", "arguments": {}}})[0].json_schema()
        self.assertEqual(schema, {"type": "object", "properties": {}})


if __name__ == "__main__":
    unittest.main()
