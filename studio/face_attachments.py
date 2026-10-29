"""Validate face attachments against Fusion before adding them to model input."""
import json
from fusion_cad.errors import ArgumentError


def resolve_attachments(bridge, attachments):
    if not isinstance(attachments, list) or len(attachments) > 12:
        raise ArgumentError('Attach at most 12 faces per message')
    if not attachments:
        return []
    refs, tags, documents = [], set(), set()
    for item in attachments:
        if not isinstance(item, dict):
            raise ArgumentError('Invalid face attachment')
        ref, tag, label, document = (item.get('ref'), item.get('tag'), item.get('label',''), item.get('document_id'))
        if not isinstance(ref,str) or not ref.startswith('picked_') or len(ref)>80:
            raise ArgumentError('Invalid face reference; select and attach the face again')
        if not isinstance(tag,str) or not tag.strip() or len(tag)>32 or tag in tags:
            raise ArgumentError('Face attachment names must be distinct and at most 32 characters')
        if not isinstance(label,str) or len(label)>160:
            raise ArgumentError('Keep each face label under 160 characters')
        if not isinstance(document,str) or not document:
            raise ArgumentError('Face attachment is missing its document. Select and attach it again')
        refs.append(ref); tags.add(tag); documents.add(document)
    if len(set(refs))!=len(refs) or len(documents)!=1:
        raise ArgumentError('Attach distinct faces from one document; remove duplicates or faces from another design')
    outcome=bridge.call('resolve_selected_faces',{'refs':refs},confirm=False)
    if not outcome.get('ok'):
        raise ArgumentError(outcome.get('error',{}).get('message','Could not validate the selected faces'))
    result=outcome['result']
    document=result.get('document_id') or outcome.get('document_id')
    if document not in documents:
        raise ArgumentError('The active document changed. Switch back to the selected faces’ document or reattach them')
    fresh={face['ref']:face for face in result.get('faces',[])}
    if set(fresh)!=set(refs):
        raise ArgumentError('Not all attached faces were resolved; select and attach them again')
    return [{**fresh[item['ref']], 'tag':item['tag'], 'label':item.get('label',''),
             'document_id':document} for item in attachments]


def model_block(faces):
    return {'type':'text','text':
        'User-selected Fusion faces, revalidated for this message. These are exact locations, not inferred from an image. '
        'Use each selector directly with inspect_geometry, measure_geometry, verify_relationship, place_component or extrude_between_faces. '
        'Labels describe user intent; they do not prove a geometric relationship. Do not substitute other faces if a reference becomes invalid.\n'
        +json.dumps(faces,separators=(',',':'))}
