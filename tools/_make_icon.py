"""Paint the app icon with daub itself.

The brand IS the product: the icon is a real daub render - one bold
ink sweep lifting to the upper right, a thin echo returning, and a
vermillion seal - on ice-white paper. The 512px render is then clipped
to a rounded card (Apple-marketing squircle-ish, r=22.5%) and cut into
a multi-size .ico plus a 256px PNG preview.

  pack_venv/Scripts/python.exe tools/_make_icon.py
"""

import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAUB = os.path.join(ROOT, "target", "release", "daub.exe")
CAL = os.path.join(HERE, "data", "ink_calib.json")
TIPS = os.path.join(HERE, "data", "brush_lib.json")
WORK = os.path.join(os.environ.get("TEMP", "/tmp"), "daub_icon")

SIZE = 512
BG = (246, 249, 251)          # #f6f9fb ice-white paper
INK = "#2e4356"               # slate ink (the atelier's type colour)
INK_2 = "#55708a"             # thin echo
SEAL = "#c8403a"              # vermillion seal


def bezier(p0, p1, p2, n=16, p_from=0.9, p_to=0.35):
    """Quadratic bezier sampled with a lifting-stroke pressure taper."""
    pts = []
    for i in range(n):
        t = i / (n - 1)
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append([round(x), round(y), round(p_from + (p_to - p_from) * t,
                                              3)])
    return pts


def serpentine(x0, y0, x1, y1, rows, size):
    """A filled square-ish seal: serpentine sweep of a wide round nib."""
    pts = []
    step = (y1 - y0) / (rows - 1)
    for r in range(rows):
        y = y0 + r * step
        xs = (x0, x1) if r % 2 == 0 else (x1, x0)
        pts.append([round(xs[0]), round(y), 1.0])
        pts.append([round(xs[1]), round(y), 1.0])
    return pts


def plan():
    strokes = [
        # the main gesture: a confident sweep lifting to upper-right
        {"layer": "L1", "preset": "d) Ink-3 Gpen", "size": 54,
         "opacity": 1.0, "color": INK,
         "points": bezier((115, 378), (238, 332), (398, 148))},
        # the echo: thin, returning, lighter
        {"layer": "L2", "preset": "d) Ink-2 Fineliner", "size": 15,
         "opacity": 0.9, "color": INK_2,
         "points": bezier((152, 414), (268, 392), (410, 242),
                          p_from=0.75, p_to=0.15)},
        # the seal
        {"layer": "L3", "preset": "b) Basic-5 Size", "size": 44,
         "opacity": 1.0, "color": SEAL,
         "points": serpentine(368, 372, 412, 408, 6, 44)},
    ]
    return {"reference": "icon", "canvas": [SIZE, SIZE], "seed": 20260907,
            "bg": list(BG), "detail_rois": [], "count": len(strokes),
            "strokes": strokes}


def main():
    os.makedirs(WORK, exist_ok=True)
    pj = os.path.join(WORK, "icon_plan.json")
    with open(pj, "w", encoding="utf-8") as fh:
        json.dump(plan(), fh)
    png = os.path.join(WORK, "icon_512.png")
    r = subprocess.run(
        [DAUB, "render", pj, "--out", png, "--cal", CAL, "--tips", TIPS],
        capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(png):
        sys.exit("daub render failed:\n%s\n%s" % (r.stdout, r.stderr))

    img = Image.open(png).convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.width - 1, img.height - 1],
        radius=round(img.width * 0.225), fill=255)
    img.putalpha(mask)

    ico = os.path.join(HERE, "data", "app_icon.ico")
    img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                         (40, 40), (32, 32), (24, 24), (20, 20), (16, 16)])
    prev = os.path.join(WORK, "app_icon_256.png")
    img.resize((256, 256), Image.LANCZOS).save(prev)
    print("icon ok: %s (%s bytes), preview %s"
          % (ico, os.path.getsize(ico), prev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
