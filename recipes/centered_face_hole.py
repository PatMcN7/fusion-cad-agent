"""Exact circular through cuts centered on rectangular planar root-body faces."""
import math

def center_and_extent(low, high, body_low, body_high):
    center = [(a+b)/2 for a,b in zip(low, high)]
    reach = math.sqrt(sum(max(abs(body_low[i]-center[i]), abs(body_high[i]-center[i]))**2 for i in range(3)))
    return center, 2*(reach + 0.01)

def run(args):
    import adsk.core
    import adsk.fusion
    import ops
    import pick
    import spatial
    from checkpoints import operation
    from errors import ArgumentError, ToolFailure
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise ArgumentError('An active Fusion design is required')
    face = spatial.resolve(design, {'face_ref': args['face_ref']})
    origin, normal = spatial.plane(face)
    if getattr(face, 'assemblyContext', None) is not None:
        raise ArgumentError('This tool currently supports root-component faces only')
    body = face.body
    component = body.parentComponent
    if component != design.rootComponent or not body.isSolid:
        raise ArgumentError('Select a planar face of a solid root body')
    edges = list(spatial.items(face.edges))
    if len(edges) != 4 or face.loops.count != 1 or any(e.geometry.curveType != adsk.core.Curve3DTypes.Line3DCurveType for e in edges):
        raise ArgumentError('Centering currently requires a rectangular face with four straight edges')
    axis = max(range(3), key=lambda i: abs(normal[i]))
    if abs(normal[axis]) < 1-1e-8:
        raise ArgumentError('Centering currently requires an axis-aligned rectangular face')
    def bounds(entity):
        box = entity.boundingBox
        return ([box.minPoint.x,box.minPoint.y,box.minPoint.z], [box.maxPoint.x,box.maxPoint.y,box.maxPoint.z])
    low, high = bounds(face)
    body_low, body_high = bounds(body)
    tangents = [i for i in range(3) if i != axis]
    area = math.prod(high[i]-low[i] for i in tangents)
    if area <= 0 or abs(face.area-area) > max(1e-7, area*1e-8):
        raise ArgumentError('The selected face is not a full axis-aligned rectangle')
    radius = design.unitsManager.evaluateExpression(args['radius'], 'cm')
    if not math.isfinite(radius) or radius <= 0:
        raise ArgumentError('Radius must be a positive finite length')
    if any(radius >= (high[i]-low[i])/2-1e-7 for i in tangents):
        raise ArgumentError('The circle must fit strictly inside the selected face')
    center, extent = center_and_extent(low, high, body_low, body_high)
    before = body.volume
    name = args.get('name', 'Centered circular through hole')
    with operation(design, name) as (_, suite):
        sketch = ops.sketch_on(component, face, name + ' Profile')
        ops.draw_circles(sketch, [center], radius)
        profiles = pick.profiles(sketch, loops=1, curves=1)
        if len(profiles) != 1:
            raise ToolFailure('Expected exactly one native circular profile')
        feature = ops.extrude(component, profiles.collection(), extent, operation='cut', participants=[body], symmetric=True, name=name)
        sketch.isVisible = False
        after = body.volume
        if after >= before-1e-8:
            raise ToolFailure('The circular cut did not remove material')
        cylinders = []
        for current in spatial.items(body.faces):
            geometry = current.geometry
            if geometry.surfaceType != adsk.core.SurfaceTypes.CylinderSurfaceType:
                continue
            direction = [geometry.axis.x,geometry.axis.y,geometry.axis.z]
            location = [geometry.origin.x,geometry.origin.y,geometry.origin.z]
            alignment = abs(sum(direction[i]*normal[i] for i in range(3)))
            offset = [location[i]-center[i] for i in range(3)]
            projection = sum(offset[i]*normal[i] for i in range(3))
            error = math.sqrt(sum((offset[i]-projection*normal[i])**2 for i in range(3)))
            if abs(geometry.radius-radius) < 1e-6 and alignment > 1-1e-8 and error < 1e-6:
                cylinders.append({'radius_cm':geometry.radius,'axis_error_cm':error,'normal_alignment':alignment})
        if not cylinders:
            raise ToolFailure('No resulting cylindrical wall matches the requested radius and centered normal axis')
        report = spatial.report(body)
    return {'feature_name':feature.name,'source_face_ref':args['face_ref'],'center_cm':center,'radius_cm':radius,'extent_mode':'symmetric distance beyond entire target bounds','symmetric_full_length_cm':extent,'removed_volume_cm3':before-after,'matching_cylindrical_walls':cylinders,'body':report,'limitation':'Through extent is constructed from body bounds; no independent exit-opening check was run'}
