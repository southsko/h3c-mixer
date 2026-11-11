#!/usr/bin/env python3
"""
PrusaSlicer FullSpectrum (.3mf) -> Bambu Lab H3C mixed-filament project.

PrusaSlicer stores color mixes as "virtual extruders" in
Metadata/Prusa_Slicer_full_spectrum.json, which Bambu Studio ignores, so such a
file opens with wrong/random colors. This keeps every mix EXACTLY as designed:

  * each Prusa physical extruder that is actually used -> one H3C slot
    (same color; load a matching spool, or override with --colors)
  * each Prusa virtual extruder -> one Bambu mixed filament with the same
    components and ratios (no color matching is done)
  * every part keeps its extruder/mix; object positions are kept

Also handles plain multi-extruder Prusa 3MFs (no mixes). Dropping a Prusa file on
h3c_mix.py / the .bat dispatches here automatically.

Usage:  python prusa2h3c.py file.3mf [--colors "#0090DA,#E24585,#FCE300,..."] [--no-pause]
"""
import json, os, re, sys, zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h3c_mix  # noqa: E402  (build_settings, BASE_FILE)

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
PROC = "0.12mm High Quality @BBL H3C"
MAX_PHYS, MAX_MIXED = 32, 64

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""
ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
              ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n')
SUBTYPE = {"ModelPart": ("normal_part", "model"), "NegativeVolume": ("negative_part", "other"),
           "ParameterModifier": ("modifier_part", "other"), "SupportEnforcer": ("support_enforcer", "other"),
           "SupportBlocker": ("support_blocker", "other")}


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ reading
def read_prusa(zin):
    names = zin.namelist()
    # colours / print settings
    cfg = {}
    for line in zin.read("Metadata/Slic3r_PE.config").decode("utf-8", "replace").splitlines():
        mo = re.match(r";\s*([a-z_0-9]+)\s*=\s*(.*)$", line)
        if mo:
            cfg[mo.group(1)] = mo.group(2)
    ext_cols = [c.strip().upper() for c in cfg.get("extruder_colour", "").split(";") if c.strip()]
    fil_cols = [c.strip().upper() for c in cfg.get("filament_colour", "").split(";") if c.strip()]
    phys_cols = {i + 1: (c if re.fullmatch(r"#[0-9A-F]{6}", c) else "") for i, c in enumerate(ext_cols)}
    for i, c in enumerate(fil_cols):
        if not phys_cols.get(i + 1) and re.fullmatch(r"#[0-9A-F]{6}", c):
            phys_cols[i + 1] = c

    # full spectrum mixes
    virt = {}
    fs_name = "Metadata/Prusa_Slicer_full_spectrum.json"
    if fs_name in names:
        fs = json.loads(zin.read(fs_name))
        for p in fs.get("physical_extruders", []):
            if p.get("color"):
                phys_cols[int(p["id"])] = p["color"].upper()
        for v in fs.get("virtual_extruders", []):
            comps = [(int(c["extruder"]), float(c["ratio"])) for c in v.get("components", [])]
            virt[int(v["id"])] = {"color": (v.get("color") or "").upper(), "comps": comps,
                                  "kind": v.get("kind", "")}

    # geometry
    root = ET.fromstring(zin.read("3D/3dmodel.model"))
    title = ""
    for md in root.findall("m:metadata", NS):
        if md.get("name") == "Title" and md.text:
            title = md.text
    meshes = {}
    for obj in root.findall("m:resources/m:object", NS):
        oid = int(obj.get("id"))
        mesh = obj.find("m:mesh", NS)
        if mesh is None:
            if obj.find("m:components", NS) is not None:
                raise SystemExit("Object %d uses components; only mesh objects are supported." % oid)
            continue
        verts = [(v.get("x"), v.get("y"), v.get("z")) for v in mesh.findall("m:vertices/m:vertex", NS)]
        tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in mesh.findall("m:triangles/m:triangle", NS)]
        meshes[oid] = (verts, tris)
    items = [(int(it.get("objectid")), it.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0"))
             for it in root.findall("m:build/m:item", NS)]

    # volumes
    mroot = ET.fromstring(zin.read("Metadata/Slic3r_PE_model.config"))
    objects = []
    for o in mroot.findall("object"):
        oid = int(o.get("id"))
        ometa = {m.get("key"): m.get("value") for m in o.findall("metadata")}
        vols = []
        for v in o.findall("volume"):
            vm = {m.get("key"): m.get("value") for m in v.findall("metadata")}
            vols.append({"first": int(v.get("firstid")), "last": int(v.get("lastid")),
                         "name": vm.get("name", "part"), "type": vm.get("volume_type", "ModelPart"),
                         "extruder": int(vm.get("extruder") or ometa.get("extruder") or 1),
                         "source_file": vm.get("source_file", "")})
        if not vols and oid in meshes:
            vols.append({"first": 0, "last": len(meshes[oid][1]) - 1, "name": ometa.get("name", "object"),
                         "type": "ModelPart", "extruder": int(ometa.get("extruder") or 1), "source_file": ""})
        objects.append({"id": oid, "name": ometa.get("name", "object_%d" % oid), "volumes": vols})
    return {"title": title, "phys_cols": phys_cols, "virt": virt, "meshes": meshes,
            "items": items, "objects": objects, "cfg": cfg}


# ------------------------------------------------------------------ slots
def plan_slots(src, colour_override):
    virt = src["virt"]
    used = sorted({v["extruder"] for o in src["objects"] for v in o["volumes"]})
    used_virt = [e for e in used if e in virt]
    phys_used = set(e for e in used if e not in virt)
    for e in used_virt:
        phys_used |= {c for c, _ in virt[e]["comps"]}
    phys_used = sorted(phys_used)
    if len(phys_used) > MAX_PHYS:
        raise SystemExit("%d physical extruders used; Bambu Studio allows %d." % (len(phys_used), MAX_PHYS))
    slot = {e: i + 1 for i, e in enumerate(phys_used)}            # prusa physical -> H3C slot
    colours = [src["phys_cols"].get(e) or "#808080" for e in phys_used]
    if colour_override:
        for i, c in enumerate(colour_override[:len(colours)]):
            colours[i] = c
    comps, ratios, mix_cols, mix_desc = [], [], [], []
    for e in used_virt:
        cs = [(slot[c], r) for c, r in virt[e]["comps"] if r > 0]
        if len(cs) < 2:                                            # degenerate mix = solid
            slot[e] = cs[0][0] if cs else 1
            continue
        tot = sum(r for _, r in cs)
        rs = [round(r / tot, 4) for _, r in cs]
        rs[-1] = round(1.0 - sum(rs[:-1]), 4)
        slot[e] = len(colours) + len(comps) + 1
        comps.append(",".join(str(s) for s, _ in cs))
        ratios.append(",".join("%.4f" % r for r in rs))
        mix_cols.append(virt[e]["color"] if re.fullmatch(r"#[0-9A-F]{6}", virt[e]["color"]) else "#808080")
        mix_desc.append(" + ".join("slot %d %d%%" % (s, round(r * 100)) for (s, _), r in zip(cs, rs)))
    if len(comps) > MAX_MIXED:
        raise SystemExit("%d mixed filaments; Bambu Studio allows %d." % (len(comps), MAX_MIXED))
    return slot, colours, comps, ratios, mix_cols, mix_desc, phys_used, used_virt


# ------------------------------------------------------------------ writing
def submesh(verts, tris, first, last):
    idx, out_v, out_t = {}, [], []
    for a, b, c in tris[first:last + 1]:
        tri = []
        for v in (a, b, c):
            if v not in idx:
                idx[v] = len(out_v); out_v.append(verts[v])
            tri.append(idx[v])
        out_t.append(tri)
    return out_v, out_t


def convert(path, base=None, colour_override=None):
    base = base or json.load(open(h3c_mix.BASE_FILE))
    print("\n=== %s" % path)
    zin = zipfile.ZipFile(path)
    src = read_prusa(zin)
    zin.close()
    print("Source: PrusaSlicer, %s, %d object(s), %d mixes defined" %
          (src["cfg"].get("printer_model", "?"), len(src["objects"]), len(src["virt"])))

    slot, colours, comps, ratios, mix_cols, mix_desc, phys_used, used_virt = plan_slots(src, colour_override)
    n_phys, n = len(colours), len(colours) + len(comps)
    all_cols = colours + mix_cols
    P = h3c_mix.build_settings(base, all_cols, comps, ratios, n, n_phys)
    P["print_settings_id"] = PROC
    P["enable_mixed_color_sublayer"] = "1"

    # --- geometry: one object file per source object, parts as sub-objects
    part_id = 0
    obj_files, assemblies, ms_objects = {}, [], []
    item_tf = dict(src["items"])
    for oi, o in enumerate(src["objects"], 1):
        if o["id"] not in src["meshes"]:
            continue
        verts, tris = src["meshes"][o["id"]]
        fname = "3D/Objects/object_%d.model" % oi
        buf = [MODEL_HEAD, ' <resources>\n']
        parts, faces = [], 0
        for v in o["volumes"]:
            part_id += 1
            sv, st = submesh(verts, tris, v["first"], v["last"])
            subtype, otype = SUBTYPE.get(v["type"], ("normal_part", "model"))
            buf.append('  <object id="%d" p:UUID="0001%04X-81cb-4c03-9d28-80fed5dfa1dc" type="%s">\n   <mesh>\n    <vertices>\n'
                       % (part_id, part_id - 1, otype))
            buf.extend('     <vertex x="%s" y="%s" z="%s"/>\n' % t for t in sv)
            buf.append('    </vertices>\n    <triangles>\n')
            buf.extend('     <triangle v1="%d" v2="%d" v3="%d"/>\n' % tuple(t) for t in st)
            buf.append('    </triangles>\n   </mesh>\n  </object>\n')
            parts.append((part_id, v, subtype, len(st)))
            faces += len(st)
        buf.append(' </resources>\n</model>\n')
        obj_files[fname] = "".join(buf)
        assemblies.append((oi, fname, parts, o, faces))

    asm_base = part_id
    main = [MODEL_HEAD, ' <metadata name="Application">BambuStudio-%s</metadata>\n' % base["studio_version"],
            ' <metadata name="Title">%s</metadata>\n' % esc(src["title"]), ' <resources>\n']
    ms = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    instances = []
    for k, (oi, fname, parts, o, faces) in enumerate(assemblies):
        aid = asm_base + k + 1
        main.append('  <object id="%d" p:UUID="%08X-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n   <components>\n' % (aid, aid))
        for pid, v, subtype, nt in parts:
            main.append('    <component p:path="/%s" objectid="%d" p:UUID="0001%04X-b206-40ff-9872-83e8017abed1" '
                        'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n' % (fname, pid, pid - 1))
        main.append('   </components>\n  </object>\n')
        ms.append('  <object id="%d">\n    <metadata key="name" value="%s"/>\n    <metadata key="extruder" value="%d"/>\n'
                  '    <metadata face_count="%d"/>\n' % (aid, esc(o["name"]), slot[parts[0][1]["extruder"]], faces))
        for pid, v, subtype, nt in parts:
            ms.append('    <part id="%d" subtype="%s">\n      <metadata key="name" value="%s"/>\n'
                      '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                      '      <metadata key="source_file" value="%s"/>\n      <metadata key="source_object_id" value="0"/>\n'
                      '      <metadata key="source_volume_id" value="0"/>\n      <metadata key="extruder" value="%d"/>\n'
                      '      <mesh_stat face_count="%d" edges_fixed="0" degenerate_facets="0" facets_removed="0" '
                      'facets_reversed="0" backwards_edges="0"/>\n    </part>\n'
                      % (pid, subtype, esc(v["name"]), esc(v["source_file"]), slot[v["extruder"]], nt))
        ms.append('  </object>\n')
        instances.append((aid, item_tf.get(o["id"], "1 0 0 0 1 0 0 0 1 0 0 0")))
    main.append(' </resources>\n <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n')
    for aid, tf in instances:
        main.append('  <item objectid="%d" p:UUID="%08X-b1ec-4553-aec9-835e5b724bb4" transform="%s" printable="1"/>\n' % (aid, aid, tf))
    main.append(' </build>\n</model>\n')
    ms.append('  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value=""/>\n'
              '    <metadata key="locked" value="false"/>\n    <metadata key="filament_map_mode" value="Auto For Match"/>\n'
              '    <metadata key="filament_maps" value="%s"/>\n    <metadata key="filament_volume_maps" value="%s"/>\n'
              '    <metadata key="gcode_file" value=""/>\n' % (" ".join(["1"] * n), " ".join(["0"] * n_phys + ["2"] * len(comps))))
    for i, (aid, _) in enumerate(instances):
        ms.append('    <model_instance>\n      <metadata key="object_id" value="%d"/>\n      <metadata key="instance_id" value="0"/>\n'
                  '      <metadata key="identify_id" value="%d"/>\n    </model_instance>\n' % (aid, 100 + i))
    ms.append('  </plate>\n  <assemble>\n  </assemble>\n</config>\n')

    rels = ['<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n']
    for i, fname in enumerate(obj_files, 1):
        rels.append(' <Relationship Target="/%s" Id="rel-%d" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' % (fname, i))
    rels.append('</Relationships>\n')
    cut = ['<?xml version="1.0" encoding="utf-8"?>\n<objects>\n']
    cut += [' <object id="%d">\n  <cut_id id="0" check_sum="1" connectors_cnt="0"/>\n </object>\n' % aid for aid, _ in instances]
    cut.append('</objects>\n')
    slice_info = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
                  '    <header_item key="X-BBL-Client-Version" value="%s"/>\n  </header>\n</config>\n' % base["studio_version"])

    out = os.path.splitext(path)[0] + "_H3C_mixed.3mf"
    files = [("[Content_Types].xml", CONTENT_TYPES), ("_rels/.rels", ROOT_RELS), ("3D/3dmodel.model", "".join(main)),
             ("3D/_rels/3dmodel.model.rels", "".join(rels))] + sorted(obj_files.items()) + [
             ("Metadata/model_settings.config", "".join(ms)), ("Metadata/project_settings.config", json.dumps(P, indent=4)),
             ("Metadata/cut_information.xml", "".join(cut)), ("Metadata/slice_info.config", slice_info),
             ("Metadata/filament_sequence.json", '{"plate_1":{"nozzle_sequence":[],"optimal_assignment":[],"sequence":[]}}')]
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    os.replace(tmp, out)

    # --- report
    print("\nLoad the H3C (any PLA; colors are what the design expects):")
    for i, (e, c) in enumerate(zip(phys_used, colours), 1):
        print("  slot %2d  %s   (Prusa extruder %d)" % (i, c, e))
    if comps:
        print("\nMixed filaments (ratios kept exactly as designed):")
        for i, (d, c) in enumerate(zip(mix_desc, mix_cols), n_phys + 1):
            print("  slot %2d  %s   %s" % (i, c, d))
    dropped = sorted(set(src["phys_cols"]) - set(phys_used))
    if dropped:
        print("\nUnused Prusa extruders dropped: %s" % dropped)
    print("\n%d spools + %d mixes = %d filament slots. Process: %s, mixed-color sublayers on."
          % (n_phys, len(comps), n, PROC))
    print("Wrote: %s" % out)
    return out


def main():
    args = sys.argv[1:]
    pause = "--no-pause" not in args
    cols = None
    if "--colors" in args:
        i = args.index("--colors")
        cols = [c.strip().upper() for c in args[i + 1].split(",")]
        bad = [c for c in cols if not re.fullmatch(r"#[0-9A-F]{6}", c)]
        if bad:
            raise SystemExit("Bad --colors value(s): %s (use #RRGGBB)" % bad)
        del args[i:i + 2]
    files = [a for a in args if not a.startswith("--")]
    try:
        if not files:
            print(__doc__)
            return
        base = json.load(open(h3c_mix.BASE_FILE))
        for f in files:
            try:
                convert(f, base, cols)
            except SystemExit as e:
                print("! %s" % e)
            except Exception as e:
                print("! Failed on %s: %r" % (f, e))
    finally:
        if pause:
            input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
