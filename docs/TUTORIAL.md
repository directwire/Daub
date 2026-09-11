# daub tutorial — from clone to painting

This walks the full path: build the renderer, render a shipped demo,
plan a painting of your own image, edit the layers, cut the reveal
video, and hand the whole atelier to an AI agent over MCP.

The mental model first, because everything below hangs off it:

- **daub (this repo) is the renderer.** It turns a *plan* — a JSON
  stroke catalog: strokes with brush, position, width, pressure — into
  a finished painting (PNG, layered `.kra`, layered `.psd`, reveal
  video). It knows nothing about images; plans in, painting out.
- **A planner is anything that emits plans.** The planner is where the
  *style* lives: one planner paints in thin ink linework, another could
  go impasto, a third pointillist — the renderer executes whatever it
  is given, identically on every machine, byte for byte.
- **The planner ecosystem is open.** The ready-made planner lives at
  [directwire/daub-planner](https://github.com/directwire/daub-planner)
  (including a reference implementation derived from the planner that
  painted this repo's gallery), and anyone can ship their own — see
  [step 7](#7-write-your-own-planner).

## 0. Platforms

Windows, Linux and macOS are all first-class, x64 and arm64:

| Platform | Status |
|---|---|
| Windows x64 | CI-built release binary on every push |
| Linux x64 / arm64 | CI-built release binary on every push |
| macOS Intel (x64) | CI-built release binary on every push |
| macOS Apple Silicon (arm64) | CI-built release binary on every push |
| Any browser | `web/wasm.html` — the same core compiled to wasm, zero install |

The result is a single static executable with no runtime dependencies
beyond the OS. There are no releases yet — grab the binary from the CI
artifact of the latest green run, or build from source (next step).

## 1. Build and first render

```bash
git clone https://github.com/directwire/daub && cd daub
cargo build --release
```

A demo plan ships in `examples/`. Render it:

```bash
target/release/daub render examples/demo_plan.json --out demo.png
```

That plan carries **8973 strokes across 10 layers** (900×724); on a
plain laptop core it renders in well under a second. Add the layered
exports and they open directly in Krita, Photoshop or Clip Studio:

```bash
target/release/daub render examples/demo_plan.json \
    --out demo.png --kra demo.kra --psd demo.psd
```

What you just rendered: `demo_reference.png` is the source image the
planner measured, `demo_plan.json` is the stroke catalog it planned,
`demo.png` is the painting. The plan header records the canvas, seed
and background; the strokes are the work.

## 2. Plan a painting of your own

Clone the planner and give it any image:

```bash
git clone https://github.com/directwire/daub-planner
cd daub-planner/reference
pip install -r requirements.txt
python stroke_engine.py plan your_image.jpg my_plan.json
```

Render it with the binary you built in step 1 (adjust the path to
where your daub clone lives):

```bash
/path/to/daub/target/release/daub render my_plan.json --out painting.png
```

Planning is deterministic — seeded, no clock — so the same image plans
the same strokes on any machine, byte for byte. You can verify that
with the shipped demo: `python stroke_engine.py plan examples/demo_reference.png
my_plan.json` reproduces `examples/demo_plan.json` exactly (8973
strokes, seed 20260904).

Or run the whole chain — plan *and* render, plus reveal video — in one
command. Point `DAUB_KRMCP_TOOLS` at the planner directory so daub's
own tooling finds it:

```bash
export DAUB_KRMCP_TOOLS=/path/to/daub-planner/reference
python tools/daub_paint.py your_image.jpg painting.png --timelapse painting.mp4
```

## 3. Edit like a painter

A plan is meant to be edited. The layer stack is the first-appearance
order of `layer` in the strokes array; typical edits:

- **mute a layer** — delete its strokes (order is never reordered);
- **re-assign a brush** — swap every stroke of a layer to another
  preset (`daub presets` lists the 26 calibrated ones; unknown names
  fail loud);
- **scale a layer's stroke sizes** — multiply widths proportionally,
  geometry untouched.

Edit the JSON by hand, or let an agent do it with the MCP
`daub_edit_plan` tool (step 5), which saves an edited *copy* and can
re-render it in the same call. Either way the validator is the same
one command: `daub render edited.json --out check.png` — it either
renders or fails loud on a contract violation.

## 4. The reveal video

`daub_paint.py --timelapse` (step 2) renders the painting stroke by
stroke into an mp4; it needs `ffmpeg` on PATH (or set `DAUB_FFMPEG`).
Through MCP, `daub_timelapse` cuts the video and `daub_frame` freezes
any moment of it as a PNG plus the truncated plan at that stroke
count.

## 5. Drive daub from an agent (MCP)

`tools/daub_mcp.py` is a hand-written stdio JSON-RPC 2.0 server — zero
third-party dependencies — exposing the atelier as **12 MCP tools**:
render, plan, inspect, edit layers, watch strokes grow, cut reveal
videos, freeze frames. Returned images are downscaled and attached as
MCP image content, so the agent genuinely *sees* the painting.

Prerequisites:

- the built binary (step 1);
- python 3.8+ with `pip install pillow numpy` (the MCP layer's image
  handling; the render engine itself is pure Rust);
- only if the agent should also *plan*: the daub-planner reference
  dependencies (`scipy`, `scikit-image`) and `DAUB_KRMCP_TOOLS`;
- `ffmpeg` on PATH for videos (optional).

Register with Claude Code (user scope):

```bash
claude mcp add -s user daub -- python /path/to/daub/tools/daub_mcp.py
```

Any MCP client that speaks stdio works the same way — the server is a
plain script; point your client at it.

**How the server finds the engine.** Resolution order: `DAUB_PAINT_EXE`
env var → `dist/daub_paint.exe` if you packaged one → the dev fallback
(the running interpreter + `tools/daub_paint.py`, which in turn uses
`target/release/daub`). On a fresh clone the dev fallback just works
after step 1 — nothing to configure.

Environment variables:

| Variable | Meaning |
|---|---|
| `DAUB_PAINT_EXE` | explicit path to the engine driver (overrides all resolution) |
| `DAUB_KRMCP_TOOLS` | planner directory — lights up the image→plan side |
| `DAUB_FFMPEG` | ffmpeg path when it is not on PATH |
| `DAUB_BIN` | the `daub` binary for the planner's own preview renderer |

A first session — paste these to your agent one at a time:

1. `daub_status` — self-check: engine found, ffmpeg present, brush
   library intact, plus a real two-stroke render smoke.
2. `daub_list_brushes` — the 26 calibrated presets (the legal brush
   domain).
3. `daub_render examples/demo_plan.json --out demo.png --kra demo.kra`
   — render the shipped demo; the image comes back into the chat.
4. `daub_edit_plan examples/demo_plan.json --mute-layer <top layer>`
   then re-render — watch the painting change.
5. `daub_plan assets/gallery_mist.webp` — the full image→painting
   chain (slow, an async job; `daub_wait` follows its progress), then
   `daub_preview_strokes` at fraction 0.3 to watch it grow.

Troubleshooting:

| Symptom | Fix |
|---|---|
| `daub_paint not found` | build (step 1) or set `DAUB_PAINT_EXE` |
| plan-side commands complain no planner | set `DAUB_KRMCP_TOOLS` (step 2) |
| timelapse/frame jobs fail | `ffmpeg` not found — PATH or `DAUB_FFMPEG` |
| unknown preset fails loud | `daub presets` is the legal domain |

## 6. Zero install: the browser build

`web/wasm.html` is the same render core compiled to wasm (~885 KB,
single file, double-click from `file://`). TruthPlayer replays the
first *k* strokes as ground truth, and KRA/PSD export works in the
browser; output is byte-identical to the native binary. `python
tools/daub_web.py` runs the local web studio around it: upload an
image, watch the painting grow live, edit layers, export everything.

## 7. Write your own planner

The renderer does not care who planned the strokes — that is the
whole point. A planner is any program that emits the plan JSON
contract ([The plan contract](../README.md#the-plan-contract) in the
README; byte-level details in
[docs/DOWNSTREAM_GUIDE.md](DOWNSTREAM_GUIDE.md)), and each planner
brings its own style: pick different brushes, different stroke
strategies, different layer logic, and the same renderer paints a
different way.

Two starting points, both validated by the same loop —
`daub render your-plan.json --out check.png`, which either renders or
fails loud:

- fork the reference planner and bend its strategy (brush pool,
  banding, layer ruler), or
- emit plans directly from your own pipeline — anything that writes
  the contract works out of the box.

Planner implementations live in
[directwire/daub-planner](https://github.com/directwire/daub-planner):
one directory per implementation with a README covering inputs,
outputs and dependencies. Pull requests welcome.
