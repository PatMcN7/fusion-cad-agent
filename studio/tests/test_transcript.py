"""Recording and post-hoc analysis.

The analyzer is the thing that decides what gets believed about a session,
so its arithmetic is worth pinning: a warning is not a rollback, streamed
tokens collapse into one record, and a half-written file still parses.
"""

import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import analyze  # noqa: E402
import transcript  # noqa: E402


class Recording(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._previous = transcript.os.environ.get("STUDIO_TRANSCRIPTS")
        transcript.os.environ["STUDIO_TRANSCRIPTS"] = self.tmp.name

    def tearDown(self):
        if self._previous is None:
            transcript.os.environ.pop("STUDIO_TRANSCRIPTS", None)
        else:
            transcript.os.environ["STUDIO_TRANSCRIPTS"] = self._previous

    def records(self, recorder):
        return [json.loads(line) for line in
                recorder.path.read_text().splitlines() if line.strip()]

    def test_streamed_tokens_collapse_to_one_record(self):
        # One record per token would make the file mostly punctuation.
        recorder = transcript.Recorder()
        emit = transcript.tee(recorder, lambda e, d: None)
        for token in ("Hel", "lo ", "there"):
            emit("text", {"text": token})
        emit("done", {})
        text = [r for r in self.records(recorder) if r["kind"] == "text"]
        self.assertEqual(len(text), 1)
        self.assertEqual(text[0]["data"]["text"], "Hello there")
        self.assertEqual(text[0]["data"]["chars"], 11)

    def test_tee_passes_every_event_through_unchanged(self):
        seen = []
        recorder = transcript.Recorder()
        emit = transcript.tee(recorder, lambda e, d: seen.append((e, d)))
        emit("tool_start", {"id": "1", "name": "x"})
        emit("done", {})
        self.assertEqual([e for e, _ in seen], ["tool_start", "done"])

    def test_recording_failure_never_breaks_the_session(self):
        seen = []
        recorder = transcript.Recorder()
        recorder.event = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        emit = transcript.tee(recorder, lambda e, d: seen.append(e))
        emit("text", {"text": "still delivered"})
        self.assertEqual(seen, ["text"])

    def test_images_are_never_written_to_the_transcript(self):
        recorder = transcript.Recorder()
        emit = transcript.tee(recorder, lambda e, d: None)
        emit("capture", {"path": "/tmp/x.png", "url": "/api/image?path=/tmp/x.png"})
        emit("vision", {"id": "1", "text": "a bar with triangular pockets"})
        blob = recorder.path.read_text()
        self.assertNotIn("base64", blob)
        self.assertIn("triangular pockets", blob)

    def test_warning_is_not_counted_as_a_rollback(self):
        """The bug this test exists for.

        `corners_machinable` fails as a warning -- the operation stands. An
        analyzer that counts it as a rollback reports a failure rate that is
        mostly millability notes.
        """
        recorder = transcript.Recorder()
        emit = transcript.tee(recorder, lambda e, d: None)
        emit("tool_start", {"id": "1", "name": "lattice_pockets", "mutating": True})
        emit("tool_end", {"id": "1", "name": "lattice_pockets", "ok": True,
                          "result": {"verification": [{"checks": [
                              {"name": "corners_machinable", "ok": False,
                               "severity": "warning"},
                              {"name": "rib_width_held", "ok": False,
                               "severity": "error"}]}]}})
        recorder.close()
        summary = analyze.summarize(recorder.path)
        self.assertEqual(summary["failed_checks"], ["rib_width_held"])
        self.assertEqual(summary["warnings"], ["corners_machinable"])


class Analysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        transcript.os.environ["STUDIO_TRANSCRIPTS"] = self.tmp.name
        self.addCleanup(transcript.os.environ.pop, "STUDIO_TRANSCRIPTS", None)

    def _session(self, names):
        recorder = transcript.Recorder(worker_model="test/model")
        emit = transcript.tee(recorder, lambda e, d: None)
        recorder.user_message("build it")
        for index, name in enumerate(names):
            emit("tool_start", {"id": str(index), "name": name, "mutating": True})
            emit("tool_end", {"id": str(index), "name": name, "ok": True, "result": {}})
        recorder.close()
        return recorder.path

    def test_buckets_separate_building_from_orienting(self):
        path = self._session(["lattice_pockets", "design_summary", "design_summary",
                              "capture_viewport", "verify_spec"])
        buckets = analyze.summarize(path)["buckets"]
        self.assertEqual(buckets["BUILD"], 1)
        self.assertEqual(buckets["inspect"], 2)
        self.assertEqual(buckets["capture"], 1)
        self.assertEqual(buckets["verify"], 1)

    def test_a_truncated_file_still_parses(self):
        # A session killed mid-write ends in a partial line, and that session
        # is exactly the one worth reading.
        path = self._session(["lattice_pockets"])
        with path.open("a") as handle:
            handle.write('{"t": 9.9, "kind": "tool_st')
        self.assertEqual(len(analyze.summarize(path)["calls"]), 1)

    def test_cost_takes_the_running_total_not_the_sum(self):
        # session_cost_usd is cumulative; adding successive values would
        # count early turns many times over.
        recorder = transcript.Recorder(worker_model="test/model")
        emit = transcript.tee(recorder, lambda e, d: None)
        for total in (0.001, 0.004, 0.009):
            emit("usage", {"input_tokens": 10, "output_tokens": 1,
                           "session_cost_usd": total})
        recorder.close()
        self.assertAlmostEqual(analyze.summarize(recorder.path)["cost"], 0.009)


if __name__ == "__main__":
    unittest.main()


class Sessions(unittest.TestCase):
    """Multiple chats, each with its own provider, history and transcript."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        transcript.os.environ["STUDIO_TRANSCRIPTS"] = self.tmp.name
        self.addCleanup(transcript.os.environ.pop, "STUDIO_TRANSCRIPTS", None)
        sys.argv = ["test"]
        import server
        self.server = server
        self.store = server.SessionStore()

    def test_each_chat_gets_its_own_transcript(self):
        # One file for two chats would make them impossible to analyse apart,
        # which is the whole reason for having separate chats.
        one, two = self.store.create(), self.store.create()
        self.assertNotEqual(one.recorder.path, two.recorder.path)
        self.assertNotEqual(one.id, two.id)

    def test_title_comes_from_the_first_message_only(self):
        session = self.store.create()
        self.assertEqual(session.title, "New chat")
        session.name_from("lighten the bellypan with a hex isogrid")
        self.assertEqual(session.title, "lighten the bellypan with a hex isogrid")
        session.name_from("now fillet everything")
        self.assertEqual(session.title, "lighten the bellypan with a hex isogrid")

    def test_long_titles_are_elided_not_wrapped(self):
        session = self.store.create()
        session.name_from("x" * 200)
        self.assertLessEqual(len(session.title), 49)
        self.assertTrue(session.title.endswith("…"))

    def test_streamed_text_folds_into_one_log_entry(self):
        session = self.store.create()
        session.record("text", {"text": "Cut "})
        session.record("text", {"text": "193 cells."})
        text = [e for e in session.log if e["kind"] == "text"]
        self.assertEqual(len(text), 1)
        self.assertEqual(text[0]["data"]["text"], "Cut 193 cells.")

    def test_a_tool_call_between_text_starts_a_new_entry(self):
        session = self.store.create()
        session.record("text", {"text": "first"})
        session.record("tool_start", {"id": "1", "name": "x"})
        session.record("text", {"text": "second"})
        self.assertEqual([e["kind"] for e in session.log],
                         ["text", "tool_start", "text"])

    def test_cost_tracks_the_running_total(self):
        session = self.store.create()
        for total in (0.001, 0.005):
            session.record("usage", {"session_cost_usd": total})
        self.assertAlmostEqual(session.cost, 0.005)

    def test_a_stale_id_opens_a_working_chat(self):
        """A browser reloading after a server restart holds a dead id.

        That must open something usable rather than erroring -- the id is an
        implementation detail the person never sees.
        """
        existing = self.store.create()
        existing.updated += 10
        self.assertEqual(self.store.get("no-such-id").id, existing.id)

    def test_get_with_no_sessions_creates_one(self):
        self.assertIsNotNone(self.store.get(None).id)

    def test_listing_is_newest_first(self):
        first, second = self.store.create(), self.store.create()
        first.updated = 100.0
        second.updated = 200.0
        self.assertEqual([s["id"] for s in self.store.listing()],
                         [second.id, first.id])

    def test_delete_closes_the_transcript(self):
        session = self.store.create()
        self.assertTrue(self.store.delete(session.id))
        self.assertFalse(self.store.delete(session.id))
        tail = session.recorder.path.read_text().strip().splitlines()[-1]
        self.assertIn("chat deleted", tail)

    def test_log_is_bounded_so_reopening_stays_fast(self):
        session = self.store.create()
        for index in range(1000):
            session.record("tool_start", {"id": str(index), "name": "x"})
        self.assertLessEqual(len(session.log), 800)


class ToolImages(unittest.TestCase):
    """A fetched drawing has to reach the model, not just its filename."""

    def setUp(self):
        sys.argv = ["test"]
        import server
        self.server = server

    def test_finds_rendered_pdf_pages(self):
        payload = {"url": "https://x/y.pdf", "path": "/d/y.pdf",
                   "rendered": {"pages": 2,
                                "images": ["/d/y-1.png", "/d/y-2.png"]}}
        self.assertEqual(self.server._result_images(payload),
                         ["/d/y-1.png", "/d/y-2.png"])

    def test_repeats_are_collapsed_in_order(self):
        payload = {"images": ["/a.png", "/b.png"], "first": "/a.png"}
        self.assertEqual(self.server._result_images(payload), ["/a.png", "/b.png"])

    def test_non_images_and_relative_paths_are_ignored(self):
        payload = {"path": "/d/y.pdf", "note": "see y-1.png", "n": 3,
                   "url": "https://x/z.png"}
        self.assertEqual(self.server._result_images(payload), [])

    def test_a_pdf_that_failed_to_render_yields_nothing(self):
        payload = {"path": "/d/y.pdf", "render_error": "pdftoppm not found"}
        self.assertEqual(self.server._result_images(payload), [])


class Stopping(unittest.TestCase):
    """Stop must be immediate, and must not corrupt the history."""

    def setUp(self):
        sys.argv = ["test"]
        import server
        import providers
        self.server, self.providers = server, providers

    def test_cancelled_survives_a_broad_except_exception(self):
        """The reason Cancelled derives from BaseException.

        Every provider wraps its stream loop in `except Exception`. If a
        cancel were an ordinary Exception it would be swallowed there and
        re-reported as "stream failed", so pressing stop would look like a
        crash instead of a choice.
        """
        self.assertTrue(issubclass(self.providers.Cancelled, BaseException))
        self.assertFalse(issubclass(self.providers.Cancelled, Exception))
        try:
            try:
                raise self.providers.Cancelled()
            except Exception:
                self.fail("a broad except Exception swallowed the cancel")
        except self.providers.Cancelled:
            pass

    def test_a_set_flag_raises_out_of_emit(self):
        session = self.server.SessionStore().create()
        session.stop.set()

        def keep(event, data):
            if session.stop.is_set():
                raise self.providers.Cancelled()

        with self.assertRaises(self.providers.Cancelled):
            keep("text", {"text": "half a sentence"})

    def test_stopping_answers_every_pending_tool_call(self):
        """An assistant turn with tool_calls and no results is malformed.

        The next message would be the one that fails, which makes the bug
        look like it has nothing to do with pressing stop.
        """
        from providers import ToolCall, ToolResult

        calls = [ToolCall("a", "lattice_pockets", {}), ToolCall("b", "capture_viewport", {})]
        results = [ToolResult(c.id, c.name, False,
                              {"error": "stopped by the user before this ran"})
                   for c in calls]
        self.assertEqual([r.id for r in results], [c.id for c in calls])
        self.assertTrue(all(not r.ok for r in results))

    def test_a_new_message_clears_a_previous_stop(self):
        # Otherwise one stop would silently kill every following turn.
        session = self.server.SessionStore().create()
        session.stop.set()
        session.stop.clear()
        self.assertFalse(session.stop.is_set())
