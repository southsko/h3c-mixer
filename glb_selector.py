"""
Colour-count selector for glb2bambu: shows the model painted with N colours (front
and side), the palette, and how the colour error falls as N grows, with a suggested
N marked. Returns the chosen N, or None if cancelled.
"""
import numpy as np
from PIL import Image

VIEW_PX = 300


class Previewer:
    """Rasterises the mesh once into per-pixel triangle-id maps (front + side, back faces
    culled, painter's order). Recolouring for any palette is then a single lookup."""

    def __init__(self, prep, px=VIEW_PX):
        from PIL import ImageDraw
        P, F = prep["P"], prep["F"]
        self.maps = []
        for view in ("front", "side"):
            V = P if view == "front" else P[:, [1, 0, 2]] * np.array([1, -1, 1])
            x, y, depth = V[:, 0], V[:, 2], V[:, 1]           # camera looks along +y
            tri = V[F]
            nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            vis = np.where(nrm[:, 1] < 0)[0]
            s = (px - 12) / max(np.ptp(x), np.ptp(y), 1e-9)
            X = (x - x.min()) * s + (px - np.ptp(x) * s) / 2
            Y = px - ((y - y.min()) * s + (px - np.ptp(y) * s) / 2)
            vis = vis[np.argsort(-depth[F[vis]].mean(1))]       # far to near
            img = Image.new("RGB", (px, px), (0, 0, 0))
            dr = ImageDraw.Draw(img)
            for i in vis:                                       # id+1 packed into RGB
                a, b, c = F[i]
                v = int(i) + 1
                dr.polygon([(X[a], Y[a]), (X[b], Y[b]), (X[c], Y[c])],
                           fill=((v >> 16) & 255, (v >> 8) & 255, v & 255))
            arr = np.asarray(img).astype(np.int64)
            self.maps.append((arr[..., 0] << 16 | arr[..., 1] << 8 | arr[..., 2]) - 1)
        self.px = px

    def render(self, face_rgb):
        """face_rgb: (F,3) uint8 -> PIL image with both views side by side."""
        out = []
        for m in self.maps:
            img = np.full((self.px, self.px, 3), 255, np.uint8)
            hit = m >= 0
            img[hit] = face_rgb[m[hit]]
            out.append(img)
        return Image.fromarray(np.hstack(out))


def face_colours_for(prep, cent):
    """Assign every triangle to the nearest centroid; return (F,3) sRGB."""
    from glb2bambu import assign, oklab_to_srgb
    lbl = assign(prep["lab"], cent)
    pal = oklab_to_srgb(cent).astype(np.uint8)
    return pal[lbl], pal


def choose(prep, curve, suggested, reason, _test_hook=None):
    import tkinter as tk
    from PIL import ImageTk

    ks = sorted(curve)
    root = tk.Tk()
    root.title("GLB colours - %s" % prep["path"].replace("\\", "/").split("/")[-1])
    status = tk.StringVar(value="Building preview...")
    tk.Label(root, textvariable=status, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
    root.update()
    pv = Previewer(prep)

    result = {"n": None}
    n_var = tk.IntVar(value=suggested)

    img_lbl = tk.Label(root)
    img_lbl.pack(padx=10, pady=6)
    pal_cv = tk.Canvas(root, height=30, highlightthickness=0)
    pal_cv.pack(fill="x", padx=10)

    row = tk.Frame(root)
    row.pack(fill="x", padx=10, pady=6)
    tk.Label(row, text="Colours:").pack(side="left")
    scale = tk.Scale(row, from_=ks[0], to=ks[-1], orient="horizontal", variable=n_var,
                     length=420, showvalue=True, resolution=1)
    scale.pack(side="left", padx=6)
    tk.Button(row, text="Use suggestion (%d)" % suggested,
              command=lambda: (n_var.set(suggested), update())).pack(side="left", padx=6)

    W, H, pad = 640, 130, 28
    chart = tk.Canvas(root, width=W, height=H, highlightthickness=0, bg="white")
    chart.pack(padx=10)
    errs = [curve[k][0] for k in ks]
    emax, emin = max(errs), 0.0

    def cx(k):
        return pad + (k - ks[0]) / max(1, ks[-1] - ks[0]) * (W - 2 * pad)

    def cy(e):
        return H - pad + 8 - (e - emin) / max(emax - emin, 1e-9) * (H - 2 * pad)

    def draw_chart(cur):
        chart.delete("all")
        chart.create_text(pad, 10, anchor="w", fill="#555",
                          text="colour error vs number of colours (lower = closer to the texture)")
        pts = [(cx(k), cy(e)) for k, e in zip(ks, errs)]
        chart.create_line(*[c for p in pts for c in p], fill="#888", width=2)
        for k in (ks[0], 8, 16, 24, ks[-1]):
            if ks[0] <= k <= ks[-1]:
                chart.create_text(cx(k), H - 8, text=str(k), fill="#777")
        sx, sy = cx(suggested), cy(curve[suggested][0])
        chart.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, outline="#1E9E5A", width=2)
        chart.create_text(sx, sy - 14, text="suggested", fill="#1E9E5A")
        ux, uy = cx(cur), cy(curve[cur][0])
        chart.create_oval(ux - 4, uy - 4, ux + 4, uy + 4, fill="#1E66F5", outline="")

    state = {"after": None, "photo": None}

    def update(*_):
        k = int(n_var.get())
        cent = curve[k][1]
        rgb, pal = face_colours_for(prep, cent)
        photo = ImageTk.PhotoImage(pv.render(rgb))
        state["photo"] = photo
        img_lbl.configure(image=photo)
        pal_cv.delete("all")
        sw = min(30, max(10, (W - 10) // max(1, len(pal))))
        order = np.argsort(-np.bincount(np.argmin(((prep["lab"][:, None] - cent[None]) ** 2).sum(-1)[::7], 1),
                                        minlength=len(pal)))
        for j, i in enumerate(order):
            c = "#%02X%02X%02X" % tuple(int(v) for v in pal[i])
            pal_cv.create_rectangle(j * sw, 2, j * sw + sw - 2, 28, fill=c, outline="#999")
        draw_chart(k)
        status.set("%d colours%s.  Bambu Studio allows up to 32 filaments (plus 64 mixed). "
                   "Preview is approximate; the final palette is refined." %
                   (k, "  (suggested: %s)" % reason if k == suggested else ""))

    def on_slide(_):
        if state["after"]:
            root.after_cancel(state["after"])
        state["after"] = root.after(150, update)

    scale.configure(command=on_slide)

    # ---- options (everything is set here; nothing needs command-line flags)
    ext = prep["P"].max(0) - prep["P"].min(0)
    base_size = float(ext.max())
    opt = tk.Frame(root)
    opt.pack(fill="x", padx=10, pady=(8, 0))
    tk.Label(opt, text="Size (longest side, mm):").pack(side="left")
    size_var = tk.StringVar(value="%g" % round(base_size, 1))
    size_entry = tk.Entry(opt, textvariable=size_var, width=7)
    size_entry.pack(side="left", padx=4)
    dims = tk.StringVar()
    tk.Label(opt, textvariable=dims, fg="#555").pack(side="left", padx=6)
    h3c_var = tk.BooleanVar(value=True)
    tk.Checkbutton(opt, text="Then convert to H3C PLA Palette CMYKW mixes",
                   variable=h3c_var).pack(side="right")
    err = tk.StringVar()
    tk.Label(root, textvariable=err, fg="#C0392B", anchor="w").pack(fill="x", padx=10)

    def read_size():
        try:
            v = float(size_var.get())
        except ValueError:
            return None
        return v if 5.0 <= v <= 320.0 else None

    def show_dims(*_):
        v = read_size()
        if v is None:
            dims.set("")
            err.set("Enter a size between 5 and 320 mm.")
            return
        err.set("")
        d = ext * (v / base_size)
        dims.set("= %.0f x %.0f x %.0f mm" % (d[0], d[1], d[2]))
    size_var.trace_add("write", show_dims)
    show_dims()

    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10, pady=8)

    def done(val):
        result["n"] = val
        root.destroy()

    def make():
        v = read_size()
        if v is None:
            err.set("Enter a size between 5 and 320 mm.")
            size_entry.focus_set()
            return
        done({"n": int(n_var.get()), "size_mm": v, "make_h3c": bool(h3c_var.get())})
    tk.Button(bar, text="Make file", width=14, command=make).pack(side="right", padx=4)
    tk.Button(bar, text="Cancel", width=12, command=lambda: done(None)).pack(side="right", padx=4)
    root.protocol("WM_DELETE_WINDOW", lambda: done(None))
    update()
    if _test_hook:
        _test_hook(root, {"n": n_var, "size": size_var, "h3c": h3c_var, "update": update,
                          "make": make, "cancel": lambda: done(None)})
    root.mainloop()
    return result["n"]
