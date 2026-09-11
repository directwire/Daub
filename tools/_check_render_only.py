"""_check_render_only.py - simulate a planner-free OSS clone and prove
the renderer-only contract.

The open-source repository ships the renderer; the planner
(stroke_engine) stays a private checkout. This check builds a throwaway
copy of the tree WITHOUT any planner sibling checkout and drives
daub_paint inside it:

  1. --render-only renders a synthetic plan (png + kra + psd land,
     nonzero bytes) - no planner anywhere near
  2. full plan mode fails loud with the NO_PLANNER guidance
  3. --refine fails loud with the same guidance
  4. (real tree) a bad DAUB_KRMCP_TOOLS env fails loud at import

Run:  pack_venv/Scripts/python.exe tools/_check_render_only.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# shipped pure-python chain of the renderer path (everything daub_paint
# touches when no planner exists)
CHAIN = ["daub_paint.py", "daub_refine.py", "render_timelapse.py",
         "daub_frame.py"]


def leg(n, ok, detail=""):
    print("  %s. %s%s" % (n, "PASS" if ok else "FAIL",
                          " - " + detail if detail else ""))
    if not ok:
        sys.exit(1)


def synthetic_plan(path):
    doc = {
        "canvas": [96, 96],
        "seed": 20260910,
        "strokes": [
            {"layer": "X1", "preset": "b) Basic-2 Opacity", "size": 6,
             "opacity": 0.9, "color": "#2244cc",
             "points": [[12, 12, 0.4], [48, 30, 0.8], [84, 12, 0.3]]},
            {"layer": "L5", "preset": "d) Ink-3 Gpen", "size": 3,
             "opacity": 0.8, "color": "#8c2d1d",
             "points": [[12, 80, 0.9], [84, 80, 0.5]]},
        ],
    }
    doc["count"] = len(doc["strokes"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def run(argv, env=None, cwd=None):
    return subprocess.run(
        [PY, "-u"] + argv, capture_output=True, encoding="utf-8",
        errors="replace", env=env, cwd=cwd or os.path.join(sim, "repo"))


tmp = tempfile.mkdtemp(prefix="daub_oss_sim_")
sim = os.path.join(tmp, "sim")
os.makedirs(os.path.join(sim, "repo", "tools", "data"))
os.makedirs(os.path.join(sim, "repo", "target", "release"))

# the OSS clone: python chain + vendored data + a built binary, and NO
# planner sibling anywhere under tmp
for f in CHAIN:
    shutil.copy(os.path.join(HERE, f), os.path.join(sim, "repo", "tools", f))
shutil.copytree(os.path.join(HERE, "data"),
                os.path.join(sim, "repo", "tools", "data"),
                dirs_exist_ok=True)
shutil.copy(os.path.join(ROOT, "target", "release",
                         "daub.exe" if os.name == "nt" else "daub"),
            os.path.join(sim, "repo", "target", "release",
                         "daub.exe" if os.name == "nt" else "daub"))

plan = os.path.join(tmp, "t.json")
synthetic_plan(plan)
repo = os.path.join(sim, "repo")

try:
    # 1. renderer-only works with nothing but the clone
    out = os.path.join(tmp, "r1.png")
    r = run(["tools/daub_paint.py", "--render-only", plan, out,
             "--kra", out + ".kra", "--psd", out + ".psd"], cwd=repo)
    leg(1, r.returncode == 0 and os.path.isfile(out)
        and os.path.getsize(out) > 500
        and os.path.getsize(out + ".kra") > 26
        and os.path.getsize(out + ".psd") > 26,
        (r.stdout + r.stderr).strip().splitlines()[-1:] and
        ((r.stdout + r.stderr).strip().splitlines() or [""])[-1][:100])

    # 2. plan mode fails loud with the guidance
    r = run(["tools/daub_paint.py", plan, os.path.join(tmp, "r2.png")],
            cwd=repo)
    blob = r.stdout + r.stderr
    leg(2, r.returncode != 0
        and "not part of this repository" in blob
        and "DAUB_KRMCP_TOOLS" in blob,
        "rc=%s" % r.returncode)

    # 3. refine fails loud the same way
    r = run(["tools/daub_paint.py", "--refine", plan,
             os.path.join(tmp, "r3.png"),
             "--regions", "0,0,40,40"], cwd=repo)
    blob = r.stdout + r.stderr
    leg(3, r.returncode != 0
        and "not part of this repository" in blob, "rc=%s" % r.returncode)

    # 4. bad env fails loud even in the real tree (strict by design:
    # pointing it at a wrong tree must never silently re-route)
    env = dict(os.environ, DAUB_KRMCP_TOOLS=tmp)   # exists, no engine
    r = run(["tools/daub_paint.py", "--render-only", plan,
             os.path.join(tmp, "r4.png")],
            env=env, cwd=ROOT)
    blob = r.stdout + r.stderr
    leg(4, r.returncode != 0 and "holds no stroke_engine.py" in blob,
        "rc=%s" % r.returncode)

    print("ALL PASS - %s" % tmp)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
