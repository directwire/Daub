"""鉴权门冒烟（公网部署前置）。

DAUB_AUTH="u1:p1,u2:p2" 起服务，断言八件事：
1. 无凭据 GET /        -> 401 + WWW-Authenticate: Basic
2. 错密码              -> 401
3. 对密码 u1           -> 200 text/html（落页正常）
4. 第二账号 u2         -> 200（多账号都通）
5. 不存在账号          -> 401（字典查找不通过）
6. 无凭据 POST /api/job -> 401（提交口不放行、不建 job）
7. 无凭据 /api/artifact/x -> 401（产物口无旁路、不泄漏存在性）
8. 不设 DAUB_AUTH 再起一个 -> GET / 200（本地原行为一字不变）

用法：pack_venv python tools/_check_auth.py
"""
import base64
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8793
URL = "http://127.0.0.1:%d" % PORT


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def req(path, auth=None, method="GET"):
    r = urllib.request.Request(URL + path, method=method)
    if auth:
        r.add_header("Authorization", "Basic " + base64.b64encode(
            auth.encode("utf-8")).decode("ascii"))
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def spawn(env_auth):
    env = dict(os.environ)
    env.pop("DAUB_AUTH", None)
    if env_auth:
        env["DAUB_AUTH"] = env_auth
    p = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "daub_web.py"),
         "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for _ in range(60):
        try:
            urllib.request.urlopen(URL + "/api/cal", timeout=1).read()
            return p
        except urllib.error.HTTPError:
            return p          # 401 也是"服务起来了"
        except Exception:
            time.sleep(0.5)
    p.terminate()
    fail("daub_web.py 起不来（端口 %d 占用？）" % PORT)


def main():
    p = spawn("boss:secret1,alice:pw2")
    try:
        code, hdr, _ = req("/")
        if code != 401 or "Basic" not in hdr.get("WWW-Authenticate", ""):
            fail("1 无凭据 GET / 期望 401+WWW-Authenticate，得 %s %s"
                 % (code, hdr.get("WWW-Authenticate")))
        code, _, _ = req("/", auth="boss:WRONG")
        if code != 401:
            fail("2 错密码期望 401，得 %s" % code)
        code, hdr, body = req("/", auth="boss:secret1")
        if code != 200 or "text/html" not in hdr.get("Content-Type", ""):
            fail("3 对密码期望 200 html，得 %s %s" % (code, hdr.get("Content-Type")))
        if b"<html" not in body[:400].lower():
            fail("3 落页内容不对（拿到的不是工作台 HTML）")
        code, _, _ = req("/api/cal", auth="alice:pw2")
        if code != 200:
            fail("4 第二账号期望 200，得 %s" % code)
        code, _, _ = req("/", auth="mallory:x")
        if code != 401:
            fail("5 不存在账号期望 401，得 %s" % code)
        code, _, _ = req("/api/job?tier=std", method="POST")
        if code != 401:
            fail("6 无凭据 POST /api/job 期望 401，得 %s" % code)
        code, _, _ = req("/api/artifact/deadbeef/x_plan.json")
        if code != 401:
            fail("7 无凭据 artifact 期望 401，得 %s" % code)
        print("auth: 401-noauth / 401-wrongpw / 200-ok / 200-second / "
              "401-unknown / 401-post / 401-artifact —— 七腿全绿")
    finally:
        p.terminate()
        p.wait(timeout=10)

    p = spawn(None)
    try:
        code, _, _ = req("/")
        if code != 200:
            fail("8 不设 DAUB_AUTH 期望 200（原行为），得 %s" % code)
        print("auth: 无 DAUB_AUTH 时本地原行为不变（GET / 200）")
    finally:
        p.terminate()
        p.wait(timeout=10)
    print("PASS: 鉴权门八腿全绿")


if __name__ == "__main__":
    main()
