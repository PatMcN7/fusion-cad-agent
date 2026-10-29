import importlib
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from test_registry import _install_adsk_stub
_install_adsk_stub()
import face_selection
import spatial
import tools
from errors import ArgumentError


class Object:
    def __init__(self,**kw):self.__dict__.update(kw)


class Faces(unittest.TestCase):
    def setUp(self):
        face_selection._faces.clear()
        self.root=Object();self.doc=Object(name='Assembly')
        self.native=Object(objectType='Face',nativeObject=None,assemblyContext=None,isValid=True,entityToken='token',area=2,body=Object(name='Pad',parentComponent=self.root))
        self.design=Object(rootComponent=self.root,parentDocument=self.doc,activeOccurrence=None,findEntityByToken=Mock(return_value=[self.native]))
        self.patches=[patch.object(face_selection.adsk.fusion,'BRepFace',NS(classType=lambda:'Face')),
                      patch.object(spatial,'report',return_value={'selector':{'token':'long-token'},'bounds_cm':{}})]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def test_current_selection_ignores_bodies_and_deduplicates_faces(self):
        selection=[Object(entity=self.native),Object(entity=self.native),Object(entity=Object(objectType='Body'))]
        ui=Object(activeSelections=Object(count=3,item=lambda i:selection[i]))
        result=face_selection.get_selected_faces(self.design,ui,{})
        self.assertEqual(result['count'],1);self.assertEqual(result['ignored_non_faces'],1)
        face=result['faces'][0]
        self.assertEqual(face['selector'],{'face_ref':face['ref']})
        self.assertIs(spatial.resolve(self.design,face['selector']),self.native)
        self.assertEqual(face_selection.get_selected_faces(self.design,ui,{})['faces'][0]['ref'],face['ref'])

    def test_handle_survives_geometry_hot_reload(self):
        ref=face_selection.remember(self.design,self.native)
        importlib.reload(face_selection)
        self.assertIs(face_selection.resolve(self.design,ref),self.native)

    def test_wrong_document_stale_and_ambiguous_faces_are_rejected(self):
        ref=face_selection.remember(self.design,self.native)
        self.design.parentDocument=Object(name='Other')
        with self.assertRaisesRegex(ArgumentError,'another open document'):face_selection.resolve(self.design,ref)
        self.design.findEntityByToken.assert_not_called()
        self.design.parentDocument=self.doc
        for found in [[],[self.native,self.native]]:
            self.design.findEntityByToken.return_value=found
            with self.assertRaisesRegex(ArgumentError,'no longer resolves'):face_selection.resolve(self.design,ref)
        with self.assertRaisesRegex(ArgumentError,'expired'):face_selection.resolve(self.design,'invented')

    def test_component_instance_identity_survives_rename(self):
        component=Object();self.native.body.parentComponent=component
        occurrence=Object(component=component,isValid=True,fullPathName='Module:2')
        proxy=Object(nativeObject=self.native,assemblyContext=occurrence)
        self.native.createForAssemblyContext=Mock(return_value=proxy)
        ref=face_selection.remember(self.design,proxy)
        occurrence.fullPathName='Renamed:2'
        self.assertIs(face_selection.resolve(self.design,ref),proxy)
        self.native.createForAssemblyContext.assert_called_once_with(occurrence)
        occurrence.isValid=False
        with self.assertRaisesRegex(ArgumentError,'instance'):face_selection.resolve(self.design,ref)

    def test_highlight_validates_every_reference_before_touching_selection(self):
        ref=face_selection.remember(self.design,self.native)
        ui=Object(activeSelections=Mock())
        with self.assertRaises(ArgumentError):face_selection.highlight_selected_faces(self.design,ui,{'refs':[ref,'missing']})
        ui.activeSelections.clear.assert_not_called()

    def test_handle_cannot_be_combined_with_heuristic_selector(self):
        ref=face_selection.remember(self.design,self.native)
        with self.assertRaisesRegex(ArgumentError,'alone'):spatial.resolve(self.design,{'face_ref':ref,'body':'Other'})

    def test_joint_uses_exact_occurrence_proxy_without_heuristic_selection(self):
        occurrence=Object();face=Object(assemblyContext=occurrence)
        with patch.object(tools,'_active_design',return_value=self.design),patch.object(face_selection,'resolve',return_value=face):
            self.assertIs(tools._face_for_joint(occurrence,{'face_ref':'picked_one'},'move_face'),face)
            with self.assertRaisesRegex(ArgumentError,'different component'):tools._face_for_joint(Object(),{'face_ref':'picked_one'},'move_face')
