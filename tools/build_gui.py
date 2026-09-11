"""Build the daub_gui front-end exe (onefile, windowed).

The GUI bundles NO planner code - planning lives in dist/daub_paint.exe
(build_paint.py first), launched as a subprocess. It DOES bundle the
raw daub.exe (cargo build --release) for the workbench's --layers-dir
exports and per-layer re-preset renders. Only PySide6 + the stdlib-only
render_timelapse helpers (imported from tools/, found automatically)
ride along on top, which keeps the GUI well under the engine's ~90MB.

Usage:
  pack_venv/Scripts/python.exe tools/build_gui.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "dist", "daub_paint.exe")
DAUB = os.path.join(ROOT, "target", "release", "daub.exe")

if not os.path.isfile(ENGINE):
    sys.exit("missing %s - the GUI launches it as its engine; run "
             "tools/build_paint.py first" % ENGINE)
if not os.path.isfile(DAUB):
    sys.exit("missing %s - the workbench renders layers with raw "
             "daub.exe; run `cargo build --release` first" % DAUB)
try:
    import PySide6  # noqa: F401
except ImportError:
    sys.exit("PySide6 missing from the build interpreter - "
             "pip install PySide6 (see tools/pack_requirements.txt)")
TIPS_DIR = os.path.join(HERE, "data", "brush_tips")
if not os.path.isdir(TIPS_DIR):
    sys.exit("missing %s - the tip engines are built FROM these .kpp "
             "files; without them every workbench layer render fails "
             "(a past build shipped without them: silent whole-render "
             "fallback, re-renders on every mute)"
             % TIPS_DIR)
ICON = os.path.join(HERE, "data", "app_icon.ico")
if not os.path.isfile(ICON):
    sys.exit("missing %s - the app icon, painted by daub itself via "
             "tools/_make_icon.py" % ICON)

# optional argv[1]: alternate distpath (staging builds while the live
# exe locks dist/daub_gui.exe)
DIST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile", "--clean", "--noconfirm", "--windowed",
    "--name", "daub_gui",
    "--distpath", DIST,
    "--workpath", os.path.join(ROOT, "build"),
    "--specpath", os.path.join(ROOT, "build"),
    # preset-list data source for the workbench combo (the engine
    # carries its own copy; this one is only read by the GUI)
    "--add-data", os.path.join(HERE, "data", "ink_calib.json") + ";data",
    # tip registry AND the .kpp files the engines bake from (the JSON
    # alone is useless: daub.exe fails "tip preset ... no tip engine"
    # without data/brush_tips next to it)
    "--add-data", os.path.join(HERE, "data", "brush_lib.json") + ";data",
    "--add-data", TIPS_DIR + ";data/brush_tips",
    # the daub-painted icon: exe shell icon AND the runtime window icon
    "--add-data", ICON + ";data",
    "--icon", ICON,
    "--add-binary", DAUB + ";.",
    os.path.join(HERE, "daub_gui.py"),
]
r = subprocess.run(cmd)
if r.returncode != 0:
    sys.exit(r.returncode)
out = os.path.join(DIST, "daub_gui.exe")
print("built %s (%.0fMB)" % (out, os.path.getsize(out) / 1e6), flush=True)
