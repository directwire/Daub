"""Build the daub_paint production exe (onefile, Krita-free).

Consumes the vendored assets in tools/data (run check_assets.py first -
the build ships whatever is in there). Output: dist/daub_paint.exe
(~90MB: numpy+PIL runtime + 15MB brush tips + daub.exe); onefile
startup pays a few seconds of unpack, the chain itself is the usual
plan ~80s + render ~2s.

Usage:
  pack_venv/Scripts/python.exe tools/build_paint.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "target", "release", "daub.exe")
# stroke_engine/krmcp_port live in the upstream planner checkout (the
# planner is separate code, NOT part of this repository); --paths lets
# PyInstaller's analysis follow the real import chain (so numpy/PIL get
# bundled), stroke_trace is loaded by stroke_engine via importlib from
# a file path - invisible to the analysis - so it rides as data at the
# bundle root instead. Resolution: DAUB_KRMCP_TOOLS env (required).
KRMCP = os.environ.get("DAUB_KRMCP_TOOLS") or ""
if not KRMCP or not os.path.isfile(os.path.join(KRMCP, "stroke_engine.py")):
    sys.exit("planner not found under %r - build_paint bundles the "
             "closed stroke_engine; set DAUB_KRMCP_TOOLS to the "
             "planner's tools directory (renderer-only users don't "
             "need this script at all)" % KRMCP)

if not os.path.isfile(SRC):
    sys.exit("missing %s - cargo build --release first" % SRC)

# stroke_trace prefers skimage.morphology.skeletonize and only falls
# back to its hand-rolled Zhang-Suen on ImportError - the two produce
# DIFFERENT skeletons, so a bundle built without scikit-image plans
# differently from the dev chain (measured: 87693 vs 87624 strokes on
# img1200, anchors 6.606 vs 6.827). Build-time gate: no skimage, no exe.
try:
    import skimage  # noqa: F401
except ImportError:
    sys.exit("scikit-image missing from the build interpreter - "
             "stroke_trace would silently take its Zhang-Suen fallback "
             "and plan output would diverge from the dev chain. "
             "pip install scikit-image (see tools/pack_requirements.txt)")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile", "--clean", "--noconfirm",
    "--name", "daub_paint",
    "--distpath", os.path.join(ROOT, "dist"),
    "--workpath", os.path.join(ROOT, "build"),
    "--specpath", os.path.join(ROOT, "build"),
    # stroke_trace is loaded by stroke_engine via importlib from a file
    # path - PyInstaller cannot see it, so it rides as data at the
    # bundle root (dirname(__file__) == _MEIPASS when frozen)
    "--paths", KRMCP,
    # daub_paint lazy-imports tools/render_timelapse.py (--timelapse);
    # without our own dir on the analysis path the frozen exe would
    # crash on that import.
    "--paths", HERE,
    "--add-data", os.path.join(KRMCP, "stroke_trace.py") + ";.",
    "--add-data", os.path.join(HERE, "data", "ink_calib.json") + ";.",
    "--add-data", os.path.join(HERE, "data", "fork_ink_calib.json") + ";.",
    "--add-data", os.path.join(HERE, "data", "brush_lib.json") + ";data",
    "--add-data", os.path.join(HERE, "data", "brush_tips") + ";data/brush_tips",
    "--add-binary", SRC + ";.",
    os.path.join(HERE, "daub_paint.py"),
]
r = subprocess.run(cmd)
if r.returncode != 0:
    sys.exit(r.returncode)
out = os.path.join(ROOT, "dist", "daub_paint.exe")
print("built %s (%.0fMB)" % (out, os.path.getsize(out) / 1e6), flush=True)
