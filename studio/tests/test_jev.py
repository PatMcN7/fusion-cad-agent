"""No keys, network, Fusion, or other models needed for these harness tests."""
import copy
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import providers
import server
from providers import Cancelled, ProviderError, ToolResult
from providers.jev_provider import JevProvider, WORKFLOWS, DIRECTIONS, choice
from fusion_cad.spatial_contracts import SPECS

FACES = [{"ref": "picked_a", "tag": "A", "label": "Start", "token": "private-native-token"},
         {"ref": "picked_b", "tag": "B", "label": "End"}]


def answer(options, selected, probability=1.0, confidence=1.0):
    probabilities = {k: (probability if k == selected else (1-probability)/(len(options)-1)) for k in options}
    return {"type": "choice", "choice": selected, "confidence": confidence, "probabilities": probabilities}


def response(workflow="minimum", direction="ambiguous", **kwargs):
    return {"model": "jev-1.13.0", "answers": {
        "workflow": answer(WORKFLOWS, workflow, **kwargs),
        "direction": answer(DIRECTIONS, direction)},
        "usage": {"input_tokens": 120, "output_tokens": 40}}


def registry():
    result = {name: {"mutating": mutating, "description": description, "input_schema": schema}
              for name, (mutating, description, schema) in SPECS.items()}
    for name, props in {
        "get_selected_faces": {},
        "resolve_selected_faces": {"refs": {"type": "array", "items": {"type": "string"}}},
        "operation_status": {"operation_id": {"type": "string"}},
    }.items():
        result[name] = {"mutating": False, "description": name,
                        "input_schema": {"type": "object", "properties": props}}
    schemas = json.loads((Path(__file__).resolve().parents[2] / 'fusion_cad' / 'tool_schemas.json').read_text())
    mutations = {'create_test_box', 'extrude_polygons', 'create_document', 'set_6061_material', 'bodies_to_components', 'undo_last'}
    for name in ('design_summary', 'list_joint_candidates', 'check_interference', 'list_documents', *mutations):
        result[name] = {'description': name, 'mutating': name in mutations, 'input_schema': schemas[name]}
    result['feature_tree'] = {'description': 'features', 'mutating': False, 'input_schema': {'type': 'object', 'properties': {}}}
    return result


class Harness(unittest.TestCase):
    def setup_run(self, reply=None, faces=None, outcomes=None):
        provider = JevProvider("jev-1.13.0", api_key="test-not-a-real-key")
        provider._evaluate = Mock(return_value=reply or response())
        selected = FACES if faces is None else faces
        outcomes = outcomes or {}
        def call(name, arguments, confirm=False):
            if name in outcomes:
                return copy.deepcopy(outcomes[name])
            if name in {"get_selected_faces", "resolve_selected_faces"}:
                payload = {"faces": copy.deepcopy(selected), "document_id": "doc-1"}
            elif name == "measure_geometry":
                payload = {"distance_cm": 2.5, "mode": arguments["mode"], "units": "cm"}
            else:
                payload = {"body": "New body", "verification": {"passed_checks": ["solid"]}}
            return {"ok": True, "result": payload}
        bridge = SimpleNamespace(registry=Mock(side_effect=registry), call=Mock(side_effect=call))
        events = []
        return provider, bridge, events

    def run_agent(self, provider, bridge, events, attachments=False, text="Measure the distance between these faces"):
        blocks = [{"type": "text", "text": text}]
        if attachments:
            blocks.append({"type": "text", "text": "face metadata", "fusion_faces": copy.deepcopy(FACES)})
        with patch.object(server.Agent, "_capture") as capture, patch.object(server, "DESCRIBER") as vision:
            server.Agent(bridge, provider).run(blocks, lambda e, d: events.append((e, d)))
            capture.assert_not_called()
            vision.describe.assert_not_called()
        return [c.args[0] for c in bridge.call.call_args_list]

    def test_measure_one_model_call_native_arguments_no_images(self):
        p, b, events = self.setup_run()
        names = self.run_agent(p, b, events)
        self.assertEqual(names, ["get_selected_faces", "measure_geometry"])
        p._evaluate.assert_called_once()
        self.assertEqual(b.call.call_args.args[1], {"a": {"face_ref": "picked_a"}, "b": {"face_ref": "picked_b"}, "mode": "minimum"})
        self.assertFalse(b.call.call_args.kwargs["confirm"])
        self.assertNotIn("private-native-token", json.dumps(p._evaluate.call_args.args))
        self.assertTrue(any(e == "jev_decision" for e, _ in events))
        self.assertTrue(all("seconds" in d for e, d in events if e == "tool_end"))

    def test_plane_mode_is_not_closest_distance(self):
        p, b, events = self.setup_run(response("planes"))
        self.run_agent(p, b, events)
        self.assertEqual(b.call.call_args.args[1]["mode"], "plane_separation")

    def test_attached_faces_override_current_selection_and_keep_order(self):
        p, b, events = self.setup_run(response("extrude", "second_to_first"), faces=list(reversed(FACES)))
        names = self.run_agent(p, b, events, attachments=True)
        self.assertEqual(names, ["resolve_selected_faces", "resolve_selected_faces", "extrude_between_faces"])
        self.assertEqual(b.call.call_args.args[1], {"start": {"face_ref": "picked_b"}, "end": {"face_ref": "picked_a"}})
        self.assertTrue(b.call.call_args.kwargs["confirm"])
        p._evaluate.assert_called_once()
        self.assertIn("no visual review", " ".join(d["text"] for e, d in events if e == "text"))

    def test_inspect_uses_native_selection_report_without_extra_inspection(self):
        p, b, events = self.setup_run(response("inspect"))
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])
        self.assertTrue(any("native Fusion" in d.get("text", "") for e, d in events if e == "text"))

    def test_empty_selection_stops_face_workflow_but_still_routes(self):
        p, b, events = self.setup_run(faces=[])
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])
        p._evaluate.assert_called_once()

    def test_unsupported_or_uncertain_request_never_executes(self):
        for reply in [response("unsupported"), response(probability=.6, confidence=.4)]:
            with self.subTest(reply=reply):
                p, b, events = self.setup_run(reply)
                self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])

    def test_extrusion_requires_explicit_direction(self):
        p, b, events = self.setup_run(response("extrude", "ambiguous"))
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])
        self.assertTrue(any("Label the two" in d.get("text", "") for _, d in events))

    def test_extrusion_rejects_uncertain_direction(self):
        reply = response("extrude", "first_to_second")
        reply["answers"]["direction"] = answer(DIRECTIONS, "first_to_second", .7, .5)
        p, b, events = self.setup_run(reply)
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])

    def test_pair_workflows_require_exactly_two_faces(self):
        for workflow in ["minimum", "planes", "extrude"]:
            for faces in [FACES[:1], FACES + [{"ref": "picked_c"}]]:
                p, b, events = self.setup_run(response(workflow, "first_to_second"), faces)
                self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces"])

    def test_failed_revalidation_stops_mutation(self):
        failure = {"ok": False, "error": {"code": "invalid_arguments", "message": "Face belongs to another document"}}
        p, b, events = self.setup_run(response("extrude", "first_to_second"), outcomes={"resolve_selected_faces": failure})
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces", "resolve_selected_faces"])
        self.assertTrue(any("another document" in d.get("text", "") for _, d in events))

    def test_missing_resolved_face_stops_mutation(self):
        p, b, events = self.setup_run(response("extrude", "first_to_second"), outcomes={
            "resolve_selected_faces": {"ok": True, "result": {"faces": FACES[:1]}}})
        with self.assertRaisesRegex(ProviderError, "Not all"):
            self.run_agent(p, b, events)
        self.assertNotIn("extrude_between_faces", [c.args[0] for c in b.call.call_args_list])

    def test_native_failure_surfaces_recovery_without_retry(self):
        failure = {"ok": False, "error": {"code": "tool_failure", "recovery": {"status": "partial"}}}
        p, b, events = self.setup_run(response("extrude", "first_to_second"), outcomes={"extrude_between_faces": failure})
        names = self.run_agent(p, b, events)
        self.assertEqual(names.count("extrude_between_faces"), 1)
        self.assertTrue(any("partial" in d.get("text", "") for _, d in events))

    def test_timeout_polls_once_never_replays_then_next_turn_only_checks_status(self):
        outcomes = {"extrude_between_faces": {"ok": False, "error": {"code": "outcome_unknown", "operation_id": "op1"}},
                    "operation_status": {"ok": True, "result": {"state": "running", "operation_id": "op1"}}}
        p, b, events = self.setup_run(response("extrude", "first_to_second"), outcomes=outcomes)
        names = self.run_agent(p, b, events)
        self.assertEqual(names.count("extrude_between_faces"), 1)
        self.assertEqual(names[-1], "operation_status")
        self.assertEqual(p.pending_operation, "op1")
        b.call.reset_mock()
        self.assertEqual(self.run_agent(p, b, events), ["operation_status"])
        p._evaluate.assert_called_once()
        outcomes["operation_status"] = {"ok": True, "result": {"state": "succeeded", "operation_id": "op1"}}
        b.call.reset_mock()
        self.assertEqual(self.run_agent(p, b, events), ["operation_status"])
        self.assertIsNone(p.pending_operation)

    def test_api_failure_never_mutates(self):
        p, b, events = self.setup_run(response("extrude", "first_to_second"))
        p._evaluate.side_effect = ProviderError("Unavailable")
        with self.assertRaises(ProviderError):
            self.run_agent(p, b, events)
        self.assertEqual([c.args[0] for c in b.call.call_args_list], ["get_selected_faces"])

    def test_cancel_after_decision_never_mutates(self):
        p, b, events = self.setup_run(response("extrude", "first_to_second"))
        def emit(event, data):
            if event == "jev_decision":
                raise Cancelled()
        with self.assertRaises(Cancelled):
            server.Agent(b, p).run([{"type": "text", "text": "Extrude Start to End"}], emit)
        self.assertEqual([c.args[0] for c in b.call.call_args_list], ["get_selected_faces"])

    def test_missing_native_tool_fails_without_mutation(self):
        p, b, events = self.setup_run(response("extrude", "first_to_second"))
        tools = registry()
        del tools["extrude_between_faces"]
        b.registry.side_effect = lambda: tools
        with self.assertRaisesRegex(ProviderError, "unavailable tool"):
            self.run_agent(p, b, events)
        self.assertNotIn("extrude_between_faces", [c.args[0] for c in b.call.call_args_list])

    def test_status_failure_does_not_loop(self):
        outcomes = {"extrude_between_faces": {"ok": False, "error": {"code": "timeout", "operation_id": "op1"}},
                    "operation_status": {"ok": False, "error": {"code": "outcome_unknown", "operation_id": "op1"}}}
        p, b, events = self.setup_run(response("extrude", "first_to_second"), outcomes=outcomes)
        self.assertEqual(self.run_agent(p, b, events).count("operation_status"), 1)
        self.assertEqual(p.pending_operation, "op1")

    def test_new_turn_does_not_reuse_old_faces_or_history(self):
        p, b, events = self.setup_run()
        self.run_agent(p, b, events, attachments=True)
        b.call.reset_mock()
        self.assertEqual(self.run_agent(p, b, events), ["get_selected_faces", "measure_geometry"])
        self.assertEqual(p._evaluate.call_count, 2)


class Contracts(unittest.TestCase):
    def test_factory_and_key_required(self):
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
            with self.assertRaisesRegex(ProviderError, "TYPESAFE_API_KEY"):
                providers.build("jev", "jev-1.13.0")
        self.assertIsInstance(providers.build("jev", "jev-1.13.0", api_key="test"), JevProvider)
        self.assertEqual(providers.PRESETS["jev-1.13.0"]["provider"], "jev")

    def test_images_rejected_without_fallback(self):
        p = JevProvider("jev-1.13.0", api_key="test")
        with self.assertRaisesRegex(ProviderError, "cannot read images"):
            p.add_user([{"type": "image", "data": "anything"}])

    def test_http_chat_rejects_images_before_vision_fallback(self):
        p = JevProvider("jev-1.13.0", api_key="test")
        handler = object.__new__(server.Handler)
        bridge = Mock()
        handler._session = Mock(return_value=SimpleNamespace(ensure=lambda: p, bridge=bridge))
        handler._read_json = Mock(return_value={"text": "Build this", "images": ["data:image/png;base64,QUJD"]})
        handler._send = Mock()
        with patch.object(server, "_describe_attachments") as describe:
            handler._chat()
        describe.assert_not_called()
        bridge.call.assert_not_called()
        self.assertEqual(handler._send.call_args.args[0], 400)
        self.assertIn("does not use images", handler._send.call_args.args[1]["error"])

    def test_missing_unknown_nan_and_inconsistent_answers_fail_closed(self):
        good = answer(WORKFLOWS, "extrude")
        bad = [None, {}, {**good, "choice": []}, {**good, "choice": "delete_everything"},
               {**good, "confidence": float("nan")}, {**good, "confidence": True},
               {**good, "probabilities": {"extrude": 1}},
               {**good, "choice": "inspect"}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ProviderError):
                choice(item, WORKFLOWS)

    def test_wrong_tool_result_id_rejected(self):
        p = JevProvider("jev-1.13.0", api_key="test")
        with self.assertRaises(ProviderError):
            p.add_tool_results([ToolResult("wrong", "measure_geometry", True, {})])

    def test_http_request_contract(self):
        p = JevProvider("jev-1.13.0", api_key="test-secret")
        raw = json.dumps(response()).encode()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)) as http:
            result = p._evaluate({"request": "inspect"}, {"workflow": {"type": "choice", "criteria": WORKFLOWS}})
        req = http.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(json.loads(req.data)["model"], "jev-1.13.0")
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual(http.call_args.kwargs["timeout"], 15)

    def test_http_errors_redact_response_body_and_never_retry(self):
        p = JevProvider("jev-1.13.0", api_key="test-secret")
        error = urllib.error.HTTPError("https://api.typesafe.ai", 429, "test-secret", {}, io.BytesIO(b"test-secret"))
        with patch("urllib.request.urlopen", side_effect=error) as http:
            with self.assertRaises(ProviderError) as caught:
                p._evaluate({}, {})
        self.assertNotIn("test-secret", str(caught.exception))
        http.assert_called_once()

    def test_invalid_json_and_oversized_response(self):
        p = JevProvider("jev-1.13.0", api_key="test")
        for raw in [b"not json", b"[]", b"x" * 256_001]:
            with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)), self.assertRaises(ProviderError):
                p._evaluate({}, {})


if __name__ == "__main__":
    unittest.main()
