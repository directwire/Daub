"""Guard against drift between the vendored assets in tools/data and
the upstream originals they were copied from (the calibration tables
and .kpp brush tips are the upstream calibration checkout's to fit,
daub's to consume - so the copies must never silently diverge).

ink_calib.json / fork_ink_calib.json / every .kpp must match byte for
byte. brush_lib.json matches after re-applying the one sanctioned
transform (absolute kpp paths -> tip_root-relative, forward slashes).

Exit 0 = in sync; 1 = drift (re-vendor before shipping).

Usage:  python tools/check_assets.py [--src PATH_TO_UPSTREAM_TOOLS]
"""

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# the upstream originals: DAUB_KRMCP_TOOLS env, or --src (this dev
# drift-guard is meaningless without them - downstream users have
# nothing to compare against and never need to run it)
DEFAULT_SRC = os.environ.get("DAUB_KRMCP_TOOLS") or ""
DATA = os.path.join(HERE, "data")


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    args = ap.parse_args()
    if not os.path.isdir(args.src):
        sys.exit("upstream tools not found at %r - this drift-guard "
                 "compares the vendored assets against their upstream "
                 "originals; pass --src or set DAUB_KRMCP_TOOLS "
                 "(renderer users never need it)" % args.src)
    bad = []

    for f in ("ink_calib.json", "fork_ink_calib.json"):
        a, b = (os.path.join(p, f) for p in (DATA, args.src))
        if sha(a) != sha(b):
            bad.append(f)

    src_tips = os.path.join(args.src, "brush_tips")
    dst_tips = os.path.join(DATA, "brush_tips")
    src_files = {os.path.relpath(os.path.join(r, f), src_tips)
                 for r, _, fs in os.walk(src_tips) for f in fs}
    dst_files = {os.path.relpath(os.path.join(r, f), dst_tips)
                 for r, _, fs in os.walk(dst_tips) for f in fs}
    for rel in sorted(src_files - dst_files):
        bad.append("brush_tips missing copy: %s" % rel)
    for rel in sorted(dst_files - src_files):
        bad.append("brush_tips vendored-only (deleted upstream?): %s" % rel)
    for rel in sorted(src_files & dst_files):
        if sha(os.path.join(src_tips, rel)) != sha(os.path.join(dst_tips, rel)):
            bad.append("brush_tips differs: %s" % rel)

    lib = json.load(open(os.path.join(DATA, "brush_lib.json")))
    ref = json.load(open(os.path.join(args.src, "brush_lib.json")))
    prefix = os.path.join(args.src, "brush_tips") + os.sep
    for name, e in ref["presets"].items():
        kpp = e.get("kpp", "").replace("/", os.sep)
        if kpp.startswith(prefix):
            e["kpp"] = os.path.relpath(
                kpp, os.path.join(args.src, "brush_tips")).replace(os.sep, "/")
    if lib != ref:
        bad.append("brush_lib.json (beyond the sanctioned path rewrite)")

    if bad:
        print("ASSET DRIFT (%d):" % len(bad))
        for b in bad:
            print("  " + b)
        return 1
    print("assets in sync: 2 calib + %d kpp + brush_lib (%d presets)"
          % (len(src_files), len(ref["presets"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
