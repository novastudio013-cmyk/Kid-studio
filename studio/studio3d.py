"""studio3d.py - builds cartoon characters and scenes from primitives in Blender.

Run headless:
  blender -b --factory-startup -P studio3d.py -- --test-still /tmp/pip.png
  blender -b --factory-startup -P studio3d.py -- --scene counting --count 3 --frames 60 --out out/s1

Technique (borrowed from procedural-kids-shorts): UV spheres / cones / beveled beziers only,
toon outline = Solidify modifier with flipped normals + emission shader in slot 1,
orthographic auto-fitting camera, sine-based idle animation.
"""
import argparse
import math
import sys
from pathlib import Path

import json
import bpy
from mathutils import Matrix, Vector

# ------------------------------------------------------------------ palettes
# Pip: baby penguin, blue-grey feathers, white belly, orange beanie
C_BODY = "#9DBEDD"
C_BELLY = "#FBFDFF"
C_BEAK = "#F59B2E"
C_FEET = "#F59B2E"
C_HAT = "#EF7A2F"
C_HAT_RIBBON = "#FFC94A"
C_INK = "#22314A"
C_BLUSH = "#F7A8C4"

OUTW = 0.030
HULL_OUTLINE = False   # ink outline costs ~8x render time on CPU Cycles; off by default
RES_X, RES_Y = 864, 1536

_mats = {}


def lin(hexstr):
    h = hexstr.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (*out, 1.0)


def mat(hexstr, rough=0.42, coat=0.3, emit=0.0):
    key = (hexstr, rough, coat, emit)
    if key in _mats:
        return _mats[key]
    m = bpy.data.materials.new("m_" + hexstr)
    try:
        m.use_nodes = True
    except Exception:
        pass
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = lin(hexstr)
    b.inputs["Roughness"].default_value = rough
    if "Coat Weight" in b.inputs:
        b.inputs["Coat Weight"].default_value = coat
    if emit:
        b.inputs["Emission Color"].default_value = lin(hexstr)
        b.inputs["Emission Strength"].default_value = emit
    _mats[key] = m
    return m


def _outline_mat_cycles():
    """Inverted-hull ink that works in Cycles: only back faces are opaque."""
    if "outline_cy" in _mats:
        return _mats["outline_cy"]
    m = bpy.data.materials.new("outline_cy")
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = lin(C_INK)
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(geo.outputs["Backfacing"], mix.inputs[0])
    nt.links.new(em.outputs[0], mix.inputs[1])
    nt.links.new(tr.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs[0])
    _mats["outline_cy"] = m
    return m


def _outline_mat():
    if "outline" in _mats:
        return _mats["outline"]
    m = bpy.data.materials.new("outline")
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = lin(C_INK)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(em.outputs[0], out.inputs[0])
    m.use_backface_culling = True
    _mats["outline"] = m
    return m


def link_parent(o, parent):
    if parent is None:
        return
    bpy.context.view_layer.update()
    mw = o.matrix_world.copy()
    o.parent = parent
    o.matrix_world = mw


def finish_obj(o, m, outline, parent):
    o.data.materials.append(m)
    for p in o.data.polygons:
        p.use_smooth = True
    if outline and HULL_OUTLINE:
        o.data.materials.append(_outline_mat_cycles())
        mod = o.modifiers.new("outline", "SOLIDIFY")
        mod.thickness = OUTW
        mod.offset = 1.0
        mod.use_flip_normals = True
        mod.use_rim = False
        mod.material_offset = 1
    link_parent(o, parent)
    return o


def select_only(o):
    for x in bpy.context.selected_objects:
        x.select_set(False)
    o.select_set(True)
    bpy.context.view_layer.objects.active = o


def ell(loc, size, m, parent=None, outline=True, rot=(0, 0, 0), quat=None, seg=24):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=max(8, seg // 2), radius=1, location=loc)
    o = bpy.context.active_object
    if quat is not None:
        o.rotation_mode = "QUATERNION"
        o.rotation_quaternion = quat
    else:
        o.rotation_euler = rot
    o.scale = size
    select_only(o)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish_obj(o, m, outline, parent)


def cone(loc, r, depth, m, parent=None, outline=True, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=r, radius2=r * 0.12, depth=depth,
                                    location=loc, rotation=rot)
    return finish_obj(bpy.context.active_object, m, outline, parent)


def torus(loc, major, minor, m, parent=None, outline=True, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(location=loc, rotation=rot, major_radius=major,
                                     minor_radius=minor, major_segments=48, minor_segments=16)
    return finish_obj(bpy.context.active_object, m, outline, parent)


def tube(pts, radii, m, parent=None, outline=True, caps=True):
    cu = bpy.data.curves.new("tube", "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = 1.0
    cu.bevel_resolution = 6
    cu.resolution_u = 12
    cu.use_fill_caps = True
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(len(pts) - 1)
    for bp, p, r in zip(sp.bezier_points, pts, radii):
        bp.co = p
        bp.handle_left_type = bp.handle_right_type = "AUTO"
        bp.radius = r
    o = bpy.data.objects.new("tube", cu)
    bpy.context.collection.objects.link(o)
    select_only(o)
    bpy.ops.object.convert(target="MESH")
    o = bpy.context.active_object
    finish_obj(o, m, outline, parent)
    if caps:
        for p, r in ((pts[0], radii[0]), (pts[-1], radii[-1])):
            ell(p, (r, r, r), m, parent, outline)
    return o


def pivot(name, loc, parent=None):
    e = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(e)
    e.matrix_world = Matrix.Translation(Vector(loc))
    link_parent(e, parent)
    return e


def patch(center, radii, d, r, m, parent, thick=0.03):
    d = Vector(d).normalized()
    c, R = Vector(center), Vector(radii)
    p = c + Vector((R.x * d.x, R.y * d.y, R.z * d.z))
    n = Vector((d.x / R.x, d.y / R.y, d.z / R.z)).normalized()
    return ell(p - n * thick * 0.3, (r, r * 0.8, thick), m, parent, outline=False,
               quat=n.to_track_quat("Z", "Y"))


def eye(loc, r, head):
    """Big shiny toddler eye: white, iris, pupil, catchlight."""
    x, y, z = loc
    piv = pivot("eye", loc, head)
    ell((x, y, z), (r, r * 0.55, r * 1.12), mat("#FFFFFF", 0.2), piv, outline=False)
    py = y - r * 0.42
    ell((x, py, z - r * 0.04), (r * 0.66, r * 0.26, r * 0.76), mat("#2B3A55", 0.25), piv, outline=False)
    ell((x, py - r * 0.16, z - r * 0.04), (r * 0.34, r * 0.20, r * 0.72), mat("#12161F", 0.15), piv, outline=False)
    ell((x - r * 0.28, py - r * 0.30, z + r * 0.36), (r * 0.22,) * 3, mat("#FFFFFF", 0.2, emit=1.4), piv, outline=False)
    return piv


# ------------------------------------------------------------------ PIP
def pip():
    """Pip the baby penguin. Returns rig dict."""
    body_m = mat(C_BODY, 0.45)
    belly_m = mat(C_BELLY, 0.5)
    beak_m = mat(C_BEAK, 0.35)
    hat_m = mat(C_HAT, 0.62)
    rib_m = mat(C_HAT_RIBBON, 0.5)
    blush_m = mat(C_BLUSH, 0.55)

    root = pivot("root", (0, 0, 0))
    body = pivot("body", (0, 0, 0), root)

    # round baby body
    bc, br = (0, 0, 1.05), (0.82, 0.76, 0.92)
    ell(bc, br, body_m, body)
    # white belly patch
    ell((0, -0.30, 0.92), (0.60, 0.52, 0.66), belly_m, body, outline=False)

    # wings
    wings = []
    for sx in (-1, 1):
        wp = pivot("wing", (sx * 0.70, 0, 1.15), body)
        ell((sx * 0.76, 0.02, 1.05), (0.16, 0.30, 0.46), body_m, wp, rot=(0, 0, sx * 0.18))
        wings.append((wp, sx))

    # feet
    for sx in (-1, 1):
        ell((sx * 0.30, -0.42, 0.16), (0.24, 0.34, 0.13), beak_m, body)

    # head
    head = pivot("head", (0, 0, 1.80), body)
    hc, hr = (0, 0, 2.02), (0.66, 0.62, 0.62)
    ell(hc, hr, body_m, head)
    # beak
    cone((0, -0.62, 1.94), 0.17, 0.28, beak_m, head, rot=(math.radians(88), 0, 0))
    # blush
    for sx in (-1, 1):
        ell((sx * 0.44, -0.44, 1.86), (0.13, 0.05, 0.09), blush_m, head, outline=False)
    eyes = [eye((sx * 0.27, -0.50, 2.16), 0.20, head) for sx in (-1, 1)]

    # knitted beanie: cap + brim + pompom
    ell((0, 0.04, 2.52), (0.68, 0.66, 0.42), hat_m, head)
    torus((0, 0.04, 2.34), 0.62, 0.11, rib_m, head, rot=(math.radians(4), 0, 0))
    ell((0, 0.05, 2.88), (0.17, 0.17, 0.17), rib_m, head)
    # little knit bobbles on the band
    for i in range(10):
        a = 2 * math.pi * i / 10
        ell((0.62 * math.cos(a), 0.04 + 0.60 * math.sin(a), 2.34),
            (0.055, 0.055, 0.055), rib_m, head, outline=False)

    return dict(root=root, body=body, head=head, wings=wings, eyes=eyes, yaw=14)


# ------------------------------------------------------------------ other characters
def duck():
    """Dilly the duck - round yellow duckling."""
    body_m = mat("#FFD23F", 0.45)
    beak_m = mat("#F58A2E", 0.35)
    blush_m = mat(C_BLUSH, 0.55)
    wing_m = mat("#F2C02E", 0.5)

    root = pivot("root", (0, 0, 0))
    body = pivot("body", (0, 0, 0), root)
    ell((0, 0, 1.02), (0.80, 0.74, 0.86), body_m, body)
    ell((0, -0.30, 0.92), (0.55, 0.46, 0.60), mat("#FFE9A8", 0.5), body, outline=False)

    wings = []
    for sx in (-1, 1):
        wp = pivot("wing", (sx * 0.68, 0, 1.10), body)
        ell((sx * 0.74, 0.02, 1.02), (0.15, 0.28, 0.42), wing_m, wp, rot=(0, 0, sx * 0.18))
        wings.append((wp, sx))

    for sx in (-1, 1):
        ell((sx * 0.28, -0.40, 0.14), (0.22, 0.32, 0.11), beak_m, body)

    head = pivot("head", (0, 0, 1.72), body)
    ell((0, 0, 1.96), (0.62, 0.58, 0.60), body_m, head)
    # flat duck bill
    ell((0, -0.60, 1.86), (0.26, 0.20, 0.10), beak_m, head)
    ell((0, -0.66, 1.83), (0.21, 0.15, 0.055), mat("#E0761F", 0.4), head, outline=False)
    for sx in (-1, 1):
        ell((sx * 0.42, -0.40, 1.80), (0.12, 0.05, 0.085), blush_m, head, outline=False)
    eyes = [eye((sx * 0.25, -0.47, 2.10), 0.19, head) for sx in (-1, 1)]
    # little tuft
    for i, dx in enumerate((-0.09, 0.0, 0.09)):
        ell((dx, -0.04, 2.44 - abs(dx)), (0.055, 0.05, 0.13), body_m, head)
    return dict(root=root, body=body, head=head, wings=wings, eyes=eyes, yaw=14)


def frog():
    """Freda the frog."""
    body_m = mat("#7BC95A", 0.5)
    belly_m = mat("#E6F5B8", 0.55)
    blush_m = mat(C_BLUSH, 0.55)
    dark = mat("#4E8F35", 0.5)

    root = pivot("root", (0, 0, 0))
    body = pivot("body", (0, 0, 0), root)
    ell((0, 0, 0.92), (0.88, 0.80, 0.72), body_m, body)
    ell((0, -0.34, 0.80), (0.62, 0.48, 0.50), belly_m, body, outline=False)

    wings = []
    for sx in (-1, 1):
        wp = pivot("wing", (sx * 0.72, -0.05, 0.86), body)
        ell((sx * 0.80, -0.05, 0.78), (0.16, 0.34, 0.20), body_m, wp, rot=(0, 0, sx * 0.25))
        wings.append((wp, sx))

    # big frog feet
    for sx in (-1, 1):
        for dx in (-0.12, 0, 0.12):
            ell((sx * 0.42 + dx, -0.46, 0.12), (0.10, 0.26, 0.09), body_m, body)

    head = pivot("head", (0, 0, 1.42), body)
    ell((0, 0, 1.62), (0.78, 0.62, 0.50), body_m, head)
    # wide friendly mouth line
    tube([(-0.30, -0.54, 1.46), (0, -0.60, 1.42), (0.30, -0.54, 1.46)],
         [0.020, 0.022, 0.020], dark, head, outline=False, caps=False)
    for sx in (-1, 1):
        ell((sx * 0.50, -0.42, 1.54), (0.12, 0.05, 0.085), blush_m, head, outline=False)
    # eyes sit on top of the head in little domes
    eyes = []
    for sx in (-1, 1):
        ell((sx * 0.34, -0.24, 2.05), (0.27, 0.27, 0.25), body_m, head)
        eyes.append(eye((sx * 0.34, -0.40, 2.10), 0.20, head))
    return dict(root=root, body=body, head=head, wings=wings, eyes=eyes, yaw=12)


def cat():
    """Clem the cat."""
    body_m = mat("#F7A440", 0.48)
    cream = mat("#FFE3C2", 0.5)
    pink = mat("#FF9FB8", 0.5)
    dark = mat("#D9802A", 0.5)

    root = pivot("root", (0, 0, 0))
    body = pivot("body", (0, 0, 0), root)
    ell((0, 0, 0.98), (0.76, 0.70, 0.84), body_m, body)
    ell((0, -0.32, 0.88), (0.50, 0.44, 0.58), cream, body, outline=False)

    wings = []
    for sx in (-1, 1):
        wp = pivot("wing", (sx * 0.66, 0, 1.00), body)
        ell((sx * 0.72, -0.04, 0.92), (0.14, 0.24, 0.36), body_m, wp, rot=(0, 0, sx * 0.2))
        wings.append((wp, sx))

    for sx in (-1, 1):
        ell((sx * 0.30, -0.38, 0.14), (0.24, 0.30, 0.14), cream, body)

    # tail
    tl = pivot("tail", (0.52, 0.42, 0.60), body)
    tube([(0.52, 0.42, 0.60), (0.95, 0.30, 0.95), (1.00, 0.02, 1.35), (0.82, -0.10, 1.55)],
         [0.09, 0.085, 0.08, 0.075], body_m, tl)

    head = pivot("head", (0, -0.05, 1.66), body)
    hc, hr = (0, -0.10, 1.94), (0.70, 0.62, 0.58)
    ell(hc, hr, body_m, head)
    # ears
    ear_rig = []
    for sx in (-1, 1):
        ep = pivot("ear", (sx * 0.42, -0.10, 2.30), head)
        cone((sx * 0.44, -0.10, 2.48), 0.26, 0.52, body_m, ep, rot=(0, sx * 0.30, 0))
        cone((sx * 0.43, -0.17, 2.42), 0.14, 0.30, pink, ep, outline=False, rot=(-0.12, sx * 0.30, 0))
        ear_rig.append((ep, sx))
    # muzzle + nose + whiskers
    for sx in (-1, 1):
        ell((sx * 0.15, -0.62, 1.80), (0.20, 0.13, 0.15), cream, head, outline=False)
    ell((0, -0.72, 1.88), (0.075, 0.05, 0.06), pink, head, outline=False)
    for sx in (-1, 1):
        for dz in (0.05, -0.05):
            tube([(sx * 0.26, -0.68, 1.80 + dz * 0.4), (sx * 0.80, -0.62, 1.80 + dz * 1.5)],
                 [0.011, 0.009], mat("#8A5A32", 0.6), head, outline=False, caps=False)
    for sx in (-1, 1):
        ell((sx * 0.46, -0.44, 1.76), (0.11, 0.05, 0.08), pink, head, outline=False)
    eyes = [eye((sx * 0.29, -0.52, 2.04), 0.20, head) for sx in (-1, 1)]
    return dict(root=root, body=body, head=head, wings=wings, ears=ear_rig, eyes=eyes, yaw=16)


CHARACTERS = {"pip": pip, "duck": duck, "frog": frog, "cat": cat}


# ------------------------------------------------------------------ props
def balloon(loc, hexcolor, parent=None, r=0.30):
    m = mat(hexcolor, 0.25, coat=0.6)
    p = pivot("balloon", loc, parent)
    ell((loc[0], loc[1], loc[2]), (r, r, r * 1.18), m, p)
    ell((loc[0] - r * 0.35, loc[1] - r * 0.4, loc[2] + r * 0.45),
        (r * 0.22, r * 0.18, r * 0.28), mat("#FFFFFF", 0.2, emit=1.0), p, outline=False)
    tube([(loc[0], loc[1], loc[2] - r * 1.1), (loc[0], loc[1], loc[2] - r * 2.4)],
         [0.014, 0.012], mat("#B0B8C4", 0.6), p, caps=False)
    return p


def star(loc, r=0.26, parent=None, hexcolor="#FFD23F"):
    m = mat(hexcolor, 0.35, emit=0.25)
    bpy.ops.mesh.primitive_circle_add(vertices=10, radius=r, fill_type="NGON", location=loc)
    o = bpy.context.active_object
    for i, v in enumerate(o.data.vertices):
        if i % 2:
            v.co *= 0.45
    select_only(o)
    mod = o.modifiers.new("star_thickness", "SOLIDIFY")
    mod.thickness = 0.14
    mod.offset = 0
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return finish_obj(bpy.context.active_object, m, True, parent)


def apple(loc, parent=None, r=0.24):
    p = pivot("apple", loc, parent)
    ell(loc, (r, r, r * 0.95), mat("#E03B3B", 0.35, coat=0.5), p)
    ell((loc[0] - r * 0.35, loc[1] - r * 0.42, loc[2] + r * 0.4),
        (r * 0.2, r * 0.16, r * 0.24), mat("#FFFFFF", 0.2, emit=1.0), p, outline=False)
    tube([(loc[0], loc[1], loc[2] + r * 0.85), (loc[0] + 0.05, loc[1] + 0.02, loc[2] + r * 1.5)],
         [0.03, 0.026], mat("#7A4A20", 0.7), p, caps=False)
    ell((loc[0] + 0.14, loc[1], loc[2] + r * 1.35), (0.13, 0.05, 0.08), mat("#3FA34D", 0.5), p)
    return p


def beach_ball(loc, hexcolor="#F25C54", r=0.34, parent=None):
    m1, m2 = mat(hexcolor, 0.3, coat=0.5), mat("#FFF3E0", 0.3, coat=0.5)
    p = pivot("ball", loc, parent)
    ell(loc, (r, r, r), m1, p)
    for sx in (-1, 1):
        ell((loc[0] + sx * r * 0.55, loc[1] - r * 0.6, loc[2] + r * 0.3),
            (r * 0.34, r * 0.22, r * 0.5), m2, p, outline=False)
    ell((loc[0] - r * 0.4, loc[1] - r * 0.72, loc[2] + r * 0.42),
        (r * 0.14, r * 0.1, r * 0.16), mat("#FFFFFF", 0.2, emit=1.2), p, outline=False)
    return p


def tree(loc, scale=1.0, parent=None):
    trunk = mat("#8A5A32", 0.8)
    leaf = mat("#4FA83D", 0.7)
    leaf2 = mat("#3C8F2E", 0.7)
    ell((loc[0], loc[1], loc[2] + 0.55 * scale), (0.13 * scale, 0.13 * scale, 0.55 * scale), trunk, parent)
    for dx, dy, dz, r in ((0, 0, 1.35, 0.62), (0.34, 0.1, 1.05, 0.44), (-0.32, -0.08, 1.12, 0.40)):
        ell((loc[0] + dx * scale, loc[1] + dy * scale, loc[2] + dz * scale),
            (r * scale, r * scale, r * scale * 0.92), leaf if dz > 1.2 else leaf2, parent)


def hill(loc, r=2.2, hexcolor="#7EC850"):
    ell(loc, (r, r * 0.75, r * 0.62), mat(hexcolor, 0.8))


def sun_disc(loc, r=0.9):
    ell(loc, (r, r * 0.25, r), mat("#FFD23F", 0.4, emit=1.6))


# ------------------------------------------------------------------ scene setup
def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    _mats.clear()


def setup_world(sky="#CFEBFF", ground="#8FD653", back="#EAF6FF"):
    sc = bpy.context.scene
    w = bpy.data.worlds.new("world")
    sc.world = w
    try:
        w.use_nodes = True
    except Exception:
        pass
    bg = next(n for n in w.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = lin(sky)
    bg.inputs["Strength"].default_value = 0.55

    # ground plane
    bpy.ops.mesh.primitive_plane_add(size=42, location=(0, 0, 0))
    g = bpy.context.active_object
    g.name = "GROUND"
    g.data.materials.append(mat(ground, 0.75))

    # flat-emission backdrop: renders as an exact sky colour, immune to exposure
    bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 9.5, 8), rotation=(math.radians(90), 0, 0))
    wall = bpy.context.active_object
    wall.name = "BACKDROP"
    em = bpy.data.materials.new("m_backdrop")
    try:
        em.use_nodes = True
    except Exception:
        pass
    nt = em.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    e = nt.nodes.new("ShaderNodeEmission")
    e.inputs["Color"].default_value = lin(back)
    e.inputs["Strength"].default_value = 1.0
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(e.outputs[0], o.inputs[0])
    wall.data.materials.append(em)

    def sun(name, pos, energy, shadow=True, angle=0.3):
        L = bpy.data.lights.new(name, "SUN")
        L.energy = energy
        L.angle = angle
        L.use_shadow = shadow
        o = bpy.data.objects.new(name, L)
        sc.collection.objects.link(o)
        o.rotation_euler = (-Vector(pos)).to_track_quat("-Z", "Y").to_euler()

    sun("key", (-0.55, -1.0, 1.25), 2.6)
    sun("fill", (1.0, -0.55, 0.15), 0.9, shadow=False)
    sun("rim", (0.35, 1.0, 0.85), 1.5, shadow=False)
    return g


def mesh_points(objs=None):
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    pool = objs or [o for o in bpy.context.scene.objects if o.type == "MESH"]
    for o in pool:
        if o.name in ("GROUND", "BACKDROP"):
            continue
        ev = o.evaluated_get(dg)
        pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    return pts


def fit_camera(pts, fill=0.80, bottom=0.13, tilt=0.20, zoom=1.0):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam")
    cd.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cd)
    sc.collection.objects.link(cam)
    sc.camera = cam
    center = sum(pts, Vector()) / len(pts)
    direction = Vector((0, -1, tilt)).normalized()
    cam.location = center + direction * 40
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()
    inv = cam.matrix_world.inverted()
    cp = [inv @ p for p in pts]
    minx, maxx = min(p.x for p in cp), max(p.x for p in cp)
    miny, maxy = min(p.y for p in cp), max(p.y for p in cp)
    w_, h_ = maxx - minx, maxy - miny
    S = max(h_ / fill, w_ / (fill * RES_X / RES_Y))
    cd.ortho_scale = S / max(0.2, zoom)
    cd.clip_end = 200
    shift = Vector(((minx + maxx) / 2, miny + S * (0.5 - bottom), 0))
    cam.location = cam.matrix_world @ shift
    return cam


def animate_idle(P, frames, blink=True):
    for f in range(1, frames + 1):
        t = (f - 1) / frames
        s = math.sin(2 * math.pi * t)
        s2 = math.sin(4 * math.pi * t)
        P["body"].scale = (1 + 0.012 * s, 1 + 0.012 * s, 1 + 0.026 * s)
        P["body"].keyframe_insert("scale", frame=f)
        P["body"].location = (0, 0, 0.035 * s)
        P["body"].keyframe_insert("location", frame=f)
        P["head"].rotation_euler = (math.radians(2.5 * s2), math.radians(5 * s), 0)
        P["head"].keyframe_insert("rotation_euler", frame=f)
        for wp, sx in P["wings"]:
            wp.rotation_euler = (0, 0, sx * math.radians(7 * s))
            wp.keyframe_insert("rotation_euler", frame=f)
        if blink:
            u = (f - 1) % frames
            b = {int(frames * .62): .7, int(frames * .63): .12, int(frames * .64): .08,
                 int(frames * .65): .14, int(frames * .66): .7}.get(u, 1.0) if frames >= 30 else 1.0
            for e in P["eyes"]:
                e.scale = (1, 1, b)
                e.keyframe_insert("scale", frame=f)


def setup_render(frames, out_path, samples=48, transparent=False):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    import os as _os
    sc.cycles.use_denoising = _os.environ.get("BLENDER_DENOISE", "0") == "1"
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.adaptive_threshold = 0.05
    sc.cycles.max_bounces = 3           # cheap: mostly diffuse + one spec bounce
    sc.cycles.diffuse_bounces = 2
    sc.cycles.glossy_bounces = 2
    sc.cycles.transmission_bounces = 2
    sc.cycles.transparent_max_bounces = 2
    sc.cycles.caustics_reflective = False
    sc.cycles.caustics_refractive = False
    sc.cycles.blur_glossy = 1.5
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = transparent
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.exposure = -0.10
    sc.view_settings.look = "None"
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.fps = 30
    sc.frame_start, sc.frame_end = 1, frames
    sc.render.filepath = str(out_path)


# ------------------------------------------------------------------ scenes
KINDS = ("counting", "colors", "guess_animal", "story")


def build_scene(kind, spec, frames):
    """spec: dict with keys used per kind."""
    clear()
    setup_world(spec.get("sky", "#CFEBFF"), spec.get("ground", "#8FD653"))
    P = pip()
    P["root"].rotation_euler = (0, 0, math.radians(P["yaw"]))

    if kind == "counting":
        n = int(spec.get("count", 3))
        one = spec.get("balloon_color", "#E03B3B")
        for i in range(n):
            a = math.pi * (0.18 + 0.64 * (i + 0.5) / max(1, n))
            x = math.cos(a) * 2.5
            y = 1.2 + math.sin(a) * 0.5
            z = 3.0 + math.sin(a) * 0.55
            balloon((x, y, z), one, None, 0.36)
            # wavy string
            tube([(x, y, z - 0.40), (x + 0.12, y - 0.02, z - 0.95),
                  (x - 0.10, y, z - 1.50), (x + 0.04, y + 0.02, z - 1.95)],
                 [0.016, 0.014, 0.013, 0.012], mat("#C6CDD8", 0.6), None, caps=False)

    elif kind == "colors":
        cols = spec.get("colors") or ["#E03B3B"] * 3
        for i, c in enumerate(cols):
            apple((-2.2 + i * 1.0, 1.2, 0.42), None, 0.26)

    elif kind == "guess_animal":
        if spec.get("animal"):
            pass  # animal built by caller
    elif kind == "story":
        star((-2.4, 1.6, 2.6), 0.30)
        star((2.3, 1.8, 3.0), 0.24)

    animate_idle(P, frames)
    fit_camera(mesh_points())
    return P


# ------------------------------------------------------------------ shot renderer
def apply_pose(P, pose):
    """Static pose so consecutive stills of one scene are not identical."""
    arm = float(pose.get("arm_raise", 0.0))
    tilt = float(pose.get("head_tilt", 0.0))
    lean = float(pose.get("lean", 0.0))
    hop = float(pose.get("hop", 0.0))
    for wp, sx in P["wings"]:
        wp.rotation_euler = (0, math.radians(-arm * 55 * sx), sx * math.radians(6 + arm * 10))
    for ep, sx in P.get("ears", []):
        ep.rotation_euler = (0, 0, sx * math.radians(4 + arm * 12))
    P["head"].rotation_euler = (math.radians(tilt), math.radians(lean * 0.5), math.radians(lean))
    P["body"].location = (0, 0, hop * 0.18)
    P["body"].scale = (1.0 + hop * 0.02, 1.0 + hop * 0.02, 1.0 - hop * 0.03)


def ortho_project(cam, p, w, h):
    """Exact screen position for an orthographic camera. Returns (x, y_from_top) in 0..1.

    Blender's world_to_camera_view applies ortho_scale to the horizontal axis regardless of
    sensor fit, which is wrong for portrait renders - measured 13% error at 864x1536.
    """
    pc = cam.matrix_world.inverted() @ p
    S = cam.data.ortho_scale
    if h >= w:                      # sensor_fit AUTO: ortho_scale spans the larger dimension
        vh, vw = S, S * w / h
    else:
        vw, vh = S, S * h / w
    u = 0.5 + pc.x / vw
    v = 0.5 + pc.y / vh
    return u, 1.0 - v


def char_pts(P):
    """World-space bounds of the character only (used for framing)."""
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in bpy.context.scene.objects:
        if o.type != "MESH" or o.name in ("GROUND", "BACKDROP"):
            continue
        a, is_char = o, False
        while a is not None:
            if a is P["root"]:
                is_char = True
                break
            a = a.parent
        if not is_char:
            continue
        ev = o.evaluated_get(dg)
        pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    return pts or mesh_points()


def build_full_scene(spec):
    """Assemble one shot from a JSON-able spec dict."""
    global RES_X, RES_Y
    if spec.get("res"):
        RES_X, RES_Y = int(spec["res"][0]), int(spec["res"][1])
    ss = float(spec.get("supersample", 1.0))
    if ss > 1.0:
        # render above the delivery resolution; the encoder downsamples with Lanczos.
        # This is what removes jagged edges - far cheaper than a higher sample count.
        RES_X, RES_Y = int(RES_X * ss) // 2 * 2, int(RES_Y * ss) // 2 * 2
    kind = spec.get("kind", "story")
    props = spec.get("props", {})
    clear()
    setup_world(spec.get("sky", "#CFEBFF"), spec.get("ground", "#8FD653"), spec.get("back", "#EAF6FF"))

    # scenery behind the character
    for i in range(int(props.get("hills", 2))):
        hill((-4.2 + i * 4.6, 6.2 + i * 0.9, 0.10), 1.9 + 0.4 * (i % 2),
             "#7EC850" if i % 2 == 0 else "#6FBA45")
    for i in range(int(props.get("trees", 0))):
        tree((-3.3 + i * 2.7, 4.6 + (i % 2) * 1.5, 0.0), 1.25 + 0.2 * (i % 3))
    if props.get("sun"):
        sun_disc((2.15, 6.2, 3.95), 0.52)

    who = spec.get("character", "pip")
    P = CHARACTERS.get(who, pip)()
    P["who"] = who
    P["root"].rotation_euler = (0, 0, math.radians(float(spec.get("yaw", P["yaw"]))))

    # foreground props
    n = int(props.get("balloons", 0))
    if n:
        col = props.get("balloon_color", "#E03B3B")
        for i in range(n):
            a = math.pi * (0.18 + 0.64 * (i + 0.5) / max(1, n))
            x, y = math.cos(a) * 2.45, 1.15 + math.sin(a) * 0.5
            z = 2.95 + math.sin(a) * 0.5
            balloon((x, y, z), col, None, 0.34)
            tube([(x, y, z - 0.38), (x + 0.11, y - 0.02, z - 0.92),
                  (x - 0.09, y, z - 1.46), (x + 0.03, y + 0.02, z - 1.9)],
                 [0.015, 0.013, 0.012, 0.011], mat("#C6CDD8", 0.6), None, caps=False)
    for i in range(int(props.get("apples", 0))):
        apple((-0.95 + i * 0.95, -0.95, 0.38), None, 0.30)
    for i in range(int(props.get("stars", 0))):
        star((-1.15 + i * 1.15, 0.75, 2.65 + (i % 2) * 0.55), 0.30)
    if props.get("ball_color"):
        beach_ball((1.45, 1.15, 0.52), props["ball_color"], 0.38)
    if props.get("fish"):
        for i in range(int(props["fish"])):
            ell((-1.9 + i * 1.3, 1.2, 0.35 + i * 0.12), (0.22, 0.13, 0.15),
                mat("#4FA8D8", 0.3, coat=0.4), None)

    apply_pose(P, spec.get("pose", {}))
    bpy.context.view_layer.update()

    # a bush in front hides most of the character for "guess the animal" clue shots
    if props.get("bush"):
        ell((0, -1.55, 0.62), (1.30, 0.55, 0.95), mat("#4FA83D", 0.7))
        ell((-0.9, -1.45, 0.42), (0.75, 0.45, 0.62), mat("#3C8F2E", 0.7))
        ell((0.95, -1.42, 0.45), (0.70, 0.42, 0.58), mat("#3C8F2E", 0.7))

    cam = spec.get("cam", {})
    camera = fit_camera(char_pts(P),
               fill=float(cam.get("fill", 0.44)),
               bottom=float(cam.get("bottom", 0.155)),
               tilt=float(cam.get("tilt", 0.22)),
               zoom=float(cam.get("zoom", 1.0)))

    # ---- project the mouth anchor into image space for the 2D lip-sync layer
    bpy.context.view_layer.update()
    ANCH = {"pip": (Vector((0, -0.64, 1.875)), 0.062),
            "duck": (Vector((0, -0.70, 1.812)), 0.078),
            "frog": (Vector((0, -0.60, 1.442)), 0.140),
            "cat": (Vector((0, -0.70, 1.765)), 0.070)}
    alocal, mw = ANCH.get(who, ANCH["pip"])
    anchor = P["head"].matrix_world @ (alocal - P["head"].location)
    u, v = ortho_project(camera, anchor, RES_X, RES_Y)
    info = {"beak_x": round(u, 4), "beak_y": round(v, 4), "mouth_w": mw, "who": who}
    Path(spec["out"] + ".json").write_text(json.dumps(info))
    return P


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="JSON file: {samples:int, shots:[spec,...]}")
    ap.add_argument("--test-still")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--samples", type=int, default=12)
    a = ap.parse_args(argv)

    if a.test_still:
        build_scene("counting", {"count": a.count}, 24)
        setup_render(1, "", samples=a.samples)
        bpy.context.scene.render.filepath = a.test_still
        bpy.ops.render.render(write_still=True)
        print("WROTE", a.test_still)
        return

    import json
    spec = json.load(open(a.shots))
    samples = int(spec.get("samples", a.samples))
    for i, shot in enumerate(spec["shots"]):
        out = Path(shot["out"]); out.parent.mkdir(parents=True, exist_ok=True)
        build_full_scene(shot)
        setup_render(1, "", samples=samples)
        bpy.context.scene.render.filepath = str(out)
        bpy.ops.render.render(write_still=True)
        print("SHOT %d/%d -> %s" % (i + 1, len(spec["shots"]), out))
    print("ALL SHOTS DONE")


if __name__ == "__main__":
    main()
