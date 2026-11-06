"""Builds the demo scene (tower + camera + lights) and optionally renders previews.

Run from a shell:

    blender -b --factory-startup --python cyber_tower/tools/make_demo.py -- \
        blend=cyber_tower/cyber_tower_demo.blend renders=cyber_tower/images \
        seeds=9 samples=64 res=60

Arguments (all optional, key=value after "--"):
    blend    path of the .blend to save
    renders  directory for preview PNGs (omit to skip rendering)
    seeds    comma separated seeds; the first one is kept in the .blend (default 9)
    views    comma separated camera angles in degrees (default -32,28,152)
    samples  Cycles samples (default 64)
    res      resolution percentage of 1080x1920 (default 60)
"""

import importlib.util
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(os.path.dirname(HERE), "cyber_tower_generator.py")


def load_addon():
    spec = importlib.util.spec_from_file_location("cyber_tower_generator", ADDON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return dict(a.split("=", 1) for a in argv)


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def setup_scene(top):
    sc = bpy.context.scene
    # ground
    me = bpy.data.meshes.new("Ground")
    s = 600.0
    me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
    ground = bpy.data.objects.new("Ground", me)
    ground.location.z = -0.3
    sc.collection.objects.link(ground)
    gm = bpy.data.materials.new("Ground")
    if hasattr(gm, "use_nodes") and not gm.use_nodes:
        gm.use_nodes = True
    b = gm.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.5, 0.51, 0.52, 1)
    b.inputs["Roughness"].default_value = 0.85
    me.materials.append(gm)
    # world: soft studio grey
    w = bpy.data.worlds.new("Studio")
    sc.world = w
    if not w.node_tree:
        w.use_nodes = True
    nt = w.node_tree
    bg = nt.nodes["Background"]
    bg.inputs[1].default_value = 1.0
    grad = nt.nodes.new("ShaderNodeTexGradient")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    nt.links.new(tc.outputs["Generated"], sep.inputs[0])
    nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
    ramp.color_ramp.elements[0].position = 0.45
    ramp.color_ramp.elements[0].color = (0.55, 0.57, 0.6, 1)
    ramp.color_ramp.elements[1].position = 0.75
    ramp.color_ramp.elements[1].color = (0.82, 0.84, 0.87, 1)
    nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    del grad
    # key light
    sun = bpy.data.lights.new("Key", 'SUN')
    sun.energy = 2.6
    sun.angle = math.radians(6)
    so = bpy.data.objects.new("Key", sun)
    so.rotation_euler = (math.radians(52), 0, math.radians(-28))
    sc.collection.objects.link(so)
    # camera
    cam = bpy.data.cameras.new("Camera")
    cam.lens = 32
    co = bpy.data.objects.new("Camera", cam)
    sc.collection.objects.link(co)
    sc.camera = co
    # render
    sc.render.engine = 'CYCLES'
    sc.cycles.device = 'CPU'
    sc.cycles.use_denoising = True
    sc.render.resolution_x = 1080
    sc.render.resolution_y = 1920
    sc.view_settings.view_transform = 'AgX'
    try:
        sc.view_settings.look = 'AgX - Medium High Contrast'
    except TypeError:
        pass
    add_glare(sc)
    return co


def add_glare(sc):
    """Fog glow for the neon. Compositor API differs between 4.x and 5.x."""
    if hasattr(sc.render, "compositor_device"):
        sc.render.compositor_device = 'CPU'
    try:
        if hasattr(sc, "compositing_node_group"):          # Blender 5.x
            ng = bpy.data.node_groups.new("Glow", 'CompositorNodeTree')
            ng.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
            rl = ng.nodes.new('CompositorNodeRLayers')
            go = ng.nodes.new('NodeGroupOutput')
            gl = ng.nodes.new('CompositorNodeGlare')
            ng.links.new(rl.outputs[0], gl.inputs[0])
            ng.links.new(gl.outputs[0], go.inputs[0])
            _glare_settings(gl)
            sc.compositing_node_group = ng
        else:                                               # Blender 4.x
            sc.use_nodes = True
            nt = sc.node_tree
            rl = nt.nodes.get("Render Layers") or nt.nodes.new('CompositorNodeRLayers')
            comp = nt.nodes.get("Composite") or nt.nodes.new('CompositorNodeComposite')
            gl = nt.nodes.new('CompositorNodeGlare')
            nt.links.new(rl.outputs[0], gl.inputs[0])
            nt.links.new(gl.outputs[0], comp.inputs[0])
            _glare_settings(gl)
    except Exception as exc:  # glare is cosmetic only
        print("glare skipped:", exc)


def _glare_settings(gl):
    def put(prop, sock, value):
        if hasattr(gl, prop):
            try:
                setattr(gl, prop, value)
                return
            except Exception:
                pass
        for s in gl.inputs:
            if s.name == sock:
                s.default_value = value
                return
    put("glare_type", "Type", 'FOG_GLOW' if hasattr(gl, "glare_type") else "Fog Glow")
    put("threshold", "Threshold", 1.6)
    put("size", "Size", 7 if hasattr(gl, "size") else 0.55)
    put("mix", "Strength", 0.0 if hasattr(gl, "mix") else 0.6)


def aim(cam, angle_deg, dist, height, target):
    a = math.radians(angle_deg)
    cam.location = Vector((math.sin(a) * dist, -math.cos(a) * dist, height))
    cam.rotation_euler = (target - cam.location).to_track_quat('-Z', 'Y').to_euler()


def main():
    o = args()
    ct = load_addon()
    ct.register()
    clear_scene()
    seeds = [int(s) for s in o.get("seeds", "9").split(",")]
    views = [float(v) for v in o.get("views", "-32,28,152").split(",")]
    sc = bpy.context.scene
    tower = ct.add_tower(bpy.context, seed=seeds[0], name="CyberTower")
    for kv in [e for e in o.get("set", "").split(";") if e]:
        k, v = kv.split(":", 1)
        cur = ct.get_input(tower.modifiers[0], k)
        ct.set_input(tower.modifiers[0], k, v if isinstance(cur, str) else type(cur)(eval(v)))
    cam = setup_scene(45.0)
    sc.cycles.samples = int(o.get("samples", 64))
    sc.render.resolution_percentage = int(o.get("res", 60))
    out_dir = o.get("renders")
    target = Vector((0, 0, float(o.get("tz", 19.0))))
    dist = float(o.get("dist", 60.0))
    camz = float(o.get("cz", 3.0))
    aim(cam, views[0], dist, camz, target)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        mod = tower.modifiers[0]
        for sd in seeds:
            ct.set_input(mod, "Seed", sd)
            for k, a in enumerate(views):
                aim(cam, a, dist, camz, target)
                sc.render.filepath = os.path.join(out_dir, "tower_seed%d_view%d.png" % (sd, k))
                bpy.ops.render.render(write_still=True)
        ct.set_input(mod, "Seed", seeds[0])
        aim(cam, views[0], dist, camz, target)
    blend = o.get("blend")
    if blend:
        sc.render.resolution_percentage = 100
        sc.cycles.samples = 128
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend), compress=True)


main()
