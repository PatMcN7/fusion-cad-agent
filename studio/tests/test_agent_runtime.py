import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import agent_runtime
import server
import transcript
import analyze
from providers import ToolCall, Turn, ToolResult


def spec(args=None,mutating=False):
    return {'description':'test', 'mutating':mutating, 'input_schema':{'type':'object','properties':args or {},'additionalProperties':False}}


class Loop(unittest.TestCase):
    def run_agent(self, turns, registry, results):
        provider=SimpleNamespace(stream=Mock(side_effect=turns),add_user=Mock(),add_tool_results=Mock())
        bridge=SimpleNamespace(registry=Mock(return_value=registry),call=Mock(side_effect=results),capability_version='v1')
        agent=server.Agent(bridge,provider)
        events=[]
        with patch.object(agent,'_capture') as automatic, patch.object(agent,'_look_at_file',return_value='One clean plate') as look:
            automatic.return_value=None
            agent.run([{'type':'text','text':'Build then inspect'}],lambda k,d:events.append((k,d)))
        return provider,bridge,events,automatic,look

    def test_different_workflows_load_without_unrelated_specialists(self):
        cases = [('geometry','extrude_polygons'), ('assembly','list_joint_candidates'),
                 ('files','import_geometry'), ('engineering','mechanics_report'),
                 ('validation','verify_spec'), ('patterns','edit_lattice')]
        registry={name:spec() for group,name in cases}
        for group,name in cases:
            with self.subTest(group=group):
                turns=[Turn(tool_calls=[ToolCall('load','load_tools',{'group':group})]),
                       Turn(tool_calls=[ToolCall('act',name,{})]),Turn()]
                provider,bridge,*_=self.run_agent(turns,registry,[{'ok':True,'result':{}}])
                initial={s.name for s in provider.stream.call_args_list[0].args[1]}
                self.assertEqual(initial,{'load_tools','get_result'}) # fixture contains only specialist tools
                loaded={s.name for s in provider.stream.call_args_list[1].args[1]}
                self.assertIn(name,loaded)
                if group!='patterns': self.assertNotIn('edit_lattice',loaded)
                bridge.call.assert_called_once_with(name,{},confirm=False)

    def test_specialist_instructions_are_absent_until_loaded(self):
        initial=agent_runtime.PROMPT+agent_runtime.guidance({'core'})
        for term in ('lattice_pockets','edit_lattice','8x6','corner_mode'):
            self.assertNotIn(term,initial)
        self.assertIn('corner_mode',agent_runtime.guidance({'core','patterns'}))
        self.assertIn('XY coordinates',agent_runtime.guidance({'core','geometry'}))
        self.assertNotIn('corner_mode',agent_runtime.guidance({'core','assembly'}))
        self.assertIn('Source editing:',agent_runtime.guidance({'all'}))

    def test_two_mutations_get_one_explicit_final_capture(self):
        turns=[Turn(tool_calls=[ToolCall('load','load_tools',{'group':'geometry'})]),Turn(tool_calls=[ToolCall('a','extrude_polygons',{})]),Turn(tool_calls=[ToolCall('b','cut_polygons',{})]),Turn(tool_calls=[ToolCall('c','capture_viewport',{})]),Turn(text='Done')]
        registry={n:spec(mutating=n!='capture_viewport') for n in ('extrude_polygons','cut_polygons','capture_viewport')}
        results=[{'ok':True,'result':{}},{'ok':True,'result':{}},{'ok':True,'result':{'captures':[{'path':'/tmp/plate.png'}]}}]
        provider,bridge,events,automatic,look=self.run_agent(turns,registry,results)
        automatic.assert_not_called()
        self.assertEqual(look.call_count,1)
        self.assertEqual(bridge.call.call_count,3)
        self.assertEqual(sum(k=='provider_timing' for k,d in events),5)
        self.assertEqual(sum(k=='turn_end' for k,d in events),1)

    def test_empty_document_is_available_without_loading_or_visual_review(self):
        turns=[Turn(tool_calls=[ToolCall('new','create_document',{})]),Turn(text='Created')]
        provider,bridge,events,automatic,look=self.run_agent(
            turns,{'create_document':spec(mutating=True)},
            [{'ok':True,'result':{'document_id':'new-doc','design':{'body_count':0,'timeline_entries':0}}}])
        bridge.call.assert_called_once_with('create_document',{},confirm=True)
        automatic.assert_not_called()
        look.assert_not_called()

    def test_loading_tool_group_exposes_required_array_schema(self):
        turns=[Turn(tool_calls=[ToolCall('a','load_tools',{'group':'editing'})]),Turn(tool_calls=[ToolCall('b','delete_entities',{'refs':['entity_1']})]),Turn(tool_calls=[ToolCall('c','capture_viewport',{})]),Turn()]
        registry={'delete_entities':spec({'refs':{'type':'array','items':{'type':'string'}}},True),'capture_viewport':spec()}
        registry['delete_entities']['input_schema']['required']=['refs']
        provider,bridge,*_=self.run_agent(turns,registry,[{'ok':True,'result':{}},{'ok':True,'result':{}}])
        initial=[s.name for s in provider.stream.call_args_list[0].args[1]]
        self.assertNotIn('delete_entities',initial)
        loaded=next(s for s in provider.stream.call_args_list[1].args[1] if s.name=='delete_entities')
        self.assertEqual(loaded.required,['refs'])
        self.assertEqual(loaded.properties['refs']['type'],'array')

    def test_duplicate_failures_do_not_repeat_backend_forever(self):
        turns=[Turn(tool_calls=[ToolCall(str(i),'inspect_entity',{})]) for i in range(4)]+[Turn()]
        provider,bridge,events,*_=self.run_agent(turns,{'inspect_entity':spec()},[{'ok':False,'error':{'code':'invalid_arguments','message':'bad'}}]*2)
        self.assertEqual(bridge.call.call_count,2)
        self.assertIn('already failed twice',str(events))

    def test_varying_failed_arguments_cannot_run_indefinitely(self):
        turns=[Turn(tool_calls=[ToolCall(str(i),'inspect_entity',{'ref':str(i)})]) for i in range(8)]
        provider,bridge,events,*_=self.run_agent(turns,{'inspect_entity':spec({'ref':{'type':'string'}})},[{'ok':False,'error':{'message':'wrong face'}}]*8)
        self.assertEqual(bridge.call.call_count,5)
        self.assertIn('task is incomplete',str(events))

    def test_advertised_operation_binding_passes_loop_validation(self):
        turns=[Turn(tool_calls=[ToolCall('a','switch_document',{'operation_id':'retry-id','document_id':'observed'})]),Turn()]
        provider,bridge,*_=self.run_agent(turns,{'switch_document':spec(mutating=True)},[{'ok':True,'result':{}}])
        self.assertEqual(bridge.call.call_count,1)
        self.assertEqual(bridge.call.call_args.args[1]['document_id'],'observed')

    def test_completion_waits_for_fallback_review_and_forwards_image(self):
        events=[]
        turns=iter([Turn(tool_calls=[ToolCall('load','load_tools',{'group':'geometry'})]),Turn(tool_calls=[ToolCall('a','create_test_box',{})]),
                    Turn(text='Premature success'), Turn(text='Reviewed result')])
        def stream(prompt,specs,emit):
            turn=next(turns)
            if turn.text: emit('text',{'text':turn.text})
            return turn
        provider=SimpleNamespace(stream=stream,add_user=Mock(),add_tool_results=Mock())
        bridge=SimpleNamespace(registry=lambda:{'create_test_box':spec(mutating=True)},
                               call=Mock(return_value={'ok':True,'result':{}}))
        agent=server.Agent(bridge,provider)
        with patch.object(agent,'_capture',return_value=('/tmp/final.png','encoded-image')), patch.object(server,'DESCRIBER',None):
            agent.run([{'type':'text','text':'Build'}],lambda k,d:events.append((k,d)))
        texts=[d['text'] for k,d in events if k=='text']
        self.assertEqual(texts,['Reviewed result'])
        self.assertEqual(provider.add_user.call_args.args[0][-1]['data'],'encoded-image')
        self.assertLess(next(i for i,(k,d) in enumerate(events) if k=='capture'),
                        next(i for i,(k,d) in enumerate(events) if k=='text'))

    def test_context_compaction_preserves_refs_and_unresolved_error(self):
        payload={'result_ref':'r1','lattice_ref':'entity_a','text':'x'*8000}
        error={'result_ref':'r2','code':'outcome_unknown','operation_id':'pending','message':'x'*2000}
        messages=[{'role':'tool','content':json.dumps(payload)},{'role':'tool','content':json.dumps(error)}]+[{'role':'assistant','content':'ok'}]*12
        agent_runtime.compact_messages(messages)
        self.assertEqual(json.loads(messages[0]['content'])['lattice_ref'],'entity_a')
        self.assertLess(len(messages[0]['content']),500)
        self.assertEqual(json.loads(messages[1]['content']),error)

    def test_large_result_is_valid_json_with_retrieval_reference(self):
        text=ToolResult('a','read_file',True,{'result_ref':'r1','text':'x'*100000}).as_text()
        value=json.loads(text)
        self.assertEqual(value['result_ref'],'r1')
        self.assertTrue(value['truncated'])


class Telemetry(unittest.TestCase):
    def test_interrupted_run_reports_observed_time_as_lower_bound(self):
        records=[{'t':10,'kind':'user','data':{'text':'go'}},
                 {'t':30,'kind':'tool_start','data':{'id':'a','name':'cut_polygons'}}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'session.jsonl'
            path.write_text('\n'.join(map(json.dumps,records)))
            summary=analyze.summarize(path)
        self.assertEqual(summary['active_seconds'],20)
        self.assertTrue(summary['unfinished_turn'])

    def test_dictionary_verification_does_not_drop_capture_completion(self):
        result=transcript.Recorder._summarize_tool_end({'name':'capture_viewport','ok':True,'result':{'verification':{'checks':[],'ok':True}}})
        self.assertTrue(result['ok'])

    def test_active_time_excludes_user_pauses_and_vision_cost_is_separate(self):
        records=[(0,'session_start',{}),(10,'user',{'text':'go'}),(10,'turn_start',{}),
                 (12,'provider_timing',{'role':'worker','seconds':2}),
                 (15,'provider_timing',{'role':'vision','seconds':3}),
                 (15,'vision_usage',{'input_tokens':200,'output_tokens':20,'cost_usd':.02}),
                 (16,'turn_end',{'active_seconds':6}),(16,'done',{}),
                 (100,'user',{'text':'again'}),(102,'turn_end',{'active_seconds':2}),(102,'done',{})]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'session.jsonl'
            path.write_text('\n'.join(json.dumps({'t':t,'kind':k,'data':d}) for t,k,d in records))
            summary=analyze.summarize(path)
        self.assertEqual(summary['active_seconds'],8)
        self.assertEqual(summary['provider_seconds']['vision'],3)
        self.assertEqual(summary['vision_cost'],.02)
        self.assertEqual(summary['cost'],0)
