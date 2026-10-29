"""Exact user-picked BRep faces, retained for this live add-in session.

Native calls run on Fusion's main thread. Handles bind to the actual open
Document and occurrence, so another component instance is never substituted.
"""
import uuid
import adsk.fusion
from errors import ArgumentError
import spatial

if '_faces' not in globals():
    _faces = {}
MAX_FACES = 12
MAX_HANDLES = 256


def resolve(design, ref):
    saved = _faces.get(ref)
    if not saved:
        raise ArgumentError('This face attachment has expired. Select the face again in Fusion and attach it again.')
    if saved['document'] != design.parentDocument:
        raise ArgumentError('This face belongs to another open document. Switch to that document or remove the attachment.')
    found = design.findEntityByToken(saved['token'])
    if len(found) != 1 or not found[0].isValid:
        raise ArgumentError('The attached face was deleted or no longer resolves uniquely. Select it again; no replacement was chosen.')
    face = getattr(found[0], 'nativeObject', None) or found[0]
    if face.objectType != adsk.fusion.BRepFace.classType():
        raise ArgumentError('The attachment no longer identifies a face. Select it again.')
    occurrence = saved['occurrence']
    if occurrence is not None:
        if not occurrence.isValid or face.body.parentComponent != occurrence.component:
            raise ArgumentError('The selected component instance is no longer available. Select the face again.')
        face = face.createForAssemblyContext(occurrence)
        if face is None:
            raise ArgumentError('Cannot resolve the attached face in its selected component instance.')
    elif face.body.parentComponent != design.rootComponent:
        raise ArgumentError('The selected face has moved into a component. Select it again in the assembly.')
    return face


def remember(design, face):
    native = getattr(face, 'nativeObject', None) or face
    occurrence = face.assemblyContext
    if occurrence is None and native.body.parentComponent != design.rootComponent:
        active = design.activeOccurrence
        if active is None or active.component != native.body.parentComponent:
            raise ArgumentError('Select this face in the assembly so its component instance is unambiguous.')
        occurrence = active
    for ref, saved in _faces.items():
        if saved['document'] == design.parentDocument and saved['face'] == native and saved['occurrence'] == occurrence:
            return ref
    ref = 'picked_' + uuid.uuid4().hex[:12]
    _faces[ref] = {'document': design.parentDocument, 'face': native,
                   'token': native.entityToken, 'occurrence': occurrence}
    while len(_faces) > MAX_HANDLES:
        del _faces[next(iter(_faces))]
    return ref


def describe(design, ref):
    face = resolve(design, ref)
    info = spatial.report(face)
    # Compact handle instead of repeating long native tokens in every prompt.
    info['selector'] = {'face_ref': ref}
    info.update(ref=ref, body=face.body.name, area_cm2=face.area,
                occurrence=face.assemblyContext.fullPathName if face.assemblyContext else None)
    return info


def get_selected_faces(design, ui, arguments):
    selected = ui.activeSelections
    faces = []
    ignored = 0
    for i in range(selected.count):
        entity = selected.item(i).entity
        if entity.objectType != adsk.fusion.BRepFace.classType():
            ignored += 1
            continue
        if not entity.isValid:
            raise ArgumentError('A selected face is no longer valid. Select it again.')
        ref = remember(design, entity)
        if ref not in faces:
            faces.append(ref)
    if len(faces) > MAX_FACES:
        raise ArgumentError('Attach at most 12 faces per message. Reduce the current selection.')
    return {'faces': [describe(design, ref) for ref in faces], 'count': len(faces),
            'ignored_non_faces': ignored, 'document': design.parentDocument.name,
            'frame': 'root', 'units': 'cm',
            'hint': 'Select faces in Fusion, then attach them. Bodies, edges and components are not treated as faces.'}


def resolve_selected_faces(design, ui, arguments):
    refs = arguments['refs']
    if len(set(refs)) != len(refs):
        raise ArgumentError('Pass distinct face references')
    return {'faces': [describe(design, ref) for ref in refs], 'count': len(refs),
            'document': design.parentDocument.name, 'frame': 'root', 'units': 'cm'}


def highlight_selected_faces(design, ui, arguments):
    # Validate every face before replacing the user's current selection.
    faces = [resolve(design, ref) for ref in arguments['refs']]
    selected = ui.activeSelections
    previous = [selected.item(i).entity for i in range(selected.count)]
    try:
        selected.clear()
        for face in faces:
            if not selected.add(face):
                raise ArgumentError('Fusion could not highlight this face in the current command. Return to selection mode and retry.')
    except Exception:
        selected.clear()
        for entity in previous:
            if entity.isValid:
                selected.add(entity)
        raise
    return {'highlighted': arguments['refs'], 'count': len(faces), 'geometry_changed': False}
