"""Durability, duplicate delivery, and interrupted operation behavior."""

import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import execution
from errors import ProtocolError


def request(identifier="test-operation", **changes):
    return {"tool": "cut", "arguments": {"depth": "1 in"}, "confirm": True,
            "operation_id": identifier, **changes}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.journal = execution.Journal(self.path)

    def test_duplicate_delivery_has_one_owner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.journal.submit(request()), range(16)))
        self.assertEqual(sum(fresh for _, fresh in results), 1)
        self.assertTrue(self.journal.start("test-operation"))
        self.assertFalse(self.journal.start("test-operation"))

    def test_id_cannot_be_reused_for_different_arguments(self):
        self.journal.submit(request())
        with self.assertRaises(ProtocolError):
            self.journal.submit(request(arguments={"depth": "2 in"}))

    def test_transport_correlation_is_not_operation_identity(self):
        self.journal.submit(request(request_id="first"))
        _, fresh = self.journal.submit(request(request_id="second"))
        self.assertFalse(fresh)

    def test_result_survives_restart_and_cannot_execute_again(self):
        self.journal.submit(request())
        self.journal.start("test-operation")
        result = {"ok": True, "result": {"volume": 12.3}}
        self.journal.finish("test-operation", result)
        restarted = execution.Journal(self.path)
        record, fresh = restarted.submit(request())
        self.assertFalse(fresh)
        self.assertEqual(execution.pending_response(record), result)
        self.assertFalse(restarted.start("test-operation"))

    def test_both_queued_and_running_become_unknown_after_restart(self):
        for identifier in ("queued", "running"):
            self.journal.submit(request(identifier))
        self.journal.start("running")
        restarted = execution.Journal(self.path)
        for identifier in ("queued", "running"):
            record, fresh = restarted.submit(request(identifier))
            self.assertFalse(fresh)
            self.assertEqual(record["state"], "outcome_unknown")
            self.assertFalse(restarted.start(identifier))

    def test_cancel_only_prevents_work_that_has_not_started(self):
        self.journal.submit(request())
        self.assertTrue(self.journal.cancel("test-operation"))
        self.assertFalse(self.journal.start("test-operation"))
        self.journal.submit(request("running"))
        self.journal.start("running")
        self.assertFalse(self.journal.cancel("running"))
        self.assertEqual(self.journal.get("running")["state"], "running")

    def test_failed_operation_keeps_recovery_evidence(self):
        self.journal.submit(request())
        self.journal.start("test-operation")
        failure = {"ok": False, "error": {"recovery": {"status": "partial"}}}
        self.journal.finish("test-operation", failure)
        self.assertEqual(self.journal.get("test-operation")["response"], failure)

    def test_payload_is_not_written_to_journal(self):
        self.journal.submit(request(arguments={"text": "private-project-source"}))
        self.assertNotIn("private-project-source", (self.path / "test-operation.json").read_text())

    def test_invalid_id_and_nonfinite_payload_are_rejected(self):
        for identifier in (None, "../escape", "a/b", "", "x" * 129):
            with self.assertRaises(ProtocolError):
                self.journal.submit(request(identifier))
        with self.assertRaises(ProtocolError):
            self.journal.submit(request(arguments={"depth": float("nan")}))
        self.assertEqual(list(self.path.iterdir()), [])

    def test_corrupt_record_does_not_allow_silent_replay(self):
        self.journal.submit(request())
        (self.path / "test-operation.json").write_text("invalid json")
        with self.assertRaises(json.JSONDecodeError):
            execution.Journal(self.path)


class BridgeDispatchTests(unittest.TestCase):
    """Drive real HTTP-handler/dispatch methods with a manually pumped event queue.

    This verifies lifetime and serialization logic without emulating the kernel.
    """

    @classmethod
    def setUpClass(cls):
        from test_registry import _install_adsk_stub
        _install_adsk_stub()
        import adsk.core
        source = Path(__file__).resolve().parents[1] / "FusionCadAgent.py"
        spec = importlib.util.spec_from_file_location("bridge_dispatch_test", source)
        cls.shell = importlib.util.module_from_spec(spec)
        with patch.object(adsk.core, "CustomEventHandler", object):
            spec.loader.exec_module(cls.shell)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shell._journal = execution.Journal(Path(self.temp.name))
        self.shell._pending.clear()
        self.shell._ui = None
        self.shell.REQUEST_TIMEOUT_SECONDS = 0.001
        self.shell._log = Mock()
        self.events = []
        self.shell._app = SimpleNamespace(activeDocument=object(),
            fireCustomEvent=lambda event, identifier: self.events.append(identifier))
        self.real_dispatch = self.shell._dispatch
        self.dispatch = Mock(return_value={"ok": True, "result": {"built": 1}})
        patcher = patch.object(self.shell, "_dispatch", self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, payload):
        handler = object.__new__(self.shell.BridgeRequestHandler)
        handler._send = Mock()
        payload = dict(payload)
        payload.setdefault("document_id", self.shell._document_bindings.identify(self.shell._app.activeDocument))
        handler._submit(payload)
        return handler._send.call_args.args

    def pump(self):
        self.shell.ExecuteEventHandler().notify(SimpleNamespace(additionalInfo=self.events.pop(0)))

    def test_timeout_keeps_work_and_duplicate_does_not_enqueue_again(self):
        status, response = self.submit(request())
        self.assertEqual(status, 202)
        self.assertEqual(response["error"]["code"], "operation_pending")
        self.submit(request())
        self.assertEqual(self.events, ["test-operation"])
        self.pump()
        self.assertEqual(self.dispatch.call_count, 1)
        status, response = self.submit(request())
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(self.dispatch.call_count, 1)
        self.assertEqual(self.shell._pending, {})

    def test_status_does_not_wait_for_fusion_event(self):
        self.submit(request())
        handler = object.__new__(self.shell.BridgeRequestHandler)
        handler.path = "/v1/operations/test-operation"
        handler._require_auth = lambda: True
        handler._send = Mock()
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[1]["operation"]["state"], "queued")
        self.dispatch.assert_not_called()

    def test_cancelled_event_does_not_execute(self):
        self.submit(request())
        self.shell._journal.cancel("test-operation")
        self.pump()
        self.dispatch.assert_not_called()
        _, result = self.submit(request())
        self.assertEqual(result["error"]["code"], "cancelled")

    def test_unknown_operation_is_never_enqueued(self):
        self.submit(request())
        self.shell._journal.unknown("test-operation", "test crash")
        self.shell._pending.clear()
        self.events.clear()
        _, result = self.submit(request())
        self.assertEqual(result["error"]["code"], "outcome_unknown")
        self.assertEqual(self.events, [])

    def test_failure_carries_recovery_into_durable_result(self):
        from errors import ToolFailure
        failure = ToolFailure("bad cut")
        failure.recovery = {"status": "partial", "completed_stages": ["plate"]}
        failure.design = {"body_count": 2}
        self.dispatch.side_effect = failure
        self.submit(request())
        self.pump()
        result = self.shell._journal.get("test-operation")["response"]
        self.assertEqual(result["error"]["recovery"], failure.recovery)
        self.assertEqual(result["error"]["design"], failure.design)

    def test_persistence_failure_after_execution_is_not_success(self):
        self.submit(request())
        with patch.object(self.shell._journal, "finish", side_effect=OSError("disk unavailable")):
            self.pump()
        self.assertEqual(self.shell._journal.get("test-operation")["state"], "outcome_unknown")
        self.assertEqual(self.dispatch.call_count, 1)


    def test_switching_tabs_before_dispatch_rejects_the_mutation(self):
        first, second = object(), object()
        self.shell._app.activeDocument = first
        identifier = self.shell._document_bindings.identify(first)
        mutate = Mock(return_value={"built": 1})
        registry = SimpleNamespace(TOOLS={"cut": {"mutating": True}}, HANDLERS={"cut": mutate})
        self.dispatch.side_effect = self.real_dispatch
        with patch.object(self.shell, "_reload_if_changed", return_value=[]), patch.object(
                self.shell, "_tools_module", return_value=registry):
            self.submit(request(document_id=identifier))
            self.shell._app.activeDocument = second
            self.pump()
        mutate.assert_not_called()
        record = self.shell._journal.get("test-operation")
        self.assertEqual(record["state"], "failed")
        self.assertIn("Active document changed", record["response"]["error"]["message"])


class DocumentBindingTests(unittest.TestCase):
    def test_same_named_documents_get_distinct_ids(self):
        class Document:
            name = "Untitled"
            isValid = True
        first, second = Document(), Document()
        bindings = execution.DocumentBindings()
        first_id = bindings.identify(first)
        self.assertEqual(bindings.identify(first), first_id)
        self.assertNotEqual(bindings.identify(second), first_id)
        with self.assertRaises(ProtocolError):
            bindings.require(second, first_id)

    def test_restart_and_no_document_are_explicit_identities(self):
        bindings = execution.DocumentBindings()
        self.assertEqual(bindings.identify(None), bindings.identify(None))
        self.assertNotEqual(bindings.identify(None), execution.DocumentBindings().identify(None))

    def test_closed_document_reference_is_not_reused(self):
        document = SimpleNamespace(isValid=True)
        bindings = execution.DocumentBindings()
        old = bindings.identify(document)
        document.isValid = False
        fresh = SimpleNamespace(isValid=True)
        self.assertNotEqual(bindings.identify(fresh), old)
