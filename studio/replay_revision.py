#!/usr/bin/env python3
"""Opt-in live replay of the September 5 GLM revision session.

Creates a disposable Fusion document and uses the configured paid model. Never
invoked by unit tests. Keep the initial request explicit about creating a new doc.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import transcript

REQUESTS = [
    'Create a new 8 × 6 × 0.25 inch plate in a separate document. Add an isogrid with 2-inch cells, 0.25-inch ribs, and corners rounded for a 0.125-inch cutter',
    'Instead of in the sketch having the fillet, can you make a fillet feature? So generate the sketch as triangles and then add a fillet',
    "No they aren't the sketch itself still has a fillet even though you added a fillet feautre.",
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    args=parser.parse_args()
    session=server.Session()
    provider=session.ensure()
    print('Transcript:',session.recorder.path,flush=True)
    def show(kind,data):
        if kind in {'tool_start','tool_end','usage','vision_usage','provider_timing','error'}:
            if kind=='tool_end': data={k:data.get(k) for k in ('name','ok')}
            if kind=='tool_start': data={k:data.get(k) for k in ('name','arguments')}
            print(kind,json.dumps(data),flush=True)
        elif kind=='text': print(data.get('text',''),end='',flush=True)
        elif kind=='done':print('\nTURN COMPLETE',flush=True)
    emit=transcript.tee(session.recorder,show)
    try:
        for request in REQUESTS:
            session.recorder.user_message(request)
            print('\nUSER:',request,flush=True)
            server.Agent(session.bridge,provider).run([{'type':'text','text':request}],emit)
    finally: session.recorder.close(reason='live revision replay')
    tree = session.bridge.call('feature_tree',{},False)
    print('Final feature tree:',json.dumps(tree),flush=True)
    assert tree['ok'], tree
    rows = tree['result']['entries']
    assert len(rows) == 5, 'Expected plate sketch/extrude and lattice sketch/cut/fillet only'
    assert all(r.get('health') == 0 for r in rows), 'Unhealthy final native feature'
    sketches = [r for r in rows if r['type'] == 'Sketch']
    assert len(sketches) == 2
    lattice = next(r for r in sketches if r['curves']['non_reference_lines'] == 51)
    assert lattice['curves']['arcs'] == lattice['curves']['circles'] == 0
    assert sum(r['type']=='FilletFeature' for r in rows) == 1
    print('Native acceptance: PASS (51 triangle lines, one native fillet, five healthy timeline entries)',flush=True)


if __name__=='__main__':main()
