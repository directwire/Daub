"""Headless correction loop for existing plans: refine / topup /
groundfill, judged against daub's own render - the live-canvas
correction trio ported to the daub side to exploit the sub-second
re-render.

The live-canvas trio (stroke_engine refine/apply_refine/reconcile/
topup) was born where pixels are expensive: erasing has
halo and neighbour damage, exporting is slow, so it replays an eraser
along each offender's path and reconciles patches against the
directory. Here the directory IS the canvas, so the same algorithms
degrade into pure data-domain operations:

  erase    = delete strokes from the plan JSON (no halo, no neighbour
             damage - the live trio's honesty limit simply disappears)
  truth    = a real daub render of the current plan (sub-second,
             bit-deterministic). NOT the planner's internal sim - the
             sim overestimates coverage and trusting it headless left
             paper speckle everywhere (stroke_engine's own measure_hook
             lesson, and the whole reason paint_headless exists)
  apply    = re-render; reconcile = unnecessary (directory == canvas)

Corrective strokes come from stroke_engine._plan_strokes restricted to
the region boxes, re-measured per pass through a daub-render
measure_hook (the paint_headless pattern). Offenders are the live
trio's rule verbatim: the stroke's highest-pressure point falls inside
a region (optionally layer-filtered).

Every round scores each region (mean |diff| 0-255 + within60 %) and
the loop stops on any of three gates - target reached, marginal gain
below stop_gain, rounds budget. The flower patch measured round1
-32.9 / round2 -3.2: the plateau is real, so the report says
"plateau" instead of pretending rounds converge.

Determinism: fixed planner seed (20260904, the live trio's), daub
renders are bit-deterministic, no wall-clock in any output byte - two
runs produce identical refined plans.

Layout note: this module must stay importable WITHOUT numpy/PIL/
stroke_engine (daub_mcp imports it for the edit_plan prune op), so
all heavy imports live inside the functions that need them.
"""

import hashlib
import json
import os
import random
import subprocess
import time

PLANNER_SEED = 20260904     # the live trio's seed, kept for parity
DEFAULT_STOP_GAIN = 2.0     # mean|diff| points; flower round2 was -3.2
DEFAULT_ROUNDS = 4

# Shown wherever plan/refine machinery is asked for without a planner.
# The open-source repository ships the renderer; plans are just data
# it consumes (--render-only). The planner stays a separate checkout.
NO_PLANNER = ("stroke_engine (the planner) is not part of this "
              "repository - daub ships the renderer only. Rendering "
              "existing plans (--render-only) works out of the box; "
              "plan / refine / probe need the planner: set "
              "DAUB_KRMCP_TOOLS to a directory containing "
              "stroke_engine.py (see README, 'Bring your own "
              "planner').")

# first-appearance stack order of a full band plan; new layer names
# from _finalize_layers take their slot from this list (inserted before
# the first existing layer that canonically sits above them)
CANONICAL_LAYERS = ["F1", "L52", "L26", "L13", "L7", "L5", "L0",
                    "X1C", "X1M", "X1F"]

# F1 is the flat base coat from plan()'s extract_flat_strokes prologue;
# _plan_strokes (band + detail-ROI replanning) never re-plans it, so a
# refine round that erases F1 ink just deletes coverage (first face-box
# run: 420 F1 strokes erased, 0 repainted, score 38.93 -> 46.79). The
# offender rule therefore defaults to the replannable layers; an
# explicit layers= always wins verbatim.
NON_REPLANNABLE = {"F1"}


# ------------------------------------------------------------- plan I/O

def load_plan(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc.get("strokes"), list):
        raise ValueError("not a daub plan: %s" % path)
    return doc


def plan_fingerprint(strokes):
    """sha256 over the strokes array - the stale-patch guard: a report
    records what it edited, and anything re-applied onto a different
    array fails loud instead of silently double-editing."""
    blob = json.dumps(strokes, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def parse_regions(raw, canvas):
    """Accepts [[x0,y0,x1,y1], ...] (MCP) or "x0,y0,x1,y1;..." (CLI).
    Validates against the canvas - refine refuses to run wholesale."""
    if isinstance(raw, str):
        boxes = []
        for part in raw.split(";"):
            part = part.strip()
            if part:
                v = [int(round(float(x))) for x in part.split(",")]
                if len(v) != 4:
                    raise ValueError("region %r is not x0,y0,x1,y1" % part)
                boxes.append(v)
        raw = boxes
    if not raw:
        raise ValueError("refine needs region boxes (wholesale redo is "
                         "what a fresh daub_paint plan is for)")
    W, H = int(canvas[0]), int(canvas[1])
    out = []
    for b in raw:
        x0, y0, x1, y1 = [int(v) for v in b]
        if not (0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H):
            raise ValueError("region %s outside canvas %dx%d or inverted"
                             % (b, W, H))
        out.append([x0, y0, x1, y1])
    return out


# ---------------------------------------------------------- stroke ops

def anchor_offenders(strokes, regions, layers=None):
    """The live trio's rule: a stroke is an offender when its
    highest-pressure point falls inside one of the boxes. Data-domain:
    the returned strokes are references into the caller's list."""
    if layers is not None:
        known = {s.get("layer") for s in strokes}
        for name in layers:
            if name not in known:
                raise ValueError("no layer named %r (have: %s)"
                                 % (name, sorted(known)))
    inside = lambda x, y: any(x0 <= x < x1 and y0 <= y < y1
                              for x0, y0, x1, y1 in regions)
    out = []
    for s in strokes:
        if layers is not None and s.get("layer") not in layers:
            continue
        pts = s.get("points") or []
        if not pts:
            continue
        anchor = max(pts, key=lambda p: p[2])
        if inside(anchor[0], anchor[1]):
            out.append(s)
    return out


def merge_strokes(base, additions, to_bottom=()):
    """Append additions without ever reordering existing strokes (the
    GUI's law). Existing layers: their strokes land at the end of the
    array - same layer group, painted last within the layer, stack slot
    untouched. NEW layer names take their stack slot from
    CANONICAL_LAYERS (inserted before the first existing layer that
    canonically sits above); names in `to_bottom` (groundfill's U
    layer) go to array position 0 = bottom of the stack; unknown names
    ride on top (end). Returns (merged, layers_added)."""
    existing = []
    for s in base:
        name = s.get("layer")
        if name not in existing:
            existing.append(name)

    def spot_for(name):
        above = CANONICAL_LAYERS[CANONICAL_LAYERS.index(name) + 1:]
        for up in above:
            for i, s in enumerate(base):
                if s.get("layer") == up:
                    return i
        return len(base)          # nothing canonical above -> ride on top

    groups = {}
    for s in additions:
        groups.setdefault(s.get("layer"), []).append(s)
    out = list(base)
    added = []
    for name, group in groups.items():
        if name in to_bottom:
            out[0:0] = group
        elif name in existing or name not in CANONICAL_LAYERS:
            out.extend(group)
        else:
            out[spot_for(name):spot_for(name)] = group
        if name not in existing and name not in added:
            added.append(name)
    return out, added


# ------------------------------------------------------------- metrics

def region_metrics(ref_im, render_im, regions):
    """Per-region mean |diff| (0-255, channel-mean) + within60 % of
    pixels whose max-channel diff is <= 60 - the project's metric
    family (kimono gate semantics)."""
    import numpy as np
    a = np.asarray(ref_im.convert("RGB"), dtype=np.int16)
    b = np.asarray(render_im.convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        raise ValueError("render %s != ref %s"
                         % (b.shape[:2][::-1], a.shape[:2][::-1]))
    dmax = np.abs(a - b).max(axis=2)
    rows = []
    for x0, y0, x1, y1 in regions:
        dm = dmax[y0:y1, x0:x1]
        rows.append({
            "box": [x0, y0, x1, y1],
            "mean_abs": round(float(dm.astype(np.float64).mean()), 2),
            "within60": round(float((dm <= 60).mean()) * 100.0, 1),
            "px": int(dm.size)})
    return rows


def score(rows):
    """Area-weighted mean|diff| over all region boxes - the loop's
    single gate number."""
    num = sum(r["mean_abs"] * r["px"] for r in rows)
    den = sum(r["px"] for r in rows) or 1
    return round(num / den, 2)


# -------------------------------------------------------------- render

class Renderer:
    """daub subprocess wrapper: a plan doc (verbatim header + strokes)
    in, PIL image out. Truth = daub's own raster, never the sim."""

    def __init__(self, daub, cal, tips, workdir):
        self.daub = daub
        self.cal = cal
        self.tips = tips
        self.workdir = workdir
        self.n = 0
        os.makedirs(workdir, exist_ok=True)

    def render(self, header, strokes, keep_png=False):
        import copy
        from PIL import Image
        self.n += 1
        tmp_doc = os.path.join(self.workdir, "refine_%03d.json" % self.n)
        out_png = os.path.abspath(tmp_doc[:-5] + ".png")
        doc = dict(copy.deepcopy(header))
        doc["strokes"] = strokes
        doc["count"] = len(strokes)
        with open(tmp_doc, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        cmd = [self.daub, "render", tmp_doc, "--out", out_png,
               "--cal", self.cal, "--tips", self.tips]
        r = subprocess.run(cmd, capture_output=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.isfile(out_png) \
                or os.path.getsize(out_png) <= 0:
            raise RuntimeError("daub render failed rc=%s\n%s\n%s"
                               % (r.returncode, r.stdout, r.stderr))
        im = Image.open(out_png)
        im.load()
        im = im.convert("RGB")
        if not keep_png:
            try:
                os.remove(out_png)
            except OSError:
                pass
        try:
            os.remove(tmp_doc)
        except OSError:
            pass
        return im


def _project(s):
    """Planner-internal stroke -> final dict (paint_headless's formula,
    verbatim: layer from band, opacity = _op + 0.35*contrast)."""
    return {"layer": "L%d" % int(s["band"][0]),
            "preset": s["_preset"],
            "size": int(round(s["size"])),
            "opacity": round(min(1.0, s["_op"] + 0.35 * s["contrast"]), 3),
            "color": s["color"], "points": s["points"]}


# ---------------------------------------------------------------- loop

def run(plan_path, out_png, regions, mode="refine", layers=None,
        rounds=DEFAULT_ROUNDS, max_passes=None, stop_gain=DEFAULT_STOP_GAIN,
        target=None, groundfill_size=20, ref_path=None,
        daub=None, cal=None, tips=None, workdir=None, log=print):
    """The correction loop. Returns the report dict (also written to
    <out_png stem>_refine_report.json). The input plan is never
    overwritten; the refined plan lands at <stem>_refined_plan.json."""
    import copy
    try:
        import stroke_engine as se
    except ImportError:
        raise ValueError(NO_PLANNER)

    if daub is None or cal is None or tips is None:
        raise ValueError("run() needs explicit daub/cal/tips paths "
                         "(the caller resolves them)")
    t0 = time.time()
    plan_path = os.path.abspath(plan_path)
    src = load_plan(plan_path)
    header = {k: copy.deepcopy(v) for k, v in src.items()
              if k not in ("strokes", "count")}
    canvas = header.get("canvas")
    if not canvas:
        raise ValueError("plan has no canvas header: %s" % plan_path)
    regions = parse_regions(regions, canvas)

    ref_path = ref_path or header.get("reference")
    if not ref_path or not os.path.isfile(ref_path):
        raise ValueError("reference image not resolvable (%r) - pass "
                         "ref_path" % ref_path)
    from PIL import Image
    ref = Image.open(ref_path).convert("RGB")
    if list(ref.size) != [int(canvas[0]), int(canvas[1])]:
        raise ValueError("reference %dx%d != plan canvas %s"
                         % (ref.size[0], ref.size[1], canvas))
    if workdir is None:
        workdir = os.path.splitext(out_png)[0] + "_refine_work"
    rnd = Renderer(daub, cal, tips, workdir)

    base = src["strokes"]
    base_fp = plan_fingerprint(base)
    out_plan = os.path.splitext(os.path.abspath(out_png))[0] \
        + "_refined_plan.json"
    report_path = os.path.splitext(os.path.abspath(out_png))[0] \
        + "_refine_report.json"

    def log_json(msg):
        log(json.dumps(msg, ensure_ascii=False))

    rounds_out = []
    prev_rows = region_metrics(ref, rnd.render(header, base), regions)
    prev = score(prev_rows)
    log_json({"round": 0, "score": prev, "per_region": prev_rows})
    rounds_out.append({"round": 0, "score": prev, "per_region": prev_rows})
    stopped_by = "rounds"
    if mode == "groundfill":
        mode_rounds = ["groundfill"]     # single shot: one block coat
    elif mode == "auto":
        mode_rounds = ["refine"] + ["topup"] * (max(1, rounds) - 1)
    else:
        mode_rounds = [mode] * max(1, rounds)
    for rno, rmode in enumerate(mode_rounds, start=1):
        t1 = time.time()
        added = []
        if rmode == "groundfill":
            gf, u_layer = se.plan_groundfill(
                ref, rnd.render(header, base), regions,
                size=groundfill_size, colors=12, gate=45,
                skip_carried=False, row_frac=0.3, along_frac=0.3,
                min_run=1.0)   # headless fractions: capsules don't bloom
            gf = [{"layer": s["layer"], "preset": s["preset"],
                   "size": int(round(s["size"])), "opacity": s["opacity"],
                   "color": s["color"], "points": s["points"]} for s in gf]
            base, added = merge_strokes(base, gf, to_bottom=(u_layer,))
            erased, painted = 0, len(gf)
        elif rmode == "topup":
            seed = rnd.render(header, base)
            done = []

            def measure(ps):
                done.extend(_project(s) for s in ps)
                return rnd.render(header, base + done)

            correctives = se._plan_strokes(
                ref, seed, regions, random.Random(PLANNER_SEED), t0,
                tag="topup r%d " % rno, max_passes=max_passes,
                measure_hook=measure)
            se._finalize_layers(correctives)
            add = [{"layer": s["layer"], "preset": s["preset"],
                    "size": int(round(s["size"])), "opacity": s["opacity"],
                    "color": s["color"], "points": s["points"]}
                   for s in correctives]
            se._assign_layers_by_width(add)   # 统一尺子:
            # 补笔也按实测宽重划带层,否则名义带补笔会局部重引入大压小
            base, added = merge_strokes(base, add)
            erased, painted = 0, len(add)
        else:
            eff_layers = layers
            if eff_layers is None:      # default: replannable layers only
                present = []
                for s in base:
                    name = s.get("layer")
                    if name not in present:
                        present.append(name)
                eff_layers = [n for n in present
                              if n not in NON_REPLANNABLE]
            offenders = anchor_offenders(base, regions, eff_layers)
            if not offenders:
                log("no strokes anchored inside the regions - nothing "
                    "to erase (round %d)" % rno)
                stopped_by = "no_offenders"
                break
            doomed = {id(s) for s in offenders}
            pruned = [s for s in base if id(s) not in doomed]
            seed = rnd.render(header, pruned)
            done = []

            def measure(ps):
                done.extend(_project(s) for s in ps)
                return rnd.render(header, pruned + done)

            correctives = se._plan_strokes(
                ref, seed, regions, random.Random(PLANNER_SEED), t0,
                tag="refine r%d " % rno, max_passes=max_passes,
                measure_hook=measure)
            se._finalize_layers(correctives)
            add = [{"layer": s["layer"], "preset": s["preset"],
                    "size": int(round(s["size"])), "opacity": s["opacity"],
                    "color": s["color"], "points": s["points"]}
                   for s in correctives]
            se._assign_layers_by_width(add)   # 同上:补笔跟统一尺子
            base, added = merge_strokes(pruned, add)
            erased, painted = len(offenders), len(add)
        rows = region_metrics(ref, rnd.render(header, base), regions)
        cur = score(rows)
        gain = round(prev - cur, 2)
        entry = {"round": rno, "mode": rmode, "erased": erased,
                 "painted": painted, "layers_added": added,
                 "score": cur, "gain": gain, "per_region": rows,
                 "seconds": round(time.time() - t1, 1)}
        log_json(entry)
        rounds_out.append(entry)
        prev, prev_rows = cur, rows
        if target is not None and cur <= target:
            stopped_by = "target"
            break
        if gain < stop_gain:
            stopped_by = "stop_gain"
            log("plateau: gain %.2f < stop_gain %.2f - further rounds "
                "measured diminishing (flower patch: round1 -32.9, "
                "round2 -3.2)" % (gain, stop_gain))
            break

    out_png_abs = os.path.abspath(out_png)
    os.makedirs(os.path.dirname(out_png_abs) or ".", exist_ok=True)
    final_doc = dict(header)
    final_doc["strokes"] = base
    final_doc["count"] = len(base)
    with open(out_plan, "w", encoding="utf-8") as fh:
        json.dump(final_doc, fh, ensure_ascii=False)
        fh.write("\n")
    # the deliverable renders from the WRITTEN refined plan, so the png
    # on disk is provably a render of exactly that file
    cmd = [daub, "render", out_plan, "--out", out_png_abs,
           "--cal", cal, "--tips", tips]
    r = subprocess.run(cmd, capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(out_png_abs) \
            or os.path.getsize(out_png_abs) <= 0:
        raise RuntimeError("final render failed rc=%s\n%s\n%s"
                           % (r.returncode, r.stdout, r.stderr))

    report = {"kind": "daub_refine", "mode": mode,
              "source_plan": plan_path, "source_fingerprint": base_fp,
              "ref": os.path.abspath(ref_path),
              "regions": regions, "gates": {"rounds": rounds,
                                            "stop_gain": stop_gain,
                                            "target": target},
              "rounds": rounds_out, "stopped_by": stopped_by,
              "score_first": rounds_out[0]["score"],
              "score_final": prev,
              "out_plan": out_plan, "out_png": out_png_abs,
              "plan_fingerprint": plan_fingerprint(base),
              "seconds": round(time.time() - t0, 1)}
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    report["report_path"] = report_path
    log("refine done -> %s | png %s | report %s (%.1fs, %d rounds, "
        "score %s -> %s, stopped_by=%s)"
        % (out_plan, out_png_abs, report_path, report["seconds"],
           len(rounds_out) - 1, report["score_first"],
           report["score_final"], stopped_by))
    return report
