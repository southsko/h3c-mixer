#!/usr/bin/env python3
"""
GLB -> Bambu Studio painted 3MF (MakerLab "Image to 3D" GLB export, or any
textured / vertex-coloured glTF binary).

The GLB keeps the full-resolution texture, so this is the better source than
MakerLab's own 3MF export (which snaps colours to the Bambu catalog first).

  * samples the base-colour texture (or vertex colours) for every triangle
  * reduces the colours to N (default 20) with area-weighted k-means in OKLab
  * cleans up lone speckles (majority filter over neighbouring triangles)
  * welds UV-seam vertices so the mesh is watertight for slicing
  * scales to --size mm (longest side, default 100), Y-up -> Z-up, sits on the bed
  * writes <name>_<N>colors.3mf: one painted object, N filaments with the found
    colours, H3C 0.4 / 0.12mm High Quality settings

Drop the result on h3c_mix.py (or the .bat) to turn those colours into PLA Palette
CMYKW mixes. Dropping a .glb on h3c_mix.py / the .bat does both steps at once.

Usage:  python glb2bambu.py model.glb [--colors 20] [--size 100] [--no-smooth] [--no-pause]
Needs:  numpy and Pillow   (pip install numpy pillow)
"""
import io, json, os, struct, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import numpy as np
    from PIL import Image
except ImportError:                                    # pragma: no cover
    np = None

import h3c_mix  # noqa: E402  build_settings, BASE_FILE

DEFAULT_COLORS = 20
DEFAULT_SIZE = 100.0       # mm, longest side
MIN_SHARE = 0.002          # clusters covering less of the surface than this get merged
MIN_DIST = 0.045           # OKLab distance under which two palette colours count as look-alikes
SMOOTH_PASSES = 4          # majority filter over neighbouring triangles, after clustering
BLUR_PASSES = 3            # neighbour colour averaging, before clustering
BED_CENTER = (165.0, 160.0)
COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
        5125: np.uint32, 5126: np.float32} if np else {}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


# ------------------------------------------------------------------ GLB reading
def read_glb(path):
    data = open(path, "rb").read()
    magic, ver, _ = struct.unpack("<4sII", data[:12])
    if magic != b"glTF" or ver != 2:
        raise SystemExit("Not a glTF 2.0 binary (.glb).")
    off, js, binc = 12, None, b""
    while off < len(data):
        ln, typ = struct.unpack("<II", data[off:off + 8])
        chunk = data[off + 8:off + 8 + ln]
        if typ == 0x4E4F534A:
            js = json.loads(chunk)
        elif typ == 0x004E4942:
            binc = chunk
        off += 8 + ln
    return js, binc


def accessor(js, binc, idx):
    a = js["accessors"][idx]
    bv = js["bufferViews"][a["bufferView"]]
    dt = np.dtype(COMP[a["componentType"]]).newbyteorder("<")
    n = NCOMP[a["type"]]
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", 0)
    if stride and stride != dt.itemsize * n:
        raw = np.frombuffer(binc, dtype=np.uint8, count=stride * (a["count"] - 1) + dt.itemsize * n, offset=start)
        out = np.lib.stride_tricks.as_strided(raw, shape=(a["count"], dt.itemsize * n), strides=(stride, 1))
        arr = np.frombuffer(out.copy().tobytes(), dtype=dt).reshape(a["count"], n)
    else:
        arr = np.frombuffer(binc, dtype=dt, count=a["count"] * n, offset=start).reshape(a["count"], n)
    if a.get("normalized"):
        arr = arr.astype(np.float32) / np.iinfo(dt).max
    return arr


def image(js, binc, tex_idx):
    src = js["textures"][tex_idx]["source"]
    im = js["images"][src]
    if "bufferView" not in im:
        raise SystemExit("Texture is an external file; only embedded GLB textures are supported.")
    bv = js["bufferViews"][im["bufferView"]]
    o = bv.get("byteOffset", 0)
    return np.asarray(Image.open(io.BytesIO(binc[o:o + bv["byteLength"]])).convert("RGB"))


def node_matrices(js):
    """World matrix per mesh index (first instance)."""
    def local(nd):
        if "matrix" in nd:
            return np.array(nd["matrix"], dtype=np.float64).reshape(4, 4).T
        m = np.eye(4)
        if "scale" in nd:
            m = np.diag(list(nd["scale"]) + [1.0]) @ m
        if "rotation" in nd:
            x, y, z, w = nd["rotation"]
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            rr = np.eye(4); rr[:3, :3] = r; m = rr @ m
        if "translation" in nd:
            t = np.eye(4); t[:3, 3] = nd["translation"]; m = t @ m
        return m
    out = {}
    scene = js.get("scenes", [{}])[js.get("scene", 0)] if js.get("scenes") else {"nodes": list(range(len(js.get("nodes", []))))}

    def walk(i, parent):
        nd = js["nodes"][i]
        w = parent @ local(nd)
        if "mesh" in nd and nd["mesh"] not in out:
            out[nd["mesh"]] = w
        for c in nd.get("children", []):
            walk(c, w)
    for i in scene.get("nodes", []):
        walk(i, np.eye(4))
    return out


def load_colored_mesh(path):
    """-> positions (V,3) float64, faces (F,3) int64, face RGB (F,3) uint8"""
    js, binc = read_glb(path)
    mats = node_matrices(js)
    P, Fc, C = [], [], []
    vbase = 0
    for mi, mesh in enumerate(js["meshes"]):
        M = mats.get(mi, np.eye(4))
        for prim in mesh["primitives"]:
            if prim.get("mode", 4) != 4:
                continue
            at = prim["attributes"]
            pos = accessor(js, binc, at["POSITION"]).astype(np.float64)
            pos = (np.c_[pos, np.ones(len(pos))] @ M.T)[:, :3]
            idx = (accessor(js, binc, prim["indices"]).reshape(-1, 3).astype(np.int64)
                   if "indices" in prim else np.arange(len(pos)).reshape(-1, 3))
            col = face_colors(js, binc, prim, at, idx, len(pos))
            P.append(pos); Fc.append(idx + vbase); C.append(col)
            vbase += len(pos)
    if not P:
        raise SystemExit("No triangle meshes in this GLB.")
    return np.vstack(P), np.vstack(Fc), np.vstack(C)


def face_colors(js, binc, prim, at, idx, nverts):
    mat = js.get("materials", [{}])[prim["material"]] if "material" in prim else {}
    pbr = mat.get("pbrMetallicRoughness", {})
    factor = np.array(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3])
    if "baseColorTexture" in pbr and "TEXCOORD_%d" % pbr["baseColorTexture"].get("texCoord", 0) in at:
        tex = image(js, binc, pbr["baseColorTexture"]["index"])
        uv = accessor(js, binc, at["TEXCOORD_%d" % pbr["baseColorTexture"].get("texCoord", 0)]).astype(np.float64)
        H, W = tex.shape[:2]
        t = uv[idx]                                            # (F,3,2)
        # 4 points well inside each triangle (centroid + 3 towards the corners), so
        # samples never fall in the black gaps between UV islands
        bary = np.array([[1 / 3, 1 / 3, 1 / 3], [0.6, 0.2, 0.2], [0.2, 0.6, 0.2], [0.2, 0.2, 0.6]])
        pts = np.einsum("sk,fkd->fsd", bary, t)               # (F,4,2)
        u = np.mod(pts[..., 0], 1.0); v = np.mod(pts[..., 1], 1.0)
        x = np.clip((u * W).astype(np.int64), 0, W - 1)
        y = np.clip((v * H).astype(np.int64), 0, H - 1)      # glTF UV origin is top-left
        s = tex[y, x].astype(np.float64)                      # (F,4,3)
        rgb = np.median(s, axis=1)
    elif "COLOR_0" in at:
        vc = accessor(js, binc, at["COLOR_0"]).astype(np.float64)[:, :3]
        if vc.max() <= 1.0:
            vc = vc * 255
        rgb = vc[idx].mean(axis=1)
    else:
        rgb = np.tile(factor * 255, (len(idx), 1))
    return np.clip(rgb * factor, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ colour maths
def srgb_to_oklab(rgb):
    c = rgb / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    l = lin @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                        [0.5363325363, 0.6806995451, 0.2817188376],
                        [0.0514459929, 0.1073969566, 0.6299787005]])
    l = np.cbrt(l)
    return l @ np.array([[0.2104542553, 1.9779984951, 0.0259040371],
                         [0.7936177850, -2.4285922050, 0.7827717662],
                         [-0.0040720468, 0.4505937099, -0.8086757660]])


def oklab_to_srgb(lab):
    l = lab @ np.array([[1.0, 1.0, 1.0],
                        [0.3963377774, -0.1055613458, -0.0894841775],
                        [0.2158037573, -0.0638541728, -1.2914855480]])
    l = l ** 3
    lin = l @ np.array([[4.0767416621, -1.2684380046, -0.0041960863],
                        [-3.3077115913, 2.6097574011, -0.7034186147],
                        [0.2309699292, -0.3413193965, 1.7076147010]])
    lin = np.clip(lin, 0, 1)
    c = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(np.round(c * 255), 0, 255).astype(int)


def kmeans(X, w, k, iters=25, seed=1):
    rng = np.random.default_rng(seed)
    sub = rng.choice(len(X), size=min(len(X), 200000), replace=False, p=w / w.sum())
    S = X[sub]
    cent = [S[rng.integers(len(S))]]
    d2 = ((S - cent[0]) ** 2).sum(1)
    for _ in range(1, k):                                      # k-means++ init
        cent.append(S[rng.choice(len(S), p=d2 / d2.sum())])
        d2 = np.minimum(d2, ((S - cent[-1]) ** 2).sum(1))
    cent = np.array(cent)
    for _ in range(iters):
        lab = assign(X, cent)
        new = np.array([np.average(X[lab == j], axis=0, weights=w[lab == j]) if (lab == j).any() else cent[j]
                        for j in range(k)])
        if np.allclose(new, cent, atol=1e-5):
            break
        cent = new
    return cent, assign(X, cent)


def merge_close(cent, weight, min_dist):
    """Agglomerate centroids closer than min_dist (OKLab). Returns old->group index map."""
    groups = [[i] for i in range(len(cent))]
    C = [cent[i].copy() for i in range(len(cent))]
    W = [float(weight[i]) for i in range(len(cent))]
    while len(C) > 1:
        best, bi, bj = None, -1, -1
        for i in range(len(C)):
            for j in range(i + 1, len(C)):
                d = float(np.sqrt(((C[i] - C[j]) ** 2).sum()))
                if best is None or d < best:
                    best, bi, bj = d, i, j
        if best >= min_dist:
            break
        w = W[bi] + W[bj]
        C[bi] = (C[bi] * W[bi] + C[bj] * W[bj]) / max(w, 1e-12)
        W[bi] = w
        groups[bi] += groups[bj]
        del C[bj], W[bj], groups[bj]
    m = np.empty(len(cent), dtype=np.int64)
    for g, members in enumerate(groups):
        m[members] = g
    return m


def distinct_palette(lab, w, n, min_dist):
    """k-means, then merge look-alike clusters and spend the freed slots on new ones,
    until there are n visibly different colours (or it stops improving)."""
    k = n
    best = None
    for _ in range(6):
        cent, labels = kmeans(lab, w, k)
        share = np.bincount(labels, weights=w, minlength=k)
        m = merge_close(cent, share, min_dist)
        labels = m[labels]
        g = int(m.max()) + 1
        if best is None or g > best[0]:
            best = (g, labels)
        if g >= n or k >= 4 * n:
            break
        k += n - g
    g, labels = best
    if g > n:                       # overshoot: final k-means on the merged centroids
        cent = np.array([np.average(lab[labels == j], axis=0, weights=w[labels == j]) for j in range(g)])
        m = merge_close(cent, np.bincount(labels, weights=w), 1e9 if n == 1 else min_dist)
        while int(m.max()) + 1 > n:
            min_dist *= 1.15
            m = merge_close(cent, np.bincount(labels, weights=w), min_dist)
        labels = m[labels]
    return labels


def assign(X, cent):
    out = np.empty(len(X), dtype=np.int64)
    for i in range(0, len(X), 200000):
        out[i:i + 200000] = ((X[i:i + 200000, None, :] - cent[None]) ** 2).sum(-1).argmin(1)
    return out


# ------------------------------------------------------------------ mesh helpers
def weld(P, F):
    key = np.round(P, 6)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    F = inv.reshape(-1)[F]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])   # drop collapsed tris
    return uniq, F, keep


def face_neighbors(F):
    """(F,3) neighbour index across each edge, -1 if none."""
    e = np.stack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]], axis=1).reshape(-1, 2)
    e = np.sort(e, axis=1)
    fid = np.repeat(np.arange(len(F)), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    es, fs = e[order], fid[order]
    same = (es[1:] == es[:-1]).all(1)
    nb = np.full(len(e), -1, dtype=np.int64)
    a, b = order[:-1][same], order[1:][same]
    nb[a] = fid[b]; nb[b] = fid[a]
    return nb.reshape(-1, 3)


def blur_colors(lab, area, nb, passes):
    """Average each triangle's colour with its edge neighbours (area-weighted), a few
    times over: removes texture grain (pores, baked shadow noise) before clustering."""
    has = nb >= 0
    nbi = np.maximum(nb, 0)
    for _ in range(passes):
        w = area[nbi] * has                                     # (F,3)
        tot = area + w.sum(1)
        lab = (lab * area[:, None] + (lab[nbi] * w[..., None]).sum(1)) / tot[:, None]
    return lab


def smooth(labels, nb, passes):
    for _ in range(passes):
        n = np.where(nb >= 0, labels[np.maximum(nb, 0)], -1)
        # a triangle whose 2+ neighbours agree on a different colour takes it
        a, b, c = n[:, 0], n[:, 1], n[:, 2]
        maj = np.where((a == b) & (a >= 0), a, np.where((b == c) & (b >= 0), b, np.where((a == c) & (a >= 0), a, -1)))
        change = (maj >= 0) & (maj != labels)
        labels = np.where(change, maj, labels)
    return labels


def paint_code(state):
    """Bambu/Prusa TriangleSelector leaf code for filament `state` (1-based)."""
    if state < 3:
        return "%X" % (state << 2)
    n = state - 3
    nib = [0b1100] + [0xF] * (n // 15) + [n % 15]
    return "".join("%X" % x for x in reversed(nib))


# ------------------------------------------------------------------ 3MF writing
MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
              ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n')


def write_3mf(out, P, F, labels, colours, name, base):
    n = len(colours)
    codes = np.array([paint_code(i + 1) for i in range(n)])
    vtxt = "\n".join("     <vertex x=\"%.5f\" y=\"%.5f\" z=\"%.5f\"/>" % tuple(p) for p in P)
    ttxt = "\n".join('     <triangle v1="%d" v2="%d" v3="%d" paint_color="%s"/>' % (a, b, c, s)
                     for (a, b, c), s in zip(F.tolist(), codes[labels].tolist()))
    obj = (MODEL_HEAD + ' <resources>\n  <object id="1" p:UUID="00010000-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
           '   <mesh>\n    <vertices>\n' + vtxt + '\n    </vertices>\n    <triangles>\n' + ttxt +
           '\n    </triangles>\n   </mesh>\n  </object>\n </resources>\n</model>\n')
    main = (MODEL_HEAD + ' <metadata name="Application">BambuStudio-%s</metadata>\n' % base["studio_version"] +
            ' <metadata name="Title">%s</metadata>\n' % h3c_escape(name) +
            ' <resources>\n  <object id="2" p:UUID="00000002-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n   <components>\n'
            '    <component p:path="/3D/Objects/object_1.model" objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" '
            'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n   </components>\n  </object>\n </resources>\n'
            ' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n'
            '  <item objectid="2" p:UUID="00000002-b1ec-4553-aec9-835e5b724bb4" transform="1 0 0 0 1 0 0 0 1 %.3f %.3f 0" printable="1"/>\n'
            ' </build>\n</model>\n' % BED_CENTER)
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <object id="2">\n'
          '    <metadata key="name" value="%s"/>\n    <metadata key="extruder" value="1"/>\n'
          '    <metadata face_count="%d"/>\n    <part id="1" subtype="normal_part">\n'
          '      <metadata key="name" value="%s"/>\n'
          '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
          '      <metadata key="extruder" value="1"/>\n'
          '      <mesh_stat face_count="%d" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
          '    </part>\n  </object>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
          '    <metadata key="plater_name" value=""/>\n    <metadata key="locked" value="false"/>\n'
          '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
          '    <metadata key="gcode_file" value=""/>\n    <model_instance>\n'
          '      <metadata key="object_id" value="2"/>\n      <metadata key="instance_id" value="0"/>\n'
          '      <metadata key="identify_id" value="100"/>\n    </model_instance>\n  </plate>\n'
          '  <assemble>\n  </assemble>\n</config>\n') % (h3c_escape(name), len(F), h3c_escape(name), len(F))
    S = h3c_mix.build_settings(base, colours, [], [], n, n)
    S["enable_mixed_color_sublayer"] = "0"
    files = [
        ("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
         ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
         ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
         ' <Default Extension="png" ContentType="image/png"/>\n <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n'),
        ("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
         ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n'),
        ("3D/3dmodel.model", main),
        ("3D/_rels/3dmodel.model.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
         ' <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n'),
        ("3D/Objects/object_1.model", obj),
        ("Metadata/model_settings.config", ms),
        ("Metadata/project_settings.config", json.dumps(S, indent=4)),
        ("Metadata/slice_info.config", '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n'
         '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
         '    <header_item key="X-BBL-Client-Version" value="%s"/>\n  </header>\n</config>\n' % base["studio_version"]),
        ("Metadata/filament_sequence.json", '{"plate_1":{"nozzle_sequence":[],"optimal_assignment":[],"sequence":[]}}'),
    ]
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for nm, data in files:
            z.writestr(nm, data)
    os.replace(tmp, out)


def h3c_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ------------------------------------------------------------------ main flow
def convert(path, n_colors=DEFAULT_COLORS, size_mm=DEFAULT_SIZE, do_smooth=True, base=None):
    if np is None:
        raise SystemExit("glb2bambu needs numpy and Pillow:  pip install numpy pillow")
    base = base or json.load(open(h3c_mix.BASE_FILE))
    print("\n=== %s" % path)
    P, F, rgb = load_colored_mesh(path)
    print("Loaded %d triangles, %d vertices" % (len(F), len(P)))

    # Y-up (glTF) -> Z-up (3MF), scale longest side to size_mm, sit on the bed, centre XY
    P = P[:, [0, 2, 1]] * np.array([1, -1, 1])
    P = P - P.min(0)
    P = P * (size_mm / P.max())
    P[:, 0] -= P[:, 0].max() / 2
    P[:, 1] -= P[:, 1].max() / 2
    P, F, keep = weld(P, F)
    F, rgb = F[keep], rgb[keep]
    ext = P.max(0) - P.min(0)
    print("Welded to %d vertices; size %.1f x %.1f x %.1f mm" % (len(P), ext[0], ext[1], ext[2]))

    v0, v1, v2 = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) + 1e-12
    lab = srgb_to_oklab(rgb.astype(np.float64))
    nb = face_neighbors(F)
    if do_smooth:
        lab = blur_colors(lab, area, nb, BLUR_PASSES)
    k = min(n_colors, max(1, len(np.unique(rgb, axis=0))))
    labels = distinct_palette(lab, area, k, MIN_DIST)
    k = int(labels.max()) + 1
    cent = np.array([np.average(lab[labels == j], axis=0, weights=area[labels == j]) for j in range(k)])

    if do_smooth:
        before = labels.copy()
        labels = smooth(labels, nb, SMOOTH_PASSES)
        print("Speckle cleanup changed %.2f%% of triangles" % (100.0 * (labels != before).mean()))

    # merge near-empty clusters, recompute colours, order by surface share
    share = np.bincount(labels, weights=area, minlength=k) / area.sum()
    small = np.where(share < MIN_SHARE)[0]
    if len(small) and len(small) < k:
        big = np.setdiff1d(np.arange(k), small)
        for s in small:
            tgt = big[((cent[big] - cent[s]) ** 2).sum(1).argmin()]
            labels[labels == s] = tgt
    used = np.unique(labels)
    cent = np.array([np.average(lab[labels == j], axis=0, weights=area[labels == j]) for j in used])
    remap = {j: i for i, j in enumerate(used)}
    labels = np.vectorize(remap.get)(labels)
    share = np.bincount(labels, weights=area) / area.sum()
    order = np.argsort(-share)
    rank = np.empty_like(order); rank[order] = np.arange(len(order))
    labels = rank[labels]; cent = cent[order]; share = share[order]
    colours = ["#%02X%02X%02X" % tuple(c) for c in oklab_to_srgb(cent)]

    out = os.path.splitext(path)[0] + "_%dcolors.3mf" % len(colours)
    write_3mf(out, P, F, labels, colours, os.path.splitext(os.path.basename(path))[0], base)

    print("\n%d colours (by surface share):" % len(colours))
    for i, (c, s) in enumerate(zip(colours, share), 1):
        print("  filament %2d  %s  %5.1f%%" % (i, c, 100 * s))
    if len(small):
        print("(%d tiny colour(s) under %.1f%% merged into their nearest neighbour)" % (len(small), 100 * MIN_SHARE))
    print("Wrote: %s" % out)
    return out


def parse_args(args):
    opts = {"colors": DEFAULT_COLORS, "size": DEFAULT_SIZE, "smooth": True}
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--colors":
            opts["colors"] = int(args[i + 1]); i += 2; continue
        if a == "--size":
            opts["size"] = float(args[i + 1]); i += 2; continue
        if a == "--no-smooth":
            opts["smooth"] = False
        else:
            rest.append(a)
        i += 1
    return opts, rest


def main():
    opts, args = parse_args(sys.argv[1:])
    pause = "--no-pause" not in args
    files = [a for a in args if not a.startswith("--")]
    try:
        if not files:
            print(__doc__)
            return
        for f in files:
            try:
                convert(f, opts["colors"], opts["size"], opts["smooth"])
            except SystemExit as e:
                print("! %s" % e)
            except Exception as e:
                print("! Failed on %s: %r" % (f, e))
    finally:
        if pause:
            input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
