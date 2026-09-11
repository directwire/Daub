"""Display names for the calibrated brush pool (brush names read as
plain Chinese in the app).

The RAW preset keys (ink_calib / plan JSON / MCP values) are the
fail-loud contract and never change - this table only drives DISPLAY
text, and `norm()` maps a display string back to its raw key so combos
can accept either.

  from preset_names import zh, norm   # zh(raw)->display, norm(either)->raw
"""

PRESET_ZH = {
    # ---- the original 7 ----
    "b) Basic-2 Opacity": "圆头笔（压感浓淡）",
    "b) Basic-5 Size": "圆头笔（压感粗细）",
    "d) Ink-1 Precision": "精细勾线笔",
    "d) Ink-2 Fineliner": "针管笔",
    "d) Ink-3 Gpen": "漫画 G 笔",
    "d) Ink-8 Sumi-e": "水墨笔",
    "b) Airbrush Soft": "喷枪（柔边）",
    # ---- 09-07 pool expansion (20) ----
    "Ink ballpen": "圆珠笔",
    "Ink circle 10": "圆头墨笔",
    "Fill circle": "填色圆头笔",
    "Basic tip soft": "软头笔",
    "Airbrush pressure": "喷枪（压感）",
    "c) Pencil-1 Hard": "硬铅笔",
    "c) Pencil-3 Large 4B": "4B 铅笔",
    "c) Pencil-5 Tilted": "侧锋铅笔",
    "h) Charcoal Pencil Medium": "木炭条",
    "h) Chalk Soft": "软粉笔",
    "h) Chalk Grainy": "颗粒粉笔",
    "e) Marker Chisel Smooth": "平头马克笔",
    "e) Marker Dry": "干涩马克笔",
    "f) Bristles-1 Details": "细鬃毛笔",
    "f) Bristles-3 Large Smooth": "宽鬃毛笔",
    "i) Wet Paint": "湿颜料笔",
    "j) Watercolor Fringe": "水彩（晕边）",
    "g) Dry Brushing": "干刷（飞白）",
    "g) Dry Bristles": "干鬃毛刷",
}

_ZH2RAW = {v: k for k, v in PRESET_ZH.items()}


def zh(raw):
    """Display name for a raw preset key (passthrough when unknown)."""
    return PRESET_ZH.get(raw, raw)


def norm(text):
    """Raw key for whatever a combo handed us (Chinese name, raw key,
    or any free-typed value - unknowns pass through for the engine's
    fail-loud guard to judge)."""
    t = (text or "").strip()
    return _ZH2RAW.get(t, t)
