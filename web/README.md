# daub web 回放 —— 浏览器里的"看它画"

任意一台设备的浏览器打开 `index.html`，把一张 `<stem>_plan.json` 拖进去，
就能看 daub 计划逐笔"长"出来——零依赖、零构建、`file://` 直开，手机也行。
这是 daub 作为**生图生态下游工序**的第一块对外表面：计划文件本身即作品
（数组序即 z 序，W11 统一尺子保证），回放器只负责忠实重放。

## 用法

- **直开**：双击 `index.html`（或任意静态托管），拖入 plan JSON 即玩；
  内置样例按钮（`sample_plan.js`，img1197 512px 缩样）开箱即看。
- **URL 参数**：`?sample=1` 自动载样例 · `bare=1` 只留画布（嵌入/冒烟）·
  `speed=N` 倍速 · `autoplay=1` 立即播放 · `final=1` 同步呈现终帧 ·
  `svg=1` 终帧走 SVG 表面 · `turbo=1` 定时器驱动（无头）·
  `mute=L26,L13` 初始层静音（与 chip 点按同路，可深链）。
- **控制**：播放/暂停、进度条 seek、0.5×–100× 倍速、图层 chip 点按静音
  （首现序=层栈序，静音=整层剔除不重排）。
- **导出**：`⬇ 导出 webm` —— MediaRecorder 把整场回放录成 VP9 视频，
  直接分享（冒烟已验：真 VP9 流、可解码）。

## 架构（两个文件，约 400 行）

| 文件 | 职责 |
|---|---|
| `replay.js` | `PlanPlayer`（载入/播放/seek/层静音）+ 两个表面：`drawStroke`（canvas2d，交互回放）、`renderSVG`（SVG 序列化，无头保真度采集+矢量导出）。二者共享同一套笔模数学。 |
| `index.html` | 演示页 + 无头冒烟约定（探针 `#probe` data-* 属性供 `--dump-dom` 断言）。 |

**笔模 v2（数据裁决）**：线宽 = `size × 端点压力`，透明度 = 该笔 `opacity`
常量。三个候选同 harness 实测（512 样例 vs `daub.exe` 真渲 mean|diff|）：
朴素端点 6.75 ≪ 家族参数 9.89 ≪ 朴素中点 14.61 —— 规划器已把每笔的
size/opacity 调好，任何额外"家族整形"只会帮倒忙，v1 家族表已删。

**诚实保真度**（只报数，不吹收敛）：
`_smoke/_check_replay.py` 四证据门禁全绿：
`mean|diff| = 7.66 / within60 = 99.7%`（vs daub.exe 真渲，512 样例，
入库 `web/_smoke/fidelity_baseline.json`）；canvas 与 SVG 双表面
parity mean|diff| = 0.01（用户看到的=冒烟量到的）。剩余 ~7 的差来自
daub 真渲的软边与笔尖纹理——逐像素忠实路径已通：`wasm.html`
（见下节），回放仍保留为过程表面，**不说"还原"，只报数**。

## WASM 真渲核（`wasm.html` —— 逐像素真值表面）

render.rs 同一核编译进 wasm（396KB base64 内联，**file:// 双击即用，
零服务器**），浏览器内渲出的像素与 `daub_paint.exe --render-only`
**逐像素一致：mean|diff| = 0.00**（512 样例 3784 笔，182ms，
入库 `web/_smoke/wasm_fidelity.json`）。与 JS 笔模回放互补：
回放是"过程表面"，这里是"真值表面"。

- 限制：tip 引擎（.kpp 需 fs）不进 wasm——W11 带层计划笔笔走胶囊
  校准，与 daub.exe 默认路径同路；非带层旧计划用 `--no-tips` 真值对表。
- `?plan=<url>` 远程计划插座（与 index.html 同约）：管线把 plan.json
  扔进静态目录即接完；**托管方要么与页面同源，要么带
  `Access-Control-Allow-Origin` 头**（CORS，file:// 页面跨源 fetch
  必需，无头冒烟已实证）。
- **拖拽**：与 index.html 同约，整页投放任意 `<stem>_plan.json` 即真渲。
  无头验证走 `?droptest=<url>`（页面 fetch 计划文本后合成
  File+DataTransfer 派发真 `DragEvent("drop")`，与真人拖拽同一条
  handler 链，只有 OS 级传输是模拟的）——冒烟第五证据腿，px 与内置
  样例逐像素对表。
- 资产再生成：`cargo build --release --lib --target wasm32-unknown-unknown`
  然后 `python tools/_gen_web_wasm_assets.py`。
- 门禁：`python web/_smoke/_check_wasm.py`（五证据：自证词 + 尺寸对账 +
  截图 vs 真值 ≈0 + `?plan=` 插座腿 px 对表 + `?droptest=` 拖拽腿
  px 对表；>2 即 FAIL——这是"找回归"的尺子，不是近似口径）。
  冷启动首跑偶发 fetch 竞态 FAIL，重跑即绿（虚拟时间已知脾气）。
- 无头坑（wasm 特有两条，防重蹈）：b64 必须内联 bindgen **重写后**的
  `daub_bg.wasm`（拿 raw 模块配 glue 会报 import #0 not an object）；
  0.2.128 no-modules 的 `init()` 返回 **raw exports**，包装导出挂在
  `wasm_bindgen` 自身——拿 init 返回值直接调函数会拿到 multi-value
  Array 而不是包装对象。

## 无头冒烟（四证据，防"看着像跑了"）

```bash
pack_venv/Scripts/python.exe web/_smoke/_check_replay.py
```

1. **探针**：`--dump-dom` 断言 `data-done="1"`（回放真放完）；
2. **画了东西**：终帧 vs 纯 bg mean|diff| ≫ 0；
3. **保真度**：SVG 光栅终帧 vs `daub.exe` 真渲（truth）量化入库；
4. **webm 导出**：无头 MediaRecorder 导出 → base64 取回 → ffmpeg 验
   VP9 流可解码。

无头三坑（已在失败台账）：headless 的 canvas2d 大计划软光栅腐败
（>~500 笔显示黑、读回零，六旗标无解）→ 保真度走 `renderSVG` 光栅；
`--dump-dom` 与 `--screenshot` 在 virtual-time 下行为分叉；截图路径
必须绝对路径。canvas/SVG parity 0.01 保证换表面不换画。

## 重新生成样例

```bash
# 512 样例计划（dev 树规划）
pack_venv/Scripts/python.exe tools/daub_paint.py web/_smoke/ref512.png web/_smoke/build/sample512.png
# plan JSON → sample_plan.js（冒烟在样例缺失时也会自动生成）
```
