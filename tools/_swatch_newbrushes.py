"""Acceptance swatch for the brush-pool expansion: one plan painting
every NEW preset twice (fine line size 12 with a pressure ramp, bold
wave size 44), rendered through daub itself. Eyeball acceptance gate.

  pack_venv/Scripts/python.exe tools/_swatch_newbrushes.py <out.png>
"""

import json
import math
import os
import sys

NEW = [
    "Ink ballpen", "Ink circle 10", "Fill circle", "Basic tip soft",
    "Airbrush pressure",
    "c) Pencil-1 Hard", "c) Pencil-3 Large 4B", "c) Pencil-5 Tilted",
    "h) Charcoal Pencil Medium",
    "e) Marker Chisel Smooth", "e) Marker Dry",
    "f) Bristles-1 Details", "f) Bristles-3 Large Smooth",
    "i) Wet Paint", "j) Watercolor Fringe",
    "g) Dry Brushing", "g) Dry Bristles",
    "h) Chalk Soft", "h) Chalk Grainy",
]

W, H = 1700, 120 + len(NEW) * 170 + 60
INK = "#2e4356"


def fine_row(y):
    """size-12 line, pressure ramp 0.2 -> 1.0 -> 0.3 (taper both ends)."""
    pts = []
    n = 46
    for k in range(n):
        t = k / (n - 1)
        p = 0.2 + 0.8 * math.sin(math.pi * min(t * 1.25, 1.0)) ** 0.8
        x = 60 + t * 1000
        pts.append([round(x, 1), float(y), round(min(p, 1.0), 3)])
    return pts


def bold_wave(y):
    """size-44 wave, full pressure - shows texture / edge character."""
    pts = []
    n = 56
    for k in range(n):
        t = k / (n - 1)
        x = 60 + t * 1000
        yv = y + 26 * math.sin(t * 2.4 * math.pi)
        pts.append([round(x, 1), round(yv, 1), 1.0])
    return pts


def main():
    out_png = sys.argv[1] if len(sys.argv) > 1 else "swatch.png"
    strokes = []
    y = 110
    for name in NEW:
        strokes.append({"layer": "fine", "preset": name, "size": 12,
                        "opacity": 1.0, "color": INK,
                        "points": fine_row(y)})
        strokes.append({"layer": "bold", "preset": name, "size": 44,
                        "opacity": 1.0, "color": INK,
                        "points": bold_wave(y + 62)})
        y += 170
    plan = {"reference": "swatch", "canvas": [W, H], "seed": 20260907,
            "bg": [250, 250, 250], "detail_rois": [], "count": len(strokes),
            "strokes": strokes}
    out_json = os.path.splitext(out_png)[0] + ".json"
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
    print(out_json)


if __name__ == "__main__":
    main()
