"""画坊 web studio - web front for the daub engine.

Local first: port the GUI's features to the web one by one, server
deployment last. One file, stdlib + PIL/numpy, zero new deps:

    python tools/daub_web.py [--port 8787] [--open]

Binds 127.0.0.1 - a public deployment sits behind a reverse proxy,
and the auth gate below is mandatory there.

Reuse map (mirrors daub_gui.py, same contracts):
  engine      dist/daub_paint.exe (env DAUB_PAINT_EXE > dist exe > dev 脚本)
  raw render  target/release/daub.exe --layers-dir (env DAUB_EXE)
  progress    engine stdout line grammar (RX_* below, same as GUI)
  live growth planner writes <stem>_plan_partial.json; poller copies it
              and ships the copy to --render-only (GUI LiveGrowth, sans Qt)
  layers      layers.json + headerless premultiplied BGRA -> straight-alpha
              PNG (numpy un-premultiply), served for the client workbench

Job layout (web_jobs/<id>/):
  <id>.png            final painting            <id>_plan.json   plan (truth)
  <id>_plan_partial.json  live-growth source    <id>_work/       engine scratch
  <id>.kra/.psd/_timelapse.mp4  optional artifacts
  meta.json           gallery record            live.png         newest live frame
  layers/             web layer PNGs + layers_web.json
"""

import argparse
import base64
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def _qtext(s):
    """Recover real unicode from a query value.

    BaseHTTPRequestHandler decodes the request line as latin-1. Browser
    clients percent-encode UTF-8, which parse_qs+unquote already handle;
    bare non-ASCII bytes (curl from a GBK console) arrive as latin-1
    chars — re-encode and try utf-8/gbk before giving up.
    """
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s  # already real unicode (percent-encoded path)
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return s

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

INK_CALIB = os.path.join(HERE, "data", "ink_calib.json")
TIPS_JSON = os.path.join(HERE, "data", "brush_lib.json")
UI_HTML = os.path.join(HERE, "daub_web.html")
ICON = os.path.join(HERE, "data", "app_icon.ico")
SHOWCASE_DIR = os.path.join(HERE, "web_showcase")   # make_showcase.py output
WASM_DIR = os.path.join(ROOT, "web", "wasm")        # truth-panel wasm assets

try:
    from PIL import Image
    import numpy as np
except ImportError:
    sys.exit("daub_web needs PIL + numpy on the server python "
             "(tested: Python313 has PIL 12.2 / numpy 2.4)")

from preset_names import zh  # brush display names (same table as GUI)

JOBS_DIR = os.path.join(ROOT, "web_jobs")

# jid 白名单：服务端生成的是 hex（时间戳+序号），URL 里来的必须过这个
# 门——os.path.join(JOBS_DIR, jid, ...) 拼路径前先钉死，`..`/`.`/斜杠
# 注入全数拒绝（job_from_disk 返回 None → 路由 404，不泄漏存在性）。
_JID_RX = re.compile(r"^[0-9a-f]{1,16}$")

# 上传参考图硬护栏（在 PIL 真解码【前】用 header 断，防图像炸弹把 2G
# 机器的 worker OOM 杀掉：64MB 压缩体可解出上亿像素）。8192px 是 tier
# full 的体面上限，正常用户 512-2048 感知不到；格式只收 web 三件套。
MAX_REF_DIM = 8192
REF_FORMATS = ("PNG", "JPEG", "WEBP")

# ------------------------------------------------------------ auth ----
# 公网部署的鉴权门（红线：算力暴露公网前必先加鉴权）。DAUB_AUTH 形如
# "admin:pw1,alice:pw2"（逗号分隔多账号，冒号后允许含冒号）时全站要求
# HTTP Basic Auth；不设 = 本地工作室原行为，一字不变。恒定时间比较防
# 时序侧信道；401 带 WWW-Authenticate 触发浏览器原生弹窗，fetch 同源
# 默认带凭据，前端 JS 零改动。
_AUTH_USERS = None


def _load_auth():
    global _AUTH_USERS
    if _AUTH_USERS is None:
        raw = (os.environ.get("DAUB_AUTH") or "").strip()
        _AUTH_USERS = {}
        for pair in raw.split(","):
            if ":" in pair:
                u, _, p = pair.partition(":")
                if u:
                    _AUTH_USERS[u] = p
    return _AUTH_USERS


def _auth_ok(header_value):
    users = _load_auth()
    if not users:
        return True
    if not header_value.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header_value[6:].strip(),
                               validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, pw = raw.partition(":")
    expect = users.get(user)
    return bool(expect) and hmac.compare_digest(expect, pw)

# engine stdout grammar (daub_gui.py:411-422 - keep in sync)
RX_PLAN0 = re.compile(r"^plan: (.+)$")
RX_BAND = re.compile(r"^band\s+(\d+)-\s*(\d+)\s+pass (\d+): (\d+) strokes")
RX_BAND_T = re.compile(r"^band\s+(\d+)-\s*(\d+)\s+total: (\d+) strokes")
RX_PLAN_DONE = re.compile(r"^plan done: (\d+) strokes")
RX_DAUB = re.compile(r"^daub: (\d+) strokes.*total ([0-9.]+)s")
RX_LAYER = re.compile(r"^layer (\S+) done at (\d+) strokes")
RX_VIDEO = re.compile(r"^video (\d+)% \((\d+)/(\d+) frames\)")
RX_DONE = re.compile(r"^daub_paint done -> (.+)$")

TIER_MAXDIM = {"fast": 1400, "std": 2400, "full": 0}
MAX_UPLOAD = 48 * 1024 * 1024
LAYER_PNG_CAP = 2048          # workbench serves layers capped to this edge

_MCP_TOOLS = None


def mcp_tools():
    """daub_mcp.TOOLS, imported lazily and cached - the web studio's MCP
    panel reads the registry straight from the server module, so the page
    tracks the engine automatically when tools come and go. Import is
    side-effect-free (server only starts under __main__)."""
    global _MCP_TOOLS
    if _MCP_TOOLS is None:
        import daub_mcp
        _MCP_TOOLS = [{"name": t["name"],
                       "description": t.get("description", ""),
                       "required": t.get("inputSchema",
                                         {}).get("required", [])}
                      for t in daub_mcp.TOOLS]
    return _MCP_TOOLS


def resolve_engine():
    """[argv...] for the full pipeline engine: env > dist exe > dev
    script. dist exe 是 Windows 本地 studio 的出货真身；dev 脚本兜底
    （repo 自带的 python 解释器跑 tools/daub_paint.py）是给 Linux 算力
    机的——那边没有打包产物，venv + dev 布局就是它的出货形态。"""
    env = os.environ.get("DAUB_PAINT_EXE")
    if env and os.path.isfile(env):
        return [env]
    exe = os.path.join(ROOT, "dist", "daub_paint.exe")
    if os.path.isfile(exe):
        return [exe]
    dev = os.path.join(ROOT, "tools", "daub_paint.py")
    if os.path.isfile(dev):
        return [sys.executable, dev]
    return None


def resolve_daub():
    """[argv...] for the RAW renderer (layers pass): env > dev build."""
    env = os.environ.get("DAUB_EXE")
    if env and os.path.isfile(env):
        return [env]
    # 二进制名随 os.name（daub_paint 688ace6 同款适配）：Windows 取
    # daub.exe，Linux 算力机取 daub，别再让服务器找不到自家的引擎。
    exe = os.path.join(ROOT, "target", "release",
                       "daub.exe" if os.name == "nt" else "daub")
    if os.path.isfile(exe):
        return [exe]
    return None


def resolve_ffmpeg():
    """ffmpeg.exe or None (winget Gyan lands on PATH; GUI lesson: also
    probe the winget Links dir because a frozen parent can inherit a
    stale PATH - a plain python server usually inherits a fresh one)."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    links = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "WinGet", "Links", "ffmpeg.exe")
    return links if os.path.isfile(links) else None


def load_presets():
    """Calibrated brush pool -> [{raw, zh}] (display order: zh sort)."""
    try:
        with open(INK_CALIB, encoding="utf-8") as fh:
            keys = sorted(json.load(fh)["presets"].keys())
    except Exception:
        keys = []
    return [{"raw": k, "zh": zh(k)} for k in keys]


class Job:
    """One painting job: filesystem layout + runtime state."""

    def __init__(self, jid, name, tier, pen, want):
        self.id = jid
        self.dir = os.path.join(JOBS_DIR, jid)
        self.stem = jid                       # fs stem == job id (URL-safe)
        self.out_png = os.path.join(self.dir, jid + ".png")
        self.plan_path = os.path.join(self.dir, jid + "_plan.json")
        self.partial_path = os.path.join(self.dir, jid + "_plan_partial.json")
        self.workdir = os.path.join(self.dir, jid + "_work")
        self.meta = {
            "id": jid, "name": name, "ts": time.time(), "tier": tier,
            "pen": pen, "want": want,        # {"kra":bool,"psd":bool,"mp4":bool,"layers":bool}
            "state": "queued",               # queued|planning|rendering|done|fail|cancelled
            "strokes": 0, "live_count": 0, "live_seq": 0,
            "phase": "", "canvas": None, "error": "",
            "artifacts": {},                 # key -> absolute path
        }
        self.proc = None
        self.cancel = threading.Event()
        self.tail = []                       # recent engine lines (guarded)
        self.lock = threading.Lock()
        self.live_lock = threading.Lock()
        self._live_seen = 0
        self._live_seq = 0

    # -- meta persistence (gallery reads meta.json from disk) ----------
    def save_meta(self):
        with self.lock:
            snap = dict(self.meta)
        try:
            with open(os.path.join(self.dir, "meta.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def set_state(self, state, **fields):
        with self.lock:
            self.meta["state"] = state
            self.meta.update(fields)
        self.save_meta()

    def push_tail(self, line):
        with self.lock:
            self.tail.append(line)
            if len(self.tail) > 8:
                del self.tail[0]
            self.meta["tail"] = list(self.tail)

    def snapshot(self):
        with self.lock:
            d = dict(self.meta)
        d["artifacts"] = {k: os.path.basename(v)
                          for k, v in d.get("artifacts", {}).items()}
        return d


# ---------------------------------------------------------------- jobs --

_JOBS = {}
_JOBS_LK = threading.Lock()
_QUEUE = queue.Queue()
_SEQ = [0]


def new_job(name, tier, pen, want):
    with _JOBS_LK:
        _SEQ[0] += 1
        jid = "%x%02x" % (int(time.time()), _SEQ[0] & 0xff)
        while jid in _JOBS or os.path.isdir(os.path.join(JOBS_DIR, jid)):
            _SEQ[0] += 1
            jid = "%x%02x" % (int(time.time()), _SEQ[0] & 0xff)
        j = Job(jid, name, tier, pen, want)
        _JOBS[jid] = j
    os.makedirs(j.dir, exist_ok=True)
    j.save_meta()
    return j


def get_job(jid):
    with _JOBS_LK:
        return _JOBS.get(jid)


def job_from_disk(jid):
    """Rehydrate a past job for gallery viewing (runtime state rebuilt
    as a shell - artifacts/artwork load from disk, engine won't rerun)."""
    if not _JID_RX.match(jid or ""):
        return None
    meta_p = os.path.join(JOBS_DIR, jid, "meta.json")
    try:
        with open(meta_p, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    j = Job(jid, meta.get("name", jid), meta.get("tier", "std"),
            meta.get("pen", ""), meta.get("want", {}))
    with j.lock:
        j.meta.update(meta)
        j.tail = list(meta.get("tail", []))
    return j


def _engine_line(j, line):
    """Consume one engine stdout line: progress + tail (GUI grammar)."""
    line = line.strip()
    if not line:
        return
    j.push_tail(line)
    fields = {}
    m = RX_PLAN0.match(line)
    if m:
        txt = m.group(1)
        if "\\" in txt or "/" in txt:
            txt = txt.replace("\\", "/").rsplit("/", 1)[-1]
        fields["phase"] = "规划中 · " + txt[:48]
    m = RX_BAND.match(line)
    if m:
        fields["phase"] = "带宽 %s-%spx 第%s遍 · %s 笔" % (
            m.group(1), m.group(2), m.group(3), m.group(4))
    m = RX_BAND_T.match(line)
    if m:
        fields["strokes"] = int(m.group(3))
    m = RX_PLAN_DONE.match(line)
    if m:
        fields.update(state="rendering", strokes=int(m.group(1)),
                      phase="渲染中 · %s 笔" % m.group(1))
    m = RX_DAUB.match(line)
    if m:
        fields.update(strokes=int(m.group(1)))
    m = RX_LAYER.match(line)
    if m:
        fields["phase"] = "图层 %s 完成于 %s 笔" % (m.group(1), m.group(2))
    m = RX_VIDEO.match(line)
    if m:
        fields["phase"] = "视频 %s%%（%s/%s 帧）" % (
            m.group(1), m.group(2), m.group(3))
    m = RX_DONE.match(line)
    if m:
        fields["phase"] = "完成"
    if fields:
        with j.lock:
            j.meta.update(fields)


def _run_engine(j, argv, tag):
    """Spawn one engine subprocess, pump stdout through _engine_line,
    return (rc, tail). Separate reader keeps the pipe from filling."""
    eng_env = dict(os.environ)
    if resolve_ffmpeg():
        eng_env["DAUB_FFMPEG"] = resolve_ffmpeg()
    try:
        p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env=eng_env)
    except OSError as e:
        j.push_tail("%s spawn failed: %s" % (tag, e))
        return 127, list(j.tail)
    with j.lock:
        j.proc = p
    for line in p.stdout:
        _engine_line(j, line)
    rc = p.wait()
    with j.lock:
        j.proc = None
    return rc, list(j.tail)


# ---------------------------------------------------------- live growth --

def _live_thread(j):
    """GUI LiveGrowth without Qt: poll <stem>_plan_partial.json, ship a
    private copy to --render-only serially. Throttled - each frame costs
    one frozen-exe spawn (~7s), so render on >=3% growth or final."""
    eng = resolve_engine()
    png = [os.path.join(j.workdir, "_live_a.png"),
           os.path.join(j.workdir, "_live_b.png")]
    while not j.cancel.is_set():
        st = j.snapshot()
        if st["state"] not in ("planning",):
            return
        try:
            with open(j.partial_path, encoding="utf-8") as fh:
                d = json.load(fh)
            n = int(d["count"])
        except (OSError, ValueError, KeyError):
            time.sleep(1.0)
            continue
        if n <= 0 or n <= j._live_seen:
            time.sleep(1.0)
            continue
        grew = n - j._live_seen
        if j._live_seen and grew < max(150, 0.03 * n):
            time.sleep(1.0)
            continue
        os.makedirs(j.workdir, exist_ok=True)
        dst = os.path.join(j.workdir, "_live.json")
        try:
            shutil.copyfile(j.partial_path, dst)
        except OSError:
            time.sleep(1.0)
            continue
        j._live_seen = n
        j._live_seq += 1
        out = png[j._live_seq % 2]
        rc, _ = _run_engine(j, eng + ["--render-only", dst, out], "live")
        if j.cancel.is_set():
            return
        if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            with j.live_lock:
                try:
                    os.replace(out, os.path.join(j.dir, "live.png"))
                except OSError:
                    pass
                j.meta["live_count"] = n
                j.meta["live_seq"] = j._live_seq
        time.sleep(0.5)


# ------------------------------------------------------------- worker --

def _layers_pass(j, plan_path=None, raw_dir="layers_raw",
                 out_dir="layers", mf_name="layers_web.json",
                 prefix="", png_out="_layers.png"):
    """Raw daub.exe --layers-dir + BGRA -> straight-alpha web PNGs.
    plan_path/raw_dir/out_dir/mf_name/prefix parameterize the EDIT pass:
    the per-layer size ruler renders a sibling <stem>_edit_plan.json into
    its own dirs with e_-prefixed PNGs, leaving the original set
    untouched."""
    daub = resolve_daub()
    if not daub:
        return False, "raw daub.exe not found (target/release/daub.exe)"
    plan_path = plan_path or j.plan_path
    ldir = os.path.join(j.dir, raw_dir)
    os.makedirs(j.workdir, exist_ok=True)
    png = os.path.join(j.workdir, png_out)
    rc, tail = _run_engine(j, daub + ["render", plan_path, "--out", png,
                                      "--cal", INK_CALIB, "--tips", TIPS_JSON,
                                      "--layers-dir", ldir], "layers")
    if rc != 0:
        return False, "layers render rc=%s: %s" % (rc, tail[-1:] or "")
    mf_p = os.path.join(ldir, "layers.json")
    try:
        with open(mf_p, encoding="utf-8") as fh:
            mf = json.load(fh)
    except (OSError, ValueError):
        return False, "layers.json unreadable"
    w, h = int(mf["width"]), int(mf["height"])
    scale = min(1.0, LAYER_PNG_CAP / float(max(w, h)))
    out_layers = []
    web_dir = os.path.join(j.dir, out_dir)
    os.makedirs(web_dir, exist_ok=True)
    for ent in mf["layers"]:
        raw_p = os.path.join(ldir, ent["file"])
        try:
            raw = np.fromfile(raw_p, dtype=np.uint8)
            if raw.size != w * h * 4:
                return False, "layer %s size mismatch" % ent["name"]
            bgra = raw.reshape(h, w, 4).astype(np.float32)
            a = bgra[:, :, 3:4]
            rgb = np.clip(bgra[:, :, 2::-1] * 255.0 / np.maximum(a, 1.0),
                          0, 255).astype(np.uint8)
            img = Image.frombytes("RGBA", (w, h),
                                  np.dstack([rgb, bgra[:, :, 3].astype(np.uint8)]).tobytes())
            if scale < 1.0:
                img = img.resize((max(1, round(w * scale)),
                                  max(1, round(h * scale))), Image.LANCZOS)
            name = "%s%s.png" % (prefix,
                                 re.sub(r"[^A-Za-z0-9_-]", "_", ent["name"]))
            img.save(os.path.join(web_dir, name))
            out_layers.append({"name": ent["name"], "png": name,
                               "w": img.width, "h": img.height})
        except (OSError, ValueError) as e:
            return False, "layer %s convert: %s" % (ent["name"], e)
    with open(os.path.join(web_dir, mf_name), "w",
              encoding="utf-8") as fh:
        json.dump({"width": round(w * scale), "height": round(h * scale),
                   "paper": mf.get("paper", [255, 255, 255]),
                   "layers": out_layers}, fh, ensure_ascii=False)
    return True, ""


def _finish_ok(j):
    arts = {"png": j.out_png, "plan": j.plan_path}
    for key, ext in (("kra", ".kra"), ("psd", ".psd")):
        if j.meta["want"].get(key):
            p = os.path.join(j.dir, j.stem + ext)
            if os.path.isfile(p):
                arts[key] = p
    mp4 = os.path.join(j.dir, j.stem + "_timelapse.mp4")
    if j.meta["want"].get("mp4") and os.path.isfile(mp4):
        arts["mp4"] = mp4
    with j.lock:
        j.meta["artifacts"] = arts
    if j.meta["want"].get("layers"):
        ok, err = _layers_pass(j)
        with j.lock:
            j.meta["layers_err"] = err
    with j.lock:
        j.meta["state"] = "done"
        j.meta["phase"] = "完成"
    j.save_meta()


def worker_loop():
    while True:
        j = _QUEUE.get()
        try:
            if j.cancel.is_set():
                j.set_state("cancelled")
                continue
            j.set_state("planning", phase="排队完成 · 启动引擎")
            eng = resolve_engine()
            if not eng:
                j.set_state("fail", error="engine not found - build "
                                          "dist/daub_paint.exe first")
                continue
            argv = list(eng) + [j.src_path, j.out_png]
            if j.meta["pen"]:
                argv += ["--pen", j.meta["pen"]]
            if j.meta["want"].get("kra"):
                argv += ["--kra", os.path.join(j.dir, j.stem + ".kra")]
            if j.meta["want"].get("psd"):
                argv += ["--psd", os.path.join(j.dir, j.stem + ".psd")]
            if j.meta["want"].get("mp4"):
                argv += ["--timelapse",
                         os.path.join(j.dir, j.stem + "_timelapse.mp4")]
            lt = threading.Thread(target=_live_thread, args=(j,),
                                  daemon=True)
            lt.start()
            rc, tail = _run_engine(j, argv, "engine")
            lt.join(timeout=20)
            if j.cancel.is_set():
                j.set_state("cancelled")
                continue
            if rc != 0 or not os.path.isfile(j.out_png):
                j.set_state("fail", error="engine rc=%s :: %s"
                            % (rc, " ｜ ".join(tail[-2:])))
                continue
            _finish_ok(j)
        except Exception as e:                     # never kill the worker
            try:
                j.set_state("fail", error="worker: %r" % (e,))
            except Exception:
                pass
        finally:
            _QUEUE.task_done()


# --------------------------------------------------------- frame jobs --

_FRAME_Q = queue.Queue()

# the size-ruler edit render serializes the same way: it re-renders every
# layer into layers_edit/, so two overlapping passes would interleave
# writes into the same dir
_EDIT_LOCK = threading.Lock()


def _frame_worker():
    """One dedicated thread for 定格取材 extractions (serialized: each
    is a raw-daub re-render + ffmpeg pass, tens of seconds on real
    plans). State lives in meta['frames'][tag] so it survives
    restarts; artifacts register into meta['artifacts'] and ride the
    existing /api/artifact route for free."""
    while True:
        j, t = _FRAME_Q.get()
        tag = "%g" % t

        def fstate(state, **kw):
            with j.lock:
                rec = j.meta.setdefault("frames", {}).setdefault(tag, {})
                rec["state"] = state
                rec.update(kw)
            j.save_meta()

        try:
            mp4 = os.path.join(j.dir, j.stem + "_timelapse.mp4")
            if not os.path.isfile(mp4):
                fstate("fail", error="job has no timelapse video")
                continue
            daub = resolve_daub()
            if not daub:
                fstate("fail", error="raw renderer missing (daub)")
                continue
            sys.path.insert(0, HERE)
            import daub_frame as dfr
            try:
                import PIL   # noqa: F401  pixel verification needs PIL
                verify = "auto"
            except ImportError:
                verify = "skip"    # count-pin only; the log says so
            log_tail = []

            def flg(m):
                log_tail.append(str(m))
                fstate("running", log="\n".join(log_tail[-6:]))
            # export-format toggles were pinned onto the frame record at
            # submit time (default all on - the historical behaviour)
            with j.lock:
                rec0 = j.meta.get("frames", {}).get(tag) or {}
            flags = {"do_cut": rec0.get("cut", True),
                     "do_kra": rec0.get("kra", True),
                     "do_psd": rec0.get("psd", True)}
            # the web worker always exports the default cadence
            # (big_first) - explicit order: one engine render, no duel
            res = dfr.run(video=mp4, seconds=t, plan=j.plan_path,
                          out_dir=j.dir, base=j.stem, order="big_first",
                          daub=daub[0], verify=verify, log=flg, **flags)
            arts = {"frame_png": res["frame_png"],
                    "frame_plan": res["plan"]}
            if res["cut_mp4"]:
                arts["frame_cut"] = res["cut_mp4"]
            if res["kra"]:
                arts["frame_kra"] = res["kra"]
            if res["psd"]:
                arts["frame_psd"] = res["psd"]
            with j.lock:
                j.meta["artifacts"].update(arts)
                rec = j.meta.setdefault("frames", {}).setdefault(tag, {})
                rec.update(state="done", frame=res["frame"],
                           strokes=res["strokes"], of=res["of"],
                           mean_diff=res["mean_diff_vs_mp4"],
                           artifacts={k: os.path.basename(v)
                                      for k, v in arts.items()})
            j.save_meta()
        except Exception as e:                     # never kill the worker
            try:
                fstate("fail", error="%s: %s" % (type(e).__name__, e))
            except Exception:
                pass
        finally:
            _FRAME_Q.task_done()


def prepare_upload(j, data, maxdim):
    """Decode + tier-downscale the upload, save as the engine reference.
    Returns (ok, err). The processed copy is ALSO the compare-slide ref."""
    try:
        img = Image.open(_bytes_io(data))
        # header 断（decode 前像素还没落地）：格式白名单 + 边长硬上限。
        # PIL 默认炸弹防线在 2x 阈值才 raise，1-2x 之间只警告继续解码
        # ——1.79 亿像素 RGB ≈ 537MB 峰值，2G 机器直接被 OOM 杀服务。
        if (img.format or "") not in REF_FORMATS:
            return False, "unsupported format %s (png/jpeg/webp only)" % (
                img.format or "unknown")
        if max(img.size) > MAX_REF_DIM:
            return False, "image too large %dpx (max %d)" % (
                max(img.size), MAX_REF_DIM)
        img = img.convert("RGB")
        if maxdim and max(img.size) > maxdim:
            r = maxdim / float(max(img.size))
            img = img.resize((max(1, round(img.width * r)),
                              max(1, round(img.height * r))), Image.LANCZOS)
        img.save(j.src_path)
        with j.lock:
            j.meta["canvas"] = [img.width, img.height]
        return True, ""
    except Exception as e:
        return False, "image decode failed: %s" % e


def _bytes_io(data):
    import io
    return io.BytesIO(data)


# ---------------------------------------------------------------- http --

CACHE_OFF = {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                       # keep the studio console calm

    def _deny(self):
        body = b"auth required"
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", 'Basic realm="daub"')
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- helpers ----
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or CACHE_OFF).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path, ctype, cache=False, extra=None):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        hdr = {} if cache else dict(CACHE_OFF)
        hdr.update(extra or {})
        # JSON 大件按 Accept-Encoding 走 gzip：plan ~2.2MB -> ~0.45MB，
        # 是公网部署里唯一的大流量项（图片/视频本就是压缩格式，不动）。
        if (ctype.startswith("application/json") and len(body) > 1024
                and "gzip" in (self.headers.get("Accept-Encoding") or "")):
            import gzip as _gzip
            body = _gzip.compress(body, 6)
            hdr["Content-Encoding"] = "gzip"
            hdr["Vary"] = "Accept-Encoding"
        self._send(200, body, ctype, hdr)

    def _jobdir_file(self, j, rel, ctype, cache=False):
        """Serve a file that must live directly inside the job dir
        (rel = basename only - no traversal)."""
        if os.path.basename(rel) != rel or not rel:
            self._json({"error": "bad path"}, 400)
            return
        p = os.path.join(j.dir, rel)
        if not os.path.isfile(p):
            self._json({"error": "not ready"}, 404)
            return
        self._file(p, ctype, cache)

    # ---- GET ----
    def do_GET(self):
        if not _auth_ok(self.headers.get("Authorization", "")):
            self._deny()
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if u.path in ("/", "/index.html"):
            self._file(UI_HTML, "text/html; charset=utf-8")
        elif u.path == "/favicon.ico":
            if os.path.isfile(ICON):
                self._file(ICON, "image/x-icon", cache=True)
            else:
                self._send(204, b"", "image/x-icon")
        elif u.path == "/api/presets":
            self._json({"presets": load_presets(),
                        "ffmpeg": bool(resolve_ffmpeg()),
                        "engine": bool(resolve_engine()),
                        "daub": bool(resolve_daub())})
        elif u.path == "/api/cal":
            # 真值面板的墨校准（与 tools/data/ink_calib.json 同一份活源）
            self._file(INK_CALIB, "application/json; charset=utf-8")
        elif len(parts) == 3 and parts[0] == "web" and parts[1] == "wasm":
            # 真值面板的 wasm 资产。字典即白名单（点名服务，无目录遍历面）。
            name = parts[2]
            ctype = {"daub.js": "text/javascript; charset=utf-8",
                     "daub_b64.js": "text/javascript; charset=utf-8",
                     "ink_calib.js": "text/javascript; charset=utf-8",
                     "daub_bg.wasm": "application/wasm"}.get(name)
            p = os.path.join(WASM_DIR, name)
            if ctype and os.path.isfile(p):
                self._file(p, ctype)
            else:
                self._json({"error": "no such asset"}, 404)
        elif u.path == "/api/gallery":
            self._json({"jobs": gallery_list()})
        elif u.path == "/api/showcase":
            p = os.path.join(SHOWCASE_DIR, "manifest.json")
            if os.path.isfile(p):
                self._file(p, "application/json; charset=utf-8")
            else:
                self._json({"hero": None, "cases": []})
        elif len(parts) == 3 and parts[0] == "api" \
                and parts[1] == "showcase":
            name = parts[2]
            if os.path.basename(name) != name or not name:
                self._json({"error": "bad path"}, 400)
                return
            p = os.path.join(SHOWCASE_DIR, name)
            ctype = {"png": "image/png", "jpg": "image/jpeg",
                     "jpeg": "image/jpeg", "webp": "image/webp",
                     "mp4": "video/mp4", "webm": "video/webm",
                     "json": "application/json; charset=utf-8"}.get(
                         name.rsplit(".", 1)[-1].lower())
            if ctype and os.path.isfile(p):
                self._file(p, ctype, cache=True, extra={
                    "Cache-Control": "public, max-age=604800"})
            else:
                self._json({"error": "no such asset"}, 404)
        elif len(parts) == 4 and parts[0] == "api" \
                and parts[1] == "showcase" and parts[2] == "fonts":
            name = parts[3]                       # self-hosted display fonts
            if os.path.basename(name) != name or not name:
                self._json({"error": "bad path"}, 400)
                return
            p = os.path.join(SHOWCASE_DIR, "fonts", name)
            ctype = {"woff": "font/woff", "woff2": "font/woff2"}.get(
                name.rsplit(".", 1)[-1].lower())
            if ctype and os.path.isfile(p):
                self._file(p, ctype, cache=True, extra={
                    "Cache-Control": "public, max-age=604800"})
            else:
                self._json({"error": "no such asset"}, 404)
        elif parts == ["api", "mcp", "tools"]:
            self._json({"tools": mcp_tools()})
        elif len(parts) == 3 and parts[0] == "api" and parts[1] == "job":
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            self._json({"job": j.snapshot()})
        elif len(parts) == 4 and parts[0] == "api" and parts[1] == "job":
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            what = parts[3]
            if what == "status":
                self._json({"job": j.snapshot()})
            elif what == "live.png":
                self._jobdir_file(j, "live.png", "image/png")
            elif what == "ref.png":
                self._jobdir_file(j, j.stem + "_ref.png", "image/png",
                                  cache=True)
            elif what == "out.png":
                self._jobdir_file(j, j.stem + ".png", "image/png", cache=True)
            elif what == "layers.json":
                # ?edit=1 serves the size-ruler manifest (plus the sizes
                # so a reloaded page can reposition the sliders); no
                # active edit or no edited render yet -> the original
                edit = q.get("edit", ["0"])[0] == "1" and j.meta.get("edit")
                base = "layers_edit" if edit else "layers"
                p = os.path.join(j.dir, base,
                                 "layers_edit_web.json" if edit
                                 else "layers_web.json")
                if edit and os.path.isfile(p):
                    with open(p, encoding="utf-8") as fh:
                        mf = json.load(fh)
                    mf["edit_sizes"] = j.meta["edit"].get("sizes") or {}
                    self._json(mf)
                elif os.path.isfile(p):
                    self._file(p, "application/json; charset=utf-8",
                               cache=True)
                else:
                    self._json({"error": j.snapshot().get(
                        "layers_err") or "layers not ready"}, 404)
        elif len(parts) == 5 and parts[0] == "api" and parts[1] == "job" \
                and parts[4].endswith(".png") and parts[3] == "layer":
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            name = re.sub(r"[^A-Za-z0-9_-]", "", parts[4][:-4])
            # e_-prefixed PNGs are the size-ruler edit pass's output
            sub = "layers_edit" if name.startswith("e_") else "layers"
            p = os.path.join(j.dir, sub, name + ".png")
            if os.path.isfile(p):
                self._file(p, "image/png", cache=True)
            else:
                self._json({"error": "no layer"}, 404)
        elif len(parts) == 4 and parts[0] == "api" and parts[1] == "artifact":
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            want = parts[3]
            arts = j.meta.get("artifacts", {})
            p = next((v for v in arts.values()
                      if os.path.basename(v) == want), None)
            ctype = {"png": "image/png", "kra": "application/octet-stream",
                     "psd": "application/octet-stream",
                     "mp4": "video/mp4", "plan": "application/json",
                     "json": "application/json; charset=utf-8"}.get(
                         want.rsplit(".", 1)[-1], "application/octet-stream")
            if p and os.path.isfile(p):
                extra = {}
                if q.get("dl"):
                    extra["Content-Disposition"] = \
                        "attachment; filename=\"%s\"" % want
                self._file(p, ctype, cache=bool(q.get("dl")), extra=extra)
            else:
                self._json({"error": "artifact not ready"}, 404)
            return
        else:
            self._json({"error": "no route"}, 404)

    # ---- POST ----
    def do_POST(self):
        if not _auth_ok(self.headers.get("Authorization", "")):
            self._deny()
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if u.path == "/api/job":
            # 磁盘闸：jobs 所在盘剩余 <2GB 拒收新活。参考图 48MB/笔 +
            # 产物在 40G 盘上，凭据一旦泄漏被持续灌盘会把全机拖死——
            # 2r/s 限速挡的是频次，这道闸挡的是体量。
            if shutil.disk_usage(JOBS_DIR).free < 2 * 1024 ** 3:
                self._json({"error": "disk almost full, try later"}, 503)
                return
            try:
                ln = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                ln = 0
            if not (0 < ln <= MAX_UPLOAD):
                self._json({"error": "bad size"}, 400)
                return
            data = self.rfile.read(ln)
            name = (_qtext(q.get("name", [" painting"])[0]) or "painting")[:60]
            tier = q.get("tier", ["std"])[0]
            if tier not in TIER_MAXDIM:
                tier = "std"
            pen = _qtext(q.get("pen", [""])[0])[:60]
            want = {"kra": q.get("kra", ["0"])[0] == "1",
                    "psd": q.get("psd", ["0"])[0] == "1",
                    "mp4": q.get("mp4", ["0"])[0] == "1" and
                           bool(resolve_ffmpeg()),
                    "layers": q.get("layers", ["1"])[0] == "1"}
            j = new_job(name, tier, pen, want)
            j.src_path = os.path.join(j.dir, j.stem + "_ref.png")
            ok, err = prepare_upload(j, data, TIER_MAXDIM[tier])
            if not ok:
                j.set_state("fail", error=err)
                self._json({"error": err}, 400)
                return
            j.save_meta()
            _QUEUE.put(j)
            self._json({"id": j.id})
        elif len(parts) == 4 and parts[3] == "cancel":
            j = get_job(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            j.cancel.set()
            with j.lock:
                if j.proc and j.proc.poll() is None:
                    try:
                        j.proc.terminate()
                    except OSError:
                        pass
            self._json({"ok": True})
        elif len(parts) == 4 and parts[3] == "edit":
            # per-layer size ruler (all ends): {layer: factor} ->
            # sibling <stem>_edit_plan.json (the original is never
            # touched) re-rendered into layers_edit/ as e_-prefixed PNGs;
            # an empty sizes object resets back to the original set.
            # Synchronous on purpose - the client awaits the new layer
            # set; the thread server keeps other requests flowing.
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            if j.meta.get("state") != "done":
                self._json({"error": "job not done"}, 409)
                return
            try:
                ln = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                ln = 0
            if not (0 < ln <= 1 << 20):
                self._json({"error": "bad size"}, 400)
                return
            try:
                body = json.loads(self.rfile.read(ln).decode("utf-8"))
            except ValueError:
                self._json({"error": "bad json"}, 400)
                return
            if not resolve_daub():
                self._json({"error": "raw renderer missing (daub)"}, 503)
                return
            sys.path.insert(0, HERE)
            import render_timelapse as rtl
            try:
                doc = rtl.load_plan(j.plan_path)
                names = rtl.layer_names(doc)
                # JSON keys are strings: bare digits = 0-based
                # first-appearance index, same contract as
                # daub_edit_plan's layer_pens / layer_sizes
                raw = body.get("sizes") or {}
                mapped = {names[int(k)] if k.isdigit() else k: v
                          for k, v in raw.items()}
                sizes = rtl.norm_size_scales(mapped, set(names))
            except (OSError, IndexError, ValueError) as e:
                self._json({"error": str(e) or "bad sizes"}, 400)
                return
            if not _EDIT_LOCK.acquire(blocking=False):
                self._json({"error": "another edit render is running - "
                            "retry in a moment"}, 409)
                return
            try:
                if sizes:
                    seq, _n = rtl.apply_size_scales(doc["strokes"], sizes)
                    doc2 = dict(doc, strokes=seq, count=len(seq))
                    edit_p = os.path.join(j.dir,
                                          j.stem + "_edit_plan.json")
                    with open(edit_p, "w", encoding="utf-8") as fh:
                        json.dump(doc2, fh, ensure_ascii=False)
                    ok, err = _layers_pass(j, plan_path=edit_p,
                                           raw_dir="layers_edit_raw",
                                           out_dir="layers_edit",
                                           mf_name="layers_edit_web.json",
                                           prefix="e_",
                                           png_out="_layers_edit.png")
                    if not ok:
                        self._json({"error": err}, 500)
                        return
                    with j.lock:
                        j.meta["edit"] = {"sizes": sizes,
                                          "plan": os.path.basename(edit_p)}
                        j.meta["artifacts"]["plan_edit"] = edit_p
                    j.save_meta()
                else:
                    # reset: drop the edit, the original set rules again
                    with j.lock:
                        j.meta["edit"] = None
                        j.meta["artifacts"].pop("plan_edit", None)
                    j.save_meta()
                p = os.path.join(j.dir, "layers_edit" if sizes else "layers",
                                 "layers_edit_web.json" if sizes
                                 else "layers_web.json")
                mf = None
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as fh:
                        mf = json.load(fh)
                self._json({"edit": sizes, "layers": mf})
            finally:
                _EDIT_LOCK.release()
        elif len(parts) == 4 and parts[3] == "frame":
            # 定格取材: one moment of this job's timelapse as png / cut
            # mp4 / layered kra/psd / truncated plan. Async - poll the
            # job status (meta['frames'][tag]); artifacts ride
            # /api/artifact once done.
            j = get_job(parts[2]) or job_from_disk(parts[2])
            if not j:
                self._json({"error": "no such job"}, 404)
                return
            try:
                t = float(str(q.get("t", [""])[0]).strip().rstrip("sS"))
            except ValueError:
                self._json({"error": "bad t (seconds, e.g. 12)"}, 400)
                return
            if not (0 <= t <= 7200):
                self._json({"error": "t out of range"}, 400)
                return
            if not resolve_daub():
                self._json({"error": "raw renderer missing (daub)"}, 503)
                return
            if not resolve_ffmpeg():
                self._json({"error": "ffmpeg missing"}, 503)
                return
            if not os.path.isfile(os.path.join(j.dir,
                                               j.stem + "_timelapse.mp4")):
                self._json({"error": "job has no timelapse video "
                            "(submit with mp4=1 first)"}, 409)
                return
            tag = "%g" % t
            with j.lock:
                cur = (j.meta.setdefault("frames", {})
                       .setdefault(tag, {}).get("state"))
            if cur in ("queued", "running"):
                self._json({"id": j.id, "tag": tag, "state": cur})
                return
            # companion-export toggles (frame png + truncated plan are
            # unconditional); the frame worker reads them at dequeue
            want = {"cut": q.get("cut", ["1"])[0] == "1",
                    "kra": q.get("kra", ["1"])[0] == "1",
                    "psd": q.get("psd", ["1"])[0] == "1"}
            with j.lock:
                j.meta.setdefault("frames", {})[tag] = \
                    dict({"state": "queued"}, **want)
            j.save_meta()
            _FRAME_Q.put((j, t))
            self._json({"id": j.id, "tag": tag, "state": "queued"})
        else:
            self._json({"error": "no route"}, 404)


def gallery_list():
    """Past jobs, newest first - from disk, so it survives restarts."""
    out = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for jid in os.listdir(JOBS_DIR):
        try:
            with open(os.path.join(JOBS_DIR, jid, "meta.json"),
                      encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({k: m.get(k) for k in
                    ("id", "name", "ts", "state", "strokes", "tier",
                     "canvas")})
    out.sort(key=lambda d: d.get("ts") or 0, reverse=True)
    return out[:60]


def main():
    global JOBS_DIR
    ap = argparse.ArgumentParser(description="画坊 local web studio")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true",
                    help="open the browser once the server is up")
    ap.add_argument("--jobs-dir", default=JOBS_DIR)
    args = ap.parse_args()
    JOBS_DIR = os.path.abspath(args.jobs_dir)
    os.makedirs(JOBS_DIR, exist_ok=True)
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not resolve_engine():
        print("!! dist/daub_paint.exe not found - build it first "
              "(build_paint.py) or set DAUB_PAINT_EXE")
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=_frame_worker, daemon=True).start()

    url = "http://127.0.0.1:%d/" % args.port
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print("!! cannot bind %s: %s (busy? try --port 8788)" % (url, e))
        sys.exit(2)
    print("daub web studio -> %s   (Ctrl+C to stop)" % url)
    print("   jobs: %s" % JOBS_DIR)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
