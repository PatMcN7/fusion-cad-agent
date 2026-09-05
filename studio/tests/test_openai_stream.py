"""Replay OpenAI's streaming wire format against the adapter.

The risky part of this adapter is not the request shape, it is reassembly:
OpenAI streams tool arguments as a JSON *string* split into arbitrary
fragments, indexed per call, with the name arriving in one chunk and the id
possibly in another. Getting that wrong produces malformed arguments only for
certain payload sizes, which a single happy-path live call would not reveal.

These fakes mimic the documented chunk shape, so the parsing is exercised with
no key, no credits, and no network.
"""

import sys
import types
import unittest
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent))

from providers import ProviderError  # noqa: E402
from providers.openai_provider import OpenAIProvider  # noqa: E402


def chunk(content=None, calls=None, finish=None, reasoning=None):
    """One `chat.completion.chunk` as the SDK surfaces it."""
    delta = types.SimpleNamespace(content=content, tool_calls=calls)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice])


def call_fragment(index, cid=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=cid, function=fn)


def provider_with(chunks):
    """An adapter whose HTTP layer is replaced by a canned stream."""
    p = OpenAIProvider.__new__(OpenAIProvider)
    p.model, p.base_url, p.effort = "gpt-4o", None, "high"
    p.reset()
    p.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: iter(chunks))))
    return p


class StreamedToolCalls(unittest.TestCase):
    def test_arguments_split_across_fragments_reassemble(self):
        p = provider_with([
            chunk(calls=[call_fragment(0, "call_a", "create_test_box", '{"wid')]),
            chunk(calls=[call_fragment(0, arguments='th": "2 ')]),
            chunk(calls=[call_fragment(0, arguments='in"}')]),
            chunk(finish="tool_calls"),
        ])
        turn = p.stream("sys", [], lambda *a: None)
        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].arguments, {"width": "2 in"})
        self.assertEqual(turn.tool_calls[0].id, "call_a")

    def test_parallel_calls_interleaved_by_index(self):
        # Fragments for different calls arrive interleaved; they must not mix.
        p = provider_with([
            chunk(calls=[call_fragment(0, "a", "health", "{}")]),
            chunk(calls=[call_fragment(1, "b", "design_summary", '{"x')]),
            chunk(calls=[call_fragment(1, arguments='": 1}')]),
            chunk(finish="tool_calls"),
        ])
        turn = p.stream("sys", [], lambda *a: None)
        self.assertEqual([c.name for c in turn.tool_calls], ["health", "design_summary"])
        self.assertEqual(turn.tool_calls[1].arguments, {"x": 1})

    def test_empty_arguments_become_empty_dict(self):
        p = provider_with([
            chunk(calls=[call_fragment(0, "a", "health", None)]),
            chunk(finish="tool_calls"),
        ])
        turn = p.stream("sys", [], lambda *a: None)
        self.assertEqual(turn.tool_calls[0].arguments, {})

    def test_malformed_json_is_surfaced_not_raised(self):
        # A truncated fragment must not kill the turn -- the agent loop turns
        # this marker into an error result so the model can retry.
        p = provider_with([
            chunk(calls=[call_fragment(0, "a", "create_test_box", '{"width": ')]),
            chunk(finish="tool_calls"),
        ])
        turn = p.stream("sys", [], lambda *a: None)
        self.assertIn("__parse_error__", turn.tool_calls[0].arguments)

    def test_missing_id_gets_a_synthesized_one(self):
        # Some OpenAI-compatible servers omit the id entirely.
        p = provider_with([
            chunk(calls=[call_fragment(0, None, "health", "{}")]),
            chunk(finish="tool_calls"),
        ])
        turn = p.stream("sys", [], lambda *a: None)
        self.assertTrue(turn.tool_calls[0].id)

    def test_tool_pending_announced_once_per_call(self):
        events = []
        p = provider_with([
            chunk(calls=[call_fragment(0, "a", "health", "{")]),
            chunk(calls=[call_fragment(0, arguments="}")]),
            chunk(finish="tool_calls"),
        ])
        p.stream("sys", [], lambda kind, data: events.append((kind, data)))
        self.assertEqual([k for k, _ in events].count("tool_pending"), 1)


class StreamedText(unittest.TestCase):
    def test_text_accumulates_and_streams(self):
        events = []
        p = provider_with([chunk(content="Hel"), chunk(content="lo"), chunk(finish="stop")])
        turn = p.stream("sys", [], lambda k, d: events.append((k, d)))
        self.assertEqual(turn.text, "Hello")
        self.assertEqual(turn.stop_reason, "end")
        self.assertEqual([d["text"] for k, d in events if k == "text"], ["Hel", "lo"])

    def test_reasoning_trace_surfaces_as_thinking(self):
        events = []
        p = provider_with([chunk(reasoning="pondering"), chunk(content="hi"),
                           chunk(finish="stop")])
        p.stream("sys", [], lambda k, d: events.append((k, d)))
        kinds = [k for k, _ in events]
        self.assertIn("thinking_start", kinds)
        self.assertIn("thinking", kinds)

    def test_length_finish_maps_to_length(self):
        p = provider_with([chunk(content="x"), chunk(finish="length")])
        self.assertEqual(p.stream("sys", [], lambda *a: None).stop_reason, "length")

    def test_empty_choices_chunk_is_ignored(self):
        # Some proxies emit a keepalive chunk with no choices.
        p = provider_with([types.SimpleNamespace(choices=[]),
                           chunk(content="ok"), chunk(finish="stop")])
        self.assertEqual(p.stream("sys", [], lambda *a: None).text, "ok")


class HistoryShape(unittest.TestCase):
    def test_assistant_turn_records_native_tool_calls(self):
        p = provider_with([
            chunk(calls=[call_fragment(0, "a", "health", "{}")]),
            chunk(finish="tool_calls"),
        ])
        p.stream("sys", [], lambda *a: None)
        last = p.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertEqual(last["tool_calls"][0]["function"]["name"], "health")
        # Arguments must go back as the raw string OpenAI sent, not a dict.
        self.assertIsInstance(last["tool_calls"][0]["function"]["arguments"], str)

    def test_images_follow_as_a_user_turn_not_inside_the_tool_message(self):
        from providers import ToolResult

        p = provider_with([chunk(finish="stop")])
        p.add_tool_results([ToolResult("a", "health", True, {"ok": 1}, image_b64="QUJD")])
        self.assertEqual(p.messages[0]["role"], "tool")
        self.assertIsInstance(p.messages[0]["content"], str)  # text only
        self.assertEqual(p.messages[1]["role"], "user")
        self.assertTrue(any(b["type"] == "image_url" for b in p.messages[1]["content"]))

    def test_result_without_image_adds_no_extra_turn(self):
        from providers import ToolResult

        p = provider_with([chunk(finish="stop")])
        p.add_tool_results([ToolResult("a", "health", True, {"ok": 1})])
        self.assertEqual([m["role"] for m in p.messages], ["tool"])


class Failure(unittest.TestCase):
    def test_transport_error_becomes_provider_error(self):
        p = OpenAIProvider.__new__(OpenAIProvider)
        p.model, p.base_url, p.effort = "gpt-4o", None, "high"
        p.reset()

        def boom(**kw):
            raise RuntimeError("connection reset")

        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=boom)))
        with self.assertRaises(ProviderError):
            p.stream("sys", [], lambda *a: None)


if __name__ == "__main__":
    unittest.main()
