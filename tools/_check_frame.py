"""_check_frame.py - smoke for daub_frame (tools/daub_frame.py).

  1. reveal_sequence/prefix_list unit invariants + equivalence with the
     pre-refactor inline code (they moved out of render_timelapse.run -
     one source of truth for the timelapse stroke axis)
  2. playback_timeline: totals match the engine/concat frame formula
     for both orders; holds anchor exactly on layer-end frames;
     small_first discards the last prefix frame
  3. E2E: a tiny real plan (scaled-down slices of a real one) ->
     render_timelapse.run() makes the mp4 -> daub_frame CLI pulls
     t=1.0s (a layer-end hold) and t=6.5s (the final hold): files land,
     the pixel argmin verification passes, the cut is frame-exact and
     the tail state equals the full plan with a byte-stable re-render.
  4. Library form (2u/3h-3k): parse_seconds / output_paths naming,
     run() with explicit order (duel skipped), plan-only mode (GUI
     host: no cut, no verify, explicit order required), verify="skip".

Run:  pack_venv/Scripts/python.exe tools/_check_frame.py [real_plan.json]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render_timelapse as rt   # noqa: E402

PY = sys.executable
LEGS = []


def leg(n, ok, detail=""):
    LEGS.append(bool(ok))
    print("  %s. %s%s" % (n, "PASS" if ok else "FAIL",
                          " - " + detail if detail else ""))
    if not ok:
        sys.exit(1)


# a small two-layer plan with hand-checkable numbers
def mini_plan():
    return {
        "canvas": [200, 150],
        "bg": [245, 240, 225],
        "seed": 7,
        "count": 13,
        "strokes": (
            [{"layer": "F1", "preset": "b) Basic-2 Opacity",
              "size": 10 + i, "opacity": 1.0, "color": "#a04040",
              "points": [[10.0 + 5 * i, 20.0 + 3 * i, 1.0],
                         [40.0 + 5 * i, 30.0 + 3 * i, 1.0]]}
             for i in range(8)]
            + [{"layer": "L2", "preset": "b) Basic-2 Opacity",
                "size": 4 + i, "opacity": 1.0, "color": "#4040a0",
                "points": [[60.0 + 4 * i, 60.0 + 2 * i, 1.0],
                           [90.0 + 4 * i, 70.0 + 2 * i, 1.0]]}
               for i in range(5)]
        ),
    }


def old_reveal_and_prefixes(d, order, reveal_frames):
    """render_timelapse.run()'s inline block, byte for byte, before the
    reveal_sequence/prefix_list extraction - the equivalence anchor
    for seq/bounds/reveal. Prefix VALUES deliberately diverge on the
    degenerate k=0 case (tiny first layer): prefix_list now skips it,
    the engine refuses zero-stroke prefixes (09-10, real crash)."""
    strokes = d["strokes"]
    stack = rt.layer_names(d)
    groups = {L: [] for L in stack}
    for s in strokes:
        groups[s["layer"]].append(s)
    reveal = list(reversed(stack)) if order == "small_first" else stack
    seq, bounds = [], []
    for L in reveal:
        seq.extend(sorted(groups[L], key=lambda s: s["size"]))
        bounds.append(len(seq))
    w = [rt.math.sqrt(b - p) for p, b in
         zip([0] + bounds[:-1], bounds)]
    per = [max(2, round(reveal_frames * x / sum(w))) for x in w]
    prefixes = []
    for p, b, n in zip([0] + bounds[:-1], bounds, per):
        for i in range(1, n + 1):
            k = p + (b - p) * i // n
            if not prefixes or k > prefixes[-1]:
                prefixes.append(k)
    prefixes[-1] = len(seq)
    return seq, bounds, reveal, prefixes


d = mini_plan()
print("1. reveal_sequence / prefix_list invariants")
for order in ("big_first", "small_first"):
    seq, bounds, reveal = rt.reveal_sequence(d, order=order)
    oseq, ob, orev, opref = old_reveal_and_prefixes(d, order, 120)
    leg("1a%s" % order[0],
        seq == oseq and bounds == ob and reveal == orev,
        "seq/bounds/reveal identical to pre-refactor")
    prefixes, per = rt.prefix_list(seq, bounds, reveal_frames=120)
    leg("1b%s" % order[0],
        all(prefixes[i] < prefixes[i + 1]
            for i in range(len(prefixes) - 1))
        and prefixes[-1] == len(seq) and len(prefixes) >= len(bounds)
        and all(b in prefixes for b in bounds)
        and prefixes[0] >= 1,
        "%d prefixes, strictly increasing from 1, layer ends land on "
        "frames" % len(prefixes))
    # per-layer order: sizes ascending within each layer chunk
    chunks_ok = True
    for L in set(s["layer"] for s in seq):
        sizes = [s["size"] for s in seq if s["layer"] == L]
        chunks_ok &= sizes == sorted(sizes)
    leg("1c%s" % order[0], chunks_ok, "per-layer sizes ascending")

print("2. playback_timeline frame accounting")
import daub_frame   # noqa: E402

for order in ("big_first", "small_first"):
    seq, bounds, reveal = rt.reveal_sequence(d, order=order)
    prefixes, _ = rt.prefix_list(seq, bounds, reveal_frames=120)
    tl = daub_frame.playback_timeline(prefixes, bounds, 15, 1.0, 3.0,
                                      order)
    want = (len(prefixes) + len(bounds) * round(1.0 * 15)
            + round(3.0 * 15) + 1)
    leg("2a%s" % order[0], len(tl) == want,
        "%d frames == prefixes + layer holds + final hold(+1) formula"
        % len(tl))
    leg("2b%s" % order[0], tl.count(daub_frame.TAIL) ==
        round(3.0 * 15) + 1 + (round(1.0 * 15) + 1
                               if order == "small_first" else 0),
        "tail block count right (small_first absorbs the last prefix)")
    # every layer-end prefix frame is held: its fi appears 1+hold times
    hold = round(1.0 * 15)
    ends = [i for i, k in enumerate(prefixes[:-1]) if k in bounds]
    anchors_ok = all(tl.count(i) == 1 + hold for i in ends)
    leg("2c%s" % order[0], anchors_ok,
        "%d layer-end frames each held %d ticks" % (len(ends), hold))

print("2u. parse_seconds / output_paths units")
leg("2u", [daub_frame.parse_seconds(x) for x in
           (12, 12.5, "12", "12.5", " 12s ", "12S")]
    == [12.0, 12.5, 12.0, 12.5, 12.0, 12.0],
    "seconds parsing: int/float/str/trailing-s")
pp = daub_frame.output_paths(
    os.path.join("C:", os.sep, "vid", "demo_edit_timelapse.mp4"), 12)
leg("2v", pp["base"] == "demo" and pp["tag"] == "12"
    and pp["frame_png"].endswith("demo_t12s_frame.png")
    and pp["plan"].endswith("demo_t12s_plan.json")
    and pp["kra"].endswith("demo_t12s.kra")
    and pp["psd"].endswith("demo_t12s.psd")
    and pp["cut_mp4"].endswith("demo_t12s_cut.mp4"),
    "output_paths: _edit_timelapse stem stripped, %g tag")
pp2 = daub_frame.output_paths(None, 0.5, out_dir="x", base="job1")
leg("2w", pp2["base"] == "job1" and pp2["tag"] == "0.5"
    and pp2["frame_png"].endswith(
        os.path.join("x", "job1_t0.5s_frame.png")),
    "output_paths: plan-only host (video=None, explicit base)")

print("3. E2E: tiny plan -> timelapse mp4 -> daub_frame")
if len(sys.argv) > 1:
    real = rt.load_plan(sys.argv[1])
    # scaled-down slices of a real plan: real stroke schema, tiny canvas
    sx, sy = 96.0 / real["canvas"][0], 144.0 / real["canvas"][1]
    picks, seen = [], {}
    for s in real["strokes"]:
        seen[s["layer"]] = seen.get(s["layer"], 0) + 1
        if seen[s["layer"]] <= 12:
            picks.append(dict(
                s,
                size=max(1.0, s["size"] * sx),
                points=[[x * sx, y * sy, p] for x, y, p in s["points"]]))
        if len(seen) >= 2 and all(v >= 12 for v in seen.values()):
            break
    d2 = dict(real, canvas=[96, 144], strokes=picks,
              count=len(picks))
    log2 = []
    tmp = tempfile.mkdtemp(prefix="daub_frame_")
    mp4 = os.path.join(tmp, "tiny_edit_timelapse.mp4")
    rt.run(d2, os.path.join(tmp, "final.png"), mp4,
           workdir=os.path.join(tmp, "tiny_edit_timelapse_frames"),
           log=log2.append)
    # the pipe path never writes final_png (daub_paint renders it
    # separately) - do the same, then mimic the GUI layout so the tail
    # leg exercises the determinism check against _video_final.png
    final = os.path.join(tmp, "final.png")
    rt.render_prefix(rt.DAUB, d2, d2["strokes"], len(d2["strokes"]),
                     os.path.join(tmp, "_fp.json"), final,
                     cal=rt.CAL, tips=rt.TIPS)
    os.makedirs(os.path.join(tmp, "tiny_work"), exist_ok=True)
    shutil.copyfile(final,
                    os.path.join(tmp, "tiny_work", "_video_final.png"))
    leg("3a", os.path.isfile(mp4) and os.path.getsize(mp4) > 1000,
        "tiny timelapse rendered (%d frames of log)"
        % len([x for x in log2 if x.startswith("video ")]))

    def run_frame(t, extra=()):
        r = subprocess.run(
            [PY, os.path.join(HERE, "daub_frame.py"), mp4, str(t),
             *extra],
            capture_output=True, encoding="utf-8", errors="replace")
        return r

    plan_file = os.path.join(tmp, "tiny_plan_src.json")
    with open(plan_file, "w", encoding="utf-8") as fh:
        json.dump(d2, fh)

    r = run_frame(1.0, ("--plan", plan_file))
    out = r.stdout
    leg("3b", r.returncode == 0 and '"strokes": 12' in out,
        "t=1.0s -> layer-end hold, F1's 12 strokes (rc=%d)"
        % r.returncode)
    if r.returncode != 0:
        print(r.stdout[-1500:], r.stderr[-1500:])
        sys.exit(1)
    js = json.loads(out[out.rindex("{"):])
    leg("3c", all(os.path.isfile(js[f]) and
                  os.path.getsize(js[f]) > 0
                  for f in ("frame_png", "cut_mp4", "kra", "psd",
                            "plan")),
        "png/cut/kra/psd/plan all landed")
    leg("3d", js["mean_diff_vs_mp4"] < 6.0,
        "moment verified vs mp4 pixels (mean|diff|=%.3f, argmin)"
        % js["mean_diff_vs_mp4"])

    r = run_frame(6.5, ("--plan", plan_file))
    out = r.stdout
    leg("3e", r.returncode == 0 and '"strokes": 24' in out,
        "t=6.5s -> final hold = the full plan (rc=%d)" % r.returncode)
    if r.returncode != 0:
        print(out[-1500:], r.stderr[-1500:])
        sys.exit(1)
    leg("3f", "mean|diff|=0.0000" in out,
        "tail re-render byte-stable vs the video's own final png")

    r = run_frame(99, ("--plan", plan_file))
    leg("3g", r.returncode != 0 and "past the video" in r.stderr,
        "t past the end fails loud with the duration in the message")

    # library form - the host entries (MCP fn-job / GUI worker / web
    # thread) all ride daub_frame.run() directly
    lg3 = []
    res = daub_frame.run(video=mp4, seconds=2.0, plan=plan_file,
                         order="small_first", log=lg3.append)
    leg("3h", os.path.isfile(res["frame_png"])
        and os.path.isfile(res["kra"]) and os.path.isfile(res["psd"])
        and os.path.isfile(res["cut_mp4"])
        and res["order"] == "small_first"
        and res["plan_in"] == os.path.abspath(plan_file),
        "run(): explicit order - same files, duel skipped "
        "(mean|diff|=%.3f)" % res["mean_diff_vs_mp4"])

    res2 = daub_frame.run(video=None, seconds=2.0, plan=plan_file,
                          order="big_first", out_dir=tmp, base="tiny",
                          log=lg3.append)
    leg("3i", os.path.isfile(res2["frame_png"])
        and res2["cut_mp4"] is None and res2["video"] is None
        and res2["mean_diff_vs_mp4"] is None,
        "run() plan-only: png/kra/psd land, no cut, no verify")

    try:
        daub_frame.run(video=None, seconds=2.0, plan=plan_file,
                       log=lg3.append)
        leg("3j", False, "plan-only with order='auto' must fail loud")
    except daub_frame.FrameError as e:
        leg("3j", "explicit reveal order" in str(e),
            "plan-only with order='auto' fails loud")

    res3 = daub_frame.run(video=mp4, seconds=2.0, plan=plan_file,
                          order="small_first", verify="skip",
                          log=lg3.append)
    leg("3k", res3["mean_diff_vs_mp4"] is None
        and any("skipped" in x for x in lg3),
        "verify='skip': pixel legs skipped, skip logged loud")

    with open(res["plan"], encoding="utf-8") as fh:
        trj = json.load(fh)
    leg("3l", trj.get("count") == len(trj["strokes"]) == res["strokes"],
        "truncated plan: count header moves WITH the strokes (%d)"
        % res["strokes"])

    print("ALL PASS - %s" % tmp)
