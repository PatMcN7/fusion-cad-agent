#!/usr/bin/env python3
"""Exercise spatial tools in a separate unsaved document, restoring active document.

Run explicitly with --live. Creates two datum plates, a referenced extrusion and
one rotated component copy. Leaves this small validation document for inspection.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bridge_client import Bridge


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--output',default='/tmp/fusion-spatial-validation.json')
    parser.add_argument('--resume-document',type=int,help='Reuse an existing validation document containing only the two datum plates')
    args=parser.parse_args()
    if not args.live:parser.error('Pass --live to create an isolated validation document in Fusion')
    bridge=Bridge();registry=bridge.registry();events=[]
    def call(name,arguments=None):
        result=bridge.call(name,arguments or {},confirm=bool(registry[name].get('mutating')))
        events.append({'tool':name,'arguments':arguments or {},'response':result})
        Path(args.output).write_text(json.dumps(events,indent=2))
        if not result.get('ok'):raise RuntimeError(json.dumps(result.get('error')))
        print(name, 'ok',flush=True)
        return result['result']
    docs=call('list_documents')
    original=next((d['index'] for d in docs['documents'] if d.get('active')),None)
    try:
        if args.resume_document is None:
            call('create_document')
            for name,z in [('Lower datum','0 cm'),('Upper datum','5 cm')]:
                call('extrude_polygons',{'polygons':[[[0,0],[1,0],[1,1],[0,1]]],'units':'cm','height':'0.1 cm','base':z,'name':name})
        else:
            call('switch_document',{'index':args.resume_document})
            listing=call('inspect_geometry',{'limit':50})
            assert sorted(e.get('name') for e in listing['entities'])==['Lower datum','Upper datum'], 'Not a clean validation fixture'
        def face(body,z):
            found=call('inspect_geometry',{'entity':{'body':body},'limit':12})
            return next(f['selector'] for f in found['faces'] if 'plane_origin_cm' in f and abs(f['plane_origin_cm'][2]-z)<1e-6 and abs(f['normal'][2])>.99)
        a,b=face('Lower datum',.1),face('Upper datum',5)
        measured=call('measure_geometry',{'a':a,'b':b,'mode':'plane_separation'})
        assert abs(measured['distance_cm']-4.9)<1e-6
        call('update_design_state',{'upsert':[{'id':'span','statement':'Separation between datum faces','kind':'derived','status':'established','evidence':[measured['evidence_ref']]}],'next_action':'Extrude the selected section up to the opposite datum'})
        extruded=call('extrude_between_faces',{'start':a,'end':b,'name':'Referenced span'})
        box=extruded['body']['bounds_cm'];assert abs(box['max'][2]-box['min'][2]-4.9)<1e-6
        assert call('update_design_state')['entries'][0]['status']=='stale'
        verified=call('verify_relationship',{'a':a,'b':b,'relation':'plane_distance','expected':'49 mm'})
        assert verified['passed']
        rejected=call('verify_relationship',{'a':a,'b':b,'relation':'plane_distance','expected':'40 mm'})
        assert not rejected['passed']
        call('bodies_to_components',{'bodies':['Lower datum','Upper datum']})
        listing=call('inspect_geometry',{'limit':50})
        lower=next(e['selector']['occurrence'] for e in listing['entities'] if 'Lower datum' in e.get('name',''))
        copied=call('place_component',{'occurrence':lower,'anchor':{'occurrence':lower},'position':['10 cm','2 cm','3 cm'],'rotation_axis':[0,0,1],'rotation_degrees':90,'copy':True})
        assert copied['anchor_error_cm']<1e-6
        assert abs(sum(copied['bounds_cm'][k][0] for k in ['min','max'])/2-10)<1e-6
        moved=call('place_component',{'occurrence':copied['occurrence'],'anchor':{'occurrence':copied['occurrence']},'target':{'occurrence':lower},'offset':['5 cm','0 cm','0 cm']})
        assert moved['anchor_error_cm']<1e-6
        measure=call('measure_geometry',{'a':{'occurrence':lower},'b':{'occurrence':copied['occurrence']}})
        assert abs(measure['distance_cm']-4)<1e-5
        component=call('inspect_geometry',{'entity':{'occurrence':copied['occurrence']}})
        copied_faces=call('inspect_geometry',{'entity':component['bodies'][0]['selector']})['faces']
        original_component=call('inspect_geometry',{'entity':{'occurrence':lower}})
        original_faces=call('inspect_geometry',{'entity':original_component['bodies'][0]['selector']})['faces']
        def positive_x(faces):return next(f['selector'] for f in faces if f.get('normal',[0,0,0])[0]>.99)
        proxy_measure=call('measure_geometry',{'a':positive_x(original_faces),'b':positive_x(copied_faces),'mode':'plane_separation'})
        assert abs(proxy_measure['distance_cm']-5)<1e-6, 'Face proxies must measure in root coordinates after rotation and translation'
        call('capture_viewport',{'views':['iso'],'clean':True,'prefix':'design_tools_validation','width':900,'height':700,'expected_stage':'Two thin datum plates connected by a square vertical extrusion, and one separate copied plate to the side.'})
        print('PASS: native measurement, referenced extrusion, passing/failing relation checks, copy/rotation/placement, evidence invalidation',flush=True)
    finally:
        if original is not None:call('switch_document',{'index':original})

if __name__=='__main__':main()
