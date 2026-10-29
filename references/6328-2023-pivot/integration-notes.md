# 6328 pivot research and integration worksheet

Status: physical arm structure and four vendor Kraken models are now in the unsaved drivebase assembly. Rail/link hardware is modeled. This is an engineering prototype, not a released fabrication design: transmissions, axle clamps/retention, load validation, and the deferred gripper remain unresolved. See the current implementation record at the end; earlier research and layout sections preserve the dependency history.

Source: [6328 A-Frame Pivot 2023](https://frcdesign.org/mechanism-examples/pivots/6328_2023_pivot/). The [offline article](index.html) preserves the source wording and all mechanism pictures. Image filenames below are abbreviated; manifest.json records their exact names, URLs, dimensions, and hashes.

## What the source establishes

The folding mechanism uses paired braced towers and two moving arm links plus a wrist. A low gearbox with two NEOs drives the first pivots through a common transverse transmission and chains. Stationary axles carry bearings; moving plates and sprockets rotate around them. Opposed clamp plates and Mitee-Bite clamps secure the axles, with snap rings retaining the stack. Aluminum inserts carry bearings; PA12-CF parts locate them inside MAXtube. Bolted joints aid replacement. Cross tubes and gussets brace the links.

| Source detail | Reported value |
|---|---|
| First-joint axle stock | 1.25-inch OD, 0.25-inch wall; bearing seats turned to 30 mm |
| Outer clamp plate / bearing insert plate | 0.25-inch aluminum |
| Clamp movement clearance | Holes oversized by 0.005 inch |
| Axle retention | Snap rings, 98541A134 |
| Second-joint spacer tube | 1.25-inch OD, 0.028-inch wall |
| Second-joint non-bearing axle region | Turned 0.005 inch below 30 mm |
| Second-joint total reduction | 228:1, including chain |
| Output sprocket | REV 64-tooth MAXSpline; center enlarged to 1.375 inch |
| Cross braces | 2 × 1-inch tubing, 1/16-inch wall |

The source reports an approximately 1,800-pound clamp-force calculation; this is not a verified capacity for our design. It does not establish our mount spacing, link lengths, payload, or drivetrain attachment details.

## Image review index

Every technical image was opened in four review sheets, not inferred from alt text alone.

| Image | Detail reviewed |
|---|---|
| 6328_2023_pivot | Overall folded arrangement and tower feet |
| 6328_forces | Extended-arm gravity moment diagram |
| 6328_gearbox | Low motor and transmission arrangement |
| 6328_1 | First-pivot assembly |
| 6328_2 | Opposed axle-contact profiles |
| 6328_3 | Movable clamp plate and eccentric clamp |
| 6328_4 | Section through bearings, spacers, and retaining rings |
| 6328_5 | Joint-to-link attachment |
| 6328_6 | Bearing plate inside printed locator |
| 6328_7 | Encoder-side packaging |
| 6328_8 | Second-pivot assembly |
| 6328_9 | Second-pivot section and axle retention |
| 6328_10 | Gearbox position along link |
| 6328_11 | Side clearance through the folded mechanism |
| 6328_12 | Gearbox plates and output chain arrangement |
| 6328_13 | Cross-bracing and nested link widths |

## Fixed inputs from our conversation

- Existing drivebase: 30 × 30-inch outside envelope; WCP-1702 corner modules.
- Frame: modeled 1 × 2-inch tubes, narrow side up. The 1/8-inch wall was an explicit earlier design assumption.
- Bellypan: 1/16-inch 6061, waterjet-cut triangular lattice, with perimeter rivets.
- Four tube plugs are present; plug attachment screws have not been modeled.
- Preserve unrelated Fusion documents; keep the task designs unsaved.
- Current authorization: proceed with Kraken adaptation; resolve consequential sizing inputs before dependent arm geometry.

These describe the established task, not new measurements taken during this research pass.

## Decisions to make together

Motor requirement update: Kraken X60s at the base/shoulder and Kraken X44s at the outer joints, replacing the reference NEO choices. Accepted working arrangement following “Ok go for it”: two X60s at the base, one X44 at the elbow, and one X44 at the wrist.

Vendor STEP models and dimensioned PDFs are downloaded under `motors/`; `motors/manifest.json` records sources and hashes. Both PDF drawings were visually reviewed. Two X60 and two X44 instances have been imported into the assembly and their pilot axes and mounting-hole axes matched to native plates.

Motor mounting must be adapted: WCP specifies a 2-inch mounting bolt circle for the X60 and 1.375-inch for the X44, with a common 0.750-inch pilot. Gear ratios remain to be sized from payload and motion requirements.
Sources: [X60 physical specifications](https://docs.wcproducts.com/welcome/electronics/kraken-x60/kraken-x60-motor/overview-and-features/physical-specifications), [X44 physical specifications](https://docs.wcproducts.com/welcome/electronics/kraken-x44/kraken-x44-motor/overview-and-features/physical-specifications).

| Requirement | Current status | Why it determines the design |
|---|---|---|
| Task and payload | Open: object, gripper, combined mass, approximate center of mass | Sets joint moments and stability cases |
| Working reach | Score at every 2023 CHARGED UP grid level; wrist poses still depend on gripper geometry and robot approach distance | Sets pivot height and link lengths |
| Direction of operation | Fixed: front and back, confirmed by user | Sets nesting, clearance, and base placement |
| Scope | Open: complete arm and wrist, or selected pivot stages | Sets motors, mass, and interfaces |
| Stowed envelope | Open: desired height and allowed overhang | Sets fold angles and link proportions |
| Dynamics | Open: desired move time, acceleration, and holding behavior | Sets drive requirements beyond static weight |
| Packaging | Open: battery/electronics, existing equipment, maintenance access | Defines occupied space and service routes |
| Fabrication | Waterjet confirmed; turning, precision boring, tapping, and PA12-CF printing unconfirmed | Determines which source details can be reproduced |
| Structural attachment | Open: exact rail faces and permitted additional crossmembers | Determines the load path and mounting pattern |

## Proposed integration relationships — not yet accepted requirements

1. Carry tower reactions into frame rails or dedicated structural crossmembers. Do not assume the perforated bellypan and its existing rivets are sufficient for arm loads.
2. Keep paired pivot bearings coaxial and the two sides synchronized. Derive tower spacing from the selected arm width, bearing stack, and drivebase interfaces.
3. Check the full swept volume, including motors, chains, wiring, and the end effector, against modules, rails, and reserved equipment spaces.
4. Size supports, joints, fasteners, and drive reductions from the agreed load cases. Check robot tipping with the actual mass distribution and wheel contact polygon.
5. Derive mounting holes from selected native faces and measured interfaces when modeling begins. Images explain arrangement; they are not dimensioned mounting drawings.
6. Keep bearing fits, clamp clearances, and retaining-ring grooves distinct from waterjet profile tolerances.

## Dependency order

Purpose and payload → reach and stowed envelope → pivot/link arrangement → tower placement and clearances → structural load cases → supports and joint sizing → exact mounting geometry → CAD and motion/fit verification.

Next input: gripper choice, mass, and grasp offset. The user specified cones/cubes at all 2023 scoring levels from either end of the robot; no default gripper dimensions have been substituted.

## 2023 scoring targets

Source: [FIRST 2023 game manual, section 5.5](https://firstfrc.blob.core.windows.net/frc2023/Manual/HTML/2023FRCGameManual.htm), checked 2026-09-07.

| Target | Height above carpet (in) | Distance behind grid front face (in) |
|---|---:|---:|
| Middle cone node tip / axis | 34 | 22.75 |
| High cone node tip / axis | 46 | 39.75 |
| Middle cube wall top / shelf front | 23.5 | 14.25 |
| High cube wall top / shelf front | 35.5 | 31.625 |

These are field dimensions, not wrist coordinates or required link lengths. Cone insertion needs a clearance pose above the node; account for the held cone and grasp offset. Add robot-to-grid spacing before converting horizontal field offsets into robot coordinates. Include hybrid placement and outer-node barrier clearance in the swept-volume checks.

## Provisional arm layout — gripper deferred

User requested proceeding with the arm and designing the gripper later. This stage is a kinematic layout, not fabrication CAD. Input coordinates are in `arm-layout-inputs.json` and are regeneration inputs rather than associative Fusion parameters.

Explicit working allowances:

- Front = +X; rear = −X. Shoulder centered, 16 inches above the unloaded imported tire tangent floor.
- Two equal 35-inch links. An 8-inch outward wrist-to-grasp offset and 10-pound combined gripper/game-piece budget are reserved; neither is a verified gripper design or load rating.
- Grid front face assumed 20.25 inches from robot center: 15-inch half-frame + 3.25-inch bumper projection + 2-inch approach gap.
- Cone grasp targets use 14 inches above each pole tip for approach and 8 inches above for release. Actual grasp geometry and insertion path must be checked with the eventual gripper/cone model.
- Cube horizontal targets use shelf mid-depth (8.5 inches); wrist heights provisionally 42 inches high, 30 inches middle. Hybrid wrist height is provisionally 8 inches at 8 inches behind grid front.
- Proposed tower centerlines are at Y=±10 inches with feet X=±5 inches, 8 inches above floor. Future crossmembers/support joints have not been designed. Link centerline markers use Y=±8 inches and ±6.5 inches to illustrate nesting; these widths are assumptions.

These yield 15 discrete poses: stow plus seven scoring/clearance poses at each end. Worst shoulder-to-wrist distance is 68.118 inches, below the 70-inch sum of link lengths. Stow wrist is X=8 inches at shoulder height; its elbow is approximately 50.771 inches above floor. This checks reach of assumed wrist points only. It does not establish gripper fit, collision-free transitions, bearing/chain packaging, structural capacity, or competition readiness. Solid bars in the layout represent centerlines, not actual tube stock.

Motor requirement remains two Kraken X60s at the base and one X44 at each outer joint. Actual motor mounting and reductions follow the load and packaging design.

## Current structural implementation — 2026-09-07

The assembly contains two 30-inch 2 × 2 × 0.125-inch crossmembers bearing on the existing rails, two 0.25-inch A-frame plates, paired upper and forearm 2 × 1 × 0.125-inch tubes, bolted endplates, transverse braces, nominal 30-mm dead axles, 6806 bearing envelopes, and an empty wrist carrier. Link tubes are 31 inches long between endplates; joint-center distances remain 35 inches. The shoulder is 16 inches above the imported unloaded tire floor. Material choices and gripper allowances remain provisional.

Upper tube centers remain at Y=±8 inches. Forearm centers were narrowed to Y=±5.5 inches after native Boolean checks found opposing bolt hardware intersected at the earlier width. Existing body labels can retain their original Y coordinate after the native Move features; measured geometry and the Move features establish the current locations.

Eight through-bolts connect the crossmembers to the rails. Their holes continue through the bellypan and their lower washers/nuts are below its underside at root Z=0.0625 inch. A further eight bolts attach the tower foot angles and gearbox platform to the crossmembers. Thirty-two bolts join link tubes and endplates; 56 crush sleeves were added in these stacks. Hardware is simplified: cylindrical head/nut envelopes and unthreaded shanks, not manufacturing models of threaded parts.

The first native fit test exposed reversed Y extrusion directions in the new structure. Forty extrusion features were repaired and their native health checked. Subsequent checks found pan/bolt collisions and folded link bolt collisions, which were corrected with through-pan holes, lower hardware placement and narrower forearm spacing. The corrected folded structure passed 445 native Boolean tests with no unresolved kernel pairs before the latest motor bracket additions. That result is pose-specific and becomes stale when new parts are added.

All four motor pilots and at least two mounting holes per motor aligned to their plates within 1.5e-14 cm in that check. Motor mounting plates do not imply a finished gear reduction. The elbow motor was subsequently moved outboard of the upper link after the rearward path check found it in the forearm sweep.

The initial 60-sample motion study passed front poses but found collisions on direct rear transitions, including the wrist assembly crossing the shoulder axle. A revised route unfolds in front (absolute upper/forearm angles 50°/50°), then bends to 50°/140° before moving to rear scoring targets. This route still requires native checking after the motor bracket changes. It must not be described as a certified continuous swept volume. Bumpers, field obstacles, gripper and wiring are not included in these collision checks.

The motor bracket stage completed successfully (journal operation `b10acfe622c147c1a4e9a1161f731efc`): 18 aluminum and 134 simplified steel hardware bodies were added. Both X44 bracket standoffs measure 0.25 inch between their actual tube and plate faces. Eight motor screws use 0.5-inch under-head length with 0.21-inch engagement. Fusion shut down after that stage; the live unsaved document state must be recovered and inspected before further edits. No final bracket collision check or completed-arm capture was obtained after that stage.

Pending: motor bracket verification; native revolute/rigid joints; revised motion samples; synchronization of new arm mounting holes into the separate frame/pan document; precise axle clamp and axial-retention design; gear cases, reductions and chains; tube crossbrace connections; wrist crossplate; static/dynamic structural and tipping checks. Waterjet profiles do not establish finished bearing fits or turned axle tolerances.
