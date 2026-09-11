"""WASM 真渲核的浏览器验证环（W12-T6）。

证据链，缺一即 FAIL：
1. wasm.html 自证词：#probe data-done="1" —— base64 内联 wasm 在虚拟
   时间预算内编译+渲完（render_plan_rgba 真跑过）。
2. 逐像素对表：截图 vs daub.exe --render-only 真渲（truth.png）的
   mean|diff| —— 同一 Rust 渲染核，期望 ≈0；>2 即 FAIL（找回归，
   不是"近似入库"）。这与 replay.js 的 7.66 近似口径是两把尺子。
3. 尺寸对账：r.width/height == plan.canvas。

用法：pack_venv python web/_smoke/_check_wasm.py
资产缺失时自动调 tools/_gen_web_wasm_assets.py（需先 cargo build wasm）。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)
ROOT = os.path.dirname(WEB)
DAUB = os.path.join(ROOT, "target", "release", "daub.exe")
CAL = os.path.join(ROOT, "tools", "data", "ink_calib.json")
PLAN = os.path.join(HERE, "build", "sample512_plan.json")
PAGE = "file:///" + os.path.join(WEB, "wasm.html").replace("\\", "/")
BUILD = os.path.join(HERE, "build")
BASELINE = os.path.join(HERE, "wasm_fidelity.json")
GEN = os.path.join(ROOT, "tools", "_gen_web_wasm_assets.py")
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
os.makedirs(BUILD, exist_ok=True)


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    fail("no chrome/msedge found")


def headless(browser, url, extra):
    # --no-proxy-server：?plan= 插座腿要真连 127.0.0.1，系统代理会拦
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-proxy-server",
           "--window-size=512,512", "--virtual-time-budget=200000"]
    r = subprocess.run(cmd + extra + [url], capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        fail("headless rc=%s\n%s" % (r.returncode, r.stderr[-500:]))
    return r


def render_truth(plan_path, out_png):
    subprocess.check_call([DAUB, "render", plan_path, "--out", out_png,
                           "--cal", CAL, "--no-tips"],
                          stdout=subprocess.DEVNULL)
    return out_png


def mean_abs(a_path, b_path):
    from PIL import Image, ImageChops
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        fail("size mismatch %s vs %s" % (a.size, b.size))
    h = ImageChops.difference(a, b).histogram()
    n = a.size[0] * a.size[1]
    # 分通道直方图求均值（ImageChops.histogram 为三通道拼接）
    tot = sum(i * h[i] for i in range(256))
    tot += sum(i * h[256 + i] for i in range(256))
    tot += sum(i * h[512 + i] for i in range(256))
    return tot / (3.0 * n)


def main():
    from PIL import Image
    browser = find_browser()
    for asset in ("daub.js", "daub_b64.js", "ink_calib.js"):
        if not os.path.isfile(os.path.join(WEB, "wasm", asset)):
            print("asset %s missing - regenerating…" % asset)
            subprocess.check_call([sys.executable, GEN])
            break

    # 证据 1：自证词 dump-dom
    dom = headless(browser, PAGE, ["--dump-dom"]).stdout
    done = re.search(r'data-done="([^"]*)"', dom)
    if not done or done.group(1) != "1":
        err = re.search(r'data-err="([^"]*)"', dom)
        fail("probe not done; err=%s; tail=%s"
             % (err and err.group(1), dom[-300:]))
    px = re.search(r'data-px="([^"]*)"', dom)
    ms = re.search(r'data-ms="([^"]*)"', dom)
    dw = re.search(r'data-w="(\d+)"', dom)
    dh = re.search(r'data-h="(\d+)"', dom)

    # 证据 3：尺寸对账（plan.canvas）
    plan = json.load(open(PLAN, encoding="utf-8"))
    if not dw or not dh or (int(dw.group(1)), int(dh.group(1))) != tuple(plan["canvas"]):
        fail("wasm dims %s/%s != plan.canvas %s"
             % (dw and dw.group(1), dh and dh.group(1), plan["canvas"]))

    # 真值：daub.exe 同计划 --no-tips（带层计划无 tip 引擎，逐像素同路）
    truth = render_truth(PLAN, os.path.join(BUILD, "truth_wasm.png"))

    # 证据 2：截图 vs 真值，期望 ≈0（bare=1 让视口截图=画布逐像素）
    shot = os.path.abspath(os.path.join(BUILD, "wasm_render.png"))
    headless(browser, PAGE + "?bare=1", ["--screenshot=" + shot])
    Image.open(shot).convert("RGB").save(shot)
    diff = mean_abs(shot, truth)

    # 证据 4：?plan=<url> 远程计划插座。页面在 file://，跨源 fetch 需要
    # CORS 头——真实部署中计划托管方要么与页面同源，要么带 ACAO，二选一。
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
        url = "http://127.0.0.1:%d/sample512_plan.json" % srv.server_address[1]
        dom2 = headless(browser, PAGE + "?plan=" + url,
                        ["--dump-dom"]).stdout
        done2 = re.search(r'data-done="([^"]*)"', dom2)
        if not done2 or done2.group(1) != "1":
            err2 = re.search(r'data-err="([^"]*)"', dom2)
            fail("?plan= socket: probe not done; err=%s"
                 % (err2 and err2.group(1)))
        px2 = re.search(r'data-px="([^"]*)"', dom2)
        if px2 and px and px2.group(1) != px.group(1):
            fail("?plan= render px differ from sample render")
        print("?plan= socket: done=1, px match sample")

        # 证据 5：拖拽腿。?droptest= 让页面 fetch 计划文本后合成
        # File+DataTransfer 派发真 DragEvent("drop")——走的是与真人拖拽
        # 同一条 handler 链（File.text() → 真渲），只有 OS 级传输是模拟的。
        dom3 = headless(browser, PAGE + "?droptest=" + url,
                        ["--dump-dom"]).stdout
        done3 = re.search(r'data-done="([^"]*)"', dom3)
        if not done3 or done3.group(1) != "1":
            err3 = re.search(r'data-err="([^"]*)"', dom3)
            fail("droptest: probe not done; err=%s" % (err3 and err3.group(1)))
        px3 = re.search(r'data-px="([^"]*)"', dom3)
        if not px3 or px3.group(1) != px.group(1):
            fail("droptest render px differ from sample render")
        print("droptest: done=1, px match sample (真 drop handler 链)")

        # 证据 6：真值三出口契约。wasm_exports.html 用 TruthPlayer 出终
        # 帧（与 render_plan_rgba 逐像素互证），build_kra_bytes /
        # build_psd_bytes 以 base64 写进探针 data-* 属性随 dump-dom 落地
        # （不走网络回传——virtual-time 预算不等 2MB 上传，POST 半途被
        # 拆是实证过的死法），与 native daub.exe（--no-tips 同路）产物
        # 逐字节对表——kra/psd 全程固定日期，字节决定论是硬契约。
        # 文档名随输出 stem：两侧都用 "smoke.*" 才能逐字节相等。
        page6 = ("file:///" + os.path.join(WEB, "_smoke", "wasm_exports.html")
                 .replace("\\", "/") + "?plan=" + url)
        dom6 = headless(browser, page6, ["--dump-dom"]).stdout
        done6 = re.search(r'data-done="([^"]*)"', dom6)
        if not done6 or done6.group(1) != "1":
            err6 = re.search(r'data-err="([^"]*)"', dom6)
            fail("exports: probe not done; err=%s" % (err6 and err6.group(1)))
        grab = lambda attr: re.search(r'data-%s="([^"]*)"' % attr, dom6)
        exp = {k: grab(k).group(1) for k in
               ("px_player", "px_whole", "ms", "strokes", "kra", "psd")}
        if exp["px_player"] != exp["px_whole"]:
            fail("TruthPlayer 终帧 != render_plan_rgba（%s vs %s）"
                 % (exp["px_player"], exp["px_whole"]))
        if exp["px_player"] != px.group(1):
            fail("TruthPlayer 终帧 != 样例真值 px")
        native6 = os.path.join(BUILD, "smoke_native.png")
        subprocess.check_call(
            [DAUB, "render", PLAN, "--out", native6, "--cal", CAL,
             "--no-tips", "--kra", os.path.join(BUILD, "smoke.kra"),
             "--psd", os.path.join(BUILD, "smoke.psd")],
            stdout=subprocess.DEVNULL)
        import base64 as _b64
        import hashlib
        for ext in ("kra", "psd"):
            with open(os.path.join(BUILD, "browser." + ext), "wb") as fh:
                fh.write(_b64.b64decode(exp[ext]))
            h = lambda p: hashlib.sha256(open(os.path.join(BUILD, p),
                                              "rb").read()).hexdigest()[:16]
            if h("browser." + ext) != h("smoke." + ext):
                fail("browser %s bytes != native (browser=%s native=%s)"
                     % (ext, h("browser." + ext), h("smoke." + ext)))
        print("exports: TruthPlayer px==whole==sample; kra/psd bytes "
              "== native (%s strokes, %sms)" % (exp["strokes"], exp["ms"]))
    finally:
        srv.shutdown()

    print("probe px=%s ms=%s dims=%sx%s" % (px and px.group(1),
                                            ms and ms.group(1),
                                            dw.group(1), dh.group(1)))
    print("wasm-in-browser vs daub.exe truth: mean|diff| = %.2f" % diff)
    if diff > 2.0:
        fail("wasm render diverges from daub.exe (mean|diff| %.2f > 2)" % diff)
    json.dump({"mean_abs_vs_truth": round(diff, 2),
               "w_ms": ms and ms.group(1),
               "canvas": plan["canvas"],
               "strokes": plan["count"]},
              open(BASELINE, "w", encoding="utf-8"), indent=1)
    print("PASS: wasm 真渲核浏览器对表全绿 ->", BASELINE)


if __name__ == "__main__":
    main()
