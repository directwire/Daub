"""daub_frame: pull one moment out of a paint-down timelapse.

"视频第 12 秒初的那一帧，出 png / 从开头到这帧的视频 / 这帧的
kra / psd" - the timelapse's time axis is STROKE COUNT, not wall
clock: playback frames are reveal-sequence prefixes (render_timelapse),
with ~1s holds at layer ends and a ~3s final hold. The mp4 was
rendered from a plan (the GUI exports <stem>_work/_edit.json through
daub_paint --timelapse), and the engine is deterministic - so the
timeline is fully reconstructible and any moment can be re-rendered
exactly:

  1. rebuild the reveal sequence + prefix table + playback timeline
     (the same helpers render_timelapse.run() plays - one source of
     truth for the stroke axis), t -> playback frame floor(t*fps);
  2. frame png: re-render seq[:K] fresh - the mp4 frame is a crf20
     re-encode of exactly this (verified below), the re-render is the
     lossless truth;
  3. cut mp4: frames 0..j inclusive, frame-exact via -frames:v (the
     video is VFR - container frame rates lie, timestamps stretch);
  4. .kra/.psd: the truncated plan (header verbatim, strokes=seq[:K],
     write_prefix discipline) rendered with --kra/--psd in one engine
     pass - the layered state of that very moment;
  5. verify: the reveal frame split is a per-layer sqrt, the same
     multiset under both reveal orders - so the timeline LENGTH is
     usually identical and canNOT pin the order; when --order isn't
     given the two candidates duel at frame j (render both, keep the
     one that matches the mp4 pixels). Afterwards ours must sit far
     below the crf20 ceiling, and a neighbor frame may only win
     CLEARLY (holds duplicate the frame - x264 noise makes ties look
     like coin-flip argmins; real off-by-ones win by a wide margin).

Usage:
  python tools/daub_frame.py <video.mp4> <seconds> [--out-dir DIR]
        [--plan PLAN.json] [--order auto|big_first|small_first]
        [--fps 15] [--reveal-frames 120] [--layer-hold 1.0]
        [--final-hold 3.0] [--no-cut] [--no-kra] [--no-psd]
        [--daub EXE]

Outputs (next to the video by default), for t=12 from
<X>_edit_timelapse.mp4:
  <X>_t12s_frame.png   the moment, lossless (engine re-render)
  <X>_t12s_cut.mp4     the video from the start through that frame
  <X>_t12s.kra / .psd  the layered state at that moment
  <X>_t12s_plan.json   the truncated plan (the moment's source)
"""

import argparse
import bisect
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render_timelapse as rt   # noqa: E402  (reveal truth + ffmpeg)

DAUB = rt.DAUB
CAL = rt.CAL
TIPS = rt.TIPS
TAIL = -1   # playback_timeline sentinel: the final hold (full plan)


class FrameError(RuntimeError):
    """A daub/ffmpeg step or a mapping check failed."""


def log(m):
    print(m, flush=True)


def resolve_ffprobe():
    ff = rt.resolve_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff), "ffprobe.exe")
        if os.path.isfile(cand):
            return cand
    return None


def muxed_frames(video):
    """Frame count of the muxed video, or None when ffprobe is absent
    or the container won't say."""
    fp = resolve_ffprobe()
    if fp is None:
        return None
    try:
        r = subprocess.run(
            [fp, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "json", video],
            capture_output=True, encoding="utf-8", errors="replace")
        n = json.loads(r.stdout)["streams"][0].get("nb_frames")
        return int(n) if n else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def playback_timeline(prefixes, bounds, fps, layer_hold, final_hold,
                      order):
    """Playback order as prefix indices (TAIL = the final hold),
    frame for frame what run()/_pipe_video play: every reveal frame
    once, +round(layer_hold*fps) repeats when a layer just finished,
    then the closing block - big_first plays the full-sequence frame
    into the final hold; small_first discards it (the reversed-stack
    composite is not the approved pixels) and holds the tail only.
    The tail block is round(final_hold*fps)+1: the pipe path really
    writes that extra frame and ffmpeg 8.x's concat repeats the last
    entry once - both encodes mux identically. Note the total is the
    SAME under both orders (the last prefix is always a layer end;
    its hold lives in the closing block either way) - frame counts
    can't pin the order, pixels can."""
    hold = round(layer_hold * fps)
    tail = round(final_hold * fps) + 1
    tl, li = [], 0
    for fi, k in enumerate(prefixes[:-1]):
        tl.append(fi)
        if k == bounds[li]:
            tl.extend([fi] * hold)
            li += 1
    last = len(prefixes) - 1
    if order == "small_first":
        tl.extend([TAIL] * (1 + hold + tail))
    else:
        tl.extend([last] * (1 + hold))
        tl.extend([TAIL] * tail)
    return tl


def grab_frame(ff, video, t, out_png):
    """One decoded frame at timestamp t (input seek decodes forward to
    the first frame with pts >= t - frame-accurate) as a png."""
    r = subprocess.run([ff, "-y", "-ss", "%.6f" % t, "-i", video,
                        "-frames:v", "1", out_png],
                       capture_output=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0 or not os.path.isfile(out_png):
        raise FrameError("ffmpeg frame grab at %.3fs failed:\n%s"
                         % (t, r.stderr[-1500:]))
    return out_png


def mean_abs_diff(a, b):
    try:
        from PIL import Image
        import numpy as np
    except ImportError as e:
        raise FrameError("PIL+numpy needed for the pixel checks "
                         "(run under pack_venv): %s" % e)
    A = np.asarray(Image.open(a).convert("RGB"), dtype=np.int16)
    B = np.asarray(Image.open(b).convert("RGB"), dtype=np.int16)
    if A.shape != B.shape:
        raise FrameError("frame sizes differ: %s vs %s"
                         % (A.shape, B.shape))
    return float(np.abs(A - B).mean())


def render_state(daub, d, seq, k, plan_path, out_png, kra=None, psd=None,
                 cal=CAL, tips=TIPS):
    """One engine render of the state at k strokes: plan header
    verbatim, strokes swapped for seq[:k] (write_prefix discipline -
    invent nothing), png + optional kra/psd in the same pass. cal/tips
    default to the dev-tree assets; the frozen GUI passes its own
    bundled copies."""
    rt.write_prefix(d, seq, k, plan_path)
    argv = [daub, "render", plan_path, "--out", out_png,
            "--cal", cal, "--tips", tips]
    if kra:
        argv += ["--kra", kra]
    if psd:
        argv += ["--psd", psd]
    r = subprocess.run(argv, capture_output=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise FrameError("daub render failed at %d strokes:\n%s\n%s"
                         % (k, r.stdout, r.stderr))
    return out_png


def state_at(cand, j, total_strokes):
    """(kind, seq_or_strokes, k) painted state at playback frame j of
    one candidate: a reveal prefix, or the final hold's full plan."""
    fi = cand["tl"][j]
    if fi == TAIL:
        return "tail", None, total_strokes
    return "prefix", cand["seq"], cand["prefixes"][fi]


def layer_report(reveal, bounds, k):
    """Human line for the state at k strokes: layers finished, the one
    in progress and its within-layer progress."""
    done = [L for i, L in enumerate(reveal) if bounds[i] <= k]
    idx = bisect.bisect_left(bounds, k)
    if idx >= len(reveal):
        return "all %d layers done" % len(reveal)
    prev = bounds[idx - 1] if idx else 0
    total = bounds[idx] - prev
    return ("done: %s | painting %s (%d/%d strokes of it)"
            % (",".join(done) or "-", reveal[idx], k - prev, total))


def parse_seconds(s):
    """12 / 12.5 / "12s" -> float (第12秒初 -> 12)."""
    return float(str(s).strip().rstrip("sS"))


def output_paths(video, t, out_dir=None, base=None):
    """The deliverable paths for one moment, before any rendering.
    `video` may be None (plan-only hosts pass their own base stem)."""
    vdir = os.path.dirname(video) if video else \
        os.path.abspath(out_dir or ".")
    if base is None:
        stem = os.path.splitext(os.path.basename(video or ""))[0]
        suffix = "_edit_timelapse"
        base = stem[:-len(suffix)] if stem.endswith(suffix) else stem
    outd = os.path.abspath(out_dir) if out_dir else vdir
    tag = "%g" % t
    j = lambda ext: os.path.join(outd, "%s_t%ss%s" % (base, tag, ext))
    return {"out_dir": outd, "base": base, "tag": tag,
            "frame_png": j("_frame.png"), "plan": j("_plan.json"),
            "kra": j(".kra"), "psd": j(".psd"), "cut_mp4": j("_cut.mp4")}


def run(video=None, seconds=None, plan=None, out_dir=None, base=None,
        order="auto", fps=15.0, reveal_frames=120, layer_hold=1.0,
        final_hold=3.0, do_cut=True, do_kra=True, do_psd=True,
        daub=DAUB, cal=CAL, tips=TIPS, verify="auto", log=None) -> dict:
    """The one entry every host rides (CLI main, MCP job, GUI worker,
    web backend thread). `video` may be None for plan-only hosts (the
    GUI workbench: the plan is the source and it KNOWS its reveal
    order - pass it, "auto" needs a video to duel against). `verify`:
    "auto" runs the pixel gross-mismatch guard (needs PIL+numpy);
    "skip" rides the frame-count pin alone - the honest choice where
    PIL is absent (the frozen GUI bundles numpy, not PIL), and it is
    logged, never silent. Returns the summary dict (also the CLI's
    JSON payload)."""
    lg = log or (lambda m: print(m, flush=True))
    if seconds is None:
        raise FrameError("seconds is required")
    t = parse_seconds(seconds)
    if t < 0:
        raise FrameError("seconds must be >= 0")
    if video is not None:
        video = os.path.abspath(video)
        if not os.path.isfile(video):
            raise FrameError("no such video: %s" % video)
    daub = os.path.abspath(daub)
    if not os.path.isfile(daub):
        raise FrameError("daub engine not found: %s (build "
                         "target/release or pass --daub/--daub EXE)" % daub)
    ff = None
    if video is not None:
        ff = rt.resolve_ffmpeg()
        if ff is None:
            raise FrameError("ffmpeg not found (set DAUB_FFMPEG or add "
                             "ffmpeg to PATH)")

    p = output_paths(video, t, out_dir=out_dir, base=base)
    outd, tag = p["out_dir"], p["tag"]
    os.makedirs(outd, exist_ok=True)

    plan_path = plan
    if plan_path is None:
        if video is None:
            raise FrameError("plan-only mode needs the plan")
        vdir = os.path.dirname(video)
        edit = os.path.join(vdir, p["base"] + "_work", "_edit.json")
        orig = os.path.join(vdir, p["base"] + "_plan.json")
        if os.path.isfile(edit):
            plan_path = edit       # what the GUI's video actually rode
        elif os.path.isfile(orig):
            plan_path = orig
            lg("note: no %s - using the original plan (workbench "
               "mutes/presets, if any, won't be reflected)"
               % os.path.basename(edit))
        else:
            raise FrameError("no plan found: pass --plan (looked for %s "
                             "and %s)" % (edit, orig))
    d = rt.load_plan(plan_path)
    total = len(d["strokes"])
    lg("plan: %s (%d strokes, canvas %s)"
       % (os.path.basename(plan_path), total, d.get("canvas")))

    # ---- rebuild the timeline per reveal-order candidate
    if order == "auto" and video is None:
        raise FrameError("plan-only mode needs an explicit reveal order "
                         "(big_first | small_first) - the exporter knows "
                         "it; 'auto' pins the order against video pixels")
    want_orders = (("big_first", "small_first")
                   if order == "auto" else (order,))
    cands = {}
    for o in want_orders:
        seq, bounds, reveal = rt.reveal_sequence(d, order=o)
        prefixes, _per = rt.prefix_list(seq, bounds,
                                        reveal_frames=reveal_frames)
        tl = playback_timeline(prefixes, bounds, fps,
                               layer_hold, final_hold, o)
        cands[o] = {"seq": seq, "bounds": bounds, "reveal": reveal,
                    "prefixes": prefixes, "tl": tl}
        lg("order %s: %d reveal prefixes, %d layers, timeline %d "
           "frames" % (o, len(prefixes), len(bounds), len(tl)))

    j = int(math.floor(t * fps))
    live = {o: c for o, c in cands.items() if j < len(c["tl"])}
    if not live:
        raise FrameError("t=%.2fs is past the video: %d frames = %.2fs "
                         "at --fps %g (frame %d)"
                         % (t, len(cands[want_orders[0]]["tl"]),
                            len(cands[want_orders[0]]["tl"]) / fps,
                            fps, j))

    # ---- pin the order: explicit wins (with a count-pin guard when a
    # video rides along); otherwise frame count when it discriminates
    # (rare - the totals usually tie), else the pixel duel at frame j
    nb = muxed_frames(video) if video is not None else None
    if want_orders[0] != "auto":
        order = want_orders[0]
        if video is not None and nb is not None \
                and nb != len(cands[order]["tl"]):
            raise FrameError(
                "video has %d frames but this plan's %s timeline is %d - "
                "the video was not rendered from this plan at this "
                "cadence; refusing to guess (check --plan/--fps/...)"
                % (nb, order, len(cands[order]["tl"])))
    else:
        order = None
        if len(live) == 2 and nb is not None:
            hit = [o for o in live if len(live[o]["tl"]) == nb]
            if len(hit) == 1:
                order = hit[0]
                lg("order pinned by frame count (%d muxed): %s"
                   % (nb, order))
        if order is None and len(live) == 2:
            lg("dueling orders in pixels at frame %d (muxed total %s)"
               % (j, nb))
            chk = os.path.join(outd, "_order_check.png")
            duel_plan = os.path.join(outd, "_order_duel_plan.json")
            grab_frame(ff, video, j / fps, chk)
            scores = {}
            for o, c in live.items():
                _kind, seq_o, k_o = state_at(c, j, total)
                png_o = os.path.join(outd, "_order_duel_%s.png" % o)
                render_state(daub, d, seq_o if seq_o is not None
                             else d["strokes"], k_o, duel_plan, png_o)
                scores[o] = mean_abs_diff(png_o, chk)
                os.remove(png_o)
                lg("  %s: mean|diff|=%.3f" % (o, scores[o]))
            try:
                os.remove(chk)
                os.remove(duel_plan)
            except OSError:
                pass
            order = min(scores, key=scores.get)
            lose = max(scores.values())
            if lose < scores[order] * 1.2:
                lg("duel nearly tied (%.3f vs %.3f) - both orders agree "
                   "visually at this moment; picking %s"
                   % (scores[order], lose, order))
            else:
                lg("order %s wins the duel (%.3f vs %.3f)"
                   % (order, scores[order], lose))
        if order is None:
            order = next(iter(live))

    c = live[order]
    seq, bounds, reveal, prefixes, tl = (c["seq"], c["bounds"],
                                         c["reveal"], c["prefixes"],
                                         c["tl"])
    lg("moment: t=%ss -> playback frame %d/%d (order %s)"
       % (tag, j, len(tl) - 1, order))

    # ---- render the moment (png + kra/psd in one pass)
    kind, seq_k, k = state_at(c, j, total)
    out_png, plan_out = p["frame_png"], p["plan"]
    kra = p["kra"] if do_kra else None
    psd = p["psd"] if do_psd else None
    cut = p["cut_mp4"] if (do_cut and video is not None) else None

    if kind == "tail":
        lg("state: final hold - full plan, %d strokes (%s)"
           % (k, layer_report(reveal, bounds, k)))
        render_state(daub, d, d["strokes"], k, plan_out, out_png,
                     kra, psd, cal, tips)
        truth = None
        if video is not None:
            truth = os.path.join(os.path.dirname(video),
                                 p["base"] + "_work", "_video_final.png")
        if truth and os.path.isfile(truth) and verify == "auto":
            m = mean_abs_diff(out_png, truth)
            lg("determinism: re-render vs the video's own final "
               "png: mean|diff|=%.4f%s"
               % (m, "" if m == 0 else " (expected 0!)"))
    else:
        lg("state: reveal frame %d -> %d/%d strokes (%s)"
           % (tl[j] if kind == "prefix" else -1, k, len(seq),
              layer_report(reveal, bounds, k)))
        render_state(daub, d, seq, k, plan_out, out_png, kra, psd,
                     cal, tips)
    for q in (out_png, plan_out) + tuple(x for x in (kra, psd) if x):
        lg("wrote %s (%.1fMB)"
           % (os.path.basename(q), os.path.getsize(q) / 1e6))

    # ---- verify the moment (gross-mismatch guard; exactness is the
    # frame-count pin + determinism, see the module docstring). skipped
    # hosts log it - the skip itself stays visible in the job log.
    ours = None
    if video is None or verify == "skip":
        if video is not None:
            lg("verify: pixel check skipped (verify=skip) - exactness "
               "rides the frame-count pin")
    else:
        chk = [os.path.join(outd, "_frame_check_%d.png" % x)
               for x in (-1, 0, 1)]
        means = {}
        for off, path in zip((-1, 0, 1), chk):
            jt = j + off
            if jt < 0 or jt >= len(tl):
                continue
            grab_frame(ff, video, jt / fps, path)
            means[off] = mean_abs_diff(out_png, path)
            lg("check: mp4 frame %d vs ours: mean|diff|=%.3f"
               % (jt, means[off]))
        for path in chk:
            try:
                os.remove(path)
            except OSError:
                pass
        ours = means.get(0)
        if ours is None:
            raise FrameError("our own frame failed to extract")
        clear = {o: m for o, m in means.items() if m < ours * 0.6}
        if clear:
            raise FrameError(
                "mapping looks OFF BY ONE: neighbor %s matches the mp4 "
                "far better (mean %.3f vs ours %.3f) - the timeline "
                "reconstruction disagrees with the video, investigate "
                "before trusting these files" % (clear, min(
                    clear.values()), ours))
        if ours >= 12.0:
            raise FrameError(
                "mean|diff|=%.3f - way above the crf20 ceiling (~2-6); "
                "this moment is not from this video" % ours)
        lg("verified: mean|diff|=%.3f (lossy ceiling ~2-6); neighbors "
           "%s - no off-by-one"
           % (ours, {o: round(m, 3) for o, m in means.items()
                     if o != 0}))

    # ---- the cut: frame-exact, -frames:v j+1 = frames 0..j inclusive
    # (the VFR container makes -t 12 boundary-ambiguous at t=12.0)
    if cut is not None:
        r = subprocess.run(
            [ff, "-y", "-i", video, "-frames:v", str(j + 1),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", cut],
            capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise FrameError("ffmpeg cut failed:\n%s" % r.stderr[-1500:])
        got = muxed_frames(cut)
        lg("wrote %s (%.1fMB, %s frames - asked for %d)"
           % (os.path.basename(cut), os.path.getsize(cut) / 1e6,
              got if got is not None else "?", j + 1))
        if got is not None and got != j + 1:
            raise FrameError("cut frame count %s != %d" % (got, j + 1))

    return {
        "video": video, "plan_in": os.path.abspath(plan_path),
        "t": t, "frame": j, "order": order,
        "strokes": k, "of": total,
        "state": layer_report(reveal, bounds, k),
        "frame_png": out_png, "cut_mp4": cut,
        "kra": kra, "psd": psd, "plan": plan_out,
        "mean_diff_vs_mp4": ours,
    }


def main():
    ap = argparse.ArgumentParser(
        prog="daub_frame",
        description="one moment of a paint-down timelapse as png / "
                    "cut mp4 / layered kra / psd")
    ap.add_argument("video")
    ap.add_argument("seconds", type=str,
                    help="the moment, e.g. 12 or 12.5 (a trailing 's' "
                         "is fine - 第12秒初 -> 12)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--plan", default=None,
                    help="plan the video was rendered from (default: "
                         "<stem>_work/_edit.json, else <stem>_plan.json)")
    ap.add_argument("--order", default="auto",
                    choices=("auto", "big_first", "small_first"))
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--reveal-frames", type=int, default=120)
    ap.add_argument("--layer-hold", type=float, default=1.0)
    ap.add_argument("--final-hold", type=float, default=3.0)
    ap.add_argument("--no-cut", action="store_true")
    ap.add_argument("--no-kra", action="store_true")
    ap.add_argument("--no-psd", action="store_true")
    ap.add_argument("--daub", default=DAUB)
    args = ap.parse_args()
    try:
        res = run(video=args.video, seconds=args.seconds, plan=args.plan,
                  out_dir=args.out_dir, order=args.order, fps=args.fps,
                  reveal_frames=args.reveal_frames,
                  layer_hold=args.layer_hold,
                  final_hold=args.final_hold, do_cut=not args.no_cut,
                  do_kra=not args.no_kra, do_psd=not args.no_psd,
                  daub=args.daub)
    except FrameError as e:
        sys.exit(str(e))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
