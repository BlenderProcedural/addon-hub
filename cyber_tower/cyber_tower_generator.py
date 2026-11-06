bl_info = {
    "name": "Cyber Tower Generator",
    "author": "blenderprocedural",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Add > Mesh > Cyber Tower",
    "description": "Procedural Geometry Nodes generator for detailed steel-frame "
                   "brutalist / cyberpunk towers (louvers, fins, bracing, pods, "
                   "neon sign band, rooftop mast, street props)",
    "category": "Add Mesh",
}

"""
Cyber Tower Generator
=====================

Everything in the tower is built by Geometry Nodes. This script only *writes*
the node groups (and the materials they use); after that the generator lives
entirely inside the .blend file and is driven from the modifier panel.

Node groups (all prefixed "CT "):
    Cyber Tower            main modifier group (all parameters)
    CT Panel               dispatches one facade panel to its generator
    CT Facade *            facade generators (louver, fins, concrete, vents, annex)
    CT Ground *            ground floor generators (shop, buttress, loading)
    CT Level *             open levels (neon sign level, roof level)
    CT Bracing / Pods      overlays
    CT Frame / Roof / Street   global structure, rooftop and surroundings
    CT Box / Beams / ...   small reusable building blocks

The tower is a stack of rows (ground, N blocks, optional sign level, optional
open roof level). Every row is split into six facade segments (two masses on
the front and back, one on each side) and every segment gets a facade type
from a weighted random pick. A For Each Element zone builds each panel in its
own local space and places it on the building.
"""

import math

import bpy

GROUP_VERSION = 1
MAIN_GROUP = "Cyber Tower"

# ---------------------------------------------------------------------------
#  Tiny node-building DSL
# ---------------------------------------------------------------------------

_TREES = []
MAT = {}
GR = {}


def _tree():
    return _TREES[-1]


def _ins(n):
    return [s for s in n.inputs if s.enabled and s.type != 'CUSTOM']


def _outs(n):
    return [s for s in n.outputs if s.enabled and s.type != 'CUSTOM']


def _find(socks, key):
    if isinstance(key, int):
        return socks[key]
    for s in socks:
        if s.name == key:
            return s
    for s in socks:
        if s.identifier == key:
            return s
    raise KeyError("socket %r not in %s" % (key, [s.name for s in socks]))


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class S:
    """Wraps a node output socket; supports arithmetic to build math nodes."""

    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s

    def __getitem__(self, key):
        return S(_find(_outs(self.s.node), key))

    @property
    def node(self):
        return self.s.node

    def isvec(self):
        return self.s.type == 'VECTOR'

    def __add__(self, o): return _arith('ADD', self, o)
    def __radd__(self, o): return _arith('ADD', o, self)
    def __sub__(self, o): return _arith('SUBTRACT', self, o)
    def __rsub__(self, o): return _arith('SUBTRACT', o, self)
    def __mul__(self, o): return _arith('MULTIPLY', self, o)
    def __rmul__(self, o): return _arith('MULTIPLY', o, self)
    def __truediv__(self, o): return _arith('DIVIDE', self, o)
    def __rtruediv__(self, o): return _arith('DIVIDE', o, self)
    def __neg__(self): return _arith('MULTIPLY', self, -1.0)
    def __mod__(self, o): return fm('FLOORED_MODULO', self, o)
    def __gt__(self, o): return cmp(self, o, 'GREATER_THAN')
    def __lt__(self, o): return cmp(self, o, 'LESS_THAN')
    def __ge__(self, o): return cmp(self, o, 'GREATER_EQUAL')
    def __le__(self, o): return cmp(self, o, 'LESS_EQUAL')
    def __and__(self, o): return node('FunctionNodeBooleanMath', self, o, operation='AND')
    def __or__(self, o): return node('FunctionNodeBooleanMath', self, o, operation='OR')
    def __invert__(self): return node('FunctionNodeBooleanMath', self, operation='NOT')
    __hash__ = object.__hash__


def _isvec(v):
    if isinstance(v, S):
        return v.isvec()
    return isinstance(v, (tuple, list))


def _arith(op, a, b):
    if _isnum(a) and _isnum(b):
        return {'ADD': a + b, 'SUBTRACT': a - b, 'MULTIPLY': a * b,
                'DIVIDE': a / b}[op]
    if _isvec(a) or _isvec(b):
        if op == 'MULTIPLY':
            if not _isvec(a):
                a, b = b, a
            if not _isvec(b):
                return node('ShaderNodeVectorMath', a, b, operation='SCALE')
        if op == 'DIVIDE' and not _isvec(b):
            inv = 1.0 / b if _isnum(b) else fm('DIVIDE', 1.0, b)
            return node('ShaderNodeVectorMath', a, inv, operation='SCALE')
        return node('ShaderNodeVectorMath', a, b, operation=op)
    return node('ShaderNodeMath', a, b, operation=op)


def _link(out_sock, in_sock):
    _tree().links.new(out_sock, in_sock)


def _set(sock, v):
    if v is None:
        return
    if isinstance(v, S):
        _link(v.s, sock)
        return
    if isinstance(v, bpy.types.NodeSocket):
        _link(v, sock)
        return
    if isinstance(v, (tuple, list)) and any(isinstance(c, S) for c in v):
        _link(vec(*v).s, sock)
        return
    if sock.type in ('VECTOR', 'ROTATION') and _isnum(v):
        v = (v, v, v)
    elif sock.type == 'INT':
        v = int(round(v))
    elif sock.type == 'BOOLEAN':
        v = bool(v)
    elif sock.type == 'VALUE':
        v = float(v)
    sock.default_value = v


def _prop(n, k, v):
    if hasattr(n, k):
        setattr(n, k, v)
        return
    # Blender 5.x turned some enum properties into menu input sockets.
    name = k.replace('_', ' ').title()
    for s in n.inputs:
        if s.name == name:
            s.default_value = v
            return
    raise AttributeError("%s has no property/menu %s" % (n.bl_idname, k))


def node(idname, *args, **props):
    """Create a node. Positional args feed the enabled inputs in order, a dict
    feeds inputs by name, keyword args set node properties."""
    n = _tree().nodes.new(idname)
    for k, v in props.items():
        _prop(n, k, v)
    smap, pos = {}, []
    for a in args:
        if isinstance(a, dict):
            smap.update(a)
        else:
            pos.append(a)
    if pos:
        ins = _ins(n)
        for i, v in enumerate(pos):
            _set(ins[i], v)
    for k, v in smap.items():
        if v is not None:
            _set(_find(_ins(n), k), v)
    outs = _outs(n)
    return S(outs[0]) if outs else n


def call(group, smap=None):
    n = _tree().nodes.new('GeometryNodeGroup')
    n.node_tree = group
    n.label = group.name.replace("CT ", "")
    for k, v in (smap or {}).items():
        if v is not None:
            _set(_find(_ins(n), k), v)
    return S(_outs(n)[0])


_SOCK = {
    'GEO': 'NodeSocketGeometry', 'FLOAT': 'NodeSocketFloat',
    'INT': 'NodeSocketInt', 'BOOL': 'NodeSocketBool',
    'VEC': 'NodeSocketVector', 'STR': 'NodeSocketString',
    'MAT': 'NodeSocketMaterial', 'ROT': 'NodeSocketRotation',
}


class Group:
    """Context manager that creates a geometry node group.

    inputs: list of (name, type, default, min, max, description) tuples or
    "#Panel" strings that open an interface panel."""

    def __init__(self, name, inputs=(), outputs=(("Geometry", 'GEO'),),
                 modifier=False, description=""):
        old = bpy.data.node_groups.get(name)
        if old is not None:
            bpy.data.node_groups.remove(old)
        t = bpy.data.node_groups.new(name, 'GeometryNodeTree')
        t.description = description
        if modifier and hasattr(t, "is_modifier"):
            t.is_modifier = True
        panel = None
        for spec in inputs:
            if isinstance(spec, str):
                panel = t.interface.new_panel(spec.lstrip('#'),
                                              default_closed=spec.startswith('##'))
                continue
            self._socket(t, spec, 'INPUT', panel)
        for spec in outputs:
            self._socket(t, spec, 'OUTPUT', None)
        self.tree = t
        self.gi = t.nodes.new('NodeGroupInput')
        self.go = t.nodes.new('NodeGroupOutput')

    @staticmethod
    def _socket(t, spec, io, panel):
        name, typ = spec[0], spec[1]
        item = t.interface.new_socket(name, in_out=io,
                                      socket_type=_SOCK[typ], parent=panel)
        if len(spec) > 2 and spec[2] is not None:
            item.default_value = spec[2]
        if len(spec) > 4 and spec[3] is not None:
            item.min_value, item.max_value = spec[3], spec[4]
        if len(spec) > 5:
            item.description = spec[5]
        return item

    def __enter__(self):
        _TREES.append(self.tree)
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            layout(self.tree)
        _TREES.pop()
        return False

    def __getitem__(self, name):
        return S(self.gi.outputs[name])

    def out(self, *vals, **named):
        for i, v in enumerate(vals):
            _set(self.go.inputs[i], v)
        for k, v in named.items():
            _set(self.go.inputs[k], v)


def layout(tree):
    """Simple layered layout so the generated trees are readable."""
    nodes = list(tree.nodes)
    incoming = {n: set() for n in nodes}
    outgoing = {n: set() for n in nodes}
    for l in tree.links:
        incoming[l.to_node].add(l.from_node)
        outgoing[l.from_node].add(l.to_node)
    # zone pairs keep a fixed relation
    depth = {n: 0 for n in nodes}
    indeg = {n: len(incoming[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    order = []
    while queue:
        n = queue.pop()
        order.append(n)
        for m in outgoing[n]:
            depth[m] = max(depth[m], depth[n] + 1)
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    cols = {}
    for n in order:
        cols.setdefault(depth[n], []).append(n)
    for c, ns in cols.items():
        y = 0.0
        for n in ns:
            n.location = (c * 240.0, y)
            nsock = len(_ins(n)) + len(_outs(n))
            y -= 60 + 22 * nsock + (40 if len(n.bl_rna.properties) > 60 else 0)


# --- convenience wrappers ----------------------------------------------------

def vec(x, y, z):
    if _isnum(x) and _isnum(y) and _isnum(z):
        return (x, y, z)
    return node('ShaderNodeCombineXYZ', x, y, z)


def sep(v):
    n = node('ShaderNodeSeparateXYZ', v)
    return n, n['Y'], n['Z']


def fm(op, *a):
    return node('ShaderNodeMath', *a, operation=op)


def vm(op, *a):
    return node('ShaderNodeVectorMath', *a, operation=op)


def flo(a): return fm('FLOOR', a)
def ceil_(a): return fm('CEIL', a)
def rint(a): return fm('ROUND', a)
def fmax(a, b): return fm('MAXIMUM', a, b)
def fmin(a, b): return fm('MINIMUM', a, b)
def fabs(a): return fm('ABSOLUTE', a)
def clamp(a, lo, hi): return fmin(fmax(a, lo), hi)


def cmp(a, b, op='GREATER_THAN', dt='FLOAT'):
    return node('FunctionNodeCompare', a, b, data_type=dt, operation=op)


def ieq(a, b):
    return node('FunctionNodeCompare', a, b, data_type='INT', operation='EQUAL')


def switch(c, f, t, typ='GEOMETRY'):
    return node('GeometryNodeSwitch', {'Switch': c, 'False': f, 'True': t},
                input_type=typ)


def iswitch(i, items, dt='GEOMETRY'):
    n = _tree().nodes.new('GeometryNodeIndexSwitch')
    n.data_type = dt
    while len(n.index_switch_items) < len(items):
        n.index_switch_items.new()
    _set(n.inputs[0], i)
    for k, it in enumerate(items):
        _set(n.inputs[k + 1], it)
    return S(n.outputs[0])


def const_int(v):
    n = _tree().nodes.new('FunctionNodeInputInt')
    n.integer = int(v)
    return S(n.outputs[0])


def _rid(id):
    # An unlinked ID socket means "index" (a field). A constant ID must be
    # linked from a value node so the result stays a single value.
    return const_int(id) if _isnum(id) else id


def rnd(lo, hi, id=None, seed=0):
    return node('FunctionNodeRandomValue',
                {'Min': lo, 'Max': hi, 'ID': _rid(id), 'Seed': seed}, data_type='FLOAT')


# Integer and boolean variants are derived from the FLOAT random value on
# purpose: Blender 5.x changed the INT / BOOLEAN hashing of the Random Value
# node, the float one is identical in 4.x and 5.x, so a seed builds the same
# tower in every supported version. The seed offsets keep the streams apart.

def rndi(lo, hi, id=None, seed=0):
    f = rnd(0.0, 1.0, id, seed + 7919)
    return fmin(flo(lo + f * (hi - lo + 1)), hi)


def chance(p, id=None, seed=0):
    return rnd(0.0, 1.0, id, seed + 104729) < p


def index():
    return node('GeometryNodeInputIndex')


def position():
    return node('GeometryNodeInputPosition')


def cube(size):
    return node('GeometryNodeMeshCube', {'Size': size})


def cyl(r, depth, verts=12):
    return node('GeometryNodeMeshCylinder',
                {'Vertices': verts, 'Radius': r, 'Depth': depth})


def xform(g, t=None, r=None, s=None):
    return node('GeometryNodeTransform',
                {'Geometry': g, 'Translation': t, 'Rotation': r, 'Scale': s})


def join(*gs):
    n = _tree().nodes.new('GeometryNodeJoinGeometry')
    for g in gs:
        if g is not None:
            _link(g.s, n.inputs[0])
    return S(n.outputs[0])


def _matval(m):
    return MAT[m] if isinstance(m, str) else m


def setmat(g, m, sel=None):
    return node('GeometryNodeSetMaterial',
                {'Geometry': g, 'Selection': sel, 'Material': _matval(m)})


def box(size, center=(0, 0, 0), mat='Concrete'):
    return call(GR['box'], {'Size': size, 'Center': center,
                            'Material': _matval(mat)})


def inst(pts, ins, rot=None, scale=None, sel=None):
    return node('GeometryNodeInstanceOnPoints',
                {'Points': pts, 'Selection': sel, 'Instance': ins,
                 'Rotation': rot, 'Scale': scale})


def realize(g):
    return node('GeometryNodeRealizeInstances', {'Geometry': g})


def points(count, pos=None):
    return node('GeometryNodePoints', {'Count': count, 'Position': pos})


def mline(count, start=(0, 0, 0), offset=(0, 0, 1)):
    return node('GeometryNodeMeshLine',
                {'Count': count, 'Start Location': start, 'Offset': offset},
                mode='OFFSET')


def seg(a, b):
    """Single edge from a to b."""
    n = _tree().nodes.new('GeometryNodeMeshLine')
    n.mode = 'END_POINTS'
    ins = _ins(n)
    _set(ins[0], 2)
    _set(ins[1], a)
    _set(ins[2], b)
    return S(n.outputs[0])


def store(g, name, v, dt='FLOAT', domain='POINT', sel=None):
    return node('GeometryNodeStoreNamedAttribute',
                {'Geometry': g, 'Selection': sel, 'Name': name, 'Value': v},
                data_type=dt, domain=domain)


def named(name, dt='FLOAT'):
    return node('GeometryNodeInputNamedAttribute', {'Name': name}, data_type=dt)


def delete(g, sel, domain='POINT'):
    return node('GeometryNodeDeleteGeometry', {'Geometry': g, 'Selection': sel},
                domain=domain)


def setpos(g, sel=None, p=None, off=None):
    return node('GeometryNodeSetPosition',
                {'Geometry': g, 'Selection': sel, 'Position': p, 'Offset': off})


def capture(g, fields):
    """Capture fields anonymously. fields: list of (name, socket_type, field).
    Returns (geometry, {name: captured field})."""
    n = _tree().nodes.new('GeometryNodeCaptureAttribute')
    for name, dt, _ in fields:
        n.capture_items.new(dt, name)
    _set(n.inputs['Geometry'], g)
    for name, _, f in fields:
        _set(n.inputs[name], f)
    return S(n.outputs['Geometry']), {name: S(n.outputs[name]) for name, _, _ in fields}


def remove_attrs(g, names):
    for nm in names:
        g = node('GeometryNodeRemoveAttribute', {'Geometry': g, 'Name': nm})
    return g


def align_x(v):
    return node('FunctionNodeAlignRotationToVector', {'Vector': v},
                axis='X', pivot_axis='AUTO')


def beams(mesh, width, depth, mat='Steel', boxed=False):
    return call(GR['beams'], {'Mesh': mesh, 'Width': width, 'Depth': depth,
                              'Box Profile': boxed, 'Material': _matval(mat)})


def grid_fields(nx):
    """ix / iy fields for a point cloud laid out as rows of nx points."""
    i = index()
    return fm('FLOORED_MODULO', i, nx), flo(i / nx)


PI = math.pi


# ---------------------------------------------------------------------------
#  Materials
# ---------------------------------------------------------------------------

def _new_mat(name):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (600, 0)
    return m, nt, out


def _bsdf(nt, color, rough=0.5, metal=0.0, emit=None, strength=0.0):
    b = nt.nodes.new('ShaderNodeBsdfPrincipled')
    b.location = (300, 0)
    b.inputs['Base Color'].default_value = (*color, 1.0)
    b.inputs['Roughness'].default_value = rough
    b.inputs['Metallic'].default_value = metal
    if emit is not None:
        b.inputs['Emission Color'].default_value = (*emit, 1.0)
        b.inputs['Emission Strength'].default_value = strength
    return b


def _texcoord(nt):
    tc = nt.nodes.new('ShaderNodeTexCoord')
    tc.location = (-900, 0)
    return tc.outputs['Object']


def _attr(nt, name):
    a = nt.nodes.new('ShaderNodeAttribute')
    a.attribute_name = name
    a.location = (-900, -300)
    return a.outputs['Fac']


def _simple(name, color, rough=0.5, metal=0.0, emit=None, strength=0.0):
    m, nt, out = _new_mat(name)
    b = _bsdf(nt, color, rough, metal, emit, strength)
    nt.links.new(b.outputs[0], out.inputs[0])
    return m


def _concrete(name, c_lo, c_hi, board=True, var=0.18):
    m, nt, out = _new_mat(name)
    L = nt.links
    co = _texcoord(nt)
    b = _bsdf(nt, c_hi, 0.88)
    noise = nt.nodes.new('ShaderNodeTexNoise')
    noise.inputs['Scale'].default_value = 0.7
    noise.inputs['Detail'].default_value = 8.0
    L.new(co, noise.inputs['Vector'])
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].position = 0.3
    ramp.color_ramp.elements[0].color = (*c_lo, 1)
    ramp.color_ramp.elements[1].position = 0.75
    ramp.color_ramp.elements[1].color = (*c_hi, 1)
    L.new(noise.outputs['Fac'], ramp.inputs['Fac'])
    col = ramp.outputs['Color']
    if board:
        # faint board-formed horizontal lines
        sep_ = nt.nodes.new('ShaderNodeSeparateXYZ')
        L.new(co, sep_.inputs[0])
        wave = nt.nodes.new('ShaderNodeTexWave')
        wave.wave_type = 'BANDS'
        wave.bands_direction = 'Z'
        wave.inputs['Scale'].default_value = 0.55
        wave.inputs['Distortion'].default_value = 1.5
        wave.inputs['Detail Scale'].default_value = 3.0
        L.new(co, wave.inputs['Vector'])
        wr = nt.nodes.new('ShaderNodeValToRGB')
        wr.color_ramp.elements[0].position = 0.0
        wr.color_ramp.elements[0].color = (0.82, 0.82, 0.82, 1)
        wr.color_ramp.elements[1].position = 0.18
        wr.color_ramp.elements[1].color = (1, 1, 1, 1)
        L.new(wave.outputs['Fac'], wr.inputs['Fac'])
        mix = nt.nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        mix.blend_type = 'MULTIPLY'
        mix.inputs['Factor'].default_value = 1.0
        L.new(col, mix.inputs['A'])
        L.new(wr.outputs['Color'], mix.inputs['B'])
        col = mix.outputs['Result']
    # rain streaks: noise stretched along Z
    mp = nt.nodes.new('ShaderNodeMapping')
    mp.inputs['Scale'].default_value = (1.6, 1.6, 0.12)
    L.new(co, mp.inputs['Vector'])
    st = nt.nodes.new('ShaderNodeTexNoise')
    st.inputs['Scale'].default_value = 3.0
    st.inputs['Detail'].default_value = 4.0
    L.new(mp.outputs['Vector'], st.inputs['Vector'])
    sr = nt.nodes.new('ShaderNodeValToRGB')
    sr.color_ramp.elements[0].position = 0.35
    sr.color_ramp.elements[0].color = (0.72, 0.72, 0.72, 1)
    sr.color_ramp.elements[1].position = 0.6
    sr.color_ramp.elements[1].color = (1, 1, 1, 1)
    L.new(st.outputs['Fac'], sr.inputs['Fac'])
    mixs = nt.nodes.new('ShaderNodeMix')
    mixs.data_type = 'RGBA'
    mixs.blend_type = 'MULTIPLY'
    mixs.inputs['Factor'].default_value = 1.0
    L.new(col, mixs.inputs['A'])
    L.new(sr.outputs['Color'], mixs.inputs['B'])
    col = mixs.outputs['Result']
    # per panel brightness variation from the ct_rand attribute
    rmap = nt.nodes.new('ShaderNodeMapRange')
    rmap.inputs['To Min'].default_value = 1.0 - var
    rmap.inputs['To Max'].default_value = 1.0 + var * 0.6
    L.new(_attr(nt, 'ct_rand'), rmap.inputs['Value'])
    mix2 = nt.nodes.new('ShaderNodeMix')
    mix2.data_type = 'RGBA'
    mix2.blend_type = 'MULTIPLY'
    mix2.inputs['Factor'].default_value = 1.0
    L.new(col, mix2.inputs['A'])
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    for i in range(3):
        L.new(rmap.outputs['Result'], comb.inputs[i])
    L.new(comb.outputs[0], mix2.inputs['B'])
    L.new(mix2.outputs['Result'], b.inputs['Base Color'])
    # bump
    fine = nt.nodes.new('ShaderNodeTexNoise')
    fine.inputs['Scale'].default_value = 18.0
    fine.inputs['Detail'].default_value = 6.0
    L.new(co, fine.inputs['Vector'])
    bump = nt.nodes.new('ShaderNodeBump')
    bump.inputs['Strength'].default_value = 0.12
    L.new(fine.outputs['Fac'], bump.inputs['Height'])
    L.new(bump.outputs['Normal'], b.inputs['Normal'])
    L.new(b.outputs[0], out.inputs[0])
    return m


def _glass(name):
    m, nt, out = _new_mat(name)
    L = nt.links
    co = _texcoord(nt)
    b = _bsdf(nt, (0.05, 0.04, 0.05), 0.04, 0.55)
    noise = nt.nodes.new('ShaderNodeTexNoise')
    noise.inputs['Scale'].default_value = 0.25
    L.new(co, noise.inputs['Vector'])
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].color = (0.02, 0.018, 0.026, 1)
    ramp.color_ramp.elements[1].color = (0.11, 0.06, 0.065, 1)
    L.new(noise.outputs['Fac'], ramp.inputs['Fac'])
    L.new(ramp.outputs['Color'], b.inputs['Base Color'])
    L.new(b.outputs[0], out.inputs[0])
    return m


def _core(name):
    """Dark curtain wall with scattered lit windows."""
    m, nt, out = _new_mat(name)
    L = nt.links
    co = _texcoord(nt)
    b = _bsdf(nt, (0.02, 0.022, 0.026), 0.12, 0.3)
    brick = nt.nodes.new('ShaderNodeTexBrick')
    brick.inputs['Scale'].default_value = 0.35
    brick.inputs['Mortar Size'].default_value = 0.04
    brick.inputs['Color1'].default_value = (0, 0, 0, 1)
    brick.inputs['Color2'].default_value = (1, 1, 1, 1)
    brick.offset = 0.0
    L.new(co, brick.inputs['Vector'])
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.interpolation = 'CONSTANT'
    ramp.color_ramp.elements[0].color = (0, 0, 0, 1)
    ramp.color_ramp.elements[1].position = 0.78
    ramp.color_ramp.elements[1].color = (1.0, 0.86, 0.66, 1)
    L.new(brick.outputs['Color'], ramp.inputs['Fac'])
    L.new(ramp.outputs['Color'], b.inputs['Emission Color'])
    b.inputs['Emission Strength'].default_value = 0.9
    L.new(b.outputs[0], out.inputs[0])
    return m


def _screen(name):
    m, nt, out = _new_mat(name)
    L = nt.links
    co = _texcoord(nt)
    b = _bsdf(nt, (0.02, 0.02, 0.02), 0.2)
    mag = nt.nodes.new('ShaderNodeTexMagic')
    mag.inputs['Scale'].default_value = 1.6
    mag.turbulence_depth = 3
    L.new(co, mag.inputs['Vector'])
    noise = nt.nodes.new('ShaderNodeTexNoise')
    noise.inputs['Scale'].default_value = 2.5
    L.new(co, noise.inputs['Vector'])
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    els = ramp.color_ramp.elements
    els[0].color = (0.02, 0.08, 0.6, 1)
    els[1].color = (0.8, 0.05, 0.45, 1)
    e = els.new(0.5)
    e.color = (0.05, 0.7, 0.75, 1)
    L.new(noise.outputs['Fac'], ramp.inputs['Fac'])
    mix = nt.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.blend_type = 'SCREEN'
    mix.inputs['Factor'].default_value = 0.3
    L.new(ramp.outputs['Color'], mix.inputs['A'])
    L.new(mag.outputs['Color'], mix.inputs['B'])
    L.new(mix.outputs['Result'], b.inputs['Emission Color'])
    b.inputs['Emission Strength'].default_value = 1.1
    L.new(b.outputs[0], out.inputs[0])
    return m


def _vending(name):
    m, nt, out = _new_mat(name)
    L = nt.links
    b = _bsdf(nt, (0.05, 0.05, 0.05), 0.2)
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.interpolation = 'CONSTANT'
    els = ramp.color_ramp.elements
    els[0].color = (0.9, 0.12, 0.1, 1)
    els[1].position = 0.75
    els[1].color = (0.95, 0.95, 1.0, 1)
    e = els.new(0.25)
    e.color = (0.1, 0.4, 1.0, 1)
    e = els.new(0.5)
    e.color = (0.15, 0.85, 0.35, 1)
    L.new(_attr(nt, 'ct_hue'), ramp.inputs['Fac'])
    L.new(ramp.outputs['Color'], b.inputs['Emission Color'])
    b.inputs['Emission Strength'].default_value = 1.2
    L.new(ramp.outputs['Color'], b.inputs['Base Color'])
    L.new(b.outputs[0], out.inputs[0])
    return m


def _vending_body(name):
    m, nt, out = _new_mat(name)
    L = nt.links
    b = _bsdf(nt, (0.5, 0.5, 0.5), 0.35, 0.1)
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.interpolation = 'CONSTANT'
    els = ramp.color_ramp.elements
    els[0].color = (0.45, 0.03, 0.03, 1)
    els[1].position = 0.75
    els[1].color = (0.6, 0.6, 0.62, 1)
    e = els.new(0.25)
    e.color = (0.03, 0.12, 0.45, 1)
    e = els.new(0.5)
    e.color = (0.05, 0.05, 0.06, 1)
    L.new(_attr(nt, 'ct_hue'), ramp.inputs['Fac'])
    L.new(ramp.outputs['Color'], b.inputs['Base Color'])
    L.new(b.outputs[0], out.inputs[0])
    return m


def _shutter(name):
    m, nt, out = _new_mat(name)
    L = nt.links
    co = _texcoord(nt)
    b = _bsdf(nt, (0.32, 0.33, 0.34), 0.45, 0.7)
    wave = nt.nodes.new('ShaderNodeTexWave')
    wave.wave_type = 'BANDS'
    wave.bands_direction = 'Z'
    wave.inputs['Scale'].default_value = 7.0
    L.new(co, wave.inputs['Vector'])
    bump = nt.nodes.new('ShaderNodeBump')
    bump.inputs['Strength'].default_value = 0.5
    L.new(wave.outputs['Fac'], bump.inputs['Height'])
    L.new(bump.outputs['Normal'], b.inputs['Normal'])
    L.new(b.outputs[0], out.inputs[0])
    return m


def build_materials():
    MAT.clear()
    MAT['Concrete'] = _concrete("CT Concrete", (0.2, 0.2, 0.2), (0.33, 0.33, 0.325))
    MAT['Concrete Dark'] = _concrete("CT Concrete Dark", (0.16, 0.16, 0.16),
                                     (0.24, 0.24, 0.235), board=False, var=0.05)
    MAT['Pod'] = _concrete("CT Pod Panel", (0.3, 0.3, 0.3), (0.42, 0.42, 0.41),
                           board=False, var=0.12)
    MAT['Groove'] = _simple("CT Groove", (0.11, 0.11, 0.11), 0.9)
    MAT['Steel'] = _simple("CT Steel", (0.045, 0.048, 0.052), 0.42, 0.85)
    MAT['Steel Light'] = _simple("CT Steel Light", (0.25, 0.26, 0.27), 0.35, 0.85)
    MAT['Gusset'] = _simple("CT Gusset", (0.62, 0.43, 0.27), 0.38, 0.65)
    MAT['Louver'] = _simple("CT Louver", (0.9, 0.9, 0.88), 0.38, 0.1)
    MAT['Dark'] = _simple("CT Dark", (0.012, 0.012, 0.014), 0.7)
    MAT['Glass'] = _glass("CT Glass")
    MAT['Dark Glass'] = _simple("CT Dark Glass", (0.015, 0.016, 0.02), 0.08, 0.4)
    MAT['Core'] = _core("CT Core Glass")
    MAT['Neon'] = _simple("CT Neon", (0.7, 0.9, 0.3), 0.3, 0.0,
                          (0.78, 1.0, 0.32), 7.0)
    MAT['Light'] = _simple("CT Light", (0.9, 0.9, 0.9), 0.3, 0.0,
                           (1.0, 0.97, 0.92), 12.0)
    MAT['Light Warm'] = _simple("CT Light Warm", (0.9, 0.8, 0.6), 0.3, 0.0,
                                (1.0, 0.78, 0.5), 14.0)
    MAT['Screen'] = _screen("CT Screen")
    MAT['Sign Blue'] = _simple("CT Sign Blue", (0.01, 0.03, 0.2), 0.3, 0.0,
                               (0.02, 0.12, 1.0), 1.4)
    MAT['Sign White'] = _simple("CT Sign White", (0.9, 0.9, 0.9), 0.3, 0.0,
                                (0.92, 0.96, 1.0), 9.0)
    MAT['Vending'] = _vending("CT Vending")
    MAT['Vending Body'] = _vending_body("CT Vending Body")
    MAT['Shop Glass'] = _simple("CT Shop Glass", (0.02, 0.018, 0.016), 0.05, 0.3,
                                (1.0, 0.55, 0.25), 0.25)
    MAT['Sign Red'] = _simple("CT Sign Red", (0.25, 0.02, 0.03), 0.3, 0.0,
                              (1.0, 0.12, 0.1), 3.0)
    MAT['Signal'] = _simple("CT Signal Red", (0.3, 0.02, 0.02), 0.3, 0.0,
                            (1.0, 0.08, 0.04), 12.0)
    MAT['Signal Green'] = _simple("CT Signal Green", (0.02, 0.3, 0.1), 0.3, 0.0,
                                  (0.1, 1.0, 0.45), 10.0)
    MAT['Shutter'] = _shutter("CT Shutter")
    MAT['Barrier'] = _concrete("CT Barrier", (0.62, 0.62, 0.6), (0.74, 0.74, 0.72),
                               board=False, var=0.04)
    MAT['Dish'] = _simple("CT Dish", (0.75, 0.75, 0.74), 0.5, 0.1)
    MAT['Hazard'] = _simple("CT Hazard Yellow", (0.75, 0.55, 0.05), 0.5, 0.1)
    return MAT


# ---------------------------------------------------------------------------
#  Basic building blocks
# ---------------------------------------------------------------------------

P_IN = [
    ("Width", 'FLOAT', 6.0, 0.5, 200.0),
    ("Height", 'FLOAT', 6.6, 0.5, 200.0),
    ("Floors", 'INT', 2, 1, 50),
    ("Floor Height", 'FLOAT', 3.3, 1.0, 20.0),
    ("Seed", 'INT', 0, 0, 1000000),
]


def _p(g):
    return g["Width"], g["Height"], g["Floors"], g["Floor Height"], g["Seed"]


def build_box():
    with Group("CT Box", [("Size", 'VEC', (1, 1, 1)), ("Center", 'VEC', (0, 0, 0)),
                          ("Material", 'MAT')],
               description="Box of a given size around a center") as g:
        c = cube(g["Size"])
        c = xform(c, g["Center"])
        g.out(setmat(c, g["Material"]))
    GR['box'] = g.tree


def build_ibeam():
    with Group("CT I-Beam Unit", [("Width", 'FLOAT', 0.3), ("Depth", 'FLOAT', 0.35),
                                  ("Material", 'MAT')],
               description="I profile, 1 m long along X, centered") as g:
        w, d, m = g["Width"], g["Depth"], g["Material"]
        tf = fmax(d * 0.11, 0.025)
        tw = fmax(w * 0.16, 0.02)
        top = box(vec(1.0, w, tf), vec(0, 0, (d - tf) * 0.5), m)
        bot = box(vec(1.0, w, tf), vec(0, 0, (tf - d) * 0.5), m)
        web = box(vec(1.0, tw, d - tf * 2.0), (0, 0, 0), m)
        g.out(join(top, bot, web))
    GR['ibeam'] = g.tree


def build_beams():
    with Group("CT Beams", [("Mesh", 'GEO'), ("Width", 'FLOAT', 0.3),
                            ("Depth", 'FLOAT', 0.35), ("Box Profile", 'BOOL', False),
                            ("Material", 'MAT')],
               description="Turns every edge of a mesh into a steel member") as g:
        ev = node('GeometryNodeInputMeshEdgeVertices')
        d = ev['Position 2'] - ev['Position 1']
        m = store(g["Mesh"], "ct_dir", d, 'FLOAT_VECTOR', 'EDGE')
        pts = node('GeometryNodeMeshToPoints', {'Mesh': m}, mode='EDGES')
        dv = named("ct_dir", 'FLOAT_VECTOR')
        ib = call(GR['ibeam'], {'Width': g["Width"], 'Depth': g["Depth"],
                                'Material': g["Material"]})
        bx = box(vec(1.0, g["Width"], g["Depth"]), (0, 0, 0), g["Material"])
        unit = switch(g["Box Profile"], ib, bx)
        length = vm('LENGTH', dv)
        res = inst(pts, unit, rot=align_x(dv), scale=vec(length, 1.0, 1.0))
        g.out(node('GeometryNodeRemoveAttribute', {'Geometry': res, 'Name': 'ct_dir'}))
    GR['beams'] = g.tree


def build_railing():
    with Group("CT Railing", [("Length", 'FLOAT', 6.0), ("Height", 'FLOAT', 1.05),
                              ("Spacing", 'FLOAT', 1.5)],
               description="Guard rail along X, centered, standing on z=0") as g:
        L, H, sp = g["Length"], g["Height"], g["Spacing"]
        n = fmax(ceil_(L / sp), 1) + 1
        posts = inst(mline(n, vec(L * -0.5, 0, 0), vec(L / (n - 1), 0, 0)),
                     box(vec(0.05, 0.05, H), vec(0, 0, H * 0.5), 'Steel Light'))
        top = box(vec(L, 0.06, 0.05), vec(0, 0, H), 'Steel Light')
        mid = box(vec(L, 0.035, 0.035), vec(0, 0, H * 0.52), 'Steel Light')
        kick = box(vec(L, 0.02, 0.09), vec(0, 0, 0.07), 'Steel Light')
        g.out(join(posts, top, mid, kick))
    GR['railing'] = g.tree


def railing(length, height=1.05, spacing=1.5):
    return call(GR['railing'], {'Length': length, 'Height': height, 'Spacing': spacing})


def build_ceiling_lights():
    with Group("CT Ceiling Lights", [("Width", 'FLOAT', 8.0), ("Depth", 'FLOAT', 1.5),
                                     ("Z", 'FLOAT', 3.0), ("Spacing", 'FLOAT', 1.1)],
               description="Grid of small soffit lights, depth grows towards +Y") as g:
        w, d, z, sp = g["Width"], g["Depth"], g["Z"], g["Spacing"]
        nx = fmax(flo(w / sp), 1)
        ny = fmax(flo(d / 0.7), 1)
        ix, iy = grid_fields(nx)
        pos = vec(w * -0.5 + (ix + 0.5) * (w / nx), (iy + 0.5) * (d / ny), z)
        g.out(inst(points(nx * ny, pos), box((0.6, 0.1, 0.03), (0, 0, 0), 'Light')))
    GR['clights'] = g.tree


def ceiling_lights(w, d, z, sp=1.1):
    return call(GR['clights'], {'Width': w, 'Depth': d, 'Z': z, 'Spacing': sp})


def build_grooves():
    with Group("CT Grooves", [("Width", 'FLOAT', 6.0), ("Height", 'FLOAT', 6.0),
                              ("Spacing", 'FLOAT', 1.2), ("Joint Spacing", 'FLOAT', 3.0)],
               description="Precast panel joints on a wall face (y = 0)") as g:
        w, h, sp, js = g["Width"], g["Height"], g["Spacing"], g["Joint Spacing"]
        nh = fmax(flo((h - 0.3) / sp), 0)
        hg = inst(mline(nh, vec(0, 0, sp), vec(0, 0, sp)),
                  box(vec(w, 0.02, 0.028), (0, -0.004, 0), 'Groove'))
        nj = fmax(rint(w / js), 1)
        jw = w / nj
        vg = inst(mline(nj - 1, vec(w * -0.5 + jw, 0, 0), vec(jw, 0, 0)),
                  box(vec(0.028, 0.02, h), vec(0, -0.004, h * 0.5), 'Groove'))
        g.out(join(hg, vg))
    GR['grooves'] = g.tree


def grooves(w, h, sp, js):
    return call(GR['grooves'], {'Width': w, 'Height': h, 'Spacing': sp, 'Joint Spacing': js})


def build_label():
    with Group("CT Label", [("Text", 'STR', "SKY"), ("Size", 'FLOAT', 0.6),
                            ("Depth", 'FLOAT', 0.03), ("Condense", 'FLOAT', 0.8),
                            ("Material", 'MAT')],
               description="Centered extruded text in the XZ plane, facing -Y") as g:
        crv = node('GeometryNodeStringToCurves', {'String': g["Text"], 'Size': g["Size"]})
        mesh = realize(node('GeometryNodeFillCurve', {'Curve': crv}))
        bb = node('GeometryNodeBoundBox', {'Geometry': mesh})
        mid = (bb['Min'] + bb['Max']) * -0.5
        mesh = xform(mesh, mid)
        mesh = node('GeometryNodeExtrudeMesh', {'Mesh': mesh, 'Offset Scale': g["Depth"]})
        mesh = xform(mesh, None, (PI / 2, 0, 0), vec(g["Condense"], 1, 1))
        g.out(setmat(mesh, g["Material"]))
    GR['label'] = g.tree


def build_neon_text():
    with Group("CT Neon Text", [("Text", 'STR', "NORTH CLOUD SECURITY"),
                                ("Size", 'FLOAT', 1.6), ("Width", 'FLOAT', 10.0),
                                ("Offset", 'FLOAT', 0.0), ("Condense", 'FLOAT', 0.62)],
               description="Repeated neon lettering. Offset is the position along the "
                           "building perimeter, so neighbouring panels continue the text") as g:
        t, size, cond = g["Text"], g["Size"], g["Condense"]
        copies = 8
        sj = _tree().nodes.new('GeometryNodeStringJoin')
        _set(sj.inputs['Delimiter'], "    ")
        for _ in range(copies):
            rr = _tree().nodes.new('NodeReroute')
            _link(t.s, rr.inputs[0])
            _link(rr.outputs[0], sj.inputs['Strings'])
        # width of a single copy and of all copies -> repeat period
        one = realize(node('GeometryNodeStringToCurves', {'String': t, 'Size': size}))
        b1 = node('GeometryNodeBoundBox', {'Geometry': one})
        w1 = sep(b1['Max'])[0] - sep(b1['Min'])[0]
        crv = node('GeometryNodeStringToCurves', {'String': S(sj.outputs[0]), 'Size': size})
        mesh = realize(node('GeometryNodeFillCurve', {'Curve': crv}))
        bb = node('GeometryNodeBoundBox', {'Geometry': mesh})
        mn_x, mn_y, _ = sep(bb['Min'])
        mx_x, _, _ = sep(bb['Max'])
        period = fmax((mx_x - mn_x - w1) / (copies - 1), 0.1)
        off = fm('FLOORED_MODULO', g["Offset"] / cond, period)
        shift = mn_x * -1.0 - off - (g["Width"] / cond) * 0.5
        mesh = xform(mesh, vec(shift, mn_y * -1.0, 0))
        mesh = node('GeometryNodeExtrudeMesh', {'Mesh': mesh, 'Offset Scale': 0.07})
        mesh = xform(mesh, None, (PI / 2, 0, 0), vec(cond, 1, 1))
        px, _, _ = sep(position())
        mesh = delete(mesh, fabs(px) > g["Width"] * 0.5, 'FACE')
        g.out(setmat(mesh, 'Neon'))
    GR['neon'] = g.tree


# ---------------------------------------------------------------------------
#  Facade generators (local space: x along facade, z up, -Y = outside)
# ---------------------------------------------------------------------------

def build_facade_concrete():
    with Group("CT Facade Concrete", P_IN + [("Screen Chance", 'FLOAT', 0.3),
                                             ("Vents", 'BOOL', True)],
               description="Board formed or precast wall with relief panels, slots, vents, screen") as g:
        w, h, n, fh, sd = _p(g)
        wall = box(vec(w, 0.3, h), vec(0, 0.15, h * 0.5), 'Concrete')
        planks = chance(0.5, 8, sd)
        gsp = switch(planks, rnd(1.0, 1.8, 1, sd), rnd(0.34, 0.55, 9, sd), 'FLOAT')
        jsp = switch(planks, rnd(2.2, 4.2, 2, sd), rnd(3.5, 6.0, 10, sd), 'FLOAT')
        gr = grooves(w, h, gsp, jsp)
        # relief panels (precast style only)
        nx = fmax(rint(w / rnd(2.4, 3.6, 3, sd)), 1)
        ny = fmax(n, 1)
        cw, ch = w / nx, h / ny
        ix, iy = grid_fields(nx)
        rp = points(nx * ny, vec(w * -0.5 + (ix + 0.5) * cw, -0.035, (iy + 0.5) * ch))
        relief = inst(rp, box(vec(cw - 0.16, 0.07, ch - 0.2), (0, 0, 0), 'Concrete'),
                      sel=chance(0.25, None, sd + 11) & ~planks)
        # horizontal slot windows
        sl_w = w * rnd(0.25, 0.6, 12, sd)
        sl_x = rnd(-0.5, 0.5, 13, sd) * (w - sl_w - 0.6)
        slot = join(box(vec(sl_w + 0.16, 0.1, 0.5), vec(sl_x, -0.02, 0), 'Steel'),
                    box(vec(sl_w, 0.12, 0.36), vec(sl_x, -0.03, 0), 'Glass'),
                    inst(mline(fmax(flo(sl_w / 0.8), 1), vec(sl_x - sl_w * 0.5, -0.09, 0), vec(0.8, 0, 0)),
                         box((0.04, 0.03, 0.36), (0, 0, 0), 'Steel')))
        slots = inst(mline(n, vec(0, 0, fh * 0.68), vec(0, 0, fh)), slot,
                     sel=chance(0.2, 14, sd))
        # small vents
        nv = rndi(-1, 2, 4, sd)
        vp = points(nv, vec(rnd(w * -0.5 + 0.7, w * 0.5 - 0.7, None, sd + 21), -0.04,
                            rnd(0.8, h - 1.0, None, sd + 22)))
        vent = join(box((0.9, 0.08, 0.56), (0, 0, 0), 'Steel'),
                    box((0.74, 0.1, 0.07), (0, -0.01, 0.15), 'Dark'),
                    box((0.74, 0.1, 0.07), (0, -0.01, 0.0), 'Dark'),
                    box((0.74, 0.1, 0.07), (0, -0.01, -0.15), 'Dark'))
        vents = inst(vp, vent, sel=g["Vents"])
        # vertical cable tray / downpipe
        tx = rnd(-0.45, 0.45, 15, sd) * w
        tray = join(box(vec(0.32, 0.1, h), vec(tx, -0.09, h * 0.5), 'Steel Light'),
                    setmat(xform(cyl(0.07, h, 10), vec(tx + 0.42, -0.12, h * 0.5)), 'Steel'),
                    inst(mline(fmax(flo(h / 1.5), 1), vec(tx, -0.06, 0.75), (0, 0, 1.5)),
                         box((0.62, 0.14, 0.06), (0.2, 0, 0), 'Steel')))
        tray = switch(chance(0.35, 16, sd), None, tray)
        # billboard screen
        sw = fmin(w * 0.42, 3.0)
        sh = fmin(h * 0.5, 4.2)
        sx = rnd(-0.5, 0.5, 6, sd) * fmax(w - sw - 0.8, 0.0)
        sz = h * 0.56
        scr = join(box(vec(sw + 0.24, 0.16, sh + 0.24), vec(sx, -0.06, sz), 'Steel'),
                   box(vec(sw, 0.06, sh), vec(sx, -0.15, sz), 'Screen'),
                   box(vec(0.1, 0.4, 0.1), vec(sx - sw * 0.35, 0.0, sz + sh * 0.5 + 0.1), 'Steel'),
                   box(vec(0.1, 0.4, 0.1), vec(sx + sw * 0.35, 0.0, sz + sh * 0.5 + 0.1), 'Steel'))
        on = chance(g["Screen Chance"], 7, sd) & (w > 3.0) & (h > 4.5)
        g.out(join(wall, gr, relief, slots, vents, tray, switch(on, None, scr)))
    GR['concrete'] = g.tree


def concrete(w, h, n, fh, sd, screen=0.0, vents=True):
    return call(GR['concrete'], {'Width': w, 'Height': h, 'Floors': n,
                                 'Floor Height': fh, 'Seed': sd,
                                 'Screen Chance': screen, 'Vents': vents})


def build_facade_louver():
    with Group("CT Facade Louver", P_IN,
               description="Spandrel bands with half drawn horizontal blinds") as g:
        w, h, n, fh, sd = _p(g)
        sp_h = fmin(fh * 0.24, 0.8)
        hz = fh - sp_h - 0.06
        back = box(vec(w, 0.1, h), vec(0, 0.42, h * 0.5), 'Dark')
        fl = mline(n, (0, 0, 0), vec(0, 0, fh))
        spandrel = inst(fl, box(vec(w, 0.36, sp_h), vec(0, 0.12, sp_h * 0.5), 'Concrete'))
        lip = inst(fl, box(vec(w, 0.08, 0.06), vec(0, -0.04, sp_h - 0.03), 'Steel'))
        glass = inst(fl, box(vec(w, 0.04, hz), vec(0, 0.3, sp_h + hz * 0.5), 'Dark Glass'))
        m = fmax(rint(w / rnd(1.2, 2.3, 1, sd)), 1)
        segw = w / m
        ns = fmax(flo(hz / 0.105), 1)
        i = index()
        col = fm('FLOORED_MODULO', i, m)
        rest = flo(i / m)
        s = fm('FLOORED_MODULO', rest, ns)
        f = flo(rest / ns)
        p = points(m * n * ns,
                   vec(w * -0.5 + (col + 0.5) * segw, -0.03,
                       f * fh + sp_h + (s + 0.5) * (hz / ns)))
        drop = rnd(0.3, 1.25, col + f * 131, sd + 3)
        keep = (s / ns) > (1.0 - drop)
        slats = inst(p, box(vec(segw - 0.07, 0.09, 0.042), (0, 0, 0), 'Louver'), sel=keep)
        mull = inst(mline(m + 1, vec(w * -0.5, 0, 0), vec(segw, 0, 0)),
                    box(vec(0.06, 0.16, h), vec(0, -0.02, h * 0.5), 'Steel'))
        g.out(join(back, spandrel, lip, glass, slats, mull))
    GR['louver'] = g.tree


def build_facade_fins():
    with Group("CT Facade Fins", P_IN,
               description="Tinted curtain wall behind vertical fins") as g:
        w, h, n, fh, sd = _p(g)
        glass = box(vec(w, 0.08, h), vec(0, 0.24, h * 0.5), 'Glass')
        fl = mline(n, (0, 0, 0), vec(0, 0, fh))
        inner = inst(fl, box(vec(w, 0.04, 0.85), vec(0, 0.19, 0.42), 'Dark'))
        nf = fmax(rint(w / rnd(0.5, 0.95, 1, sd)), 2)
        fx = w / nf
        i = index()
        fp = points(nf + 1, vec(w * -0.5 + i * fx, -0.02, h * 0.5))
        keep = chance(0.9, None, sd + 5) | (i < 1) | (i > nf - 0.5)
        fins = inst(fp, box(vec(0.07, 0.3, h - 0.04), (0, 0, 0), 'Louver'), sel=keep)
        trans = inst(fl, box(vec(w, 0.3, 0.14), vec(0, 0.05, 0.07), 'Steel'))
        mid = inst(fl, box(vec(w, 0.1, 0.05), vec(0, 0.17, fh * 0.62), 'Steel'))
        g.out(join(glass, inner, fins, trans, mid))
    GR['fins'] = g.tree


def build_ac_unit():
    with Group("CT AC Unit", [], description="Wall mounted condenser") as g:
        body = box((1.0, 0.55, 0.72), (0, 0, 0), 'Steel Light')
        fan = xform(cyl(0.25, 0.04, 20), (0.12, -0.28, 0), (PI / 2, 0, 0))
        fan = setmat(fan, 'Dark')
        grill = box((0.06, 0.05, 0.6), (-0.36, -0.28, 0), 'Dark')
        brk = join(box((0.06, 0.5, 0.06), (-0.35, 0.3, -0.38), 'Steel'),
                   box((0.06, 0.5, 0.06), (0.35, 0.3, -0.38), 'Steel'))
        g.out(join(body, fan, grill, brk))
    GR['ac'] = g.tree


def build_facade_vents():
    with Group("CT Facade Vents", P_IN,
               description="Concrete with service shafts of stacked vent units") as g:
        w, h, n, fh, sd = _p(g)
        base = concrete(w, h, n, fh, sd, 0.0, False)
        ns = rndi(1, 2, 1, sd)
        ns = fmin(ns, fmax(flo(w / 3.0), 1))
        xr = rnd(-0.3, 0.3, 2, sd) * w
        i = index()
        two = ns > 1.5
        stx = switch(two, xr, w * -0.27 + i * (w * 0.54), 'FLOAT')
        channel = inst(points(ns, vec(stx, 0, 0)),
                       box(vec(1.05, 0.06, h - 0.3), vec(0, -0.02, h * 0.5), 'Dark'))
        st = fm('FLOORED_MODULO', i, ns)
        rest = flo(i / ns)
        k = fm('FLOORED_MODULO', rest, 2)
        f = flo(rest / 2)
        ux = switch(two, xr, w * -0.27 + st * (w * 0.54), 'FLOAT')
        unit = join(box((0.82, 0.22, 0.52), (0, 0, 0), 'Steel Light'),
                    box((0.64, 0.24, 0.07), (0, -0.005, 0.13), 'Dark'),
                    box((0.64, 0.24, 0.07), (0, -0.005, 0.0), 'Dark'),
                    box((0.64, 0.24, 0.07), (0, -0.005, -0.13), 'Dark'))
        units = inst(points(ns * n * 2, vec(ux, -0.12, f * fh + fh * (0.3 + k * 0.4))), unit)
        acp = points(n, vec(rnd(w * -0.5 + 0.9, w * 0.5 - 0.9, None, sd + 8), -0.32,
                            i * fh + fh * 0.42))
        ac = inst(acp, call(GR['ac']), sel=chance(0.45, None, sd + 9))
        g.out(join(base, channel, units, ac))
    GR['vents'] = g.tree


def build_pod():
    with Group("CT Pod", [("Width", 'FLOAT', 2.8), ("Depth", 'FLOAT', 1.8),
                          ("Height", 'FLOAT', 2.4), ("Seed", 'INT', 0)],
               description="Cantilevered service pod, hangs below z=0, back at y=0") as g:
        W, D, H, sd = g["Width"], g["Depth"], g["Height"], g["Seed"]
        hu = H * 0.55
        hl = H - hu
        upper = box(vec(W, D, hu), vec(0, D * -0.5, hu * -0.5), 'Pod')
        lower = xform(cube(vec(W, D, hl)), vec(0, D * -0.5, hu * -1.0 - hl * 0.5))
        _, py, pz = sep(position())
        sel = (pz < hu * -1.0 - hl * 0.5) & (py < D * -0.5)
        lower = setmat(setpos(lower, sel, None, vec(0, D * 0.62, 0)), 'Pod')
        cap = box(vec(W + 0.1, D + 0.1, 0.12), vec(0, D * -0.5, 0.06), 'Steel')
        wx = rnd(-0.25, 0.25, 1, sd) * W
        win = join(box(vec(W * 0.26 + 0.1, 0.06, hu * 0.42 + 0.1), vec(wx, D * -1.0 - 0.02, hu * -0.5), 'Steel'),
                   box(vec(W * 0.26, 0.08, hu * 0.42), vec(wx, D * -1.0 - 0.03, hu * -0.5), 'Dark'))
        ribs = join(box(vec(0.07, 0.08, hu * 0.8), vec(W * -0.4, D * -1.0 - 0.03, hu * -0.5), 'Steel'),
                    box(vec(0.07, 0.08, hu * 0.8), vec(W * 0.4, D * -1.0 - 0.03, hu * -0.5), 'Steel'))
        light = box(vec(W * 0.55, 0.16, 0.035), vec(0, D * -0.2, H * -1.0 - 0.01), 'Light')
        bracket = box(vec(W * 0.7, 0.55, 0.18), vec(0, 0.25, -0.12), 'Steel')
        seam = box(vec(W + 0.02, D + 0.02, 0.04), vec(0, D * -0.5, hu * -1.0), 'Steel')
        g.out(join(upper, lower, cap, win, ribs, light, bracket, seam))
    GR['pod'] = g.tree


def build_pods():
    with Group("CT Pods", [("Width", 'FLOAT', 6.0), ("Height", 'FLOAT', 6.0),
                           ("Seed", 'INT', 0), ("Offset", 'FLOAT', 0.45)],
               description="One or two pods hanging from the top of a panel") as g:
        w, h, sd = g["Width"], g["Height"], g["Seed"]
        cnt = fmin(rndi(1, 2, 1, sd), fmax(flo(w / 3.6), 1))
        slot = w / cnt
        i = index()
        px = w * -0.5 + (i + 0.5) * slot + rnd(-0.12, 0.12, None, sd + 2) * slot
        p = points(cnt, vec(px, g["Offset"] * -1.0, h - 0.7))
        pw = fmin(slot * 0.8, rnd(2.6, 3.8, 3, sd))
        pod = call(GR['pod'], {'Width': pw, 'Depth': rnd(1.6, 2.5, 4, sd),
                               'Height': fmin(rnd(2.2, 3.2, 5, sd), h * 0.6), 'Seed': sd})
        g.out(inst(p, pod))
    GR['pods'] = g.tree


def build_facade_annex():
    with Group("CT Facade Annex", P_IN,
               description="Concrete wall with a large cantilevered volume") as g:
        w, h, n, fh, sd = _p(g)
        base = concrete(w, h, n, fh, sd, 0.0, True)
        bw = w * rnd(0.5, 0.85, 1, sd)
        bd = rnd(1.3, 2.7, 2, sd)
        top = h - 0.65
        bh = fmin(h * rnd(0.55, 1.0, 3, sd), top - 0.3)
        bx = rnd(-0.5, 0.5, 4, sd) * (w - bw)
        vol = box(vec(bw, bd, bh), vec(bx, bd * -0.5, top - bh * 0.5), 'Concrete')
        face = xform(grooves(bw, bh, rnd(0.7, 1.3, 5, sd), 2.6), vec(bx, bd * -1.0, top - bh))
        cap = box(vec(bw + 0.14, bd + 0.14, 0.14), vec(bx, bd * -0.5, top + 0.07), 'Steel')
        under = box(vec(bw * 0.8, 0.1, 0.035), vec(bx, bd * -1.0 + 0.4, top - bh - 0.02), 'Light')
        win = box(vec(bw * 0.7, 0.05, 0.4), vec(bx, bd * -1.0 - 0.02, top - bh * 0.25), 'Dark')
        win = switch(chance(0.5, 6, sd), None, win)
        pods = xform(call(GR['pods'], {'Width': bw, 'Height': bh, 'Seed': sd + 3, 'Offset': 0.35}),
                     vec(bx, bd * -1.0, top - bh + 0.2))
        pods = switch(chance(0.5, 7, sd) & (bh > 4.0), None, pods)
        g.out(join(base, vol, face, cap, under, win, pods))
    GR['annex'] = g.tree


def build_bracing():
    with Group("CT Bracing", [("Width", 'FLOAT', 8.0), ("Height", 'FLOAT', 7.0),
                              ("Seed", 'INT', 0), ("Pattern", 'INT', 0),
                              ("Size", 'FLOAT', 0.28)],
               description="Steel bracing (X / V / chevron / zigzag) with gusset plates") as g:
        w, h, pat, size = g["Width"], g["Height"], g["Pattern"], g["Size"]
        nb = fmax(rint(w / fmax(h * 1.05, 3.5)), 1)
        bw = w / nb
        z0 = 0.4
        hz = h - 0.8
        X = join(seg((-0.5, 0, 0), (0.5, 0, 1)), seg((0.5, 0, 0), (-0.5, 0, 1)))
        V = join(seg((-0.5, 0, 1), (0, 0, 0)), seg((0.5, 0, 1), (0, 0, 0)))
        A = join(seg((-0.5, 0, 0), (0, 0, 1)), seg((0.5, 0, 0), (0, 0, 1)))
        N = seg((-0.5, 0, 0), (0.5, 0, 1))
        unit = iswitch(pat, [X, V, A, N])
        i = index()
        sgn = switch(fm('FLOORED_MODULO', i, 2) > 0.5, 1.0, -1.0, 'FLOAT')
        bp = points(nb, vec(w * -0.5 + (i + 0.5) * bw, -0.27, z0))
        edges = realize(inst(bp, unit, scale=vec(bw * sgn, 1.0, hz)))
        mem = beams(edges, size, size * 1.15, 'Steel')
        ev = node('GeometryNodeInputMeshEdgeVertices')
        d = ev['Position 2'] - ev['Position 1']
        dn = vm('NORMALIZE', d)
        e2 = store(edges, "ct_dir", d, 'FLOAT_VECTOR', 'EDGE')
        pa = node('GeometryNodeMeshToPoints', {'Mesh': e2, 'Position': ev['Position 1'] + dn * (size * 1.6)},
                  mode='EDGES')
        pb = node('GeometryNodeMeshToPoints', {'Mesh': e2, 'Position': ev['Position 2'] - dn * (size * 1.6)},
                  mode='EDGES')
        plate = box(vec(size * 3.8, 0.06, size * 2.4), (0, 0, 0), 'Gusset')
        gus = inst(join(pa, pb), plate, rot=align_x(named("ct_dir", 'FLOAT_VECTOR')))
        gus = node('GeometryNodeRemoveAttribute', {'Geometry': gus, 'Name': 'ct_dir'})
        g.out(join(mem, gus))
    GR['bracing'] = g.tree


# ---------------------------------------------------------------------------
#  Ground floor generators
# ---------------------------------------------------------------------------

def build_ground_shop():
    with Group("CT Ground Shop", P_IN + [("Text", 'STR', "SKY")],
               description="Recessed shop front with neon sign, shutter, vending machines") as g:
        w, h, n, fh, sd = _p(g)
        shh = fmin(h - 0.8, 3.9)
        uh = h - shh
        upper = xform(call(GR['louver'], {'Width': w, 'Height': uh, 'Floors': 1,
                                          'Floor Height': uh, 'Seed': sd}),
                      vec(0, 0, shh))
        back = box(vec(w, 0.1, shh), vec(0, 1.35, shh * 0.5), 'Dark')
        gw = w * rnd(0.35, 0.55, 1, sd)
        gx = rnd(-0.5, 0.5, 2, sd) * (w - gw - 1.0)
        shop_glass = join(box(vec(gw, 0.05, shh * 0.68), vec(gx, 1.28, shh * 0.34 + 0.15), 'Shop Glass'),
                          inst(mline(fmax(flo(gw / 1.1), 1) + 1, vec(gx - gw * 0.5, 1.22, shh * 0.34 + 0.15),
                                     vec(gw / fmax(flo(gw / 1.1), 1), 0, 0)),
                               box(vec(0.06, 0.06, shh * 0.7), (0, 0, 0), 'Steel')),
                          box(vec(gw + 0.1, 0.08, 0.08), vec(gx, 1.22, shh * 0.68 + 0.17), 'Steel'))
        sides = join(box(vec(0.3, 1.4, shh), vec(w * -0.5 + 0.15, 0.7, shh * 0.5), 'Concrete'),
                     box(vec(0.3, 1.4, shh), vec(w * 0.5 - 0.15, 0.7, shh * 0.5), 'Concrete'))
        ceil = box(vec(w, 1.45, 0.28), vec(0, 0.72, shh + 0.14), 'Concrete')
        lights = ceiling_lights(w - 0.8, 1.2, shh - 0.02)
        sw = w * rnd(0.25, 0.42, 3, sd)
        sx = w * -0.5 + 0.45 + sw * 0.5
        shutter = join(box(vec(sw, 0.08, shh - 0.95), vec(sx, 0.95, (shh - 0.95) * 0.5), 'Shutter'),
                       box(vec(sw + 0.12, 0.38, 0.42), vec(sx, 0.85, shh - 0.95 + 0.21), 'Steel'))
        sgw = fmin(w * 0.5, 4.6)
        sgh = 0.85
        sgx = rnd(-0.5, 0.5, 4, sd) * (w - sgw - 0.8)
        sgz = shh - 0.62
        label = call(GR['label'], {'Text': g["Text"], 'Size': sgh * 0.72, 'Depth': 0.04,
                                   'Condense': 0.78, 'Material': MAT['Sign White']})
        sign = join(box(vec(sgw + 0.1, 0.12, sgh + 0.1), vec(sgx, -0.06, sgz), 'Steel'),
                    box(vec(sgw, 0.14, sgh), vec(sgx, -0.12, sgz), 'Sign Blue'),
                    xform(label, vec(sgx + sgw * 0.08, -0.2, sgz)),
                    xform(box((0.9, 0.025, 0.05), (0, 0, 0), 'Sign White'),
                          vec(sgx - sgw * 0.32, -0.2, sgz - 0.05), (0, -0.45, 0)),
                    xform(box((0.5, 0.025, 0.05), (0, 0, 0), 'Sign White'),
                          vec(sgx - sgw * 0.38, -0.2, sgz + 0.18), (0, 0.0, 0)))
        nvm = rndi(2, 3, 5, sd)
        vp = points(nvm, vec(w * 0.5 - 0.9 - index() * 0.95, -0.42, 0.95))
        vend = join(box((0.88, 0.78, 1.9), (0, 0, 0), 'Vending Body'),
                    box((0.7, 0.04, 1.25), (0, -0.4, 0.22), 'Vending'),
                    box((0.3, 0.05, 0.25), (0.15, -0.4, -0.6), 'Dark'))
        vend = store(inst(vp, vend), "ct_hue", rnd(0.0, 1.0, None, sd + 6), 'FLOAT', 'INSTANCE')
        g.out(join(upper, back, shop_glass, sides, ceil, lights, shutter, sign, vend))
    GR['shop'] = g.tree


def build_stair():
    with Group("CT Stair", P_IN,
               description="External steel stair in a cage in front of a concrete wall") as g:
        w, h, n, fh, sd = _p(g)
        wall = concrete(w, h, n, fh, sd, 0.0, False)
        sw = fmin(w - 0.4, 5.2)
        sx = rnd(-0.5, 0.5, 1, sd) * (w - sw)
        nf = fmax(rint(h / 1.75), 1)
        rise = h / nf
        land = 1.0
        run = sw - land * 2.0
        yc = -1.0
        sdw = 1.1
        nst = fmax(rint(rise / 0.18), 2)
        i = index()
        steps = inst(points(nst, vec(run * -0.5 + (i + 0.5) * (run / nst), 0,
                                     (i + 0.5) * (rise / nst))),
                     box(vec(run / nst + 0.03, sdw, 0.06), (0, 0, 0), 'Steel'))
        ang = fm('ARCTANGENT', rise / run)
        L = fm('SQRT', run * run + rise * rise)
        rot = vec(0, ang * -1.0, 0)
        stringers = join(xform(box(vec(L, 0.06, 0.28), (0, 0, 0), 'Steel'), vec(0, sdw * 0.5 + 0.03, rise * 0.5), rot),
                         xform(box(vec(L, 0.06, 0.28), (0, 0, 0), 'Steel'), vec(0, sdw * -0.5 - 0.03, rise * 0.5), rot))
        rails = join(xform(box(vec(L, 0.04, 0.04), (0, 0, 0), 'Steel Light'), vec(0, sdw * 0.5 + 0.04, rise * 0.5 + 0.95), rot),
                     xform(box(vec(L, 0.04, 0.04), (0, 0, 0), 'Steel Light'), vec(0, sdw * -0.5 - 0.04, rise * 0.5 + 0.95), rot))
        landing = join(box(vec(land, sdw + 0.1, 0.14), vec(run * 0.5 + land * 0.5, 0, rise - 0.07), 'Steel'),
                       box(vec(0.04, sdw, 0.04), vec(run * 0.5 + land - 0.02, 0, rise + 1.0), 'Steel Light'))
        flight = join(steps, stringers, rails, landing)
        flights = inst(points(nf, vec(sx, yc, i * rise)), flight, rot=vec(0, 0, i * PI))
        px = fm('FLOORED_MODULO', i, 2)
        py = flo(i / 2)
        posts = inst(points(4, vec(sx + (px - 0.5) * (sw + 0.12), yc + (py - 0.5) * 1.35, h * 0.5)),
                     box(vec(0.16, 0.16, h), (0, 0, 0), 'Steel'))
        lv = points(nf, vec(sx, yc, (i + 1) * rise))
        levels = inst(lv, join(box(vec(sw + 0.2, 0.12, 0.2), vec(0, -0.68, -0.1), 'Steel'),
                               box(vec(sw + 0.2, 0.12, 0.2), vec(0, 0.68, -0.1), 'Steel')))
        g.out(join(wall, flights, posts, levels))
    GR['stair'] = g.tree


def build_ground_buttress():
    with Group("CT Ground Buttress", P_IN,
               description="Sloped concrete buttresses with dark recessed doors") as g:
        w, h, n, fh, sd = _p(g)
        wall = box(vec(w, 0.3, h), vec(0, 0.15, h * 0.5), 'Concrete')
        gr = grooves(w, h, rnd(1.0, 1.6, 1, sd), 3.0)
        nb = fmax(rint(w / 3.2), 1)
        bw = (w - 0.6) / nb
        bh = fmin(h * 0.62, 3.8)
        bd = rnd(1.6, 2.6, 2, sd)
        wedge = xform(cube(vec(0.6, bd, bh)), vec(0, bd * -0.5, bh * 0.5))
        _, py, pz = sep(position())
        wedge = setpos(wedge, (pz > bh * 0.5) & (py < bd * -0.5), None, vec(0, 0, bh * -0.72))
        wedge = setmat(wedge, 'Concrete')
        i = index()
        wedges = inst(points(nb + 1, vec(w * -0.5 + 0.3 + i * bw, 0, 0)), wedge)
        door = join(box(vec(bw * 0.5, 0.08, bh * 0.78), vec(0, -0.02, bh * 0.39), 'Dark'),
                    box(vec(bw * 0.34, 0.14, 0.05), vec(0, -0.1, bh * 0.78 + 0.12), 'Light'),
                    box(vec(bw * 0.5 + 0.16, 0.1, 0.08), vec(0, -0.04, bh * 0.78 + 0.04), 'Steel'))
        doors = inst(points(nb, vec(w * -0.5 + 0.3 + (i + 0.5) * bw, 0, 0)), door,
                     sel=chance(0.75, None, sd + 3))
        ledge = box(vec(w, 0.5, 0.25), vec(0, -0.2, bh + 0.35), 'Concrete')
        g.out(join(wall, gr, wedges, doors, ledge))
    GR['buttress'] = g.tree


def build_ground_loading():
    with Group("CT Ground Loading", P_IN,
               description="Roller shutter bays with lamps and bollards") as g:
        w, h, n, fh, sd = _p(g)
        wall = concrete(w, h, n, fh, sd, 0.0, False)
        nd = fmax(fmin(flo(w / 4.6), 3), 1)
        dw = fmin(3.8, w / nd - 0.9)
        dh = fmin(h - 1.3, 4.3)
        door = join(box(vec(dw, 0.1, dh), vec(0, -0.02, dh * 0.5), 'Shutter'),
                    box(vec(dw + 0.34, 0.14, 0.16), vec(0, -0.05, dh + 0.08), 'Steel'),
                    box(vec(0.16, 0.14, dh), vec(dw * -0.5 - 0.08, -0.05, dh * 0.5), 'Steel'),
                    box(vec(0.16, 0.14, dh), vec(dw * 0.5 + 0.08, -0.05, dh * 0.5), 'Steel'),
                    box(vec(0.4, 0.3, 0.12), vec(0, -0.16, dh + 0.5), 'Steel'),
                    box(vec(0.34, 0.24, 0.02), vec(0, -0.16, dh + 0.43), 'Light Warm'),
                    xform(setmat(cyl(0.1, 0.9), 'Hazard'), vec(dw * -0.5 - 0.35, -0.55, 0.45)),
                    xform(setmat(cyl(0.1, 0.9), 'Hazard'), vec(dw * 0.5 + 0.35, -0.55, 0.45)))
        doors = inst(points(nd, vec(w * -0.5 + (index() + 0.5) * (w / nd), 0, 0)), door)
        g.out(join(wall, doors))
    GR['loading'] = g.tree


# ---------------------------------------------------------------------------
#  Open levels
# ---------------------------------------------------------------------------

SIGN_OFFSET = 0.62


def build_level_sign():
    with Group("CT Level Sign", [("Width", 'FLOAT', 10.0), ("Height", 'FLOAT', 2.6),
                                 ("Seed", 'INT', 0),
                                 ("Text", 'STR', "NORTH CLOUD SECURITY"),
                                 ("Text Offset", 'FLOAT', 0.0)],
               description="Open transfer level with soffit lights and neon lettering") as g:
        w, h, sd = g["Width"], g["Height"], g["Seed"]
        lights = ceiling_lights(w, 1.5, h - 0.44)
        # the lettering runs on a ring SIGN_OFFSET outside the facade; panels are
        # widened so neighbouring faces meet exactly at the ring corners
        txt = call(GR['neon'], {'Text': g["Text"], 'Size': h * 0.6,
                                'Width': w + SIGN_OFFSET * 2.0, 'Offset': g["Text Offset"]})
        txt = xform(txt, vec(0, -SIGN_OFFSET, h * 0.17))
        ledge = box(vec(w, 0.32, 0.24), vec(0, -0.04, 0.12), 'Steel')
        rail = xform(railing(w, 1.0, 1.6), (0, 0.1, 0.24))
        g.out(join(lights, txt, ledge, rail))
    GR['sign'] = g.tree


def build_level_roof():
    with Group("CT Level Roof", [("Width", 'FLOAT', 10.0), ("Height", 'FLOAT', 3.6),
                                 ("Seed", 'INT', 0)],
               description="Open plant level: soffit lights, guard rail, partial louvres") as g:
        w, h, sd = g["Width"], g["Height"], g["Seed"]
        lights = ceiling_lights(w, 1.5, h - 0.44)
        rail = xform(railing(w, 1.05, 1.5), (0, 0.0, 0.0))
        pw = w * rnd(0.3, 0.65, 1, sd)
        px = rnd(-0.5, 0.5, 2, sd) * (w - pw)
        nfin = fmax(flo(pw / 0.2), 1)
        fins = inst(points(nfin, vec(px - pw * 0.5 + (index() + 0.5) * (pw / nfin), 0.25, (h - 0.5) * 0.5)),
                    box(vec(0.04, 0.14, h - 0.5), (0, 0, 0), 'Louver'))
        fins = switch(chance(0.55, 3, sd), None, fins)
        g.out(join(lights, rail, fins))
    GR['roof_level'] = g.tree


# ---------------------------------------------------------------------------
#  Panel dispatcher
# ---------------------------------------------------------------------------

# type codes
T_LOUVER, T_FINS, T_CONCRETE, T_VENTS, T_ANNEX = 0, 1, 2, 3, 4
T_SHOP, T_STAIR, T_BUTTRESS, T_LOADING = 5, 6, 7, 8
T_SIGN, T_ROOF_OPEN, T_ROOF_CORE = 9, 10, 11


def build_panel():
    with Group("CT Panel", P_IN + [
            ("Type", 'INT', 0, 0, 11),
            ("Brace", 'BOOL', False), ("Brace Pattern", 'INT', 0, 0, 3),
            ("Brace Size", 'FLOAT', 0.28),
            ("Pods", 'BOOL', False), ("Screen Chance", 'FLOAT', 0.3),
            ("Sign Text", 'STR', "NORTH CLOUD SECURITY"), ("Shop Text", 'STR', "SKY"),
            ("Text Offset", 'FLOAT', 0.0)],
            description="Builds one facade panel of the given type in local space") as g:
        w, h, n, fh, sd = _p(g)
        common = {'Width': w, 'Height': h, 'Floors': n, 'Floor Height': fh, 'Seed': sd}
        shop = dict(common)
        shop['Text'] = g["Shop Text"]
        core = dict(common)
        core.update({'Floors': 1, 'Floor Height': h})
        items = [
            call(GR['louver'], common),
            call(GR['fins'], common),
            concrete(w, h, n, fh, sd, switch(g["Brace"], g["Screen Chance"], 0.0, 'FLOAT'), True),
            call(GR['vents'], common),
            call(GR['annex'], common),
            call(GR['shop'], shop),
            call(GR['stair'], common),
            call(GR['buttress'], common),
            call(GR['loading'], common),
            call(GR['sign'], {'Width': w, 'Height': h, 'Seed': sd, 'Text': g["Sign Text"],
                              'Text Offset': g["Text Offset"]}),
            call(GR['roof_level'], {'Width': w, 'Height': h, 'Seed': sd}),
            call(GR['vents'], core),
        ]
        base = iswitch(g["Type"], items)
        br = call(GR['bracing'], {'Width': w, 'Height': h, 'Seed': sd,
                                  'Pattern': g["Brace Pattern"], 'Size': g["Brace Size"]})
        pods = call(GR['pods'], {'Width': w, 'Height': h, 'Seed': sd + 17})
        g.out(join(base, switch(g["Brace"], None, br), switch(g["Pods"], None, pods)))
    GR['panel'] = g.tree


# ---------------------------------------------------------------------------
#  Global structure
# ---------------------------------------------------------------------------

def build_frame():
    with Group("CT Frame", [("Rows", 'GEO'), ("Width", 'FLOAT', 16.0),
                            ("Depth", 'FLOAT', 12.0), ("Split X", 'FLOAT', -1.0),
                            ("Top", 'FLOAT', 30.0), ("Column Size", 'FLOAT', 0.55),
                            ("Girder Depth", 'FLOAT', 0.6), ("Core Inset", 'FLOAT', 1.6)],
               description="Columns, ring girders, slabs and the glazed cores of open levels") as g:
        W, D, xs, top = g["Width"], g["Depth"], g["Split X"], g["Top"]
        cs, gd, ci = g["Column Size"], g["Girder Depth"], g["Core Inset"]
        co = 0.18
        xo = W * 0.5 + co
        yo = D * 0.5 + co
        i = index()
        mid_b = (xs + W * 0.5) * 0.5
        cpos = iswitch(i, [vec(xo * -1.0, yo * -1.0, 0), vec(xo, yo * -1.0, 0), vec(xo, yo, 0),
                           vec(xo * -1.0, yo, 0), vec(xs, yo * -1.0, 0), vec(xs, yo, 0),
                           vec(xo, 0, 0), vec(xo * -1.0, 0, 0),
                           vec(mid_b, yo * -1.0, 0), vec(mid_b, yo, 0)], 'VECTOR')
        side_mid = D > 10.5
        b_mid = (W * 0.5 - xs) > 11.0
        keep = iswitch(i, [True, True, True, True, True, True, side_mid, side_mid, b_mid, b_mid],
                       'BOOLEAN')
        cp = delete(points(10, cpos), ~keep)
        col_edges = realize(inst(cp, seg((0, 0, 0), vec(0, 0, top + 0.02))))
        cols = beams(col_edges, cs, cs * 0.9, 'Steel')
        base_pl = inst(cp, box(vec(cs + 0.3, cs + 0.3, 0.12), (0, 0, 0.06), 'Steel'))
        rows, cap = capture(g["Rows"], [("kind", 'INT', named("kind", 'INT')),
                                         ("h", 'FLOAT', named("h"))])
        rows = remove_attrs(rows, ("h", "kind", "floors"))
        lv = delete(rows, ieq(index(), 0))
        levels = join(lv, points(1, vec(0, 0, top)))
        rect = node('GeometryNodeCurveToMesh', {
            'Curve': node('GeometryNodeCurvePrimitiveQuadrilateral',
                          {'Width': xo * 2.0, 'Height': yo * 2.0})})
        g_edges = realize(inst(levels, rect))
        girders = beams(xform(g_edges, vec(0, 0, gd * -0.5)), 0.36, gd, 'Steel')
        slabs = inst(levels, box(vec(W + 0.1, D + 0.1, 0.42), (0, 0, -0.21), 'Concrete Dark'))
        kind = cap["kind"]
        hh = cap["h"]
        core = inst(rows, box(vec(W - ci * 2.0, D - ci * 2.0, 1.0), (0, 0, 0.5), 'Core'),
                    sel=kind > 1.5, scale=vec(1.0, 1.0, hh))
        floor_in = inst(rows, box(vec(W - 0.2, D - 0.2, 0.1), (0, 0, 0.05), 'Concrete Dark'),
                        sel=kind > 1.5)
        g.out(join(cols, base_pl, girders, slabs, core, floor_in))
    GR['frame'] = g.tree


def build_hvac():
    with Group("CT HVAC", [("Seed", 'INT', 0)], description="Rooftop air handling unit") as g:
        sd = g["Seed"]
        L = rnd(1.8, 3.2, 1, sd)
        body = box(vec(L, 1.4, 1.15), (0, 0, 0.7), 'Steel Light')
        base = box(vec(L + 0.2, 1.6, 0.15), (0, 0, 0.075), 'Steel')
        nf = fmax(flo(L / 0.95), 1)
        fan = join(setmat(xform(cyl(0.36, 0.12, 24), (0, 0, 1.3)), 'Steel'),
                   setmat(xform(cyl(0.3, 0.13, 24), (0, 0, 1.31)), 'Dark'))
        fans = inst(points(nf, vec(L * -0.5 + (index() + 0.5) * (L / nf), 0, 0)), fan)
        grill = inst(mline(fmax(flo(L / 0.12), 1), vec(L * -0.5 + 0.1, -0.71, 0.75), vec(0.12, 0, 0)),
                     box((0.03, 0.03, 0.75), (0, 0, 0), 'Dark'))
        pipe = setmat(xform(cyl(0.08, 1.2), vec(L * 0.5 + 0.2, 0.3, 0.6)), 'Steel Light')
        g.out(join(body, base, fans, grill, pipe))
    GR['hvac'] = g.tree


def build_mast():
    with Group("CT Lattice Mast", [("Height", 'FLOAT', 10.0), ("Size", 'FLOAT', 0.9),
                                   ("Seed", 'INT', 0)],
               description="Telecom lattice tower with sector antennas") as g:
        H, a = g["Height"], g["Size"] * 0.5
        i = index()
        legs = realize(inst(points(4, vec((fm('FLOORED_MODULO', i, 2) - 0.5) * 2.0 * a,
                                         (flo(i / 2) - 0.5) * 2.0 * a, 0)),
                            seg((0, 0, 0), vec(0, 0, H))))
        step = a * 1.6
        nz = fmax(flo(H / step), 1)
        zig = mline(nz + 1, (0, 0, 0), vec(0, 0, step))
        zig = setpos(zig, None, vec((fm('FLOORED_MODULO', index(), 2) * 2.0 - 1.0) * a, a * -1.0,
                                    index() * step))
        faces = join(zig, xform(zig, None, (0, 0, PI / 2)), xform(zig, None, (0, 0, PI)),
                     xform(zig, None, (0, 0, PI * 1.5)))
        lattice = join(beams(legs, 0.09, 0.09, 'Steel Light', True),
                       beams(realize(faces), 0.04, 0.04, 'Steel Light', True))
        panel = box((0.12, 0.34, 1.4), (0, 0, 0), 'Dish')
        sect = inst(points(6, vec(fm('COSINE', i * (PI / 3)) * (a + 0.4),
                                  fm('SINE', i * (PI / 3)) * (a + 0.4), H - 1.2)),
                    panel, rot=vec(0, 0, i * (PI / 3)))
        plat = box(vec(a * 2.0 + 0.7, a * 2.0 + 0.7, 0.08), vec(0, 0, H - 2.2), 'Steel')
        pole = setmat(xform(cyl(0.05, 3.5), vec(0, 0, H + 1.75)), 'Steel Light')
        beacon = setmat(xform(node('GeometryNodeMeshUVSphere', {'Radius': 0.12}), vec(0, 0, H + 3.55)), 'Signal')
        foot = box(vec(a * 2.0 + 0.5, a * 2.0 + 0.5, 0.3), (0, 0, 0.15), 'Concrete Dark')
        dishes = setmat(xform(node('GeometryNodeMeshUVSphere', {'Radius': 0.35, 'Segments': 16, 'Rings': 8}),
                              vec(a + 0.25, 0, H - 3.4), None, (0.4, 1, 1)), 'Dish')
        g.out(join(lattice, sect, plat, pole, beacon, foot, dishes))
    GR['mast'] = g.tree


def build_whip():
    with Group("CT Whip Antenna", [("Height", 'FLOAT', 7.0), ("Seed", 'INT', 0)],
               description="Guyed antenna pole") as g:
        H, sd = g["Height"], g["Seed"]
        pole = join(setmat(xform(cyl(0.05, H), vec(0, 0, H * 0.5)), 'Steel Light'),
                    setmat(xform(cyl(0.022, H * 0.5), vec(0, 0, H * 1.25)), 'Steel Light'))
        base = box((0.45, 0.45, 0.2), (0, 0, 0.1), 'Steel')
        rot = rnd(0, PI, 1, sd)
        top = vec(0, 0, H * 0.7)
        wires = []
        for k in range(3):
            ang = rot + k * (2 * PI / 3)
            anchor = vec(fm('COSINE', ang) * 2.4, fm('SINE', ang) * 2.4, 0.05)
            wires.append(seg(anchor, top))
        guys = beams(join(*wires), 0.018, 0.018, 'Steel Light', True)
        bars = join(box((0.9, 0.03, 0.03), (0, 0, 0), 'Steel Light'),
                    box((0.03, 0.9, 0.03), (0, 0, 0), 'Steel Light'))
        bars = join(xform(bars, vec(0, 0, H * 0.92)), xform(bars, vec(0, 0, H * 0.8)))
        g.out(join(pole, base, guys, bars))
    GR['whip'] = g.tree


def build_dish():
    with Group("CT Dish", [("Radius", 'FLOAT', 0.8), ("Seed", 'INT', 0)],
               description="Satellite dish on a post") as g:
        r, sd = g["Radius"], g["Seed"]
        sph = node('GeometryNodeMeshUVSphere', {'Segments': 28, 'Rings': 14, 'Radius': r})
        _, _, pz = sep(position())
        bowl = delete(sph, pz > r * -0.55)
        bowl = xform(bowl, vec(0, 0, r * 0.55))
        bowl = node('GeometryNodeExtrudeMesh', {'Mesh': bowl, 'Offset Scale': -0.02})
        arm = box(vec(0.03, 0.03, r * 0.9), vec(0, 0, r * 0.45), 'Steel Light')
        head = box((0.12, 0.12, 0.12), vec(0, 0, r * 0.9), 'Steel Light')
        dish = join(setmat(bowl, 'Dish'), arm, head)
        tilt = rnd(0.7, 1.1, 1, sd)
        dish = xform(dish, vec(0, 0, 1.25), vec(tilt, 0, rnd(0, PI * 2, 2, sd)))
        post = setmat(xform(cyl(0.06, 1.3), (0, 0, 0.65)), 'Steel')
        foot = box((0.6, 0.6, 0.12), (0, 0, 0.06), 'Steel')
        g.out(join(dish, post, foot))
    GR['dish'] = g.tree


def build_roof():
    with Group("CT Roof", [("Width", 'FLOAT', 16.0), ("Depth", 'FLOAT', 12.0),
                           ("Split X", 'FLOAT', -1.0), ("Seed", 'INT', 0),
                           ("Mast", 'BOOL', True), ("Antennas", 'INT', 2),
                           ("Dish", 'BOOL', True), ("HVAC Density", 'FLOAT', 0.6)],
               description="Roof top: rails, plant, overrun, mast, antennas, dish (z=0 = roof)") as g:
        W, D, xs, sd = g["Width"], g["Depth"], g["Split X"], g["Seed"]
        fr = xform(railing(W + 0.1, 1.1), vec(0, D * -0.5 + 0.12, 0))
        bk = xform(railing(W + 0.1, 1.1), vec(0, D * 0.5 - 0.12, 0))
        lf = xform(railing(D + 0.1, 1.1), vec(W * -0.5 + 0.12, 0, 0), (0, 0, PI / 2))
        rt = xform(railing(D + 0.1, 1.1), vec(W * 0.5 - 0.12, 0, 0), (0, 0, PI / 2))
        parapet = join(box(vec(W + 0.3, 0.3, 0.3), vec(0, D * -0.5, 0.15), 'Concrete'),
                       box(vec(W + 0.3, 0.3, 0.3), vec(0, D * 0.5, 0.15), 'Concrete'),
                       box(vec(0.3, D + 0.3, 0.3), vec(W * -0.5, 0, 0.15), 'Concrete'),
                       box(vec(0.3, D + 0.3, 0.3), vec(W * 0.5, 0, 0.15), 'Concrete'))
        # stair / lift overrun on mass A
        wa = xs + W * 0.5
        ow = fmax(wa * rnd(0.4, 0.7, 1, sd), 2.4)
        od = D * rnd(0.3, 0.5, 2, sd)
        oh = rnd(2.8, 4.4, 3, sd)
        ox = W * -0.5 + 0.9 + ow * 0.5
        oy = rnd(-0.5, 0.5, 4, sd) * (D - od - 2.0)
        ov = join(box(vec(ow, od, oh), vec(ox, oy, oh * 0.5), 'Concrete'),
                  box(vec(ow + 0.2, od + 0.2, 0.18), vec(ox, oy, oh + 0.09), 'Steel'),
                  box(vec(1.0, 0.06, 2.1), vec(ox + ow * 0.2, oy - od * 0.5 - 0.02, 1.05), 'Steel Light'),
                  box(vec(0.5, 0.15, 0.1), vec(ox + ow * 0.2, oy - od * 0.5 - 0.1, 2.3), 'Light'),
                  xform(call(GR['louver'], {'Width': ow * 0.6, 'Height': 1.2, 'Floors': 1,
                                            'Floor Height': 1.2, 'Seed': sd}),
                        vec(ox - ow * 0.15, oy - od * 0.5 - 0.02, oh - 1.5)),
                  xform(grooves(ow, oh, 1.2, 2.5), vec(ox, oy - od * 0.5, 0)))
        # plant on mass B
        bx0 = xs + 1.8
        bx1 = W * 0.5 - 1.8
        i = index()
        ix, iy = grid_fields(3)
        hp = points(6, vec(bx0 + (ix + 0.5) * ((bx1 - bx0) / 3) + rnd(-0.4, 0.4, None, sd + 5),
                           D * -0.5 + 2.0 + (iy + 0.5) * ((D - 4.0) / 2) + rnd(-0.5, 0.5, None, sd + 6),
                           0))
        keep = chance(g["HVAC Density"], None, sd + 7) & ~(ieq(i, 5))
        hv = inst(hp, call(GR['hvac'], {'Seed': sd}),
                  rot=vec(0, 0, rndi(0, 1, None, sd + 8) * (PI / 2)), sel=keep)
        mast = xform(call(GR['mast'], {'Height': rnd(8.0, 13.0, 9, sd), 'Seed': sd}),
                     vec(bx1 + 0.3, D * 0.5 - 2.2, 0))
        mast = switch(g["Mast"], None, mast)
        wp = points(g["Antennas"], vec(rnd(W * -0.5 + 1.5, W * 0.5 - 1.5, None, sd + 10),
                                       rnd(D * -0.5 + 1.5, D * 0.5 - 1.5, None, sd + 11), 0))
        whips = inst(wp, call(GR['whip'], {'Height': rnd(5.0, 9.0, 12, sd), 'Seed': sd}),
                     scale=vec(1, 1, rnd(0.7, 1.2, None, sd + 13)))
        dish = xform(call(GR['dish'], {'Radius': rnd(0.6, 1.0, 14, sd), 'Seed': sd}),
                     vec(ox, oy, oh + 0.18))
        dish = switch(g["Dish"], None, dish)
        cable = box(vec(W * 0.6, 0.4, 0.12), vec(0, D * 0.5 - 1.0, 0.06), 'Steel')
        npipe = rndi(2, 4, 15, sd)
        pp = points(npipe, vec(ox + ow * 0.5 + 0.6 + index() * 0.55, oy + od * 0.5 - 0.4, 0))
        pipe = join(setmat(xform(cyl(0.16, 2.4, 14), (0, 0, 1.2)), 'Steel Light'),
                    setmat(xform(cyl(0.2, 0.25, 14), (0, 0, 2.3)), 'Steel'))
        pipes = inst(pp, pipe, scale=vec(1, 1, rnd(0.6, 1.4, None, sd + 16)))
        g.out(join(fr, bk, lf, rt, parapet, ov, hv, mast, whips, dish, cable, pipes))
    GR['roof'] = g.tree


def build_blade_sign():
    with Group("CT Blade Sign", [("Text", 'STR', "21"), ("Size", 'FLOAT', 1.1)],
               description="Box sign with a lit number, hangs from a bracket at x<0") as g:
        sz = g["Size"]
        frame = box(vec(sz * 1.1, 0.34, sz), (0, 0, 0), 'Steel')
        face = box(vec(sz * 0.98, 0.36, sz * 0.88), (0, 0, 0), 'Sign Red')
        lab = call(GR['label'], {'Text': g["Text"], 'Size': sz * 0.62, 'Depth': 0.03,
                                 'Condense': 1.0, 'Material': MAT['Sign White']})
        front = xform(lab, vec(0, -0.2, 0))
        back = xform(lab, vec(0, 0.2, 0), (0, 0, PI))
        arm = join(box(vec(0.7, 0.08, 0.08), vec(sz * -0.55 - 0.35, 0, sz * 0.3), 'Steel'),
                   box(vec(0.7, 0.08, 0.08), vec(sz * -0.55 - 0.35, 0, sz * -0.3), 'Steel'))
        g.out(join(frame, face, front, back, arm))
    GR['blade'] = g.tree


def build_lamp():
    with Group("CT Street Lamp", [("Height", 'FLOAT', 6.5), ("Arm", 'FLOAT', 1.6)],
               description="Street light, arm points to +X") as g:
        H, A = g["Height"], g["Arm"]
        pole = setmat(xform(cyl(0.07, H, 10), vec(0, 0, H * 0.5)), 'Steel')
        base = setmat(xform(cyl(0.14, 0.6, 10), (0, 0, 0.3)), 'Steel')
        arm = box(vec(A, 0.08, 0.08), vec(A * 0.5, 0, H - 0.05), 'Steel')
        head = box((0.75, 0.3, 0.13), vec(A, 0, H - 0.1), 'Steel')
        lt = box((0.62, 0.22, 0.02), vec(A, 0, H - 0.17), 'Light Warm')
        g.out(join(pole, base, arm, head, lt))
    GR['lamp'] = g.tree


def build_traffic():
    with Group("CT Traffic Light", [("Height", 'FLOAT', 3.4)],
               description="Signal pole, lamps face -Y") as g:
        H = g["Height"]
        pole = setmat(xform(cyl(0.065, H, 10), vec(0, 0, H * 0.5)), 'Steel')
        head = box((0.32, 0.26, 0.95), vec(0, 0, H - 0.3), 'Steel')
        visor = setmat(xform(cyl(0.11, 0.06, 14), (0, 0, 0), (PI / 2, 0, 0)), 'Dark')
        red = setmat(xform(cyl(0.085, 0.07, 14), (0, 0, 0), (PI / 2, 0, 0)), 'Signal')
        grn = setmat(xform(cyl(0.085, 0.07, 14), (0, 0, 0), (PI / 2, 0, 0)), 'Signal Green')
        lamps = join(xform(red, vec(0, -0.14, H - 0.02)), xform(visor, vec(0, -0.14, H - 0.3)),
                     xform(grn, vec(0, -0.14, H - 0.58)))
        ped = box((0.28, 0.2, 0.4), vec(0, -0.05, H * 0.55), 'Steel')
        ped_l = box((0.18, 0.02, 0.12), vec(0, -0.16, H * 0.55 + 0.08), 'Signal')
        g.out(join(pole, head, lamps, ped, ped_l))
    GR['traffic'] = g.tree


def build_barrier():
    with Group("CT Barrier", [("Length", 'FLOAT', 2.0)], description="Jersey barrier") as g:
        L = g["Length"]
        c = xform(cube(vec(L, 0.6, 0.82)), (0, 0, 0.41))
        _, py, pz = sep(position())
        c = setpos(c, pz > 0.5, None, vec(0, py * -0.6, 0))
        g.out(setmat(c, 'Barrier'))
    GR['barrier'] = g.tree


def build_street():
    with Group("CT Street", [("Width", 'FLOAT', 16.0), ("Depth", 'FLOAT', 12.0),
                             ("Margin", 'FLOAT', 3.0), ("Seed", 'INT', 0),
                             ("Props", 'BOOL', True)],
               description="Plinth with paving, rails, street lights, signals, barriers") as g:
        W, D, m, sd = g["Width"], g["Depth"], g["Margin"], g["Seed"]
        PW = W + m * 2.0
        PD = D + m * 2.0
        plinth = box(vec(PW, PD, 0.3), (0, 0, -0.15), 'Concrete Dark')
        trim = box(vec(PW + 0.08, PD + 0.08, 0.08), (0, 0, -0.04), 'Steel')
        npv = fmax(flo(PD / 1.5), 1)
        paving = inst(mline(npv, vec(0, PD * -0.5 + 1.5, 0.003), (0, 1.5, 0)),
                      box(vec(PW - 0.1, 0.03, 0.01), (0, 0, 0), 'Groove'))
        gap = rnd(3.0, 5.0, 1, sd)
        fl = (PW - gap) * 0.5
        rails = join(
            xform(railing(fl, 1.0, 1.8), vec(PW * -0.5 + fl * 0.5, PD * -0.5 + 0.12, 0)),
            xform(railing(fl, 1.0, 1.8), vec(PW * 0.5 - fl * 0.5, PD * -0.5 + 0.12, 0)),
            xform(railing(PW, 1.0, 1.8), vec(0, PD * 0.5 - 0.12, 0)),
            xform(railing(PD, 1.0, 1.8), vec(PW * -0.5 + 0.12, 0, 0), (0, 0, PI / 2)),
            xform(railing(PD, 1.0, 1.8), vec(PW * 0.5 - 0.12, 0, 0), (0, 0, PI / 2)))
        i = index()
        ix, iy = grid_fields(2)
        lx = (ix - 0.5) * (PW - 0.6)
        ly = (iy - 0.5) * (PD - 0.6)
        lamp_pts = points(4, vec(lx, ly, 0))
        lamps = inst(lamp_pts, call(GR['lamp'], {'Height': rnd(6.0, 7.5, 2, sd), 'Arm': 1.6}),
                     rot=vec(0, 0, fm('ARCTAN2', ly * -1.0, lx * -1.0)))
        mids = points(2, vec(0, (i - 0.5) * (PD - 0.6), 0))
        lamps2 = inst(mids, call(GR['lamp'], {'Height': 6.5, 'Arm': 1.6}),
                      rot=vec(0, 0, (i - 0.5) * PI * -1.0), sel=PW > 18.0)
        tl = inst(points(2, vec((i - 0.5) * (PW + 1.0), PD * -0.5 - 0.6, -0.3)),
                  call(GR['traffic'], {'Height': 3.6}))
        nbar = rndi(2, 4, 3, sd)
        bars = inst(points(nbar, vec(rnd(PW * -0.4, PW * 0.4, None, sd + 4), PD * -0.5 + 0.9, 0)),
                    call(GR['barrier'], {'Length': 2.0}),
                    rot=vec(0, 0, rnd(-0.15, 0.15, None, sd + 5)))
        nbo = fmax(flo(gap / 1.1), 1)
        bol = inst(points(nbo, vec(gap * -0.5 + (i + 0.5) * (gap / nbo), PD * -0.5 + 0.35, 0.45)),
                   setmat(cyl(0.09, 0.9, 10), 'Steel Light'))
        props = join(rails, lamps, lamps2, tl, bars, bol)
        g.out(join(plinth, trim, paving, switch(g["Props"], None, props)))
    GR['street'] = g.tree


# ---------------------------------------------------------------------------
#  Main group
# ---------------------------------------------------------------------------

def build_main():
    ins = [
        "#Layout",
        ("Seed", 'INT', 9, 0, 1000000, "Changes every random choice"),
        ("Width", 'FLOAT', 16.0, 6.0, 80.0, "Footprint size along X"),
        ("Depth", 'FLOAT', 12.0, 6.0, 80.0, "Footprint size along Y"),
        ("Mass Split", 'FLOAT', 0.42, 0.2, 0.8, "Where the front and back facades split into two masses"),
        ("Ground Height", 'FLOAT', 5.6, 3.0, 12.0),
        ("Floor Height", 'FLOAT', 3.3, 2.6, 6.0),
        ("Blocks", 'INT', 3, 1, 10, "Number of stacked facade blocks above the ground floor"),
        ("Floors Min", 'INT', 2, 1, 8, "Minimum floors per block"),
        ("Floors Max", 'INT', 3, 1, 8, "Maximum floors per block"),
        ("Sign Level After", 'INT', 1, 0, 10, "Open neon level after this block (0 = none)"),
        ("Sign Level Height", 'FLOAT', 2.6, 1.6, 6.0),
        ("Roof Level", 'BOOL', True, None, None, "Open plant level at the top"),
        ("Roof Level Height", 'FLOAT', 3.6, 2.0, 8.0),
        "#Facade Mix",
        ("Louvers", 'FLOAT', 1.4, 0.0, 10.0),
        ("Fins", 'FLOAT', 0.9, 0.0, 10.0),
        ("Concrete", 'FLOAT', 0.8, 0.0, 10.0),
        ("Vents", 'FLOAT', 0.5, 0.0, 10.0),
        ("Annex", 'FLOAT', 0.45, 0.0, 10.0),
        ("Stair Tower", 'FLOAT', 0.15, 0.0, 10.0),
        ("Row Coherence", 'FLOAT', 0.35, 0.0, 1.0, "Chance that a whole row shares one facade type"),
        ("Bracing Chance", 'FLOAT', 0.55, 0.0, 1.0),
        ("Pod Chance", 'FLOAT', 0.3, 0.0, 1.0),
        ("Screen Chance", 'FLOAT', 0.35, 0.0, 1.0),
        "#Structure",
        ("Column Size", 'FLOAT', 0.55, 0.2, 1.5),
        ("Girder Depth", 'FLOAT', 0.62, 0.2, 1.5),
        ("Brace Size", 'FLOAT', 0.32, 0.1, 0.8),
        ("Core Inset", 'FLOAT', 1.6, 0.5, 4.0, "Set back of the glazed core on open levels"),
        "#Signage",
        ("Sign Text", 'STR', "NORTH CLOUD SECURITY"),
        ("Shop Text", 'STR', "SKY"),
        ("Corner Sign", 'STR', "21", None, None, "Box sign on the front corner (empty = none)"),
        ("Sign Offset", 'FLOAT', 0.0, -1000.0, 1000.0, "Slides the neon text around the building"),
        "#Roof",
        ("Mast", 'BOOL', True),
        ("Antennas", 'INT', 2, 0, 8),
        ("Dish", 'BOOL', True),
        ("HVAC Density", 'FLOAT', 0.6, 0.0, 1.0),
        "#Street",
        ("Street", 'BOOL', True),
        ("Street Props", 'BOOL', True),
        ("Plinth Margin", 'FLOAT', 3.0, 0.5, 15.0),
        "#Output",
        ("Realize", 'BOOL', True, None, None, "Realize instances (turn off for faster viewport)"),
    ]
    with Group(MAIN_GROUP, ins, modifier=True,
               description="Procedural steel-frame tower generator") as g:
        seed = g["Seed"]
        W, D = g["Width"], g["Depth"]
        fh, gh = g["Floor Height"], g["Ground Height"]
        B = g["Blocks"]
        xs = W * -0.5 + W * g["Mass Split"]

        # ---------------- rows ----------------
        S_ = fmin(g["Sign Level After"], B)
        has_sign = S_ > 0.5
        roof_on = g["Roof Level"]
        R = 1 + B + switch(has_sign, 0, 1, 'INT') + switch(roof_on, 0, 1, 'INT')
        r = index()
        is_ground = ieq(r, 0)
        is_roof = roof_on & ieq(r, R - 1)
        is_sign = has_sign & ieq(r, S_ + 1)
        kind = switch(is_ground, switch(is_roof, switch(is_sign, 1, 2, 'INT'), 3, 'INT'), 0, 'INT')
        fmin_ = g["Floors Min"]
        floors = rndi(fmin_, fmax(g["Floors Max"], fmin_), r, seed + 101)
        floors = switch(ieq(kind, 1), 1, floors, 'INT')
        hrow = iswitch(kind, [gh, floors * fh, g["Sign Level Height"], g["Roof Level Height"]],
                       'FLOAT')
        rows = points(R)
        rows = store(rows, "h", hrow)
        rows = store(rows, "kind", kind, 'INT')
        rows = store(rows, "floors", floors, 'INT')
        acc = node('GeometryNodeAccumulateField', {'Value': named("h")}, data_type='FLOAT')
        rows = setpos(rows, None, vec(0, 0, acc['Trailing']))
        top = node('GeometryNodeAttributeStatistic', {'Geometry': rows, 'Attribute': named("h")})['Sum']

        # ---------------- panels ----------------
        pi_ = index()
        pr = flo(pi_ / 6)
        ps = fm('FLOORED_MODULO', pi_, 6)

        def sample(name, dt='FLOAT'):
            return node('GeometryNodeSampleIndex',
                        {'Geometry': rows, 'Value': named(name, dt), 'Index': pr},
                        data_type=dt, domain='POINT')

        p_h = sample("h")
        p_kind = sample("kind", 'INT')
        p_fl = sample("floors", 'INT')
        _, _, p_z = sep(node('GeometryNodeSampleIndex',
                             {'Geometry': rows, 'Value': position(), 'Index': pr},
                             data_type='FLOAT_VECTOR', domain='POINT'))
        wa = xs + W * 0.5
        wb = W * 0.5 - xs
        face = iswitch(ps, [0, 0, 1, 2, 2, 3], 'INT')
        p_u = iswitch(ps, [(W * -0.5 + xs) * 0.5, (xs + W * 0.5) * 0.5, 0.0,
                           (xs + W * 0.5) * -0.5, (W * 0.5 - xs) * 0.5, 0.0], 'FLOAT')
        p_w = iswitch(ps, [wa, wb, D, wb, wa, D], 'FLOAT')
        e = SIGN_OFFSET
        p_off = iswitch(ps, [0.0, wa, W + e * 2.0, W + D + e * 4.0, W + D + e * 4.0 + wb,
                             W * 2.0 + D + e * 6.0], 'FLOAT') + g["Sign Offset"]

        # facade type pick
        coh = chance(g["Row Coherence"], pr, seed + 202)
        tid = switch(coh, pi_ + 1000, pr, 'INT')
        wts = [g["Louvers"], g["Fins"], g["Concrete"], g["Vents"], g["Annex"], g["Stair Tower"]]
        tot = wts[0] + wts[1] + wts[2] + wts[3] + wts[4] + wts[5]
        rv = rnd(0.0, fmax(tot, 0.0001), tid, seed + 303)
        acc_w = 0.0
        pick = None
        for k in range(5):
            acc_w = wts[k] + acc_w if k else wts[0]
            c = switch(rv > acc_w, 0, 1, 'INT')
            pick = c if pick is None else pick + c
        block_type = iswitch(pick, [T_LOUVER, T_FINS, T_CONCRETE, T_VENTS, T_ANNEX, T_STAIR], 'INT')
        gpick = rndi(0, 4, pi_, seed + 404)
        ground_type = iswitch(gpick, [T_STAIR, T_BUTTRESS, T_LOADING, T_BUTTRESS, T_CONCRETE], 'INT')
        ground_type = switch(ieq(ps, 1), ground_type, T_SHOP, 'INT')
        roof_type = switch(chance(0.3, pi_, seed + 505), T_ROOF_OPEN, T_ROOF_CORE, 'INT')
        p_type = iswitch(p_kind, [ground_type, block_type, T_SIGN, roof_type], 'INT')

        braceable = (p_type < 3.5) & ieq(p_kind, 1)
        brace = braceable & chance(g["Bracing Chance"], pi_, seed + 606)
        pattern = rndi(0, 3, pi_, seed + 707)
        pods = braceable & chance(g["Pod Chance"], pi_, seed + 808) & (p_h > 5.0)
        pseed = rndi(0, 999999, pi_, seed + 909)

        panels = points(R * 6)
        attrs = [("w", p_w, 'FLOAT'), ("ph", p_h, 'FLOAT'), ("pz", p_z, 'FLOAT'),
                 ("pu", p_u, 'FLOAT'), ("face", face, 'INT'), ("ptype", p_type, 'INT'),
                 ("pfl", p_fl, 'INT'), ("pseed", pseed, 'INT'), ("brace", brace, 'BOOLEAN'),
                 ("pat", pattern, 'INT'), ("pods", pods, 'BOOLEAN'), ("poff", p_off, 'FLOAT')]

        # ---------------- for each panel ----------------
        nt = _tree()
        fi = nt.nodes.new('GeometryNodeForeachGeometryElementInput')
        fo = nt.nodes.new('GeometryNodeForeachGeometryElementOutput')
        fi.pair_with_output(fo)
        fo.domain = 'POINT'
        types = {'FLOAT': 'FLOAT', 'INT': 'INT', 'BOOLEAN': 'BOOLEAN'}
        for name, _, dt in attrs:
            fo.input_items.new(types[dt], name)
        _link(panels.s, fi.inputs['Geometry'])
        for name, v, dt in attrs:
            _set(fi.inputs[name], v)
        it = {name: S(fi.outputs[name]) for name, _, _ in attrs}
        pg = call(GR['panel'], {
            'Width': it["w"], 'Height': it["ph"], 'Floors': it["pfl"], 'Floor Height': fh,
            'Seed': it["pseed"], 'Type': it["ptype"], 'Brace': it["brace"],
            'Brace Pattern': it["pat"], 'Brace Size': g["Brace Size"], 'Pods': it["pods"],
            'Screen Chance': g["Screen Chance"], 'Sign Text': g["Sign Text"],
            'Shop Text': g["Shop Text"], 'Text Offset': it["poff"]})
        pg = realize(pg)
        pg = store(pg, "ct_rand", rnd(0.0, 1.0, 5, it["pseed"]))
        f = it["face"]
        origin = iswitch(f, [vec(0, D * -0.5, 0), vec(W * 0.5, 0, 0), vec(0, D * 0.5, 0),
                             vec(W * -0.5, 0, 0)], 'VECTOR')
        fdir = iswitch(f, [(1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0)], 'VECTOR')
        tr = origin + fdir * it["pu"] + vec(0, 0, it["pz"])
        pg = xform(pg, tr, vec(0, 0, f * (PI / 2)))
        _link(pg.s, fo.inputs['Generation_0'])
        facades = S(fo.outputs['Generation_0'])

        # ---------------- global parts ----------------
        frame = call(GR['frame'], {'Rows': rows, 'Width': W, 'Depth': D, 'Split X': xs,
                                   'Top': top, 'Column Size': g["Column Size"],
                                   'Girder Depth': g["Girder Depth"],
                                   'Core Inset': g["Core Inset"]})
        roof = call(GR['roof'], {'Width': W, 'Depth': D, 'Split X': xs, 'Seed': seed,
                                 'Mast': g["Mast"], 'Antennas': g["Antennas"],
                                 'Dish': g["Dish"], 'HVAC Density': g["HVAC Density"]})
        roof = xform(roof, vec(0, 0, top))
        street = call(GR['street'], {'Width': W, 'Depth': D, 'Margin': g["Plinth Margin"],
                                     'Seed': seed, 'Props': g["Street Props"]})
        street = switch(g["Street"], None, street)
        blade = call(GR['blade'], {'Text': g["Corner Sign"], 'Size': 1.1})
        blade = xform(blade, vec(W * 0.5 + 0.18 + 1.25, D * -0.5 - 0.18, gh - 1.6))
        blade = switch(node('FunctionNodeStringLength', g["Corner Sign"]) > 0.5, None, blade)
        allg = join(facades, frame, roof, street, blade)
        allg = switch(g["Realize"], allg, realize(allg))
        allg = node('GeometryNodeSetShadeSmooth', {'Geometry': allg, 'Shade Smooth': False},
                    domain='FACE')
        g.out(allg)
    return g.tree


# ---------------------------------------------------------------------------
#  Build everything
# ---------------------------------------------------------------------------

BUILDERS = [
    build_box, build_ibeam, build_beams, build_railing, build_ceiling_lights,
    build_grooves, build_label, build_neon_text,
    build_facade_concrete, build_facade_louver, build_facade_fins, build_ac_unit,
    build_facade_vents, build_pod, build_pods, build_facade_annex, build_bracing,
    build_ground_shop, build_stair, build_ground_buttress, build_ground_loading,
    build_level_sign, build_level_roof, build_panel,
    build_frame, build_hvac, build_mast, build_whip, build_dish, build_roof,
    build_blade_sign, build_lamp, build_traffic, build_barrier, build_street,
]


def build_all():
    """(Re)builds materials and every node group. Returns the main group."""
    build_materials()
    GR.clear()
    for b in BUILDERS:
        b()
    main = build_main()
    main["ct_version"] = GROUP_VERSION
    return main


def ensure_groups():
    main = bpy.data.node_groups.get(MAIN_GROUP)
    if main is not None and main.get("ct_version") == GROUP_VERSION:
        return main
    return build_all()


def add_tower(context=None, seed=9, name="CyberTower", location=(0, 0, 0)):
    tree = ensure_groups()
    me = bpy.data.meshes.new(name)
    ob = bpy.data.objects.new(name, me)
    coll = context.collection if context is not None else bpy.context.scene.collection
    coll.objects.link(ob)
    ob.location = location
    mod = ob.modifiers.new("Cyber Tower", 'NODES')
    mod.node_group = tree
    set_input(mod, "Seed", seed)
    return ob


def _input_items(group):
    return [it for it in group.interface.items_tree
            if getattr(it, "item_type", None) == 'SOCKET' and it.in_out == 'INPUT']


def _set_ident(mod, ident, value):
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "inputs"):   # Blender 5.x
        getattr(props.inputs, ident).value = value
    else:                                                # Blender 4.x
        mod[ident] = value


def _get_ident(mod, ident):
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "inputs"):
        return getattr(props.inputs, ident).value
    return mod[ident]


def set_input(mod, name, value):
    """Set a modifier input by its interface name (works in 4.x and 5.x)."""
    for item in _input_items(mod.node_group):
        if item.name == name:
            _set_ident(mod, item.identifier, value)
            mod.node_group.interface_update(bpy.context)
            return
    raise KeyError(name)


def get_input(mod, name):
    for item in _input_items(mod.node_group):
        if item.name == name:
            return _get_ident(mod, item.identifier)
    raise KeyError(name)


# ---------------------------------------------------------------------------
#  Add-on registration
# ---------------------------------------------------------------------------

class CT_OT_add_tower(bpy.types.Operator):
    """Add a procedural Cyber Tower (Geometry Nodes)"""
    bl_idname = "mesh.cyber_tower_add"
    bl_label = "Cyber Tower"
    bl_options = {'REGISTER', 'UNDO'}

    seed: bpy.props.IntProperty(name="Seed", default=9, min=0)

    def execute(self, context):
        for o in context.selected_objects:
            o.select_set(False)
        ob = add_tower(context, self.seed, location=context.scene.cursor.location)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {'FINISHED'}


class CT_OT_rebuild_groups(bpy.types.Operator):
    """Rebuild all Cyber Tower node groups and materials from the add-on"""
    bl_idname = "object.cyber_tower_rebuild"
    bl_label = "Rebuild Cyber Tower Node Groups"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        users = [m for o in bpy.data.objects for m in o.modifiers
                 if m.type == 'NODES' and m.node_group and m.node_group.name == MAIN_GROUP]
        values = []
        for m in users:
            vals = {}
            for it in _input_items(m.node_group):
                try:
                    v = _get_ident(m, it.identifier)
                    vals[it.name] = v[:] if hasattr(v, "__len__") and not isinstance(v, str) else v
                except Exception:
                    pass
            values.append(vals)
        tree = build_all()
        for m, vals in zip(users, values):
            m.node_group = tree
            for k, v in vals.items():
                try:
                    set_input(m, k, v)
                except Exception:
                    pass
        return {'FINISHED'}


def _menu(self, context):
    self.layout.operator(CT_OT_add_tower.bl_idname, icon='MOD_BUILD')


def _object_menu(self, context):
    self.layout.separator()
    self.layout.operator(CT_OT_rebuild_groups.bl_idname, icon='FILE_REFRESH')


_classes = (CT_OT_add_tower, CT_OT_rebuild_groups)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.VIEW3D_MT_mesh_add.append(_menu)
    bpy.types.VIEW3D_MT_object.append(_object_menu)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(_object_menu)
    bpy.types.VIEW3D_MT_mesh_add.remove(_menu)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
