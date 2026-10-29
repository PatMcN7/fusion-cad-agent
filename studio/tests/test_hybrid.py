"""Hybrid orchestration tests with real Studio execution and no model/network calls."""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import providers
import test_jev as helpers
from providers import Cancelled, ProviderError
from providers.astra_planner import AstraPlanner
from providers.hybrid_provider import HybridProvider
from hybrid_plan import PlanError, Values, failed_checks, validate_plan
from fusion_cad.design_state import DesignState, TOOLS as DESIGN_TOOLS


def step(identity, tool='', payload=None, kind='tool', critical=False):
    return {'id': identity, 'kind': kind, 'tool': tool, 'payload_json': json.dumps(payload if payload is not None else {}),
            'purpose': 'Perform ' + identity, 'critical': critical}


def plan(steps=(), checks=(), next='finish', message=''):
    return {'goal': 'Build and check the requested geometry', 'assumptions': [], 'intent_json': '{}',
            'steps': list(steps), 'completion_checks': list(checks), 'next': next, 'message': message}


def ref(path):
    return {'$ref': path}


def box(identity='box'):
    return step(identity, 'extrude_polygons', {'polygons': [[[0,0],[10,0],[10,10],[0,10]]], 'units':'mm', 'height':'2 mm'})


def check(identity='verify'):
    return step(identity, 'verify_spec', {'body_count': 1})


def registry():
    result = {**helpers.registry(), **DESIGN_TOOLS}
    schemas = json.loads((Path(__file__).resolve().parents[2] / 'fusion_cad' / 'tool_schemas.json').read_text())
    result['verify_spec'] = {'description':'Verify design', 'mutating': False, 'input_schema': schemas['verify_spec']}
    result['capture_viewport'] = {'description':'Capture viewport', 'mutating':False, 'input_schema':schemas['capture_viewport']}
    return result


class FakeBridge:
    def __init__(self, overrides=None):
        self._document_id = 'doc1'
        self._unresolved = None
        self.design_state = DesignState()
        self.overrides = overrides or {}
        self.calls = []

    def registry(self):
        return registry()

    def call(self, name, arguments, confirm=False):
        self.calls.append((name, copy.deepcopy(arguments), confirm))
        if confirm:
            if arguments.get('document_id') != self._document_id:
                return {'ok':False,'error':{'code':'document_changed'}}
            self.design_state.invalidate(self._document_id)
        if name in self.overrides:
            value = self.overrides[name]
            result = value(arguments) if callable(value) else copy.deepcopy(value)
        elif name == 'update_design_state':
            return {'ok':True, 'result':self.design_state.update(arguments)}
        elif name in {'get_selected_faces', 'resolve_selected_faces'}:
            result = {'ok':True,'result':{'faces':copy.deepcopy(helpers.FACES)}}
        elif name == 'design_summary':
            result = {'ok':True,'result':{'document':{'name':'Design'}, 'bodies':[{'name':'Plate'}]}}
        elif name == 'inspect_geometry':
            result = {'ok':True,'result':{'entities':[{'name':'Motor','selector':{'occurrence':'Motor:1'}},
                                                      {'name':'Frame','selector':{'occurrence':'Frame:1'}}]}}
        elif name == 'measure_geometry':
            result = {'ok':True,'result':{'distance_cm':3.0}}
        elif name in {'verify_spec', 'check_interference'}:
            result = {'ok':True,'result':{'ok':True,'passed':1,'checks':[]}}
        else:
            result = {'ok':True,'result':{'body':{'name':'Plate'},'verification':{'ok':True,'passed':1,'checks':[]}}}
        if result.get('ok'):
            if name == 'create_document': self._document_id = 'doc2'
            result['result'].setdefault('document_id', self._document_id)
            if name == 'operation_status' and result['result'].get('state') in {'succeeded','failed'}:
                self._unresolved = None
            result['result'] = self.design_state.observe(name, arguments, result['result'], self._document_id)
        elif result.get('error', {}).get('code') in {'timeout','outcome_unknown','operation_pending'}:
            self._unresolved = result['error'].get('operation_id')
        return result


class Engine(unittest.TestCase):
    def make(self, plans, overrides=None):
        with patch('providers.hybrid_provider.AstraPlanner') as factory:
            provider = HybridProvider('gpt-6-astra', api_key='test')
        provider.planner.plan.side_effect = plans
        provider.jev = SimpleNamespace(_evaluate=Mock(), model='jev-1.13.0')
        return provider, FakeBridge(overrides), []

    def run_agent(self, p, bridge, events, emit=None):
        with patch.object(server.Agent, '_capture') as capture, patch.object(server, 'DESCRIBER') as describer:
            server.Agent(bridge, p).run([{'type':'text','text':'Build the requested part'}],
                                       emit or (lambda e,d: events.append((e,d))))
            capture.assert_not_called()
            describer.describe.assert_not_called()

    def mutations(self, bridge):
        return [c for c in bridge.calls if c[2]]

    def test_established_plan_executes_without_jev_or_extra_astra_calls(self):
        p,b,events = self.make([plan([box(), check()], ['verify'])])
        self.run_agent(p,b,events)
        p.planner.plan.assert_called_once()
        p.jev._evaluate.assert_not_called()
        self.assertEqual([c[0] for c in self.mutations(b)], ['extrude_polygons'])
        self.assertEqual(self.mutations(b)[0][1]['document_id'], 'doc1')
        self.assertEqual(b.design_state.next_action, 'Build and check the requested geometry')
        self.assertTrue(any(e=='hybrid_plan' for e,d in events))
        self.assertIn('Checked: verify', p.message)

    def test_measure_compute_and_build_without_model_roundtrips(self):
        steps = [step('gap','measure_geometry',{'a':{'face_ref':'picked_a'},'b':{'face_ref':'picked_b'}}),
                 step('thickness',kind='compute',payload={'$calc':'subtract','args':[ref('gap.distance_cm'),.2]}),
                 step('make','extrude_polygons',{'polygons':[[[0,0],[10,0],[10,10],[0,10]]],
                                                'height':{'$length':{'value':ref('thickness'),'unit':'cm'}}}), check()]
        p,b,e = self.make([plan(steps,['verify'])])
        self.run_agent(p,b,e)
        self.assertEqual(self.mutations(b)[0][1]['height'], '2.8 cm')
        p.planner.plan.assert_called_once()
        p.jev._evaluate.assert_not_called()

    def test_chained_fixed_computations_survive_geometry_edits(self):
        steps = [step('fixed',kind='compute',payload=4),
                 step('half',kind='compute',payload={'$calc':'divide','args':[ref('fixed'),2]}),
                 box('first'),
                 step('second','extrude_polygons',{'polygons':[[[20,0],[30,0],[30,10],[20,10]]],
                                                  'height':{'$length':{'value':ref('half'),'unit':'mm'}}}),check()]
        p,b,e = self.make([plan(steps,['verify'])])
        self.run_agent(p,b,e)
        self.assertEqual(len(self.mutations(b)),2)
        p.planner.plan.assert_called_once()
        self.assertEqual(self.mutations(b)[1][1]['height'],'2 mm')

    def test_explicit_capture_is_displayed_without_vision_or_auto_capture(self):
        p,b,e = self.make([plan([step('picture','capture_viewport',{'views':['current'],'fit':False})])],
                         {'capture_viewport':{'ok':True,'result':{'captures':[{'path':'/tmp/native-capture.png'}]}}})
        self.run_agent(p,b,e)
        self.assertTrue(any(event=='capture' for event,data in e))
        p.planner.plan.assert_called_once()
        p.jev._evaluate.assert_not_called()

    def test_one_explicit_choice_uses_jev_then_native_selected_reference(self):
        steps = [step('survey','inspect_geometry',{}),
                 step('target',kind='choose',payload={'question':'Which component is the motor?', 'candidates':ref('survey.entities')}),
                 step('inspect','inspect_geometry',{'entity':ref('target.selector')})]
        p,b,e = self.make([plan(steps)])
        p.jev._evaluate.return_value = {'answers':{'selection':helpers.answer({'candidate_0':{},'candidate_1':{},'abstain':''},'candidate_0')}}
        self.run_agent(p,b,e)
        self.assertEqual(b.calls[-1][1], {'entity':{'occurrence':'Motor:1'}})
        p.planner.plan.assert_called_once()
        p.jev._evaluate.assert_called_once()

    def test_critical_choice_asks_user_without_jev(self):
        steps = [step('survey','inspect_geometry',{}),
                 step('mount',kind='choose',critical=True,payload={'question':'Which face is the mounting interface?', 'candidates':ref('survey.entities')})]
        p,b,e = self.make([plan(steps)])
        self.run_agent(p,b,e)
        p.jev._evaluate.assert_not_called()
        self.assertFalse(self.mutations(b))
        self.assertTrue(any(event=='face_selection_request' for event,data in e))

    def test_uncertain_choice_returns_observations_to_astra(self):
        first = plan([step('survey','inspect_geometry',{}),
                      step('target',kind='choose',payload={'question':'Which one?', 'candidates':ref('survey.entities')}), box(), check()], ['verify'])
        p,b,e = self.make([first, plan(next='ask',message='Please identify the component.')])
        p.jev._evaluate.return_value = {'answers':{'selection':helpers.answer({'candidate_0':{},'candidate_1':{},'abstain':''},'abstain')}}
        self.run_agent(p,b,e)
        self.assertFalse(self.mutations(b))
        self.assertEqual(p.planner.plan.call_count,2)
        self.assertIn('uncertain', json.dumps(p.planner.plan.call_args.args[0]).lower())
        self.assertEqual(p.message,'Please identify the component.')

    def test_partial_failure_discards_tail_and_replans_with_recovery(self):
        failure = {'ok':False,'error':{'code':'tool_failure','recovery':{'status':'partial','completed_stages':['sketch']}}}
        p,b,e = self.make([plan([box(),step('unwanted','create_document',{}),check()],['verify']),
                          plan(next='ask',message='Partial geometry remains; inspect it before continuing.')],
                         {'extrude_polygons':failure})
        self.run_agent(p,b,e)
        self.assertEqual([c[0] for c in self.mutations(b)],['extrude_polygons'])
        self.assertIn('partial',json.dumps(p.planner.plan.call_args.args[0]))
        self.assertFalse(p.planner.plan.call_args.args[0]['records']['design']['fresh'])

    def test_identical_mutation_cannot_be_retried_by_replanner(self):
        failure = {'ok':False,'error':{'code':'tool_failure','recovery':{'status':'partial'}}}
        p,b,e = self.make([plan([box('first'),check('v1')],['v1']),
                          plan([box('retry'),check('v2')],['v2']), plan(next='ask',message='Stopped.')],
                         {'extrude_polygons':failure})
        self.run_agent(p,b,e)
        self.assertEqual(len(self.mutations(b)),1)
        self.assertEqual(p.planner.plan.call_count,3)
        self.assertIn('already attempted',json.dumps(p.planner.plan.call_args.args[0]))

    def test_timeout_polls_once_without_replanning_or_replay(self):
        p,b,e = self.make([plan([box(),check()],['verify'])], {
            'extrude_polygons':{'ok':False,'error':{'code':'outcome_unknown','operation_id':'op1'}},
            'operation_status':{'ok':True,'result':{'state':'running','operation_id':'op1'}}})
        self.run_agent(p,b,e)
        self.assertEqual(p.pending_operation,'op1')
        self.assertEqual(b.calls[-1][0],'operation_status')
        self.assertEqual(len(self.mutations(b)),1)
        p.planner.plan.assert_called_once()
        b.calls=[]
        self.run_agent(p,b,e)
        self.assertEqual([c[0] for c in b.calls],['operation_status'])
        p.planner.plan.assert_called_once()
        b.overrides['operation_status']={'ok':True,'result':{'state':'succeeded','operation_id':'op1'}}
        b.calls=[]
        self.run_agent(p,b,e)
        self.assertIsNone(p.pending_operation)
        self.assertEqual([c[0] for c in b.calls],['operation_status'])

    def test_invalid_plan_is_rejected_before_execution_and_budgeted(self):
        invalid = plan([step('bad','write_file',{'path':'x','content':'bad'})])
        p,b,e = self.make([invalid,invalid,invalid])
        self.run_agent(p,b,e)
        self.assertFalse(self.mutations(b))
        self.assertEqual(p.planner.plan.call_count,3)
        self.assertIn('budget',p.message.lower())

    def test_assert_failure_replans_without_running_dependent_mutation(self):
        steps = [step('gap','measure_geometry',{'a':{'face_ref':'picked_a'},'b':{'face_ref':'picked_b'}}),
                 step('fits',kind='assert',payload={'op':'gte','left':ref('gap.distance_cm'),'right':100}),box(),check()]
        p,b,e = self.make([plan(steps,['verify']),plan(next='ask',message='The measured gap is too small.')])
        self.run_agent(p,b,e)
        self.assertFalse(self.mutations(b))
        self.assertIn('assertion failed',json.dumps(p.planner.plan.call_args.args[0]))

    def test_stale_measurement_cannot_drive_a_second_mutation(self):
        steps = [step('gap','measure_geometry',{'a':{'face_ref':'picked_a'},'b':{'face_ref':'picked_b'}}),box(),
                 step('bad','extrude_polygons',{'polygons':[[[0,0],[1,0],[1,1]]],
                                               'height':{'$length':{'value':ref('gap.distance_cm'),'unit':'cm'}}}),check()]
        p,b,e = self.make([plan(steps,['verify']),plan(next='ask',message='Remeasure first.')])
        self.run_agent(p,b,e)
        self.assertEqual(len(self.mutations(b)),1)
        self.assertIn('stale',json.dumps(p.planner.plan.call_args.args[0]))

    def test_verification_false_even_with_ok_transport_stops_plan(self):
        p,b,e = self.make([plan([box(),check(),step('never','create_document',{})],['verify']),
                          plan(next='ask',message='Verification failed.')],
                         {'verify_spec':{'ok':True,'result':{'ok':False,'passed':0,'failed':1}}})
        self.run_agent(p,b,e)
        self.assertEqual(len(self.mutations(b)),1)
        self.assertIn('verification_failure',json.dumps(p.planner.plan.call_args.args[0]))

    def test_check_before_later_mutation_cannot_certify_completion(self):
        p,b,e = self.make([plan([check(),box()],['verify']),plan(next='ask',message='Need a fresh check.')])
        self.run_agent(p,b,e)
        self.assertEqual(p.planner.plan.call_count,2)
        self.assertIn('Completion check',json.dumps(p.planner.plan.call_args.args[0]))

    def test_empty_verification_is_not_positive_evidence(self):
        p,b,e = self.make([plan([box(),check()],['verify']),plan(next='ask',message='Check missing.')],
                         {'verify_spec':{'ok':True,'result':{}}})
        self.run_agent(p,b,e)
        self.assertIn('no positive native check',json.dumps(p.planner.plan.call_args.args[0]))

    def test_controlled_document_creation_rebinds_subsequent_mutation(self):
        p,b,e = self.make([plan([step('doc','create_document',{}),box(),check()],['verify'])])
        self.run_agent(p,b,e)
        self.assertEqual([args['document_id'] for _,args,_ in self.mutations(b)],['doc1','doc2'])

    def test_document_switch_discards_plan_instead_of_retargeting(self):
        p,b,e = self.make([plan([box(),check()],['verify'])])
        def emit(event,data):
            e.append((event,data))
            if event=='hybrid_plan': b._document_id='other-doc'
        self.run_agent(p,b,e,emit)
        self.assertFalse(self.mutations(b))
        p.planner.plan.assert_called_once()
        self.assertIn('document changed',p.message)

    def test_stop_after_plan_prevents_mutation(self):
        p,b,e = self.make([plan([box(),check()],['verify'])])
        def emit(event,data):
            if event=='hybrid_plan': raise Cancelled()
        with self.assertRaises(Cancelled): self.run_agent(p,b,e,emit)
        self.assertFalse(self.mutations(b))

    def test_new_turn_has_fresh_state_not_old_geometry_references(self):
        p,b,e = self.make([plan([step('survey','inspect_geometry',{})]),plan()])
        self.run_agent(p,b,e)
        self.run_agent(p,b,e)
        state=p.planner.plan.call_args.args[0]
        self.assertNotIn('survey',state['records'])
        self.assertFalse(state['previous_turn']['fresh'])
        self.assertIn('inspect_geometry',json.dumps(state['previous_turn']))

    def test_native_warning_is_reported_without_unnecessary_replanning(self):
        p,b,e = self.make([plan([box(),check()],['verify'])],{'extrude_polygons':{'ok':True,'result':{
            'verification':{'ok':True,'passed':1,'warned':1,'checks':[{'ok':False,'severity':'warning','name':'thin_wall'}]}}}})
        self.run_agent(p,b,e)
        p.planner.plan.assert_called_once()
        self.assertTrue(any(event=='hybrid_verification' for event,data in e))


class Contracts(unittest.TestCase):
    def test_expression_arithmetic_units_and_no_eval(self):
        values=Values(); values.put('gap',{'distance_cm':3},native=True)
        self.assertEqual(values.resolve({'$length':{'value':{'$calc':'subtract','args':[ref('gap.distance_cm'),.2]},'unit':'cm'}}),'2.8 cm')
        for expression in [{'$eval':'__import__("os")'}, {'$calc':'divide','args':[1,0]}, {'$calc':'pow','args':[2,100]}]:
            with self.assertRaises(PlanError): values.resolve(expression)
        values.mutate()
        with self.assertRaisesRegex(PlanError,'stale'): values.resolve(ref('gap.distance_cm'))

    def test_plan_rejects_future_refs_duplicate_ids_missing_checks_and_protocol_override(self):
        cases=[plan([step('x',kind='compute',payload=ref('future'))]),
               plan([step('x',kind='compute',payload=1),step('x',kind='compute',payload=2)]),
               plan([box()]),
               plan([step('x','design_summary',{'document_id':'fake'})]),
               plan([step('x',kind='assert',payload={'op':'eq','left':True,'right':True})],['x'])]
        for candidate in cases:
            with self.subTest(candidate=candidate),self.assertRaises(PlanError):
                validate_plan(candidate,registry(),{})

    def test_verification_boolean_is_not_integer_count(self):
        self.assertFalse(failed_checks({'passed':0,'ok':True}))
        self.assertTrue(failed_checks({'passed':False}))
        self.assertTrue(failed_checks({'verification':[{'ok':False}]}))
        self.assertFalse(failed_checks({'verification':{'checks':[{'ok':False,'severity':'warning'}]}}))

    def test_planner_command_isolated_and_no_legacy_full_auto(self):
        with patch('shutil.which',return_value='/bin/codex'):
            planner=AstraPlanner()
        cmd=planner.command(Path('/tmp/isolated'))
        self.assertIn('--ignore-user-config',cmd)
        self.assertIn('--ignore-rules',cmd)
        self.assertIn('--ephemeral',cmd)
        self.assertEqual(cmd[cmd.index('--sandbox')+1],'read-only')
        self.assertIn('mcp_servers={}',cmd)
        self.assertIn('shell_tool',cmd)
        self.assertIn('unified_exec',cmd)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',cmd)
        self.assertNotIn('resume',cmd)
        self.assertEqual(cmd[-1],'-')

    def test_provider_registered_without_changing_existing_codex(self):
        self.assertEqual(providers.PRESETS['hybrid:gpt-6-astra']['provider'],'hybrid')
        with patch('providers.hybrid_provider.AstraPlanner'):
            p=providers.build('hybrid','hybrid:gpt-6-astra',api_key='test')
        self.assertEqual(p.model,'gpt-6-astra')
        self.assertFalse(getattr(p,'is_agent',False))


class PlannerTransport(unittest.TestCase):
    def planner(self):
        with patch('shutil.which',return_value='/bin/codex'):
            return AstraPlanner()

    def launch(self, answer=None, event=None):
        def spawn(argv, **kwargs):
            directory=Path(kwargs['cwd'])
            (directory/'answer.json').write_text(json.dumps(answer if answer is not None else plan()))
            kwargs['stdout'].write(json.dumps(event or {'type':'turn.completed','usage':{'input_tokens':42}})+'\n')
            kwargs['stdout'].flush()
            return SimpleNamespace(returncode=0,poll=lambda:0)
        return spawn

    def test_structured_plan_temp_cleanup_and_jev_secret_not_forwarded(self):
        planner=self.planner(); events=[]
        with patch.dict('os.environ',{'TYPESAFE_API_KEY':'not-for-astra'}), patch('subprocess.Popen',side_effect=self.launch()) as launch:
            result=planner.plan({'request':'inspect'},[],lambda e,d:events.append((e,d)))
        self.assertEqual(result,plan())
        self.assertNotIn('TYPESAFE_API_KEY',launch.call_args.kwargs['env'])
        self.assertFalse(Path(launch.call_args.kwargs['cwd']).exists())
        self.assertTrue(any(e=='provider_timing' and d['role']=='astra_planner' for e,d in events))

    def test_unexpected_planner_tool_event_rejects_entire_plan(self):
        planner=self.planner()
        with patch('subprocess.Popen',side_effect=self.launch(event={'type':'item.started','item':{'type':'mcp_tool_call'}})):
            with self.assertRaisesRegex(ProviderError,'attempted tool use'):
                planner.plan({},[],lambda *args:None)

    def test_cancel_terminates_planner(self):
        planner=self.planner()
        process=SimpleNamespace(poll=lambda:None)
        with patch('subprocess.Popen',return_value=process), patch.object(planner,'terminate') as terminate:
            with self.assertRaises(Cancelled):
                planner.plan({},[],lambda *args:None,stopped=lambda:True)
        terminate.assert_called_once_with(process)

    def test_deadline_terminates_planner(self):
        planner=self.planner()
        process=SimpleNamespace(poll=lambda:None)
        with patch('subprocess.Popen',return_value=process), patch.object(planner,'terminate') as terminate, patch('providers.astra_planner.time.monotonic',side_effect=[0,241]):
            with self.assertRaisesRegex(ProviderError,'240 seconds'):
                planner.plan({},[],lambda *args:None)
        terminate.assert_called_once_with(process)


if __name__=='__main__': unittest.main()
