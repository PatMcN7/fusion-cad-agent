"""All clients preserve operation identity and expose nonblocking status."""

import unittest
from unittest.mock import Mock

from fusion_cad.bridge_client import Bridge, BridgeError, tool_schemas


class ClientTests(unittest.TestCase):
    def test_success_returns_the_generated_operation_id(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(return_value={"ok": True, "result": {}})
        result = bridge.call("cut", {}, True)
        payload = bridge._request.call_args.args[2]
        self.assertEqual(result["result"]["operation_id"], payload["operation_id"])

    def test_retry_id_is_removed_from_modeling_arguments(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(return_value={"ok": True, "result": {}})
        bridge.call("cut", {"operation_id": "retry", "depth": "1 in"}, True)
        payload = bridge._request.call_args.args[2]
        self.assertEqual(payload["operation_id"], "retry")
        self.assertEqual(payload["arguments"], {"depth": "1 in"})

    def test_lost_response_keeps_id_and_prevents_new_mutation(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(side_effect=BridgeError("lost response"))
        result = bridge.call("cut", {}, True, operation_id="lost")
        self.assertEqual(result["error"]["operation_id"], "lost")
        result = bridge.call("cut", {}, True)
        self.assertEqual(result["error"]["code"], "outcome_unknown")
        self.assertEqual(bridge._request.call_count, 1)

    def test_status_routes_directly_without_registry_lookup(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._unresolved = "lost"
        bridge._request = Mock(return_value={"ok": True, "operation": {
            "operation_id": "lost", "state": "succeeded", "response": {"ok": True}}})
        result = bridge.call("operation_status", {"operation_id": "lost"}, False)
        self.assertEqual(bridge._request.call_args.args, ("GET", "/v1/operations/lost"))
        self.assertEqual(result["result"]["state"], "succeeded")
        self.assertIsNone(bridge._unresolved)

    def test_pending_operation_uses_cached_tool_list(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(return_value={"ok": True, "tools": {"cut": {"mutating": True}}})
        bridge.registry()
        bridge._unresolved = "pending"
        bridge._request.side_effect = AssertionError("would block behind kernel")
        self.assertIn("operation_status", bridge.registry())

    def test_cancellation_needs_confirmation_but_does_not_create_an_operation(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(return_value={"ok": True, "cancelled": True,
                                            "operation": {"state": "failed"}})
        bridge.call("cancel_operation", {"operation_id": "queued"}, False)
        bridge._request.assert_not_called()
        bridge.call("cancel_operation", {"operation_id": "queued"}, True)
        self.assertEqual(bridge._request.call_args.args, ("POST", "/v1/operations/queued/cancel"))

    def test_schemas_preserve_nested_types_and_expose_retry_identity(self):
        spec = tool_schemas({"cut": {"mutating": True, "arguments": {
            "polygons": {"type": "array", "items": {"type": "array"}}}}})[0]
        props = spec["input_schema"]["properties"]
        self.assertEqual(props["polygons"]["items"]["type"], "array")
        self.assertEqual(props["operation_id"]["type"], "string")


    def test_retry_keeps_original_document_after_a_tool_switches_documents(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(side_effect=[
            {"ok": True, "result": {}, "document_id": "doc-B"},
            {"ok": True, "result": {}, "document_id": "doc-B"},
        ])
        bridge.call("create_test_box", {}, True, operation_id="create")
        self.assertEqual(bridge._document_id, "doc-B")
        bridge.call("create_test_box", {}, True, operation_id="create")
        for call in bridge._request.call_args_list:
            self.assertEqual(call.args[2]["document_id"], "doc-A")

    def test_registry_refresh_does_not_silently_rebind_a_chat(self):
        bridge = Bridge()
        bridge._document_id = "doc-A"
        bridge._request = Mock(return_value={"ok": True, "tools": {}, "document_id": "doc-B"})
        bridge.registry()
        self.assertEqual(bridge._document_id, "doc-A")
        bridge._request.return_value = {"ok": True, "result": {}, "document_id": "doc-B"}
        bridge.call("design_summary", {}, False)
        self.assertEqual(bridge._document_id, "doc-B")

    def test_invalid_control_ids_return_structured_errors(self):
        bridge = Bridge()
        bridge._request = Mock(side_effect=AssertionError("invalid input reached transport"))
        for name in ("operation_status", "cancel_operation"):
            result = bridge.call(name, {"operation_id": "../escape"}, True)
            self.assertEqual(result["error"]["code"], "bad_request")
