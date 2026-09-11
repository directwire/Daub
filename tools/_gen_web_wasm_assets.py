#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重生成 web/wasm 资产（T6 WASM 真渲核）。

前置（手动跑一次）：
    cargo build --release --lib --target wasm32-unknown-unknown
本脚本做三件事：
    1. wasm-bindgen --target no-modules 出 web/wasm/daub.js + daub_bg.wasm
       （no-modules = 经典脚本挂 window.wasm_bindgen，file:// 可用；
        ES module 会被 file:// CORS 拦）
    2. daub.wasm → base64 内联 web/wasm/daub_b64.js（file:// 下 fetch
       静态 .wasm 同样被拦，内联是唯一免服务器路径；452KB b64 可接受）
    3. tools/data/ink_calib.json → web/wasm/ink_calib.js（window.DAUB_CAL）

用法：python tools/_gen_web_wasm_assets.py
"""
import base64
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WASM_RAW = os.path.join(ROOT, "target", "wasm32-unknown-unknown",
                        "release", "daub.wasm")
OUT = os.path.join(ROOT, "web", "wasm")
BINDGEN = os.path.join(os.path.expanduser("~"), ".cargo", "bin",
                       "wasm-bindgen.exe")


def main():
    if not os.path.isfile(WASM_RAW):
        sys.exit("missing %s - run: cargo build --release --lib "
                 "--target wasm32-unknown-unknown" % WASM_RAW)
    os.makedirs(OUT, exist_ok=True)
    subprocess.check_call([BINDGEN, "--target", "no-modules",
                           "--out-dir", OUT, WASM_RAW])
    for stale in ("daub.d.ts", "daub_bg.wasm.d.ts"):
        p = os.path.join(OUT, stale)
        if os.path.isfile(p):
            os.remove(p)
    # 关键：b64 必须内联 bindgen 重写后的 daub_bg.wasm（imports 已从
    # __wbindgen_placeholder__ 改写到 ./daub_bg.js 键位），拿 raw 模块
    # 配 glue 会在 instantiate 时报 import #0 module is not an object。
    rewritten = os.path.join(OUT, "daub_bg.wasm")
    raw = open(rewritten, "rb").read()
    b64 = base64.b64encode(raw).decode("ascii")
    with io.open(os.path.join(OUT, "daub_b64.js"), "w",
                 encoding="utf-8", newline="") as f:
        f.write('// 自动生成：daub 渲染核 wasm 的 base64'
                '（tools/_gen_web_wasm_assets.py 重生成）。\n')
        f.write('// 内联避免 file:// 下 fetch 被 CORS 拦；%d bytes 原始'
                ' / %d b64。\n' % (len(raw), len(b64)))
        f.write('window.DAUB_WASM_B64 = "%s";\n' % b64)
    with io.open(os.path.join(HERE, "data", "ink_calib.json"),
                 encoding="utf-8") as f:
        cal = f.read()
    json.loads(cal)  # 契约自检：不是合法 JSON 就别上网页
    with io.open(os.path.join(OUT, "ink_calib.js"), "w",
                 encoding="utf-8", newline="") as f:
        f.write('// 自动生成：ink_calib.json 的 JS 包装'
                '（tools/_gen_web_wasm_assets.py 重生成）。\n')
        f.write('window.DAUB_CAL = %s;\n' % cal)
    print("web/wasm regenerated: daub.js + daub_bg.wasm + daub_b64.js "
          "(%dKB) + ink_calib.js" % (len(b64) // 1024))


if __name__ == "__main__":
    main()
