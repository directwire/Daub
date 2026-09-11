"""_check_psd: external PSD validator (psd-tools reads what daub wrote).

The Rust unit tests pin the BYTES; this tool pins the SEMANTICS with an
independent reader - the two failure modes are disjoint, so green on
both is real evidence.

  pack_venv/Scripts/python.exe tools/_check_psd.py OUT.psd \
      --layers-dir OUT_layers --truth OUT.png [--psd2 OUT2.psd]

Evidence (each prints PASS/FAIL; any FAIL exits non-zero):
  1. psd-tools opens it: size == manifest, depth 8, merged 3ch RGB
  2. layer count == manifest layers + 1 (the paper coat)
  3. names + order bottom-first == ["paper"] + manifest (index 0 = bottom)
  4. every layer: opacity 255 / blend NORMAL / visible
  5. per-layer pixels: premultiplied TWICE they equal the layers-dir
     BGRA bytes, max diff <= 2, a==0 skipped. (The layers-dir carries
     the house format: write_layers_dir premultiplies the already-
     premultiplied LayerBuf, and Qt SourceOver on that reproduces the
     deliverable PNG's rendering equation bit for bit - verified 09-09.
     The PSD stores straight alpha, hence the double premul to compare.)
  6. recomposite from the PSD's straight pixels over the paper ground
     using the house equation (src premultiplied once, then scaled by
     af again - the same one render.rs composite uses) vs the truth
     PNG: mean <= 1.0, max <= 2. A plain straight-alpha composite
     (psd.composite) is DIFFERENT by design - lighter soft edges.
  7. determinism: --psd2 (a second render) hashes byte-identical
  8. parse stability: two composite() passes on the same file agree

layers-dir format (render.rs write_layers_dir, bottom-first):
  layers.json {width, height, paper:[r,g,b], layers:[{name, file}...]}
  layerNN.bgra  w*h*4 bytes, premultiplied (B,G,R,A) little-endian.
"""

import argparse
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import BlendMode

FAILED = []


def check(idx, ok, text, detail=""):
    tag = "PASS" if ok else "FAIL"
    print("[%d] %s %s%s" % (idx, tag, text,
                            (" - " + detail) if detail and not ok else ""))
    if not ok:
        FAILED.append(idx)
    return ok


def premul_once(rgba, twice=False):
    """Straight RGBA uint8 (h, w, 4) -> premultiplied BGRA bytes, the
    write_layers_dir formula applied once; twice=True applies it again
    (the layers-dir format: the input LayerBuf is already premul)."""
    px = rgba.reshape(-1, 4).astype(np.uint32)
    a = px[:, 3]
    out = np.zeros_like(px)
    nz = a > 0
    az = a[nz]
    for dst, src in ((0, 2), (1, 1), (2, 0)):  # R,G,B -> B,G,R file
        out[nz, dst] = (px[nz, src] * az + 127) // 255
    out[:, 3] = a
    if twice:
        a2 = out[:, 3]
        nz2 = a2 > 0
        a2z = a2[nz2]
        for c in range(3):
            out[nz2, c] = (out[nz2, c] * a2z + 127) // 255
    return out.astype(np.uint8)


def house_composite(layers_rgba, paper, w, h):
    """render.rs composite's equation, fed from straight PSD pixels:
    dst*(1-af) + premul_once(src)*af, layers bottom-first over paper."""
    out = np.zeros((h * w, 3), dtype=np.float64)
    out[:, :] = np.array(paper, dtype=np.float64)
    for rgba in layers_rgba:
        px = rgba.reshape(-1, 4).astype(np.float64)
        a = px[:, 3]
        af = a / 255.0
        pm = np.zeros((a.size, 3), dtype=np.float64)
        nz = a > 0
        pm[nz] = ((px[nz, :3] * a[nz, None] + 127) // 255)
        for c in range(3):
            out[:, c] = out[:, c] * (1.0 - af) + pm[:, c] * af
    return np.rint(out).astype(np.uint8).reshape(h, w, 3)


def main():
    ap = argparse.ArgumentParser(prog="_check_psd")
    ap.add_argument("psd")
    ap.add_argument("--layers-dir", default=None)
    ap.add_argument("--truth", default=None)
    ap.add_argument("--psd2", default=None,
                    help="a second render of the same plan (evidence 7)")
    args = ap.parse_args()

    man = None
    if args.layers_dir:
        with open(os.path.join(args.layers_dir, "layers.json"),
                  encoding="utf-8") as fh:
            man = json.load(fh)

    psd = PSDImage.open(args.psd)
    print("psd-tools: %s (%dx%d, kind=%s)" %
          (args.psd, psd.width, psd.height, psd.kind))

    # 1) header semantics through the independent reader
    ok = True
    detail = ""
    if man is not None:
        ok = (psd.width == man["width"] and psd.height == man["height"])
        detail = "want %dx%d" % (man["width"], man["height"])
    ok &= psd.depth == 8 and psd.channels == 3 and psd.color_mode \
        == psd.color_mode.RGB
    check(1, ok, "opens: size/depth8/merged-3ch/RGB", detail)

    layers = list(psd)

    # 2) layer count
    if man is not None:
        want = len(man["layers"]) + 1  # + paper coat
        check(2, len(layers) == want,
              "layer count %d == manifest %d + 1" % (len(layers), want))
    else:
        print("[2] SKIP layer count (no --layers-dir)")

    # 3) names, bottom-first
    names = [L.name for L in layers]
    if man is not None:
        want_names = ["paper"] + [L["name"] for L in man["layers"]]
        check(3, names == want_names,
              "names bottom-first", "got %s want %s" % (names, want_names))
    else:
        check(3, names[0] == "paper", "paper coat at the bottom", str(names))

    # 4) opacity / blend / visible on every layer
    bad = ["%s(op=%s,blend=%s,vis=%s)" % (L.name, L.opacity, L.blend_mode,
                                          L.visible)
           for L in layers
           if L.opacity != 255 or L.blend_mode != BlendMode.NORMAL
           or not L.visible]
    check(4, not bad, "opacity 255 / NORMAL / visible on all %d layers"
          % len(layers), "; ".join(bad))

    # 5) per-layer pixels: PSD straight premul'd twice == layers-dir
    if man is not None and len(layers) == len(man["layers"]) + 1:
        w, h = man["width"], man["height"]
        worst = 0
        ok5 = True
        for L, entry in zip(layers[1:], man["layers"]):  # skip the coat
            bgra = np.fromfile(os.path.join(args.layers_dir, entry["file"]),
                               dtype=np.uint8)
            if bgra.size != w * h * 4:
                ok5 = False
                worst = 999
                print("     %s: bad size %d" % (entry["file"], bgra.size))
                break
            img = L.topil(apply_icc=False)
            got = np.asarray(img.convert("RGBA")) if img else None
            if got is None or got.shape != (h, w, 4):
                ok5 = False
                worst = 999
                print("     %s: layer pixels %s" %
                      (entry["name"],
                       None if got is None else (got.shape,)))
                break
            want = premul_once(got, twice=True).reshape(h, w, 4)
            known = want[:, :, 3] > 0
            d = np.abs(want[known].astype(int)
                       - bgra.reshape(h, w, 4)[known].astype(int)).max()
            worst = max(worst, int(d))
            ok5 &= d <= 2
        check(5, ok5, "per-layer pixels vs layers-dir (double-premul, <=2)",
              "worst=%d" % worst)
    else:
        print("[5] SKIP per-layer pixels")

    # 6) house-equation recomposite from PSD pixels vs truth render
    if args.truth and man is not None:
        got = house_composite([np.asarray(L.topil(apply_icc=False)
                                          .convert("RGBA"))
                               for L in layers[1:]],
                              man["paper"], man["width"], man["height"])
        want = np.asarray(Image.open(args.truth).convert("RGB"))
        if got.shape != want.shape:
            check(6, False, "recomposite vs truth",
                  "shape %s vs %s" % (got.shape, want.shape))
        else:
            diff = np.abs(got.astype(int) - want.astype(int))
            mean, mx = float(diff.mean()), int(diff.max())
            check(6, mean <= 1.0 and mx <= 2,
                  "house recomposite vs truth (mean<=1.0, max<=2)",
                  "mean=%.4f max=%d" % (mean, mx))
    else:
        print("[6] SKIP composite vs truth (need --truth + --layers-dir)")

    # 7) determinism across two independent renders
    if args.psd2:
        h1 = hashlib.sha256(open(args.psd, "rb").read()).hexdigest()
        h2 = hashlib.sha256(open(args.psd2, "rb").read()).hexdigest()
        check(7, h1 == h2, "double render sha256 identical", "%s vs %s"
              % (h1[:16], h2[:16]))
    else:
        print("[7] SKIP double render (no --psd2)")

    # 8) parse stability: same file, two composite passes
    b1 = psd.composite(force=True).tobytes()
    b2 = PSDImage.open(args.psd).composite(force=True).tobytes()
    check(8, hashlib.sha256(b1).hexdigest()
          == hashlib.sha256(b2).hexdigest(),
          "two parses of the composite agree")

    print("---- _check_psd %s (%d checks failed) ----"
          % ("FAIL" if FAILED else "PASS", len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
