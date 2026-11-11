#!/usr/bin/env python3
"""
H3C Mixer - drag a color-painted Bambu Studio .3mf onto this script (or the .bat).

It writes <name>_H3C_mixed.3mf next to the original:
  * printer  -> Bambu Lab H3C 0.4 nozzle, process 0.12mm High Quality @BBL H3C
  * spools   -> 1 White, 2 Cyan, 3 Magenta, 4 Yellow, 5 Black (Bambu PLA Palette)
  * colors   -> the file's exact hex colors are matched to mixes of those 5 spools
                (or a solid spool) using Bambu Studio's measured CMYKW PLA
                Palette data, including in-between ratios (5% steps, min 15%)
                interpolated the same way Studio previews them. All colors are
                solved together so colors that differ in the model stay
                different in the print instead of collapsing into one mix.
Object positions and the prime tower position are kept from the source file.
A PrusaSlicer 3MF (FullSpectrum mixes) is routed to prusa2h3c.py, which keeps
the designed mix ratios exactly instead of color-matching.

Usage:  python h3c_mix.py file.3mf [more.3mf ...] [--auto] [--no-pause]
        (a chooser window opens to review/override the picks; --auto skips it)
Needs only Python 3.8+ (no extra packages). The original file is never modified.
"""
import json, math, os, re, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_FILE = os.path.join(HERE, "h3c_palette_base.json")
STUDIO_RECIPES = r"C:\Program Files\Bambu Studio\resources\filament_mixing\standard_color_recipes.json"

PHYS = [("White", "#FFFFFF"), ("Cyan", "#0090DA"), ("Magenta", "#E24585"),
        ("Yellow", "#FCE300"), ("Black", "#000000")]
PHYS_IDX = {name: i + 1 for i, (name, _) in enumerate(PHYS)}
# ---------------------------------------------------------------- color math
def hex_to_lab(h):
    h = h.lstrip("#")[:6]
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    f = lambda v: v ** (1 / 3) if v > 216 / 24389 else (24389 / 27 * v + 16) / 116
    return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))]


USE_DE2000 = True       # False = CIE76 (what Bambu Studio itself scores with)


def delta_e76(a, b):
    return math.dist(a, b)


def delta_e2000(L1, L2):
    l1, a1, b1 = L1; l2, a2, b2 = L2
    C1 = math.hypot(a1, b1); C2 = math.hypot(a2, b2); Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p = a1 * (1 + G); a2p = a2 * (1 + G)
    C1p = math.hypot(a1p, b1); C2p = math.hypot(a2p, b2)
    h1 = math.degrees(math.atan2(b1, a1p)) % 360; h2 = math.degrees(math.atan2(b2, a2p)) % 360
    dL = l2 - l1; dC = C2p - C1p; dh = h2 - h1
    if C1p * C2p == 0:
        dh = 0
    elif dh > 180:
        dh -= 360
    elif dh < -180:
        dh += 360
    dH = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dh / 2))
    Lb = (l1 + l2) / 2; Cbp = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hb = h1 + h2
    else:
        hb = (h1 + h2 + 360) / 2 if abs(h1 - h2) > 180 else (h1 + h2) / 2
    T = (1 - 0.17 * math.cos(math.radians(hb - 30)) + 0.24 * math.cos(math.radians(2 * hb))
         + 0.32 * math.cos(math.radians(3 * hb + 6)) - 0.20 * math.cos(math.radians(4 * hb - 63)))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / math.sqrt(20 + (Lb - 50) ** 2)
    SC = 1 + 0.045 * Cbp; SH = 1 + 0.015 * Cbp * T
    RT = -2 * math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7)) * math.sin(math.radians(60 * math.exp(-((hb - 275) / 25) ** 2)))
    return math.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))


def delta_e(a, b):
    return delta_e2000(a, b) if USE_DE2000 else delta_e76(a, b)


def lab_to_hex(lab):        # same conversion Bambu Studio uses for previews
    l, a, b = lab
    fy = (l + 16) / 116; fx = a / 500 + fy; fz = fy - b / 200
    fi = lambda t: t ** 3 if t ** 3 > 216 / 24389 else (116 * t - 16) / (24389 / 27)
    X, Y, Z = 0.95047 * fi(fx), fi(fy), 1.08883 * fi(fz)
    rgb = [3.2406 * X - 1.5372 * Y - 0.4986 * Z, -0.9689 * X + 1.8758 * Y + 0.0415 * Z,
           0.0557 * X - 0.2040 * Y + 1.0570 * Z]
    g = lambda c: 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return "#" + "".join("%02X" % max(0, min(255, round(g(max(0.0, min(1.0, c))) * 255))) for c in rgb)


# ----------------------------------------------------- recipe candidates
# Mirrors Bambu Studio's lookup_measured_blend_color(): exact measured points are
# used as-is; in-between ratios are interpolated in Lab (2 spools: linear along
# the first ratio, 3 spools: inverse-distance p=2 over the 3 nearest measured
# points), with components sorted by hex like Studio does. So the preview colour
# Studio shows for each mix matches what this script scored.
MIN_RATIO = 15          # smallest share per spool (Bambu's measured data starts at 15%)
STEP = 5                # ratio grid step in %


def build_candidates(recipes):
    hex_of = dict(PHYS)
    sets = {}
    for e in recipes:
        pairs = sorted(zip([c["rgb"].upper() for c in e["components"]],
                           [c["key"] for c in e["components"]], e["ratios"]))
        key = tuple(k for _, k, _ in pairs)
        sets.setdefault(key, []).append(([r for _, _, r in pairs], e["measured_lab"]))
    cands = []
    for i, (name, h) in enumerate(PHYS):              # solid spools
        cands.append({"solid": i + 1, "comps": [name], "ratios": [100], "lab": hex_to_lab(h)})
    for key, anchors in sets.items():
        n = len(key)
        if n == 2:
            grid = [(a, 100 - a) for a in range(MIN_RATIO, 101 - MIN_RATIO, STEP)]
        else:
            grid = [(a, b, 100 - a - b) for a in range(MIN_RATIO, 101, STEP)
                    for b in range(MIN_RATIO, 101, STEP) if 100 - a - b >= MIN_RATIO]
        for r in grid:
            lab = blend_lab(anchors, list(r))
            cands.append({"solid": 0, "comps": list(key), "ratios": list(r), "lab": lab})
    for c in cands:
        c["hex"] = lab_to_hex(c["lab"])
        c["ok"] = oklch(c["hex"])
        c["white"] = 0.0 if c["solid"] else dict(zip(c["comps"], c["ratios"])).get("White", 0) / 100.0
    return cands


def blend_lab(anchors, r):
    for ar, lab in anchors:
        if ar == r:
            return lab
    if len(r) == 2:
        s = sorted(anchors, key=lambda a: a[0][0])
        x, lo = r[0], 0
        while lo + 2 < len(s) and s[lo + 1][0][0] <= x:
            lo += 1
        (r0, l0), (r1, l1) = s[lo], s[lo + 1]
        t = (x - r0[0]) / (r1[0] - r0[0]) if r1[0] != r0[0] else 0.0
        return [l0[i] + t * (l1[i] - l0[i]) for i in range(3)]
    d = sorted((math.hypot(r[0] - ar[0], r[1] - ar[1]), lab) for ar, lab in anchors)[:3]
    w = [1 / dd ** 2 for dd, _ in d]
    return [sum(wi * lab[i] for wi, (_, lab) in zip(w, d)) / sum(w) for i in range(3)]


# ----------------------------------------------------- palette solver
# Picks all colours together. Cost = sum of colour errors + a penalty whenever two
# colours that were clearly different in the model end up closer together than
# they were (up to KEEP_APART dE), so details don't merge into one mix.
KEEP_APART = 8.0        # dE separation worth preserving between source colours (DE2000 scale)
APART_WEIGHT = 1.0
TOP_K = 60              # candidates considered per colour

# Out-of-gamut handling. A pure #0000FF or #FF0000 is far outside what C/M/Y/K/W can
# mix; a plain colour-difference metric then happily picks a grey of the right
# darkness, which looks wrong. Calibrated on hand picks for a saturated-primaries
# model: people take the MOST VIVID mix near the right hue, avoid white in the
# mix, and accept ~20 degrees of hue shift for it. Hue/chroma are judged in OKLCh,
# whose hue lines stay straight where Lab's bend (blue/purple, red/orange).
SAT_LO = 8.0            # OKLCh chroma below which the target is muted: plain dE2000 is right
SAT_HI = 20.0           # ... and above which the vividness rule fully takes over
HUE_W = 0.2             # score per degree of hue error (vividness rule)
LIGHT_W = 0.1           # score per unit of OK lightness error (vividness rule)
WHITE_W = 20.0          # score per 100% white in the mix (vividness rule)


def oklch(hex_color):
    hx = hex_color.lstrip("#")[:6]
    c = [int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return (L * 100, math.hypot(a, bb) * 100, math.degrees(math.atan2(bb, a)) % 360)


def hue_diff(h1, h2):
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def match_score(t_lab, t_ok, c):
    """Lower is better. Blends plain dE2000 (muted targets) with the vividness rule
    (saturated targets) according to the target's OKLCh chroma."""
    d = delta_e(t_lab, c["lab"])
    Lt, Ct, Ht = t_ok
    s = min(1.0, max(0.0, (Ct - SAT_LO) / (SAT_HI - SAT_LO)))
    if s == 0.0:
        return d
    Lc, Cc, Hc = c["ok"]
    vivid = (max(0.0, Ct - Cc) + HUE_W * hue_diff(Ht, Hc) + LIGHT_W * abs(Lt - Lc)
             + WHITE_W * c["white"])
    return (1 - s) * d + s * vivid


def solve_palette(targets_hex, cands):
    T = [hex_to_lab(h) for h in targets_hex]
    TO = [oklch(h) for h in targets_hex]
    n = len(T)
    opts = [sorted(range(len(cands)), key=lambda c: match_score(T[i], TO[i], cands[c]))[:TOP_K] for i in range(n)]
    want = [[min(delta_e(T[i], T[j]), KEEP_APART) for j in range(n)] for i in range(n)]

    def pair_pen(i, ci, j, cj):
        return APART_WEIGHT * max(0.0, want[i][j] - delta_e(cands[ci]["lab"], cands[cj]["lab"]))

    pick = [o[0] for o in opts]                       # start: each colour's best
    for _ in range(50):
        changed = False
        for i in range(n):
            best, best_cost = pick[i], None
            for c in opts[i]:
                cost = match_score(T[i], TO[i], cands[c]) + sum(pair_pen(i, c, j, pick[j]) for j in range(n) if j != i)
                if best_cost is None or cost < best_cost - 1e-9:
                    best, best_cost = c, cost
            if best != pick[i]:
                pick[i] = best; changed = True
        if not changed:
            break
    return [(cands[c], delta_e(T[i], cands[c]["lab"])) for i, c in enumerate(pick)]


# ------------------------------------------------- paint string (TriangleSelector)
# Bambu/Prusa store per-triangle paint as hex nibbles written in reverse order.
# Node nibble: low 2 bits = split sides (0 = leaf). Leaf: state = nibble>>2, or
# if nibble == 0b1100 the state is 3 + next nibble (+15 per extra 0xF nibble).
def remap_paint(code, remap):
    nib = [int(ch, 16) for ch in reversed(code)]
    out, pos = [], 0

    def node():
        nonlocal pos
        c = nib[pos]; pos += 1
        split = c & 0b11
        if split:
            out.append(c)
            for _ in range(split + 1):
                node()
            return
        if (c & 0b1100) == 0b1100:
            num = 0
            nxt = nib[pos]; pos += 1
            while nxt == 0b1111:
                num += 1; nxt = nib[pos]; pos += 1
            state = nxt + 15 * num + 3
        else:
            state = c >> 2
        state = remap.get(state, state) if state else 0
        if state < 3:
            out.append(state << 2)
        else:
            out.append(0b1100)
            n = state - 3
            while n >= 15:
                out.append(0b1111); n -= 15
            out.append(n)

    while pos < len(nib):
        node()
    return "".join("%X" % x for x in reversed(out))


def paint_states(code):
    nib = [int(ch, 16) for ch in reversed(code)]
    states, pos = set(), 0

    def node():
        nonlocal pos
        c = nib[pos]; pos += 1
        split = c & 0b11
        if split:
            for _ in range(split + 1):
                node()
            return
        if (c & 0b1100) == 0b1100:
            num = 0
            nxt = nib[pos]; pos += 1
            while nxt == 0b1111:
                num += 1; nxt = nib[pos]; pos += 1
            states.add(nxt + 15 * num + 3)
        else:
            states.add(c >> 2)

    while pos < len(nib):
        node()
    return states


PAINT_RE = re.compile(r'paint_color="([0-9A-Fa-f]+)"')
EXTRUDER_RE = re.compile(r'(<metadata key="extruder" value=")(\d+)("/>)')


# ------------------------------------------------------------ settings build
def build_settings(base, colours, comps, ratios, n, n_phys=5):
    """Project settings for n slots: the first n_phys are physical spools, the rest mixed.
    When the physical spools are exactly the Palette W/C/M/Y/K set, the base's measured
    per-slot values (flush matrix etc.) are kept; otherwise neutral defaults are used."""
    B = base["settings"]
    P = dict(B)
    palette = n_phys == 5 and [c[:7].upper() for c in colours[:5]] == [h for _, h in PHYS]
    for k, m in base["slot_keys"].items():
        if k in B:
            first = B[k][:m]
            P[k] = [x for i in range(n) for x in (B[k][i * m:(i + 1) * m] if palette and i < 5 else first)]
    for k in base["matrix_keys"]:
        if k in B:
            P[k] = [B[k][r * 5 + c] if palette and r < 5 and c < 5 else ("0" if r == c else "280")
                    for r in range(n) for c in range(n)]
    for k in base["plus1_keys"]:
        if k in B:
            P[k] = [B[k][0]] * n + [B[k][-1]]
    for k in base["plus2_keys"]:
        if k in B:
            P[k] = [B[k][0]] + [B[k][1]] * n + [B[k][-1]]
    m = n - n_phys
    P["filament_colour"] = colours
    P["filament_multi_colour"] = list(colours)
    P["default_filament_colour"] = [""] * n
    P["filament_colour_type"] = ["2"] * n_phys + ["1"] * m
    P["filament_is_mixed"] = ["0"] * n_phys + ["1"] * m
    P["filament_mixed_components"] = [""] * n_phys + comps
    P["filament_mixed_sublayer_ratios"] = [""] * n_phys + ratios
    P["filament_mixed_gradient"] = ["0"] * n
    P["filament_mixed_gradient_per_part"] = ["0"] * n
    P["filament_mixed_gradient_curve"] = [""] * n
    P["filament_mixed_gradient_range"] = [""] * n
    P["filament_self_index"] = [str(i + 1) for i in range(n) for _ in range(2)]
    P["filament_map"] = ["1"] * n
    P["enable_mixed_color_sublayer"] = "1"
    return P


# ------------------------------------------------------------ colour chooser GUI
OPTION_SPREAD = 2.5     # chooser: minimum dE between the options offered for one colour


def gui_available():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def _recipe_caption(c):
    if c["solid"]:
        return c["comps"][0]
    return " ".join("%s%d" % (nm[0], r) for nm, r in zip(c["comps"], c["ratios"]))


def choose_with_gui(targets_hex, cands, default_picks, top_k=10, labels=None, _test_hook=None):
    """Window to override the auto picks. Returns the chosen candidate dicts, or None
    if cancelled. _test_hook(root, state) is called after the window is built."""
    import tkinter as tk
    n = len(targets_hex)
    rows_opts = []
    for i, t in enumerate(targets_hex):
        tl, to = hex_to_lab(t), oklch(t)
        d = default_picks[i]
        opts = [d]
        # best-scoring first, but skip anything that would look the same as an option
        # already shown, so the row offers real alternatives instead of ten near-twins
        for c in sorted(cands, key=lambda c: match_score(tl, to, c)):
            if len(opts) >= top_k:
                break
            if all(delta_e(c["lab"], o["lab"]) >= OPTION_SPREAD for o in opts):
                opts.append(c)
        for c in cands:
            if c["solid"] and not any(o is c for o in opts):
                opts.append(c)
        rows_opts.append(opts)

    root = tk.Tk()
    root.title("H3C Mixer - choose colors")
    root.geometry("1100x750")
    result = {"value": None}
    sel = [next(j for j, o in enumerate(rows_opts[i]) if o is default_picks[i]) for i in range(n)]
    swatches = [[] for _ in range(n)]

    def refresh(i):
        for j, sw in enumerate(swatches[i]):
            on = j == sel[i]
            col = "#1E66F5" if on else "#999999"
            sw.config(highlightthickness=3 if on else 1, highlightbackground=col, highlightcolor=col)

    def select(i, j):
        sel[i] = j
        refresh(i)

    def finish(val):
        result["value"] = val
        root.destroy()

    def do_convert():
        finish([rows_opts[i][sel[i]] for i in range(n)])

    def do_reset():
        for i in range(n):
            sel[i] = next(j for j, o in enumerate(rows_opts[i]) if o is default_picks[i])
            refresh(i)

    bar = tk.Frame(root)
    bar.pack(side="bottom", fill="x", padx=8, pady=6)
    tk.Button(bar, text="Convert", width=12, command=do_convert).pack(side="right", padx=4)
    tk.Button(bar, text="Reset to auto", width=12, command=do_reset).pack(side="right", padx=4)
    tk.Button(bar, text="Cancel", width=12, command=lambda: finish(None)).pack(side="right", padx=4)
    tk.Label(bar, text="Click a swatch to choose the mix for each color (caption: recipe / dE).",
             anchor="w").pack(side="left")

    outer = tk.Frame(root)
    outer.pack(side="top", fill="both", expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0)
    vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    root.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

    def safe_bg(h):
        h = (h or "").strip()[:7]
        return h if re.fullmatch(r"#[0-9A-Fa-f]{6}", h) else "#808080"

    for i, t in enumerate(targets_hex):
        row = tk.Frame(inner, pady=4)
        row.grid(row=i, column=0, sticky="w")
        head = tk.Frame(row)
        head.pack(side="left", padx=(6, 14))
        tk.Frame(head, width=48, height=48, bg=safe_bg(t), highlightthickness=1,
                 highlightbackground="#555555").pack()
        tk.Label(head, text=t[:7].upper()).pack()
        if labels:
            tk.Label(head, text=labels[i], fg="#555555").pack()
        tl = hex_to_lab(t)
        for j, c in enumerate(rows_opts[i]):
            cell = tk.Frame(row)
            cell.pack(side="left", padx=3)
            sw = tk.Frame(cell, width=44, height=44, bg=safe_bg(c.get("hex")), highlightthickness=1,
                          highlightbackground="#999999", cursor="hand2")
            sw.pack()
            swatches[i].append(sw)
            cap = "%s\n%.1f" % (_recipe_caption(c), delta_e(tl, c["lab"]))
            lb = tk.Label(cell, text=cap, font=("TkDefaultFont", 8), cursor="hand2")
            lb.pack()
            for w in (sw, lb):
                w.bind("<Button-1>", lambda e, i=i, j=j: select(i, j))
        refresh(i)

    root.update_idletasks()
    w = min(1100, max(500, inner.winfo_reqwidth() + vsb.winfo_reqwidth() + 8))
    h = min(750, inner.winfo_reqheight() + bar.winfo_reqheight() + 24)
    root.geometry("%dx%d" % (w, h))
    root.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    if _test_hook:
        _test_hook(root, {"options": rows_opts, "sel": sel, "select": select,
                          "convert": do_convert, "cancel": lambda: finish(None), "reset": do_reset})
    root.mainloop()
    return result["value"]


# ---------------------------------------------------------------- converter
def convert(src, base, recipes, gui=True):
    print("\n=== %s" % src)
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    proj = json.loads(zin.read("Metadata/project_settings.config"))
    old_cols = [c[:7].upper() for c in proj.get("filament_colour", [])]
    print("Source printer: %s, %d filaments" % (proj.get("printer_settings_id"), len(old_cols)))

    model_files = [n for n in names if n.startswith("3D/") and n.endswith(".model")]
    texts = {n: zin.read(n).decode("utf-8") for n in model_files}
    ms_name = "Metadata/model_settings.config"
    ms = zin.read(ms_name).decode("utf-8") if ms_name in names else ""

    used = set()
    for t in texts.values():
        for code in set(PAINT_RE.findall(t)):
            used |= paint_states(code.upper())
    used.discard(0)
    used |= {int(g[1]) for g in EXTRUDER_RE.findall(ms)}   # unpainted faces use these
    used.discard(0)
    if not used:
        used = {1}
    bad = [u for u in used if u > len(old_cols)]
    if bad:
        raise SystemExit("Paint references filament(s) %s but the file only defines %d." % (bad, len(old_cols)))

    # pick recipes, merge identical ones
    remap, mixes, report = {}, [], []
    order = sorted(used)
    cands = build_candidates(recipes)
    targets = [old_cols[i - 1] for i in order]
    picks = solve_palette(targets, cands)
    changed = 0
    if gui:
        chosen = False
        if gui_available():
            try:
                chosen = choose_with_gui(targets, cands, [c for c, _ in picks],
                                         labels=["filament %d" % i for i in order])
            except Exception as e:          # no display, broken Tk, ...
                print("No GUI available (%s) - using automatic picks." % e.__class__.__name__)
        if chosen is not False:
            if chosen is None:
                zin.close()
                print("Cancelled - nothing written.")
                return None
            changed = sum(1 for (c0, _), c1 in zip(picks, chosen) if c0 is not c1)
            picks = [(c, delta_e(hex_to_lab(t), c["lab"])) for t, c in zip(targets, chosen)]
    for i, (c, de) in zip(order, picks):
        if c["solid"]:
            remap[i] = c["solid"]
            report.append((i, old_cols[i - 1], "solid " + c["comps"][0], c["hex"], de))
            continue
        key = (tuple(c["comps"]), tuple(c["ratios"]))
        if key not in [mk for mk, _ in mixes]:
            mixes.append((key, c["hex"]))
        remap[i] = 6 + [mk for mk, _ in mixes].index(key)
        desc = " + ".join("%s %d%%" % (cc, p) for cc, p in zip(c["comps"], c["ratios"]))
        report.append((i, old_cols[i - 1], desc, c["hex"], de))
    shared = {}
    for i in order:
        shared.setdefault(remap[i], []).append(i)
    merged = [v for v in shared.values() if len(v) > 1]
    n = 5 + len(mixes)
    if len(mixes) > 64:     # Bambu Studio: max 32 physical + 64 mixed filaments
        raise SystemExit("Too many distinct mixes (%d, max 64). Reduce colors first." % len(mixes))

    colours = [h for _, h in PHYS] + [h for _, h in mixes]
    comps = [",".join(str(PHYS_IDX[c]) for c in k[0]) for k, _ in mixes]
    ratios = [",".join("%.4f" % (x / 100) for x in k[1]) for k, _ in mixes]
    P = build_settings(base, colours, comps, ratios, n)
    for k in ("wipe_tower_x", "wipe_tower_y"):   # keep the source tower position
        if k in proj:
            v = proj[k] if isinstance(proj[k], list) else [proj[k]]
            P[k] = (v * 2)[:len(P[k])] if isinstance(P[k], list) else v[0]

    # rewrite paint + extruders
    new_texts = {}
    for name, t in texts.items():
        cache = {}

        def sub(mo):
            c = mo.group(1).upper()
            if c not in cache:
                cache[c] = remap_paint(c, remap)
            return 'paint_color="%s"' % cache[c]
        new_texts[name] = PAINT_RE.sub(sub, t)
    ms = EXTRUDER_RE.sub(lambda mo: mo.group(1) + str(remap.get(int(mo.group(2)), int(mo.group(2)))) + mo.group(3), ms)
    ms = re.sub(r'\s*<metadata key="filament_maps" value="[^"]*"/>', "", ms)
    ms = re.sub(r'\s*<metadata key="filament_volume_maps" value="[^"]*"/>', "", ms)
    ms = re.sub(r'<metadata key="filament_map_mode" value="[^"]*"/>',
                '<metadata key="filament_map_mode" value="Auto For Match"/>\n    '
                '<metadata key="filament_maps" value="%s"/>\n    '
                '<metadata key="filament_volume_maps" value="%s"/>'
                % (" ".join(["1"] * n), " ".join(["0"] * 5 + ["2"] * (n - 5))), ms)

    out = os.path.splitext(src)[0] + "_H3C_mixed.3mf"
    over = {"Metadata/project_settings.config": json.dumps(P, indent=4), ms_name: ms}
    over.update(new_texts)
    if "Metadata/slice_info.config" in names:
        over["Metadata/slice_info.config"] = re.sub(
            r'(X-BBL-Client-Version" value=")[^"]*', r"\g<1>" + base["studio_version"],
            zin.read("Metadata/slice_info.config").decode("utf-8"))
    skip = {"Metadata/process_settings_1.config"} | {n_ for n_ in names if re.match(r"Metadata/(filament|process)_settings_\d+\.config", n_)}
    order = sorted(names, key=lambda x: (x != "[Content_Types].xml", x))
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for a in order:
            if a in skip or a.startswith("Metadata/plate_") and a.endswith(".gcode"):
                continue
            if a in over:
                z.writestr(a, over[a])
            else:
                z.writestr(zin.getinfo(a), zin.read(a))
    zin.close()
    os.replace(tmp, out)

    # report
    print("\nLoad the H3C: 1 White, 2 Cyan, 3 Magenta, 4 Yellow, 5 Black (Bambu PLA Palette)\n")
    print("  old  color    ->  recipe                                   preview  dE")
    for i, c, desc, pv, de in report:
        flag = "" if de < 3.5 else ("  (close)" if de < 7 else "  (weak match)")
        print("  %3d  %s ->  %-40s %s %5.1f%s" % (i, c, desc, pv, de, flag))
    for grp in merged:
        print("  note: colours %s are near-identical and share one filament" % ", ".join(map(str, grp)))
    print("\n%d mixed filaments, %d filament slots total." % (len(mixes), n))
    if changed:
        print("%d pick(s) changed by hand." % changed)
    print("Wrote: %s" % out)

    return out


def main():
    args = sys.argv[1:]
    pause = "--no-pause" not in args
    gui = "--auto" not in args
    files = [a for a in args if not a.startswith("--")]
    try:
        if not files:
            print(__doc__)
            return
        base = json.load(open(BASE_FILE))
        recipes = base["recipes"]
        if os.path.exists(STUDIO_RECIPES):      # prefer the installed (possibly newer) table
            try:
                live = [e for e in json.load(open(STUDIO_RECIPES))["entries"]
                        if e["mode"] == "CMYKW" and e["material"] == "PLA Palette"]
                if live:
                    recipes = live
            except Exception:
                pass
        for f in files:
            if not f.lower().endswith(".3mf"):
                print("Skipping %s (not a .3mf)" % f)
                continue
            try:
                with zipfile.ZipFile(f) as zz:
                    is_prusa = ("Metadata/Slic3r_PE.config" in zz.namelist()
                                and "Metadata/project_settings.config" not in zz.namelist())
                if is_prusa:
                    if HERE not in sys.path:
                        sys.path.insert(0, HERE)
                    import prusa2h3c
                    prusa2h3c.convert(f, base)
                else:
                    convert(f, base, recipes, gui=gui)
            except SystemExit as e:
                print("! %s" % e)
            except Exception as e:
                print("! Failed on %s: %r" % (f, e))
    finally:
        if pause:
            input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
