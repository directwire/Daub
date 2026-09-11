# daub × Krita —— AI 底稿重绘插件

艺术家的主场里给 daub 一个按钮：选一张 AI 底稿 → 校准笔刷重绘 →
**分层 .kra 直接在 Krita 打开**（daub 的层就是 Krita 的层，无私有
格式，打开后照常深修）。

## 安装（四步）

1. 找到 pykrita 目录：Krita ▸ 设置 ▸ 管理资源 ▸ 打开资源文件夹 ▸
   `pykrita/`；
2. 把 **`daub_ai_draft.py` 和 `daub_ai_draft.desktop` 两个文件**（在
   `daub_ai_draft/` 子目录里）拷进 pykrita **根目录**——pykrita 是
   平铺模块不是包，别整文件夹拷；
3. （零配置可选）把冻结 `daub_paint.exe` 拷进 pykrita 同目录（插件
   自动发现），或设环境变量 `DAUB_PAINT_EXE`；
4. 重启 Krita → 设置 ▸ 配置 Krita ▸ Python 插件管理器 勾选
   **"daub AI 底稿重绘"** → 再重启一次 → 菜单 **工具 ▸ daub 画坊：
   AI 底稿重绘**。

## 用法

点菜单 → 选 AI 底稿图（png/jpg/webp…）→ daub 规划+渲染（512px 约
4-26s，取决于底稿复杂度）→ 完成后 .kra 自动在新标签打开。
产物三件落在底稿同目录：`<名>_daub.png / _daub.kra / _daub_plan.json`
（plan.json 喂 `web/index.html` 可逐笔回放整幅画的生长过程）。

## 验证（脱 Krita 单测）

```bash
python integrations/krita/_smoke_krita_plugin.py
```

五证据：裸导入（`_HAS_KRITA` 守卫）/ krita 桩注入验注册路径 /
fail-loud / 命令形状 / 真渲端到端（512 样例，kra 840KB 合法 zip）。
