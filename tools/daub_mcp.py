"""daub_mcp: MCP stdio server - the atelier, drivable by AI.

Exposes the full daub chain (and the GUI workbench's editing powers) as
MCP tools, so Claude Code / other MCP clients can paint the way a human
would at the atelier: plan a reference, watch it grow, mute layers,
re-assign brushes, export PNG / layered .kra / timelapse video.

Zero third-party dependencies: the MCP stdio transport is just
newline-delimited JSON-RPC 2.0, hand-rolled here so the server runs on
the frozen pack_venv interpreter as-is.  Protocol traffic goes to
stdout; all diagnostics go to stderr.

Run (any cwd):
  pack_venv/Scripts/python.exe tools/daub_mcp.py

Register (Claude Code, user scope):
  claude mcp add -s user daub -- python /path/to/daub/tools/daub_mcp.py

Tools:
  daub_status          - engine / ffmpeg / brush-lib self-check
  daub_list_brushes    - the 26 calibrated presets (the legal pen domain)
  daub_inspect_plan    - plan structure: header + per-layer census
  daub_plan            - reference -> plan + painting (slow, async job)
  daub_render          - plan -> PNG [+ .kra] [+ .psd] as-is (sub-second)
  daub_edit_plan       - workbench: mute layers / re-assign pens, then
                         save a NEW plan copy (never overwrites) and
                         optionally render the result
  daub_preview_strokes - live-growth: render the first `fraction` of a
                         plan (the GUI's partial-dump mechanism)
  daub_timelapse       - plan -> reveal video (slow, async job)
  daub_frame           - one timelapse moment -> png / cut mp4 /
                         layered kra/psd / truncated plan (async job)
  daub_wait            - await an async job (emits progress)
  daub_cancel          - cancel a running job

Discipline mirrored from the GUI (hard-won truths):
  - layer order = first appearance in `strokes`; muting removes those
    strokes and never reorders the rest;
  - header keys are copied verbatim, only strokes+count change;
  - `--render-only` on an edited copy is the one true render path;
  - pens outside the ink_calib table fail loud at the engine, so
    daub_edit_plan validates against the same table first.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from preset_names import zh  # Chinese display names (same tools/ dir)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# dev fallback engine: the project venv when it exists, else whatever
# python is running this server (the Linux box's layout)
if os.name == "nt":
    PY = os.path.join(ROOT, "pack_venv", "Scripts", "python.exe")
else:
    PY = os.path.join(ROOT, "pack_venv", "bin", "python")
if not os.path.isfile(PY):
    PY = sys.executable
CAL = os.path.join(HERE, "data", "ink_calib.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # silent spawn

SERVER_INFO = {"name": "daub", "version": "0.1.0"}
PROTOCOL = "2025-06-18"

# ---------------------------------------------------------------- stderr log


def log(*a):
    print("[daub_mcp]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------- engine paths

_ENGINE = None


def resolve_engine():
    """daub_paint invocation prefix, or raise.  Order: DAUB_PAINT_EXE
    env > dist exe > dev fallback (pack_venv python -u tools/...)."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    cand = os.environ.get("DAUB_PAINT_EXE")
    if cand and os.path.isfile(cand):
        _ENGINE = [cand]
    elif os.path.isfile(os.path.join(ROOT, "dist", "daub_paint.exe")):
        _ENGINE = [os.path.join(ROOT, "dist", "daub_paint.exe")]
    elif os.path.isfile(PY) and os.path.isfile(
            os.path.join(HERE, "daub_paint.py")):
        _ENGINE = [PY, "-u", os.path.join(HERE, "daub_paint.py")]
    else:
        raise RuntimeError(
            "daub_paint not found: build it (tools/build_paint.py) or set "
            "DAUB_PAINT_EXE")
    log("engine:", " ".join(_ENGINE))
    return _ENGINE


_FFMPEG = None


def resolve_ffmpeg():
    """ffmpeg path or None.  The MCP host process inherits the AI
    client's PATH, which is often stale - mirror the GUI's escalation:
    env > which > repo copies > registry PATH (Windows only)."""
    global _FFMPEG
    if _FFMPEG is not None:
        return _FFMPEG
    try:
        import winreg
    except ImportError:                     # not Windows
        winreg = None
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ff = os.environ.get("DAUB_FFMPEG")
    if not (ff and os.path.isfile(ff)):
        ff = shutil.which("ffmpeg")
    if not ff:
        for cand in (os.path.join(ROOT, "dist", exe),
                     os.path.join(ROOT, "vendor", "ffmpeg", exe)):
            if os.path.isfile(cand):
                ff = cand
                break
    if not ff and winreg is not None:
        hives = [(winreg.HKEY_CURRENT_USER, r"Environment"),
                 (winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control"
                  r"\Session Manager\Environment")]
        for hive, sub in hives:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    raw, _typ = winreg.QueryValueEx(k, "Path")
            except OSError:
                continue
            for d in os.path.expandvars(raw).split(";"):
                d = d.strip().strip('"')
                if d and os.path.isfile(os.path.join(d, "ffmpeg.exe")):
                    ff = os.path.join(d, "ffmpeg.exe")
                    break
            if ff:
                break
    _FFMPEG = ff
    if ff:
        log("ffmpeg:", ff)
    return ff


def engine_env():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    ff = resolve_ffmpeg()
    if ff:
        env["DAUB_FFMPEG"] = ff
    return env


def spawn_engine(args, on_line):
    """Popen the engine, one background thread drains merged stdout
    line by line into on_line.  Returns the Popen."""
    argv = resolve_engine() + list(args)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=ROOT,
                         env=engine_env(), creationflags=CREATE_NO_WINDOW)

    def drain():
        for raw in p.stdout:
            on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
        p.stdout.close()

    threading.Thread(target=drain, daemon=True).start()
    return p


# ---------------------------------------------------------------- brush lib

def brush_table():
    with open(CAL, encoding="utf-8") as fh:
        cal = json.load(fh)
    return sorted(cal["presets"].keys())


BRUSH_NOTES = {
    "b) Basic-2 Opacity": "workhorse flat round; the planner's volume "
                          "brush - most coverage strokes live here",
    "b) Basic-5 Size": "soft size-varied round; mid-tone blocking",
    "d) Ink-2 Fineliner": "thin technical liner; hairlines and echoes",
    "d) Ink-3 Gpen": "sharp G-pen; confident dark linework, main "
                     "silhouette ink",
    "d) Ink-8 Sumi-e": "ink-wash brush; broad tone, calligraphic edges",
    "b) Airbrush Soft": "soft spray; gentle gradients / atmosphere",
    "d) Ink-1 Precision": "1px-class precision liner; the finest detail",
    # ---- 2026-09-07 pool expansion (real-Krita calibrated) ----
    "Ink ballpen": "ballpoint; ink only under pressure, scratchy "
                   "breaks at low pressure - sketchy hatching",
    "Ink circle 10": "technical round pen; even clean line, drafting",
    "Fill circle": "flat filler; full-width solid coverage, no taper",
    "Basic tip soft": "soft-edged round; gentler than Basic-2",
    "Airbrush pressure": "pressure airbrush; whisper-soft shading",
    "c) Pencil-1 Hard": "hard HB graphite; light grainy grey line",
    "c) Pencil-3 Large 4B": "soft 4B pencil; darker sketchy graphite",
    "c) Pencil-5 Tilted": "tilted flat pencil; narrow directional "
                          "streaks (draw across the grain)",
    "h) Charcoal Pencil Medium": "charcoal; rough dark expressive line",
    "e) Marker Chisel Smooth": "chisel marker; narrow along the stroke "
                               "direction, design-liner feel",
    "e) Marker Dry": "dry marker; broken streaky passes",
    "f) Bristles-1 Details": "detail bristles; fine multi-hair texture",
    "f) Bristles-3 Large Smooth": "large smooth bristle block; broad "
                                  "painterly coverage",
    "i) Wet Paint": "wet paint; semi-opaque rich strokes",
    "j) Watercolor Fringe": "watercolor with edge fringing; transparent "
                            "washes",
    "g) Dry Brushing": "dry brush; strong broken flying-white texture",
    "g) Dry Bristles": "dry bristle block; coarse toothy texture",
    "h) Chalk Soft": "soft chalk; powdy rounded coverage",
    "h) Chalk Grainy": "grainy chalk; toothy powdy texture",
}


# ------------------------------------------------------------------- jobs

class Job:
    def __init__(self, jid, label):
        self.id = jid
        self.label = label
        self.proc = None
        self.rc = None
        self.lines = []
        self.artifacts = {}
        self.done = threading.Event()

    def state(self):
        if self.proc is not None and self.rc is None \
                and self.proc.poll() is None:
            return "running"
        if self.rc is None:
            return "missing" if self.done.is_set() else "running"
        return "done" if self.rc == 0 else "failed(rc=%s)" % self.rc

    def tail(self, n=15):
        return self.lines[-n:]


JOBS = {}
_JOBS_LOCK = threading.Lock()


def new_job(label):
    jid = "job-%04d" % (len(JOBS) + 1)
    j = Job(jid, label)
    with _JOBS_LOCK:
        JOBS[jid] = j
    return j


def _watch(job):
    def on_line(s):
        job.lines.append(s)
        if len(job.lines) % 25 == 0:
            log(job.id, job.label, "…", s)
    code = job.proc.wait()
    job.rc = code
    job.done.set()
    log(job.id, job.label, "-> rc", code,
        "(%d lines)" % len(job.lines))


def start_job(label, args, artifacts=()):
    job = new_job(label)
    holder = {}

    def on_line(s):
        job.lines.append(s)
        if len(job.lines) % 50 == 0:
            log(job.id, job.label, "…", s)

    job.proc = spawn_engine(args, on_line)
    # the path IS the artifact; daub_wait gates each with [-s]
    for a in artifacts:
        job.artifacts[a] = a
    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    return job


def start_fn_job(label, fn, artifacts=(), lines=None):
    """Job that runs a Python callable instead of an engine spawn
    (render_timelapse.run in-process for the parameterized path).
    `lines` replaces the job's log list before the thread starts."""
    job = new_job(label)
    if lines is not None:
        job.lines = lines
    for a in artifacts:
        job.artifacts[a] = a

    def wrap():
        try:
            job.lines.append("fn job: running in-process")
            rc = fn() or 0
        except Exception as e:
            job.lines.append("%s: %s" % (type(e).__name__, e))
            rc = 1
        job.rc = rc
        job.done.set()
        log(job.id, job.label, "-> rc", rc)

    threading.Thread(target=wrap, daemon=True).start()
    return job


# ------------------------------------------------------- plan JSON helpers

def load_plan(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc.get("strokes"), list):
        raise ValueError("not a daub plan: %s" % path)
    return doc


def plan_layers(doc):
    """Per-layer census in first-appearance order (the GUI's law)."""
    layers = []
    for s in doc["strokes"]:
        if s.get("layer") not in [L["name"] for L in layers]:
            layers.append({"name": s.get("layer"), "strokes": 0,
                           "presets": {}, "sizes": []})
        L = next(l for l in layers if l["name"] == s.get("layer"))
        L["strokes"] += 1
        L["presets"][s.get("preset")] = \
            L["presets"].get(s.get("preset"), 0) + 1
        sz = s.get("size")
        if isinstance(sz, (int, float)):
            L["sizes"].append(sz)
    out = []
    for i, L in enumerate(layers):
        sz = L["sizes"]
        out.append({
            "index": i,
            "name": L["name"],
            "strokes": L["strokes"],
            "share": round(L["strokes"] / max(1, len(doc["strokes"])), 3),
            "presets": L["presets"],
            "size_min": min(sz) if sz else None,
            "size_max": max(sz) if sz else None,
        })
    return out


def edit_doc(doc, mute_layers=(), layer_pens=None, prune_regions=None,
             prune_layers=None, layer_sizes=None):
    """Workbench edit on a deep-copied doc: drop muted layers' strokes,
    overwrite the survivors' preset per layer, scale the survivors'
    stroke sizes per layer (layer_sizes: {layer: multiplier} - the
    per-layer size ruler; one multiplication rides the whole pressure
    width profile proportionally, geometry untouched), prune strokes
    anchored inside region boxes (the live trio's
    max-pressure anchor rule, data-domain), fix count, keep every
    header key verbatim, never reorder.  Returns (new_doc, notes)."""
    import copy
    mute = set(mute_layers or ())
    pens = dict(layer_pens or {})
    doc = copy.deepcopy(doc)
    before = len(doc["strokes"])
    known = {s.get("layer") for s in doc["strokes"]}
    kept, dropped, repainted = [], 0, 0
    for s in doc["strokes"]:
        if s.get("layer") in mute:
            dropped += 1
            continue
        pen = pens.get(s.get("layer"))
        if pen is not None and pen != s.get("preset"):
            s["preset"] = pen
            repainted += 1
        kept.append(s)
    sizes = {}
    if layer_sizes:
        import render_timelapse as rtl
        sizes = rtl.norm_size_scales(layer_sizes, known)
        kept, resized = rtl.apply_size_scales(kept, sizes)
    else:
        resized = 0
    pruned = 0
    if prune_regions:
        import daub_refine
        boxes = daub_refine.parse_regions(prune_regions, doc["canvas"])
        offenders = daub_refine.anchor_offenders(kept, boxes, prune_layers)
        doomed = {id(s) for s in offenders}
        kept = [s for s in kept if id(s) not in doomed]
        pruned = len(doomed)
    doc["strokes"] = kept
    doc["count"] = len(kept)
    notes = {"strokes_before": before, "strokes_after": len(kept),
             "dropped": dropped, "repainted": repainted, "resized": resized,
             "size_scales": sizes, "pruned": pruned,
             "layers_now": [L["name"] for L in plan_layers(doc)]}
    return doc, notes


def prefix_doc(doc, fraction):
    """Live-growth prefix: verbatim header, first k strokes, count=k."""
    k = max(1, min(len(doc["strokes"]),
                   int(round(len(doc["strokes"]) * fraction))))
    import copy
    d = copy.deepcopy(doc)
    d["strokes"] = d["strokes"][:k]
    d["count"] = k
    return d, k


# ------------------------------------------------------------ image output

def image_block(png_path, max_side=1024):
    """PNG -> MCP image content (downsampled) or None."""
    try:
        from PIL import Image
        im = Image.open(png_path)
        im.load()
        if max(im.size) > max_side:
            r = max_side / max(im.size)
            im = im.resize((max(1, round(im.width * r)),
                            max(1, round(im.height * r))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG", optimize=True)
        return {"type": "image", "data":
                base64.b64encode(buf.getvalue()).decode("ascii"),
                "mimeType": "image/png"}
    except Exception as e:  # no PIL / unreadable - ship the path only
        log("image_block skipped:", e)
        return None


def render_args(plan_path, out_png, kra=None, psd=None):
    args = ["--render-only", plan_path, out_png]
    if kra:
        args += ["--kra", kra]
    if psd:
        args += ["--psd", psd]
    return args


def run_sync(args, timeout=300):
    """Small engine runs (render / preview): blocking, collects lines."""
    argv = resolve_engine() + list(args)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=ROOT,
                         env=engine_env(), creationflags=CREATE_NO_WINDOW)
    lines = []
    for raw in p.stdout:
        lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
    rc = p.wait(timeout=timeout)
    return rc, lines


def gate_png(path):
    """Delivery discipline: [ -s ] on the artifact."""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RuntimeError("engine produced no output at %s" % path)


# ------------------------------------------------------------------- tools

TOOLS = [
    {"name": "daub_status",
     "description":
         "Self-check the daub atelier: which daub_paint engine resolves, "
         "ffmpeg availability (timelapse needs it), brush library, and a "
         "2-stroke engine smoke render. Call this first when unsure.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daub_list_brushes",
     "description":
         "The 26 calibrated brush presets - the ONLY legal values for "
         "pen re-assignment (daub_edit_plan layer_pens). Anything else "
         "fails loud at the engine. Lines are `raw — 中文名｜说明`.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daub_inspect_plan",
     "description":
         "Read a daub plan JSON: header (canvas/seed/bg/reference/count) "
         "plus a per-layer census in first-appearance order - layer "
         "index, name, stroke count, share, preset mix, size range. "
         "Feed this before daub_edit_plan decisions.",
     "inputSchema": {"type": "object",
                     "required": ["plan"],
                     "properties": {"plan": {
                         "type": "string",
                         "description": "path to <stem>_plan.json"}}}},
    {"name": "daub_plan",
     "description":
         "Image in, painting out - the FULL chain. Plans strokes from a "
         "reference image (the slow part, seconds to ~2 min for large "
         "images) and renders the final PNG. Runs as an async job: "
         "returns job_id immediately, then daub_wait on it. Artifacts: "
         "<out> png + <stem>_plan.json next to it. Optional: --kra "
         "layered file, single-pen override.",
     "inputSchema": {"type": "object",
                     "required": ["reference", "out_png"],
                     "properties": {
                         "reference": {"type": "string",
                                       "description": "path to the "
                                       "reference image"},
                         "out_png": {"type": "string",
                                     "description": "output painting "
                                     "path (plan JSON lands beside it)"},
                         "pen": {"type": "string",
                                 "description": "force ONE preset for "
                                 "the whole painting (must be a "
                                 "daub_list_brushes value)"},
                         "kra": {"type": "string",
                                 "description": "also write a layered "
                                 ".kra here"},
                         "psd": {"type": "string",
                                 "description": "also write a layered "
                                 ".psd (Photoshop / Clip Studio open "
                                 "it directly)"}}}},
    {"name": "daub_render",
     "description":
         "Render an existing plan as-is (sub-second) - PNG, optionally "
         "also a layered .kra for Krita or a layered .psd for "
         "Photoshop / Clip Studio / Affinity. Use after daub_edit_plan "
         "or on any <stem>_plan.json. Returns the image too.",
     "inputSchema": {"type": "object",
                     "required": ["plan", "out_png"],
                     "properties": {
                         "plan": {"type": "string"},
                         "out_png": {"type": "string"},
                         "kra": {"type": "string"},
                         "psd": {"type": "string"},
                         "show_image": {"type": "boolean",
                                        "default": True}}}},
    {"name": "daub_edit_plan",
     "description":
         "The layer workbench, programmatically: mute layers (their "
         "strokes drop out, order preserved), re-assign brushes per "
         "layer (values must be daub_list_brushes presets), scale "
         "every layer's stroke sizes by a proportional factor "
         "(layer_sizes - the per-layer fine-tuning ruler, geometry "
         "untouched), prune strokes whose highest-pressure point falls "
         "inside region boxes (data-domain erase - no eraser halo), "
         "then save a NEW plan copy (default <stem>_edit_plan.json; "
         "never overwrites the input) and optionally render a preview "
         "PNG. Layer addressing: names as strings or 0-based "
         "first-appearance indices as integers.",
     "inputSchema": {"type": "object",
                     "required": ["plan"],
                     "properties": {
                         "plan": {"type": "string"},
                         "out_plan": {"type": "string",
                                      "description": "edited copy path "
                                      "(default <stem>_edit_plan.json)"},
                         "mute_layers": {"type": "array",
                                         "description": "layers to "
                                         "silence (names or indices)"},
                         "layer_pens": {"type": "object",
                                        "description": "layer -> preset "
                                        "re-assignment"},
                         "layer_sizes": {"type": "object",
                                         "description": "layer -> size "
                                         "multiplier, e.g. {\"L7\": "
                                         "1.15} - proportional stroke "
                                         "size ruler (0.2..3.0, 1.0 = "
                                         "unchanged)"},
                         "prune_regions": {"type": "array",
                                           "description": "[x0,y0,x1,y1] "
                                           "boxes; strokes anchored "
                                           "(max-pressure point) inside "
                                           "are deleted"},
                         "prune_layers": {"type": "array",
                                          "description": "restrict the "
                                          "prune rule to these layer "
                                          "names"},
                         "render_preview": {"type": "boolean",
                                            "default": True},
                         "show_image": {"type": "boolean",
                                        "default": True}}}},
    {"name": "daub_refine",
     "description":
         "The headless correction loop on an existing plan (async job, "
         "daub_wait it): erase strokes anchored in region boxes and "
         "re-plan corrective ink seeded on the real daub render (refine), "
         "additive deficit passes (topup), a ground block coat "
         "(groundfill), or one refine then topups (auto). Every round is "
         "scored against the reference (mean|diff + within60) and the "
         "loop stops on target / gain plateau / rounds budget - reported "
         "honestly, no fake convergence. Truth = daub's own raster, never "
         "the planner's sim. Writes <stem>_refined_plan.json + "
         "<stem>_refine_report.json + the rendered PNG; never overwrites "
         "the input plan.",
     "inputSchema": {"type": "object",
                     "required": ["plan", "regions"],
                     "properties": {
                         "plan": {"type": "string"},
                         "regions": {"type": "array",
                                     "items": {"type": "array",
                                               "items":
                                               {"type": "number"},
                                               "minItems": 4,
                                               "maxItems": 4},
                                     "description": "[x0,y0,x1,y1] "
                                     "boxes within the canvas"},
                         "mode": {"type": "string",
                                  "enum": ["refine", "topup",
                                           "groundfill", "auto"],
                                  "default": "refine"},
                         "layers": {"type": "array",
                                    "description": "restrict the "
                                    "offender rule to these layer "
                                    "names (refine mode)"},
                         "rounds": {"type": "integer", "minimum": 1,
                                    "maximum": 12, "default": 4},
                         "max_passes": {"type": "integer",
                                        "description": "planner refill "
                                        "pass budget per round"},
                         "stop_gain": {"type": "number",
                                       "default": 2.0,
                                       "description": "stop when a "
                                       "round improves less than "
                                       "this"},
                         "target": {"type": "number",
                                    "description": "stop once the "
                                    "region score reaches this"},
                         "groundfill_size": {"type": "integer",
                                             "default": 20},
                         "ref_img": {"type": "string",
                                     "description": "override the "
                                     "reference (default: plan "
                                     "header's)"},
                         "out_png": {"type": "string",
                                     "description": "deliverable PNG "
                                     "(default <plan stem>_refined."
                                     "png)"}}}},
    {"name": "daub_preview_strokes",
     "description":
         "Live growth: render only the first `fraction` (0.01-1.0) of a "
         "plan's strokes - the same mechanism the GUI's live preview "
         "rides. Great for watching a painting grow frame by frame.",
     "inputSchema": {"type": "object",
                     "required": ["plan", "fraction"],
                     "properties": {
                         "plan": {"type": "string"},
                         "fraction": {"type": "number",
                                      "minimum": 0.01, "maximum": 1.0},
                         "out_png": {"type": "string"},
                         "show_image": {"type": "boolean",
                                        "default": True}}}},
    {"name": "daub_timelapse",
     "description":
         "Render a plan as a reveal video (strokes stream in layer by "
         "layer, ffmpeg-concatenated). Slow - async job, daub_wait it. "
         "Needs ffmpeg (check daub_status). Without cadence params the "
         "frozen engine's default cadence rides (the GUI export path); "
         "passing fps/reveal_frames/layer_hold/final_hold/reveal_order "
         "switches to an in-process render_timelapse run, which needs "
         "the dev tree.",
     "inputSchema": {"type": "object",
                     "required": ["plan", "out_mp4"],
                     "properties": {
                         "plan": {"type": "string"},
                         "out_mp4": {"type": "string"},
                         "fps": {"type": "integer", "default": 15},
                         "reveal_frames": {"type": "integer",
                                           "default": 120},
                         "layer_hold": {"type": "number", "default": 1.0},
                         "final_hold": {"type": "number",
                                        "default": 3.0},
                         "reveal_order": {"type": "string",
                                          "enum": ["big_first",
                                                   "small_first"],
                                          "default": "big_first",
                                          "description": "big_first = "
                                                         "flat bed "
                                                         "first; "
                                                         "small_first = "
                                                         "detail "
                                                         "sketches in "
                                                         "first, bed "
                                                         "slams in "
                                                         "last"}}}},
    {"name": "daub_frame",
     "description":
         "One moment of a paint-down timelapse as deliverables: frame "
         "PNG + frame-exact cut mp4 + layered .kra/.psd + the "
         "truncated plan at that stroke count. Needs the dev-tree "
         "engine and ffmpeg (check daub_status). With a video the "
         "reveal order is pinned against pixels (order 'auto'); "
         "plan-only (no video) needs an explicit order. Slow - async "
         "job, daub_wait it.",
     "inputSchema": {"type": "object",
                     "required": ["seconds"],
                     "properties": {
                         "video": {"type": "string",
                                   "description": "the *_edit_timelapse"
                                                  ".mp4 (omit for "
                                                  "plan-only; then "
                                                  "plan+order are "
                                                  "required)"},
                         "plan": {"type": "string",
                                  "description": "plan behind the video "
                                                 "(default: "
                                                 "<stem>_work/_edit.json"
                                                 ", else "
                                                 "<stem>_plan.json)"},
                         "seconds": {"type": ["number", "string"],
                                     "description": "the moment, e.g. "
                                                    "12 or \"12s\""},
                         "out_dir": {"type": "string"},
                         "base": {"type": "string",
                                  "description": "output stem override "
                                                 "(default: video stem "
                                                 "minus "
                                                 "_edit_timelapse)"},
                         "order": {"type": "string",
                                   "enum": ["auto", "big_first",
                                            "small_first"],
                                   "default": "auto"},
                         "fps": {"type": "number", "default": 15},
                         "reveal_frames": {"type": "integer",
                                           "default": 120},
                         "layer_hold": {"type": "number", "default": 1.0},
                         "final_hold": {"type": "number",
                                        "default": 3.0},
                         "cut": {"type": "boolean", "default": True},
                         "kra": {"type": "boolean", "default": True},
                         "psd": {"type": "boolean", "default": True},
                         "verify": {"type": "string",
                                    "enum": ["auto", "skip"],
                                    "default": "auto",
                                    "description": "skip = count-pin "
                                                   "only (no PIL on "
                                                   "this host)"}}}},
    {"name": "daub_wait",
     "description":
         "Wait for an async job (daub_plan / daub_refine / "
         "daub_timelapse / daub_frame). Blocks up to timeout_s, "
         "emitting progress; "
         "returns state, artifacts, engine log tail - for refine jobs "
         "the per-round score lines stream through here. Safe to call "
         "repeatedly.",
     "inputSchema": {"type": "object",
                     "required": ["job_id"],
                     "properties": {
                         "job_id": {"type": "string"},
                         "timeout_s": {"type": "number",
                                       "default": 240}}}},
    {"name": "daub_cancel",
     "description": "Cancel a running async job (kills the engine "
                    "process).",
     "inputSchema": {"type": "object",
                     "required": ["job_id"],
                     "properties": {"job_id": {"type": "string"}}}},
]


def norm_layers(doc, spec):
    """Accept layer names or 0-based indices; return the name set."""
    layers = plan_layers(doc)
    names = set()
    for x in spec or ():
        if isinstance(x, bool):
            raise ValueError("bad layer spec: %r" % (x,))
        if isinstance(x, int):
            names.add(layers[x]["name"])
        elif isinstance(x, str):
            if x not in {L["name"] for L in layers}:
                raise ValueError("no layer named %r (have: %s)" % (
                    x, [L["name"] for L in layers]))
            names.add(x)
    return names


# -------------------------------------------------------------- dispatch

def tool_status(_args):
    out = {}
    try:
        eng = resolve_engine()
        out["engine"] = " ".join(eng)
    except RuntimeError as e:
        out["engine"] = str(e)
    ff = resolve_ffmpeg()
    out["ffmpeg"] = ff or "MISSING (timelapse disabled)"
    out["brush_lib"] = CAL if os.path.isfile(CAL) else "MISSING"
    out["brushes"] = brush_table() if os.path.isfile(CAL) else []
    # 2-stroke smoke render through the real engine
    import tempfile
    td = tempfile.mkdtemp(prefix="daub_mcp_probe_")
    tiny = os.path.join(td, "tiny.json")
    doc = {"reference": "probe", "canvas": [96, 96], "seed": 1,
           "bg": [246, 249, 251], "detail_rois": [], "count": 2,
           "strokes": [
               {"layer": "probe", "preset": "d) Ink-3 Gpen", "size": 6,
                "opacity": 1.0, "color": "#2e4356",
                "points": [[10, 70, 1], [40, 55, 1], [70, 20, 1]]},
               {"layer": "probe2", "preset": "b) Basic-2 Opacity",
                "size": 10, "opacity": 0.9, "color": "#55708a",
                "points": [[30, 80, 1], [60, 78, 1], [85, 60, 1]]}]}
    with open(tiny, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    png = os.path.join(td, "tiny.png")
    rc, lines = run_sync(render_args(tiny, png), timeout=120)
    out["engine_render_smoke"] = "rc=%d %s" % (
        rc, "OK" if rc == 0 and os.path.getsize(png) > 0 else "FAIL")
    out["smoke_tail"] = lines[-3:]
    return {"content": [{"type": "text", "text": json.dumps(
        out, ensure_ascii=False, indent=1)}]}


def tool_list_brushes(_args):
    # raw key first (it is the value tools accept), Chinese display name
    # after it so humans/AI can pick in plain language
    lines = ["%s — %s｜%s" % (b, zh(b), BRUSH_NOTES.get(b, ""))
             for b in brush_table()]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def tool_inspect(args):
    doc = load_plan(args["plan"])
    hdr = {k: doc.get(k) for k in
           ("reference", "canvas", "seed", "bg", "count")}
    hdr["detail_rois"] = len(doc.get("detail_rois") or [])
    census = plan_layers(doc)
    text = json.dumps({"header": hdr, "layers": census},
                      ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}]}


def tool_plan(args):
    out_png = os.path.abspath(args["out_png"])
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    jargs = [os.path.abspath(args["reference"]), out_png]
    if args.get("pen"):
        if args["pen"] not in brush_table():
            raise ValueError("pen %r not in calib table" % args["pen"])
        jargs += ["--pen", args["pen"]]
    if args.get("kra"):
        jargs += ["--kra", os.path.abspath(args["kra"])]
    if args.get("psd"):
        jargs += ["--psd", os.path.abspath(args["psd"])]
    stem = os.path.splitext(out_png)[0]
    job = start_job("plan", jargs,
                    artifacts=[out_png, stem + "_plan.json"])
    return {"content": [{"type": "text", "text": json.dumps({
        "job_id": job.id, "state": "running",
        "artifacts": list(job.artifacts),
        "note": "slow (planning is CPU-bound, up to ~2 min on large "
                "images) - poll with daub_wait"},
        ensure_ascii=False)}]}


def tool_render(args):
    out_png = os.path.abspath(args["out_png"])
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    kra = os.path.abspath(args["kra"]) if args.get("kra") else None
    if kra:
        os.makedirs(os.path.dirname(kra) or ".", exist_ok=True)
    psd = os.path.abspath(args["psd"]) if args.get("psd") else None
    if psd:
        os.makedirs(os.path.dirname(psd) or ".", exist_ok=True)
    rc, lines = run_sync(render_args(os.path.abspath(args["plan"]),
                                     out_png, kra, psd=psd), timeout=300)
    if rc != 0:
        return {"content": [{"type": "text", "text":
                "render failed rc=%d\n%s" % (rc, "\n".join(lines[-10:]))}],
                "isError": True}
    gate_png(out_png)
    doc = load_plan(args["plan"])
    content = [{"type": "text", "text": json.dumps({
        "out_png": out_png, "kra": kra, "psd": psd,
        "strokes": doc.get("count"),
        "bytes": os.path.getsize(out_png)}, ensure_ascii=False)}]
    if args.get("show_image", True):
        blk = image_block(out_png)
        if blk:
            content.append(blk)
    return {"content": content}


def tool_edit(args):
    plan = os.path.abspath(args["plan"])
    doc = load_plan(plan)
    mute = norm_layers(doc, args.get("mute_layers"))
    pens = {}
    legal = set(brush_table())
    layer_names = [L["name"] for L in plan_layers(doc)]

    def map_key(k):
        # JSON object keys are strings: "3" means first-appearance idx 3
        if k.isdigit():
            if int(k) >= len(layer_names):
                raise ValueError("layer index %s out of range (have %d)"
                                 % (k, len(layer_names)))
            return layer_names[int(k)]
        if k not in layer_names:
            raise ValueError("no layer named %r (have: %s)"
                             % (k, layer_names))
        return k

    for k, v in (args.get("layer_pens") or {}).items():
        if v not in legal:
            raise ValueError("pen %r not in calib table; legal: %s"
                             % (v, sorted(legal)))
        pens[map_key(k)] = v
    sizes = {}
    for k, v in (args.get("layer_sizes") or {}).items():
        sizes[map_key(k)] = v
    new_doc, notes = edit_doc(doc, mute, pens,
                              prune_regions=args.get("prune_regions"),
                              prune_layers=args.get("prune_layers"),
                              layer_sizes=sizes or None)
    out = args.get("out_plan") or \
        os.path.splitext(plan)[0] + "_edit_plan.json"
    out = os.path.abspath(out)
    if os.path.normcase(out) == os.path.normcase(plan):
        raise ValueError("refusing to overwrite the input plan - pass "
                         "out_plan (default adds _edit_plan)")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(new_doc, fh, ensure_ascii=False)
    notes["out_plan"] = out
    notes["bytes"] = os.path.getsize(out)
    content = [{"type": "text", "text": json.dumps(notes,
                                                   ensure_ascii=False,
                                                   indent=1)}]
    if args.get("render_preview", True):
        stem = os.path.splitext(out)[0]
        if stem.endswith("_edit"):
            stem = stem[:-len("_edit")]
        png = stem + "_edit_preview.png"
        rc, lines = run_sync(render_args(out, png), timeout=300)
        if rc != 0:
            content.append({"type": "text", "text":
                            "preview render failed rc=%d\n%s"
                            % (rc, "\n".join(lines[-6:]))})
        else:
            gate_png(png)
            content[0]["text"] = json.dumps(
                dict(notes, preview=png), ensure_ascii=False, indent=1)
            if args.get("show_image", True):
                blk = image_block(png)
                if blk:
                    content.append(blk)
    return {"content": content}


def tool_refine(args):
    """The headless correction loop (daub_refine.run) as an async job:
    the engine process plans, renders and scores; per-round metrics
    stream out as JSON lines the wait surfaces."""
    plan = os.path.abspath(args["plan"])
    doc = load_plan(plan)               # early fail: is it a plan at all
    import daub_refine                  # lazy: stdlib-only at import time
    mode = args.get("mode", "refine")
    if mode not in ("refine", "topup", "groundfill", "auto"):
        raise ValueError("mode must be refine|topup|groundfill|auto")
    regions = args.get("regions")
    if not regions:
        raise ValueError("refine needs regions (wholesale redo is a "
                         "fresh daub_plan)")
    daub_refine.parse_regions(regions, doc["canvas"])   # fail before spawn
    layers = args.get("layers") or None
    out_png = os.path.abspath(args.get("out_png") or
                              os.path.splitext(plan)[0] + "_refined.png")
    stem = os.path.splitext(out_png)[0]
    out_plan = stem + "_refined_plan.json"
    report = stem + "_refine_report.json"
    for p in (out_png, out_plan, report):
        if os.path.normcase(p) == os.path.normcase(plan):
            raise ValueError("refusing to overwrite the input plan - "
                             "pass out_png")
    a = ["--refine", plan, out_png, "--mode", mode]
    # (--refine is a flag: REF positional carries the plan, out_png the
    # deliverable - same shape as --render-only)
    for b in regions:
        a += ["--regions", ",".join(str(int(v)) for v in b)]
    if layers:
        a += ["--layers", ",".join(layers)]
    if args.get("rounds"):
        a += ["--rounds", str(int(args["rounds"]))]
    if args.get("max_passes"):
        a += ["--max-passes", str(int(args["max_passes"]))]
    if args.get("stop_gain") is not None:
        a += ["--stop-gain", str(float(args["stop_gain"]))]
    if args.get("target") is not None:
        a += ["--target", str(float(args["target"]))]
    if args.get("groundfill_size"):
        a += ["--gf-size", str(int(args["groundfill_size"]))]
    if args.get("ref_img"):
        a += ["--ref-img", args["ref_img"]]
    job = start_job("refine-%s" % mode, a,
                    artifacts=[out_png, out_plan, report])
    return {"content": [{"type": "text", "text": json.dumps(
        {"job_id": job.id, "mode": mode, "regions": regions,
         "layers": layers, "artifacts": [out_png, out_plan, report],
         "note": "daub_wait on %s; per-round scores stream as JSON "
                 "lines" % job.id}, ensure_ascii=False)}]}


def tool_preview(args):
    plan = os.path.abspath(args["plan"])
    doc = load_plan(plan)
    frac = float(args["fraction"])
    if not 0.01 <= frac <= 1.0:
        raise ValueError("fraction must be within 0.01..1.0")
    pd, k = prefix_doc(doc, frac)
    out = args.get("out_png") or os.path.join(
        os.path.dirname(plan) or ".",
        os.path.splitext(os.path.basename(plan))[0]
        + "_grow_%02d.png" % round(frac * 100))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".prefix.json"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pd, fh, ensure_ascii=False)
    rc, lines = run_sync(render_args(tmp, out), timeout=300)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if rc != 0:
        return {"content": [{"type": "text", "text":
                "preview render failed rc=%d\n%s"
                % (rc, "\n".join(lines[-8:]))}], "isError": True}
    gate_png(out)
    content = [{"type": "text", "text": json.dumps({
        "out_png": out, "fraction": frac, "strokes_shown": k,
        "strokes_total": doc.get("count")}, ensure_ascii=False)}]
    if args.get("show_image", True):
        blk = image_block(out)
        if blk:
            content.append(blk)
    return {"content": content}


def tool_timelapse(args):
    # sync guard: the schema enum constrains compliant clients only -
    # a bad order must die here, not after the job thread starts
    if "reveal_order" in args and args["reveal_order"] not in (
            "big_first", "small_first"):
        return {"content": [{"type": "text", "text":
                "invalid reveal_order %r (big_first | small_first)"
                % (args["reveal_order"],)}], "isError": True}
    plan = os.path.abspath(args["plan"])
    out_mp4 = os.path.abspath(args["out_mp4"])
    os.makedirs(os.path.dirname(out_mp4) or ".", exist_ok=True)
    stem = os.path.splitext(plan)[0]
    final_png = stem + ".png"
    if not os.path.isfile(final_png):
        # render the final frame first - timelapse needs it
        rc, lines = run_sync(render_args(plan, final_png), timeout=300)
        if rc != 0:
            raise RuntimeError("final-frame render failed: %s"
                               % "\n".join(lines[-5:]))
    paced = {k: args[k] for k in
             ("fps", "reveal_frames", "layer_hold", "final_hold",
              "reveal_order") if k in args}
    paced_flag = bool(paced)
    if not paced:
        # default cadence: the exact path the GUI export rides
        jargs = ["--render-only", plan, final_png,
                 "--timelapse", out_mp4]
        job = start_job("timelapse", jargs, artifacts=[out_mp4])
    else:
        # explicit cadence: render_timelapse.run in-process (needs the
        # dev tree: target/release/daub.exe + tools/data assets)
        daub_bin = os.path.join(ROOT, "target", "release",
                                "daub.exe" if os.name == "nt" else "daub")
        if not os.path.isfile(daub_bin):
            raise RuntimeError(
                "cadence parameters need the dev tree "
                "(%s missing) - retry without fps/reveal_frames/"
                "layer_hold/final_hold/reveal_order" % daub_bin)
        # MCP-facing name is reveal_order; run()'s kwarg is order
        order = paced.pop("reveal_order", "big_first")
        sys.path.insert(0, HERE)
        import render_timelapse as rt
        buf = []

        def rtlog(m):
            buf.append(str(m).rstrip())

        def run_it():
            rt.run(plan, final_png, out_mp4, daub=daub_bin, order=order,
                   log=rtlog, **paced)

        job = start_fn_job("timelapse-paced", run_it,
                           artifacts=[out_mp4], lines=buf)
    return {"content": [{"type": "text", "text": json.dumps({
        "job_id": job.id, "state": "running", "paced": paced_flag,
        "artifacts": [out_mp4],
        "note": "poll with daub_wait"}, ensure_ascii=False)}]}


RX_VIDEO = re.compile(r"^video (\d+)% \((\d+)/(\d+) frames\)$")


def tool_frame(args):
    # sync guards: die here, not after the job thread starts
    order = args.get("order", "auto")
    verify = args.get("verify", "auto")
    if order not in ("auto", "big_first", "small_first"):
        return {"content": [{"type": "text", "text":
                "invalid order %r (auto | big_first | small_first)"
                % (order,)}], "isError": True}
    if verify not in ("auto", "skip"):
        return {"content": [{"type": "text", "text":
                "invalid verify %r (auto | skip)" % (verify,)}],
                "isError": True}
    if "seconds" not in args:
        return {"content": [{"type": "text", "text":
                "seconds is required"}], "isError": True}
    video = os.path.abspath(args["video"]) if args.get("video") else None
    plan = os.path.abspath(args["plan"]) if args.get("plan") else None
    if video is None and order == "auto":
        return {"content": [{"type": "text", "text":
                "plan-only (no video) needs an explicit order: "
                "big_first | small_first"}], "isError": True}
    if video is None and plan is None:
        return {"content": [{"type": "text", "text":
                "pass video (a *_edit_timelapse.mp4) or plan"}],
                "isError": True}
    daub_bin = os.path.join(ROOT, "target", "release",
                            "daub.exe" if os.name == "nt" else "daub")
    if not os.path.isfile(daub_bin):
        raise RuntimeError(
            "daub_frame needs the dev tree (%s missing)" % daub_bin)
    if video is not None and resolve_ffmpeg() is None:
        raise RuntimeError("ffmpeg not found (daub_status checks it)")

    sys.path.insert(0, HERE)
    import daub_frame as dfr
    t = dfr.parse_seconds(args["seconds"])
    out_dir = os.path.abspath(args["out_dir"]) if args.get("out_dir") \
        else None
    paths = dfr.output_paths(video, t, out_dir=out_dir,
                             base=args.get("base"))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    reg = {"frame_png": paths["frame_png"], "plan": paths["plan"]}
    if args.get("kra", True):
        reg["kra"] = paths["kra"]
    if args.get("psd", True):
        reg["psd"] = paths["psd"]
    if video is not None and args.get("cut", True):
        reg["cut_mp4"] = paths["cut_mp4"]
    buf = []

    def run_it():
        res = dfr.run(video=video, seconds=args["seconds"], plan=plan,
                      out_dir=out_dir, base=args.get("base"), order=order,
                      fps=args.get("fps", 15.0),
                      reveal_frames=args.get("reveal_frames", 120),
                      layer_hold=args.get("layer_hold", 1.0),
                      final_hold=args.get("final_hold", 3.0),
                      do_cut=args.get("cut", True),
                      do_kra=args.get("kra", True),
                      do_psd=args.get("psd", True), daub=daub_bin,
                      verify=verify, log=buf.append)
        # the summary dict rides the log as its last line - greppable
        # from daub_wait's log_tail without a second channel
        buf.append("summary " + json.dumps(res, ensure_ascii=False))

    job = start_fn_job("frame", run_it, artifacts=list(reg), lines=buf)
    job.artifacts.clear()
    job.artifacts.update(reg)
    return {"content": [{"type": "text", "text": json.dumps({
        "job_id": job.id, "state": "running",
        "artifacts": reg,
        "note": "poll with daub_wait; the artifacts dict carries the "
                "exact output paths"}, ensure_ascii=False)}]}


def video_progress(lines):
    """Latest 'video N% (k/total frames)' milestone from render_timelapse
    (emitted on both the pipe and legacy paths), or None. Before the
    frozen engine shipped these lines this returns None and callers
    fall back to the elapsed-time heuristic."""
    for s in reversed(lines):
        m = RX_VIDEO.match(s.strip())
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def job_report(job):
    vp = video_progress(job.lines)
    return {"job_id": job.id, "label": job.label, "state": job.state(),
            "rc": job.rc,
            "video_progress": (
                {"pct": vp[0], "frames": vp[1], "total": vp[2]}
                if vp else None),
            "artifacts": {
                a: (p if p and os.path.isfile(p) and
                    os.path.getsize(p) > 0 else None)
                for a, p in job.artifacts.items()},
            "log_tail": job.tail()}


def tool_wait(args, progress=None):
    job = JOBS.get(args["job_id"])
    if job is None:
        raise ValueError("no such job: %s" % args["job_id"])
    timeout = float(args.get("timeout_s", 240))
    waited = 0.0
    while not job.done.wait(0.5):
        waited += 0.5
        if progress:
            # true percent when the engine emits milestones; elapsed-time
            # extrapolation is only the pre-milestone placeholder
            vp = video_progress(job.lines)
            if vp is not None:
                progress(vp[0] / 100.0,
                         "%s: video %d%% (%d/%d frames)"
                         % (job.label, vp[0], vp[1], vp[2]))
            else:
                progress(min(waited / (waited + 30.0), 0.95),
                         "%s: %d log lines" % (job.label, len(job.lines)))
        if waited >= timeout:
            break
    rep = job_report(job)
    # verify artifacts with the [-s] gate before calling anything done
    if job.state() == "done":
        missing = [a for a, p in rep["artifacts"].items() if not p]
        if missing:
            rep["state"] = "failed(empty artifacts: %s)" % missing
    return {"content": [{"type": "text", "text": json.dumps(
        rep, ensure_ascii=False, indent=1)}]}


def tool_cancel(args):
    job = JOBS.get(args["job_id"])
    if job is None:
        raise ValueError("no such job: %s" % args["job_id"])
    if job.proc is None:
        raise ValueError("%s runs in-process and cannot be killed; "
                         "wait it out" % job.id)
    if job.proc.poll() is None:
        job.proc.kill()
        return {"content": [{"type": "text", "text":
                "%s killed (%s)" % (job.id, job.label)}]}
    return {"content": [{"type": "text", "text":
            "%s already finished rc=%s" % (job.id, job.rc)}]}


HANDLERS = {
    "daub_status": tool_status,
    "daub_list_brushes": tool_list_brushes,
    "daub_inspect_plan": tool_inspect,
    "daub_plan": tool_plan,
    "daub_render": tool_render,
    "daub_edit_plan": tool_edit,
    "daub_refine": tool_refine,
    "daub_preview_strokes": tool_preview,
    "daub_timelapse": tool_timelapse,
    "daub_frame": tool_frame,
    "daub_cancel": tool_cancel,
}


# ------------------------------------------------------------------ main

def write(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve():
    # The host process's stdout may default to a legacy ANSI codepage
    # (GBK on this machine): protocol lines carry CJK paths, so pin
    # both directions to UTF-8 - the same trap render_timelapse's
    # frames.txt hit in the GUI chain.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    log("serving on stdio; engine resolution on first call")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            write({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": str(e)}})
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:
            continue  # notification (initialized ...)
        try:
            if method == "initialize":
                # echo the client's protocol version: maximum compat
                ver = (msg.get("params") or {}).get("protocolVersion") \
                    or PROTOCOL
                write({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO}})
            elif method == "ping":
                write({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif method == "tools/list":
                write({"jsonrpc": "2.0", "id": mid,
                       "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                ptoken = ((params.get("_meta") or {})
                          .get("progressToken"))

                def progress(frac, text, _t=ptoken, _mid=mid):
                    write({"jsonrpc": "2.0", "method":
                           "notifications/progress", "params": {
                               "progressToken": _t,
                               "progress": round(frac, 3),
                               "message": text}})

                try:
                    if name == "daub_wait":
                        result = tool_wait(args, progress if ptoken
                                           is not None else None)
                    else:
                        h = HANDLERS.get(name)
                        if h is None:
                            raise ValueError("unknown tool: %s" % name)
                        result = h(args)
                except Exception as e:
                    log("tool %s error: %s" % (name, e))
                    result = {"content": [{"type": "text",
                                           "text": "%s: %s"
                                           % (type(e).__name__, e)}],
                              "isError": True}
                write({"jsonrpc": "2.0", "id": mid, "result": result})
            else:
                write({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601,
                    "message": "method not found: %s" % method}})
        except Exception as e:  # never die on one bad message
            log("dispatch error:", repr(e))
            write({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603, "message": repr(e)}})
    log("stdin closed - bye")


if __name__ == "__main__":
    serve()
