"""Reference-based geometry tools. Native calls run on Fusion's main thread.

Selectors always resolve in the root assembly frame. No fuzzy face selection and
no hidden first-body fallback. Placement is limited to top-level occurrences.
"""
import math
import adsk.core
import adsk.fusion
from errors import ArgumentError, ToolFailure
from checkpoints import operation
from spatial_contracts import SPECS


def xyz(p):
    return [p.x, p.y, p.z]


def dot(a,b):
    return sum(x*y for x,y in zip(a,b))


def sub(a,b):
    return [x-y for x,y in zip(a,b)]


def length(v):
    return math.sqrt(dot(v,v))


def unit(v):
    n = length(v)
    if n < 1e-12:
        raise ArgumentError("Direction vector must be nonzero")
    return [x/n for x in v]


def items(collection):
    return [collection.item(i) for i in range(collection.count)]


def occurrence(design, name):
    found = [o for o in items(design.rootComponent.allOccurrences) if o.fullPathName == name]
    if len(found) != 1:
        raise ArgumentError("Use an exact occurrence full path from inspect_geometry")
    return found[0]


def resolve(design, selector):
    if selector.get("face_ref"):
        if set(selector) != {"face_ref"}:
            raise ArgumentError("Use face_ref alone; do not combine it with guessed body or occurrence fields")
        import face_selection
        return face_selection.resolve(design, selector["face_ref"])
    if not selector or (selector.get('body') and selector.get('token')):
        raise ArgumentError("Select an occurrence, an exact body, or a returned token; do not combine body and token")
    occ = occurrence(design, selector['occurrence']) if selector.get('occurrence') else None
    component = occ.component if occ else design.rootComponent
    if selector.get('token'):
        found = design.findEntityByToken(selector['token'])
        if len(found) != 1:
            raise ArgumentError("Entity token is stale or ambiguous; inspect geometry again")
        entity = found[0]
        entity = getattr(entity, 'nativeObject', None) or entity
        parent = getattr(entity, 'parentComponent', None)
        if parent is None and getattr(entity, 'body', None):
            parent = entity.body.parentComponent
        if parent != component:
            raise ArgumentError("Token does not belong to the selected component; include its returned occurrence context")
    elif selector.get('body'):
        found = [b for b in items(component.bRepBodies) if b.name == selector['body']]
        if len(found) != 1:
            raise ArgumentError("Body name is missing or ambiguous; inspect geometry and use a returned token")
        entity = found[0]
    elif occ:
        return occ
    else:
        raise ArgumentError("Select an occurrence, body or token")
    if occ:
        entity = entity.createForAssemblyContext(occ)
        if entity is None:
            raise ArgumentError("Could not resolve this entity in the root assembly context")
    return entity


def selector_for(entity, occ=None):
    native = getattr(entity, 'nativeObject', None) or entity
    result = {'token': native.entityToken}
    context = occ or getattr(entity, 'assemblyContext', None)
    if context:
        result['occurrence'] = context.fullPathName
    return result


def bounds(entity):
    box = getattr(entity, 'preciseBoundingBox', None) or entity.boundingBox
    return {'min': xyz(box.minPoint), 'max': xyz(box.maxPoint)}


def plane(entity):
    geometry = getattr(entity, 'geometry', None)
    if geometry is None or geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
        raise ArgumentError("This operation requires selected planar faces")
    normal = unit(xyz(geometry.normal))
    if getattr(entity, 'isParamReversed', False):
        normal = [-v for v in normal]
    return xyz(geometry.origin), normal


def plane_metrics(a, b):
    pa, na = plane(a)
    pb, nb = plane(b)
    cosine = max(-1.0, min(1.0, dot(na,nb)))
    angle = math.degrees(math.acos(abs(cosine)))
    return {'signed_separation_cm': dot(sub(pb,pa),na), 'normal_angle_degrees': angle,
            'normals_same_direction': cosine >= 0, 'normal_a':na, 'normal_b':nb}


def report(entity, occ=None):
    result = {'selector':selector_for(entity,occ), 'type':entity.objectType,
              'bounds_cm':bounds(entity)}
    if hasattr(entity,'name'):
        result['name'] = entity.name
    if hasattr(entity,'pointOnFace'):
        result['point_on_face_cm'] = xyz(entity.pointOnFace)
        g=entity.geometry
        if g.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
            result['plane_origin_cm'], result['normal'] = plane(entity)
        elif g.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
            result.update(axis_origin_cm=xyz(g.origin),axis=xyz(g.axis),radius_cm=g.radius)
    return result


def inspect_geometry(design, app, arguments):
    offset, limit = arguments.get('offset',0),arguments.get('limit',12)
    selected = arguments.get('entity')
    if not selected:
        rows = [{'selector':{'occurrence':o.fullPathName},'name':o.fullPathName,
                 'body_count':o.component.bRepBodies.count,'grounded':o.isGrounded,'ground_to_parent':getattr(o,'isGroundToParent',None),'transform':o.transform2.asArray()} for o in items(design.rootComponent.allOccurrences)]
        rows += [{'selector':selector_for(b),'name':b.name,'face_count':b.faces.count} for b in items(design.rootComponent.bRepBodies)]
        result = {'entities': rows[offset:offset+limit], 'total': len(rows)}
    else:
        entity = resolve(design,selected)
        if entity.objectType == adsk.fusion.Occurrence.classType():
            bodies=items(entity.component.bRepBodies)
            children=items(entity.childOccurrences)
            # childOccurrences carries this exact assembly instance's context.
            rows=[{'kind':'occurrence','selector':{'occurrence':child.fullPathName},
                   'name':child.name,'body_count':child.component.bRepBodies.count,
                   'child_count':child.childOccurrences.count} for child in children]
            rows += [dict(report(body.createForAssemblyContext(entity),entity),kind='body',
                          face_count=body.faces.count) for body in bodies]
            page=rows[offset:offset+limit]
            result={'entity':{'selector':selected,'bounds_cm':bounds(entity),'grounded':entity.isGrounded,'ground_to_parent':getattr(entity,'isGroundToParent',None),'transform':entity.transform2.asArray()},
                    'total':len(rows),'direct_body_count':len(bodies),'child_count':len(children),
                    'entities':page,'children':[row for row in page if row['kind']=='occurrence'],
                    'bodies':[row for row in page if row['kind']=='body'],
                    'next_step':'Inspect a returned child selector to descend, then a body selector to get exact faces. Bounds are not mounting surfaces.'}
        elif hasattr(entity,'faces'):
            result={'entity':report(entity),'total':entity.faces.count,
                    'faces':[dict(report(entity.faces.item(i)),index=i) for i in range(offset,min(offset+limit,entity.faces.count))]}
        else:
            result={'entity':report(entity),'total':0}
    result.update(frame='root',units='cm',offset=offset,next_offset=offset+limit if offset+limit<result['total'] else None)
    return result


def measure_geometry(design, app, arguments):
    a,b = resolve(design,arguments['a']),resolve(design,arguments['b'])
    result={'a':arguments['a'],'b':arguments['b'],'frame':'root','units':'cm','mode':arguments.get('mode','minimum')}
    if result['mode']=='plane_separation':
        metrics=plane_metrics(a,b)
        if metrics['normal_angle_degrees'] > 1e-5:
            raise ArgumentError("Planes are not parallel; use minimum distance or verify parallelism")
        result.update(metrics)
        result['distance_cm']=abs(metrics['signed_separation_cm'])
    else:
        measurement=app.measureManager.measureMinimumDistance(a,b)
        if measurement is None:
            raise ToolFailure("Fusion could not measure these selected entities")
        result.update(distance_cm=measurement.value,point_a_cm=xyz(measurement.positionOne),point_b_cm=xyz(measurement.positionTwo))
    return result


def expression(design, value, field, units='cm'):
    if not isinstance(value,str) or not design.unitsManager.isValidExpression(value,units):
        raise ArgumentError(f'{field} must be a valid {units} expression')
    result=design.unitsManager.evaluateExpression(value,units)
    if not math.isfinite(result):
        raise ArgumentError(f'{field} must be finite')
    return result


def verify_relationship(design, app, arguments):
    relation=arguments['relation']
    if relation=='parallel':
        metrics=plane_metrics(resolve(design,arguments['a']),resolve(design,arguments['b']))
        actual=math.radians(metrics['normal_angle_degrees'])
        tolerance=expression(design,arguments.get('tolerance','0.01 deg'),'tolerance','rad')
        expected=0
        result={**metrics,'units':'rad','frame':'root'}
    else:
        mode='plane_separation' if relation in {'coplanar','plane_distance'} else 'minimum'
        result=measure_geometry(design,app,{**arguments,'mode':mode})
        actual=result['distance_cm']
        if relation!='coplanar' and 'expected' not in arguments:
            raise ArgumentError('expected distance is required for this relationship')
        expected=0 if relation=='coplanar' else expression(design,arguments['expected'],'expected')
        tolerance=expression(design,arguments.get('tolerance','0.01 mm'),'tolerance')
    if tolerance<0 or expected<0:
        raise ArgumentError('Expected distance and tolerance must be nonnegative')
    passed=actual+tolerance>=expected if relation=='minimum_clearance' else abs(actual-expected)<=tolerance
    return {**result,'a':arguments['a'],'b':arguments['b'],'relation':relation,'actual':actual,'expected':expected,'tolerance':tolerance,'passed':passed}


def anchor(entity, kind):
    if kind=='point':
        if not hasattr(entity,'pointOnFace'):
            raise ArgumentError('point anchor requires a face')
        return xyz(entity.pointOnFace)
    box=bounds(entity)
    return [(a+b)/2 for a,b in zip(box['min'],box['max'])] if kind=='center' else box[kind]


def placement_matrix(old, source, target, axis, degrees):
    """World rotation R and translation t such that R*source+t == target."""
    x,y,z=unit(axis); c=math.cos(math.radians(degrees)); s=math.sin(math.radians(degrees)); d=1-c
    r=[[c+x*x*d,x*y*d-z*s,x*z*d+y*s], [y*x*d+z*s,c+y*y*d,y*z*d-x*s], [z*x*d-y*s,z*y*d+x*s,c+z*z*d]]
    shift=[target[i]-dot(r[i],source) for i in range(3)]
    out=[0.0]*16
    for i in range(3):
        for j in range(4):
            out[i*4+j]=sum(r[i][k]*old[k*4+j] for k in range(3))+(shift[i] if j==3 else 0)
    out[15]=1.0
    return out


def place_component(design, app, arguments):
    moving=occurrence(design,arguments['occurrence'])
    if moving.assemblyContext is not None:
        raise ArgumentError('Placement currently supports top-level occurrences only')
    anchor_entity=resolve(design,arguments['anchor'])
    anchor_occurrence=(anchor_entity if anchor_entity.objectType==adsk.fusion.Occurrence.classType() else getattr(anchor_entity,'assemblyContext',None))
    if anchor_occurrence!=moving and not (
        anchor_occurrence is not None and
        anchor_occurrence.fullPathName.startswith(moving.fullPathName+'+')
    ):
        raise ArgumentError('Anchor must belong to the moving occurrence')
    if ('position' in arguments)==('target' in arguments):
        raise ArgumentError('Supply exactly one of position or target')
    copying=arguments.get('copy',False)
    grounded,ground_to_parent=moving.isGrounded,getattr(moving,'isGroundToParent',False)
    if not copying and (moving.joints.count or moving.asBuiltJoints.count or getattr(getattr(moving,'rigidGroups',None),'count',0)):
        raise ArgumentError('Occurrence is joined or in a rigid group; edit the constraint instead of forcing placement')
    if not copying and (grounded or ground_to_parent) and not arguments.get('release_grounding',False):
        raise ArgumentError('Occurrence is grounded; pass release_grounding=true explicitly to place it independently')
    source=anchor(anchor_entity,arguments.get('anchor_point','center'))
    target=([expression(design,v,'position') for v in arguments['position']] if 'position' in arguments
            else anchor(resolve(design,arguments['target']),arguments.get('target_point','center')))
    offset=[expression(design,v,'offset') for v in arguments.get('offset',['0 cm']*3)]
    target=[a+b for a,b in zip(target,offset)]
    before=moving.transform2.copy()
    matrix=adsk.core.Matrix3D.create()
    values=placement_matrix(before.asArray(),source,target,arguments.get('rotation_axis',[0,0,1]),arguments.get('rotation_degrees',0))
    if not matrix.setWithArray(values):
        raise ArgumentError('Invalid placement transform')
    placed=None
    try:
        with operation(design,'place_component'):
            if copying:
                placed=design.rootComponent.occurrences.addExistingComponent(moving.component,matrix)
                placed.isGrounded=False
                if hasattr(placed,'isGroundToParent'):placed.isGroundToParent=False
                placed.transform2=matrix
            else:
                placed=moving
                if grounded:placed.isGrounded=False
                if ground_to_parent:placed.isGroundToParent=False
                placed.transform2=matrix
            if design.designType==adsk.fusion.DesignTypes.ParametricDesignType and design.snapshots.hasPendingSnapshot:
                design.snapshots.add()
            # Check the actual transform, not the requested matrix. Geometry anchors
            # such as bounding-box corners can change identity after rotation.
            local=adsk.core.Point3D.create(*source)
            inverse=before.copy(); inverse.invert(); local.transformBy(inverse)
            local.transformBy(placed.transform2)
            actual=xyz(local)
            error=length(sub(actual,target))
            if error>1e-6:
                if not copying:
                    placed.transform2=before
                raise ToolFailure(f'Placement anchor missed target by {error} cm')
    except Exception as exc:
        recovery=getattr(exc,'recovery',{})
        try:
            if not copying:
                moving.transform2=before
                moving.isGrounded=grounded
                if hasattr(moving,'isGroundToParent'):moving.isGroundToParent=ground_to_parent
                recovery['placement_transform_restored']=all(abs(a-b)<1e-7 for a,b in zip(moving.transform2.asArray(),before.asArray()))
                recovery['grounding_restored']=(moving.isGrounded==grounded and getattr(moving,'isGroundToParent',False)==ground_to_parent)
            elif placed is not None and placed.isValid:
                recovery['copied_occurrence_removed']=bool(placed.deleteMe())
        except Exception as cleanup:
            recovery['placement_cleanup_error']=str(cleanup)
        exc.recovery=recovery
        raise
    return {'occurrence':placed.fullPathName,'copied':copying,'frame':'root','units':'cm',
            'anchor_before_cm':source,'target_cm':target,'actual_anchor_cm':actual,
            'anchor_error_cm':error,'transform':placed.transform2.asArray(),'bounds_cm':bounds(placed),
            'associative':False,'passed':True,'released_grounding':bool(not copying and (grounded or ground_to_parent))}


def extrude_between_faces(design, app, arguments):
    start,end=resolve(design,arguments['start']),resolve(design,arguments['end'])
    metrics=plane_metrics(start,end)
    distance=metrics['signed_separation_cm']
    if metrics['normal_angle_degrees']>1e-5 or abs(distance)<1e-6:
        raise ArgumentError('Select parallel planar faces on distinct planes')
    if design.timeline.markerPosition!=design.timeline.count:
        raise ArgumentError('Move the timeline to its end before creating dependent geometry')
    existing_bodies=items(design.rootComponent.bRepBodies)
    with operation(design,'extrude_between_faces'):
        features=design.rootComponent.features.extrudeFeatures
        inp=features.createInput(start,adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        inp.isSolid=True
        extent=adsk.fusion.ToEntityExtentDefinition.create(end,False)
        # Extrusion direction follows the underlying plane, not face orientation.
        reversed_face=getattr(start,'isParamReversed',False)
        positive=(distance>0) != reversed_face
        direction=(adsk.fusion.ExtentDirections.PositiveExtentDirection if positive else adsk.fusion.ExtentDirections.NegativeExtentDirection)
        if not inp.setOneSideExtent(extent,direction):
            raise ToolFailure('Fusion rejected the referenced extent')
        feature=features.add(inp)
        feature.name=arguments.get('name','Referenced Extrusion')
        # Feature.bodies includes participating source bodies for a face
        # profile. Identify the newly created root body, not collection count.
        created=[b for b in items(design.rootComponent.bRepBodies) if b not in existing_bodies]
        if len(created)!=1 or not created[0].isSolid:
            raise ToolFailure(f'Expected one new solid body; new={len(created)}; health={feature.healthState}; message={feature.errorOrWarningMessage}')
        body=created[0]
        body.name=feature.name
    return {'feature_token':feature.entityToken,'body':report(body),'start':arguments['start'],'end':arguments['end'],
            'extent_type':'ToEntityExtentDefinition','plane_separation_cm':abs(distance),'frame':'root','units':'cm',
            'associative':True,'limits':'Native face dependencies, not a general clearance or manufacturability check'}
