"""Task-specific WCP-1702 frame construction and native assembly checks.

No document creation, switching, saving, or preexisting body edits.
"""
import math
import adsk.core
import adsk.fusion
from fusion_cad import ops
from fusion_cad import pick
from fusion_cad import spatial
from fusion_cad.checkpoints import operation
from fusion_cad.errors import ArgumentError, ToolFailure

C = 2.54

def _cm(design, value):
    return design.unitsManager.evaluateExpression(value, 'cm')

def _module_bodies(design, names):
    for occ in spatial.items(design.rootComponent.allOccurrences):
        if any(occ.fullPathName == n or occ.fullPathName.startswith(n + '+') for n in names):
            for native in spatial.items(occ.component.bRepBodies):
                if native.isSolid:
                    yield occ.fullPathName, native.createForAssemblyContext(occ)

def survey(design, app, args):
    face = spatial.resolve(design, {'face_ref': args['support_face']})
    origin, normal = spatial.plane(face)
    if normal[2] < .999999:
        raise ArgumentError('Selected support must face upward in root coordinates')
    thickness = _cm(design, args['pan_thickness'])
    boxes = []
    for path, body in _module_bodies(design, args['modules']):
        box = spatial.bounds(body)
        if box['min'][2] <= origin[2] + 1e-7 and box['max'][2] >= origin[2] - thickness - 1e-7:
            boxes.append({'occurrence': path, 'body': body.name, 'bounds_cm': box})
    return {'support': spatial.report(face), 'pan_slice_cm': [origin[2]-thickness, origin[2]],
            'occupied_body_bounds': boxes,
            'scope': 'Conservative projected body boxes intersecting the bellypan slab; not mounting datums'}

def build_frame(design, app, args):
    root = design.rootComponent
    if root.bRepBodies.count:
        raise ArgumentError('Frame recipe requires no existing root bodies')
    size, width, height, wall, base, diameter = [_cm(design, args[k]) for k in
        ('frame_size', 'tube_width', 'tube_height', 'wall', 'base', 'hole_diameter')]
    if not 0 < wall < min(width, height)/2:
        raise ArgumentError('Invalid hollow tube wall')
    half = size/2
    length = size-width
    pitch = _cm(design, args['hole_pitch'])
    if abs(length/pitch-round(length/pitch)) > 1e-7:
        raise ArgumentError('Tube length must be an integral number of hole pitches')
    xs = [-half+i*pitch for i in range(1, round(length/pitch))]
    with operation(design, 'frc_native_frame'):
        pinput = root.constructionPlanes.createInput()
        pinput.setByOffset(root.yZConstructionPlane, adsk.core.ValueInput.createByReal(-half))
        plane = root.constructionPlanes.add(pinput)
        plane.name = 'Rail start datum'
        sketch = ops.sketch_on(root, plane, '29 inch rail hollow section')
        ops.draw_rect(sketch, (-half,half-width,base), (-half,half,base+height))
        ops.draw_rect(sketch, (-half,half-width+wall,base+wall), (-half,half-wall,base+height-wall))
        feature = ops.extrude(root, pick.profiles(sketch, loops=2).one(), length, name='Hollow rail')
        body = feature.bodies.item(0)
        body.name = 'Frame Rail North'
        top = pick.faces(body).planar().facing('+z').extreme('+z').one()
        ops.cut_holes(root, body, top, [(x,half-width/2,base+height) for x in xs],
                      diameter/2, -height, name='WCP narrow face half-inch pitch')
        side = pick.faces(body).planar().facing('+y').extreme('+y').one()
        side_x = [-half+width/2, half-width-width/2]
        side_z = [base+height/4, base+height/2, base+3*height/4]
        ops.cut_holes(root, body, side, [(x,half,z) for x in side_x for z in side_z],
                      diameter/2, -width, name='Tube plug side bolt provisions')
        ops.circular_pattern(root, [body], root.zConstructionAxis, 4, name='Four butt-jointed rails')
        members = spatial.items(root.bRepBodies)
        if len(members) != 4 or any(not b.isSolid for b in members):
            raise ToolFailure('Frame must contain four hollow solid bodies')
        for member in members:
            b = spatial.bounds(member)
            cx = (b['min'][0]+b['max'][0])/2
            cy = (b['min'][1]+b['max'][1])/2
            side_name = ('North' if cy>0 else 'South') if abs(cy)>abs(cx) else ('East' if cx>0 else 'West')
            member.name = 'Frame Rail '+side_name
        material = ops.set_material(members, app=app)
        for sk in spatial.items(root.sketches):
            sk.isVisible = False
        plane.isLightBulbOn = False
    return {'rail_length_in': length/C, 'holes_per_narrow_face':len(xs),
            'side_holes_per_wide_face':6, 'material':material,
            'bodies':[spatial.report(b) for b in members]}

def drill_pan(design, app, args):
    root = design.rootComponent
    body = spatial.resolve(design, {'body':args['body']})
    face = pick.faces(body).planar().facing('+z').extreme('+z').one()
    z = spatial.plane(face)[0][2]
    centers = [(p[0]*C,p[1]*C,z) for p in args['centers_in']]
    with operation(design, 'bellypan_mounting_holes'):
        ops.cut_holes(root, body, face, centers, _cm(design,args['diameter'])/2,
                      -_cm(design,args['thickness']), name='Bellypan rail mounting holes')
    return {'hole_count':len(centers), 'body':spatial.report(body)}

def validate(design, app, args):
    root = design.rootComponent
    made = [spatial.resolve(design, {'body':n}) for n in args['bodies']]
    others = list(_module_bodies(design,args.get('modules',[])))
    manager = adsk.fusion.TemporaryBRepManager.get()
    collisions = []
    tested = 0
    pairs = [(a, path, b) for a in made for path,b in others]
    pairs += [(a,b.name,b) for i,a in enumerate(made) for b in made[i+1:]]
    for a,path,b in pairs:
        ba,bb = spatial.bounds(a),spatial.bounds(b)
        if any(ba['max'][i] <= bb['min'][i]+1e-7 or bb['max'][i] <= ba['min'][i]+1e-7 for i in range(3)):
            continue
        one,two = manager.copy(a),manager.copy(b)
        # A copied proxy must retain its root-coordinate placement.
        cb = spatial.bounds(two)
        if any(abs(cb[k][i]-bb[k][i])>1e-5 for k in ('min','max') for i in range(3)):
            raise ToolFailure('Temporary proxy copy did not retain root coordinates')
        if not manager.booleanOperation(one,two,adsk.fusion.BooleanTypes.IntersectionBooleanType):
            raise ToolFailure('Temporary intersection failed')
        tested += 1
        if one.volume > 1e-6:
            collisions.append({'body':a.name,'other':path,'volume_cm3':one.volume})
    def axes(body, radius):
        found = []
        for f in spatial.items(body.faces):
            g = f.geometry
            if g.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType and abs(g.radius-radius)<1e-6 and abs(g.axis.z)>.999999:
                found.append((g.origin.x,g.origin.y))
        return found
    rails = [b for b in made if b.name.startswith('Frame Rail ')]
    rail_axes = [p for b in rails for p in axes(b,.196*C/2)]
    errors = []
    module_holes = 0
    for path,b in others:
        if path.endswith('+WCP-1704 Rev1:1'):
            # These ten faces were returned by native inspection of the unchanged
            # WCP-1704 import. Other equal-diameter holes mount internal mechanisms.
            mounting = [b.faces.item(i).geometry for i in range(34,44)]
            if any(g.surfaceType != adsk.core.SurfaceTypes.CylinderSurfaceType or abs(g.radius-.20193)>1e-6 for g in mounting):
                raise ToolFailure('Inspected flange-hole identities changed; inspect again')
            for x,y in [(g.origin.x,g.origin.y) for g in mounting]:
                module_holes += 1
                errors.append(min(math.hypot(x-a,y-c) for a,c in rail_axes))
    pan_holes = 0
    for b in made:
        if b.name == 'Bellypan 6061 1-16':
            for x,y in axes(b,.196*C/2):
                pan_holes += 1
                errors.append(min(math.hypot(x-a,y-c) for a,c in rail_axes))
    max_error = max(errors,default=0)
    return {'tested_solid_intersections':tested,'collisions':collisions,
            'module_mounting_holes_checked':module_holes,'pan_mounting_holes_checked':pan_holes,
            'maximum_hole_axis_error_cm':max_error,'passed':not collisions and max_error<1e-6,
            'body_reports':[dict(spatial.report(b),volume_in3=b.volume/C**3,material=b.material.name) for b in made]}

STR = {'type':'string'}
def build_rivets(design, app, args):
    import importlib
    from recipes import frc_hardware
    importlib.reload(frc_hardware)
    return frc_hardware.rivets(design, app, args)

def validate_hardware(design, app, args):
    import importlib
    from recipes import frc_hardware
    importlib.reload(frc_hardware)
    return frc_hardware.validate(design, app, args)

NAMES = {'type':'array','items':STR,'minItems':1}
POINTS = {'type':'array','items':{'type':'array','items':{'type':'number'},'minItems':2,'maxItems':2},'minItems':1}
SPECS = {
 'frc_drivebase_rivets':(True,build_rivets,dict(pan=STR,rails=NAMES,**{k:STR for k in ('hole_diameter','body_diameter','head_diameter','head_height','tail_diameter','tail_height')})),
 'frc_drivebase_hardware_check':(False,validate_hardware,{'hardware':NAMES,'pan':STR,'rails':NAMES}),
 'frc_drivebase_survey':(False,survey,{'support_face':STR,'pan_thickness':STR,'modules':NAMES}),
 'frc_drivebase_frame':(True,build_frame,{k:STR for k in ('frame_size','tube_width','tube_height','wall','base','hole_diameter','hole_pitch')}),
 'frc_drivebase_drill_pan':(True,drill_pan,{'body':STR,'centers_in':POINTS,'diameter':STR,'thickness':STR}),
 'frc_drivebase_validate':(False,validate,{'bodies':NAMES,'modules':{'type':'array','items':STR}}),
}
