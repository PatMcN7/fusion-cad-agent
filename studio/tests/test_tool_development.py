"""Trusted development is opt-in; source changes never masquerade as CAD work."""
import ast
from contextlib import nullcontext
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_hybrid as h
from hybrid_plan import PlanError, validate_plan
from tool_development import guard
from providers.hybrid_provider import HybridProvider


def registry():
    tools=h.registry()
    schemas=json.loads((Path(__file__).resolve().parents[2]/'tool_schemas.json').read_text())
    for name in ('read_file','write_file','run_tests'):
        tools[name]={'mutating':name=='write_file','description':name,'input_schema':schemas[name]}
    tools['patch_file']={'mutating':True,'description':'patch','input_schema':{
        'type':'object','properties':{key:{'type':'string'} for key in ('path','old_text','new_text','expected_sha256')},
        'required':['path','old_text','new_text','expected_sha256'],'additionalProperties':False}}
    return tools


def source_step():
    return h.step('new_source','write_file',{'path':'recipes/_hybrid_test_new_tool.py','content':'# example\n'})


class Development(unittest.TestCase):
    run_agent=h.Engine.run_agent

    def make(self, plans, overrides=None, enabled=True):
        with patch('providers.hybrid_provider.AstraPlanner'):
            p=HybridProvider('gpt-6-astra',allow_development=enabled,api_key='test')
        p.planner.plan.side_effect=plans
        b=h.FakeBridge(overrides)
        b.registry=registry
        return p,b,[]

    def test_blocker_and_noop_explanations_are_not_hidden(self):
        for status in ('blocked','finish'):
            p,b,e=self.make([h.plan(next=status,message='No. The circular through-cut tool is missing.')],enabled=False)
            self.run_agent(p,b,e)
            self.assertIn('circular through-cut',p.message)
            self.assertIn('No CAD changes',p.message)
            self.assertNotIn('Section executed',p.message)
            self.assertEqual(next(d['message'] for kind,d in e if kind=='hybrid_plan'),'No. The circular through-cut tool is missing.')

    def test_standard_mode_still_excludes_source_tools(self):
        p,b,e=self.make([h.plan(next='blocked',message='Need tool-building mode.')],enabled=False)
        self.run_agent(p,b,e)
        catalogue=p.planner.plan.call_args.args[1]
        self.assertNotIn('write_file',[t['name'] for t in catalogue])
        self.assertFalse(p.planner.plan.call_args.args[0]['tool_development']['enabled'])

    def test_development_read_write_test_then_geometry(self):
        build=h.plan([source_step(),h.step('tests','run_tests',{'pattern':'test*.py'})],next='replan')
        p,b,e=self.make([build,h.plan([h.box(),h.check()],['verify'])],overrides={
            'write_file':{'ok':True,'result':{'path':'recipes/_hybrid_test_new_tool.py','created':True}},
            'run_tests':{'ok':True,'result':{'passed':True,'tests':12}}})
        self.run_agent(p,b,e)
        self.assertFalse(p.source_dirty)
        self.assertTrue(p.mutated)
        names=[c[0] for c in b.calls]
        self.assertLess(names.index('run_tests'),names.index('extrude_polygons'))
        self.assertIn('write_file',[t['name'] for t in p.planner.plan.call_args_list[0].args[1]])

    def test_new_registered_tool_is_discovered_and_dispatched(self):
        first=h.plan([source_step(),h.step('tests','run_tests',{})],next='replan')
        second=h.plan([h.step('cut','new_native_cut',{'radius':'3 in'}),h.check()],['verify'])
        p,b,e=self.make([first,second],overrides={'run_tests':{'ok':True,'result':{'passed':True,'tests':5}}})
        def live_registry():
            tools=registry()
            if any(name=='write_file' for name,args,confirm in b.calls):
                tools['new_native_cut']={'description':'New cut','mutating':True,'input_schema':{
                    'type':'object','properties':{'radius':{'type':'string'}},'required':['radius'],'additionalProperties':False}}
            return tools
        b.registry=live_registry
        self.run_agent(p,b,e)
        self.assertTrue(any(name=='new_native_cut' and confirm for name,args,confirm in b.calls))

    def test_failed_empty_or_focused_tests_do_not_block_cad(self):
        for result, arguments in [({'passed':False,'tests':1},{}),
                                  ({'passed':True,'tests':0},{}),
                                  ({'passed':True,'tests':2},{'pattern':'test_one.py'})]:
            first=h.plan([source_step(),h.step('tests','run_tests',arguments)],next='replan')
            p,b,e=self.make([first,h.plan([h.box(),h.check()],['verify'])],overrides={
                'run_tests':{'ok':True,'result':result}})
            self.run_agent(p,b,e)
            self.assertTrue(p.source_dirty)
            self.assertTrue(any(name=='extrude_polygons' for name,args,confirm in b.calls))

    def test_inline_full_tests_are_recorded_but_not_required(self):
        write=h.step('write','write_file',{'path':'recipes/_hybrid_test_new_tool.py','content':'# example\n','run_tests':True})
        p,b,e=self.make([h.plan([write],next='replan'),h.plan([h.box(),h.check()],['verify']),
                        h.plan(next='blocked',message='Run the complete suite.')],overrides={
            'write_file':{'ok':True,'result':{'tests':{'passed':True,'tests':100}}}})
        self.run_agent(p,b,e)
        self.assertTrue(any(name=='extrude_polygons' for name,args,confirm in b.calls))
        self.assertFalse(p.source_dirty)

    def test_source_and_geometry_cannot_share_plan_section_in_strict_validation(self):
        with self.assertRaisesRegex(PlanError,'Separate source'):
            validate_plan(h.plan([source_step(),h.box(),h.check()],['verify']),registry(),{})

    def test_dev_cad_needs_no_completion_check(self):
        p,b,e=self.make([h.plan([h.box()])])
        self.run_agent(p,b,e)
        self.assertTrue(p.mutated)
        self.assertIn('Completion checks were not established',p.message)
        p.planner.plan.assert_called_once()

    def test_write_register_and_use_in_one_section_without_tests(self):
        p,b,e=self.make([h.plan([source_step(),h.step('cut','new_native_cut',{'radius':'3 in'})])])
        def live_registry():
            tools=registry()
            if any(name=='write_file' for name,args,confirm in b.calls):
                tools['new_native_cut']={'description':'New cut','mutating':True,'input_schema':{
                    'type':'object','properties':{'radius':{'type':'string'}},'required':['radius'],'additionalProperties':False}}
            return tools
        b.registry=live_registry
        self.run_agent(p,b,e)
        self.assertTrue(any(name=='new_native_cut' for name,args,confirm in b.calls))
        self.assertFalse(any(name=='run_tests' for name,args,confirm in b.calls))
        self.assertFalse(next(args for name,args,confirm in b.calls if name=='write_file')['run_tests'])
        self.assertTrue(p.source_dirty)
        p.planner.plan.assert_called_once()

    def test_unregistered_deferred_tool_still_cannot_execute(self):
        p,b,e=self.make([h.plan([source_step(),h.step('bad','does_not_exist',{})]),
                        h.plan(next='blocked',message='Registration failed.')])
        self.run_agent(p,b,e)
        self.assertFalse(any(name=='does_not_exist' for name,args,confirm in b.calls))
        self.assertIn('Registration failed',p.message)

    def test_source_alone_is_not_reported_as_cad_mutation(self):
        p,b,e=self.make([h.plan([source_step(),h.step('tests','run_tests',{})],message='Tool implemented; not run on geometry.')],overrides={
            'run_tests':{'ok':True,'result':{'passed':True,'tests':10}}})
        self.run_agent(p,b,e)
        self.assertFalse(p.mutated)
        self.assertIn('No CAD changes',p.message)


class PathPolicy(unittest.TestCase):
    def test_source_writes_are_mutating_without_geometry_undo_side_effects(self):
        source=(Path(__file__).resolve().parents[2]/'tools.py').read_text()
        tree=ast.parse(source)
        register=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_register')
        namespace={'TOOLS':{},'HANDLERS':{},'_echo_state':lambda handler:('wrapped',handler)}
        exec(compile(ast.Module(body=[register],type_ignores=[]),'register','exec'),namespace)
        handler=lambda args:args
        namespace['_register']('patch_file',handler,'Patch',{},mutating=True)
        self.assertTrue(namespace['TOOLS']['patch_file']['mutating'])
        self.assertEqual(namespace['HANDLERS']['patch_file'],('wrapped',handler))
        echo=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_echo_state')
        invalidate=Mock()
        scope={'Any':object,'tool_scope':lambda _:nullcontext(),'design_digest':lambda:{},
               'editing':SimpleNamespace(invalidate_undo=invalidate)}
        exec(compile(ast.Module(body=[echo],type_ignores=[]),'echo','exec'),scope)
        for name in ('write_file','patch_file'):
            def write(arguments): return {'path':'tools.py'}
            write.__name__=name
            result=scope['_echo_state'](write)({})
            self.assertIn('design',result)
        invalidate.assert_not_called()
        registration=next(n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                          and isinstance(n.value.func,ast.Name) and n.value.func.id=='_register'
                          and n.value.args and isinstance(n.value.args[0],ast.Constant) and n.value.args[0].value=='patch_file')
        self.assertIs(registration.value.args[-1].value,True)

    def test_new_recipe_and_hash_guarded_existing_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            guard('write_file',{'path':'recipes/cut_round.py'},root)
            (root/'tools.py').write_text('old')
            guard('patch_file',{'path':'tools.py','expected_sha256':'a'*64},root)
            with self.assertRaisesRegex(PlanError,'hash-guarded'):
                guard('write_file',{'path':'tools.py'},root)
            with self.assertRaises(PlanError):
                guard('patch_file',{'path':'tools.py','expected_sha256':'wrong'},root)

    def test_runtime_security_files_traversal_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root=Path(directory)
            for name in ('studio/server.py','FusionCadAgent.py','workspace.py','execution.py','bridge_client.py','.env','../escape.py'):
                with self.subTest(name=name),self.assertRaises(PlanError):
                    guard('write_file',{'path':name},root)
            (root/'recipes').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(PlanError):
                guard('write_file',{'path':'recipes/escape.py'},root)


if __name__=='__main__':unittest.main()
