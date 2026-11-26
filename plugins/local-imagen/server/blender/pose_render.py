"""local-imagen pose renderer. Runs inside Blender (background mode) and needs only bpy:

    blender -b --factory-startup --python pose_render.py -- <jobs.json>

jobs.json = {"jobs": [job, ...]}; each job renders one pose guide:
    {"out": "<folder>", "name": "<file stem>", "width": 896, "height": 1152, "samples": 24,
     "outputs": ["mannequin", "depth"], "color_sides": false,
     "figure": {"heads": 7, "build": 1.0, "shoulders": 1.0, "hips": 1.0},
     "pose": {...see POSE KEYS below...},
     "camera": {"azimuth": 0, "elevation": 5, "distance": 4.0, "ortho": false, "margin": 0.06}}
    (distance is in figure heights: smaller = stronger perspective; the framing zooms to fit the figure)

Writes <name>.png (RGBA: grey mannequin + its ground shadow on a transparent background; the MCP server puts it
on white), <name>-depth.png (near = white, far = dark, background black) and <name>.json (2D joint positions in
pixels, from the top-left corner).

The figure is a jointed artist's mannequin built from simple shapes. All angles are in degrees.
Axes: the figure faces -Y; +X is the figure's own left; +Z is up.
POSE KEYS
  root:      turn (+ = turn toward the figure's left), pitch (+ = lean/flip forward), roll (+ = lean toward the
             figure's left), lift (height above the ground, in heads), ground (true = lowest point on the floor)
  spine:     bend (+ forward, - arch back), side (+ toward the figure's left), twist (+ chest turns to its left)
  head:      nod (+ down), tilt (+ toward the left shoulder), turn (+ toward the figure's left)
  left_arm / right_arm:
             aim (direction of the upper arm relative to the chest: {"forward": f, "up": u, "out": o}, negative =
             back / down / across; or a string like "forward", "down-out", "up-forward"), twist (+ = turns
             outward), elbow (bend, 0 = straight), wrist (+ = bend toward the palm), wrist_tilt (+ = toward the
             thumb), hand ("relaxed", "open", "fist")
  left_leg / right_leg:
             aim (thigh direction relative to the hips, same form), twist (+ = knee and toes turn outward),
             knee (bend, 0 = straight), ankle (+ = toes point down)
  props:     [{"type": "sword", "hand": "right", "grip": "normal" | "reverse", "length": 3.2 (blade, in heads),
              "two_handed": false (true = the other hand also holds the grip; its arm is placed automatically)},
             {"type": "scabbard", "side": "left", "angle": 35 (tilt back from vertical), "length": 3.4 (heads),
              "sheathed": true (a sword's hilt sticks out of it)}]
  DETAIL (job "detail": ["hands", "feet", "pelvis"]): finer parts for precision work.
    hands: jointed fingers (3 segments each) and thumb. Per arm: "hand" = "fist" | "grip" | "relaxed" | "open" |
           "point", or "fingers": {"index": [mcp, pip, dip], "middle": [...], "ring": [...], "pinky": [...],
           "thumb": "wrap" | "open" | "relaxed"} (flexion angles in degrees). A hand holding a sword grips it.
    feet:  a hinged foot (heel, arch, toe box on the ball of the foot, ankle bones); leg key "toes" (+ = bent up).
    pelvis: a belt line, a front marker and hip bones, so the pelvis rotation reads clearly.
    eyes:  eyeballs with pupils; head key "gaze": {"up": deg, "left": deg} aims them on their own (e.g. the head
           nods down while the eyes look up).
  Extra outputs: "parts" = <name>-parts.png, every body part in its own flat colour (the JSON lists them), for
  attention masks.
"""
import json
import math
import sys
import time

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Quaternion, Vector

X, Y, Z = Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))
HEIGHT = 1.7  # metres; only matters for light falloff


def q(axis, deg):
    return Quaternion(axis, math.radians(deg))


def num(d, key, default=0.0):
    v = d.get(key, default) if isinstance(d, dict) else default
    return float(v) if v is not None else default


# ------------------------------------------------------------------ mesh helpers
def add_ellipsoid(bm, center, radii, rot=None, segs=(28, 14)):
    m = Matrix.Translation(Vector(center))
    if rot is not None:
        m = m @ rot.to_matrix().to_4x4()
    m = m @ Matrix.Diagonal((radii[0], radii[1], radii[2], 1.0))
    bmesh.ops.create_uvsphere(bm, u_segments=segs[0], v_segments=segs[1], radius=1.0, matrix=m)


def add_cone(bm, p0, p1, r0, r1, segs=28):
    p0, p1 = Vector(p0), Vector(p1)
    d = p1 - p0
    rot = Z.rotation_difference(d.normalized()) if d.length > 1e-9 else Quaternion()
    m = Matrix.Translation((p0 + p1) / 2) @ rot.to_matrix().to_4x4()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segs, radius1=r0, radius2=r1,
                          depth=d.length, matrix=m)


def add_capsule(bm, p0, p1, r0, r1):
    add_ellipsoid(bm, p0, (r0, r0, r0))
    add_ellipsoid(bm, p1, (r1, r1, r1))
    add_cone(bm, p0, p1, r0, r1)


def make_obj(name, build_fn, parent, mat):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    build_fn(bm)
    for f in bm.faces:
        f.smooth = True
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.parent = parent
    me.materials.append(mat)
    return ob


def make_joint(name, parent, local_pos):
    e = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(e)
    e.empty_display_size = 0.02
    e.parent = parent
    e.location = Vector(local_pos)
    e.rotation_mode = "QUATERNION"
    return e


def material(name, color, rough=0.55, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    return m


# ------------------------------------------------------------------ the mannequin
def build_figure(fig, pose, color_sides):
    heads = max(2.5, min(10.0, num(fig, "heads", 7)))
    bw = max(0.6, min(1.8, num(fig, "build", 1.0)))
    sw = max(0.6, min(1.6, num(fig, "shoulders", 1.0))) * bw
    hw = max(0.6, min(1.6, num(fig, "hips", 1.0))) * bw
    H = HEIGHT / heads          # head height
    B = HEIGHT - H              # chin height
    u = B / 6.0                 # body unit (= one head on a 7-head figure)

    grey = (0.52, 0.52, 0.53)
    mats = {"body": material("body", grey), "sword": material("sword", (0.28, 0.29, 0.31), 0.35, 0.6),
            "detail": material("detail", (0.40, 0.40, 0.43)), "eye": material("eye", (0.88, 0.88, 0.90)),
            "pupil": material("pupil", (0.04, 0.04, 0.05))}
    if color_sides:  # optional: tell left from right at a glance (left = cool, right = warm)
        mats["left"], mats["right"] = material("left", (0.50, 0.58, 0.70)), material("right", (0.72, 0.58, 0.50))
    else:
        mats["left"] = mats["right"] = mats["body"]

    J = {}
    J["root"] = make_joint("root", None, (0, 0, 3.75 * u))
    J["spine"] = make_joint("spine", J["root"], (0, 0, 0.5 * u))
    J["chest"] = make_joint("chest", J["spine"], (0, 0, 0.6 * u))
    J["neck"] = make_joint("neck", J["chest"], (0, 0.04 * u, 0.87 * u))
    J["head"] = make_joint("head", J["neck"], (0, 0.01 * u, B - 5.72 * u - 0.04 * H))
    body = mats["body"]

    def pelvis(bm):
        add_ellipsoid(bm, (0, 0.02 * u, -0.12 * u), (0.58 * u * hw, 0.40 * u * bw, 0.40 * u))
        add_ellipsoid(bm, (0, 0.04 * u, -0.36 * u), (0.42 * u * hw, 0.34 * u * bw, 0.22 * u))   # crotch
    make_obj("pelvis", pelvis, J["root"], body)
    if "pelvis" in DETAIL:   # belt line, front marker, hip bones: the pelvis rotation reads at a glance
        def pelvis_marks(bm):
            add_ellipsoid(bm, (0, 0.02 * u, 0.08 * u), (0.62 * u * hw, 0.43 * u * bw, 0.055 * u))      # belt line
            add_ellipsoid(bm, (0, -0.44 * u * bw, 0.06 * u), (0.09 * u, 0.05 * u, 0.08 * u), segs=(16, 8))
            for sx in (1, -1):
                add_ellipsoid(bm, (sx * 0.50 * u * hw, -0.16 * u * bw, 0.0), (0.09 * u, 0.09 * u, 0.09 * u),
                              segs=(16, 8))                                                            # hip bones
        make_obj("pelvis_marks", pelvis_marks, J["root"], mats.get("detail", body))
    make_obj("abdomen", lambda bm: add_ellipsoid(bm, (0, 0.01 * u, 0.14 * u),
                                                 (0.44 * u * (sw + hw) / 2, 0.32 * u * bw, 0.42 * u)),
             J["spine"], body)

    def chest(bm):
        add_ellipsoid(bm, (0, 0.0, 0.36 * u), (0.56 * u * sw, 0.40 * u * bw, 0.66 * u))            # ribcage
        add_ellipsoid(bm, (0, 0.03 * u, 0.62 * u), (0.80 * u * sw, 0.34 * u * bw, 0.27 * u))       # shoulder line
    make_obj("chest", chest, J["chest"], body)
    make_obj("neck", lambda bm: add_cone(bm, (0, 0, -0.1 * u), (0, 0, B - 5.72 * u + 0.25 * H), 0.2 * u * bw,
                                         0.17 * u * bw), J["neck"], body)

    def head(bm):
        # one egg: round skull, narrower jaw with the chin forward (no creases that read as an open mouth)
        res = bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
        for v in res["verts"]:
            x, y, z = v.co
            if z < 0:
                k = 1 - 0.38 * (-z) ** 1.5
                x, y = x * k, y * k - 0.10 * (-z) ** 2
            v.co = Vector((x * 0.36 * H, y * 0.44 * H, z * 0.52 * H + 0.56 * H))
        add_ellipsoid(bm, (0, -0.44 * H, 0.48 * H), (0.045 * H, 0.06 * H, 0.085 * H), segs=(16, 8))  # nose
        for s in (1, -1):
            add_ellipsoid(bm, (s * 0.35 * H, 0.04 * H, 0.52 * H), (0.04 * H, 0.09 * H, 0.13 * H), segs=(16, 8))
    make_obj("head", head, J["head"], body)
    if "eyes" in DETAIL:   # eyeballs + pupils that aim on their own: e.g. head down, eyes looking up
        gaze = (pose.get("head") or {}).get("gaze") or {}
        look = q(Z, num(gaze, "left")) @ q(X, -num(gaze, "up"))
        d = look @ Vector((0, -1, 0))
        re_ = 0.068 * H
        for side_, sx in (("left", 1), ("right", -1)):
            c = Vector((sx * 0.13 * H, -0.355 * H, 0.64 * H))
            make_obj(f"{side_}_eye", lambda bm, c_=c: add_ellipsoid(bm, c_, (re_, re_, re_), segs=(20, 10)),
                     J["head"], mats["eye"])
            make_obj(f"{side_}_pupil", lambda bm, c_=c: add_ellipsoid(bm, c_ + d * re_, (0.032 * H, 0.014 * H, 0.032 * H),
                                                                     rot=Vector((0, -1, 0)).rotation_difference(d),
                                                                     segs=(16, 8)), J["head"], mats["pupil"])

    L_up, L_fore = 1.35 * u, 1.08 * u
    L_thigh, L_shin = 1.62 * u, 1.56 * u
    sides = {"left": 1, "right": -1}
    hands = {}
    for side, s in sides.items():
        m = mats[side]
        sh = make_joint(f"{side}_shoulder", J["chest"], (s * 0.82 * u * sw, 0.04 * u, 0.70 * u))
        el = make_joint(f"{side}_elbow", sh, (0, 0, -L_up))
        wr = make_joint(f"{side}_wrist", el, (0, 0, -L_fore))
        J.update({sh.name: sh, el.name: el, wr.name: wr})
        make_obj(f"{side}_upper_arm", lambda bm: add_capsule(bm, (0, 0, 0), (0, 0, -L_up), 0.18 * u * bw,
                                                             0.125 * u * bw), sh, m)
        make_obj(f"{side}_forearm", lambda bm: add_capsule(bm, (0, 0, 0), (0, 0, -L_fore), 0.125 * u * bw,
                                                           0.085 * u * bw), el, m)
        arm = pose.get(f"{side}_arm") or {}
        shape = str(arm.get("hand", "relaxed")).lower()
        for p in pose.get("props", []):
            main = str(p.get("hand", "right")).lower()
            if p.get("type") == "sword" and (main == side or p.get("two_handed")):
                shape = "grip"
        if "hands" in DETAIL:
            flex, thumb = _hand_flex(arm, shape)
            hands[side] = make_obj(f"{side}_hand", lambda bm, s_=s, f_=flex, t_=thumb:
                                   build_hand_detailed(bm, s_, u, f_, t_), wr, m)
        else:
            hands[side] = make_obj(f"{side}_hand", lambda bm, sh_=("fist" if shape == "grip" else shape), s_=s:
                                   build_hand(bm, sh_, s_, u), wr, m)

        hp = make_joint(f"{side}_hip", J["root"], (s * 0.44 * u * hw, 0, -0.25 * u))
        kn = make_joint(f"{side}_knee", hp, (0, 0, -L_thigh))
        an = make_joint(f"{side}_ankle", kn, (0, 0, -L_shin))
        J.update({hp.name: hp, kn.name: kn, an.name: an})
        make_obj(f"{side}_thigh", lambda bm: add_capsule(bm, (0, 0, 0), (0, 0, -L_thigh), 0.29 * u * bw,
                                                         0.175 * u * bw), hp, m)

        def shin(bm, m_=m):
            add_capsule(bm, (0, 0, 0), (0, 0, -L_shin), 0.16 * u * bw, 0.10 * u * bw)
            add_ellipsoid(bm, (0, 0.04 * u, -0.45 * u), (0.15 * u * bw, 0.16 * u * bw, 0.42 * u))
        make_obj(f"{side}_shin", shin, kn, m)

        def foot(bm):
            add_ellipsoid(bm, (0, 0, 0), (0.10 * u, 0.10 * u, 0.10 * u), segs=(16, 8))
            if "feet" not in DETAIL:
                add_ellipsoid(bm, (0, -0.30 * u, -0.20 * u), (0.17 * u, 0.56 * u, 0.12 * u))   # sole
                add_ellipsoid(bm, (0, -0.10 * u, -0.10 * u), (0.15 * u, 0.32 * u, 0.17 * u))   # heel + instep
                return
            add_ellipsoid(bm, (0, 0.06 * u, -0.19 * u), (0.13 * u, 0.17 * u, 0.12 * u))      # heel
            add_ellipsoid(bm, (0, -0.28 * u, -0.20 * u), (0.15 * u, 0.32 * u, 0.11 * u))     # arch / sole
            add_ellipsoid(bm, (0, -0.16 * u, -0.08 * u), (0.13 * u, 0.26 * u, 0.13 * u))     # instep
            for sx in (1, -1):
                add_ellipsoid(bm, (sx * 0.10 * u, 0.0, -0.02 * u), (0.04 * u, 0.04 * u, 0.04 * u), segs=(12, 6))
        fo = make_obj(f"{side}_foot", foot, an, m)
        if "feet" in DETAIL:   # toe box hinged on the ball of the foot
            ball = make_joint(f"{side}_ball", fo, (0, -0.56 * u, -0.21 * u))
            J[ball.name] = ball
            make_obj(f"{side}_toes", lambda bm: add_capsule(bm, (0, 0, 0), (0, -0.28 * u, -0.01 * u), 0.115 * u,
                                                            0.085 * u), ball, m)

    props = []
    for p in pose.get("props", []):
        if p.get("type") == "sword":
            side = str(p.get("hand", "right")).lower()
            props.append(build_sword(p, hands[side], mats["sword"], u, H, 1 if side == "left" else -1))
        elif p.get("type") == "scabbard":
            props.append(build_scabbard(p, J["root"], mats["sword"], u, H, hw))
    return J, hands, props, {"H": H, "u": u, "B": B}


def build_scabbard(p, root, mat, u, H, hw):
    """A scabbard hanging from the belt at one hip, tilted back; optionally with a sheathed sword's hilt."""
    side = str(p.get("side", "left")).lower()
    s = 1 if side == "left" else -1
    L = max(1.0, num(p, "length", 3.4)) * H

    def build(bm):
        add_cone(bm, (0, 0, 0), (0, 0, -L), 0.075 * u, 0.055 * u, 16)
        add_ellipsoid(bm, (0, 0, -L), (0.06 * u, 0.06 * u, 0.08 * u), segs=(12, 6))                # chape
        add_ellipsoid(bm, (0, 0, -0.10 * u), (0.11 * u, 0.09 * u, 0.06 * u), segs=(12, 6))          # throat ring
        if p.get("sheathed", True):
            add_ellipsoid(bm, (0, 0, 0.06 * u), (0.30 * u, 0.06 * u, 0.04 * u), segs=(16, 8))      # guard
            add_cone(bm, (0, 0, 0.08 * u), (0, 0, 0.75 * u), 0.045 * u, 0.045 * u, 12)            # grip
            add_ellipsoid(bm, (0, 0, 0.80 * u), (0.065 * u,) * 3, segs=(12, 6))                    # pommel
    ob = make_obj("scabbard", build, root, mat)
    ob.location = Vector((s * 0.66 * u * hw, 0.10 * u, -0.05 * u))
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = q(X, -num(p, "angle", 35)) @ q(Y, s * num(p, "spread", 12))
    return ob


DETAIL = set()
# jointed hand, in metres for a hand on a 7-head, 1.7 m figure (scaled by u). Frame: x = index side, w = wrist ->
# knuckles, v = palm normal; mapped onto the hand object: x -> -Y (thumb forward), w -> -Z (down), v -> -s*X.
FINGERS = {"index": (0.029, 0.000, (0.041, 0.024, 0.019), 0.0086),
           "middle": (0.010, 0.004, (0.045, 0.027, 0.020), 0.0090),
           "ring": (-0.009, 0.000, (0.042, 0.026, 0.020), 0.0084),
           "pinky": (-0.026, -0.009, (0.033, 0.019, 0.017), 0.0074)}
HAND_PRESETS = {"fist": [68, 98, 48], "grip": [68, 98, 48], "relaxed": [22, 32, 16], "open": [2, 4, 2],
                "point": None}
PALM_HALF, WRIST_W = 0.0115, -0.088
GRIP_C = (-0.0085, 0.0230)                  # (w, v) of the hole of a fist: where a grip passes
U_REF = 1.457 / 6.0                         # body unit of the reference figure


def _finger_pts(name, flex):
    x, dw, lens, r = FINGERS[name]
    pts, th = [Vector((x, dw, 0.0))], 0.0
    for L, f in zip(lens, flex):
        th += math.radians(f)
        pts.append(pts[-1] + Vector((0, L * math.cos(th), L * math.sin(th))))
    return pts, [r * 1.08, r, r * 0.93, r * 0.86]


def _hand_flex(arm, shape):
    fing = arm.get("fingers") if isinstance(arm.get("fingers"), dict) else {}
    out = {}
    for f in FINGERS:
        if f in fing:
            out[f] = [float(a) for a in fing[f]][:3]
        elif shape == "point":
            out[f] = [0, 0, 0] if f == "index" else HAND_PRESETS["fist"]
        else:
            base = HAND_PRESETS.get(shape) or HAND_PRESETS["relaxed"]
            k = 1.0 + (0.15 * list(FINGERS).index(f) if shape == "relaxed" else 0.0)   # little finger curls more
            out[f] = [a * k for a in base]
    thumb = fing.get("thumb") or ("wrap" if shape in ("fist", "grip", "point") else
                                  "open" if shape == "open" else "relaxed")
    return out, thumb


def fist_center(s, u):
    """Where a held grip passes, in the hand object's frame (detailed hands)."""
    k = u / U_REF
    return Vector((-s * GRIP_C[1] * k, 0.0, -(GRIP_C[0] - WRIST_W) * k))


def build_hand_detailed(bm, s, u, flex, thumb):
    k = u / U_REF

    def T(p):   # hand-model (x, w, v) in metres -> the hand object's frame
        return Vector((-s * p.z * k, -p.x * k, -(p.y - WRIST_W) * k))
    xs = [FINGERS[f][0] for f in FINGERS]
    x0, x1 = min(xs) - 0.008, max(xs) + 0.008
    add_ellipsoid(bm, T(Vector(((x0 + x1) / 2, -0.042, 0))),
                  (PALM_HALF * 1.15 * k, ((x1 - x0) / 2 + 0.004) * k, 0.047 * k), rot=_hand_rot(s))
    for f in FINGERS:
        x, dw, _, r = FINGERS[f]
        add_ellipsoid(bm, T(Vector((x, dw - 0.002, 0.001))), (r * 1.18 * k,) * 3, segs=(16, 8))   # knuckle
        pts, radii = _finger_pts(f, flex[f])
        for i in range(3):
            add_capsule(bm, T(pts[i]), T(pts[i + 1]), radii[i] * k * (1.0 if i else 0.98), radii[i + 1] * k)
    add_ellipsoid(bm, T(Vector((x1 - 0.004, -0.056, PALM_HALF * 0.9))), (0.010 * k, 0.013 * k, 0.022 * k),
                  rot=_hand_rot(s))                                                                    # thenar
    ix = FINGERS["index"][0]
    if thumb == "wrap":   # around the grip, its tip on the index finger's middle phalanx
        pts, _ = _finger_pts("index", flex["index"])
        mid = (pts[1] + pts[2]) / 2
        out = Vector((0, mid.y - GRIP_C[0], mid.z - GRIP_C[1]))
        out = out.normalized() if out.length > 1e-6 else Vector((0, 0, 1))
        gr = 0.0104
        tip = Vector((ix - 0.010, GRIP_C[0], GRIP_C[1])) + out * (gr + 2 * 0.0088 + 0.0085)
        mcp = Vector((ix + 0.020, GRIP_C[0] - 0.032, GRIP_C[1] - 0.006))
        a0 = math.atan2(mcp.z - GRIP_C[1], mcp.y - GRIP_C[0])
        a1 = math.atan2(tip.z - GRIP_C[1], tip.y - GRIP_C[0])
        am = a0 + (((a1 - a0 + math.pi) % (2 * math.pi)) - math.pi) * 0.55
        rr = gr + 0.0135
        ip = Vector((ix + 0.010, GRIP_C[0] + rr * math.cos(am), GRIP_C[1] + rr * math.sin(am)))
    elif thumb == "open":
        mcp, ip, tip = (Vector((ix + 0.034, -0.040, 0.010)), Vector((ix + 0.050, -0.016, 0.012)),
                        Vector((ix + 0.060, 0.008, 0.012)))
    else:                 # relaxed: along the index finger's side
        mcp, ip, tip = (Vector((ix + 0.026, -0.040, 0.014)), Vector((ix + 0.030, -0.014, 0.024)),
                        Vector((ix + 0.024, 0.008, 0.032)))
    cmc = Vector((ix + 0.006, -0.068, PALM_HALF * 0.6))
    for a, b, ra, rb in ((cmc, mcp, 0.0125, 0.0105), (mcp, ip, 0.0105, 0.0098), (ip, tip, 0.0098, 0.0090)):
        add_capsule(bm, T(a), T(b), ra * k, rb * k)
    add_capsule(bm, T(Vector(((x0 + x1) / 2, WRIST_W, 0))), T(Vector(((x0 + x1) / 2, -0.070, 0))),
                0.024 * k, 0.026 * k)                                                                 # wrist
    if s > 0:   # the left hand is the mirror image: turn its faces the right way out
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))


def _hand_rot(s):
    """Ellipsoid radii are already given as (thickness, width, length) = the hand object's (X, Y, Z)."""
    return None


def build_hand(bm, shape, s, u):
    """Hand hanging down from the wrist: palm faces the body (-s*X), thumb forward (-Y)."""
    if shape == "fist":
        add_ellipsoid(bm, (0, -0.01 * u, -0.23 * u), (0.12 * u, 0.18 * u, 0.20 * u))
        add_ellipsoid(bm, (-s * 0.07 * u, -0.15 * u, -0.17 * u), (0.05 * u, 0.06 * u, 0.10 * u), segs=(16, 8))
        return
    curl = 0.0 if shape == "open" else 0.06 * u
    add_ellipsoid(bm, (0, 0, -0.21 * u), (0.065 * u, 0.19 * u, 0.22 * u))                          # palm
    add_ellipsoid(bm, (-s * curl, 0.01 * u, -0.48 * u + curl), (0.055 * u, 0.16 * u, 0.17 * u))     # fingers
    add_capsule(bm, (-s * 0.03 * u, -0.15 * u, -0.08 * u), (-s * (0.07 * u + curl), -0.27 * u, -0.27 * u),
                0.045 * u, 0.035 * u)                                                               # thumb


def build_sword(p, hand_obj, mat, u, H, s=-1):
    """Sword through the fist: the grip runs front-back through the hand; the blade leaves the thumb side
    (normal grip) or the little-finger side (reverse grip). Edges face up/down when the arm hangs."""
    blade = max(0.5, num(p, "length", 3.2)) * H
    c = fist_center(s, u) if "hands" in DETAIL else Vector((0, -0.01 * u, -0.23 * u))  # fist centre

    back = 0.62 * u if p.get("two_handed") else 0.30 * u  # a two-handed grip has room for the second fist

    def build(bm):
        add_cone(bm, (0, back, 0), (0, -0.20 * u, 0), 0.045 * u, 0.045 * u, 16)                    # grip
        add_ellipsoid(bm, (0, back + 0.03 * u, 0), (0.065 * u, 0.065 * u, 0.065 * u), segs=(16, 8))  # pommel
        add_ellipsoid(bm, (0, -0.23 * u, 0), (0.05 * u, 0.045 * u, 0.36 * u), segs=(16, 8))       # guard
        w, t, y0, y1 = 0.10 * u, 0.022 * u, -0.26 * u, -0.26 * u - blade  # blade: lozenge section + point
        tip_len = min(0.5 * u, blade * 0.15)
        rings = []
        for y, k in ((y0, 1.0), (y1 + tip_len, 0.8)):
            rings.append([bm.verts.new((0, y, k * w)), bm.verts.new((k * t, y, 0)),
                          bm.verts.new((0, y, -k * w)), bm.verts.new((-k * t, y, 0))])
        tip = bm.verts.new((0, y1, 0))
        a, b = rings
        bm.faces.new(a[::-1])
        for i in range(4):
            j = (i + 1) % 4
            bm.faces.new((a[i], a[j], b[j], b[i]))
            bm.faces.new((b[i], b[j], tip))
    sign = 1 if str(p.get("grip", "normal")).lower() != "reverse" else -1
    ob = make_obj("sword", build, hand_obj, mat)
    ob.location = c
    ob.rotation_mode = "QUATERNION"
    # a fist holds the grip about 20 degrees past square: the blade leans toward the little finger's side
    # (jointed fingers wrap a square grip, so no extra tilt there)
    tilt = 0 if "hands" in DETAIL else 20
    ob.rotation_quaternion = q(X, tilt) if sign == 1 else q(Z, 180) @ q(X, -tilt)
    return ob


# ------------------------------------------------------------------ posing
WORDS = {"forward": (0, 1, 0), "front": (0, 1, 0), "back": (0, -1, 0), "backward": (0, -1, 0), "up": (0, 0, 1),
         "down": (0, 0, -1), "out": (1, 0, 0), "outward": (1, 0, 0), "side": (1, 0, 0), "in": (-1, 0, 0),
         "across": (-1, 0, 0), "inward": (-1, 0, 0)}


def aim_vector(aim, s):
    """(out, forward, up) components -> a direction in the parent's frame for side s (+1 left, -1 right)."""
    o = f = up = 0.0
    if aim is None:
        return Vector((0, 0, -1))
    if isinstance(aim, str):
        for w in aim.replace("+", "-").replace(" ", "-").replace("_", "-").split("-"):
            if w:
                if w.lower() not in WORDS:
                    raise ValueError(f"unknown aim word '{w}' (use {', '.join(sorted(WORDS))})")
                a = WORDS[w.lower()]
                o, f, up = o + a[0], f + a[1], up + a[2]
    elif isinstance(aim, (list, tuple)) and len(aim) == 3:
        f, up, o = (float(x) for x in aim)
    elif isinstance(aim, dict):
        f = num(aim, "forward") - num(aim, "back")
        up = num(aim, "up") - num(aim, "down")
        o = num(aim, "out") - num(aim, "in") - num(aim, "across")
    v = Vector((s * o, -f, up))
    return v.normalized() if v.length > 1e-6 else Vector((0, 0, -1))


def swing(direction):
    rest = Vector((0, 0, -1))
    if direction.dot(rest) < -0.99999:          # straight up: raise through the front
        return q(X, -180)
    return rest.rotation_difference(direction)


def apply_pose(J, pose):
    root = pose.get("root") or {}
    J["root"].rotation_quaternion = q(Z, num(root, "turn")) @ q(X, num(root, "pitch")) @ q(Y, num(root, "roll"))
    sp = pose.get("spine") or {}
    half = q(Z, num(sp, "twist") / 2) @ q(X, num(sp, "bend") / 2) @ q(Y, num(sp, "side") / 2)
    J["spine"].rotation_quaternion = half
    J["chest"].rotation_quaternion = half
    hd = pose.get("head") or {}
    J["neck"].rotation_quaternion = q(Z, 0.4 * num(hd, "turn")) @ q(X, 0.4 * num(hd, "nod")) @ q(Y, 0.4 * num(hd, "tilt"))
    J["head"].rotation_quaternion = q(Z, 0.6 * num(hd, "turn")) @ q(X, 0.6 * num(hd, "nod")) @ q(Y, 0.6 * num(hd, "tilt"))
    for side, s in (("left", 1), ("right", -1)):
        a = pose.get(f"{side}_arm") or {}
        aim = a.get("aim", {"down": 1, "out": 0.12})
        J[f"{side}_shoulder"].rotation_quaternion = swing(aim_vector(aim, s)) @ q(Z, s * num(a, "twist"))
        J[f"{side}_elbow"].rotation_quaternion = q(X, -max(0.0, min(160.0, num(a, "elbow", 8))))
        J[f"{side}_wrist"].rotation_quaternion = q(Y, s * num(a, "wrist")) @ q(X, -num(a, "wrist_tilt"))
        lg = pose.get(f"{side}_leg") or {}
        aim = lg.get("aim", {"down": 1, "out": 0.04})
        J[f"{side}_hip"].rotation_quaternion = swing(aim_vector(aim, s)) @ q(Z, s * num(lg, "twist"))
        J[f"{side}_knee"].rotation_quaternion = q(X, max(0.0, min(165.0, num(lg, "knee"))))
        J[f"{side}_ankle"].rotation_quaternion = q(X, num(lg, "ankle"))
        if f"{side}_ball" in J:
            J[f"{side}_ball"].rotation_quaternion = q(X, -num(lg, "toes"))


def two_hand_ik(J, hands, sword, p, dims, warnings):
    """Second hand on a two-handed grip: solve the other arm (2-bone IK, elbow pointing down and out) so that its
    fist sits on the grip next to the pommel, turned like the main hand."""
    main = str(p.get("hand", "right")).lower()
    off = "left" if main == "right" else "right"
    s = 1 if off == "left" else -1
    u = dims["u"]
    bpy.context.view_layer.update()
    r_hand = hands[main].matrix_world.to_quaternion()
    grip = sword.matrix_world @ Vector((0, (0.36 if "hands" in DETAIL else 0.40) * u, 0))
    fc = fist_center(s, u) if "hands" in DETAIL else Vector((0, -0.01 * u, -0.23 * u))
    target = grip - r_hand @ fc                                       # where the off wrist must be
    sh, el, wr = J[f"{off}_shoulder"], J[f"{off}_elbow"], J[f"{off}_wrist"]
    S = sh.matrix_world.translation.copy()
    L1, L2 = el.location.length, wr.location.length
    dv = target - S
    d = dv.length
    if d > (L1 + L2) * 0.995:
        warnings.append(f"the {off} hand can't reach the grip (sword too far from that shoulder): bring the sword "
                        "closer to the body or turn the chest toward it")
    d = max(abs(L1 - L2) + 1e-4, min(d, (L1 + L2) * 0.995))
    axis = dv.normalized()
    W = S + axis * d
    chest = J["chest"].matrix_world.to_quaternion()
    pole = chest @ Vector((s * 0.7, 0.35, -1.0))
    pole = pole - axis * pole.dot(axis)
    if pole.length < 1e-6:
        pole = chest @ Vector((s, 0, 0))
        pole = pole - axis * pole.dot(axis)
    pole.normalize()
    a = (L1 * L1 - L2 * L2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, L1 * L1 - a * a))
    E = S + axis * a + pole * h
    dz = (E - S).normalized()
    to_w = W - E
    fy = to_w - dz * to_w.dot(dz)
    fy = fy.normalized() if fy.length > 1e-6 else (chest @ Vector((0, -1, 0)))
    zl, yl = -dz, -fy
    xl = yl.cross(zl)
    m = Matrix((xl, yl, zl)).transposed()
    sh.rotation_quaternion = chest.inverted() @ m.to_quaternion()
    bend = math.degrees(dz.angle(to_w)) if to_w.length > 1e-6 else 0.0
    el.rotation_quaternion = q(X, -bend)
    bpy.context.view_layer.update()
    wr.rotation_quaternion = el.matrix_world.to_quaternion().inverted() @ r_hand
    bpy.context.view_layer.update()


def world_verts(objs):
    pts = []
    for ob in objs:
        mw = ob.matrix_world
        pts += [mw @ v.co for v in ob.data.vertices]
    return pts


def ground(J, body_objs, pose, dims):
    root = pose.get("root") or {}
    bpy.context.view_layer.update()
    if root.get("ground", True):
        minz = min(p.z for p in world_verts(body_objs))
        J["root"].location.z -= minz
    J["root"].location.z += num(root, "lift") * dims["H"]
    bpy.context.view_layer.update()


# ------------------------------------------------------------------ scene, camera, render
def setup_scene(job):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = int(job.get("samples", 24))
    sc.cycles.use_denoising = True
    sc.render.resolution_x, sc.render.resolution_y = int(job.get("width", 896)), int(job.get("height", 1152))
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    try:
        sc.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    world = bpy.data.worlds.new("world")
    sc.world = world
    world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs[0].default_value = (0.20, 0.20, 0.21, 1.0)
    bg.inputs[1].default_value = 1.0
    return sc


def frame_camera(sc, cam, pts, cam_cfg):
    az = math.radians(num(cam_cfg, "azimuth", 0))
    el = math.radians(num(cam_cfg, "elevation", 5))
    margin = max(0.0, min(0.3, num(cam_cfg, "margin", 0.06)))
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    target = (lo + hi) / 2
    radius = max((p - target).length for p in pts)
    direction = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    cd = cam.data
    cd.sensor_fit = "AUTO"
    if cam_cfg.get("ortho"):
        cd.type = "ORTHO"
        cd.ortho_scale = radius * 2.2
        dist = radius * 4 + 1
    else:  # the distance is fixed (in figure heights), so perspective is the same for every pose; zoom = lens
        cd.type = "PERSP"
        dist = max(1.2, min(20.0, num(cam_cfg, "distance", 4.0))) * HEIGHT
        dist = max(dist, radius * 1.3)
        cd.lens = 50.0
    cd.clip_start, cd.clip_end = 0.01, 1000
    # a low-angle shot keeps the camera just above the floor, looking up (below it, the floor hides the figure)
    floor_clear = 0.12 * HEIGHT
    if target.z + direction.z * dist < floor_clear:
        el = math.asin(max(-1.0, min(1.0, (floor_clear - target.z) / dist)))
        direction = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    cam_cfg["_elevation_used"] = round(math.degrees(el), 1)

    def place(d):
        cam.location = target + direction * d
        cam.rotation_mode = "QUATERNION"
        cam.rotation_quaternion = (-direction).to_track_quat("-Z", "Y")
        bpy.context.view_layer.update()

    def extents():
        xs, ys = [], []
        for p in pts:
            v = world_to_camera_view(sc, cam, p)
            xs.append(v.x)
            ys.append(v.y)
        return min(xs), max(xs), min(ys), max(ys)

    cd.shift_x = cd.shift_y = 0.0
    for _ in range(6):
        place(dist)
        x0, x1, y0, y1 = extents()
        s = max((x1 - x0) / (1 - 2 * margin), (y1 - y0) / (1 - 2 * margin))
        if abs(s - 1) < 0.002:
            break
        if cd.type == "ORTHO":
            cd.ortho_scale *= s
        else:
            cd.lens = max(6.0, min(600.0, cd.lens / s))
    place(dist)
    x0, x1, y0, y1 = extents()
    W, Hh = sc.render.resolution_x, sc.render.resolution_y
    big = max(W, Hh)
    cd.shift_x = ((x0 + x1) / 2 - 0.5) * W / big
    cd.shift_y = ((y0 + y1) / 2 - 0.5) * Hh / big
    bpy.context.view_layer.update()
    return target, direction, dist


def add_lights(target, direction, dims):
    u = dims["u"]
    side = Vector((direction.y, -direction.x, 0)).normalized() if Vector((direction.x, direction.y)).length > 1e-6 \
        else X.copy()
    key_dir = (direction * 0.6 + side * 0.55 + Z * 1.5).normalized()   # camera-left, high above
    ld = bpy.data.lights.new("key", "AREA")
    ld.size = 6 * u
    ld.energy = 140.0 * (HEIGHT / 1.7) ** 2
    key = bpy.data.objects.new("key", ld)
    bpy.context.scene.collection.objects.link(key)
    key.location = target + key_dir * 14 * u
    key.rotation_mode = "QUATERNION"
    key.rotation_quaternion = (-key_dir).to_track_quat("-Z", "Y")


def add_floor(u):
    me = bpy.data.meshes.new("floor")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=60 * u)
    bm.to_mesh(me)
    bm.free()
    fl = bpy.data.objects.new("floor", me)
    bpy.context.scene.collection.objects.link(fl)
    fl.is_shadow_catcher = True
    return fl


def depth_material(near, far):
    m = bpy.data.materials.new("depth")
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    camd = nt.nodes.new("ShaderNodeCameraData")
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.clamp = True
    mr.inputs["From Min"].default_value = near
    mr.inputs["From Max"].default_value = far
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.22
    nt.links.new(camd.outputs["View Z Depth"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], em.inputs["Strength"])
    em.inputs["Color"].default_value = (1, 1, 1, 1)
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    return m


PART_GROUPS = ("left_hand", "right_hand", "left_forearm", "right_forearm", "left_upper_arm", "right_upper_arm",
               "left_thigh", "right_thigh", "left_shin", "right_shin", "left_foot", "right_foot", "left_toes",
               "right_toes", "head", "neck", "chest", "abdomen", "pelvis", "sword", "scabbard")


def part_group(obj_name):
    n = obj_name.split(".")[0]
    if n == "pelvis_marks":
        return "pelvis"
    if n.endswith("_eye") or n.endswith("_pupil"):
        return "head"
    if n.endswith("_toes"):
        return n.replace("_toes", "_foot")
    return n if n in PART_GROUPS else n


def render_to(sc, path):
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


def run_job(job):
    t0 = time.time()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = setup_scene(job)
    pose = job.get("pose") or {}
    DETAIL.clear()
    DETAIL.update(str(d).lower() for d in job.get("detail") or [])
    J, hands, props, dims = build_figure(job.get("figure") or {}, pose, bool(job.get("color_sides")))
    body_objs = [o for o in sc.objects if o.type == "MESH" and o.name not in [p.name for p in props]]
    apply_pose(J, pose)
    warnings = []
    for p, ob in zip([x for x in pose.get("props", []) if x.get("type") == "sword"], props):
        if p.get("two_handed"):
            two_hand_ik(J, hands, ob, p, dims, warnings)
    ground(J, body_objs, pose, dims)
    all_objs = body_objs + props
    show = set(job.get("show") or [])
    if show:   # a partial mannequin (e.g. only the hands and the sword), framed on those parts
        for o in all_objs:
            if part_group(o.name) not in show:
                o.hide_render = True
        all_objs = [o for o in all_objs if not o.hide_render] or all_objs
    pts = world_verts(all_objs)

    cam = bpy.data.objects.new("camera", bpy.data.cameras.new("camera"))
    sc.collection.objects.link(cam)
    sc.camera = cam
    cam_cfg = job.get("camera") or {}
    target, direction, dist = frame_camera(sc, cam, pts, cam_cfg)
    add_lights(target, direction, dims)
    floor = add_floor(dims["u"])
    if show:
        floor.hide_render = True

    out, name = job["out"].rstrip("/\\"), job["name"]
    outputs = job.get("outputs") or ["mannequin"]
    files = {}
    if "mannequin" in outputs:
        render_to(sc, f"{out}/{name}.png")
        files["mannequin"] = f"{name}.png"
    parts_map = {}
    if "parts" in outputs:   # every body part in one flat colour: the server builds attention masks from it
        floor.hide_render = True
        sc.render.engine = "BLENDER_WORKBENCH"
        shd = sc.display.shading
        shd.light, shd.color_type = "FLAT", "OBJECT"
        shd.show_cavity = shd.show_object_outline = False
        sc.display.render_aa = "OFF"
        sc.render.dither_intensity = 0.0
        sc.view_settings.view_transform = "Standard"
        groups = sorted({part_group(o.name) for o in all_objs})
        for i, g in enumerate(groups):
            c = ((i * 53) % 200 + 40, (i * 97) % 200 + 40, (i * 151) % 200 + 40)
            parts_map[g] = c
        for o in all_objs:
            c = parts_map[part_group(o.name)]
            lin = [((v / 255) / 12.92) if v / 255 <= 0.04045 else (((v / 255) + 0.055) / 1.055) ** 2.4 for v in c]
            o.color = (*lin, 1.0)
        render_to(sc, f"{out}/{name}-parts.png")
        files["parts"] = f"{name}-parts.png"
        sc.render.engine = "CYCLES"
        floor.hide_render = bool(show)
    inv = cam.matrix_world.inverted()
    depths = [-(inv @ p).z for p in pts]
    near, far = min(depths), max(depths)
    if "depth" in outputs:
        floor.hide_render = True
        sc.render.film_transparent = False
        bg = next(n for n in sc.world.node_tree.nodes if n.type == "BACKGROUND")
        bg.inputs[0].default_value = (0, 0, 0, 1)
        try:
            sc.view_settings.view_transform = "Raw"
        except TypeError:
            pass
        sc.cycles.samples = 8
        sc.cycles.use_denoising = False
        pad = (far - near) * 0.02 + 1e-4
        sc.view_layers[0].material_override = depth_material(near - pad, far + pad)
        render_to(sc, f"{out}/{name}-depth.png")
        files["depth"] = f"{name}-depth.png"

    W, Hh = sc.render.resolution_x, sc.render.resolution_y

    def px(p):
        v = world_to_camera_view(sc, cam, p)
        return [round(v.x * W, 1), round((1 - v.y) * Hh, 1)]
    joints = {k: px(e.matrix_world.translation) for k, e in J.items()}
    head = sc.objects["head"]
    H = dims["H"]
    for k, local in {"nose": (0, -0.50 * H, 0.48 * H), "crown": (0, 0, 1.08 * H),
                     "chin": (0, -0.38 * H, 0.06 * H)}.items():
        joints[k] = px(head.matrix_world @ Vector(local))
    u = dims["u"]
    for side in ("left", "right"):
        joints[f"{side}_hand_tip"] = px(hands[side].matrix_world @ Vector((0, 0, -0.62 * u)))
        joints[f"{side}_toe"] = px(sc.objects[f"{side}_foot"].matrix_world @ Vector((0, -0.84 * u, -0.19 * u)))
    for p in props:
        if p.name.startswith("sword"):
            lo = min(p.data.vertices, key=lambda v: v.co.y).co
            joints["sword_tip"] = px(p.matrix_world @ lo)
            joints["sword_guard"] = px(p.matrix_world @ Vector((0, -0.23 * u, 0)))
            joints["sword_pommel"] = px(p.matrix_world @ Vector((0, max(v.co.y for v in p.data.vertices), 0)))
        elif p.name.startswith("scabbard"):
            joints["scabbard_top"] = px(p.matrix_world.translation)
    if "hands" in DETAIL:
        for side, s_ in (("left", 1), ("right", -1)):
            joints[f"{side}_fist"] = px(hands[side].matrix_world @ fist_center(s_, u))
    joints["pelvis_front"] = px(sc.objects["pelvis"].matrix_world @ Vector((0, -0.44 * u, 0.06 * u)))
    xs = [px(p) for p in pts[::7]]
    bbox = [min(a for a, _ in xs), min(b for _, b in xs), max(a for a, _ in xs), max(b for _, b in xs)]
    info = {"name": name, "files": files, "warnings": warnings, "width": W, "height": Hh, "joints_px": joints,
            "figure_bbox_px": bbox, "parts": parts_map, "detail": sorted(DETAIL),
            "camera": {"azimuth": num(cam_cfg, "azimuth", 0), "elevation": num(cam_cfg, "elevation", 5),
                       "elevation_used": cam_cfg.get("_elevation_used"),
                       "distance": num(cam_cfg, "distance", 4.0), "lens": round(cam.data.lens, 1),
                       "ortho": cam.data.type == "ORTHO"},
            "seconds": round(time.time() - t0, 2), "blender": bpy.app.version_string}
    with open(f"{out}/{name}.json", "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    print(f"POSE_DONE {name} {info['seconds']}s", flush=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        raise SystemExit("usage: blender -b --factory-startup --python pose_render.py -- <jobs.json>")
    with open(argv[0], encoding="utf-8") as fh:
        spec = json.load(fh)
    jobs = spec.get("jobs") or [spec]
    failed = 0
    for job in jobs:
        try:
            run_job(job)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"POSE_FAILED {job.get('name')}: {type(e).__name__}: {e}", flush=True)
    print("POSE_ALL_DONE" if not failed else f"POSE_ERRORS {failed}", flush=True)


main()
