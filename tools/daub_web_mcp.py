"""daub_web_mcp: MCP stdio server - the daub web studio, drivable by AI.

Lets an MCP client paint the way a human would at the studio's URL:
upload a reference, submit a job, watch strokes grow, pull the finished
PNG (and .kra/.psd/.mp4) back to local disk. The human-free path into
the web atelier - same auth gate, same nginx rate limits, same queue
as browser users; no back door.

Zero third-party dependencies: the MCP stdio transport is just
newline-delimited JSON-RPC 2.0 (hand-rolled here, mirrored from
daub_mcp.py) and the HTTP client is urllib - runs on any interpreter
with the stdlib. Protocol traffic goes to stdout; diagnostics to stderr.

Run (any cwd):
  pack_venv/Scripts/python.exe tools/daub_web_mcp.py

Register (Claude Code, user scope):
  claude mcp add -s user -e DAUB_WEB_URL=https://your-studio ^
    -e DAUB_WEB_AUTH=user:password daub-web -- ^
    python /path/to/daub/tools/daub_web_mcp.py

Environment:
  DAUB_WEB_URL   studio base URL, default http://127.0.0.1:8787
  DAUB_WEB_AUTH  "user:password" - REQUIRED, fail loud without it
  DAUB_WEB_TLS   "verify" to validate certs (real-cert era); default
                 accepts the self-signed pair
  DAUB_WEB_OUT   download root, default ~/daub-web-out

Tools:
  daub_web_generate - reference image -> job -> poll -> download PNG
                      (+ .kra/.psd/.mp4/plan on request). The one-call
                      "paint this" tool; returns local file paths.
  daub_web_frame    - one timelapse moment of a finished mp4-capable
                      job -> png / cut mp4 / layered kra/psd / truncated
                      plan (async, downloads everything)
  daub_web_status   - one job's snapshot (state / strokes / artifacts)
  daub_web_fetch    - download one artifact or live.png mid-render
  daub_web_gallery  - recent jobs, newest first (disk-backed, survives
                      restarts)
  daub_web_cancel   - cancel a queued/running job

Server-side facts this client honors (tools/daub_web.py):
  - POST /api/job takes the RAW image bytes, options in the query
    string; tier caps: fast 1400 / std 2400 / full 0 (uncapped);
  - upload guard: png/jpeg/webp only, edge <= 8192, <= 48 MB;
  - jobs continue server-side even if this process dies - a timed-out
    generate is recoverable via daub_web_status + daub_web_fetch;
  - rate limits: 30 r/s per IP overall, 2 r/s on submits - the client
    polls at 2 s and never bursts, but a 429 is surfaced verbatim.
"""

import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVER_INFO = {"name": "daub-web", "version": "0.1.0"}
PROTOCOL = "2025-06-18"

TIER_MAXDIM = {"fast": 1400, "std": 2400, "full": 0}   # mirror the server
MAX_UPLOAD = 48 * 1024 * 1024
JID_RX = re.compile(r"^[0-9a-f]{1,16}$")
POLL_S = 2.0

# ---------------------------------------------------------------- stderr log


def log(*a):
    print("[daub_web_mcp]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------- config


def _cfg():
    url = (os.environ.get("DAUB_WEB_URL") or
           "http://127.0.0.1:8787").rstrip("/")
    auth = os.environ.get("DAUB_WEB_AUTH") or ""
    if ":" not in auth:
        raise RuntimeError(
            "DAUB_WEB_AUTH not set - export it as 'user:password' "
            "(the pair from the server's /etc/daub-web.env)")
    out = os.environ.get("DAUB_WEB_OUT") or os.path.join(
        os.path.expanduser("~"), "daub-web-out")
    verify = os.environ.get("DAUB_WEB_TLS") == "verify"
    return url, auth, out, verify


_CTX = None


def _context(url, verify):
    global _CTX
    if url.startswith("http://"):
        return None
    if verify:
        return ssl.create_default_context()
    if _CTX is None:                     # self-signed era: accept the pair
        _CTX = ssl._create_unverified_context()
    return _CTX


def _http(url, auth, verify, method, path, data=None, ctype=None,
          timeout=120):
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(auth.encode("utf-8")).decode("ascii"))
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_context(url, verify)) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()[:300]
        except Exception:
            pass
        try:
            msg = json.loads(body.decode("utf-8", "replace")).get(
                "error", body.decode("utf-8", "replace"))
        except ValueError:
            msg = body.decode("utf-8", "replace") or e.reason
        if e.code == 401:
            raise RuntimeError(
                "401 auth failed - check DAUB_WEB_AUTH (user:password)") \
                from None
        if e.code == 429:
            raise RuntimeError(
                "429 rate limited (30 r/s overall, 2 r/s submits) - "
                "slow down and retry") from None
        if e.code == 503:
            raise RuntimeError("503 %s" % msg) from None
        raise RuntimeError("%s %s: %s" % (e.code, e.reason, msg)) from None
    except urllib.error.URLError as e:
        raise RuntimeError("cannot reach %s: %s" % (url, e.reason)) from None


def _json(method, path, data=None, ctype=None):
    url, auth, _out, verify = _cfg()
    raw = _http(url, auth, verify, method, path, data=data, ctype=ctype)
    return json.loads(raw.decode("utf-8"))


def _download(path, dest):
    url, auth, _out, verify = _cfg()
    raw = _http(url, auth, verify, "GET", path, timeout=600)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(raw)
    return len(raw), dest


# -------------------------------------------------------------------- tools


def _check_image(image):
    p = os.path.abspath(os.path.expanduser(image))
    if not os.path.isfile(p):
        raise RuntimeError("no such image: %s" % p)
    ext = p.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise RuntimeError(
            "unsupported format .%s (png/jpeg/webp only - the server's "
            "upload guard will reject the rest)" % ext)
    size = os.path.getsize(p)
    if not 0 < size <= MAX_UPLOAD:
        raise RuntimeError("image size %d bytes (server cap %d)"
                           % (size, MAX_UPLOAD))
    with open(p, "rb") as fh:
        return p, fh.read()


def _validate_jid(jid):
    if not JID_RX.match(jid or ""):
        raise RuntimeError("bad jid %r (hex, <=16 chars)" % jid)


def tool_generate(args, progress=None):
    image = args.get("image")
    if not image:
        raise RuntimeError("image (local path) is required")
    tier = args.get("tier") or "std"
    if tier not in TIER_MAXDIM:
        raise RuntimeError("tier must be one of %s" % sorted(TIER_MAXDIM))
    path, blob = _check_image(image)

    q = {"name": args.get("name") or os.path.splitext(
             os.path.basename(path))[0][:60],
         "tier": tier}
    if args.get("pen"):
        q["pen"] = str(args["pen"])
    want = {k: bool(args.get(k)) for k in ("kra", "psd", "mp4", "plan")}
    for k, v in want.items():
        if k != "plan" and v:            # plan has no server want-flag
            q[k] = "1"
    q["layers"] = "0" if args.get("layers") is False else "1"

    t0 = time.time()
    jid = _json("POST", "/api/job?" + urllib.parse.urlencode(q),
                data=blob, ctype="application/octet-stream")["id"]
    _validate_jid(jid)
    log("job %s submitted (%d bytes ref)" % (jid, len(blob)))

    wait_s = min(float(args.get("wait_s") or 900), 3600)
    while True:
        snap = _json("GET", "/api/job/%s/status" % jid)["job"]
        state = snap.get("state")
        if progress is not None:
            progress(min((time.time() - t0) / wait_s, 0.95),
                     "%s · %s strokes" % (snap.get("phase") or state,
                                          snap.get("strokes") or 0))
        if state in ("done", "fail", "cancelled"):
            break
        if time.time() - t0 > wait_s:
            return {"jid": jid, "state": state,
                    "note": "still rendering server-side - poll with "
                            "daub_web_status, then daub_web_fetch "
                            "(jobs survive this process)",
                    "elapsed_s": round(time.time() - t0, 1)}
        time.sleep(POLL_S)

    if snap.get("state") != "done":
        return {"jid": jid, "state": snap.get("state"),
                "error": snap.get("error") or snap.get("layers_err"),
                "elapsed_s": round(time.time() - t0, 1)}

    out_dir = os.path.abspath(os.path.expanduser(
        args.get("out_dir") or _cfg()[2]))
    jdir = os.path.join(out_dir, jid)
    files = {}
    size, files["png"] = _download("/api/artifact/%s/%s"
                                   % (jid, snap["artifacts"]["png"]),
                                   os.path.join(
                                       jdir, snap["artifacts"]["png"]))
    for kind, flag in (("kra", want["kra"]), ("psd", want["psd"]),
                       ("mp4", want["mp4"]), ("plan", want["plan"])):
        base = snap["artifacts"].get(kind)
        if flag and base:
            _download("/api/artifact/%s/%s" % (jid, base),
                      os.path.join(jdir, base))
            files[kind] = os.path.join(jdir, base)
    return {"jid": jid, "state": "done",
            "strokes": snap.get("strokes"),
            "canvas": snap.get("canvas"),
            "elapsed_s": round(time.time() - t0, 1),
            "png_bytes": size,
            "files": files,
            "note": "Read the png to see the painting"}


def tool_status(args):
    jid = args.get("jid") or ""
    _validate_jid(jid)
    return _json("GET", "/api/job/%s/status" % jid)["job"]


def tool_fetch(args):
    jid = args.get("jid") or ""
    _validate_jid(jid)
    what = args.get("file") or ""
    if what not in ("live.png", "ref.png"):
        if "/" in what or "\\" in what or not what:
            raise RuntimeError(
                "file must be an artifact basename (see daub_web_status "
                "artifacts) or live.png / ref.png")
    dest = os.path.abspath(os.path.expanduser(args.get("out_path") or
                              os.path.join(_cfg()[2], jid, what)))
    if what in ("live.png", "ref.png"):
        size, dest = _download("/api/job/%s/%s" % (jid, what), dest)
    else:
        size, dest = _download("/api/artifact/%s/%s" % (jid, what), dest)
    return {"jid": jid, "file": what, "bytes": size, "saved": dest}


def tool_edit(args):
    """Per-layer size ruler on a finished web job: sizes {layer:
    multiplier} -> the server re-renders an edited layer set (sibling
    edit plan, e_-prefixed PNGs; the original is never overwritten).
    Empty sizes = reset. The POST is synchronous server-side - when it
    returns, the edited set is live."""
    jid = args.get("jid") or ""
    _validate_jid(jid)
    sizes = args.get("sizes") or {}
    if not isinstance(sizes, dict):
        raise RuntimeError("sizes must be an object {layer: multiplier}")
    body = json.dumps({"sizes": sizes}, ensure_ascii=False).encode("utf-8")
    r = _json("POST", "/api/job/%s/edit" % jid, data=body,
              ctype="application/json")
    mf = r.get("layers") or {}
    return {"jid": jid, "edit": r.get("edit") or {},
            "layers": [{"name": L.get("name"), "png": L.get("png")}
                       for L in (mf.get("layers") or [])],
            "note": "edited set is live on the studio workbench; "
                    "e_-prefixed layer PNGs ride /api/job/<jid>/layer/, "
                    "the edited plan is the plan_edit artifact"}


def tool_frame(args, progress=None):
    """定格取材 on a finished web job: one moment of its timelapse as
    frame png / cut mp4 / layered kra+psd / truncated plan. Submit,
    poll the job status (meta['frames'][tag]), download everything."""
    jid = args.get("jid") or ""
    _validate_jid(jid)
    if "seconds" not in args:
        raise RuntimeError("seconds is required (e.g. 12 or '12s')")
    t = float(str(args["seconds"]).strip().rstrip("sS"))
    if not 0 <= t <= 7200:
        raise RuntimeError("seconds out of range (0..7200)")
    qs = {"t": t}
    # companion-export toggles (frame png + truncated plan unconditional)
    for k in ("cut", "kra", "psd"):
        if k in args:
            qs[k] = 1 if args[k] else 0
    r = _json("POST", "/api/job/%s/frame?%s"
              % (jid, urllib.parse.urlencode(qs)), data=b"",
              ctype="application/json")
    tag = r.get("tag", "%g" % t)
    log("frame %s@%ss -> %s" % (jid, tag, r.get("state")))

    wait_s = min(float(args.get("wait_s") or 300), 3600)
    t0 = time.time()
    while True:
        snap = _json("GET", "/api/job/%s/status" % jid)["job"]
        rec = (snap.get("frames") or {}).get(tag) or {}
        st = rec.get("state") or r.get("state") or "queued"
        if progress is not None:
            progress(min((time.time() - t0) / wait_s, 0.95),
                     "frame %ss · %s" % (tag, st))
        if st in ("done", "fail"):
            break
        if time.time() - t0 > wait_s:
            return {"jid": jid, "tag": tag, "state": st,
                    "note": "still extracting server-side - poll "
                            "daub_web_status, then daub_web_fetch the "
                            "frame_* artifacts (jobs survive this "
                            "process)"}
        time.sleep(POLL_S)

    if rec.get("state") != "done":
        return {"jid": jid, "tag": tag, "state": rec.get("state"),
                "error": rec.get("error") or snap.get("error")}

    out_dir = os.path.abspath(os.path.expanduser(
        args.get("out_dir") or _cfg()[2]))
    jdir = os.path.join(out_dir, jid)
    files = {}
    for kind, base in (rec.get("artifacts") or {}).items():
        _download("/api/artifact/%s/%s" % (jid, base),
                  os.path.join(jdir, base))
        files[kind] = os.path.join(jdir, base)
    return {"jid": jid, "tag": tag, "state": "done",
            "frame": rec.get("frame"), "strokes": rec.get("strokes"),
            "of": rec.get("of"), "mean_diff": rec.get("mean_diff"),
            "files": files,
            "note": "frame png + cut mp4 + layered kra/psd + the "
                    "truncated plan at that stroke count; Read the png "
                    "to see the moment"}


def tool_gallery(args):
    jobs = _json("GET", "/api/gallery")["jobs"]
    try:
        limit = max(1, min(int(args.get("limit") or 20), 60))
    except (TypeError, ValueError):
        limit = 20
    return {"jobs": jobs[:limit]}


def tool_cancel(args):
    jid = args.get("jid") or ""
    _validate_jid(jid)
    return _json("POST", "/api/job/%s/cancel" % jid, data=b"",
                 ctype="application/json")


TOOLS = [
    {"name": "daub_web_generate",
     "description":
         "Paint on the daub web studio: upload a "
         "local reference image (png/jpeg/webp), wait for the brush "
         "engine, download the finished PNG locally. Optional layered "
         ".kra / .psd / timelapse .mp4 / plan JSON. Tiers: fast 1400px / "
         "std 2400px / full uncapped. Returns local file paths - Read "
         "the png to see the painting. Timed-out jobs keep rendering "
         "server-side; recover via daub_web_status + daub_web_fetch.",
     "inputSchema": {"type": "object", "required": ["image"],
                     "properties": {
                         "image": {"type": "string",
                                   "description": "local path to the "
                                                  "reference image"},
                         "name": {"type": "string",
                                  "description": "painting title"},
                         "tier": {"type": "string",
                                  "enum": ["fast", "std", "full"],
                                  "description": "default std"},
                         "pen": {"type": "string",
                                 "description": "brush preset override "
                                                "(see daub_list_brushes "
                                                "on the local daub MCP)"},
                         "kra": {"type": "boolean"},
                         "psd": {"type": "boolean"},
                         "mp4": {"type": "boolean"},
                         "plan": {"type": "boolean",
                                  "description": "also download the "
                                                 "stroke plan JSON"},
                         "layers": {"type": "boolean",
                                    "description": "generate per-layer "
                                                   "PNGs (default on)"},
                         "wait_s": {"type": "number",
                                    "description": "poll timeout, "
                                                   "default 900"},
                         "out_dir": {"type": "string",
                                     "description": "download dir "
                                                    "(default "
                                                    "~/daub-web-out/<jid>)"}}}},
    {"name": "daub_web_status",
     "description":
         "One web-studio job's snapshot: state (planning/done/fail/"
         "cancelled), strokes so far, canvas size, artifact basenames. "
         "Pass the jid from daub_web_generate or daub_web_gallery.",
     "inputSchema": {"type": "object", "required": ["jid"],
                     "properties": {"jid": {"type": "string"}}}},
    {"name": "daub_web_fetch",
     "description":
         "Download one artifact from the web studio to local disk: an "
         "artifact basename from daub_web_status's artifacts map, or "
         "live.png (mid-render progress) / ref.png. Returns the saved "
         "local path.",
     "inputSchema": {"type": "object", "required": ["jid", "file"],
                     "properties": {"jid": {"type": "string"},
                                    "file": {"type": "string"},
                                    "out_path": {"type": "string"}}}},
    {"name": "daub_web_frame",
     "description":
         "定格取材 on a finished web-studio job: pull one moment of its "
         "timelapse as a lossless frame PNG + frame-exact cut mp4 + "
         "layered .kra/.psd + the truncated plan at that stroke count. "
         "The job must have been submitted with mp4=true. Async - "
         "submits, polls, downloads everything locally (slow, needs a "
         "progress-capable client or a generous wait_s).",
     "inputSchema": {"type": "object", "required": ["jid", "seconds"],
                     "properties": {
                         "jid": {"type": "string"},
                         "seconds": {"type": ["number", "string"],
                                     "description": "the moment, e.g. 12 "
                                                    "or \"12s\" (video "
                                                    "time axis = stroke "
                                                    "count)"},
                         "wait_s": {"type": "number",
                                    "description": "poll timeout, "
                                                   "default 300"},
                         "out_dir": {"type": "string",
                                     "description": "download dir "
                                                    "(default "
                                                    "~/daub-web-out/<jid>)"
                                                    ""},
                         "cut": {"type": "boolean",
                                 "description": "also produce the cut "
                                                "mp4 up to the moment "
                                                "(default true)"},
                         "kra": {"type": "boolean",
                                 "description": "also produce the "
                                                "layered .kra at that "
                                                "stroke count (default "
                                                "true)"},
                         "psd": {"type": "boolean",
                                 "description": "also produce the "
                                                "layered .psd at that "
                                                "stroke count (default "
                                                "true)"}}}},
    {"name": "daub_web_edit",
     "description":
         "Per-layer stroke-size ruler on a finished web-studio job: "
         "sizes {layer: multiplier} (0.2..3.0, 1.0 = unchanged) scales "
         "every stroke of that layer proportionally - geometry and "
         "pressure untouched - and re-renders the layer set server-side "
         "(e_-prefixed; the original plan is never overwritten, the "
         "edited one lands as the plan_edit artifact). Empty sizes = "
         "reset. Synchronous but slow on big plans (full re-render).",
     "inputSchema": {"type": "object", "required": ["jid"],
                     "properties": {
                         "jid": {"type": "string"},
                         "sizes": {"type": "object",
                                   "description": "layer -> size "
                                   "multiplier, e.g. {\"L7\": 1.15} "
                                   "(layer name or 0-based index as "
                                   "string); empty object = reset"}}}},
    {"name": "daub_web_gallery",
     "description":
         "Recent jobs on the web studio, newest first (id, name, state, "
         "strokes, tier, canvas). Disk-backed - survives restarts.",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}}}},
    {"name": "daub_web_cancel",
     "description":
         "Cancel a queued/running web-studio job (terminates the engine "
         "process; finished jobs are unaffected).",
     "inputSchema": {"type": "object", "required": ["jid"],
                     "properties": {"jid": {"type": "string"}}}},
]

HANDLERS = {
    "daub_web_generate": tool_generate,
    "daub_web_frame": tool_frame,
    "daub_web_edit": tool_edit,
    "daub_web_status": tool_status,
    "daub_web_fetch": tool_fetch,
    "daub_web_gallery": tool_gallery,
    "daub_web_cancel": tool_cancel,
}


# ------------------------------------------------------------------ main

def write(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve():
    # The host process's stdout may default to a legacy ANSI codepage
    # (GBK on this machine): protocol lines carry CJK paths, so pin
    # both directions to UTF-8 - same trap as daub_mcp's serve().
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    log("serving on stdio; target=%s" % (os.environ.get("DAUB_WEB_URL")
                                         or "http://127.0.0.1:8787"))
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

                def progress(frac, text, _t=ptoken):
                    write({"jsonrpc": "2.0", "method":
                           "notifications/progress", "params": {
                               "progressToken": _t,
                               "progress": round(frac, 3),
                               "message": text}})

                try:
                    h = HANDLERS.get(name)
                    if h is None:
                        raise ValueError("unknown tool: %s" % name)
                    result = h(args, progress if ptoken is not None
                               else None) \
                        if name in ("daub_web_generate",
                                    "daub_web_frame") else h(args)
                    result = {"content": [{"type": "text",
                                           "text": json.dumps(
                                               result,
                                               ensure_ascii=False)}]}
                except Exception as e:
                    log("tool %s error: %s" % (name, e))
                    result = {"content": [{"type": "text",
                                           "text": "%s: %s"
                                           % (type(e).__name__, e)}],
                              "isError": True}
                write({"jsonrpc": "2.0", "id": mid, "result": result})
            else:
                write({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601,
                                 "message": "no such method"}})
        except Exception as e:                     # never kill the loop
            log("dispatch error: %r" % (e,))
            if mid is not None:
                write({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32603, "message": repr(e)}})


if __name__ == "__main__":
    serve()
