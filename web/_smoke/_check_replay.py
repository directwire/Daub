"""web 回放器的无头验证环（W12-T3）。

三条证据，缺一即 FAIL（防"看着像跑了"）：
1. dump-dom 探针：#smoke-probe data-done="1" —— 虚拟时间预算内真把
   全部笔画放完（RAF 节拍真实跑过，不是空页面）。
2. 画了东西：回放终帧 vs 纯 bg 铺底的 mean|diff| 必须显著 > 0。
3. 保真度量化：回放终帧 vs daub.exe 真渲（truth）的 mean|diff| /
   within60，写入 fidelity_baseline.json —— JS 笔模是近似（replay.js
   头注），这个数只入库报数，不许吹收敛；后续 T2 调家族参数看它降。

用法：pack_venv python web/_smoke/_check_replay.py [--regen-sample]
样例缺失时自动从 _smoke/build/sample512_plan.json 生成 sample_plan.js。
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)
ROOT = os.path.dirname(WEB)
DAUB = os.path.join(ROOT, "target", "release", "daub.exe")
CAL = os.path.join(ROOT, "tools", "data", "ink_calib.json")
TIPS = os.path.join(ROOT, "tools", "data", "brush_lib.json")
PLAN = os.path.join(HERE, "build", "sample512_plan.json")
SAMPLE_JS = os.path.join(WEB, "sample_plan.js")
PAGE = "file:///" + os.path.join(WEB, "index.html").replace("\\", "/")
BUILD = os.path.join(HERE, "build")
BASELINE = os.path.join(HERE, "fidelity_baseline.json")

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    fail("no chrome/msedge found")

def headless(browser, url, extra):
    out = os.path.join(BUILD, "headless_out")
    cmd = [browser, "--headless=new", "--disable-gpu",
           "--window-size=512,512", "--virtual-time-budget=200000"]
    r = subprocess.run(cmd + extra + [url], capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        fail("headless rc=%s\n%s" % (r.returncode, r.stderr[-500:]))
    return r

def main():
    if "--regen-sample" in sys.argv and os.path.isfile(SAMPLE_JS):
        os.remove(SAMPLE_JS)
    if not os.path.isfile(SAMPLE_JS):
        if not os.path.isfile(PLAN):
            fail("missing %s - plan ref512 via dev tree first" % PLAN)
        doc = json.load(open(PLAN, encoding="utf-8"))
        with open(SAMPLE_JS, "w", encoding="utf-8") as fh:
            fh.write("window.DAUB_SAMPLE = ")
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write(";\n")
        print("sample_plan.js written (%.1f MB, %d strokes)"
              % (os.path.getsize(SAMPLE_JS) / 1e6, len(doc["strokes"])))
    if not os.path.isfile(DAUB):
        fail("missing daub.exe (cargo build --release first)")
    browser = find_browser()

    # 证据 1：探针确认播完（dump-dom 单独跑——screenshot 不能合体）。
    # turbo=1 = setTimeout 驱动：虚拟时间下 dump-dom 实证可靠。
    url = PAGE + "?bare=1&sample=1&speed=100&autoplay=1&turbo=1"
    r = headless(browser, url, ["--dump-dom"])
    probe_ok = 'data-done="1"' in r.stdout
    if not probe_ok:
        # 把 dom 里探针现状吐出来帮排障
        for line in r.stdout.splitlines():
            if "smoke-probe" in line:
                print("probe line:", line.strip()[:200])
                break
        fail("probe data-done != 1 (playback did not finish under "
             "virtual-time budget)")
    print("PASS probe: playback finished (data-done=1)")

    # 证据 2+3：终帧截图，对 bg（画没画）与 truth（画得像不像）。
    # svg=1 = 保真度采集走 replay.js 的 SVG 序列化器（与 drawStroke
    # 同一族数学）：headless 的 canvas2d 在 ~500-2000 笔后软件光栅
    # 腐败（显示黑、getImageData 全零，T2 六旗标实证无解），SVG 4 万
    # 元素实测稳定；virtual-time 下 RAF/timer 调度另有竞态（T3），
    # final=1 同步呈现不赌调度。
    shot = os.path.join(BUILD, "replay.png").replace("\\", "/")
    final_url = PAGE + "?bare=1&sample=1&svg=1&final=1"
    r = headless(browser, final_url, ["--screenshot=" + shot])
    if not os.path.isfile(shot) or os.path.getsize(shot) <= 0:
        fail("screenshot missing")
    from PIL import Image
    import numpy as np
    rep = np.asarray(Image.open(shot).convert("RGB"), dtype=np.int16)
    truth_png = os.path.join(BUILD, "truth.png")
    cmd = [DAUB, "render", PLAN, "--out", truth_png, "--cal", CAL,
           "--tips", TIPS]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0 or not os.path.isfile(truth_png):
        fail("truth render failed: %s" % r.stderr[-300:])
    tru = np.asarray(Image.open(truth_png).convert("RGB"), dtype=np.int16)
    if rep.shape != tru.shape:
        fail("replay %s != truth %s" % (rep.shape, tru.shape))

    bg = np.zeros_like(tru)
    doc = json.load(open(PLAN, encoding="utf-8"))
    bg[:] = np.array(doc["bg"], dtype=np.int16)

    def m(a, b):
        return float(np.abs(a - b).max(axis=2).mean())

    vs_bg, vs_truth = m(rep, bg), m(rep, tru)
    dmax = np.abs(rep - tru).max(axis=2)
    within60 = round(float((dmax <= 60).mean()) * 100.0, 1)
    if vs_bg < 5.0:
        fail("replay frame ~= bare bg (vs_bg=%.2f) - nothing painted"
             % vs_bg)
    print("PASS painted: vs_bg mean|diff| = %.2f (>> 0)" % vs_bg)
    print("FIDELITY vs daub truth: mean|diff| = %.2f, within60 = %.1f%%"
          % (vs_truth, within60))

    base = {"mean_abs_vs_truth": round(vs_truth, 2),
            "within60_pct": within60,
            "mean_abs_vs_bg": round(vs_bg, 2),
            "strokes": len(doc["strokes"]),
            "plan": os.path.basename(PLAN)}
    wrote = "baseline written"
    if os.path.isfile(BASELINE):
        old = json.load(open(BASELINE, encoding="utf-8"))
        base["prev_mean_abs"] = old.get("mean_abs_vs_truth")
        wrote = "baseline updated (prev %s)" % old.get("mean_abs_vs_truth")
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(base, fh, indent=1)
        fh.write("\n")
    print(wrote)

    # 证据 4：浏览器内 webm 导出真身（MediaRecorder captureStream）。
    # 测试页自动截断 300 笔 → exportWebm() → blob as data URL 写进
    # DOM；dump-dom 取回解码落盘，ffmpeg 验流。无 ffmpeg 则 SKIP。
    test_html = os.path.join(BUILD, "webm_test.html")
    with open(os.path.join(WEB, "_smoke", "_webm_test.html"),
              encoding="utf-8") as fh:
        tpl = fh.read()
    with open(test_html, "w", encoding="utf-8") as fh:
        fh.write(tpl)
    r = headless(browser,
                 "file:///" + test_html.replace("\\", "/"), ["--dump-dom"])
    m = re.search(r'data-data="(data:video/webm;base64,[^"]+)"', r.stdout)
    if not m:
        fail("webm export produced no data URL (MediaRecorder leg)")
    import base64
    webm = os.path.join(BUILD, "export_check.webm")
    with open(webm, "wb") as fh:
        fh.write(base64.b64decode(m.group(1).split(",", 1)[1]))
    ff = shutil.which("ffmpeg")
    if not ff:
        print("SKIP webm decode: ffmpeg not on PATH "
              "(blob %d bytes exported)" % os.path.getsize(webm))
    else:
        p = subprocess.run([ff, "-hide_banner", "-i", webm, "-f", "null", "-"],
                           capture_output=True, encoding="utf-8",
                           errors="replace")
        err = p.stderr
        if "Video: vp" not in err and "Video: vp9" not in err:
            fail("exported webm has no vp8/vp9 video stream\n%s" % err[-300:])
        dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
        secs = (float(dur.group(1)) * 3600 + float(dur.group(2)) * 60
                + float(dur.group(3))) if dur else 0.0
        if secs <= 0:
            fail("exported webm duration 0")
        print("PASS webm export: %d bytes, %.2fs, vp9 stream decodes"
              % (os.path.getsize(webm), secs))

    # 证据 5：回放页拖拽腿。?droptest= 让页面 fetch 计划文本后合成
    # File+DataTransfer 派发真 DragEvent("drop") 到 #drop——与真人拖拽
    # 同一条 handler 链（File.text() → loadDoc），仅 OS 级传输是模拟的。
    # 配 final=1 走确定性呈现（done=1 同步置位，不赌播放节拍）。
    import http.server
    import threading

    class _CorsPlanServer(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=BUILD, **kw)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, *a):  # 静默
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CorsPlanServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        purl = "http://127.0.0.1:%d/sample512_plan.json" % srv.server_address[1]
        r = headless(browser,
                     PAGE + "?bare=1&final=1&droptest=" + purl,
                     ["--dump-dom"])
        if 'data-done="1"' not in r.stdout:
            fail("droptest: data-done != 1 (drop handler 链未走通)")
        m = re.search(r'data-total="(\d+)"', r.stdout)
        n_strokes = len(doc["strokes"])
        if not m or int(m.group(1)) != n_strokes:
            fail("droptest: data-total %s != %d"
                 % (m and m.group(1), n_strokes))
        print("PASS droptest: done=1, %d strokes via 真 drop handler 链"
              % n_strokes)
    finally:
        srv.shutdown()

    # 证据 6：层静音腿。?mute=F1,L52 走与 chip 点按同一条 setVisible
    # 路径，SVG 终帧透传 muted。断言：静音图 ≠ 全图层（墨真被剔除）
    # 且更靠近裸纸（剔除方向正确）。
    shot_muted = os.path.join(BUILD, "replay_muted.png").replace("\\", "/")
    r = headless(browser,
                 PAGE + "?bare=1&sample=1&svg=1&final=1&mute=F1,L52",
                 ["--screenshot=" + shot_muted])
    if not os.path.isfile(shot_muted) or os.path.getsize(shot_muted) <= 0:
        fail("muted screenshot missing")
    mu = np.asarray(Image.open(shot_muted).convert("RGB"), dtype=np.int16)
    if mu.shape != tru.shape:
        fail("muted %s != truth-shape %s" % (mu.shape, tru.shape))
    d_mute_all = float(np.abs(mu - rep).mean())
    if d_mute_all <= 1.0:
        fail("mute=F1,L52 changed nothing (mean|diff| %.2f)" % d_mute_all)
    d_mute_bg = float(np.abs(mu - bg).mean())
    d_all_bg = float(np.abs(rep - bg).mean())
    if d_mute_bg >= d_all_bg:
        fail("muted render not closer to bare paper (%.2f >= %.2f)"
             % (d_mute_bg, d_all_bg))
    print("PASS mute leg: F1,L52 removed (vs all %.2f, vs bg %.2f<%.2f)"
          % (d_mute_all, d_mute_bg, d_all_bg))

    print("ALL CHECKS PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
