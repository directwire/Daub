# -*- coding: utf-8 -*-
"""daub 后处理节点（ComfyUI 自定义节点）——"所有生图输出都是 daub 的
输入原料"落进生图生态最大枢纽。

安装：把本目录（daub_postprocess/）整个拷进 ComfyUI/custom_nodes/，
重启 ComfyUI。出图节点后接 "Daub Postprocess (画坊重绘)"：

    IMAGE 出图 ──> [Daub Postprocess] ──> IMAGE（校准笔刷重绘画作）
                          │  副产物落在 output_dir/：
                          │  <stem>.png 成品 / <stem>_plan.json 笔路计划
                          │  <stem>.kra 分层工程（勾选）
                          └─ <stem>_timelapse.mp4 逐笔生长视频（勾选）

daub 路径解析顺序：节点参数 daub_path > 环境变量 DAUB_PAINT_EXE >
ComfyUI/custom_nodes/daub_postprocess/daub_paint.exe（把冻结 exe 拷进
节点目录即零配置）。找不到时节点 raise——fail-loud，不静默跳过。

本节点是纯子进程壳：不 import daub 任何代码，ComfyUI 崩了不连坐。
"""
import os
import subprocess
import time

_NODE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_daub(daub_path=""):
    """daub_paint.exe 定位：参数 > env > 节点目录同捆绑。fail-loud。"""
    for cand in (daub_path,
                 os.environ.get("DAUB_PAINT_EXE", ""),
                 os.path.join(_NODE_DIR, "daub_paint.exe")):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        "daub_paint.exe 未找到：设节点 daub_path、环境变量 DAUB_PAINT_EXE，"
        "或把冻结 exe 拷进 custom_nodes/daub_postprocess/")


def image_tensor_to_png(image, png_path):
    """ComfyUI IMAGE（B,H,W,C，torch 0-1）→ PNG 落盘，返回 png_path。
    只用 duck-typing（.cpu/.numpy 不区分 torch/numpy），节点壳零重依赖。
    """
    import numpy as np
    from PIL import Image
    arr = image
    if hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 4:  # (B,H,W,C) 取首张；ComfyUI IMAGE 恒为 4 维
        arr = arr[0]
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).round().astype("uint8")
    Image.fromarray(arr, mode="RGB").save(png_path)
    return png_path


def png_to_image_tensor(png_path):
    """PNG → ComfyUI IMAGE 张量形状（1,H,W,C float32 0-1）。"""
    import numpy as np
    from PIL import Image
    arr = np.asarray(Image.open(png_path).convert("RGB"),
                     dtype="float32") / 255.0
    try:
        import torch
        return torch.from_numpy(arr)[None]
    except ImportError:  # 脱离 ComfyUI 单测时给 numpy 退化面
        return arr[None]


def build_command(daub, ref_png, out_png, do_kra, do_timelapse):
    """命令构造单独成函数：无头冒烟不跑真渲也能锁死参数形状。
    --kra/--timelapse 都是带路径参数：产物路径从 out_png 词干派生，
    与 daub_paint 的 <stem>_plan.json 命名规则对齐。
    """
    stem = out_png[:-4] if out_png.endswith(".png") else out_png
    cmd = [daub, ref_png, out_png]
    if do_kra:
        cmd += ["--kra", stem + ".kra"]
    if do_timelapse:
        cmd += ["--timelapse", stem + "_timelapse.mp4"]
    return cmd


class DaubPostprocess:
    CATEGORY = "daub/画坊"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "daub_path": ("STRING", {"default": ""}),
                "output_dir": ("STRING",
                               {"default": "[output]/daub"}),
                "do_kra": ("BOOLEAN", {"default": True}),
                "do_timelapse": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "plan_json")
    FUNCTION = "run"

    def run(self, image, daub_path, output_dir, do_kra, do_timelapse):
        daub = resolve_daub(daub_path)
        out_dir = output_dir.replace("[output]", os.getcwd())
        os.makedirs(out_dir, exist_ok=True)
        stem = "daub_" + time.strftime("%Y%m%d_%H%M%S")
        ref_png = os.path.join(out_dir, stem + "_ref.png")
        out_png = os.path.join(out_dir, stem + ".png")
        plan_json = os.path.join(out_dir, stem + "_plan.json")
        image_tensor_to_png(image, ref_png)
        cmd = build_command(daub, ref_png, out_png, do_kra, do_timelapse)
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace")
        if proc.returncode != 0 or not os.path.isfile(out_png) \
                or os.path.getsize(out_png) == 0:
            raise RuntimeError("daub_paint 失败 rc=%s：%s\n%s"
                               % (proc.returncode, cmd, proc.stderr[-800:]))
        print("[daub] %s -> %s（%.1fs）"
              % (ref_png, out_png, time.time() - t0))
        if not os.path.isfile(plan_json):
            plan_json = ""
        return (png_to_image_tensor(out_png), plan_json)


NODE_CLASS_MAPPINGS = {"DaubPostprocess": DaubPostprocess}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DaubPostprocess": "Daub Postprocess (画坊重绘)",
}
