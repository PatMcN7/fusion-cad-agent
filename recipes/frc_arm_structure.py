"""Task-specific arm structure and native interface survey."""
import math
import adsk.core
import adsk.fusion
import spatial
import ops
import pick
from checkpoints import operation
from errors import ArgumentError, ToolFailure
C=2.54

def motor_survey(design,app,args):
    out=[]
    for sel in args['bodies']:
        body=spatial.resolve(design,sel)
        planes=[];pilots=[];holes=[]
        for f in spatial.items(body.faces):
            g=f.geometry
            if g.surfaceType==adsk.core.SurfaceTypes.PlaneSurfaceType:
                o,n=spatial.plane(f)
                if n[2]>.99999 and f.area>1:
                    planes.append((f.area,spatial.report(f)))
            elif g.surfaceType==adsk.core.SurfaceTypes.CylinderSurfaceType and abs(g.axis.z)>.99999:
                if abs(g.radius-.375*C)<.005:pilots.append(spatial.report(f))
                if .17<g.radius<.25:holes.append(spatial.report(f))
        planes.sort(key=lambda x:-x[0])
        out.append({'body':sel,'largest_upward_planes':[p[1] for p in planes[:5]],'pilot_faces':pilots,'mount_hole_candidates':holes})
    return {'interfaces':out}

NAMES=['Arm support structure','Arm upper link','Arm forearm link','Arm wrist carrier']
def _new(root,name):
    o=root.occurrences.addNewComponent(adsk.core.Matrix3D.create());o.component.name=name
    return o

def _plane(comp,axis,value):
    p=comp.constructionPlanes.createInput()
    datum={'x':comp.yZConstructionPlane,'y':comp.xZConstructionPlane,'z':comp.xYConstructionPlane}[axis]
    p.setByOffset(datum,adsk.core.ValueInput.createByReal((-value if axis=='y' else value)*C))
    pl=comp.constructionPlanes.add(p);pl.isLightBulbOn=False
    return pl

def _poly(sk,points):
    pts=[ops._to_sketch(sk,[v*C for v in p]) for p in points]
    for i in range(len(pts)):sk.sketchCurves.sketchLines.addByTwoPoints(pts[i],pts[(i+1)%len(pts)])

def plate(comp,name,poly,y,thick,holes=(),windows=()):
    sk=comp.sketches.add(_plane(comp,'y',y));sk.name=name+' profile'
    _poly(sk,[(x,y,z) for x,z in poly])
    for w in windows:_poly(sk,[(x,y,z) for x,z in w])
    for x,z,r in holes:ops.draw_circles(sk,[(x*C,y*C,z*C)],r*C)
    pr=pick.profiles(sk,loops=1+len(holes)+len(windows)).one()
    f=ops.extrude(comp,pr,thick*C,name=name);f.bodies.item(0).name=name;sk.isVisible=False
    bb=spatial.bounds(f.bodies.item(0))
    if abs(bb['min'][1]/C-y)>1e-6 or abs(bb['max'][1]/C-y-thick)>1e-6:raise ToolFailure('Plate Y extent mismatch: '+name)
    return f.bodies.item(0)

def ring(comp,name,x,z,y,length,od,id=0):
    sk=comp.sketches.add(_plane(comp,'y',y));sk.name=name+' section'
    ops.draw_circles(sk,[(x*C,y*C,z*C)],od*C/2)
    if id:ops.draw_circles(sk,[(x*C,y*C,z*C)],id*C/2)
    f=ops.extrude(comp,pick.profiles(sk,loops=2 if id else 1).one(),length*C,name=name)
    f.bodies.item(0).name=name;sk.isVisible=False
    return f.bodies.item(0)

def tube_x(comp,name,x0,x1,y,width=1,height=2,wall=.125):
    sk=comp.sketches.add(_plane(comp,'x',x0));sk.name=name+' section'
    ops.draw_rect(sk,tuple(v*C for v in (x0,y-width/2,-height/2)),tuple(v*C for v in (x0,y+width/2,height/2)))
    ops.draw_rect(sk,tuple(v*C for v in (x0,y-width/2+wall,-height/2+wall)),tuple(v*C for v in (x0,y+width/2-wall,height/2-wall)))
    f=ops.extrude(comp,pick.profiles(sk,loops=2).one(),(x1-x0)*C,name=name)
    b=f.bodies.item(0);b.name=name;sk.isVisible=False
    return b

def _transform(x,z,dx,dz):
    m=adsk.core.Matrix3D.create();m.setWithCoordinateSystem(adsk.core.Point3D.create(x*C,0,z*C),adsk.core.Vector3D.create(dx,0,dz),adsk.core.Vector3D.create(0,1,0),adsk.core.Vector3D.create(-dz,0,dx));return m

def structure(design,app,args):
    root=design.rootComponent
    if any(o.component.name in NAMES for o in spatial.items(root.occurrences)):raise ArgumentError('Arm structure already exists')
    tops=[spatial.resolve(design,s) for s in args['support_faces']]
    zvals=[spatial.plane(f)[0][2]/C for f in tops]
    if max(zvals)-min(zvals)>1e-6 or any(spatial.plane(f)[1][2]<.99999 for f in tops):raise ArgumentError('Rail support planes changed')
    rail_top=zvals[0];base_top=rail_top+2;shoulder=12.125;L=35;ex=4;ez=math.sqrt(L*L-ex*ex)
    # Hole axes are checked against the existing drilled rails before any edit.
    wanted=[(x+o,y) for x in (-5,5) for o in (-.5,.5) for y in (-14.5,14.5)]
    railaxes=[]
    for name in ('North','South'):
        b=spatial.resolve(design,{'occurrence':'Frame Rail '+name+':1','body':'Frame Rail '+name})
        for f in spatial.items(b.faces):
            g=f.geometry
            if g.surfaceType==adsk.core.SurfaceTypes.CylinderSurfaceType and abs(g.axis.z)>.99999 and abs(g.radius-.098*C)<1e-5:railaxes.append((g.origin.x/C,g.origin.y/C))
    error=max(min(math.hypot(x-a,y-b) for a,b in railaxes) for x,y in wanted)
    if error>1e-6:raise ArgumentError('Crossmember bolt pattern does not match rails')
    made=[];steel=[]
    with operation(design,'frc_arm_structure'):
        static,upper,fore,wrist=[_new(root,n) for n in NAMES]
        sc,uc,fc,wc=[o.component for o in (static,upper,fore,wrist)]
        # Hollow crossmembers have nominal rail contact on their underside.
        for x in (-5,5):
            sk=sc.sketches.add(_plane(sc,'y',-15));sk.name='Crossmember section '+str(x)
            for inset in (0,.125):_poly(sk,[(x-1+inset,-15,rail_top+inset),(x+1-inset,-15,rail_top+inset),(x+1-inset,-15,base_top-inset),(x-1+inset,-15,base_top-inset)])
            f=ops.extrude(sc,pick.profiles(sk,loops=2).one(),30*C,name='Crossmember '+str(x));b=f.bodies.item(0);b.name='Crossmember '+str(x);sk.isVisible=False;made.append(b)
            face=pick.faces(b).planar().facing('+z').extreme('+z').one()
            ops.cut_holes(sc,b,face,[(a*C,y*C,base_top*C) for a,y in wanted if abs(a-x)<1],.098*C,-2*C,name='Rail bolt holes '+str(x))
        # Waterjet profiles; exact bearing seats require finish machining.
        outline=[(-6,base_top),(6,base_top),(1.5,shoulder+1.5),(-1.5,shoulder+1.5)]
        window=[(-3.6,base_top+1.5),(3.6,base_top+1.5),(0,shoulder-2)]
        for y in (-10,9.75):
            holes=[(0,shoulder,15/25.4)]+[(x,z,.098) for x in (-5,5) for z in (base_top+.5,base_top+1)]
            made.append(plate(sc,'A-frame plate '+str(y),outline,y,.25,holes,[window]))
        steel.append(ring(sc,'Shoulder dead axle nominal 30mm',0,shoulder,-10.5,21,30/25.4,.75))
        # Link endplates are bolted to short-ended tube stock.
        for comp,ys,role in ((uc,(-8,8),'Upper'),(fc,(-6.25,6.25),'Forearm')):
            for y in ys:
                tube=tube_x(comp,role+' 31 inch tube '+str(y),2,33,y);made.append(tube)
                holes=[(x,z,.098) for x in (2.5,3.5,L-3.5,L-2.5) for z in (-.5,.5)]
                face=pick.faces(tube).planar().facing('+y').extreme('+y').one()
                ops.cut_holes(comp,tube,face,[(x*C,(y+.5)*C,z*C) for x,z,r in holes],.098*C,-C,name=role+' tube end fasteners '+str(y))
                for end in (0,L):
                    xs=(-1.25,4) if end==0 else (L-4,L+1.25)
                    endholes=[h for h in holes if (h[0]<L/2)==(end==0)]
                    bore=42/25.4 if end==0 else 30/25.4
                    poly=[(xs[0],-1.25),(xs[1],-1.25),(xs[1],1.25),(xs[0],1.25)]
                    for yp in (y-.75,y+.5):
                        made.append(plate(comp,role+' endplate '+str((y,end,yp)),poly,yp,.25,[(end,0,bore/2)]+endholes))
                    if end==0:
                        for yp in (y-.5-7/25.4,y+.5):steel.append(ring(comp,role+' 6806 bearing envelope '+str((y,yp)),0,0,yp,7/25.4,42/25.4,30/25.4))
            # Brace the pair near the root; forearm nests inside the upper pair.
            y0=min(ys)+.5;y1=max(ys)-.5
            sk=comp.sketches.add(_plane(comp,'y',y0));sk.name=role+' crossbrace section'
            for inset in (0,.125):_poly(sk,[(9+inset,y0,-1+inset),(11-inset,y0,-1+inset),(11-inset,y0,1-inset),(9+inset,y0,1-inset)])
            f=ops.extrude(comp,pick.profiles(sk,loops=2).one(),(y1-y0)*C,name=role+' crossbrace');f.bodies.item(0).name=role+' crossbrace';made.append(f.bodies.item(0));sk.isVisible=False
        steel.append(ring(uc,'Elbow dead axle nominal 30mm',L,0,-9,18,30/25.4,.75))
        steel.append(ring(fc,'Wrist dead axle nominal 30mm',L,0,-7.25,14.5,30/25.4,.75))
        # Empty wrist carrier leaves the gripper interface accessible for later.
        for y in (-4,3.75):
            made.append(plate(wc,'Wrist cheek '+str(y),[(-1.25,-1.25),(3,-1.25),(3,1.25),(-1.25,1.25)],y,.25,[(0,0,21/25.4)]))
            steel.append(ring(wc,'Wrist 6806 bearing envelope '+str(y),0,0,y,7/25.4,42/25.4,30/25.4))
        # These plates carry native motor interfaces; reduction gears follow later.
        made.append(plate(sc,'Dual X60 input mounting plate',[(-3,base_top+.25),(3,base_top+.25),(3,9),(-3,9)],-2.5,.25,[(x,6.125,.375) for x in (-1.5,1.5)]+[(x+dx,6.125,.098) for x in (-1.5,1.5) for dx in (-1,1)]))
        for comp,y,z,label in ((uc,-5.5,2,'Elbow'),(fc,4.5,-2,'Wrist')):
            made.append(plate(comp,label+' X44 input mounting plate',[(26.5,z-1.25),(29.5,z-1.25),(29.5,z+1.25),(26.5,z+1.25)],y,.25,[(28,z,.375),(28-.6875,z,.098),(28+.6875,z,.098)]))
        ops.set_material(made,app=app)
        ops.set_material(steel,app=app,keyword='Steel')
        upper.transform2=_transform(0,shoulder,ex/L,ez/L)
        fore.transform2=_transform(ex,shoulder+ez,ex/L,-ez/L)
        wrist.transform2=_transform(8,shoulder,1,0)
        for o in (static,upper,fore,wrist):
            for sk in spatial.items(o.component.sketches):sk.isVisible=False
            for p in spatial.items(o.component.constructionPlanes):p.isLightBulbOn=False
        layout=spatial.resolve(design,{'occurrence':'Arm layout - PROVISIONAL - gripper deferred:1'})
        layout.isLightBulbOn=False
    return {'occurrences':[o.fullPathName for o in (static,upper,fore,wrist)],'aluminum_bodies':len(made),'steel_bodies':len(steel),'rail_hole_axis_error_in':error,'rail_top_in':rail_top,'shoulder_root_z_in':shoulder,'elbow_root_in':[ex,0,shoulder+ez],'wrist_root_in':[8,0,shoulder], 'limitations':'Nominal structural parts and bearing envelopes. Retention/clamps, support and link fasteners, gearbox internals and motion checks not yet complete.'}

def fit_check(design,app,args):
    root=design.rootComponent
    arm_prefixes=NAMES+['Arm Kraken X60','Arm Kraken X44']
    arms=[];obstacles=[];cache={}
    for o in spatial.items(root.allOccurrences):
        if o.fullPathName.startswith('Arm layout -'):continue
        is_arm=any(o.fullPathName.startswith(n+':') for n in arm_prefixes)
        for b in spatial.items(o.component.bRepBodies):
            if not b.isSolid:continue
            p=b.createForAssemblyContext(o);name=o.fullPathName+'/'+b.name
            row=(name,p,spatial.bounds(p))
            (arms if is_arm else obstacles).append(row)
    mgr=adsk.fusion.TemporaryBRepManager.get();collisions=[];tested=0;unresolved=[]
    pairs=[(a,b) for a in arms for b in obstacles]+[(a,b) for i,a in enumerate(arms) for b in arms[i+1:]]
    for (an,a,ab),(bn,b,bb) in pairs:
        if an.startswith('Arm Kraken ') and an.split('+')[0]==bn.split('+')[0]:continue
        if any(ab['max'][k]<=bb['min'][k]+1e-6 or bb['max'][k]<=ab['min'][k]+1e-6 for k in range(3)):continue
        aa=mgr.copy(a);bc=mgr.copy(b)
        try:
            if not mgr.booleanOperation(aa,bc,adsk.fusion.BooleanTypes.IntersectionBooleanType):raise RuntimeError('Kernel returned false')
        except RuntimeError as exc:
            unresolved.append({'a':an,'b':bn,'error':str(exc)});continue
        tested+=1
        if aa.volume>1e-5:collisions.append({'a':an,'b':bn,'volume_cm3':aa.volume})
    mounts=[]
    for motor,child,body_name,plate_occ,plate_name in [
        ('Arm Kraken X60:1','WCP-0940 (1):1','Body4',NAMES[0]+':1','Dual X60 input mounting plate'),
        ('Arm Kraken X60:2','WCP-0940 (1):1','Body4',NAMES[0]+':1','Dual X60 input mounting plate'),
        ('Arm Kraken X44:1','WCP-0941:1','Body1',NAMES[1]+':1','Elbow X44 input mounting plate'),
        ('Arm Kraken X44:2','WCP-0941:1','Body1',NAMES[2]+':1','Wrist X44 input mounting plate')]:
        mb=spatial.resolve(design,{'occurrence':motor+'+'+child,'body':body_name})
        pb=spatial.resolve(design,{'occurrence':plate_occ,'body':plate_name})
        pilots=[];plate_pilots=[];holes=[];plate_holes=[]
        for target,cs,hs in ((mb,pilots,holes),(pb,plate_pilots,plate_holes)):
            for f in spatial.items(target.faces):
                g=f.geometry
                if g.surfaceType!=adsk.core.SurfaceTypes.CylinderSurfaceType or abs(g.axis.y)<.99999:continue
                if abs(g.radius-.375*C)<1e-5:cs.append(g)
                if abs(g.radius-(.20193 if target==mb else .098*C))<1e-5:hs.append(g)
        if not pilots or not plate_pilots:raise ToolFailure('Motor pilot missing')
        pilot=pilots[0]
        axis_error=min(math.hypot(g.origin.x-pilot.origin.x,g.origin.z-pilot.origin.z) for g in plate_pilots)
        matching=[]
        for h in plate_holes:
            err=min(math.hypot(h.origin.x-g.origin.x,h.origin.z-g.origin.z) for g in holes)
            if err<1e-5:matching.append(err)
        mounts.append({'motor':motor,'pilot_axis_error_cm':axis_error,'matching_mount_holes':len(matching)})
    masses={}
    for n in NAMES:
        o=spatial.resolve(design,{'occurrence':n+':1'})
        masses[n]=sum(b.physicalProperties.mass for b in spatial.items(o.component.bRepBodies))
    return {'arm_solid_count':len(arms),'obstacle_solid_count':len(obstacles),'boolean_tests':tested,'collisions':collisions,'unresolved_pairs':unresolved,'motor_mounts':mounts,'native_structure_mass_kg':masses,'passed':not collisions and not unresolved and all(m['pilot_axis_error_cm']<1e-5 and m['matching_mount_holes']>=2 for m in mounts),'scope':'Current folded pose only; excludes hidden centerline layout and internal contacts within each vendor motor. Bearing envelopes approximate mass; imported motor mass not included in structure mass.'}

def repair_y_extents(design,app,args):
    features=[]
    for name in NAMES:
        o=spatial.resolve(design,{'occurrence':name+':1'})
        for f in spatial.items(o.component.features.extrudeFeatures):
            if f.operation==adsk.fusion.FeatureOperations.NewBodyFeatureOperation and '31 inch tube' not in f.name:
                features.append(f)
    features.sort(key=lambda f:f.timelineObject.index)
    changed=[]
    try:
        for f in features:
            distance=abs(f.extentOne.distance.value)
            f.timelineObject.rollTo(True)
            extent=adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByReal(distance))
            if not f.setOneSideExtent(extent,adsk.fusion.ExtentDirections.PositiveExtentDirection,adsk.core.ValueInput.createByString('0 deg')):raise ToolFailure('Could not repair '+f.name)
            changed.append(f.name)
    finally:design.timeline.moveToEnd()
    bad=[]
    for name in NAMES:
        o=spatial.resolve(design,{'occurrence':name+':1'})
        for f in spatial.items(o.component.features):
            if f.healthState==adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:bad.append({'name':f.name,'message':f.errorOrWarningMessage})
    cross=spatial.resolve(design,{'occurrence':NAMES[0]+':1','body':'Crossmember -5'})
    bb=spatial.bounds(cross)
    return {'changed':changed,'feature_errors':bad,'crossmember_bounds_cm':bb,'passed':not bad and abs(bb['min'][1]/C+15)<1e-6 and abs(bb['max'][1]/C-15)<1e-6}

def plate_z(comp,name,poly,z,thick,holes=()):
    sk=comp.sketches.add(_plane(comp,'z',z));sk.name=name+' profile'
    _poly(sk,[(x,y,z) for x,y in poly])
    for x,y,r in holes:ops.draw_circles(sk,[(x*C,y*C,z*C)],r*C)
    f=ops.extrude(comp,pick.profiles(sk,loops=1+len(holes)).one(),thick*C,name=name)
    b=f.bodies.item(0);b.name=name;sk.isVisible=False;return b

def ring_z(comp,name,x,y,z,length,od,id=0):
    sk=comp.sketches.add(_plane(comp,'z',z));sk.name=name+' section'
    ops.draw_circles(sk,[(x*C,y*C,z*C)],od*C/2)
    if id:ops.draw_circles(sk,[(x*C,y*C,z*C)],id*C/2)
    f=ops.extrude(comp,pick.profiles(sk,loops=2 if id else 1).one(),length*C,name=name)
    b=f.bodies.item(0);b.name=name;sk.isVisible=False;return b

def support_details(design,app,args):
    sc=spatial.resolve(design,{'occurrence':NAMES[0]+':1'}).component
    if any(b.name=='Gearbox base platform' for b in spatial.items(sc.bRepBodies)):raise ArgumentError('Support details already exist')
    cb=spatial.resolve(design,{'occurrence':NAMES[0]+':1','body':'Crossmember -5'})
    bb=spatial.bounds(cb)
    if abs(bb['min'][1]/C+15)>1e-6 or abs(bb['max'][1]/C-15)>1e-6:raise ArgumentError('Crossmember span is not repaired')
    alum=[];steel=[];counts={'rail_bolts':0,'link_bolts':0,'crush_sleeves':0}
    with operation(design,'frc_arm_support_details'):
        # Braced mounting platform bears on both crossmembers.
        holes=[(x,y,.098) for x in (-5,5) for y in (-2.5,2.5)]
        platform=plate_z(sc,'Gearbox base platform',[(-6,-3.5),(6,-3.5),(6,3.5),(-6,3.5)],4.125,.25,holes);alum.append(platform)
        for x in (-5,5):
            b=spatial.resolve(design,{'occurrence':NAMES[0]+':1','body':'Crossmember '+str(x)}).nativeObject
            face=pick.faces(b).planar().facing('+z').extreme('+z').one()
            centers=[(x*C,y*C,4.125*C) for y in (-2.5,2.5,-9,9)]
            ops.cut_holes(sc,b,face,centers,.098*C,-2*C,name='Tower and platform attachments '+str(x))
        # Four 1.25-inch angle brackets locate tower feet on crossmembers.
        for x in (-5,5):
            for side in (-1,1):
                # Positive side angle is inside the tower face at Y=9.75.
                vals=[(8.5,4.125),(9.75,4.125),(9.75,5.375),(9.625,5.375),(9.625,4.25),(8.5,4.25)]
                poly=[(side*y,z) for y,z in vals]
                sk=sc.sketches.add(_plane(sc,'x',x-.75));sk.name='Tower foot angle section'
                _poly(sk,[(x-.75,y,z) for y,z in poly])
                f=ops.extrude(sc,sk.profiles.item(0),1.5*C,name='Tower foot angle '+str((x,side)));b=f.bodies.item(0);b.name=f.name;alum.append(b);sk.isVisible=False
                top=pick.faces(b).planar().facing('+z').extreme('-z').one()
                # Through vertical hole from the horizontal leg top.
                ops.cut_holes(sc,b,top,[(x*C,side*9*C,4.25*C)],.098*C,-.125*C,name='Tower foot vertical bolt')
                face=pick.faces(b).planar().facing('+y' if side>0 else '-y').extreme('+y' if side>0 else '-y').one()
                ops.cut_holes(sc,b,face,[(x*C,side*9.75*C,z*C) for z in (4.625,5.125)],.098*C,-.125*C,name='Tower foot horizontal bolts')
        # Simplified installed hardware: unthreaded shanks, heads, washer/nut envelopes.
        def bolt_z(x,y,bottom,top,length,label):
            steel.append(ring_z(sc,label+' shank',x,y,top+.04-length,length,.190))
            steel.append(ring_z(sc,label+' head',x,y,top+.04,.190,.312))
            for z in (top,bottom-.04):steel.append(ring_z(sc,label+' washer',x,y,z,.04,.438,.203))
            steel.append(ring_z(sc,label+' nut envelope',x,y,bottom-.17,.13,.375,.196))
        for x in (-5,5):
            for dx in (-.5,.5):
                for y in (-14.5,14.5):
                    label='Rail clamp '+str((x+dx,y));bolt_z(x+dx,y,.125,4.125,4.5,label);counts['rail_bolts']+=1
                    for z in (.25,2.25):alum.append(ring_z(sc,label+' crush sleeve '+str(z),x+dx,y,z,1.75,.375,.196));counts['crush_sleeves']+=1
            for y in (-9,9,-2.5,2.5):
                top=4.25 if abs(y)==9 else 4.375
                bolt_z(x,y,2.125,top,2.5,'Crossmember top attachment '+str((x,y)))
                alum.append(ring_z(sc,'Crossmember crush sleeve '+str((x,y)),x,y,2.25,1.75,.375,.196));counts['crush_sleeves']+=1
        for cname,ys in ((NAMES[1],(-8,8)),(NAMES[2],(-6.25,6.25))):
            comp=spatial.resolve(design,{'occurrence':cname+':1'}).component
            for y in ys:
                for x in (2.5,3.5,31.5,32.5):
                    for z in (-.5,.5):
                        label='Link bolt '+str((x,y,z));top=y+.75;bottom=y-.75
                        steel.append(ring(comp,label+' shank',x,z,top+.04-1.75,1.75,.190))
                        steel.append(ring(comp,label+' head',x,z,top+.04,.19,.312))
                        for yp in (top,bottom-.04):steel.append(ring(comp,label+' washer',x,z,yp,.04,.438,.203))
                        steel.append(ring(comp,label+' nut envelope',x,z,bottom-.17,.13,.375,.196))
                        alum.append(ring(comp,label+' crush sleeve',x,z,y-.375,.75,.375,.196));counts['link_bolts']+=1;counts['crush_sleeves']+=1
        ops.set_material(alum,app=app);ops.set_material(steel,app=app,keyword='Steel')
    return {'counts':counts,'aluminum_bodies_added':len(alum),'steel_bodies_added':len(steel),'limitations':'Hardware is dimensioned simplified geometry; bolt head/nut detail and threads unmodeled. Motor bracket retention and joint axle clamps remain.'}

def move_bodies(comp,bodies,dx,dy,dz,name):
    objects=adsk.core.ObjectCollection.create()
    for b in bodies:objects.add(b)
    m=adsk.core.Matrix3D.create();m.translation=adsk.core.Vector3D.create(dx*C,dy*C,dz*C)
    f=comp.features.moveFeatures.add(comp.features.moveFeatures.createInput(objects,m));f.name=name
    return f

def clearance_repair(design,app,args):
    sc=spatial.resolve(design,{'occurrence':NAMES[0]+':1'}).component
    fc=spatial.resolve(design,{'occurrence':NAMES[2]+':1'}).component
    if any(f.name=='Forearm inward spacing positive' for f in spatial.items(fc.features.moveFeatures)):raise ArgumentError('Clearance repair already exists')
    pan=spatial.resolve(design,{'occurrence':'Bellypan 6061 1-16:1','body':'Bellypan 6061 1-16'}).nativeObject
    pb=spatial.bounds(pan)
    if abs(pb['min'][2]/C-.0625)>1e-6:raise ArgumentError('Pan underside changed')
    with operation(design,'frc_arm_clearance_repair'):
        face=pick.faces(pan).planar().facing('+z').extreme('+z').one()
        ops.cut_holes(pan.parentComponent,pan,face,[(x*C,y*C,.125*C) for x in (-5.5,-4.5,4.5,5.5) for y in (-14.5,14.5)],.098*C,-.0625*C,name='Arm crossmember through-pan clamp holes')
        lower=[b for b in spatial.items(sc.bRepBodies) if b.name.startswith('Rail clamp ') and (b.name.endswith('washer (1)') or b.name.endswith('nut envelope'))]
        if len(lower)!=16:raise ToolFailure('Expected sixteen lower clamp washers/nuts')
        move_bodies(sc,lower,0,0,-.0625,'Clamp hardware below bellypan')
        groups={-1:[],1:[]}
        for b in spatial.items(fc.bRepBodies):
            if b.name.startswith(('Forearm 31 inch tube','Forearm endplate','Forearm 6806 bearing','Link bolt ')):
                bb=spatial.bounds(b);yc=(bb['min'][1]+bb['max'][1])/2
                groups[1 if yc>0 else -1].append(b)
        for sign,bs in groups.items():move_bodies(fc,bs,0,-sign*.75,0,'Forearm inward spacing '+('positive' if sign>0 else 'negative'))
        brace=next(b for b in spatial.items(fc.bRepBodies) if b.name=='Forearm crossbrace')
        for sign in ('+y','-y'):
            face=pick.faces(brace).planar().facing(sign).extreme(sign).one()
            ops.extrude(fc,face,-.75*C,operation='cut',participants=[brace],name='Narrow forearm crossbrace '+sign)
    return {'pan_holes':8,'lower_hardware_moved':len(lower),'forearm_bodies_moved':{str(k):len(v) for k,v in groups.items()},'forearm_tube_center_y_in':[-5.5,5.5],'brace_bounds_cm':spatial.bounds(brace)}

def pose_check(design,app,args):
    """Sample temporary BRep copies; preserve all live occurrence placements."""
    root=design.rootComponent;mgr=adsk.fusion.TemporaryBRepManager.get()
    group_roots={1:NAMES[1]+':1',2:NAMES[2]+':1',3:NAMES[3]+':1'}
    inverse={}
    for k,n in group_roots.items():
        inverse[k]=spatial.resolve(design,{'occurrence':n}).transform2.copy();inverse[k].invert()
    rows=[]
    for o in spatial.items(root.allOccurrences):
        path=o.fullPathName
        if path.startswith('Arm layout -'):continue
        top=path.split('+')[0]
        group=next((k for k,n in group_roots.items() if top==n),0)
        if top=='Arm Kraken X44:1':group=1
        if top=='Arm Kraken X44:2':group=2
        for b in spatial.items(o.component.bRepBodies):
            if not b.isSolid:continue
            copy=mgr.copy(b.createForAssemblyContext(o))
            if group:mgr.transform(copy,inverse[group])
            rows.append((path+'/'+b.name,copy,group,spatial.bounds(copy) if not group else None))
    initial=math.acos(4/35);results=[];total_tests=0;seen=set()
    for p in args['poses']:
        x=p['x_in'];dz=p['height_in']-16;r=math.hypot(x,dz)
        if r>70 or r<1e-6:raise ArgumentError('Unreachable wrist target '+p['name'])
        midx=x/2;midz=dz/2;h=math.sqrt(35*35-r*r/4)
        options=[(midx+s*(-dz/r)*h,midz+s*(x/r)*h) for s in (-1,1)]
        ex,ez=max(options,key=lambda q:q[1]);ua=math.atan2(ez,ex);fa=math.atan2(dz-ez,x-ex)
        # Rear scoring changes elbow branch by unfolding clear of the shoulder.
        # It must not cross the fully folded singularity at the shoulder axle.
        if x<0:
            while fa<ua:fa+=2*math.pi
            waypoints=[(initial,-initial,0),(math.radians(50),math.radians(50),0),(math.radians(50),math.radians(140),math.pi),(ua,fa,math.pi)]
        else:waypoints=[(initial,-initial,0),(ua,fa,0)]
        trajectory=[]
        for segment,(start,end) in enumerate(zip(waypoints,waypoints[1:])):
            for step in range(1,args.get('steps',4)+1):
                t=step/args.get('steps',4);a,f,wa=[v+(w-v)*t for v,w in zip(start,end)]
                key=tuple(round(v,9) for v in (a,f,wa))
                if key in seen:continue
                seen.add(key);trajectory.append((t,a,f,wa,segment))
        for t,a,f,wa,segment in trajectory:
            exs=35*math.cos(a);ezs=12.125+35*math.sin(a)
            wx=exs+35*math.cos(f);wz=ezs+35*math.sin(f)
            transforms={1:_transform(0,12.125,math.cos(a),math.sin(a)),2:_transform(exs,ezs,math.cos(f),math.sin(f)),3:_transform(wx,wz,math.cos(wa),math.sin(wa))}
            posed=[]
            for name,body,group,fixed_bounds in rows:
                b=mgr.copy(body) if group else body
                if group:mgr.transform(b,transforms[group])
                posed.append((name,b,group,spatial.bounds(b) if group else fixed_bounds))
            collisions=[];unresolved=[];tested=0
            for i,(an,ab,ag,abox) in enumerate(posed):
                for bn,bb,bg,bbox in posed[i+1:]:
                    if ag==bg:continue
                    if any(abox['max'][k]<=bbox['min'][k]+1e-6 or bbox['max'][k]<=abox['min'][k]+1e-6 for k in range(3)):continue
                    copy=mgr.copy(ab)
                    try:
                        if not mgr.booleanOperation(copy,mgr.copy(bb),adsk.fusion.BooleanTypes.IntersectionBooleanType):raise RuntimeError('Kernel returned false')
                    except RuntimeError:
                        unresolved.append({'a':an,'b':bn});continue
                    tested+=1
                    if copy.volume>1e-5:collisions.append({'a':an,'b':bn,'volume_cm3':copy.volume})
            floor=min(bbox['min'][2]/C for n,b,g,bbox in posed if g)
            height=max(bbox['max'][2]/C+3.875 for n,b,g,bbox in posed if g)
            results.append({'pose':p['name'],'segment':segment,'fraction':t,'angles_degrees':[math.degrees(a),math.degrees(f),math.degrees(wa)],'wrist_root_in':[wx,0,wz],'min_moving_z_in':floor,'max_height_above_floor_in':height,'collisions':collisions,'unresolved':unresolved,'passed':not collisions and not unresolved and floor>=-3.875})
            total_tests+=tested
    return {'samples':results,'sample_count':len(results),'boolean_tests':total_tests,'passed':all(p['passed'] for p in results),'scope':'Discrete joint-angle samples from stow to each target. Rigid-group internal contacts checked separately. Excludes gripper, transmission, wiring, bumpers and field obstacles; not a continuous swept-volume certification.'}

def joints(design,app,args):
    root=design.rootComponent
    if any(j.name=='Arm shoulder revolute' for j in spatial.items(root.asBuiltJoints)):raise ArgumentError('Arm joints already exist')
    names=[];revolutes=[]
    with operation(design,'frc_arm_joints'):
        for left,right,body,name in [(NAMES[0],NAMES[1],'Shoulder dead axle nominal 30mm','shoulder'),(NAMES[1],NAMES[2],'Elbow dead axle nominal 30mm','elbow'),(NAMES[2],NAMES[3],'Wrist dead axle nominal 30mm','wrist')]:
            a=spatial.resolve(design,{'occurrence':left+':1'});b=spatial.resolve(design,{'occurrence':right+':1'})
            axle=spatial.resolve(design,{'occurrence':left+':1','body':body})
            face=next(f for f in spatial.items(axle.faces) if f.geometry.surfaceType==adsk.core.SurfaceTypes.CylinderSurfaceType and abs(f.geometry.radius-1.5)<1e-6)
            inp=root.asBuiltJoints.createInput(a,b,ops.joint_geometry(face))
            inp.setAsRevoluteJointMotion(adsk.fusion.JointDirections.CustomJointDirection,face)
            j=root.asBuiltJoints.add(inp);j.name='Arm '+name+' revolute';j.isLightBulbOn=False
            axis=j.jointMotion.rotationAxisVector
            if abs(axis.y)<.99999:raise ToolFailure('Joint axis is not transverse: '+name)
            revolutes.append({'name':j.name,'axis':axis.asArray(),'token':j.entityToken});names.append(j.name)
        pairs=[(NAMES[0]+':1','Frame Rail North:1','support to frame'),('Arm Kraken X60:1',NAMES[0]+':1','left X60 rigid'),('Arm Kraken X60:2',NAMES[0]+':1','right X60 rigid'),('Arm Kraken X44:1',NAMES[1]+':1','elbow motor rigid'),('Arm Kraken X44:2',NAMES[2]+':1','wrist motor rigid')]
        for an,bn,name in pairs:
            a=spatial.resolve(design,{'occurrence':an});b=spatial.resolve(design,{'occurrence':bn})
            a.isGrounded=False
            inp=root.asBuiltJoints.createInput(a,b,None);inp.setAsRigidJointMotion()
            j=root.asBuiltJoints.add(inp);j.name='Arm '+name;j.isLightBulbOn=False;names.append(j.name)
    return {'joints':names,'revolutes':revolutes,'limitations':'Kinematic joints do not substitute for physical axle retention or gear reductions.'}

def motor_brackets(design,app,args):
    sc=spatial.resolve(design,{'occurrence':NAMES[0]+':1'}).component
    if any(b.name=='X60 base angle -3.5' for b in spatial.items(sc.bRepBodies)):raise ArgumentError('Motor bracket details already exist')
    alum=[];steel=[];gaps=[]
    def body(comp,name):return next(b for b in spatial.items(comp.bRepBodies) if b.name==name)
    def drill_y(comp,b,coords):
        face=pick.faces(b).planar().facing('+y').extreme('+y').one();y=spatial.plane(face)[0][1]/C
        bb=spatial.bounds(b)
        ops.cut_holes(comp,b,face,[(x*C,y*C,z*C) for x,z in coords],.098*C,-(bb['max'][1]-bb['min'][1]),name='Bracket mounting holes '+b.name)
    def bolt_y(comp,x,z,bottom,top,length,label):
        steel.append(ring(comp,label+' shank',x,z,top+.04-length,length,.190))
        steel.append(ring(comp,label+' head',x,z,top+.04,.19,.312))
        for y in (top,bottom-.04):steel.append(ring(comp,label+' washer',x,z,y,.04,.438,.203))
        steel.append(ring(comp,label+' nut envelope',x,z,bottom-.17,.13,.375,.196))
    with operation(design,'frc_arm_motor_brackets'):
        for cn,label,y,z,ty in ((NAMES[1],'Elbow',-9,2,-8),(NAMES[2],'Wrist',4.5,-2,5.5)):
            comp=spatial.resolve(design,{'occurrence':cn+':1'}).component
            mount=body(comp,label+' X44 input mounting plate')
            tube=next(b for b in spatial.items(comp.bRepBodies) if b.name.startswith(('Upper 31 inch tube -8' if label=='Elbow' else 'Forearm 31 inch tube 6.25')))
            face=pick.faces(tube).planar().facing('-y').extreme('-y').one()
            tube_y=spatial.plane(face)[0][1]/C
            start,end=(y+.25,tube_y)
            if end<=start:raise ToolFailure('No positive motor standoff gap')
            gaps.append({'joint':label,'tube_face_y_in':tube_y,'standoff_length_in':end-start})
            lo,hi=(-1,.75) if label=='Elbow' else (-.75,1)
            sk=comp.sketches.add(_plane(comp,'y',y));sk.name=label+' motor plate attachment extension'
            _poly(sk,[(26.5,y,lo),(29.5,y,lo),(29.5,y,hi),(26.5,y,hi)])
            ops.extrude(comp,sk.profiles.item(0),.25*C,operation='join',participants=[mount],name=label+' plate structural extension');sk.isVisible=False
            coords=[(x,zz) for x in (27,29) for zz in (-.5,.5)]
            drill_y(comp,mount,coords);drill_y(comp,tube,coords)
            for x,zz in coords:
                alum.append(ring(comp,label+' motor standoff '+str((x,zz)),x,zz,start,end-start,.375,.196))
                alum.append(ring(comp,label+' mount crush sleeve '+str((x,zz)),x,zz,ty-.375,.75,.375,.196))
                bolt_y(comp,x,zz,y,ty+.5,1.75,label+' mount bolt '+str((x,zz)))
        # Short machine screws are represented at their thread-root diameter.
        for cn,label,y,z,xs,sign in [(NAMES[0],'X60',-2.25,6.125,[-2.5,-.5,.5,2.5],1),(NAMES[1],'Elbow X44',-8.75,2,[27.3125,28.6875],1),(NAMES[2],'Wrist X44',4.75,-2,[27.3125,28.6875],1)]:
            comp=spatial.resolve(design,{'occurrence':cn+':1'}).component
            for x in xs:
                headbase=y+sign*.04
                steel.append(ring(comp,label+' 10-32 motor screw core '+str(x),x,z,headbase-.5 if sign>0 else headbase,.5,.156))
                steel.append(ring(comp,label+' motor screw head '+str(x),x,z,headbase if sign>0 else headbase-.19,.19,.312))
                steel.append(ring(comp,label+' motor screw washer '+str(x),x,z,y if sign>0 else y-.04,.04,.438,.203))
        baseplate=body(sc,'Dual X60 input mounting plate')
        for lo,hi in ((-4,-3),(3,4)):
            sk=sc.sketches.add(_plane(sc,'y',-2.5));sk.name='X60 plate mounting ear'
            _poly(sk,[(lo,-2.5,4.375),(hi,-2.5,4.375),(hi,-2.5,9),(lo,-2.5,9)])
            ops.extrude(sc,sk.profiles.item(0),.25*C,operation='join',participants=[baseplate],name='X60 plate mounting ear');sk.isVisible=False
        for x in (-3.5,3.5):
            vals=[(-2.25,4.375),(-1,4.375),(-1,4.5),(-2.125,4.5),(-2.125,5.625),(-2.25,5.625)]
            sk=sc.sketches.add(_plane(sc,'x',x-.5));sk.name='X60 base angle section'
            _poly(sk,[(x-.5,y,z) for y,z in vals]);f=ops.extrude(sc,sk.profiles.item(0),C,name='X60 base angle '+str(x));b=f.bodies.item(0);b.name=f.name;sk.isVisible=False;alum.append(b)
            drill_y(sc,b,[(x,z) for z in (4.875,5.375)])
            drill_y(sc,body(sc,'Dual X60 input mounting plate'),[(x,z) for z in (4.875,5.375)])
            for z in (4.875,5.375):bolt_y(sc,x,z,-2.5,-2.125,.75,'X60 angle bolt '+str((x,z)))
            face=pick.faces(b).planar().facing('+z').extreme('-z').one()
            ops.cut_holes(sc,b,face,[(x*C,-1.5*C,4.5*C)],.098*C,-.125*C,name='X60 angle platform hole')
            platform=body(sc,'Gearbox base platform');face=pick.faces(platform).planar().facing('+z').extreme('+z').one()
            ops.cut_holes(sc,platform,face,[(x*C,-1.5*C,4.375*C)],.098*C,-.25*C,name='Platform X60 anchor hole')
            for part,z,l,od,id in [('shank',4.54-.75,.75,.19,0),('head',4.54,.19,.312,0),('upper washer',4.5,.04,.438,.203),('lower washer',4.085,.04,.438,.203),('nut envelope',3.955,.13,.375,.196)]:steel.append(ring_z(sc,'X60 platform bolt '+str(x)+' '+part,x,-1.5,z,l,od,id))
        for x in (-5,5):
            for side in (-1,1):
                for z in (4.625,5.125):bolt_y(sc,x,z,9.625 if side>0 else -10,10 if side>0 else -9.625,.75,'Tower angle bolt '+str((x,side,z)))
        ops.set_material(alum,app=app);ops.set_material(steel,app=app,keyword='Steel')
    return {'measured_standoff_gaps':gaps,'aluminum_added':len(alum),'steel_added':len(steel),'motor_screw_under_head_length_in':.5,'engagement_in':.21,'limitations':'Screw threads and nut wrenching geometry omitted. Joint axle clamps and transmissions remain.'}

def elbow_outboard(design,app,args):
    upper=spatial.resolve(design,{'occurrence':NAMES[1]+':1'});comp=upper.component
    b=next(b for b in spatial.items(comp.bRepBodies) if b.name=='Elbow X44 input mounting plate')
    bb=spatial.bounds(b)
    if abs(bb['min'][1]/C+5.5)>1e-6:raise ArgumentError('Elbow plate is not at original placement')
    motor=spatial.resolve(design,{'occurrence':'Arm Kraken X44:1'})
    with operation(design,'frc_arm_elbow_outboard'):
        move_bodies(comp,[b],0,-3.5,0,'Elbow motor plate outboard')
        origin=adsk.core.Point3D.create(28*C,-9*C,2*C);origin.transformBy(upper.transform2)
        ux=adsk.core.Vector3D.create(1,0,0);uy=adsk.core.Vector3D.create(0,0,-1);uz=adsk.core.Vector3D.create(0,1,0)
        for v in (ux,uy,uz):v.transformBy(upper.transform2)
        m=adsk.core.Matrix3D.create();m.setWithCoordinateSystem(origin,ux,uy,uz);motor.transform2=m
    return {'plate_local_y_in':[-9,-8.75],'motor_front_root_cm':origin.asArray(),'shaft_direction_root':uz.asArray()}
