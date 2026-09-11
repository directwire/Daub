"""Progressive paint-down video for a plan JSON: layer by layer, small
strokes to large, the finished painting held on screen at the end.

Lives in the daub repo and consumes the vendored assets in tools/data
(ink_calib.json / brush_lib.json / brush_tips), drift-guarded against
their upstream originals by check_assets.py; overridable with
--cal/--tips.

Every frame is a true sub-state of one rendering: the fast path writes
the reveal-ordered sequence ONCE and drives `daub render-seq` - the engine's incremental
SeqRenderer re-renders only the layer each prefix grows (byte-identical
to fresh prefix renders, unit-tested). Newest engines take that further
with --pipe: raw rgb24 frames stream from the engine's stdout straight
into ffmpeg's image2pipe, so no ~8MB intermediate files exist at all
(11.1s -> ~5s on the 31k-stroke plan, ~1.4GB tmp I/O gone). Engines
with render-seq but without --pipe emit lossless 24-bit BMP frames
consumed through the concat demuxer - whose list must stay all-BMP:
ffmpeg locks onto the first entry's decoder and silently drops png
entries that follow bmp ones, so the closing frame rides
`daub render --out f_final.bmp`. Engines without render-seq (e.g. an
older frozen daub_paint) fall back to the legacy per-frame loop;
DAUB_TIMELAPSE_LEGACY=1 forces it for A/B audits.
Within each layer strokes are re-sorted small -> big. Two reveal
orders (--order / run(order=...)):
"big_first" (default) walks the layer stack bottom-up - the flat bed
lands first, detail closes, which reads as big strokes -> small;
"small_first" reverses the stack so detail sketches
in first and the flat bed slams in last. big_first preserves the
plan's layer stack order per frame, so compositing is exact;
small_first frames carry the inverted stack (the opaque F1 bed would
composite OVER the bands in the full-seq frame), so its closing reveal
frame is spliced from the REAL render (final.png) instead - the
video ends on the real, approved pixels. Each completed layer holds
~1s; the final result holds --final-hold seconds.

Library entry: run() (used by the CLI below, daub_paint --timelapse and
the GUI's video export) plus the prefix helpers the GUI's live-growth
snapshots and layer workbench reuse (load_plan / layer_names /
layer_stats / apply_edits / write_prefix / render_prefix).

Usage:
  python tools/render_timelapse.py <plan.json> <final.png> <out.mp4>
          [--fps 15] [--reveal-frames 120] [--layer-hold 1.0]
          [--final-hold 3.0]
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAUB = os.path.join(HERE, "..", "target", "release",
                    "daub.exe" if os.name == "nt" else "daub")
CAL = os.path.join(HERE, "data", "ink_calib.json")
TIPS = os.path.join(HERE, "data", "brush_lib.json")


class TimelapseError(RuntimeError):
    """A daub/ffmpeg step failed (GUI callers survive; CLI exits)."""


def resolve_ffmpeg():
    """ffmpeg.exe path or None. DAUB_FFMPEG env wins: the frozen GUI
    frequently runs with a stale inherited PATH (Explorer predates the
    winget ffmpeg install), so PATH lookup alone lies - the GUI
    resolves via the registry and hands the absolute path down here."""
    p = os.environ.get("DAUB_FFMPEG")
    if p and os.path.isfile(p):
        return p
    return shutil.which("ffmpeg")


def load_plan(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def layer_names(d):
    """Layer stack = first appearance order in the plan (daub's rule)."""
    stack = []
    for s in d["strokes"]:
        if s["layer"] not in stack:
            stack.append(s["layer"])
    return stack


def layer_stats(seq):
    """Per layer (first-appearance order): (name, count, min/max size)."""
    groups = {}
    for s in seq:
        groups.setdefault(s["layer"], []).append(s)
    return [(L, len(g), min(s["size"] for s in g),
             max(s["size"] for s in g)) for L, g in groups.items()]


SIZE_SCALE_MIN, SIZE_SCALE_MAX = 0.2, 3.0


def norm_size_scales(raw, known_layers=None):
    """Validate a {layer: multiplier} size ruler from user-facing input:
    finite float within [0.2, 3.0]; 1.0 dropped (no-op); unknown layer
    names fail loud when known_layers is given. Keys are layer names
    verbatim - JSON object keys are always strings, so index mapping is
    the caller's job, same as layer_pens. Returns a clean dict."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("size scales must be an object {layer: factor}")
    out = {}
    for k, v in raw.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("size scale for %r is not a number: %r"
                             % (k, v))
        if not math.isfinite(f) or not (SIZE_SCALE_MIN <= f <= SIZE_SCALE_MAX):
            raise ValueError("size scale for %r out of range: %s (legal "
                             "%s..%s)" % (k, v, SIZE_SCALE_MIN,
                                          SIZE_SCALE_MAX))
        if known_layers is not None and k not in known_layers:
            raise ValueError("no layer named %r (have: %s)"
                             % (k, sorted(known_layers)))
        if f != 1.0:
            out[k] = f
    return out


def apply_size_scales(strokes, scale_by_layer):
    """The per-layer size ruler (a proportional enlarge/shrink per
    layer): multiply each stroke's `size` - and its width_range metadata -
    by the layer's factor. Pressure encodes the width PROFILE, so one
    multiplication rides the whole profile proportionally; geometry and
    per-point pressure are untouched. Copy-on-write like the preset
    rewrite; rounds to the plan's 1-decimal convention, floored at
    0.5px so a hard shrink cannot stamp dust. Returns (new_list, count
    actually scaled)."""
    if not scale_by_layer:
        return strokes, 0
    out, n = [], 0
    for s in strokes:
        k = scale_by_layer.get(s.get("layer"))
        if k is None:
            out.append(s)
            continue
        s = dict(s, size=max(0.5, round(float(s["size"]) * k, 1)))
        wr = s.get("width_range")
        if wr:
            s["width_range"] = [max(0.5, round(float(wr[0]) * k, 1)),
                                max(0.5, round(float(wr[1]) * k, 1))]
        n += 1
        out.append(s)
    return out, n


def apply_edits(d, muted=(), preset_by_layer=None,
                size_scale_by_layer=None):
    """User-facing edit copy: header verbatim, strokes filtered/rewired.

    Muting drops a layer's strokes; preset_by_layer rewrites every
    remaining stroke of that layer; size_scale_by_layer multiplies every
    remaining stroke's size by the layer's factor (the per-layer size
    ruler, geometry untouched). The array order is NOT re-sorted
    (layer stack = first appearance, and filtering preserves relative
    order), and `count` is recomputed - unlike write_prefix, whose
    stale count is deliberate, an edited plan is a user-facing artifact.
    """
    preset_by_layer = preset_by_layer or {}
    out = dict(d)
    seq = []
    for s in d["strokes"]:
        if s["layer"] in muted:
            continue
        p = preset_by_layer.get(s["layer"])
        if p and p != s.get("preset"):
            s = dict(s, preset=p)
        seq.append(s)
    seq, _n = apply_size_scales(seq, size_scale_by_layer)
    out["strokes"] = seq
    out["count"] = len(seq)
    return out


def write_prefix(d, seq, k, path):
    """Plan header verbatim (reference/canvas/bg/seed... - bg is the
    paper colour daub lays down first) with the stroke list swapped for
    seq[:k]. stroke_engine plans use a different header than
    paint_headless dumps, so invent nothing - but where the source has
    a `count` header it must move WITH the strokes (the workbench's
    edit law: strokes+count change together; a truncated plan that
    still claims the full count contradicts itself downstream -
    daub_frame's 09-10 fix)."""
    pref = dict(d)
    pref["strokes"] = list(seq[:k])
    if "count" in pref:
        pref["count"] = k
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pref, fh)


def render_prefix(daub, d, seq, k, plan_tmp, out_png, cal=CAL, tips=TIPS):
    """One daub render of the prefix seq[:k]; TimelapseError on failure.

    Shared by the timelapse loop, the GUI's live-growth snapshots and
    the workbench's edited-plan re-renders."""
    write_prefix(d, seq, k, plan_tmp)
    # utf-8 explicit: text=True alone decodes with the locale (GBK on
    # zh-CN Windows) and daub echoes the output path - Chinese filenames
    # kill the reader thread and r.stdout comes back None
    r = subprocess.run([daub, "render", plan_tmp, "--out", out_png,
                        "--cal", cal, "--tips", tips],
                       capture_output=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise TimelapseError("daub failed at prefix %d:\n%s\n%s"
                             % (k, r.stdout, r.stderr))
    return out_png


def _render_sequence(daub, d, seq, prefixes, outd, cal=CAL, tips=TIPS,
                     log=None):
    """One daub process for every prefix frame (render-seq fast path).

    Writes the reveal-ordered sequence once (header verbatim) plus the
    prefix table, and lets the engine's incremental SeqRenderer emit
    all frames as 24-bit BMP - no per-frame spawn/parse/zlib. Returns
    the frame paths, or None when the engine predates render-seq (the
    caller falls back to the per-frame loop; a real render-seq error
    raises instead so it never hides behind the fallback).
    """
    plan_tmp = os.path.join(outd, "_seq_plan.json")
    pref = dict(d)
    pref["strokes"] = list(seq)
    with open(plan_tmp, "w", encoding="utf-8") as fh:
        json.dump(pref, fh)
    pf = os.path.join(outd, "_prefixes.txt")
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write("\n".join(str(k) for k in prefixes) + "\n")
    r = subprocess.run([daub, "render-seq", plan_tmp,
                        "--out-dir", outd, "--prefixes-file", pf,
                        "--cal", cal, "--tips", tips],
                       capture_output=True, encoding="utf-8",
                       errors="replace")
    for f in (plan_tmp, pf):
        try:
            os.remove(f)
        except OSError:
            pass
    if r.returncode != 0:
        # an engine without the subcommand exits 2 printing its usage
        # banner, which never mentions render-seq; anything else is a
        # real failure and must stay loud
        if "render-seq" not in (r.stderr or ""):
            return None
        raise TimelapseError("daub render-seq failed:\n%s\n%s"
                             % (r.stdout, r.stderr))
    frames = [os.path.join(outd, "f_%06d.bmp" % i)
              for i in range(len(prefixes))]
    if not all(os.path.isfile(p) and os.path.getsize(p) > 0
               for p in frames):
        return None
    if log:
        log("render-seq: %d prefix frames in one engine pass"
            % len(prefixes))
    return frames


def _engine_pipe_support(daub):
    """True when the engine's render-seq knows --pipe/--tail-plan.

    `daub render-seq` with no args exits non-zero printing its usage
    banner; only engines that stream raw frames carry "--pipe" in it.
    One cheap spawn, once per run."""
    r = subprocess.run([daub, "render-seq"], capture_output=True,
                       encoding="utf-8", errors="replace")
    return "--pipe" in (r.stderr or "")


def _pipe_video(daub, d, seq, prefixes, bounds, reveal, out_mp4, *,
                fps, layer_hold, final_hold, order, cal, tips, log,
                outd):
    """Stream the reveal straight into ffmpeg - no frame files at all.

    The engine emits raw rgb24 frames on stdout (--pipe, rendered from
    one parse: prefixes in order, the --tail-plan frame last); this
    side relays them into ffmpeg's image2pipe demuxer in playback
    order, injecting the layer holds and the final hold as repeated
    frames - exactly what the concat list did with duplicate entries
    (same total frame count, trailing +1 included: ffmpeg 8.x drops
    the last entry's duration). The small_first splice discards the
    last prefix frame - the full sequence under the REVERSED stack
    composites the opaque flat bed over the bands - and ends on the
    --tail-plan frame, the plan's real stroke order.

    Returns True. Raises on any failure: the caller only gets here
    after the engine proved --pipe support, so there is nothing to
    fall back to. A failed encode leaves the engine/ffmpeg stderr
    logs in outd as the evidence."""
    ff = resolve_ffmpeg()
    if ff is None:
        raise TimelapseError("ffmpeg not found (set DAUB_FFMPEG or add "
                             "ffmpeg to PATH)")
    w, h = d["canvas"]
    fsize = w * h * 3
    t0 = time.perf_counter()

    plan_tmp = os.path.join(outd, "_seq_plan.json")
    pref = dict(d)
    pref["strokes"] = list(seq)
    with open(plan_tmp, "w", encoding="utf-8") as fh:
        json.dump(pref, fh)
    tail_plan = os.path.join(outd, "_tail_plan.json")
    with open(tail_plan, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    pf = os.path.join(outd, "_prefixes.txt")
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write("\n".join(str(k) for k in prefixes) + "\n")

    eng_log = os.path.join(outd, "_engine_stderr.log")
    ff_log = os.path.join(outd, "_ffmpeg_stderr.log")
    last_hold = round(layer_hold * fps)
    tail_reps = round(final_hold * fps) + 1
    eng = enc = None
    try:
        with open(eng_log, "wb") as ef, open(ff_log, "wb") as xf:
            eng = subprocess.Popen(
                [daub, "render-seq", plan_tmp, "--pipe",
                 "--tail-plan", tail_plan, "--prefixes-file", pf,
                 "--cal", cal, "--tips", tips],
                stdout=subprocess.PIPE, stderr=ef)
            enc = subprocess.Popen(
                [ff, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                 "-s", "%dx%d" % (w, h), "-r", str(fps), "-i", "-",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                 # yuv420p needs even dimensions: the plan canvas is the
                 # reference's native size, which can be odd (img64)
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 out_mp4],
                stdin=subprocess.PIPE, stderr=xf)

            def pull():
                # BufferedReader.read blocks until exactly fsize bytes
                # or EOF - a short read means the engine died mid-frame
                buf = eng.stdout.read(fsize)
                if len(buf) < fsize:
                    raise TimelapseError(
                        "engine stream ended early (%d/%d bytes)"
                        % (len(buf), fsize))
                return buf

            li = 0
            total = (len(prefixes) + len(bounds) * last_hold + tail_reps)
            milestone = max(1, total // 10)
            written = 0
            next_at = milestone
            last_pct = -1

            def fire(n):
                # emit the next milestone window if the write cursor
                # reached it. The tail block (layer hold + final hold =
                # up to ~20% of frames) must fire too - without it the
                # label froze at ~80% for the whole closing hold.
                nonlocal next_at, last_pct
                if n >= next_at:
                    last_pct = n * 100 // total
                    log("video %d%% (%d/%d frames)"
                        % (last_pct, n, total))
                    next_at += milestone

            for fi, k in enumerate(prefixes[:-1]):
                buf = pull()
                reps = 1
                if k == bounds[li]:  # layer just completed: hold ~1s
                    reps += last_hold
                    log("layer %s done at %d strokes (frame %d/%d)"
                        % (reveal[li], k, fi + 1, len(prefixes)))
                    li += 1
                for _ in range(reps):
                    enc.stdin.write(buf)
                written += reps
                fire(written)
            # the last prefix is always the full sequence (and a layer
            # end): big_first plays it, then the tail; small_first
            # discards the resorted composite and lets the tail play
            # through the layer hold into the final hold
            if order == "small_first":
                pull()  # discard the reversed-stack composite
                buf = pull()
                for _ in range(1 + last_hold + tail_reps):
                    enc.stdin.write(buf)
                written += 1 + last_hold + tail_reps
                fire(written)
            else:
                buf = pull()
                for _ in range(1 + last_hold):
                    enc.stdin.write(buf)
                written += 1 + last_hold
                fire(written)
                buf = pull()
                for _ in range(tail_reps):
                    enc.stdin.write(buf)
                written += tail_reps
                fire(written)
            if written != total:
                raise TimelapseError(
                    "frame accounting broke: wrote %d, planned %d"
                    % (written, total))
            if last_pct < 100:
                # a window can end below total: always land on 100
                log("video 100% (%d/%d frames)" % (total, total))
            enc.stdin.close()
            eng_rc = eng.wait()
            enc_rc = enc.wait()
        if eng_rc != 0 or enc_rc != 0:
            def tail_of(path):
                try:
                    with open(path, "rb") as fh:
                        return fh.read()[-2000:].decode("utf-8", "replace")
                except OSError:
                    return ""
            raise TimelapseError(
                "pipe encode failed (engine rc=%s ffmpeg rc=%s)\n"
                "engine:\n%s\nffmpeg:\n%s"
                % (eng_rc, enc_rc, tail_of(eng_log), tail_of(ff_log)))
        for f in (plan_tmp, tail_plan, pf, eng_log, ff_log):
            try:
                os.remove(f)
            except OSError:
                pass
        log("render-seq pipe: %d reveal frames + tail streamed to "
            "ffmpeg in %.1fs"
            % (len(prefixes), time.perf_counter() - t0))
        return True
    except Exception:
        # engine or ffmpeg died mid-stream: stop the other one too and
        # keep the stderr logs as the evidence
        for p in (eng, enc):
            if p is not None and p.poll() is None:
                p.kill()
        for p in (eng, enc):
            if p is not None:
                try:
                    p.wait()
                except OSError:
                    pass
        raise


def reveal_sequence(d, order="big_first"):
    """The reveal sequence and its layer-end anchors: each layer keeps
    its own paint order, re-sorted small -> big; layers walked bottom-up
    (big_first, the default) or top-down (small_first). Returns
    (seq, bounds, reveal) - bounds[i] = len(seq) when reveal[i] just
    finished (the hold anchor). Single source of truth for the
    timelapse stroke axis: run() renders it, daub_frame reconstructs a
    video moment from it."""
    strokes = d["strokes"]
    stack = layer_names(d)
    groups = {L: [] for L in stack}
    for s in strokes:
        groups[s["layer"]].append(s)
    reveal = list(reversed(stack)) if order == "small_first" else stack
    seq, bounds = [], []  # bounds = stroke count at each layer end
    for L in reveal:
        seq.extend(sorted(groups[L], key=lambda s: s["size"]))
        bounds.append(len(seq))
    return seq, bounds, reveal


def prefix_list(seq, bounds, reveal_frames=120):
    """Reveal-frame prefix lengths: per layer ~ sqrt(count), min 2,
    then mapped to global prefix lengths (strictly increasing; layer
    ends always land on a frame so the hold freezes a real layer
    completion). Returns (prefixes, per) - per is the per-layer frame
    split, for reporting."""
    w = [math.sqrt(b - p) for p, b in
         zip([0] + bounds[:-1], bounds)]
    per = [max(2, round(reveal_frames * x / sum(w))) for x in w]
    prefixes = []
    for p, b, n in zip([0] + bounds[:-1], bounds, per):
        for i in range(1, n + 1):
            k = p + (b - p) * i // n
            if k == 0:
                # a tiny first layer can spend its first frame slots
                # before stroke 1 lands (k = (b-p)*i//n == 0) - the
                # engine refuses a zero-stroke prefix, so skip it
                continue
            if not prefixes or k > prefixes[-1]:
                prefixes.append(k)
    prefixes[-1] = len(seq)
    return prefixes, per


def run(plan, final_png, out_mp4, *, fps=15, reveal_frames=120,
        layer_hold=1.0, final_hold=3.0, order="big_first", workdir=None,
        daub=DAUB, cal=CAL, tips=TIPS, log=None) -> int:
    """Render the paint-down video; returns 0 (raises TimelapseError).

    `plan` is a path or an already-loaded dict (the workbench passes an
    edited one); `log` defaults to flushed stdout for CLI parity.
    `order`: "big_first" (default) reveals the stack bottom-up - the flat
    bed lands first, detail closes (reads as big strokes -> small);
    "small_first" reverses the stack (detail first, the flat bed slams
    in last - small strokes to large, optional).
    """
    if order not in ("big_first", "small_first"):
        raise TimelapseError("unknown order %r (big_first|small_first)"
                             % (order,))
    log = log or (lambda m: print(m, flush=True))
    d = plan if isinstance(plan, dict) else load_plan(plan)

    seq, bounds, reveal = reveal_sequence(d, order=order)
    log("stack: %s" % " -> ".join(reveal))
    prefixes, per = prefix_list(seq, bounds, reveal_frames=reveal_frames)
    log("%d reveal frames over %d strokes, per-layer %s"
        % (len(prefixes), len(seq), per))

    outd = workdir or out_mp4.rsplit(".", 1)[0] + "_frames"
    os.makedirs(outd, exist_ok=True)
    plan_tmp = os.path.join(outd, "_prefix.json")

    # fast paths, best first: --pipe streams raw frames straight into
    # ffmpeg (no ~8MB intermediates at all); render-seq writes BMP
    # intermediates; the per-frame loop stays as the legacy fallback
    # and DAUB_TIMELAPSE_LEGACY=1 forces it for A/B audits
    frames = []  # image paths in playback order
    seq_frames = None
    piped = False
    if os.environ.get("DAUB_TIMELAPSE_LEGACY") != "1":
        if _engine_pipe_support(daub):
            piped = _pipe_video(daub, d, seq, prefixes, bounds, reveal,
                                out_mp4, fps=fps, layer_hold=layer_hold,
                                final_hold=final_hold, order=order,
                                cal=cal, tips=tips, log=log, outd=outd)
        else:
            seq_frames = _render_sequence(daub, d, seq, prefixes, outd,
                                          cal=cal, tips=tips, log=log)
            if seq_frames is None:
                log("render-seq unavailable - legacy per-frame loop")
    if piped:
        # same term as _pipe_video's tail_reps (round+1): the pipe
        # really writes that extra final-hold frame, so the report
        # must match ffprobe (318) instead of under-counting (317)
        total = (len(prefixes) + len(bounds) * round(layer_hold * fps)
                 + round(final_hold * fps) + 1)
        log("timelapse done: %d frames @%dfps -> %s (%.1fMB)"
            % (total, fps, out_mp4,
               os.path.getsize(out_mp4) / 1e6))
        return 0
    li = 0
    # the closing shot is the approved render, held; in seq mode it must
    # be a REAL engine BMP, not a copied png: ffmpeg's concat demuxer
    # locks onto the first entry's decoder, and png entries after bmp
    # ones fail to open and are dropped silently (proven on a 285-frame
    # list that encoded to a 239-frame mp4).
    # Rendering the plan the caller passed (deterministic engine) gives
    # pixels identical to final_png, so the small_first splice below can
    # just point at it - created before the loop because that splice is
    # the loop's last frame.
    if seq_frames is not None:
        tail = os.path.join(outd, "f_final.bmp")
        tail_plan = os.path.join(outd, "_final_plan.json")
        with open(tail_plan, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        r = subprocess.run([daub, "render", tail_plan, "--out", tail,
                            "--cal", cal, "--tips", tips],
                           capture_output=True, encoding="utf-8",
                           errors="replace")
        try:
            os.remove(tail_plan)
        except OSError:
            pass
        if r.returncode != 0:
            raise TimelapseError("daub failed on final bmp:\n%s\n%s"
                                 % (r.stdout, r.stderr))
    else:
        tail = os.path.join(outd, "f_final.png")
        shutil.copyfile(final_png, tail)
    total = (len(prefixes) + len(bounds) * round(layer_hold * fps)
             + round(final_hold * fps))
    milestone = max(1, total // 10)
    next_at = milestone
    last_pct = -1

    def fire(n):
        # same milestone discipline as the pipe path: the closing
        # final-hold extend must fire too, and the last line is 100%
        nonlocal next_at, last_pct
        if n >= next_at:
            last_pct = n * 100 // total
            log("video %d%% (%d/%d frames)" % (last_pct, n, total))
            next_at += milestone

    for fi, k in enumerate(prefixes):
        if seq_frames is not None:
            png = seq_frames[fi]
            if k == len(seq) and order == "small_first":
                # the full-seq frame under the reversed stack composites
                # the opaque flat bed OVER the bands (layer stack =
                # first appearance, now inverted) - end on the real
                # approved render instead of a reordered composite; the
                # tail bmp already IS that render (engine determinism)
                png = tail
        else:
            png = os.path.join(outd, "f_%06d.png" % fi)
            if k == len(seq) and order == "small_first":
                # same splice on the legacy path
                shutil.copyfile(final_png, png)
            else:
                render_prefix(daub, d, seq, k, plan_tmp, png,
                              cal=cal, tips=tips)
        frames.append(png)
        if k == bounds[li]:  # layer just completed: hold ~1s
            frames.extend([png] * round(layer_hold * fps))
            log("layer %s done at %d strokes (frame %d/%d)"
                % (reveal[li], k, fi + 1, len(prefixes)))
            li += 1
        fire(len(frames))
    if seq_frames is None:
        try:
            os.remove(plan_tmp)
        except OSError:
            pass

    # the approved render, held (tail was produced before the loop)
    frames.extend([tail] * round(final_hold * fps))
    fire(len(frames))
    if last_pct < 100:
        log("video 100% (%d/%d frames)" % (len(frames), len(frames)))

    lst = os.path.join(outd, "frames.txt")
    # utf-8 explicit: ffmpeg's concat demuxer reads UTF-8, but the
    # default open() is locale (GBK) - a Chinese stem writes mojibake
    # paths and ffmpeg fails (or worse, splices the wrong files)
    with open(lst, "w", encoding="utf-8") as fh:
        for p in frames:
            fh.write("file '%s'\nduration %f\n"
                     % (os.path.abspath(p), 1.0 / fps))
    # ffmpeg 8.x ignores the last entry's duration - repeat the final
    # frame once so the hold actually lands (assemble_video.py's pit)
    with open(lst, "a", encoding="utf-8") as fh:
        fh.write("file '%s'\n" % os.path.abspath(tail))

    ff = resolve_ffmpeg()
    if ff is None:
        raise TimelapseError("ffmpeg not found (set DAUB_FFMPEG or add "
                             "ffmpeg to PATH)")
    r = subprocess.run(
        [ff, "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         # yuv420p needs even dimensions: the plan canvas is the
         # reference's native size, which can be odd (img64: 1789x1440)
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         out_mp4],
        capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise TimelapseError("ffmpeg failed:\n%s" % r.stderr[-2000:])
    if seq_frames is not None:
        # BMP intermediates are pure bulk (~8MB/frame at 1440x1920) and
        # the mp4 holds the pixels once ffmpeg succeeded - clear them
        # plus the tail bmp (a failed encode keeps them as the evidence).
        # Legacy png frames keep their keep-everything behavior.
        removed = 0
        for p in list(seq_frames) + [tail]:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
        log("render-seq: cleared %d bmp frames" % removed)
    log("timelapse done: %d frames @%dfps -> %s (%.1fMB)"
        % (len(frames) + 1, fps, out_mp4,
           os.path.getsize(out_mp4) / 1e6))  # +1: ffmpeg 8.x concat
    # demuxer repeats the last entry once - len(frames) under-counts
    # the real muxed video by exactly that (ffprobe-verified 318 vs 317)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("final_png")
    ap.add_argument("out_mp4")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--reveal-frames", type=int, default=120,
                    help="total frames spent growing strokes, split "
                         "across layers by sqrt(stroke count)")
    ap.add_argument("--layer-hold", type=float, default=1.0,
                    help="seconds each completed layer holds")
    ap.add_argument("--final-hold", type=float, default=3.0,
                    help="seconds the finished painting holds")
    ap.add_argument("--order", default="big_first",
                    choices=("big_first", "small_first"),
                    help="reveal order: big_first = stack bottom-up, "
                         "flat bed first (default); small_first = "
                         "detail first, flat bed slams in last")
    ap.add_argument("--workdir", default=None,
                    help="frames dir (default <out>_frames next to mp4)")
    ap.add_argument("--cal", default=CAL,
                    help="ink calibration (default: tools/data copy)")
    ap.add_argument("--tips", default=TIPS,
                    help="brush registry (default: tools/data copy)")
    args = ap.parse_args()
    try:
        return run(args.plan, args.final_png, args.out_mp4, fps=args.fps,
                   reveal_frames=args.reveal_frames,
                   layer_hold=args.layer_hold,
                   final_hold=args.final_hold, order=args.order,
                   workdir=args.workdir, cal=args.cal, tips=args.tips)
    except TimelapseError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    sys.exit(main())
