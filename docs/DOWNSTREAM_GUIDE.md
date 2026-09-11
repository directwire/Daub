# daub 下游工序集成指南 —— 把任何生图输出变成"被画出来的作品"

> 北极星：**所有生图输出都是 daub 的输入原料。** 生成模型负责"产出图像"，
> daub 负责"把它作为作品重新画一遍"——过程（逐笔 timelapse）、笔触
> （校准笔刷沉积）、分层（.kra/.psd 可进 Krita/Photoshop 深修）、分享
> （浏览器回放/webm）。这四样是生成模型自己给不了的。

## 0. 一分钟总览

```
任意生图输出 (GPT-Image / Midjourney / SD / 照片 / 插画)
        │  一张普通图片文件（png/jpg/webp，≥512px 越大越细）
        ▼
daub_paint.exe <ref> <out.png>            # 规划+渲染一键链（唯一的黑盒进程）
        ├─ <stem>.png              成品（校准笔刷重绘的画作）
        ├─ <stem>_plan.json        过程本体：笔路计划（见 §2 契约）
        ├─ <stem>.kra（--kra）      分层 Krita 工程文件
        ├─ <stem>.psd（--psd）      分层 Photoshop 工程文件（PS/优动漫/Affinity 直开）
        └─ <stem>_timelapse.mp4（--timelapse） 逐笔生长视频
        ▼
web/index.html?plan=<plan.json 的 URL>    # 浏览器逐笔回放 + webm 导出（零依赖静态页）
```

daub 是**纯文件进、纯文件出**的进程：不碰网、不碰注册表、不需要 GPU、
不需要 Krita/Python 运行时。任何能调子进程的语言/工作流引擎都能接。

## 1. 三种接入深度

| 深度 | 接口 | 适合 |
|---|---|---|
| **A. 一条命令** | `daub_paint.exe <ref> <out.png> [--kra] [--psd] [--timelapse]` | 批量管线、n8n/ComfyUI 后处理节点、RPA |
| **B. 数据契约** | 直接产/改 `<stem>_plan.json`（§2） | 想编程控制笔路：补笔、局部重画、层操作 |
| **C. 全程 API** | MCP 11 工具（`daub_plan/daub_render/daub_refine/daub_edit_plan/daub_timelapse/…`） | Agent 工作流（Claude 等）直接操纵画坊 |

### A. 命令行（最薄插座）

```bash
# 全链：图进 → 四件套出（png + plan.json；加开关出 kra/psd/mp4）
dist\daub_paint.exe input.png out.png --kra out.kra --psd out.psd --timelapse out.mp4

# 只渲染既有计划（亚秒级；GUI 工作台/外部编辑计划后的重渲全走它）
dist\daub_paint.exe --render-only edited_plan.json final.png --kra final.kra --psd final.psd

# 局部修正循环：区域内擦除重画，真渲评分收敛才停（详见 refine 报告 json）
dist\daub_paint.exe plan.json fix.png --refine --regions "x0,y0,x1,y1;x0,y0,x1,y1"
```

> **ComfyUI 用户免抄命令**：`integrations/comfyui/daub_postprocess/`
> 拷进 `custom_nodes/` 即得"画坊重绘"节点（详见该目录 README），
> 出图节点后一连线，png/plan/kra/timelapse 四件齐落。
>
> **Krita 用户**：`integrations/krita/daub_ai_draft/` 两个文件拷进
> pykrita 根目录，工具菜单多一个「daub 画坊：AI 底稿重绘」——
> 选底稿、重绘、分层 .kra 自动开新标签（详见该目录 README）。

耗时参考：规划 ~26s/张（512px，单线程大头在 band 规划），渲染亚秒，
timelapse 由帧数决定。批量时按 CPU 核数起多进程即可（无共享状态）。

### B. plan JSON 契约（想编程控笔读这节）

```jsonc
{
  "reference": "输入图路径",       // 头字段：原样保留（编辑副本必须带头）
  "canvas": [512, 512],
  "seed": 20260904,
  "bg": [134, 129, 120],          // 底色 rgb
  "detail_rois": [[x0,y0,x1,y1]], // 细化区
  "count": 3784,
  "strokes": [
    {
      "layer": "L26",             // 带=名义带宽；首现序=层栈序
      "preset": "b) Basic-2 Opacity",  // 必须在 ink_calib 7 支白名单内
      "size": 57.1,               // 笔径 px
      "opacity": 0.42,            // 0-1
      "color": "#aeaaa0",         // 十六进制串（契约！不是数组）
      "points": [[x, y, pressure], ...]  // 压力 0-1；数组序即绘制序
    }
  ]
}
```

**z 序铁律（W11 统一尺子）**：数组序=绘制序=F1 床在最前、其余全局宽→窄。
自己插笔请追加到数组尾（同层组尾），**不要重排**——回放器、timelapse、
refine 全都把数组序当真值。

计划三写法（都验证过）：整文件替换 `strokes`+`count`（头原样保留）；
`daub_refine` 的 erase/topup/groundfill（数据域无晕无伤）；
`daub_edit_plan` 的 mute/换笔/prune（MCP 层）。

### C. MCP（Agent 直连）

`daub_mcp.py` 11 工具：`daub_status / daub_list_brushes / daub_plan /
daub_render / daub_preview_strokes / daub_edit_plan / daub_refine /
daub_timelapse / daub_wait / daub_inspect_plan / daub_cancel`。
Claude 会话注册即可用；`daub_plan` 出 `job_id`，`daub_wait` 收货。

## 2. web 回放（给下游用户的"看作品长出来"表面）

`web/index.html` 纯静态零构建：

- 拖拽任意 plan JSON 即玩；`?plan=<url>` 直接吃 http(s) 静态服务的计划
  （生图管线把 plan.json 扔进对象存储/静态目录就算接完）；
- `?final=1&svg=1` 同步终帧（无头截图用）；`?bare=1` 嵌入自己产品页；
- 内置 webm 导出（VP9，已验流）；
- **真值表面 `wasm.html`**：render.rs 同核编进 wasm（396KB 内联，
  file:// 零服务器），浏览器内逐像素复现 `daub.exe` 渲染
  （实测 mean|diff| = 0.00，3784 笔 182ms）——下游页面要"图与桌面
  渲染完全一致"就用它，要"看画长出来"就用回放。

**保真度口径（复述纪律）**：web 回放笔模 vs `daub.exe` 真渲
`mean|diff| = 7.66 / within60 = 99.7%`（512 样例实测入库）；
`wasm.html` = 0.00 逐像素。**真值永远是 daub 同核**（桌面 exe 或
wasm.html 皆可），回放是过程表面不是真值表面。

## 3. 端到端配方（可直接抄）

**ComfyUI / SD 批管后处理**：出图节点后挂"执行命令"节点：
`dist\daub_paint.exe [comfyui输出].png D:\out\[name].png --kra --psd --timelapse`
—— 出图即得过程视频+双格式分层工程。

**Agent 内容流水线**：生图 API → 落盘 → MCP `daub_plan` → `daub_refine`
（人脸区补两轮）→ `daub_timelapse` → webm/plan.json 一起发发布管道。

**人肉深修**：`--kra` 出文件 → Krita 打开（分层真笔刷）→ 改完导出。
daub 的层就是 Krita 的层，没有私有格式。PS/优动漫 用户走 `--psd`：
同一份层栈（paper 裤 + 各层自底向上、全 100% normal 可见）写成标准
Photoshop 分层格式，字节级决定论（同计划两次渲染逐字节相同）；层像素
是 straight-alpha，与 daub 交付 PNG 的合成方程在软边处天然差最多
12/255（PS 里观感略亮半档，两套方程各自的忠实——`_check_psd.py`
头注有全案）。

## 4. 诚实边界（接前必读）

- 规划 26s/张（512px）是现状，交互级提速在 backlog（T9）；批量并发可先顶。
- web 回放笔模是近似（7.66/99.7%），真值永远以 `daub.exe` 渲染为准。
- 冻结 exe 为 Windows x64；Linux/mac 需源码构建（纯 Rust 引擎可交叉编译，
  daub_paint 的规划器依赖 Python 打包）。
- 7 支笔白名单（ink_calib）：换笔请走 `daub_edit_plan` 的 layer_pens，
  不要手改 preset 字段为白名单外的值（引擎 fail-loud）。
- .psd 为 8-bit RGB v1（Krita 兼容上限）：画布 1–30000px、层名 ASCII
  ≤255 字符、层数 ≤32767，超界 fail-loud 不静默截断。
