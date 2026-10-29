"""Expanded native workflows, literal argument selection and deterministic geometry."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_jev as helpers
response, answer = helpers.response, helpers.answer
from providers.jev_inputs import dimension_candidates, name_candidates


class LiteralInputs(unittest.TestCase):
    def test_shared_unit_triple(self):
        values = dimension_candidates('Make a 100 x 60 x 5 mm plate')
        self.assertEqual([v['mm'] for v in values], [100, 60, 5])
        self.assertEqual([v['triple_axis'] for v in values], ['X', 'Y', 'Z'])

    def test_mixed_units_and_fractions(self):
        values = dimension_candidates('width 2 inches, height 3 cm, thickness 1/8 in')
        self.assertEqual([v['mm'] for v in values], [50.8, 30, 3.175])
        self.assertEqual(dimension_candidates('1 1/2 in')[0]['mm'], 38.1)
        self.assertEqual(dimension_candidates('1 by 2 by .5 cm')[-1]['mm'], 5)
        self.assertEqual(dimension_candidates('1 × 2 × 3 millimeters')[1]['mm'], 2)

    def test_no_guessed_units_or_partial_scientific_notation(self):
        self.assertEqual(dimension_candidates('100 x 60 x 5'), [])
        self.assertEqual(dimension_candidates('1e3 mm'), [])
        self.assertEqual(dimension_candidates('part100mm'), [])

    def test_invalid_negative_zero_and_extreme_lengths_stop(self):
        for text in ['-5 mm', '0 mm', '1/0 mm', '-1 1/2 in', '101 m', '1 mm ' * 13]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                dimension_candidates(text)

    def test_names_are_verbatim_and_deduplicated(self):
        self.assertEqual(name_candidates('Inspect "Frame:1/Module:2"'), {'name_0': 'Frame:1/Module:2'})
        self.assertEqual(name_candidates('Inspect “Plate” and "Plate"'), {'name_0': 'Plate'})
        self.assertEqual(name_candidates('Inspect Plate'), {})


class Expanded(unittest.TestCase):
    # Reuse the real Studio runner without duplicating its discovered tests.
    setup_run = helpers.Harness.setup_run
    run_agent = helpers.Harness.run_agent
    def run_extended(self, workflow, text, slots=None, target=None, outcomes=None):
        p, b, events = self.setup_run(faces=[], outcomes=outcomes)
        def evaluate(state, questions):
            reply = response(workflow)
            for name, question in questions.items():
                if name in {'workflow', 'direction'}:
                    continue
                selection = target if name == 'target' else (slots or {}).get(name)
                reply['answers'][name] = answer(question['criteria'], selection or 'missing')
            return reply
        p._evaluate.side_effect = evaluate
        self.run_agent(p, b, events, text=text)
        return p, b, events

    def test_global_read_workflows_without_faces(self):
        for workflow, tool in [('summary', 'design_summary'), ('features', 'feature_tree'),
                               ('assembly', 'list_joint_candidates'), ('interference', 'check_interference'),
                               ('documents', 'list_documents')]:
            with self.subTest(workflow=workflow):
                p, b, events = self.run_extended(workflow, 'Inspect this design')
                self.assertEqual(b.call.call_args.args[0], tool)
                self.assertFalse(b.call.call_args.kwargs['confirm'])
                p._evaluate.assert_called_once()

    def test_explicit_document_creation_conversion_and_scoped_undo(self):
        for workflow, tool in [('new_document', 'create_document'), ('components_all', 'bodies_to_components'), ('undo', 'undo_last')]:
            p, b, events = self.run_extended(workflow, 'Explicit supported action')
            self.assertEqual(b.call.call_args.args, (tool, {}))
            self.assertTrue(b.call.call_args.kwargs['confirm'])
            p._evaluate.assert_called_once()

    def test_box_in_new_document_uses_only_supplied_dimensions(self):
        p, b, events = self.run_extended('box', 'Create a 2 x 3 x 1/8 in box in a new document',
                                        {'width': 'dim_0', 'height': 'dim_1', 'depth': 'dim_2'})
        self.assertEqual(b.call.call_args.args, ('create_test_box', {'width': '50.8 mm', 'height': '76.2 mm', 'depth': '3.175 mm'}))
        p._evaluate.assert_called_once()

    def test_current_plate_constructs_centered_polygon_in_code(self):
        p, b, events = self.run_extended('rectangle', 'Create a 100 x 60 x 5 mm plate named "Plate"',
                                        {'width': 'dim_0', 'height': 'dim_1', 'depth': 'dim_2'}, 'name_0')
        self.assertEqual(b.call.call_args.args, ('extrude_polygons', {
            'polygons': [[[-50, -30], [50, -30], [50, 30], [-50, 30]]],
            'units': 'mm', 'height': '5 mm', 'base': '0 mm', 'name': 'Plate'}))
        self.assertTrue(any('Placement:' in d.get('text', '') for e, d in events if e == 'text'))
        p._evaluate.assert_called_once()

    def test_cube_can_reuse_one_explicit_length(self):
        p, b, events = self.run_extended('rectangle', 'Create a cube of side 10 mm',
                                        {role: 'dim_0' for role in ('width', 'height', 'depth')})
        self.assertEqual(b.call.call_args.args[1]['height'], '10 mm')
        self.assertEqual(b.call.call_args.args[1]['polygons'][0][0], [-5, -5])

    def test_missing_dimensions_never_use_native_defaults(self):
        for text, slots in [('Create a box', {}), ('Create a 10 x 20 x 30 mm box', {'width': 'dim_0', 'height': 'dim_1'})]:
            p, b, events = self.run_extended('box', text, slots)
            self.assertEqual([c.args[0] for c in b.call.call_args_list], ['get_selected_faces'])
            self.assertIn('Supply X width', p.message)

    def test_uncertain_dimension_stops_before_any_creation(self):
        p, b, events = self.setup_run(faces=[])
        def evaluate(state, questions):
            reply = response('rectangle')
            for role in ('width', 'height', 'depth'):
                reply['answers'][role] = answer(questions[role]['criteria'], 'dim_0', .6, .4)
            return reply
        p._evaluate.side_effect = evaluate
        self.assertEqual(self.run_agent(p, b, events, text='Create a cube of side 10 mm'), ['get_selected_faces'])

    def test_exact_named_body_and_nested_occurrence_inspection(self):
        for workflow, selector in [('inspect_body', {'body': 'Plate'}),
                                   ('inspect_occurrence', {'occurrence': 'Frame:1/Module:2'})]:
            target = next(iter(selector.values()))
            p, b, events = self.run_extended(workflow, f'Inspect "{target}"', target='name_0')
            self.assertEqual(b.call.call_args.args, ('inspect_geometry', {'entity': selector}))
            self.assertFalse(b.call.call_args.kwargs['confirm'])

    def test_material_and_conversion_preflight_exact_name(self):
        for workflow, tool, arguments in [('material', 'set_6061_material', {'body_names': ['Plate']}),
                                          ('components', 'bodies_to_components', {'bodies': ['Plate']})]:
            p, b, events = self.run_extended(workflow, 'Use "Plate"', target='name_0')
            calls = b.call.call_args_list
            self.assertEqual(calls[-2].args, ('inspect_geometry', {'entity': {'body': 'Plate'}}))
            self.assertEqual(calls[-1].args, (tool, arguments))
            self.assertTrue(calls[-1].kwargs['confirm'])
            p._evaluate.assert_called_once()

    def test_missing_ambiguous_names_and_native_rejection_never_mutate(self):
        for text, target in [('Set material on Plate', None), ('Set material on "Plate"', None)]:
            p, b, events = self.run_extended('material', text, target=target)
            self.assertEqual([c.args[0] for c in b.call.call_args_list], ['get_selected_faces'])
        p, b, events = self.run_extended('components', 'Convert "Plate"', target='name_0', outcomes={
            'inspect_geometry': {'ok': False, 'error': {'code': 'invalid_arguments', 'message': 'Ambiguous name'}}})
        self.assertNotIn('bodies_to_components', [c.args[0] for c in b.call.call_args_list])

    def test_explicit_unsupported_requests_do_not_partially_execute(self):
        for text in ['Create a 10 x 20 x 30 mm hollow box', 'Create a 10 x 20 x 30 mm plate then drill holes',
                     'Assign titanium to "Plate"']:
            p, b, events = self.run_extended('unsupported', text)
            self.assertEqual([c.args[0] for c in b.call.call_args_list], ['get_selected_faces'])


if __name__ == '__main__':
    unittest.main()
