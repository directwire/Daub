#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Krita 插件冒烟（W12-T8）——脱 Krita 单测。

五证据，缺一即 FAIL：
1. 插件模块裸导入成功（无 krita 环境 → _HAS_KRITA=False 守卫生效）；
2. 注册路径：注入 krita 桩再 import，addExtension 被调、菜单动作名对；
3. resolve_daub fail-loud（清 env 无捆绑必 raise）+ env 优先级；
4. build_command 形状（--kra 带路径主产物 + timelapse 开关）；
5. 真渲端到端（有 dist/daub_paint.exe 时）：run_daub 全链，
   png/kra/plan 三件齐、非空，.kra 是合法 zip（Krita 打开的本质）。

用法：python integrations/krita/_smoke_krita_plugin.py
"""
import importlib
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def load_plugin(stub_krita=False):
    """(重)导入插件模块；stub_krita=True 时注入 krita 桩验注册路径。
    用 spec_from_file_location 直载 .py： integrations/krita/ 下同名
    文件夹会被当 namespace package 遮住真模块（实测踩过）。
    """
    sys.modules.pop("daub_ai_draft", None)
    saved = sys.modules.get("krita")
    if stub_krita:
        class _FakeExt:
            def __init__(self, parent):
                self.parent = parent

        class _FakeKrita:
            registered = []

            @staticmethod
            def instance():
                return _FakeKrita

            Extension = _FakeExt

            @classmethod
            def addExtension(cls, ext):
                cls.registered.append(ext)

        _FakeKrita.Krita = _FakeKrita  # from krita import Krita 落点
        sys.modules["krita"] = _FakeKrita
    elif saved is None:
        sys.modules.pop("krita", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "daub_ai_draft",
            os.path.join(HERE, "daub_ai_draft", "daub_ai_draft.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["daub_ai_draft"] = mod
        spec.loader.exec_module(mod)
        return mod, sys.modules.get("krita")
    finally:
        if saved is not None:
            sys.modules["krita"] = saved
        elif stub_krita:
            sys.modules.pop("krita", None)


def main():
    # 证据 1：裸导入（本环境无 krita）
    mod, _ = load_plugin(stub_krita=False)
    if getattr(mod, "_HAS_KRITA", True):
        fail("bare import unexpectedly has krita")

    # 证据 2：注入 krita 桩，注册路径走通
    mod2, stub = load_plugin(stub_krita=True)
    if not getattr(mod2, "_HAS_KRITA", False):
        fail("stub krita not picked up")
    if not stub.registered or not isinstance(stub.registered[0], mod2.DaubAiDraft):
        fail("addExtension not called with DaubAiDraft")
    # 桌面清单：Krita 插件管理器靠它列插件
    desktop = io.open(os.path.join(HERE, "daub_ai_draft", "daub_ai_draft.desktop"),
                      encoding="utf-8").read()
    for key in ("ServiceTypes=Krita/PythonPlugin", "X-KDE-Library=daub_ai_draft"):
        if key not in desktop:
            fail("desktop missing %s" % key)

    # 证据 3：resolve fail-loud + env 优先
    env_bak = os.environ.pop("DAUB_PAINT_EXE", None)
    try:
        mod.resolve_daub("")
        fail("resolve_daub did not raise")
    except FileNotFoundError:
        pass
    exe = os.path.join(ROOT, "dist", "daub_paint.exe")
    os.environ["DAUB_PAINT_EXE"] = exe
    if mod.resolve_daub("") != exe:
        fail("resolve_daub env priority broken")

    # 证据 4：命令形状
    c = mod.build_command("DAUB", "a.png", "b.png", True)
    if c != ["DAUB", "a.png", "b.png", "--kra", "b.kra",
             "--timelapse", "b_timelapse.mp4"]:
        fail("build_command shape: %s" % c)

    # 证据 5：真渲端到端
    if os.path.isfile(exe):
        import tempfile
        import shutil
        td = tempfile.mkdtemp()
        ref = os.path.join(td, "draft.png")
        shutil.copy(os.path.join(ROOT, "web", "_smoke", "ref512.png"), ref)
        png, kra, plan = mod.run_daub(ref, td)
        for p in (png, kra, plan):
            if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                fail("e2e artifact missing/empty: %s" % p)
        if not zipfile.is_zipfile(kra):
            fail("kra is not a zip (Krita can't open)")
        print("e2e: %s + kra %dKB + %s"
              % (os.path.basename(png), os.path.getsize(kra) // 1024,
                 os.path.basename(plan)))
    else:
        print("e2e SKIPPED (no %s)" % exe)

    if env_bak is not None:
        os.environ["DAUB_PAINT_EXE"] = env_bak
    else:
        os.environ.pop("DAUB_PAINT_EXE", None)
    print("PASS: Krita 插件冒烟全绿")


if __name__ == "__main__":
    main()
