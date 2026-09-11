"""daub_paint: image in, painting out - the Krita-free production chain.

Bundles the full chain (stroke_engine plan -> daub render) behind one
invocation so an end user needs neither a Python env nor Krita:

  daub_paint <ref-image> <out.png> [--kra out.kra] [--psd out.psd]
             [--pen "preset"]
             [--timelapse out.mp4 [--timelapse-order small_first]]
             [--keep-plan]
  daub_paint --render-only <plan.json> <out.png> [--kra out.kra]
             [--psd out.psd] [--timelapse out.mp4]

--render-only skips planning and renders an existing plan JSON as-is -
the GUI's live-growth snapshots, layer-workbench re-renders and exports
all ride it (rendering an edited plan copy is a sub-second daub pass).
This is also the open-source mode: the repository ships the renderer,
not the planner - without stroke_engine on the path (see PLANNER below)
only --render-only works and everything else fails loud.

--timelapse rides render_timelapse: when the daub engine knows
`render-seq` the whole reveal sequence is one incremental BMP pass
(11.5x on a 31k-stroke plan); older engines fall back to the per-frame
loop silently.

--refine runs the headless correction loop (daub_refine) on the plan
given as REF: erase the strokes anchored in --regions, re-plan
corrective ink seeded on the real daub render, score each round against
the reference and stop on target / gain plateau / rounds budget.
Modes: refine / topup / groundfill / auto. The truth is daub's own
raster, never the planner's sim.

The plan JSON is written next to the output and kept by default - it is
the correction truth source (paint_topup / render_timelapse both replay
it); --keep-plan is accepted for habit compatibility and is a no-op.

Packaging (build_paint.py): stroke_engine / krmcp_port ride as code,
stroke_trace.py (loaded by stroke_engine via importlib from a file
path, invisible to PyInstaller's analysis) and the calib tables as data
at the bundle root, the vendored brush registry + tips under data/,
daub.exe as a binary. Everything resolves from sys._MEIPASS when frozen
and from tools/ when run straight from the repo.
"""

import argparse
import json
import os
import subprocess
import sys

# stroke_trace (data-file, loaded via importlib) does `from scipy
# import ndimage` and prefers `from skimage.morphology import
# skeletonize` - neither import is visible to PyInstaller's analysis
# from a data file, so import them here (an analyzed module) or the
# frozen runtime silently loses scipy / takes stroke_trace's Zhang-Suen
# fallback (different skeletons, diverging plans). Renderer-only use
# (--render-only) never touches the planner and needs neither - a
# missing scipy/skimage only breaks planning, so it's tolerated here
# and fails loud at the planner gate instead.
try:
    import scipy.ndimage  # noqa: F401
    import skimage.morphology  # noqa: F401
except ImportError:
    pass

import daub_refine   # top-level so PyInstaller's analysis follows it;
                     # the module itself is stdlib-only at import time

if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
    DAUB = os.path.join(BASE, "daub.exe")
    CAL = os.path.join(BASE, "ink_calib.json")
    TIPS = os.path.join(BASE, "data", "brush_lib.json")
    PLANNER = "bundled"     # stroke_engine rides in the bundle root
else:
    # dev: assets live in tools/data, the binary in target/release
    # (the bundle-root layout above exists only inside the exe)
    BASE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(BASE)
    sys.path.insert(0, BASE)
    DAUB = os.path.join(ROOT, "target", "release",
                        "daub.exe" if os.name == "nt" else "daub")
    CAL = os.path.join(BASE, "data", "ink_calib.json")
    TIPS = os.path.join(BASE, "data", "brush_lib.json")
    # The planner (stroke_engine) is NOT part of this repository - the
    # renderer works on existing plan JSONs without it. Resolution when
    # present: DAUB_KRMCP_TOOLS env, trusted verbatim (pointing it at
    # a missing tree fails loud, never silently re-routes). None =
    # plan/refine/probe fail loud with daub_refine.NO_PLANNER guidance.
    PLANNER = os.environ.get("DAUB_KRMCP_TOOLS")
    if PLANNER:
        if not os.path.isdir(PLANNER):
            raise SystemExit("DAUB_KRMCP_TOOLS=%r is not a directory"
                             % PLANNER)
        if not os.path.isfile(os.path.join(PLANNER, "stroke_engine.py")):
            raise SystemExit("DAUB_KRMCP_TOOLS=%r holds no stroke_engine.py"
                             % PLANNER)
        sys.path.insert(0, PLANNER)


def _import_planner():
    """stroke_engine, or exit loud with the open-source reality."""
    if not PLANNER:
        raise SystemExit(daub_refine.NO_PLANNER)
    import stroke_engine
    return stroke_engine


def _probe(se, ref_path):
    """Fingerprint the plan prologue (X1 width census -> F1 size grid).

    Dumps enough state to pin down WHERE a frozen-vs-dev planning drift
    enters: lib versions, the X1 measured-width census (vws), its median
    (= anchor), and the F1 size grid. Full arrays go to probe_dump.json
    in the CWD for element-level diffing; stdout gets the one-line
    hashes to compare by eye.
    """
    import hashlib
    import json as _json

    import numpy as np

    ref = se.Image.open(ref_path).convert("RGB")
    xs = se._auto_pen_pass(ref)
    vws = sorted(float(s.get("_vw", s["size"])) for s in xs)
    anchor = float(np.median(vws)) if vws else 10.0
    bg = se._border_bg(ref)
    here = os.path.dirname(os.path.abspath(se.__file__))
    stt = getattr(se, "_load_stroke_trace", None) and se._load_stroke_trace()
    if stt is None:  # replicate plan()'s own importlib load
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "stroke_trace", os.path.join(here, "stroke_trace.py"))
        stt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stt)
    with open(os.path.join(here, "fork_ink_calib.json")) as fh:
        cal = _json.load(fh)
    f1 = stt.extract_flat_strokes(ref, cal, anchor, colors=24, layer="F1",
                                  paper=bg)
    sizes = sorted(float(s["size"]) for s in f1)

    def h(v):
        return hashlib.sha256(
            repr(v).encode("utf-8")).hexdigest()[:16]
    import scipy
    try:
        import skimage
        skel = "skimage %s (skeletonize path)" % skimage.__version__
    except ImportError:
        skel = "zhang-suen FALLBACK (differs from the skimage plans!"
    print("numpy=%s pillow=%s scipy=%s skel=%s"
          % (np.__version__, se.Image.__version__, scipy.__version__, skel),
          flush=True)
    print("x1 n=%d anchor=%.10f vws=%s" % (len(vws), anchor, h(vws)),
          flush=True)
    print("f1 n=%d sizes=%s" % (len(sizes), h(sizes)), flush=True)
    with open("probe_dump.json", "w") as fh:
        _json.dump({"vws": vws, "anchor": anchor, "f1_sizes": sizes}, fh)
    print("dump -> probe_dump.json", flush=True)


def _render(plan_path, out_png, kra, psd=None):
    cmd = [DAUB, "render", plan_path, "--out", out_png,
           "--cal", CAL, "--tips", TIPS]
    if kra:
        cmd += ["--kra", os.path.abspath(kra)]
    if psd:
        cmd += ["--psd", os.path.abspath(psd)]
    # explicit utf-8: text=True alone decodes with the locale (GBK on
    # zh-CN Windows) and daub's stdout carries the output PATH - a
    # Chinese filename blows up the reader thread and r.stdout comes
    # back None even though the png is fine on disk
    r = subprocess.run(cmd, capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit("daub render failed:\n%s\n%s" % (r.stdout, r.stderr))
    # belt and braces: the png is already on disk at this point - a
    # lost/None stdout must not crash the exit code (the GUI's snapshot
    # chain gates on rc == 0)
    lines = (r.stdout or "").strip().splitlines()
    print(lines[-1] if lines else "daub render ok -> %s" % out_png,
          flush=True)
    return out_png


def _timelapse(plan_path, out_png, mp4, order="big_first"):
    # lazy import: render_timelapse is stdlib-only, but a module-level
    # import would still drag it through every frozen analysis; the
    # --paths HERE line in build_paint.py is what makes it bundleable.
    import render_timelapse as rt
    mp4 = os.path.abspath(mp4)
    workdir = os.path.splitext(mp4)[0] + "_frames"
    rt.run(plan_path, out_png, mp4, daub=DAUB, cal=CAL, tips=TIPS,
           order=order, workdir=workdir)


def main():
    ap = argparse.ArgumentParser(
        prog="daub_paint",
        description="image in, painting out (plan + daub render, "
                    "no Krita needed)")
    ap.add_argument("ref", help="reference image (any PIL-readable); "
                    "with --render-only: the plan JSON to render")
    ap.add_argument("out_png")
    ap.add_argument("--render-only", action="store_true",
                    help="skip planning; render the plan JSON given as "
                         "REF as-is (GUI snapshots / workbench exports)")
    ap.add_argument("--kra", default=None,
                    help="also write a layered .kra next to the png")
    ap.add_argument("--psd", default=None,
                    help="also write a layered .psd (Photoshop / Clip "
                         "Studio / Affinity open it directly)")
    ap.add_argument("--pen", default=None,
                    help="force one ink preset from the calibration table")
    ap.add_argument("--timelapse", default=None, metavar="OUT.mp4",
                    help="also render the paint-down video of the plan")
    ap.add_argument("--timelapse-order", default="big_first",
                    choices=("big_first", "small_first"),
                    help="reveal order of the video (default: flat bed "
                         "first; small_first = detail first, bed last)")
    ap.add_argument("--probe", action="store_true",
                    help="print runtime versions + planner anchor/size "
                         "fingerprints for the reference, then exit "
                         "(diagnosing frozen-vs-dev planning drift)")
    ap.add_argument("--keep-plan", action="store_true",
                    help="accepted for habit compatibility; the plan JSON "
                         "is always kept (correction truth source)")
    ap.add_argument("--refine", action="store_true",
                    help="correction loop on an existing plan instead of "
                         "planning from scratch: REF is the plan JSON, "
                         "OUT_PNG the deliverable; writes "
                         "<stem>_refined_plan.json + "
                         "<stem>_refine_report.json, never overwrites "
                         "the input plan")
    ap.add_argument("--regions", action="append", default=[],
                    metavar="X0,Y0,X1,Y1",
                    help="region box (repeatable / ;-separated) - refine "
                         "refuses to run without at least one")
    ap.add_argument("--mode", default="refine",
                    choices=["refine", "topup", "groundfill", "auto"],
                    help="refine: erase offenders + repaint; topup: "
                         "additive deficit only; groundfill: block coat "
                         "(single shot); auto: one refine then topups")
    ap.add_argument("--layers", default=None,
                    help="comma-separated layer names to restrict the "
                         "offender rule (refine mode)")
    ap.add_argument("--rounds", type=int, default=daub_refine.DEFAULT_ROUNDS,
                    help="outer round budget (default %(default)s)")
    ap.add_argument("--max-passes", type=int, default=None,
                    help="planner refill pass budget per round "
                         "(default: stroke_engine MAX_PASSES)")
    ap.add_argument("--stop-gain", type=float,
                    default=daub_refine.DEFAULT_STOP_GAIN,
                    help="stop when a round improves the region score by "
                         "less than this (default %(default)s)")
    ap.add_argument("--target", type=float, default=None,
                    help="stop once the region score reaches this")
    ap.add_argument("--gf-size", type=int, default=20,
                    help="groundfill stroke size (default %(default)s)")
    ap.add_argument("--ref-img", default=None,
                    help="override the reference image (default: the "
                         "plan header's reference)")
    args = ap.parse_args()

    if args.render_only:
        plan_path = os.path.abspath(args.ref)
        out_png = os.path.abspath(args.out_png)
        with open(plan_path, encoding="utf-8") as fh:
            d = json.load(fh)
        n = d.get("count", len(d["strokes"]))
        print("render-only: %s (%d strokes)" % (plan_path, n), flush=True)
        _render(plan_path, out_png, args.kra, psd=args.psd)
        if args.timelapse:
            _timelapse(plan_path, out_png, args.timelapse,
                       order=args.timelapse_order)
        print("daub_paint done -> %s%s%s" % (out_png,
              (" + " + args.kra) if args.kra else "",
              (" + " + args.psd) if args.psd else ""), flush=True)
        return 0

    if args.refine:
        if not PLANNER:
            raise SystemExit(daub_refine.NO_PLANNER)
        regions = ";".join(args.regions)
        layers = [x.strip() for x in (args.layers or "").split(",")
                  if x.strip()] or None
        out_png = os.path.abspath(args.out_png)
        daub_refine.run(args.ref, out_png, regions,
                        mode=args.mode, layers=layers,
                        rounds=args.rounds, max_passes=args.max_passes,
                        stop_gain=args.stop_gain, target=args.target,
                        groundfill_size=args.gf_size, ref_path=args.ref_img,
                        daub=DAUB, cal=CAL, tips=TIPS)
        print("daub_paint done -> %s" % out_png, flush=True)
        return 0

    se = _import_planner()

    ref = os.path.abspath(args.ref)
    if args.probe:
        _probe(se, ref)
        return 0
    if not os.path.isfile(ref):
        raise SystemExit("no such reference: %s" % ref)
    out_png = os.path.abspath(args.out_png)
    stem = os.path.splitext(out_png)[0]
    plan_path = stem + "_plan.json"

    print("plan: %s" % ref, flush=True)
    # plan() writes the JSON and returns 0 - read the count back from
    # the header (its "count" field is the truth source)
    se.plan(ref, plan_path, pen=args.pen)
    with open(plan_path, encoding="utf-8") as fh:
        n = json.load(fh)["count"]
    print("plan done: %d strokes -> %s" % (n, plan_path), flush=True)

    _render(plan_path, out_png, args.kra, psd=args.psd)
    if args.timelapse:
        _timelapse(plan_path, out_png, args.timelapse,
                   order=args.timelapse_order)
    print("daub_paint done -> %s%s%s" % (out_png,
          (" + " + args.kra) if args.kra else "",
          (" + " + args.psd) if args.psd else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
