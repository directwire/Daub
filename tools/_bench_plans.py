#!/usr/bin/env python3
"""Generate the deterministic benchmark plans behind README's Performance
table. Pure stdlib, fixed seed - the same bytes on any machine, so every
number in the table can be reproduced exactly:

    python tools/_bench_plans.py --out bench        # writes the plans
    target/release/daub render bench/big54.json --out big54.png
    target/release/daub render bench/kra2.json --out kra2.png --kra kra2.kra
    python tools/render_timelapse.py bench/vid31.json vid.mp4                 # incremental
    DAUB_TIMELAPSE_LEGACY=1 python tools/render_timelapse.py bench/vid31.json vid_legacy.mp4

Stroke shapes follow the real pipeline's conventions (3-point strokes,
pressure 0.05-0.95, calibrated presets, first-appearance layer stack).
"""

import argparse
import json
import math
import os
import random

PRESETS = ["b) Basic-2 Opacity", "b) Basic-5 Size", "d) Ink-2 Fineliner",
           "d) Ink-3 Gpen", "d) Ink-8 Sumi-e"]
COLORS = ["#2244cc", "#883322", "#227744", "#442288", "#886622"]
# size distribution mirrors the real pipeline (median ~12, occasional
# broad brushes); lengths follow real stroke statistics (mean ~12% of
# canvas width, median ~7%) measured on the vendored sample plan.
SIZES = [4, 6, 8, 12, 16, 24, 32, 48]
SIZE_W = [12, 20, 18, 20, 12, 10, 5, 3]


def plan(canvas, count, layers, seed):
    rng = random.Random(seed)
    strokes = []
    w, h = canvas
    for i in range(count):
        x = rng.uniform(w * 0.05, w * 0.95)
        y = rng.uniform(h * 0.05, h * 0.95)
        pts = [[round(x, 1), round(y, 1),
                round(min(1.0, max(0.05, rng.gauss(0.8, 0.18))), 2)]]
        for _ in range(2):
            step = max(0.01, abs(rng.gauss(0.06, 0.05))) * w
            ang = rng.uniform(0, 2 * math.pi)
            x = min(w * 0.98, max(w * 0.02, x + math.cos(ang) * step))
            y = min(h * 0.98, max(h * 0.02, y + math.sin(ang) * step))
            pts.append([round(x, 1), round(y, 1),
                        round(min(1.0, max(0.05, rng.gauss(0.8, 0.18))), 2)])
        strokes.append({
            "layer": layers[i % len(layers)],
            "preset": PRESETS[i % len(PRESETS)],
            "size": rng.choices(SIZES, SIZE_W)[0],
            "opacity": round(rng.uniform(0.5, 1.0), 2),
            "color": COLORS[i % len(COLORS)],
            "points": pts,
        })
    return {"canvas": list(canvas), "bg": [246, 244, 238],
            "strokes": strokes, "count": len(strokes)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    specs = [
        # (name, canvas, strokes, layer stack, seed)
        ("big54.json", (2000, 2000), 54_000,
         ["L%d" % i for i in range(10)], 20260911),
        ("kra2.json", (2000, 2000), 2_000, ["L0", "L1"], 20260912),
        ("vid31.json", (2000, 2000), 31_000,
         ["L%d" % i for i in range(6)], 20260913),
    ]
    for name, canvas, count, layers, seed in specs:
        doc = plan(canvas, count, layers, seed)
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        print("%s  %d strokes x %d layers  %.1f KB"
              % (name, count, len(layers), os.path.getsize(path) / 1024))


if __name__ == "__main__":
    main()
