"""Provisional 6328-style arm layout. Visualization geometry, not fabrication parts."""
import math
import adsk.core
import adsk.fusion
import ops
import spatial
from checkpoints import operation
from errors import ArgumentError, ToolFailure
C = 2.54
NAME = 'Arm layout - PROVISIONAL - gripper deferred'

def solve(length, shoulder, x, height):
    dz = height - shoulder
    r = math.hypot(x, dz)
    if not 0 < r <= 2*length:
        raise ArgumentError('Wrist target outside two-link workspace')
    rise = math.sqrt(max(0, length*length-r*r/4))
    # Upper elbow branch; mirror the entire pose for rear targets.
    sign = 1 if x >= 0 else -1
    return (x/2-sign*dz/r*rise, shoulder+dz/2+abs(x)/r*rise)

def build(design, app, args):
    root = design.rootComponent
    if any(o.component.name == NAME for o in spatial.items(root.occurrences)):
        raise ArgumentError('Layout already exists; inspect before changing it')
    length = design.unitsManager.evaluateExpression(args['link_length'],'cm')/C
    shoulder = design.unitsManager.evaluateExpression(args['shoulder_height'],'cm')/C
    floor = design.unitsManager.evaluateExpression(args['floor_z'],'cm')/C
    # Revalidate the measured tire extent before building dependent geometry.
    tire = spatial.resolve(design, {'occurrence':args['tire_occurrence'],'body':'Body1'})
    if abs(spatial.bounds(tire)['min'][2]/C-floor)>1e-6:
        raise ArgumentError('Tire datum changed; remeasure floor')
    poses = args['poses']
    solved = []
    for p in poses:
        e = solve(length, shoulder, p['x_in'], p['height_in'])
        solved.append(dict(p, elbow_in=list(e)))
    with operation(design,'frc_arm_provisional_layout'):
        occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = occ.component
        comp.name = NAME
        comp.description = '35 inch provisional links; 8 inch tool offset; gripper and load validation deferred. Layout solids are centerline markers, not stock or hardware.'
        lengths=[]
        sketches=[]
        for p in solved:
            sk=comp.sketches.add(comp.xZConstructionPlane)
            sk.name=p['name']+' - wrist and elbow centers'
            pts=[(0,0,(floor+shoulder)*C),(p['elbow_in'][0]*C,0,(floor+p['elbow_in'][1])*C),(p['x_in']*C,0,(floor+p['height_in'])*C)]
            for a,b in zip(pts,pts[1:]):
                line=sk.sketchCurves.sketchLines.addByTwoPoints(ops._to_sketch(sk,a),ops._to_sketch(sk,b))
                line.isFixed=True
                measured=line.startSketchPoint.worldGeometry.distanceTo(line.endSketchPoint.worldGeometry)/C
                lengths.append(measured)
            sk.isVisible=False
            sketches.append(sk.name)
        # Thin solid centerline markers keep the layout visible in clean captures.
        p=solved[0]
        def bar(name,a,b,y,width=.5,depth=.25):
            dx,dz=b[0]-a[0],b[1]-a[1]
            span=math.hypot(dx,dz)
            nx,nz=-dz/span*width/2,dx/span*width/2
            plane_in=comp.constructionPlanes.createInput()
            plane_in.setByOffset(comp.xZConstructionPlane,adsk.core.ValueInput.createByReal(-y*C))
            plane=comp.constructionPlanes.add(plane_in)
            sk=comp.sketches.add(plane);sk.name=name+' profile'
            poly=[(a[0]+nx,a[1]+nz),(b[0]+nx,b[1]+nz),(b[0]-nx,b[1]-nz),(a[0]-nx,a[1]-nz)]
            points=[ops._to_sketch(sk,(x*C,y*C,(floor+z)*C)) for x,z in poly]
            for i in range(4):sk.sketchCurves.sketchLines.addByTwoPoints(points[i],points[(i+1)%4])
            f=ops.extrude(comp,sk.profiles.item(0),depth*C,symmetric=True,name=name)
            f.bodies.item(0).name=name
            sk.isVisible=False;plane.isLightBulbOn=False
        for y in (-8,8):bar('Upper link centerline '+str(y),(0,shoulder),p['elbow_in'],y)
        for y in (-6.5,6.5):bar('Forearm centerline '+str(y),p['elbow_in'],(p['x_in'],p['height_in']),y)
        # Proposed tower feet are over future crossmembers, not mounting joints.
        for y in (-10,10):
            for x in (-5,5):bar('Tower centerline '+str((x,y)),(x,8),(0,shoulder),y)
        if max(abs(v-length) for v in lengths)>1e-6:
            raise ToolFailure('Native layout link lengths disagree with requested length')
        if comp.bRepBodies.count!=8:
            raise ToolFailure('Expected eight layout centerline solids')
        ops.set_material(spatial.items(comp.bRepBodies),app=app)
        for sk in spatial.items(comp.sketches):sk.isVisible=False
    return {'occurrence':occ.fullPathName,'poses':solved,'pose_sketches':sketches,
            'native_segment_count':len(lengths),'maximum_native_length_error_in':max(abs(v-length) for v in lengths),
            'layout_body_count':comp.bRepBodies.count,'floor_z_in':floor,
            'stowed_elbow_height_in':solved[0]['elbow_in'][1],
            'limitations':'Centerline layout only. No fabricated stock, gripper, gears, motor mounts, structural joints, or swept-volume validation.'}

S={'type':'string'}
POSE={'type':'object','properties':{'name':S,'x_in':{'type':'number'},'height_in':{'type':'number'}},'required':['name','x_in','height_in'],'additionalProperties':False}
SPECS={'frc_arm_layout':(True,build,{'link_length':S,'shoulder_height':S,'floor_z':S,'tire_occurrence':S,'poses':{'type':'array','items':POSE,'minItems':1,'maxItems':30}})}

def motor_survey(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.motor_survey(design,app,args)

SELECTOR={'type':'object','properties':{'occurrence':S,'body':S},'required':['occurrence','body'],'additionalProperties':False}
SPECS['frc_arm_motor_survey']=(False,motor_survey,{'bodies':{'type':'array','items':SELECTOR,'minItems':1,'maxItems':4}})

def structure(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.structure(design,app,args)

FACE={'type':'object','properties':{'occurrence':S,'token':S},'required':['occurrence','token'],'additionalProperties':False}
SPECS['frc_arm_structure']=(True,structure,{'support_faces':{'type':'array','items':FACE,'minItems':2,'maxItems':2}})

def fit_check(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.fit_check(design,app,args)
SPECS['frc_arm_fit_check']=(False,fit_check,{})

def repair_y_extents(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.repair_y_extents(design,app,args)
SPECS['frc_arm_repair_y_extents']=(True,repair_y_extents,{})

def support_details(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.support_details(design,app,args)
SPECS['frc_arm_support_details']=(True,support_details,{})

def clearance_repair(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.clearance_repair(design,app,args)
SPECS['frc_arm_clearance_repair']=(True,clearance_repair,{})

def pose_check(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.pose_check(design,app,args)
SPECS['frc_arm_pose_check']=(False,pose_check,{'poses':{'type':'array','items':POSE,'minItems':1,'maxItems':30},'steps':{'type':'integer','minimum':1,'maximum':6}})

def joints(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.joints(design,app,args)
SPECS['frc_arm_joints']=(True,joints,{})

def motor_brackets(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.motor_brackets(design,app,args)
SPECS['frc_arm_motor_brackets']=(True,motor_brackets,{})

def elbow_outboard(design,app,args):
    import importlib
    from recipes import frc_arm_structure
    importlib.reload(frc_arm_structure)
    return frc_arm_structure.elbow_outboard(design,app,args)
SPECS['frc_arm_elbow_outboard']=(True,elbow_outboard,{})
