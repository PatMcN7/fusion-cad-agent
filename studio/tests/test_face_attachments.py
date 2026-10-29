import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import face_attachments
from fusion_cad.errors import ArgumentError


class FaceMessages(unittest.TestCase):
    def setUp(self):
        self.attachment={'ref':'picked_one','document_id':'doc','tag':'Face 1','label':'Tube socket','body':'forged body'}
        self.bridge=NS(call=Mock(return_value={'ok':True,'result':{'document_id':'doc','faces':[{'ref':'picked_one','selector':{'face_ref':'picked_one'},'body':'Actual selected body'}]}}))

    def test_model_gets_native_identity_with_user_label_not_forged_metadata(self):
        faces=face_attachments.resolve_attachments(self.bridge,[self.attachment])
        self.assertEqual(faces[0]['body'],'Actual selected body')
        self.assertEqual(faces[0]['label'],'Tube socket')
        self.bridge.call.assert_called_once_with('resolve_selected_faces',{'refs':['picked_one']},confirm=False)
        text=face_attachments.model_block(faces)['text']
        self.assertIn('face_ref',text);self.assertNotIn('forged body',text)

    def test_no_face_attachment_adds_no_native_roundtrip(self):
        self.assertEqual(face_attachments.resolve_attachments(self.bridge,[]),[])
        self.bridge.call.assert_not_called()

    def test_invalid_documents_and_duplicate_faces_rejected_before_native_call(self):
        for attachments in [[self.attachment,self.attachment],[self.attachment,{**self.attachment,'ref':'picked_two','tag':'Face 2','document_id':'other'}]]:
            with self.assertRaises(ArgumentError):face_attachments.resolve_attachments(self.bridge,attachments)
        self.bridge.call.assert_not_called()

    def test_native_validation_failure_does_not_send_a_replacement(self):
        self.bridge.call.return_value={'ok':False,'error':{'message':'Face deleted'}}
        with self.assertRaisesRegex(ArgumentError,'Face deleted'):face_attachments.resolve_attachments(self.bridge,[self.attachment])

    def test_document_change_and_incomplete_validation_reject_message(self):
        for result in [{'document_id':'other','faces':[]},{'document_id':'doc','faces':[]}]:
            self.bridge.call.return_value={'ok':True,'result':result}
            with self.assertRaises(ArgumentError):face_attachments.resolve_attachments(self.bridge,[self.attachment])
