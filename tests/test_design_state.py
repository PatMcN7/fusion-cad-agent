import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from design_state import DesignState, SCHEMA
from bridge_client import Bridge
from errors import ArgumentError
from tool_contracts import validate


def entry(key,kind='unknown',status='open',**kw):
    return dict(id=key,statement=key,kind=kind,status=status,**kw)


class Dependencies(unittest.TestCase):
    def test_unknowns_cannot_be_silently_promoted_to_measurements(self):
        state=DesignState()
        with self.assertRaises(ArgumentError):
            state.update({'upsert':[entry('length','derived','established')]})
        state.update({'upsert':[entry('length','assumption','established',source='Provisional user-visible choice')]})
        self.assertEqual(state.snapshot()['entries'][0]['kind'],'assumption')

    def test_fabricated_failed_and_stale_evidence(self):
        state=DesignState()
        with self.assertRaises(ArgumentError):
            state.update({'upsert':[entry('gap','derived','established',evidence=['invented'])]})
        bad=state.observe('verify_relationship',{}, {'passed':False},'doc')['evidence_ref']
        with self.assertRaises(ArgumentError):
            state.update({'upsert':[entry('gap','check','established',evidence=[bad])]})
        ref=state.observe('measure_geometry',{}, {'distance_cm':3},'doc')['evidence_ref']
        state.update({'upsert':[entry('gap','derived','established',evidence=[ref]),entry('tube','derived','established',evidence=[ref],depends_on=['gap'])]})
        state.invalidate()
        self.assertEqual([e['status'] for e in state.snapshot()['entries']],['stale','stale'])
        fresh=state.observe('measure_geometry',{}, {'distance_cm':4},'doc')['evidence_ref']
        state.update({'upsert':[entry('tube','derived','established',evidence=[fresh],depends_on=['gap'])]})
        self.assertEqual(state.snapshot()['entries'][1]['status'],'blocked')

    def test_update_is_atomic_and_graph_is_acyclic(self):
        state=DesignState();state.update({'upsert':[entry('a')]})
        for args in [{'upsert':[entry('b',depends_on=['missing'])]},
                     {'upsert':[entry('a',depends_on=['b']),entry('b',depends_on=['a'])]},
                     {'next_action':'x'*501}]:
            with self.assertRaises(ArgumentError):state.update(args)
            self.assertEqual([e['id'] for e in state.snapshot()['entries']],['a'])

    def test_saved_state_survives_restart_with_stale_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';state=DesignState(path)
            ref=state.observe('measure_geometry',{}, {'distance_cm':3},'doc')['evidence_ref']
            state.update({'upsert':[entry('gap','derived','established',evidence=[ref])],'next_action':'Derive section'})
            restored=DesignState(path)
            self.assertEqual(restored.snapshot()['entries'][0]['status'],'stale')
            self.assertIn('Derive section',restored.prompt())

    def test_local_tool_does_not_touch_fusion_and_is_per_client(self):
        bridge=Bridge();bridge._request=Mock(side_effect=AssertionError('transport called'))
        response=bridge.call('update_design_state',{'upsert':[entry('spacing')]},False)
        self.assertTrue(response['ok']);bridge._request.assert_not_called()
        self.assertEqual(Bridge().design_state.snapshot()['entries'],[])

    def test_failed_edit_invalidates_measurements(self):
        bridge=Bridge();bridge._document_id='doc'
        ref=bridge.design_state.observe('measure_geometry',{}, {'distance_cm':3},'doc')['evidence_ref']
        bridge.design_state.update({'upsert':[entry('gap','derived','established',evidence=[ref])]})
        bridge._request=Mock(return_value={'ok':False,'error':{'code':'failed'}})
        bridge.call('place_component',{},True)
        self.assertEqual(bridge.design_state.snapshot()['entries'][0]['status'],'stale')

    def test_schema_allows_generic_dimensions_not_testcase_vocabulary(self):
        validate({'upsert':[entry('bearing_spacing'),entry('panel_mounts')]},SCHEMA)



class MCPDesignSurface(unittest.TestCase):
    def test_mcp_exposes_same_state_tool_and_records_dependencies(self):
        import mcp_server
        from unittest.mock import patch
        from design_state import TOOLS
        bridge=Bridge();bridge.registry=Mock(return_value=TOOLS)
        with patch.object(mcp_server,'BRIDGE',bridge):
            advertised=mcp_server.handle_tools_list({})['tools'][0]
            self.assertEqual(advertised['name'],'update_design_state')
            self.assertFalse(advertised['annotations']['readOnlyHint'])
            self.assertFalse(advertised['annotations']['destructiveHint'])
            result=mcp_server.handle_tools_call({'name':'update_design_state','arguments':{'upsert':[entry('interfaces')]}})
            self.assertFalse(result.get('isError',False))
            self.assertEqual(bridge.design_state.snapshot()['entries'][0]['id'],'interfaces')

if __name__=='__main__':unittest.main()
