#!/usr/bin/env python3
"""Generate web-showcase assets for daub_web (hero video + gallery stills).

Usage:
    python make_showcase.py [SRC_DIR] [--force]

SRC_DIR is the case folder holding the timelapse/still sources. Writes into
tools/web_showcase/ next to this script:

    hero_1080.mp4    16s timelapse re-encoded to 1080p h264 (hero bg loop)
    hero_poster.jpg  final frame of the same timelapse (video poster)
    video_full.mp4   byte-exact copy of the source timelapse (gitignored)
    case_*.jpg       gallery stills, max side 2560, JPEG q90
    case_*.mp4       per-case timelapse re-encoded to 1080p h264
    manifest.json    consumed by GET /api/showcase

Every web asset is derived from the largest originals available - the
full-resolution masters never get downscaled below 2560 on the long side.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web_showcase")
DEFAULT_SRC = os.environ.get("DAUB_SHOWCASE_SRC", "")
if not DEFAULT_SRC:
    sys.exit("set DAUB_SHOWCASE_SRC to the folder holding your source paintings/timelapses")

HERO_SRC = "微信图片_20260909135741_1260_1_edit_timelapse.mp4"

CASES = [
    # (tag, card image, timelapse video, title, tag-label)
    # image must match the video's final frame (visual truth checked)
    ("case_1279", "微信图片_20260909224706_1279_1_edit.png",
     "微信图片_20260909224706_1279_1_edit_timelapse.mp4",
     "红妆 · 落花", "古风人像"),
    ("case_1256", "微信图片_20260909131426_1256_1.png",
     "微信图片_20260909131426_1256_1_edit_timelapse.mp4",
     "红茶会 · 粉发少女", "人像插画"),
    ("case_1244", "微信图片_20260909010322_1244_1.png",
     "微信图片_20260909010322_1244_1_edit_timelapse.mp4",
     "白无垢", "和风人像"),
    ("case_064", "微信图片_20260511010958_64_1-1_edit.png",
     "微信图片_20260511010958_64_1-1_edit_timelapse.mp4",
     "白裙 · 水彩", "淡彩"),
    ("case_1188", "微信图片_20260905210634_1188_1-4.png",
     "微信图片_20260905210634_1188_1-4_edit_timelapse.mp4",
     "蛇与少女", "哥特线绘"),
]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit("cmd failed: %s\n%s" % (" ".join(cmd), r.stderr[-800:]))


def make_hero(src, force):
    hero_mp4 = os.path.join(OUT, "hero_1080.mp4")
    poster = os.path.join(OUT, "hero_poster.jpg")
    if not force and os.path.isfile(hero_mp4) and os.path.isfile(poster):
        return
    run(["ffmpeg", "-y", "-v", "error", "-i", src,
         "-vf", "scale=-2:1080", "-c:v", "libx264", "-crf", "22",
         "-preset", "medium", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", hero_mp4])
    run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.8", "-i", src,
         "-frames:v", "1", "-vf", "scale=-2:1080", "-q:v", "3", poster])


def ffprobe_json(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return json.loads(r.stdout or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=DEFAULT_SRC)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    hero_src = os.path.join(a.src, HERO_SRC)
    if not os.path.isfile(hero_src):
        sys.exit("hero source missing: %s" % hero_src)

    make_hero(hero_src, a.force)

    full = os.path.join(OUT, "video_full.mp4")
    if a.force or not os.path.isfile(full):
        shutil.copy2(hero_src, full)

    from PIL import Image
    cases = []
    for tag, img_name, vid_name, title, label in CASES:
        p = os.path.join(a.src, img_name)
        v = os.path.join(a.src, vid_name)
        if not os.path.isfile(p) or not os.path.isfile(v):
            print("skip (missing): %s" % img_name)
            continue
        jpg, mp4 = tag + ".jpg", tag + ".mp4"
        jp, vp = os.path.join(OUT, jpg), os.path.join(OUT, mp4)
        if a.force or not (os.path.isfile(jp) and os.path.isfile(vp)):
            im = Image.open(p).convert("RGB")
            w, h = im.size
            if max(w, h) > 2560:
                sc = 2560.0 / max(w, h)
                im = im.resize((round(w * sc), round(h * sc)), Image.LANCZOS)
            im.save(jp, quality=90, optimize=True)
            run(["ffmpeg", "-y", "-v", "error", "-i", v,
                 "-vf", "scale=-2:1080", "-c:v", "libx264", "-crf", "22",
                 "-preset", "medium", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-an", vp])
            w, h = im.size                   # card dims, not source dims
        else:
            w, h = Image.open(jp).size       # cached assets stay untouched
        info = ffprobe_json(v).get("format", {})
        cases.append({"file": jpg, "video": mp4,
                      "w": w, "h": h,
                      "dur": round(float(info.get("duration", 0)), 1),
                      "title": title, "tag": label,
                      "src_mb": round(os.path.getsize(p) / 1e6, 1)})

    st = next((s for s in ffprobe_json(hero_src).get("streams", [])
               if s.get("codec_type") == "video"), {})
    info = ffprobe_json(hero_src).get("format", {})
    manifest = {
        "hero": {
            "video": "hero_1080.mp4",
            "poster": "hero_poster.jpg",
            "full": "video_full.mp4",
            "w": int(st.get("width", 0)),
            "h": int(st.get("height", 0)),
            "dur": round(float(info.get("duration", 0)), 1),
            "title": "笔画回放 · 红茶会",
        },
        "cases": cases,
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print("showcase ready: %d cases, hero %sx%s %ss" % (
        len(cases), manifest["hero"]["w"], manifest["hero"]["h"],
        manifest["hero"]["dur"]))
    for n in sorted(os.listdir(OUT)):
        print("  %-18s %8.1f KB" % (n, os.path.getsize(
            os.path.join(OUT, n)) / 1024.0))


if __name__ == "__main__":
    main()
