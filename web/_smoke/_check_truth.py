"""工作台真值面板冒烟（W12-T7）。

自起 daub_web.py（测试端口），取 web_jobs 下最新一笔有 plan 产物的完稿
job，headless 打开 ?job=<id> 深链做 dump-dom，断言四件事：
1. #truth 面板已点亮（class 含 on）
2. tCnt 是 "N / N"（TruthPlayer 真渲终帧完成，N>0）
3. #tStat 无错（为空）
4. 导出四钮 off 类全摘（PNG / .KRA / .PSD / 录制 解禁）

像素与字节契约不在本脚本：TruthPlayer 终帧逐像素、KRA/PSD 逐字节
对表在 web/_smoke/_check_wasm.py 的六腿门禁里，两把尺子各管各的。

用法：pack_venv python web/_smoke/_check_truth.py [--port 8791]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)
ROOT = os.path.dirname(WEB)
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


def headless(browser, url):
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-proxy-server",
           "--window-size=512,512", "--virtual-time-budget=200000",
           "--dump-dom", url]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        fail("headless rc=%s\n%s" % (r.returncode, r.stderr[-500:]))
    return r.stdout


def latest_done_job():
    """最新一笔有 plan 产物的完稿 job（meta.json 的 artifacts.plan 在即完稿）。"""
    best = None
    for meta in glob.glob(os.path.join(ROOT, "web_jobs", "*", "meta.json")):
        try:
            m = json.load(open(meta, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (m.get("artifacts") or {}).get("plan"):
            t = os.path.getmtime(meta)
            if best is None or t > best[0]:
                best = (t, os.path.basename(os.path.dirname(meta)))
    return best and best[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()

    jid = latest_done_job()
    if not jid:
        fail("web_jobs 下没有完稿 job（先在画坊跑一笔再跑本冒烟）")
    print("job:", jid)

    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tools", "daub_web.py"),
         "--port", str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/cal" % args.port,
                    timeout=1).read()
                up = True
                break
            except Exception:
                time.sleep(0.5)
        if not up:
            fail("daub_web.py 起不来（端口 %d 占用？--port 换一个）" % args.port)

        dom = None
        reason = "no attempt"
        for attempt in range(3):
            dom = headless(find_browser(),
                           "http://127.0.0.1:%d/?job=%s" % (args.port, jid))
            if not re.search(r'<div[^>]*id="truth"[^>]*class="[^"]*\bon',
                             dom) and not re.search(
                    r'<div[^>]*class="[^"]*\bon[^"]*"[^>]*id="truth"', dom):
                reason = "#truth 面板没点亮"
            else:
                cnt = re.search(r'id="tCnt">([^<]*)<', dom)
                if not cnt or not re.match(r"^([1-9]\d*) / \1$",
                                           cnt.group(1)):
                    reason = "tCnt=%r —— TruthPlayer 终帧没出来" % (
                        cnt and cnt.group(1))
                else:
                    stat = re.search(r'id="tStat"[^>]*>([^<]*)<', dom)
                    if stat and stat.group(1).strip():
                        reason = "tStat 有错: %s" % stat.group(1)
                    else:
                        bad = next((b for b in ("tPng", "tKra", "tPsd",
                                                "tRec")
                                    if re.search(r'id="%s" class="[^"]*off'
                                                 % b, dom)), None)
                        if bad:
                            reason = "导出钮 %s 还挂着 off（没解禁）" % bad
                        else:
                            reason = None
            if reason is None:
                cnt = re.search(r'id="tCnt">([^<]*)<', dom)
                print("truth panel: on, %s strokes, no err, 4 exports "
                      "armed" % cnt.group(1).split(" /")[0])
                print("PASS: 工作台真值面板深链自启全绿 -> job %s" % jid)
                return
            print("attempt %d/3: %s" % (attempt + 1, reason))
        # 留现场：block1 走到哪、面板状态、有没有报错条
        with open(os.path.join(HERE, "build", "truth_fail.html"), "w",
                  encoding="utf-8") as fh:
            fh.write(dom)
        print("现场已存 web/_smoke/build/truth_fail.html")
        fail(reason)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
