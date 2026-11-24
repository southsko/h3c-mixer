#!/usr/bin/env python3
"""
GLB -> Bambu Studio painted 3MF (MakerLab "Image to 3D" GLB export, or any
textured / vertex-coloured glTF binary).

The GLB keeps the full-resolution texture, so this is the better source than
MakerLab's own 3MF export (which snaps colours to the Bambu catalog first).

  * samples the base-colour texture (or vertex colours) for every triangle
  * measures how many colours the texture needs and suggests a count; a window lets
    you slide 2..32 and see the model repainted live before committing
  * reduces the colours to N with area-weighted k-means in OKLab
  * cleans up lone speckles (majority filter over neighbouring triangles)
  * welds UV-seam vertices so the mesh is watertight for slicing
  * scales to --size mm (longest side, default 100), Y-up -> Z-up, sits on the bed
  * writes <name>_<N>colors.3mf: one painted object, N filaments with the found
    colours, H3C 0.4 / 0.12mm High Quality settings

Drop the result on h3c_mix.py (or the .bat) to turn those colours into PLA Palette
CMYKW mixes. Dropping a .glb on h3c_mix.py / the .bat does both steps at once.

Usage:  python glb2bambu.py model.glb|model.gltf [--colors N] [--auto] [--size 100] [--no-smooth] [--no-pause]
        --colors N  skip the window and use N colours (2..32, Bambu's filament limit)
        --auto      skip the window and use the suggested count
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


# ------------------------------------------------------------------ glTF reading
# Handles .glb and .gltf (embedded, data-URI or side-car buffers/images), every
# node instance with its full transform (mirrored instances keep correct winding),
# triangle lists/strips/fans, sparse and normalized/quantized accessors, sampler
# wrap modes, KHR_texture_transform, texCoord sets, baseColorFactor x texture x
# COLOR_0 (in linear light, as the glTF spec defines), the old
# KHR_materials_pbrSpecularGlossiness diffuse colour, and untextured materials.
# Compressed geometry (Draco / meshopt) and KTX2 textures need an external decoder
# and are reported clearly instead of producing garbage.
UNSUPPORTED_EXT = {
    "KHR_draco_mesh_compression": "Draco-compressed geometry",
    "EXT_meshopt_compression": "meshopt-compressed geometry",
    "KHR_texture_basisu": "KTX2/Basis textures",
}
GL_REPEAT, GL_CLAMP, GL_MIRROR = 10497, 33071, 33648


def read_gltf(path):
    """-> (json, [buffer bytes], base directory)"""
    base_dir = os.path.dirname(os.path.abspath(path))
    data = open(path, "rb").read()
    bufs = []
    if data[:4] == b"glTF":
        ver = struct.unpack("<I", data[4:8])[0]
        if ver != 2:
            raise SystemExit("glTF version %d is not supported (need 2.0)." % ver)
        off, js, binc = 12, None, None
        while off + 8 <= len(data):
            ln, typ = struct.unpack("<II", data[off:off + 8])
            chunk = data[off + 8:off + 8 + ln]
            if typ == 0x4E4F534A:
                js = json.loads(chunk.decode("utf-8"))
            elif typ == 0x004E4942 and binc is None:
                binc = chunk
            off += 8 + ln
        if js is None:
            raise SystemExit("GLB has no JSON chunk.")
    else:
        try:
            js = json.loads(data.decode("utf-8-sig"))
        except Exception:
            raise SystemExit("Not a glTF file (.glb or .gltf).")
        binc = None
    need = [e for e in js.get("extensionsRequired", []) + js.get("extensionsUsed", []) if e in UNSUPPORTED_EXT]
    hard = [e for e in js.get("extensionsRequired", []) if e in UNSUPPORTED_EXT]
    if hard:
        raise SystemExit("This file uses %s, which needs a decoder this tool doesn't have. Re-export it "
                         "uncompressed (e.g. Blender: untick compression; or `npx gltf-transform copy in.glb out.glb`)."
                         % ", ".join(UNSUPPORTED_EXT[e] for e in hard))
    for i, b in enumerate(js.get("buffers", [])):
        uri = b.get("uri")
        if uri is None:
            bufs.append(binc if (i == 0 and binc is not None) else b"")
        else:
            bufs.append(_load_uri(uri, base_dir))
    return js, bufs, base_dir, need


def _load_uri(uri, base_dir):
    if uri.startswith("data:"):
        import base64
        return base64.b64decode(uri.split(",", 1)[1])
    from urllib.parse import unquote
    p = os.path.join(base_dir, unquote(uri))
    if not os.path.exists(p):
        raise SystemExit("Missing file referenced by the model: %s" % p)
    return open(p, "rb").read()


def _view(js, bufs, i):
    bv = js["bufferViews"][i]
    o = bv.get("byteOffset", 0)
    return bufs[bv["buffer"]], o, bv["byteLength"], bv.get("byteStride", 0)


def accessor(js, bufs, idx):
    a = js["accessors"][idx]
    dt = np.dtype(COMP[a["componentType"]]).newbyteorder("<")
    n = NCOMP[a["type"]] if a["type"] in NCOMP else {"MAT2": 4, "MAT3": 9, "MAT4": 16}[a["type"]]
    cnt = a["count"]
    if "bufferView" in a:
        buf, o, ln, stride = _view(js, bufs, a["bufferView"])
        start = o + a.get("byteOffset", 0)
        item = dt.itemsize * n
        if stride and stride != item:
            raw = np.frombuffer(buf, dtype=np.uint8, count=stride * (cnt - 1) + item, offset=start)
            rows = np.lib.stride_tricks.as_strided(raw, shape=(cnt, item), strides=(stride, 1))
            arr = np.frombuffer(np.ascontiguousarray(rows).tobytes(), dtype=dt).reshape(cnt, n)
        else:
            arr = np.frombuffer(buf, dtype=dt, count=cnt * n, offset=start).reshape(cnt, n)
        arr = arr.copy()
    else:
        arr = np.zeros((cnt, n), dtype=dt)
    sp = a.get("sparse")
    if sp:
        ia, va = sp["indices"], sp["values"]
        b1, o1, _, _ = _view(js, bufs, ia["bufferView"])
        ids = np.frombuffer(b1, dtype=np.dtype(COMP[ia["componentType"]]).newbyteorder("<"),
                            count=sp["count"], offset=o1 + ia.get("byteOffset", 0)).astype(np.int64)
        b2, o2, _, _ = _view(js, bufs, va["bufferView"])
        vals = np.frombuffer(b2, dtype=dt, count=sp["count"] * n, offset=o2 + va.get("byteOffset", 0)).reshape(-1, n)
        arr = arr.astype(dt); arr[ids] = vals
    if a.get("normalized") and dt.kind in "iu":
        f = arr.astype(np.float64) / np.iinfo(dt).max
        arr = np.maximum(f, -1.0) if dt.kind == "i" else f
    return arr


class TexCache:
    def __init__(self, js, bufs, base_dir):
        self.js, self.bufs, self.base_dir, self.img = js, bufs, base_dir, {}

    def image(self, src):
        if src not in self.img:
            im = self.js["images"][src]
            if "bufferView" in im:
                b, o, ln, _ = _view(self.js, self.bufs, im["bufferView"])
                raw = b[o:o + ln]
            elif "uri" in im:
                raw = _load_uri(im["uri"], self.base_dir)
            else:
                raise SystemExit("Image %d has no data." % src)
            try:
                pil = Image.open(io.BytesIO(raw))
                if pil.mode in ("RGBA", "LA", "P"):
                    pil = pil.convert("RGBA")
                    bg = Image.new("RGBA", pil.size, (255, 255, 255, 255))
                    pil = Image.alpha_composite(bg, pil)
                self.img[src] = np.asarray(pil.convert("RGB"))
            except Exception as e:
                raise SystemExit("Couldn't decode texture image %d (%s). WebP/KTX2 textures may need a "
                                 "newer Pillow or re-exporting as PNG/JPEG." % (src, e.__class__.__name__))
        return self.img[src]


def _texture_for(js, tinfo, cache):
    """textureInfo -> (image array, wrapS, wrapT, texCoord index, 3x3 uv transform or None)"""
    tex = js["textures"][tinfo["index"]]
    src = tex.get("source")
    for ext in ("EXT_texture_webp", "MSFT_texture_dds", "KHR_texture_basisu"):
        if src is None and ext in tex.get("extensions", {}):
            src = tex["extensions"][ext].get("source")
    if src is None:
        return None
    samp = js.get("samplers", [{}])[tex["sampler"]] if "sampler" in tex else {}
    tc = tinfo.get("texCoord", 0)
    M = None
    tt = tinfo.get("extensions", {}).get("KHR_texture_transform")
    if tt:
        ox, oy = tt.get("offset", [0, 0]); r = tt.get("rotation", 0.0); sx, sy = tt.get("scale", [1, 1])
        c, s = np.cos(r), np.sin(r)
        M = (np.array([[1, 0, ox], [0, 1, oy], [0, 0, 1]]) @ np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])
             @ np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]]))
        tc = tt.get("texCoord", tc)
    return cache.image(src), samp.get("wrapS", GL_REPEAT), samp.get("wrapT", GL_REPEAT), tc, M


def _wrap(u, mode):
    if mode == GL_CLAMP:
        return np.clip(u, 0.0, 1.0)
    if mode == GL_MIRROR:
        t = np.mod(u, 2.0)
        return np.where(t > 1.0, 2.0 - t, t)
    return np.mod(u, 1.0)


def _srgb_to_lin(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def node_instances(js):
    """-> list of (mesh index, world matrix) for every node that carries a mesh."""
    nodes = js.get("nodes", [])

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
    if js.get("scenes"):
        roots = js["scenes"][js.get("scene", 0)].get("nodes", [])
    else:
        kids = {c for nd in nodes for c in nd.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in kids]
    out, seen = [], set()

    def walk(i, parent, depth):
        if depth > 64 or (i, depth) in seen:
            return
        seen.add((i, depth))
        nd = nodes[i]
        w = parent @ local(nd)
        if "mesh" in nd:
            out.append((nd["mesh"], w))
        for c in nd.get("children", []):
            walk(c, w, depth + 1)
    for i in roots:
        walk(i, np.eye(4), 0)
    if not out and js.get("meshes"):                    # no node graph at all
        out = [(i, np.eye(4)) for i in range(len(js["meshes"]))]
    return out


def _triangles(prim, js, bufs, nverts):
    mode = prim.get("mode", 4)
    idx = (accessor(js, bufs, prim["indices"]).reshape(-1).astype(np.int64)
           if "indices" in prim else np.arange(nverts, dtype=np.int64))
    if mode == 4:
        return idx[:len(idx) // 3 * 3].reshape(-1, 3)
    if mode == 5:                                       # strip
        t = np.stack([idx[:-2], idx[1:-1], idx[2:]], axis=1)
        odd = np.arange(len(t)) % 2 == 1
        t[odd] = t[odd][:, [1, 0, 2]]
        return t
    if mode == 6:                                       # fan
        return np.stack([np.full(len(idx) - 2, idx[0]), idx[1:-1], idx[2:]], axis=1)
    return None                                         # points / lines: nothing to print


def load_colored_mesh(path):
    """-> positions (V,3) float64, faces (F,3) int64, face sRGB (F,3) uint8, notes list"""
    js, bufs, base_dir, used_ext = read_gltf(path)
    cache = TexCache(js, bufs, base_dir)
    notes = []
    P, Fc, C = [], [], []
    vbase = 0
    skipped = 0
    for mi, M in node_instances(js):
        for prim in js["meshes"][mi]["primitives"]:
            at = prim.get("attributes", {})
            if "POSITION" not in at:
                continue
            pos = accessor(js, bufs, at["POSITION"]).astype(np.float64)[:, :3]
            tris = _triangles(prim, js, bufs, len(pos))
            if tris is None or not len(tris):
                skipped += 1
                continue
            tris = tris[(tris < len(pos)).all(1)]
            pos = (np.c_[pos, np.ones(len(pos))] @ M.T)[:, :3]
            if np.linalg.det(M[:3, :3]) < 0:                 # mirrored instance: keep outward normals
                tris = tris[:, [0, 2, 1]]
            col = face_colors(js, bufs, cache, prim, at, tris)
            P.append(pos); Fc.append(tris + vbase); C.append(col)
            vbase += len(pos)
    if skipped:
        notes.append("%d point/line primitive(s) skipped (nothing to print)" % skipped)
    if not P:
        raise SystemExit("No triangle geometry found in this file.")
    if any(e.startswith("KHR_texture_basisu") for e in used_ext):
        notes.append("file also carries KTX2 textures; the PNG/JPEG fallback was used")
    return np.vstack(P), np.vstack(Fc), np.vstack(C), notes


def face_colors(js, bufs, cache, prim, at, idx):
    mat = js["materials"][prim["material"]] if "material" in prim and js.get("materials") else {}
    pbr = mat.get("pbrMetallicRoughness", {})
    factor = np.array(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3], dtype=np.float64)
    tinfo = pbr.get("baseColorTexture")
    sg = mat.get("extensions", {}).get("KHR_materials_pbrSpecularGlossiness")
    if sg and "pbrMetallicRoughness" not in mat:
        factor = np.array(sg.get("diffuseFactor", [1, 1, 1, 1])[:3], dtype=np.float64)
        tinfo = sg.get("diffuseTexture")
    lin = np.tile(factor, (len(idx), 1))                  # linear light, per face

    tex = _texture_for(js, tinfo, cache) if tinfo else None
    if tex is not None and "TEXCOORD_%d" % tex[3] in at:
        img, ws, wt, tc, M = tex
        uv = accessor(js, bufs, at["TEXCOORD_%d" % tc]).astype(np.float64)[:, :2]
        if M is not None:
            uv = (np.c_[uv, np.ones(len(uv))] @ M.T)[:, :2]
        H, W = img.shape[:2]
        # 4 points well inside each triangle (centroid + 3 towards the corners) so samples
        # never land in the gaps between UV islands; median rejects a stray seam pixel
        bary = np.array([[1 / 3, 1 / 3, 1 / 3], [0.6, 0.2, 0.2], [0.2, 0.6, 0.2], [0.2, 0.2, 0.6]])
        pts = np.einsum("sk,fkd->fsd", bary, uv[idx])
        x = np.clip((_wrap(pts[..., 0], ws) * W).astype(np.int64), 0, W - 1)
        y = np.clip((_wrap(pts[..., 1], wt) * H).astype(np.int64), 0, H - 1)   # top-left origin
        texel = np.median(img[y, x].astype(np.float64), axis=1) / 255.0
        lin = lin * _srgb_to_lin(texel)
    elif tex is not None:
        pass                                                # textured material but no UVs: factor only
    if "COLOR_0" in at:
        vc = accessor(js, bufs, at["COLOR_0"]).astype(np.float64)[:, :3]
        if vc.max() > 1.0:                                  # un-normalized ints
            vc = vc / 255.0
        lin = lin * vc[idx].mean(axis=1)
    return np.clip(np.round(_lin_to_srgb(lin) * 255), 0, 255).astype(np.uint8)


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
WELD_MM = 1e-4             # vertices closer than this (after scaling to mm) are one vertex


def weld(P, F):
    key = np.round(P / WELD_MM).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    uniq = P[first]
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
MAX_COLORS = 32            # Bambu Studio: 32 physical filaments (+ up to 64 mixed)
MIN_COLORS = 2
SUGGEST_ERR = 0.020        # mean OKLab error ~ just-noticeable difference (about dE 2)
SUGGEST_GAIN = 0.001       # ...or stop once 2 more colours improve it by less than this
SAMPLE_TRIS = 30000


def prepare(path, size_mm=DEFAULT_SIZE, do_smooth=True):
    """Load, orient, scale, weld, measure and (optionally) de-grain. Returns a dict."""
    if np is None:
        raise SystemExit("glb2bambu needs numpy and Pillow:  pip install numpy pillow")
    print("\n=== %s" % path)
    P, F, rgb, notes = load_colored_mesh(path)
    for n in notes:
        print("  note: %s" % n)
    print("Loaded %d triangles, %d vertices" % (len(F), len(P)))
    # Y-up (glTF) -> Z-up (3MF), scale longest side to size_mm, sit on the bed, centre XY
    P = P[:, [0, 2, 1]] * np.array([1, -1, 1])
    P = P - P.min(0)
    span = P.max()
    if span <= 0:
        raise SystemExit("Model has no size.")
    P = P * (size_mm / span)
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
    n_unique = len(np.unique(rgb, axis=0))
    return {"path": path, "P": P, "F": F, "area": area, "lab": lab, "nb": nb,
            "smooth": do_smooth, "n_unique": n_unique}


def suggest(prep, kmax=MAX_COLORS):
    """Error curve on an area-weighted sample: k -> (mean error, centroids). Plus a suggestion."""
    lab, area = prep["lab"], prep["area"]
    rng = np.random.default_rng(7)
    m = min(SAMPLE_TRIS, len(lab))
    S = lab[rng.choice(len(lab), size=m, replace=len(lab) < m, p=area / area.sum())]
    w = np.ones(len(S))
    kmax = max(MIN_COLORS, min(kmax, prep["n_unique"]))
    curve = {}
    for k in range(MIN_COLORS, kmax + 1):
        cent, lbl = kmeans(S, w, k, iters=12)
        err = float(np.sqrt(((S - cent[lbl]) ** 2).sum(1)).mean())
        curve[k] = (err, cent)
    ks = sorted(curve)
    pick = ks[-1]
    for k in ks:
        good_enough = curve[k][0] <= SUGGEST_ERR
        k2 = k + 2
        flat = k2 in curve and curve[k][0] - curve[k2][0] < SUGGEST_GAIN
        if good_enough or flat:
            pick = k
            break
    reason = ("average colour error %.3f (texture is well represented)" % curve[pick][0]
              if curve[pick][0] <= SUGGEST_ERR else
              "more colours barely change the result beyond this")
    return curve, pick, reason


def finalize(prep, n_colors, base, out=None):
    P, F, area, lab, nb = prep["P"], prep["F"], prep["area"], prep["lab"], prep["nb"]
    k = max(1, min(n_colors, MAX_COLORS, prep["n_unique"]))
    labels = distinct_palette(lab, area, k, MIN_DIST)
    k = int(labels.max()) + 1
    cent = np.array([np.average(lab[labels == j], axis=0, weights=area[labels == j]) for j in range(k)])
    if prep["smooth"]:
        before = labels.copy()
        labels = smooth(labels, nb, SMOOTH_PASSES)
        print("Speckle cleanup changed %.2f%% of triangles" % (100.0 * (labels != before).mean()))
    share = np.bincount(labels, weights=area, minlength=k) / area.sum()
    small = np.where(share < MIN_SHARE)[0]
    if len(small) and len(small) < k:
        big = np.setdiff1d(np.arange(k), small)
        for s in small:
            labels[labels == s] = big[((cent[big] - cent[s]) ** 2).sum(1).argmin()]
    used = np.unique(labels)
    cent = np.array([np.average(lab[labels == j], axis=0, weights=area[labels == j]) for j in used])
    lut = np.zeros(int(labels.max()) + 1, dtype=np.int64); lut[used] = np.arange(len(used))
    labels = lut[labels]
    share = np.bincount(labels, weights=area) / area.sum()
    order = np.argsort(-share)
    rank = np.empty_like(order); rank[order] = np.arange(len(order))
    labels = rank[labels]; cent = cent[order]; share = share[order]
    colours = ["#%02X%02X%02X" % tuple(c) for c in oklab_to_srgb(cent)]
    path = prep["path"]
    out = out or os.path.splitext(path)[0] + "_%dcolors.3mf" % len(colours)
    write_3mf(out, P, F, labels, colours, os.path.splitext(os.path.basename(path))[0], base)
    print("\n%d colours (by surface share):" % len(colours))
    for i, (c, s) in enumerate(zip(colours, share), 1):
        print("  filament %2d  %s  %5.1f%%" % (i, c, 100 * s))
    if len(colours) < n_colors:
        print("(asked for %d; %d were distinct enough to be worth a filament)" % (n_colors, len(colours)))
    print("Wrote: %s" % out)
    return out


def convert(path, n_colors=None, size_mm=DEFAULT_SIZE, do_smooth=True, base=None, gui=True):
    """n_colors=None: suggest a count; with gui=True a selector window opens to confirm/change it."""
    base = base or json.load(open(h3c_mix.BASE_FILE))
    prep = prepare(path, size_mm, do_smooth)
    if n_colors is None:
        print("Measuring how many colours this texture needs...")
        curve, pick, reason = suggest(prep)
        print("Suggested: %d colours (%s)" % (pick, reason))
        n_colors = pick
        if gui:
            try:
                import glb_selector
                chosen = glb_selector.choose(prep, curve, pick, reason)
            except ImportError as e:
                print("No GUI available (%s) - using the suggestion." % e)
                chosen = pick
            except Exception as e:
                print("Selector failed (%s) - using the suggestion." % e.__class__.__name__)
                chosen = pick
            if chosen is None:
                print("Cancelled - nothing written.")
                return None
            n_colors = chosen
    return finalize(prep, n_colors, base)


def parse_args(args):
    opts = {"colors": None, "size": DEFAULT_SIZE, "smooth": True}
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
                if convert(f, opts["colors"], opts["size"], opts["smooth"], gui="--auto" not in args) is None:
                    continue
            except SystemExit as e:
                print("! %s" % e)
            except Exception as e:
                print("! Failed on %s: %r" % (f, e))
    finally:
        if pause:
            input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
