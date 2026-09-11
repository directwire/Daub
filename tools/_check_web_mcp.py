"""_check_web_mcp.py - E2E smoke for the web-studio MCP (tools/daub_web_mcp.py).

Spawns a real daub_web.py (auth on) + a real MCP stdio subprocess and
drives JSON-RPC by hand:

  1. initialize          -> serverInfo.name == "daub-web"
  2. tools/list          -> the 7 web tools
  3. daub_web_generate   -> done, PNG lands locally (real engine render,
                            mp4 requested so 4b has a video to slice)
  4. daub_web_status     -> strokes > 0
  4b. daub_web_frame     -> done, toggles OFF (kra=False psd=False):
                            exactly frame png / cut mp4 / truncated plan
  4c. frame artifacts    -> truncated plan count == strokes at t=1s
  4d. daub_web_frame     -> defaults ON: all five artifacts land
  4e. daub_web_edit      -> size ruler applies: edit map + e_-prefixed
                            layer set, manifest serves edit_sizes
  4f. edited plan        -> plan_edit artifact carries 1.5x sizes
  4g. daub_web_edit {}   -> reset: original set rules again
  4h. unknown layer      -> clean isError
  5. daub_web_fetch      -> ref.png lands locally
  6. daub_web_gallery    -> the job is listed
  7. daub_web_cancel     -> unknown jid surfaces a clean isError
  8. wrong-auth instance -> 401 surfaces as a friendly isError

Run:  pack_venv/Scripts/python.exe tools/_check_web_mcp.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8795
BASE = "http://127.0.0.1:%d" % PORT
AUTH = "check:pw-%d" % os.getpid()


def leg(n, ok, detail=""):
    print("  %s. %s%s" % (n, "PASS" if ok else "FAIL", " - " + detail
                          if detail else ""))
    if not ok:
        stop_all()
        sys.exit(1)


def rpc(proc, method, params=None, _next=[1]):
    _next[0] += 1
    line = json.dumps({"jsonrpc": "2.0", "id": _next[0],
                       "method": method, "params": params or {}})
    proc.stdin.write(line + "\n")
    proc.stdin.flush()
    while True:                       # skip stray notifications
        resp = json.loads(proc.stdout.readline())
        if resp.get("id") == _next[0]:
            return resp


def call(proc, name, args):
    return rpc(proc, "tools/call", {"name": name, "arguments": args})


def text(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def stop_all():
    for p in (MCP, WEB):
        if p and p.poll() is None:
            p.kill()


tmp = tempfile.mkdtemp(prefix="daub_web_mcp_")
jobs = os.path.join(tmp, "jobs")
out = os.path.join(tmp, "out")

# tiny colorful reference (real engine needs a real-ish image)
from PIL import Image, ImageDraw   # noqa: E402  (pack_venv has PIL)

img = Image.new("RGB", (128, 160), (245, 240, 225))
d = ImageDraw.Draw(img)
d.ellipse((20, 20, 108, 108), fill=(180, 60, 50))
d.rectangle((14, 110, 114, 150), fill=(40, 60, 120))
ref = os.path.join(tmp, "ref.png")
img.save(ref)

py = sys.executable
env = dict(os.environ, DAUB_WEB_URL=BASE, DAUB_WEB_AUTH=AUTH,
           DAUB_WEB_OUT=out)

# web studio, auth on, engine resolves through the same dev fallback the
# production Linux box uses (sys.executable + tools/daub_paint.py)
WEB = subprocess.Popen(
    [py, os.path.join(HERE, "daub_web.py"), "--port", str(PORT),
     "--jobs-dir", jobs],
    env=dict(env, DAUB_AUTH=AUTH),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
MCP = None

import time   # noqa: E402
for _ in range(40):
    try:
        import urllib.request   # noqa: E402
        req = urllib.request.Request(
            BASE + "/api/presets",
            headers={"Authorization": "Basic " + __import__("base64")
                     .b64encode(AUTH.encode()).decode()})
        urllib.request.urlopen(req, timeout=2)
        break
    except Exception:
        time.sleep(0.25)
else:
    print("web studio never came up")
    sys.exit(1)

try:
    MCP = subprocess.Popen(
        [py, os.path.join(HERE, "daub_web_mcp.py")], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8")

    r = rpc(MCP, "initialize", {"protocolVersion": "2025-06-18"})
    leg(1, r["result"]["serverInfo"]["name"] == "daub-web")

    r = rpc(MCP, "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    leg(2, names == {"daub_web_generate", "daub_web_frame",
                     "daub_web_edit", "daub_web_status",
                     "daub_web_fetch", "daub_web_gallery",
                     "daub_web_cancel"},
        "%d tools" % len(names))

    r = call(MCP, "daub_web_generate",
             {"image": ref, "name": "check", "tier": "fast", "psd": True,
              "mp4": True, "wait_s": 240})
    g = text(r)
    leg(3, g.get("state") == "done" and g.get("files", {}).get("png")
        and os.path.isfile(g["files"]["png"])
        and os.path.getsize(g["files"]["png"]) > 1000,
        "strokes=%s png=%dB" % (g.get("strokes"),
                                g.get("png_bytes") or 0))
    if g.get("files", {}).get("psd"):
        leg("3b", os.path.getsize(g["files"]["psd"]) > 26, "psd ok")

    r = call(MCP, "daub_web_status", {"jid": g["jid"]})
    s = text(r)
    leg(4, s.get("state") == "done" and (s.get("strokes") or 0) > 0,
        "strokes=%s" % s.get("strokes"))

    r = call(MCP, "daub_web_frame", {"jid": g["jid"], "seconds": 1,
                                     "kra": False, "psd": False,
                                     "wait_s": 240})
    fr = text(r)
    ffiles = fr.get("files") or {}
    leg("4b", fr.get("state") == "done" and
        all(os.path.isfile(p) and os.path.getsize(p) > 0
            for p in ffiles.values()) and
        set(ffiles) == {"frame_png", "frame_cut", "frame_plan"},
        "state=%s files=%s" % (fr.get("state"), sorted(ffiles)))
    if fr.get("state") != "done":
        print("frame detail:", fr)
        stop_all()
        sys.exit(1)
    with open(ffiles["frame_plan"], encoding="utf-8") as fh:
        frj = json.load(fh)
    leg("4c", frj.get("count") == len(frj["strokes"]) ==
        fr.get("strokes") and 0 < fr.get("strokes") <= fr.get("of"),
        "count=%s strokes=%s/%s (truncated plan consistent)"
        % (frj.get("count"), fr.get("strokes"), fr.get("of")))

    r = call(MCP, "daub_web_frame", {"jid": g["jid"], "seconds": 1.5,
                                     "wait_s": 240})
    fr2 = text(r)
    ffiles2 = fr2.get("files") or {}
    leg("4d", fr2.get("state") == "done" and
        {"frame_png", "frame_cut", "frame_kra", "frame_psd",
         "frame_plan"} <= set(ffiles2) and
        all(os.path.isfile(p) and os.path.getsize(p) > 0
            for p in ffiles2.values()),
        "state=%s files=%s" % (fr2.get("state"), sorted(ffiles2)))

    # ---- size ruler (09-10): apply / manifest / plan / reset / error --
    import base64   # noqa: E402
    authh = {"Authorization": "Basic " + base64.b64encode(
        AUTH.encode()).decode()}

    r = call(MCP, "daub_web_edit", {"jid": g["jid"], "sizes": {"1": 1.5}})
    ed = {"edit": None, "layers": []}
    if not r["result"].get("isError"):
        ed = text(r)
    else:
        print("edit error:", r["result"]["content"][0]["text"][:200])
    leg("4e", r["result"].get("isError") is None and ed.get("edit")
        and len(ed.get("layers") or []) >= 2
        and any(L["png"].startswith("e_") for L in ed["layers"]),
        "edit=%s layers=%d" % (ed.get("edit"), len(ed.get("layers") or [])))

    import urllib.request   # noqa: E402
    req = urllib.request.Request(
        BASE + "/api/job/%s/layers.json?edit=1" % g["jid"], headers=authh)
    mf = json.load(urllib.request.urlopen(req, timeout=10))
    leg("4f", mf.get("edit_sizes") == ed.get("edit") and
        all(L["png"].startswith("e_") for L in mf["layers"]),
        "edit_sizes=%s" % mf.get("edit_sizes"))

    snap = text(call(MCP, "daub_web_status", {"jid": g["jid"]}))
    pe = (snap.get("artifacts") or {}).get("plan_edit")
    pb = (snap.get("artifacts") or {}).get("plan")
    scaled_ok = None
    if pe and pb:
        f0 = text(call(MCP, "daub_web_fetch", {"jid": g["jid"],
                                               "file": pb}))
        f1 = text(call(MCP, "daub_web_fetch", {"jid": g["jid"],
                                               "file": pe}))
        d0 = json.load(open(f0["saved"], encoding="utf-8"))
        d1 = json.load(open(f1["saved"], encoding="utf-8"))
        Lname = list(ed["edit"])[0]
        s0 = sorted({s["size"] for s in d0["strokes"]
                     if s["layer"] == Lname})
        s1 = sorted({s["size"] for s in d1["strokes"]
                     if s["layer"] == Lname})
        scaled_ok = s1 and len(s0) == len(s1) and all(
            abs(b - a * 1.5) < 0.11 for a, b in zip(s0, s1))
        detail = "%s %s -> %s" % (Lname, s0[:4], s1[:4])
    else:
        detail = "plan_edit=%r plan=%r" % (pe, pb)
    leg("4g", bool(scaled_ok), detail)

    r = call(MCP, "daub_web_edit", {"jid": g["jid"], "sizes": {}})
    ed2 = text(r)
    leg("4h", ed2.get("edit") == {} and ed2.get("layers") and
        not any(L["png"].startswith("e_") for L in ed2["layers"]),
        "edit=%r layers=%d" % (ed2.get("edit"),
                               len(ed2.get("layers") or [])))

    r = call(MCP, "daub_web_edit", {"jid": g["jid"],
                                    "sizes": {"nope": 2.0}})
    leg("4i", r["result"].get("isError") is True,
        r["result"]["content"][0]["text"][:60])

    r = call(MCP, "daub_web_fetch", {"jid": g["jid"], "file": "ref.png"})
    f = text(r)
    leg(5, os.path.isfile(f.get("saved", "")), f.get("saved", ""))

    r = call(MCP, "daub_web_gallery", {"limit": 5})
    gal = text(r)
    leg(6, any(j["id"] == g["jid"] for j in gal.get("jobs", [])),
        "%d jobs" % len(gal.get("jobs", [])))

    r = call(MCP, "daub_web_cancel", {"jid": "deadbeef"})
    leg(7, r["result"].get("isError") is True,
        r["result"]["content"][0]["text"][:60])

    MCP.kill()
    env2 = dict(env, DAUB_WEB_AUTH="check:WRONG")
    MCP = subprocess.Popen(
        [py, os.path.join(HERE, "daub_web_mcp.py")], env=env2,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    rpc(MCP, "initialize", {"protocolVersion": "2025-06-18"})
    r = call(MCP, "daub_web_gallery", {})
    leg(8, r["result"].get("isError") and "401" in
        r["result"]["content"][0]["text"],
        r["result"]["content"][0]["text"][:50])

    print("ALL PASS - %s" % tmp)
finally:
    stop_all()
    shutil.rmtree(tmp, ignore_errors=True)
