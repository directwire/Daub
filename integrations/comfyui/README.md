# daub × ComfyUI —— 画坊重绘自定义节点

**所有生图输出都是 daub 的输入原料**，ComfyUI 是生图生态最大枢纽：
这个目录让"出图 → 画坊重绘"变成一个节点连线，不再需要抄命令。

## 安装（三步）

1. 把 `daub_postprocess/` 整个目录拷进 ComfyUI 的 `custom_nodes/`；
2. （零配置可选）把冻结的 `daub_paint.exe` 拷进
   `custom_nodes/daub_postprocess/`——与节点同目录即被自动发现；
   或者：节点参数 `daub_path` 填 exe 绝对路径，或设环境变量
   `DAUB_PAINT_EXE`。三级解析、找不到必 raise（fail-loud 不吞错）；
3. 重启 ComfyUI，出图节点后接 **"Daub Postprocess (画坊重绘)"**
   （分类 `daub/画坊`）。

## 节点面

| 输入 | 说明 |
|---|---|
| `image` | IMAGE（任意出图节点的输出，B,H,W,C 0-1） |
| `daub_path` | daub_paint.exe 路径（空=走 env/同目录捆绑） |
| `output_dir` | 产物目录，`[output]` 占位符=ComfyUI 运行目录 |
| `do_kra` | 出分层 .kra（Krita 可开，静默层自然缺席） |
| `do_timelapse` | 出逐笔生长 mp4（ffmpeg 需在 PATH） |

| 输出 | 说明 |
|---|---|
| `image` | 校准笔刷重绘画作（IMAGE，可继续接别的节点） |
| `plan_json` | 笔路计划路径（空串=异常）；喂给 web/index.html 即逐笔回放 |

节点是**纯子进程壳**：不 import daub 任何代码，不写 ComfyUI 内存，
崩了不连坐；产物命名 `<stem>.png / _plan.json / .kra / _timelapse.mp4`。

## 验证

```bash
python integrations/comfyui/_smoke_node.py
```

六证据门禁：裸导入 / NODE_CLASS_MAPPINGS 契约 / fail-loud / 命令形状 /
张量往返 / 真渲端到端（有 `dist/daub_paint.exe` 时真跑全链，
512 样例 4.1s，kra 841KB）。
