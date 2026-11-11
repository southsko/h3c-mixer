# H3C Mixer

Drag-and-drop converter that takes a color-painted Bambu Studio `.3mf` and
retargets it to a **Bambu Lab H3C** printing from five **PLA Palette** spools
(White, Cyan, Magenta, Yellow, Black), turning every painted color into a
**mixed filament**.

Built for MakerLab "Image to 3D" exports, but it works on any painted 3MF.

## What it does

Writes `<name>_H3C_mixed.3mf` next to the original (the original is never touched):

| | |
|---|---|
| Printer | Bambu Lab H3C 0.4 nozzle |
| Process | `0.12mm High Quality @BBL H3C` (stock preset, not "modified") |
| Mixed-color sublayers | on |
| Spools | 1 White · 2 Cyan · 3 Magenta · 4 Yellow · 5 Black — `Bambu PLA Palette @BBL H3C` |
| Colors | each painted color → a mixed filament (or a solid spool if it's close enough) |
| Prime tower | enabled; position left as in the source file — place it yourself |
| Objects / supports / plates | untouched |

### How colors are matched

* Recipes come from Bambu Studio's own **measured** C+M+Y+K+W PLA Palette data
  (`resources/filament_mixing/standard_color_recipes.json`). The installed copy is
  used if Bambu Studio is present; a bundled copy is the fallback.
* In-between ratios (5 % steps, 15 % minimum per spool) are interpolated in Lab
  exactly the way Studio previews them, so the preview color Studio shows is the
  color the script scored.
* All colors are solved **together**: colors that differ in the model are kept
  apart in the print instead of collapsing into one mix.
* Matches are scored with **CIEDE2000** (set `USE_DE2000 = False` for Studio's CIE76).
* Painted triangles are remapped at the bit level, including subdivided
  (split) triangles, so nothing is repainted or lost.

The report tells you how close each color got. Dark, saturated colors (navy,
wine red, deep purple) are outside what CMYKW can mix — those need a real spool.

## Use

Drop one or more `.3mf` files onto **`Drop 3MF here.bat`** (Windows), or:

```
python h3c_mix.py file.3mf [more.3mf ...] [--no-pause]
```

Needs Python 3.8+, no extra packages. `h3c_palette_base.json` must stay next to
the script.

> Tip: export the painted file with its **raw hex colors**. MakerLab's
> "reduced color" option can snap colors to Bambu catalog colors first, and the
> script can only match what's in the file.

## Credits

Color data and preview math follow [BambuStudio](https://github.com/bambulab/BambuStudio)
(`ColorDecomposeRecipe.cpp`, `FilamentMixer.cpp`, `TriangleSelector.cpp`).
`h3c_palette_base.json` contains H3C profile defaults and the CMYKW PLA Palette
recipe table from Bambu Studio 2.9.1. Not affiliated with Bambu Lab.
