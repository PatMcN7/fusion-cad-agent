import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_registry import _install_adsk_stub
_install_adsk_stub()
from fusion_cad import spatial
from fusion_cad.errors import ArgumentError
from fusion_cad.tool_contracts import check_contracts, validate


def collection(values):
    return NS(count=len(values),item=lambda i:values[i])


class AssemblyNavigation(unittest.TestCase):
    def test_placement_accepts_nested_anchor_but_rejects_sibling_instance(self):
        moving=NS(assemblyContext=None,fullPathName='Motor:1')
        leaf=NS(fullPathName='Motor:1+Housing:1')
        face=NS(objectType='Face',assemblyContext=leaf)
        with patch.object(spatial.adsk.fusion,'Occurrence',NS(classType=lambda:'Occurrence')), patch.object(spatial,'occurrence',return_value=moving), patch.object(spatial,'resolve',return_value=face):
            # Reaching the next preflight guard proves anchor acceptance without mutation.
            with self.assertRaisesRegex(ArgumentError,'Supply exactly one'):
                spatial.place_component(None,None,{'occurrence':'Motor:1','anchor':{}})
            for path in ('Motor:2+Housing:1','Motor:10+Housing:1'):
                leaf.fullPathName=path
                with self.assertRaisesRegex(ArgumentError,'Anchor must belong'):
                    spatial.place_component(None,None,{'occurrence':'Motor:1','anchor':{}})

    def test_empty_wrapper_exposes_exact_child_instance_then_body_faces(self):
        body=NS(faces=collection(['face-a','face-b']))
        proxy=NS(objectType='Body',faces=body.faces)
        body.createForAssemblyContext=lambda occurrence:proxy
        leaf=NS(objectType='Occurrence',name='Plate:2',fullPathName='Module:2+Plate:2',
                component=NS(bRepBodies=collection([body])),childOccurrences=collection([]),
                isGrounded=False,transform2=NS(asArray=lambda:[]))
        wrapper=NS(objectType='Occurrence',component=NS(bRepBodies=collection([])),
                   childOccurrences=collection([leaf]),isGrounded=False,transform2=NS(asArray=lambda:[]))
        with patch.object(spatial.adsk.fusion,'Occurrence',NS(classType=lambda:'Occurrence')), patch.object(spatial,'bounds',return_value={}), patch.object(spatial,'report',side_effect=lambda entity,*a:{'selector':{'token':str(entity),'occurrence':leaf.fullPathName}}):
            with patch.object(spatial,'resolve',return_value=wrapper):
                result=spatial.inspect_geometry(None,None,{'entity':{'occurrence':'Module:2'}})
            self.assertEqual(result['total'],1)
            selector=result['children'][0]['selector']
            self.assertEqual(selector,{'occurrence':'Module:2+Plate:2'})
            with patch.object(spatial,'resolve',return_value=leaf):
                result=spatial.inspect_geometry(None,None,{'entity':selector})
            self.assertEqual(result['bodies'][0]['face_count'],2)
            with patch.object(spatial,'resolve',return_value=proxy):
                result=spatial.inspect_geometry(None,None,{'entity':result['bodies'][0]['selector'],'limit':1})
            self.assertEqual(result['total'],2);self.assertEqual(result['next_offset'],1)
            self.assertEqual(len(result['faces']),1)

    def test_children_and_direct_bodies_share_one_page(self):
        child=NS(name='Child:1',fullPathName='Parent:1+Child:1',component=NS(bRepBodies=collection([])),childOccurrences=collection([]))
        body=NS(faces=collection([1]),createForAssemblyContext=lambda occurrence:'body-proxy')
        parent=NS(objectType='Occurrence',component=NS(bRepBodies=collection([body])),childOccurrences=collection([child]),isGrounded=False,transform2=NS(asArray=lambda:[]))
        with patch.object(spatial.adsk.fusion,'Occurrence',NS(classType=lambda:'Occurrence')),patch.object(spatial,'resolve',return_value=parent),patch.object(spatial,'bounds',return_value={}),patch.object(spatial,'report',return_value={'selector':{'token':'body'}}):
            first=spatial.inspect_geometry(None,None,{'entity':{'occurrence':'Parent:1'},'limit':1})
            second=spatial.inspect_geometry(None,None,{'entity':{'occurrence':'Parent:1'},'limit':1,'offset':first['next_offset']})
        self.assertEqual(first['total'],2);self.assertEqual(len(first['children']),1)
        self.assertEqual(second['next_offset'],None);self.assertEqual(len(second['bodies']),1)


class SpatialMath(unittest.TestCase):
    def test_rotated_non_origin_component_anchor_reaches_target(self):
        # Component already rotated 90 degrees and translated. New rotation
        # is about the world axis; an asymmetric off-origin anchor exercises
        # matrix order and translation errors hidden by origin-only tests.
        old=[0,-1,0,10,1,0,0,20,0,0,1,30,0,0,0,1]
        local=[2,3,4];source=[7,22,34];target=[-5,11,19]
        matrix=spatial.placement_matrix(old,source,target,[0,0,1],90)
        actual=[sum(matrix[i*4+j]*local[j] for j in range(3))+matrix[i*4+3] for i in range(3)]
        for a,b in zip(actual,target):self.assertAlmostEqual(a,b)
        for i in range(3):
            self.assertAlmostEqual(sum(matrix[i*4+j]**2 for j in range(3)),1)
        with self.assertRaises(ArgumentError):spatial.placement_matrix(old,source,target,[0,0,0],90)

    def test_parallel_planes_are_not_closest_point_distance(self):
        # Faces laterally displaced: plane separation must ignore that offset.
        with patch.object(spatial,'plane',side_effect=[([0,0,2],[0,0,1]),([8,9,7],[0,0,-1])]):
            metrics=spatial.plane_metrics(None,None)
        self.assertEqual(metrics['signed_separation_cm'],5)
        self.assertEqual(metrics['normal_angle_degrees'],0)
        self.assertFalse(metrics['normals_same_direction'])

    def test_reference_context_is_required_and_ambiguity_rejected(self):
        root=object();other=object()
        entity=NS(nativeObject=None,parentComponent=other)
        design=NS(rootComponent=root,findEntityByToken=lambda token:[entity])
        with self.assertRaisesRegex(ArgumentError,'component'):spatial.resolve(design,{'token':'face'})
        design.findEntityByToken=lambda token:[entity,entity]
        with self.assertRaisesRegex(ArgumentError,'ambiguous'):spatial.resolve(design,{'token':'face'})

    def test_clearance_and_distance_checks_differ(self):
        with patch.object(spatial,'measure_geometry',return_value={'distance_cm':3}), patch.object(spatial,'expression',side_effect=lambda d,v,f,units='cm':float(v)):
            args={'a':{'body':'a'},'b':{'body':'b'},'expected':'2','tolerance':'0.01'}
            self.assertTrue(spatial.verify_relationship(None,None,{**args,'relation':'minimum_clearance'})['passed'])
            self.assertFalse(spatial.verify_relationship(None,None,{**args,'relation':'distance'})['passed'])

    def test_schemas_are_explicit_and_bounded(self):
        schemas={n:s for n,(_,_,s) in spatial.SPECS.items()};check_contracts(schemas)
        with self.assertRaises(ArgumentError):validate({'limit':500},schemas['inspect_geometry'])
        with self.assertRaises(ArgumentError):validate({'a':{},'b':{},'mode':'isogrid'},schemas['measure_geometry'])


if __name__=='__main__':unittest.main()
