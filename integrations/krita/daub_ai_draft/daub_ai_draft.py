# -*- coding: utf-8 -*-
"""daub AI 底稿重绘 —— Krita Python 插件（单文件，可脱 Krita 单测）。

安装：本文件 + daub_ai_draft.desktop 放进 Krita 的 pykrita 目录
（设置 ▸ 管理资源 ▸ 打开资源文件夹 ▸ pykrita/）；可选拷冻结
daub_paint.exe 进同目录（或设 env DAUB_PAINT_EXE）。
重启 Krita → 设置 ▸ 配置 Krita ▸ Python 插件管理器 勾选本插件，
再重启一次 → 菜单「工具 ▸ daub 画坊：AI 底稿重绘」。

流程：选一张 AI 底稿 → daub_paint 重绘出 png/_plan.json/.kra 三件 →
分层 .kra 直接在 Krita 打开（daub 的层就是 Krita 的层，无私有格式）。

纯函数区（resolve_daub/build_command/run_daub）不碰 krita/PyQt：
无头冒烟直接 import 本文件测核心；壳体只有 Krita 存在才注册。
"""
import os
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))


def resolve_daub(daub_path=""):
    """daub_paint.exe 定位：参数 > env > 插件目录同捆绑。fail-loud。"""
    for cand in (daub_path,
                 os.environ.get("DAUB_PAINT_EXE", ""),
                 os.path.join(_HERE, "daub_paint.exe")):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        "daub_paint.exe 未找到：把冻结 exe 拷进 pykrita/daub_ai_draft/ "
        "同目录，或设环境变量 DAUB_PAINT_EXE")


def build_command(daub, ref_png, out_png, do_timelapse=False):
    """重绘命令；.kra 是本插件的落点主产物（Krita 直接开）。"""
    stem = out_png[:-4] if out_png.endswith(".png") else out_png
    cmd = [daub, ref_png, out_png, "--kra", stem + ".kra"]
    if do_timelapse:
        cmd += ["--timelapse", stem + "_timelapse.mp4"]
    return cmd


def run_daub(ref_png, out_dir=None, daub_path="", do_timelapse=False,
             log=print):
    """全链：返回 (png, kra, plan_json) 三件路径。fail-loud。"""
    daub = resolve_daub(daub_path)
    ref_png = os.path.abspath(ref_png)
    if not os.path.isfile(ref_png):
        raise FileNotFoundError("底稿不存在：%s" % ref_png)
    out_dir = os.path.abspath(out_dir or os.path.dirname(ref_png))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(ref_png))[0] + "_daub"
    out_png = os.path.join(out_dir, stem + ".png")
    kra = os.path.join(out_dir, stem + ".kra")
    plan = os.path.join(out_dir, stem + "_plan.json")
    cmd = build_command(daub, ref_png, out_png, do_timelapse)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace")
    if proc.returncode != 0 or not os.path.isfile(kra) \
            or os.path.getsize(kra) == 0:
        raise RuntimeError("daub_paint 失败 rc=%s：%s\n%s"
                           % (proc.returncode, cmd, proc.stderr[-800:]))
    log("[daub] %s -> %s" % (ref_png, kra))
    return out_png, kra, (plan if os.path.isfile(plan) else "")


try:
    from krita import Krita
    _HAS_KRITA = True
except ImportError:  # 脱 Krita 单测（无头冒烟）走这条
    _HAS_KRITA = False

if _HAS_KRITA:

    class DaubAiDraft(Krita.Extension):
        def __init__(self, parent):
            super().__init__(parent)

        def setup(self):
            pass

        def createActions(self, window):
            action = window.createAction(
                "daub_ai_draft", "daub 画坊：AI 底稿重绘", "tools")
            action.triggered.connect(self.run)

        def run(self):
            from PyQt5.QtWidgets import QFileDialog, QApplication
            from PyQt5.QtCore import Qt
            ref, _ = QFileDialog.getOpenFileName(
                None, "选一张 AI 底稿（png/jpg…）", "",
                "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
            if not ref:
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                out_png, kra, plan = run_daub(ref)
            finally:
                QApplication.restoreOverrideCursor()
            inst = Krita.instance()
            doc = inst.openDocument(kra)
            if doc and inst.activeWindow():
                inst.activeWindow().addView(doc)
            print("[daub] done:", out_png, "| plan:", plan)

    Krita.instance().addExtension(DaubAiDraft(Krita.instance()))
