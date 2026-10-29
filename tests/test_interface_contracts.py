import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_registry import _install_adsk_stub
_install_adsk_stub()
import tools
import workspace
from bridge_client import Bridge, tool_schemas
from errors import ArgumentError
from tool_contracts import validate


class Contracts(unittest.TestCase):
    def test_file_and_test_globs_are_not_lattice_enums(self):
        for name,patterns in {'list_files':['*.step','*WCP-1702*','*.pdf'],
                              'run_tests':['test_*.py','test_interface_contracts.py']}.items():
            schema=tools.TOOLS[name]['input_schema']
            self.assertNotIn('enum',schema['properties']['pattern'])
            for pattern in patterns: validate({'pattern':pattern},schema)
        self.assertEqual(tools.TOOLS['lattice_pockets']['input_schema']['properties']['pattern']['enum'],['isogrid','hex'])

    def test_untouched_unsaved_document_digest_is_not_marked_saved(self):
        from types import SimpleNamespace
        document=SimpleNamespace(name='Untitled',isSaved=False,isModified=False)
        design=SimpleNamespace(allComponents=SimpleNamespace(count=0),timeline=SimpleNamespace(count=0))
        with patch.object(tools,'APP',SimpleNamespace(activeDocument=document)), patch.object(tools,'_active_design',return_value=design):
            digest=tools.design_digest()
        self.assertFalse(digest['saved'])
        self.assertEqual(digest['body_count'],0)

    def test_general_initial_surface_and_complete_group_discovery(self):
        from studio import agent_runtime
        from bridge_client import CONTROL_TOOLS, DESIGN_TOOLS
        registry={**tools.TOOLS,**CONTROL_TOOLS,**DESIGN_TOOLS}
        initial=agent_runtime.select(registry,{'core'})
        self.assertEqual(len(initial),14)
        self.assertIn("get_selected_faces", initial)
        self.assertIn("update_design_state", initial)
        self.assertIn('create_document',initial)
        self.assertIn('design_summary',initial)
        self.assertFalse(set(initial)&{'create_test_box','lattice_pockets','edit_lattice','create_tube_frame'})
        assigned=set().union(*agent_runtime.GROUPS.values())
        self.assertEqual(assigned,set(registry))
        self.assertEqual(set(agent_runtime.GROUPS),set(agent_runtime.GROUP_DESCRIPTIONS))

    def test_same_named_documents_have_only_one_active_entry(self):
        from types import SimpleNamespace
        class Document:
            name='Untitled'
            isModified=True
            isSaved=False
        first,second=Document(),Document()
        self.assertFalse(tools._document_entry(first,second,0)['active'])
        self.assertTrue(tools._document_entry(second,second,1)['active'])

    def test_every_exposed_argument_has_explicit_type(self):
        for name, spec in tools.TOOLS.items():
            schema = spec['input_schema']
            self.assertFalse(schema['additionalProperties'])
            for key, value in schema['properties'].items():
                self.assertIn('type', value, (name, key))

    def test_delete_arrays_reach_transport_unchanged(self):
        bridge = Bridge()
        bridge._registry = tools.TOOLS
        bridge._document_id = 'observed'
        bridge._request = Mock(return_value={'ok':True, 'result':{}})
        args = {'sketch_names':['Unused Sketch']}
        result = bridge.call('delete_named_features', args, True)
        self.assertTrue(result['ok'])
        self.assertEqual(bridge._request.call_args.args[2]['arguments'], args)

    def test_stringified_array_is_rejected_before_transport_with_field_path(self):
        bridge = Bridge()
        bridge._registry = tools.TOOLS
        bridge._request = Mock()
        result = bridge.call('delete_named_features', {'feature_names':'["Cut"]'}, True)
        self.assertEqual(result['error']['context']['path'], 'arguments.feature_names')
        self.assertEqual(result['error']['context']['example'], ['Exact entity name'])
        bridge._request.assert_not_called()

    def test_required_fields_and_numeric_types_survive_schema_generation(self):
        schemas = {s['name']:s['input_schema'] for s in tool_schemas(tools.TOOLS)}
        self.assertIn('height', schemas['extrude_polygons']['required'])
        self.assertEqual(schemas['verify_spec']['properties']['body_count']['type'], 'integer')
        with self.assertRaises(ArgumentError): validate({'height':True}, schemas['capture_viewport'])
        with self.assertRaises(ArgumentError): validate({'path':'x','invented':1}, schemas['read_file'])

    def test_invalid_revision_is_rejected_before_deleting_old_chain(self):
        from types import SimpleNamespace
        recipe={'arguments':{'body':'plate'},'tokens':['old']}
        feature=SimpleNamespace(attributes=SimpleNamespace(itemByName=lambda *a:SimpleNamespace(value=json.dumps(recipe))))
        design=SimpleNamespace(findEntityByToken=lambda token:[feature])
        with patch.object(tools, '_active_design', return_value=design), patch.object(tools.editing,'resolve',return_value=feature), patch.object(tools.editing,'ref',return_value='ref'), patch.object(tools.editing,'suffix'), patch.object(tools.editing,'delete') as delete, patch.object(tools,'lattice_pockets',side_effect=ArgumentError('Too dense')):
            with self.assertRaisesRegex(ArgumentError,'Too dense'): tools.edit_lattice({'lattice_ref':'ref'})
            delete.assert_not_called()


class SourceTools(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.path=self.root/'sample.py'
        self.path.write_text('def first():\n    return 1\n\ndef second():\n    return 2\n')
        for name, value in [('REPO',self.root)]:
            p=patch.object(workspace,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(workspace,'allowed_roots',return_value=[self.root]);p.start();self.addCleanup(p.stop)
        p=patch.object(workspace,'backup_dir',return_value=self.root);p.start();self.addCleanup(p.stop)

    def test_symbol_read_patch_and_stale_hash_refusal(self):
        result=workspace.read_text(str(self.path),symbol='second')
        self.assertEqual(result['start_line'],4)
        self.assertNotIn('def first',result['text'])
        changed=workspace.patch_text(str(self.path),'return 2','return 3',result['sha256'])
        self.assertNotEqual(changed['sha256'],result['sha256'])
        self.assertTrue(Path(changed['backup']).exists())
        with self.assertRaisesRegex(ArgumentError,'File changed'):
            workspace.patch_text(str(self.path),'return 3','return 4',result['sha256'])

    def test_bad_syntax_and_ambiguous_patch_leave_file_unchanged(self):
        before=self.path.read_text();digest=workspace.read_text(str(self.path))['sha256']
        for old,new in [('return','pass'),('return 2','return (')]:
            with self.assertRaises(ArgumentError): workspace.patch_text(str(self.path),old,new,digest)
            self.assertEqual(self.path.read_text(),before)

    def test_invalid_contract_patch_preserves_previous_file(self):
        target = self.root/'tool_schemas.json'
        original = json.dumps({'inspect':{'type':'object','properties':{}}})
        target.write_text(original)
        for content in ('{', '{}', '{"inspect":{"type":"array"}}',
                        '{"inspect":{"type":"object","required":["missing"]}}'):
            with self.assertRaises(ArgumentError): workspace.write_text(str(target),content)
            self.assertEqual(target.read_text(),original)

    def test_search_does_not_follow_symlinks_outside_allowed_roots(self):
        (self.root/'outside.py').symlink_to('/etc/passwd')
        result=workspace.search_text('return',str(self.root))
        self.assertEqual(len(result['matches']),2)

    def test_repository_relative_read_works_independently_of_cwd(self):
        # Real repo relative paths must use the repo, not Fusion's process cwd.
        with patch.object(workspace,'allowed_roots',return_value=[Path(tools.__file__).parent]):
            self.assertEqual(workspace.resolve('tools.py'),Path(tools.__file__))
