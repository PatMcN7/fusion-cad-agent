"""Installed blind-rivet representation and native hardware fit checks.

Vendor WCP-0449 head/body dimensions; formed tail is an explicit approximation.
Only adds components; never edits frame, pan, or imported module geometry.
"""
import math
import adsk.core
import adsk.fusion
import ops
import pick
import spatial
from checkpoints import operation
from errors import ArgumentError, ToolFailure

C = 2.54

def _body(design, name):
    occ = spatial.occurrence(design, name)
    bodies = spatial.items(occ.component.bRepBodies)
    if len(bodies) != 1:
        raise ArgumentError('Expected one body in '+name)
    return bodies[0].createForAssemblyContext(occ)

def _axes(body, radius):
    return [f.geometry for f in spatial.items(body.faces)
            if f.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType
            and abs(f.geometry.radius-radius)<1e-6]

def _axis_distance(a,b):
    av,bv = spatial.xyz(a.axis),spatial.xyz(b.axis)
    if abs(spatial.dot(av,bv)) < .999999:
        return math.inf
    delta=spatial.sub(spatial.xyz(a.origin),spatial.xyz(b.origin))
    along=spatial.dot(delta,av)
    return spatial.length([delta[i]-along*av[i] for i in range(3)])

def rivets(design, app, args):
    root=design.rootComponent
    pan=_body(design,args['pan'])
    rails=[_body(design,n) for n in args['rails']]
    dim={k:design.unitsManager.evaluateExpression(args[k],'cm') for k in
         ('hole_diameter','body_diameter','head_diameter','head_height','tail_diameter','tail_height')}
    holes=_axes(pan,dim['hole_diameter']/2)
    if len(holes)!=32 or any(abs(h.axis.z)<.999999 for h in holes):
        raise ArgumentError('Expected the 32 established vertical pan mounting holes')
    # Explicitly inspected faces, with geometric guards against stale topology.
    bottom=pan.faces.item(417)
    pz,pn=spatial.plane(bottom)
    if pn[2]>-.999999:
        raise ArgumentError('Pan bottom face changed; re-inspect')
    grips=[]
    for hole in holes:
        matches=[b for b in rails if any(_axis_distance(hole,h)<1e-6 for h in _axes(b,dim['hole_diameter']/2))]
        if len(matches)!=1:
            raise ArgumentError('Pan hole does not uniquely align to a rail')
        inner=matches[0].faces.item(128)
        origin,normal=spatial.plane(inner)
        if normal[2]<.999999:
            raise ArgumentError('Inspected rail inner-bottom face changed')
        grips.append(origin[2]-pz[2])
    grip=grips[0]
    if max(abs(g-grip) for g in grips)>1e-6 or not .125*C<=grip<=.250*C:
        raise ArgumentError('Grip is outside the WCP-0449 range or differs between joints')
    if not 0<dim['body_diameter']<dim['hole_diameter']<min(dim['head_diameter'],dim['tail_diameter']):
        raise ArgumentError('Invalid rivet bearing/body diameters')
    name='WCP-0449 Rivet - installed simplified'
    if any(o.component.name==name for o in spatial.items(root.occurrences)):
        raise ArgumentError('Rivet component already exists')
    with operation(design,'installed_bellypan_rivets'):
        occ=root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp=occ.component
        comp.name=name
        headsk=ops.sketch_on(comp,comp.xYConstructionPlane,'Rivet head underside datum')
        ops.draw_circles(headsk,[(0,0,0)],dim['head_diameter']/2)
        head=ops.extrude(comp,pick.profiles(headsk).one(),-dim['head_height'],name='Factory head')
        body=head.bodies.item(0)
        shaftsk=ops.sketch_on(comp,comp.xYConstructionPlane,'Rivet grip section')
        ops.draw_circles(shaftsk,[(0,0,0)],dim['body_diameter']/2)
        ops.extrude(comp,pick.profiles(shaftsk).one(),grip,operation='join',name='Shank through material stack')
        tailplane=ops.plane_at(comp,grip,'Blind-side grip plane')
        tailsk=ops.sketch_on(comp,tailplane,'Approximate formed blind head')
        ops.draw_circles(tailsk,[(0,0,grip)],dim['tail_diameter']/2)
        ops.extrude(comp,pick.profiles(tailsk).one(),dim['tail_height'],operation='join',name='Approximate upset tail')
        body.name='Installed aluminum blind rivet'
        ops.set_material([body],app=app,keyword='Aluminum')
        for sk in spatial.items(comp.sketches):sk.isVisible=False
        tailplane.isLightBulbOn=False
        placed=[]
        for i,h in enumerate(holes):
            transform=adsk.core.Matrix3D.create()
            transform.translation=adsk.core.Vector3D.create(h.origin.x,h.origin.y,pz[2])
            member=occ if i==0 else root.occurrences.addExistingComponent(comp,transform)
            # addExistingComponent can compose the original occurrence placement;
            # explicitly set each returned occurrence to the desired root transform.
            member.transform2=transform
            if spatial.length(spatial.sub(spatial.xyz(member.transform2.translation),spatial.xyz(transform.translation)))>1e-6:
                raise ToolFailure('Rivet occurrence did not retain its root placement')
            placed.append(member.fullPathName)
        if design.snapshots.hasPendingSnapshot:design.snapshots.add()
    return {'rivets':placed,'count':len(placed),'grip_in':grip/C,'head_seat_z_in':pz[2]/C,
            'representation':'Installed simplified; .280 x .100 in formed tail is approximate; pulled mandrel omitted'}

def validate(design,app,args):
    hardware=[_body(design,n) for n in args['hardware']]
    rails=[_body(design,n) for n in args['rails']]
    pan=_body(design,args['pan'])
    railaxes=[g for b in rails for g in _axes(b,.196*C/2)]
    panaxes=_axes(pan,.196*C/2)
    errors=[]
    plugs=0
    rivet_count=0
    head_errors=[]
    for name,b in zip(args['hardware'],hardware):
        if name.startswith('WCP-2067'):
            plugs+=1
            for index in (18,20,21,22,23,24,26):
                g=b.faces.item(index).geometry
                if g.surfaceType!=adsk.core.SurfaceTypes.CylinderSurfaceType or abs(g.radius-.20193)>1e-6:
                    raise ArgumentError('Plug mounting face identity changed')
                errors.append(min(_axis_distance(g,h) for h in railaxes))
        else:
            rivet_count+=1
            axes=_axes(b,.189*C/2)
            if len(axes)!=1:raise ToolFailure('Expected one rivet shank')
            errors.append(min(_axis_distance(axes[0],h) for h in panaxes))
            # Factory head upper bearing surface must meet pan underside.
            planes=[spatial.plane(f) for f in spatial.items(b.faces)
                    if f.geometry.surfaceType==adsk.core.SurfaceTypes.PlaneSurfaceType]
            seat=spatial.plane(pan.faces.item(417))[0][2]
            head_errors.append(min(abs(p[2]-seat) for p,n in planes if n[2]>.999999))
    manager=adsk.fusion.TemporaryBRepManager.get()
    others=[]
    for occ in spatial.items(design.rootComponent.allOccurrences):
        if occ.fullPathName in args['hardware']:continue
        for b in spatial.items(occ.component.bRepBodies):
            if b.isSolid:others.append((occ.fullPathName,b.createForAssemblyContext(occ)))
    pairs=[(args['hardware'][i],a,n,b) for i,a in enumerate(hardware) for n,b in others]
    pairs += [(args['hardware'][i],a,args['hardware'][j],hardware[j])
              for i,a in enumerate(hardware) for j in range(i+1,len(hardware))]
    boxes={id(b):spatial.bounds(b) for b in hardware+[b for _,b in others]}
    collisions=[]
    tested=0
    for an,a,bn,b in pairs:
        ba,bb=boxes[id(a)],boxes[id(b)]
        if any(ba['max'][i]<=bb['min'][i]+1e-7 or bb['max'][i]<=ba['min'][i]+1e-7 for i in range(3)):continue
        one,two=manager.copy(a),manager.copy(b)
        if not manager.booleanOperation(one,two,adsk.fusion.BooleanTypes.IntersectionBooleanType):
            raise ToolFailure('Hardware intersection calculation failed')
        tested+=1
        if one.volume>1e-6:collisions.append({'a':an,'b':bn,'volume_cm3':one.volume})
    return {'plugs':plugs,'rivets':rivet_count,'mounting_axes_checked':len(errors),
            'max_axis_error_cm':max(errors,default=0),'max_head_seat_error_cm':max(head_errors,default=0),
            'tested_solid_intersections':tested,'collisions':collisions,
            'passed':not collisions and max(errors+head_errors,default=0)<1e-6}
