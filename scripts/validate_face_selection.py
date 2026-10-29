"""Run inside Fusion via a temporary validation handler; no geometry changes.

Exercise actual UI selection, exact handles, component proxies and measurement,
then restore the user's selection. Do not leave the handler registered afterward.
"""
import adsk.fusion
import face_selection
import spatial


def run_in_fusion(app, ui):
    design=adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise RuntimeError('Open a design containing faces for the selection smoke test')
    selection=ui.activeSelections
    previous=[selection.item(i).entity for i in range(selection.count)]
    candidates=[]
    for occurrence in spatial.items(design.rootComponent.allOccurrences):
        if occurrence.component.bRepBodies.count:
            body=occurrence.component.bRepBodies.item(0)
            if body.faces.count:
                candidates.append(body.faces.item(0).createForAssemblyContext(occurrence))
        if len(candidates)>=2:break
    if len(candidates)<2:
        for body in spatial.items(design.rootComponent.bRepBodies):
            for i in range(min(body.faces.count,2)):
                candidates.append(body.faces.item(i))
            if len(candidates)>=2:break
    if len(candidates)<2:
        raise RuntimeError('Need at least two existing faces; this test creates no geometry')
    try:
        selection.clear()
        for face in candidates[:2]:
            assert selection.add(face), 'Native selection add failed'
        selected=face_selection.get_selected_faces(design,ui,{})
        assert selected['count']==2
        refs=[face['ref'] for face in selected['faces']]
        reread=face_selection.get_selected_faces(design,ui,{})
        assert refs==[face['ref'] for face in reread['faces']], 'Duplicate attachments should retain identity'
        for actual,expected in zip(refs,candidates):
            resolved=spatial.resolve(design,{'face_ref':actual})
            assert (resolved.nativeObject or resolved)==(expected.nativeObject or expected)
            assert resolved.assemblyContext==expected.assemblyContext
        measured=spatial.measure_geometry(design,app,{'a':{'face_ref':refs[0]},'b':{'face_ref':refs[1]}})
        face_selection.highlight_selected_faces(design,ui,{'refs':[refs[1]]})
        assert selection.count==1
        highlighted=selection.item(0).entity
        assert (highlighted.nativeObject or highlighted)==(candidates[1].nativeObject or candidates[1])
        selection.clear();assert selection.add(candidates[0].body)
        ignored=face_selection.get_selected_faces(design,ui,{})
        assert ignored['count']==0 and ignored['ignored_non_faces']==1
        return {'passed':True,'faces':selected['faces'],'measurement':measured,
                'checks':['native UI selection','stable exact handles','occurrence identity','measurement with handles','highlight exact face','no body-to-face guessing'],
                'geometry_changed':False}
    finally:
        selection.clear()
        for entity in previous:
            if entity.isValid:selection.add(entity)
