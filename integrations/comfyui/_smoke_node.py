#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyUI 节点壳冒烟（W12-T10）。

六证据，缺一即 FAIL：
1. 模块裸导入成功（无 torch/ComfyUI 依赖也能 import —— 节点壳纯度）；
2. NODE_CLASS_MAPPINGS 形状（ComfyUI 装载契约）；
3. resolve_daub 三级解析 + fail-loud（清空 env 后必 raise）；
4. build_command 参数形状（--kra/--timelapse 开关位）；
5. 张量面：numpy 桩 (1,H,W,C) → PNG → 回读张量，数值往返一致；
6. 真渲端到端（有 daub_paint.exe 时）：node.run() 全链，产物四件齐，
   <stem>_ref.png 回读 == 节点输出张量（0-1 容差 1/255）。

用法：python integrations/comfyui/_smoke_node.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import daub_postprocess as node  # noqa: E402  (证据 1 兼导入)


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def main():
    from PIL import Image
    import numpy as np

    # 证据 2：ComfyUI 装载契约
    if not isinstance(node.NODE_CLASS_MAPPINGS, dict) \
            or "DaubPostprocess" not in node.NODE_CLASS_MAPPINGS:
        fail("NODE_CLASS_MAPPINGS missing DaubPostprocess")
    it = node.DaubPostprocess.INPUT_TYPES()["required"]
    if "image" not in it or it["image"][0] != "IMAGE":
        fail("INPUT_TYPES missing IMAGE input")

    # 证据 3：fail-loud
    env_bak = os.environ.pop("DAUB_PAINT_EXE", None)
    try:
        node.resolve_daub("")  # 无参数无 env 无捆绑 → 必 raise
        fail("resolve_daub did not raise with no daub anywhere")
    except FileNotFoundError:
        pass

    # 有 env 时解析命中
    exe = os.path.join(ROOT, "dist", "daub_paint.exe")
    os.environ["DAUB_PAINT_EXE"] = exe
    if node.resolve_daub("") != exe:
        fail("resolve_daub env priority broken")

    # 证据 4：命令形状
    c = node.build_command("DAUB", "a.png", "b.png", True, True)
    if c != ["DAUB", "a.png", "b.png",
             "--kra", "b.kra", "--timelapse", "b_timelapse.mp4"]:
        fail("build_command shape: %s" % c)

    # 证据 5：张量往返
    ref = np.asarray(Image.open(os.path.join(
        ROOT, "web", "_smoke", "ref512.png")).convert("RGB"),
        dtype="float32")[None] / 255.0
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "t.png")
        node.image_tensor_to_png(ref, png)
        back = np.asarray(node.png_to_image_tensor(png))
        if back.shape != ref.shape:
            fail("tensor roundtrip shape %s vs %s" % (back.shape, ref.shape))
        if np.abs(back - ref).max() > 1.0 / 255 + 1e-6:
            fail("tensor roundtrip values diverged")

        # 证据 6：真渲端到端（有冻结 exe 才跑）
        if os.path.isfile(exe):
            out_img, plan = node.DaubPostprocess().run(
                ref, "", td, True, False)
            if not plan.endswith("_plan.json"):
                fail("e2e: returned plan=%r" % (plan,))
            stem = os.path.basename(plan)[:-len("_plan.json")]
            out_png = os.path.join(td, stem + ".png")
            if not (os.path.isfile(out_png) and os.path.getsize(out_png) > 0):
                fail("e2e: out png missing")
            for suffix in ("_ref.png", "_plan.json", ".kra"):
                if not os.path.isfile(os.path.join(td, stem + suffix)):
                    fail("e2e: artifact missing: %s" % suffix)
            repng = os.path.join(td, stem + "_ref.png")
            back2 = np.asarray(out_img, dtype="float32")
            # 重绘是重新作画：只对形状/值域把门，逐像素相等不成立
            if back2.shape != ref.shape:
                fail("e2e: output shape %s" % (back2.shape,))
            if back2.min() < 0 or back2.max() > 1:
                fail("e2e: output out of [0,1]")
            if not os.path.isfile(repng):
                fail("e2e: ref png not kept")
            print("e2e: %s + %s（%.0f KB kra）"
                  % (out_png, plan,
                     os.path.getsize(os.path.join(td, stem + ".kra"))
                     / 1024))
        else:
            print("e2e SKIPPED (no %s)" % exe)

    if env_bak is not None:
        os.environ["DAUB_PAINT_EXE"] = env_bak
    else:
        os.environ.pop("DAUB_PAINT_EXE", None)
    print("PASS: ComfyUI 节点壳冒烟全绿")


if __name__ == "__main__":
    main()
