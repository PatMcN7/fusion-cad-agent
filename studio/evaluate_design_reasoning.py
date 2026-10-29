#!/usr/bin/env python3
"""Fresh-context prompt ablation on three simulated, measurement-dependent tasks.

Uses the configured real model but NO live Fusion connection. Both conditions
receive identical tools and deterministic geometry fixtures. Measures evidence
before construction, not physical CAD correctness. Outputs full traces and costs.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import envfile
import providers
import agent_runtime
from providers import ToolSpec, ToolResult
from fusion_cad.design_state import DesignState, TOOLS, obj, TEXT
from fusion_cad.spatial_contracts import SPECS
from fusion_cad.tool_contracts import validate

CASES=[
 {'id':'bearing_spacer','prompt':'Make a solid spacer between the two already positioned bearing blocks. Its section should match the selected mounting pad; its length must fit the actual available gap. The fixture has no other bodies.','gap':3.7},
 {'id':'enclosure_standoff','prompt':'Create a standoff from the circuit board support pad up to the inside lid face in this enclosure. Match the support pad section and fit the actual lid position. The two fixture bodies are already positioned.','gap':1.25},
 {'id':'fixture_stop','prompt':'Create a rectangular stop extending from the fixture backing pad to the workpiece datum face. Its size must follow those existing interfaces. The fixture bodies are already positioned.','gap':6.35},
]


def run(case, condition, output, max_steps):
    model=os.environ.get('STUDIO_MODEL','z-ai/glm-5.3-flash')
    provider=providers.build(os.environ.get('STUDIO_PROVIDER','openrouter'),model,
        base_url=os.environ.get('STUDIO_BASE_URL') or None,effort=os.environ.get('STUDIO_EFFORT','low'))
    provider.max_output_tokens=2048
    state=DesignState();calls=[];events=[];measured=False;built=False;unsupported=0
    registry={**TOOLS}
    for name in ['inspect_geometry','measure_geometry','verify_relationship','extrude_between_faces']:
        mutation,description,schema=SPECS[name]
        registry[name]={'description':description,'mutating':mutation,'input_schema':schema}
    # Both arms have identical tools. Only dependency guidance/pinned state differ.
    registry['extrude_polygons']={'description':'Create a rectangular section by explicit coordinates and height in cm.', 'input_schema':obj({'height':TEXT},['height'])}
    specs=[ToolSpec(name=n,description=s['description'],properties=s['input_schema']['properties'],required=s['input_schema'].get('required',[]),additional_properties=False) for n,s in registry.items()]
    prompt=agent_runtime.PROMPT
    if condition=='execution_only':
        start=prompt.index('For multi-part or open-ended design,')
        end=prompt.index('The initial tools cover')
        prompt=prompt[:start]+prompt[end:]
    prompt+='\nThis is a simulated CAD evaluation. All available tools are already loaded; no load_tools call or viewport capture is needed. Use the supplied fixture geometry.'
    provider.add_user([{'type':'text','text':case['prompt']}])
    started=time.monotonic()
    for step in range(max_steps):
        turn=provider.stream(prompt+(state.prompt() if condition=='dependencies' else ''),specs,lambda k,d:events.append({'kind':k,'data':d}))
        if turn.usage:events.append({'kind':'usage','data':turn.usage})
        if not turn.tool_calls:break
        results=[]
        for call in turn.tool_calls:
            ok=True
            try:
                if call.name not in registry:raise ValueError('Tool not available')
                validate(call.arguments,registry[call.name]['input_schema'])
                if call.name=='update_design_state':payload=state.update(call.arguments)
                elif call.name=='inspect_geometry':
                    payload={'frame':'root','units':'cm','faces':[
                        {'selector':{'token':'support'},'role':'existing mounting pad, use as section','normal':[0,0,1],'bounds_cm':{'min':[0,0,0],'max':[1,1,0]}},
                        {'selector':{'token':'limit'},'role':'opposing datum face, use as extent','normal':[0,0,-1]}],
                        'hint':'Use exact selectors to measure their separation. Bounds of the opposing face are intentionally omitted from this focused fixture.'}
                elif call.name in {'measure_geometry','verify_relationship','extrude_between_faces'}:
                    a=call.arguments.get('a',call.arguments.get('start',{})).get('token')
                    b=call.arguments.get('b',call.arguments.get('end',{})).get('token')
                    if {a,b}!={'support','limit'}:raise ValueError('Use the returned fixture face selectors')
                    if call.name=='extrude_between_faces':
                        unsupported+=not measured;built=True;state.invalidate('fixture')
                        payload={'completion':'completed','extent_type':'ToEntityExtentDefinition','plane_separation_cm':case['gap']}
                    elif call.name=='measure_geometry':
                        measured=True;payload={'distance_cm':case['gap'],'frame':'root','units':'cm','a':call.arguments['a'],'b':call.arguments['b']}
                    else:
                        expected=call.arguments.get('expected',str(case['gap'])+' cm')
                        # Deliberately narrow fixture units; other forms return a tool error.
                        value=float(expected.removesuffix(' cm'))
                        passed=abs(value-case['gap'])<1e-6
                        payload={'passed':passed,'actual':case['gap'],'units':'cm'}
                elif call.name=='extrude_polygons':
                    unsupported+=not measured
                    value=float(call.arguments['height'].removesuffix(' cm'))
                    built=abs(value-case['gap'])<1e-6
                    state.invalidate('fixture');payload={'completion':'completed','matches_gap':built}
                else:raise ValueError('Unsupported fixture operation')
                payload=state.observe(call.name,call.arguments,payload,'fixture')
            except Exception as exc:
                ok=False;payload={'message':str(exc)}
            calls.append({'name':call.name,'arguments':call.arguments,'ok':ok,'result':payload})
            results.append(ToolResult(call.id,call.name,ok,payload))
        provider.add_tool_results(results)
    usage=[e['data'] for e in events if e['kind']=='usage']
    summary={'case':case['id'],'condition':condition,'model':model,'seconds':round(time.monotonic()-started,2),
             'calls':len(calls),'failed_calls':sum(not c['ok'] for c in calls),'constructed':built,
             'construction_before_measurement':unsupported,'design_entries':len(state.entries),
             'output_tokens':sum(u.get('output_tokens',0) for u in usage),'input_tokens':sum(u.get('input_tokens',0) for u in usage),
             'cost_usd':sum(u.get('cost_usd',0) or 0 for u in usage)}
    output.mkdir(parents=True,exist_ok=True)
    (output/(case['id']+'-'+condition+'.json')).write_text(json.dumps({'summary':summary,'case':case,'prompt':prompt,'calls':calls,'events':events,'design_state':state.snapshot()},indent=2))
    print(json.dumps(summary),flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live-model',action='store_true');parser.add_argument('--output',default='/tmp/fusion-design-reasoning-eval');parser.add_argument('--max-steps',type=int,default=8)
    args=parser.parse_args()
    if not args.live_model:parser.error('Pass --live-model to use the configured API model; CAD is simulated')
    envfile.load()
    output=Path(args.output);summaries=[]
    for case in CASES:
        for condition in ['execution_only','dependencies']:
            summaries.append(run(case,condition,output,args.max_steps))
    (output/'summary.json').write_text(json.dumps(summaries,indent=2))

if __name__=='__main__':main()
