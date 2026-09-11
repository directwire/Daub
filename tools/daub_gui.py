"""daub 画坊 - desktop GUI for the paint chain.

Three views over one engine process boundary:

  批量队列   add images/folders -> one `daub_paint` subprocess per job
             (QProcess; kill() cancels), live band progress, final
             preview, optional .kra + timelapse video.
  实时生长   while the planner runs, stroke_engine dumps a finalized
             partial plan (~every 1.5s of progress); the selected row
             polls that file and renders each new growth stage via
             --render-only, so the artist watches the painting grow from
             the job's first second. Only the selected row grows - the
             planner owns the CPU.
  图层工作台 one raw `daub render --layers-dir` pass loads every layer
             as premultiplied BGRA; mute/unmute is then a Qt-side
             SourceOver recomposite - instant, no engine round-trip
             (only re-preset deserves a re-render,
             and that one renders just the touched layer). Re-preset
             targets are ink_calib's preset keys (27 as of the 09-07
             pool expansion) - daub fails loud outside the table.
             Exports: PNG / layered .kra
             (Krita-editable) / video / <stem>_edit_plan.json (never
             the original plan).

The GUI is a pure front-end: no scipy/skimage - the engine exe
(dist/daub_paint.exe, built by build_paint.py) carries all of that.
The workbench additionally needs the raw daub.exe (bundled by
build_gui.py) for its layer exports; without it the workbench falls
back to whole-render preview edits.
A windowed build has no stdout, so diagnostics go to
%LOCALAPPDATA%/daub/daub_gui.log.

Run:  pack_venv/Scripts/python.exe tools/daub_gui.py
Pack: pack_venv/Scripts/python.exe tools/build_gui.py
"""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, deque

try:
    import winreg            # Windows-only, like the rest of daub
except ImportError:          # pragma: no cover
    winreg = None

from PySide6.QtCore import (QEasingCurve, QObject, Property, QPropertyAnimation,
                            QRectF, QProcess, QProcessEnvironment, Qt,
                            QSettings, QSize, QTimer, Signal, QUrl)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QIcon,
                           QImage, QImageReader, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView,
                               QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFrame,
                               QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

import render_timelapse as rt  # stdlib-only helpers (same tools/ dir)
from preset_names import norm, zh  # brush display names (plain Chinese)

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # GUI exe lives in <repo>/dist next to daub_paint.exe
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    INK_CALIB = os.path.join(sys._MEIPASS, "data", "ink_calib.json")
    TIPS_JSON = os.path.join(sys._MEIPASS, "data", "brush_lib.json")
else:
    ROOT = os.path.dirname(HERE)
    INK_CALIB = os.path.join(HERE, "data", "ink_calib.json")
    TIPS_JSON = os.path.join(HERE, "data", "brush_lib.json")

LOG_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "daub",
    "daub_gui.log")

SELF_TINY = """
{"reference": "", "canvas": [96, 96], "seed": 1, "bg": [242, 244, 246],
 "detail_rois": [], "count": 2, "strokes": [
  {"layer": "F1", "preset": "b) Basic-5 Size", "size": 24.0,
   "color": "#8fa3b0", "opacity": 0.92,
   "points": [[14.0, 14.0, 0.9], [40.0, 18.0, 0.8], [62.0, 30.0, 0.9]]},
  {"layer": "X1", "preset": "d) Ink-3 Gpen", "size": 4.0,
   "color": "#222831", "opacity": 0.9,
   "points": [[20.0, 70.0, 0.9], [44.0, 66.0, 0.6], [70.0, 74.0, 0.9]]}]}
"""


def _selfcheck_log(msg):
    print(msg, flush=True)
    log.info("selfcheck: %s", msg)


def _run_logged(tag, argv, cwd=None):
    """Run one subprocess for --selfcheck, log + return (ok, detail)."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           cwd=cwd)
    except Exception as e:
        _selfcheck_log("%s SPAWN FAILED: %r" % (tag, e))
        return False, "spawn: %s" % e
    tail = " | ".join((r.stdout or "").strip().splitlines()[-3:]) or \
        "(no stdout)"
    if r.stderr and r.stderr.strip():
        tail += " || stderr: " + " | ".join(
            r.stderr.strip().splitlines()[-2:])
    ok = r.returncode == 0
    _selfcheck_log("%s rc=%s %.1fs :: %s" %
                   (tag, r.returncode, time.monotonic() - t0, tail))
    return ok, tail


def selfcheck():
    """`daub_gui.exe --selfcheck`: exercise exactly what the frozen
    workbench needs (bundled daub.exe + cal/tips data + --layers-dir),
    loudly, into the log. No GUI, no engine planner involved."""
    setup_logging()
    _selfcheck_log("---- selfcheck start (frozen=%s) ----" % FROZEN)
    ok_all = True
    daub, dsrc = resolve_daub()
    _selfcheck_log("daub: %s" % (dsrc or "MISSING"))
    ok_all &= daub is not None
    eng, esrc = resolve_engine()
    _selfcheck_log("engine: %s%s" % (esrc or "MISSING",
                                     "" if eng is not None
                                     else "  (warn: fine when the exe "
                                          "runs from a staging dir; "
                                          "it must sit IN dist/ )"))
    for p in (INK_CALIB, TIPS_JSON):
        sz = os.path.getsize(p) if os.path.isfile(p) else -1
        _selfcheck_log("data %s -> %s bytes" % (p, sz))
        ok_all &= sz > 0
    try:  # a stale bundle (exe built before a cal merge) shows up here
        _selfcheck_log("ink_calib presets: %d" %
                       len(json.load(open(INK_CALIB,
                                          encoding="utf-8"))["presets"]))
    except Exception as e:
        _selfcheck_log("ink_calib UNREADABLE: %s" % e)
        ok_all = False
    ff = resolve_ffmpeg()
    _selfcheck_log("ffmpeg: %s" % (ff or "MISSING (video export off)"))
    if daub is None:
        _selfcheck_log("---- selfcheck FAIL (no daub.exe) ----")
        return 1
    tmp = os.path.join(os.environ.get("TEMP", "."), "daub_selfcheck")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    tiny = os.path.join(tmp, "tiny.json")
    with open(tiny, "w", encoding="utf-8") as fh:
        fh.write(SELF_TINY)
    # 1) the workbench's exact initial call shape: --layers-dir
    ok, _ = _run_logged(
        "layers-render",
        daub + ["render", tiny, "--out", os.path.join(tmp, "tiny.png"),
                "--cal", INK_CALIB, "--tips", TIPS_JSON,
                "--layers-dir", os.path.join(tmp, "layers")])
    ok_all &= ok
    ljson = os.path.join(tmp, "layers", "layers.json")
    try:
        mf = json.load(open(ljson))
        n = len(mf["layers"])
        w, h = mf["width"], mf["height"]
        _selfcheck_log("layers.json OK: %d layers %dx%d" % (n, w, h))
    except Exception as e:
        n = 0
        _selfcheck_log("layers.json MISSING/BAD: %s" % e)
    ok_all &= n > 0
    # 2) the engine's --render-only shape (growth frames + legacy edits)
    if eng is not None:
        ok, _ = _run_logged(
            "engine-render-only",
            eng + ["--render-only", tiny, os.path.join(tmp, "tiny2.png")])
        ok_all &= ok
        # 3) layered PSD export rides the same engine pass - gate the
        # artifact at the spec floor (26B header alone proves nothing)
        psd_out = os.path.join(tmp, "tiny.psd")
        ok, _ = _run_logged(
            "engine-render-psd",
            eng + ["--render-only", tiny, os.path.join(tmp, "tiny3.png"),
                   "--psd", psd_out])
        ok_all &= ok
        sz = os.path.getsize(psd_out) if os.path.isfile(psd_out) else -1
        _selfcheck_log("psd %s -> %d bytes" % (psd_out, sz))
        ok_all &= sz > 26
    # 4) 定格取材: the workbench's exact FrameTask call shape (bundled
    # daub.exe + cal/tips + known order, verification auto when PIL is
    # around / skip where it isn't). Needs ffmpeg; skipped loudly when
    # absent, like the video export itself.
    if ff is not None and eng is not None:
        os.environ.setdefault("DAUB_FFMPEG", ff)
        mp4 = os.path.join(tmp, "tiny.mp4")
        ok, _ = _run_logged(
            "engine-timelapse",
            eng + ["--render-only", tiny, os.path.join(tmp, "tiny4.png"),
                   "--timelapse", mp4])
        ok_all &= ok and os.path.isfile(mp4)
        if ok:
            try:
                import daub_frame as dfr
                try:
                    import PIL   # noqa: F401
                    verify = "auto"
                except ImportError:
                    verify = "skip"
                res = dfr.run(video=mp4, seconds=1.0, plan=tiny,
                              out_dir=tmp, base="tiny", order="big_first",
                              daub=daub[0], cal=INK_CALIB, tips=TIPS_JSON,
                              verify=verify,
                              log=lambda m: _selfcheck_log("  frame: " + m))
                fr_ok = (os.path.isfile(res["frame_png"])
                         and res["strokes"] > 0)
                _selfcheck_log("frame %s -> %s (strokes=%s, verify=%s)"
                               % (res["frame_png"],
                                  "OK" if fr_ok else "MISSING",
                                  res["strokes"], verify))
                ok_all &= fr_ok
            except Exception as e:
                _selfcheck_log("frame FAILED: %r" % e)
                ok_all = False
    else:
        _selfcheck_log("frame SKIPPED (needs ffmpeg + engine)")
    _selfcheck_log("---- selfcheck %s ----" %
                   ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1

# The atelier look, 2026: a quiet near-white canvas, floating white
# cards, one confident blue used sparingly, hairline dividers, pill
# controls - the big-brand hierarchy where type does the talking and
# chrome nearly disappears. All solid fills (no glass); depth
# comes from surface contrast, never from transparency.
STYLESHEET = """
QWidget {
    color: #1d1d1f;
    font-family: "Segoe UI Variable Display", "Segoe UI",
                 "Microsoft YaHei UI";
    font-size: 13px;
}
QMainWindow, QDialog { background: #f5f5f7; }

/* ---- pages float on the canvas; tabs are segmented pills ---- */
QTabWidget::pane { background: transparent; border: none; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent;
    color: #6e6e73;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 6px 26px;
    margin-right: 8px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1d1d1f;
    border-color: #e3e3e8;
    font-weight: 600;
}
QTabBar::tab:hover:!selected { background: #ebebee; }

/* ---- buttons: pill cards, one hero blue ---- */
QPushButton {
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 16px;
    padding: 5px 20px;
    min-height: 20px;
    color: #1d1d1f;
}
QPushButton:hover { background: #f5f5f7; border-color: #c7c7cc; }
QPushButton:pressed { background: #e8e8ed; }
QPushButton:disabled {
    color: #aeaeb2; background: #f5f5f7; border-color: #e8e8ed;
}
QPushButton#primary {
    background: #0071e3;
    border-color: #0071e3;
    color: #ffffff;
    font-weight: 600;
    padding: 5px 34px;
}
QPushButton#primary:hover { background: #0077ed; }
QPushButton#primary:pressed { background: #0068d1; }
QPushButton#primary:disabled {
    background: #b7d9f7; border-color: #b7d9f7; color: #ffffff;
}

/* ---- inputs: quiet white fields, blue only when focused ---- */
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 10px;
    padding: 4px 12px;
    min-height: 20px;
    selection-background-color: #cfe5ff;
    selection-color: #1d1d1f;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #c7c7cc; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #0071e3; }
/* editable combos: the inner line edit must not draw its own frame */
QComboBox QLineEdit { border: none; background: transparent; padding: 0; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #86868b;
    margin-right: 9px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e3e3e8;
    border-radius: 12px;
    padding: 6px;
    selection-background-color: #e8f1fc;
    selection-color: #1d1d1f;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button {
    border: none; background: transparent; width: 16px;
}
QSpinBox::up-arrow {
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 4px solid #86868b;
}
QSpinBox::down-arrow {
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 4px solid #86868b;
}

/* ---- checkboxes: small, rounded, filled blue when on ---- */
QCheckBox { spacing: 8px; color: #1d1d1f; background: transparent; }
QCheckBox::indicator {
    width: 18px; height: 18px;
    border: 1px solid #c7c7cc;
    border-radius: 6px;
    background: #ffffff;
}
QCheckBox::indicator:hover { border-color: #86868b; }
QCheckBox::indicator:checked { background: #0071e3; border-color: #0071e3; }
QCheckBox::indicator:disabled { background: #f5f5f7; border-color: #e8e8ed; }
QCheckBox:disabled { color: #aeaeb2; }

/* ---- tables & lists: white cards with airy rows ---- */
QTableWidget {
    background: #ffffff;
    border: 1px solid #e8e8ed;
    border-radius: 14px;
    gridline-color: transparent;
    selection-background-color: #e8f1fc;
    selection-color: #1d1d1f;
    outline: none;
}
QTableWidget::item { padding: 6px 10px; border: none; }
QTableWidget::item:selected { background: #e8f1fc; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background: #ffffff;
    color: #86868b;
    border: none;
    border-bottom: 1px solid #e8e8ed;
    padding: 10px;
    font-size: 12px;
    font-weight: 600;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #e8e8ed;
    border-radius: 14px;
    padding: 8px;
    outline: none;
}
QListWidget::item {
    border-radius: 10px;
    padding: 4px;
    margin: 2px 1px;
    color: #1d1d1f;
}
QListWidget::item:hover { background: #f5f5f7; }
QListWidget::item:selected { background: #e8f1fc; }

/* ---- chrome: near-invisible plumbing ---- */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal:hover { background: #dcdce1; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px 2px; }
QScrollBar::handle:vertical {
    background: #c7c7cc; border-radius: 4px; min-height: 36px;
}
QScrollBar::handle:vertical:hover { background: #aeaeb2; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px 3px; }
QScrollBar::handle:horizontal {
    background: #c7c7cc; border-radius: 4px; min-width: 36px;
}
QScrollBar::handle:horizontal:hover { background: #aeaeb2; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
QStatusBar {
    background: transparent;
    color: #86868b;
    border-top: 1px solid #e8e8ed;
    font-size: 12px;
}
QToolTip {
    background: #333336;
    color: #f5f5f7;
    border: none;
    border-radius: 10px;
    padding: 6px 12px;
    font-size: 12px;
}

/* ---- type voices ---- */
QLabel { background: transparent; }
QLabel#fieldLab { color: #6e6e73; font-size: 12px; }
QLabel#wbTitle { font-size: 15px; font-weight: 600; }
QLabel#wbStatus { color: #86868b; font-size: 12px; }
QLabel#layerName { font-weight: 600; }
QLabel#layerSub { color: #86868b; font-size: 12px; }
QLabel#poeticCap {
    color: #86868b;
    font-family: Georgia, "KaiTi", "楷体";
    font-style: italic;
    font-size: 13px;
}

/* ---- the floating image card (WA_StyledBackground is set on it) ---- */
ZoomBox {
    background: #ffffff;
    border: 1px solid #e8e8ed;
    border-radius: 16px;
}
"""

ST_QUEUED, ST_RUN, ST_OK, ST_FAIL, ST_CANCEL = \
    "排队", "运行中", "✓", "✗", "已取消"

# engine stdout line grammar (daub_paint.py / stroke_engine.py)
RX_PLAN0 = re.compile(r"^plan: (.+)$")
RX_BAND = re.compile(r"^band\s+(\d+)-\s*(\d+)\s+pass (\d+): (\d+) strokes"
                     r" \((\d+)s\)")
RX_BAND_T = re.compile(r"^band\s+(\d+)-\s*(\d+)\s+total: (\d+) strokes")
RX_PLAN_DONE = re.compile(r"^plan done: (\d+) strokes -> (.+)$")
RX_DAUB = re.compile(r"^daub: (\d+) strokes.*total ([0-9.]+)s")
RX_LAYER = re.compile(r"^layer (\S+) done at (\d+) strokes"
                      r" \(frame (\d+)/(\d+)\)")
RX_TL_DONE = re.compile(r"^timelapse done: (\d+) frames")
RX_VIDEO = re.compile(r"^video (\d+)% \((\d+)/(\d+) frames\)$")
RX_DONE = re.compile(r"^daub_paint done -> (.+)$")

log = logging.getLogger("daub_gui")


def setup_logging():
    log.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        h = logging.FileHandler(LOG_PATH, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    except OSError:
        pass


class _StderrTrap:
    """Windowed exe: PySide prints slot tracebacks to stderr, which no
    one sees - the 2026-09-08 workbench video-export bug (a cross-class
    AttributeError) died exactly this way. Park stderr in the log."""

    def __init__(self, lg):
        self._lg = lg

    def write(self, s):
        for ln in s.splitlines():
            if ln.strip():
                self._lg.warning("stderr: %s", ln.strip())

    def flush(self):
        pass


def install_crash_visibility():
    """Uncaught exceptions and (when frozen) PySide's stderr tracebacks
    land in daub_gui.log - a dead slot must be diagnosable, not a
    shrug. Call once from main() after setup_logging()."""
    if not log.handlers:
        return
    prev = sys.excepthook

    def hook(tp, val, tb):
        log.error("uncaught exception", exc_info=(tp, val, tb))
        if prev not in (None, sys.__excepthook__):
            prev(tp, val, tb)
    sys.excepthook = hook
    if FROZEN:
        sys.stderr = _StderrTrap(log)


def resolve_engine():
    """[argv...] for the engine: env override > dist exe > dev fallback.

    The dev fallback runs the repo chain under the pack venv (scipy
    present); it cannot exist in a windowed frozen build, which is fine
    because the frozen GUI ships next to dist/daub_paint.exe."""
    env = os.environ.get("DAUB_PAINT_EXE")
    if env and os.path.isfile(env):
        return [env], "DAUB_PAINT_EXE=%s" % env
    exe = os.path.join(ROOT, "dist",
                       "daub_paint.exe" if os.name == "nt" else "daub_paint")
    if os.path.isfile(exe):
        return [exe], exe
    if not FROZEN:
        py = os.path.join(ROOT, "pack_venv",
                          "Scripts" if os.name == "nt" else "bin",
                          "python.exe" if os.name == "nt" else "python")
        if os.path.isfile(py):
            return [py, "-u", os.path.join(HERE, "daub_paint.py")], \
                "dev fallback (pack_venv)"
    return None, "engine not found (build_paint.py first, or set " \
                 "DAUB_PAINT_EXE)"


def resolve_daub():
    """[argv...] for the RAW daub.exe (workbench layer exports and
    per-layer re-preset renders): env > bundled (frozen) > dev build.
    Distinct from resolve_engine(): daub_paint.exe is frozen python and
    has no --layers-dir."""
    env = os.environ.get("DAUB_EXE")
    if env and os.path.isfile(env):
        return [env], "DAUB_EXE=%s" % env
    if FROZEN:
        for name in ("daub.exe", "daub"):
            exe = os.path.join(sys._MEIPASS, name)
            if os.path.isfile(exe):
                return [exe], exe
    exe = os.path.join(ROOT, "target", "release",
                       "daub.exe" if os.name == "nt" else "daub")
    if os.path.isfile(exe):
        return [exe], exe
    return None, "raw daub.exe not found (cargo build --release, or set DAUB_EXE)"


def load_presets():
    """The only legal re-preset targets: ink_calib's preset keys (daub
    fails loud on anything else, so the restriction IS the guard)."""
    try:
        with open(INK_CALIB, encoding="utf-8") as fh:
            return sorted(json.load(fh)["presets"].keys())
    except Exception as e:  # bundled data missing is a build bug - say so
        log.warning("ink_calib unreadable: %s", e)
        return []


_FFMPEG = []   # memo box: [] = unresolved, [path-or-None] = resolved


def resolve_ffmpeg():
    """ffmpeg.exe path or None; resolved once, remembered.

    ffmpeg can be installed yet the GUI says otherwise - the GUI
    process inherits Explorer's stale PATH, so shutil.which lies. Fall
    back to the registry PATH (user +
    machine hives) and a local vendor copy; the resolved absolute path
    is also exported to engine spawns as DAUB_FFMPEG so the timelapse
    chain (render_timelapse) finds it deep inside the subprocess."""
    if _FFMPEG:
        return _FFMPEG[0]
    ff = os.environ.get("DAUB_FFMPEG")
    if not (ff and os.path.isfile(ff)):
        ff = shutil.which("ffmpeg")
    if not ff:
        for cand in (os.path.join(ROOT, "dist", "ffmpeg.exe"),
                     os.path.join(ROOT, "vendor", "ffmpeg",
                                  "ffmpeg.exe")):
            if os.path.isfile(cand):
                ff = cand
                break
    if not ff and winreg is not None:
        hives = [(winreg.HKEY_CURRENT_USER, r"Environment")]
        if hasattr(winreg, "HKEY_LOCAL_MACHINE"):
            hives.append((winreg.HKEY_LOCAL_MACHINE,
                          r"SYSTEM\CurrentControlSet\Control"
                          r"\Session Manager\Environment"))
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
        if ff:
            log.info("ffmpeg via registry PATH: %s", ff)
    _FFMPEG.append(ff)
    return ff


class EngineRunner(QObject):
    """One QProcess, streamed line-by-line (utf-8, merged channels)."""

    line = Signal(str)
    done = Signal(int, bool)  # exit code, crashed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self._buf = ""

    def running(self):
        return self.proc is not None and \
            self.proc.state() != QProcess.NotRunning

    def start(self, argv):
        self._buf = ""
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        ff = resolve_ffmpeg()
        if ff:
            # render_timelapse deep inside the engine inherits the
            # same stale-PATH problem - hand it the absolute path
            env.insert("DAUB_FFMPEG", ff)
        p.setProcessEnvironment(env)
        p.readyReadStandardOutput.connect(self._drain)
        p.finished.connect(self._finished)
        p.errorOccurred.connect(self._error)
        p.start(argv[0], argv[1:])
        self.proc = p

    def _error(self, err):
        # FailedToStart never emits finished - synthesize done or every
        # "await completion" state machine (live-growth frames, jobs)
        # latches busy forever with zero diagnostics
        if err == QProcess.ProcessError.FailedToStart:
            p, self.proc = self.proc, None
            if p is not None:
                log.warning("engine failed to start: %s",
                            p.program() + " " + " ".join(p.arguments()))
                self.done.emit(1, True)

    def kill(self):
        if self.running():
            self.proc.kill()

    def _drain(self):
        if self.proc is None:
            return
        self._buf += bytes(
            self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            self.line.emit(ln.rstrip("\r"))

    def _finished(self, code, status):
        # detach BEFORE emitting: a done slot may re-enter synchronously
        # and start() the next frame on this same runner (SnapChain
        # reuses one runner for the whole chain) - work on the captured
        # p only, so the tail can never deleteLater the runner's NEW
        # proc away
        p, self.proc = self.proc, None
        if p is None:
            return  # stale finished - already handled
        self._buf += bytes(
            p.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            self.line.emit(ln.rstrip("\r"))
        if self._buf.strip():
            self.line.emit(self._buf.rstrip("\r"))
        self._buf = ""
        p.deleteLater()
        self.done.emit(code, status == QProcess.CrashExit)


class Job(QObject):
    """One queue row: naming plan + engine state."""

    def __init__(self, src, out_dir, stem, do_kra, do_mp4, do_psd, row):
        super().__init__()
        self.src = src
        self.out_dir = out_dir
        self.stem = stem
        self.out_png = os.path.join(out_dir, stem + ".png")
        # daub_paint derives <stem>_plan.json from the png path - match it
        self.plan_path = os.path.join(out_dir, stem + "_plan.json")
        self.kra_path = os.path.join(out_dir, stem + ".kra") if do_kra \
            else None
        self.psd_path = os.path.join(out_dir, stem + ".psd") if do_psd \
            else None
        self.mp4_path = os.path.join(out_dir, stem + "_timelapse.mp4") \
            if do_mp4 else None
        self.workdir = os.path.join(out_dir, stem + "_work")
        self.row = row
        self.status = ST_QUEUED
        self.strokes = None
        self.cancelled = False
        self.runner = None       # EngineRunner while running
        self.planned = False
        self.tail = deque(maxlen=6)


class LiveGrowth(QObject):
    """Poll-driven live-growth view: watch the painting actually grow,
    not a post-hoc slideshow.

    The planner dumps a finalized partial plan to
    <stem>_plan_partial.json as it works; this poller ships a private
    copy to --render-only each time the count grows, serially, catching
    up to the newest stage after each render. Stops when the job's plan
    is done - the engine's own final png closes the show (_on_job_done).
    One poller for the whole app, targeting the selected row only."""

    frame = Signal(object, str, int)    # job, png, strokes shown

    POLL_MS = 700

    def __init__(self, parent=None):
        super().__init__(parent)
        self.job = None
        self.active = False
        self._seen = 0             # strokes claimed (shown or en route)
        self._rendering = False
        self._png = None
        self._seq = 0
        self._tail = deque(maxlen=6)  # engine lines of the current frame
        self.runner = EngineRunner(self)
        self.runner.line.connect(self._tail.append)
        self.runner.done.connect(self._step_done)
        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_MS)
        self.timer.timeout.connect(self._poll)

    def start(self, job):
        self.stop()
        self.job = job
        self.active = True
        self._seen = 0
        self._seq = 0
        self._rendering = False
        self._png = None
        self.timer.start()

    def _poll(self):
        if not self.active or self._rendering:
            return
        j = self.job
        if j is None or j.runner is None or j.planned or j.cancelled:
            self.stop()            # planning over - the final takes over
            return
        pp = j.plan_path[:-5] + "_partial.json"
        try:
            with open(pp, encoding="utf-8") as fh:
                d = json.load(fh)
            n = int(d["count"])
        except (OSError, ValueError, KeyError):
            return                 # not written yet / mid-replace
        if n <= self._seen or n <= 0:
            return
        # private copy: the planner rewrites the original (os.replace,
        # but a copy also keeps daub away from a live file)
        os.makedirs(j.workdir, exist_ok=True)
        dst = os.path.join(j.workdir, "_live.json")
        try:
            shutil.copyfile(pp, dst)
        except OSError:
            return
        eng, _ = resolve_engine()
        if not eng:
            self.stop()
            return
        self._seen = n
        self._rendering = True
        self._seq += 1
        self._png = os.path.join(j.workdir, "_live_%d.png" % (self._seq % 2))
        self.runner.start(eng + ["--render-only", dst, self._png])

    def _step_done(self, code, _crashed):
        self._rendering = False
        png, n = self._png, self._seen
        if self.active and code == 0 and png is not None and \
                os.path.isfile(png) and os.path.getsize(png) > 0:
            self.frame.emit(self.job, png, n)
            self._poll()           # catch up if the planner ran ahead
        elif self.active:
            log.warning("live-growth frame %s failed (rc=%s): %s",
                        n, code, " ｜ ".join(self._tail))

    def stop(self):
        self.active = False
        self.job = None
        self._rendering = False
        self._png = None
        self.timer.stop()
        self.runner.kill()


class ZoomView(QGraphicsView):
    """Wheel-zoom (anchored under mouse), double-click to refit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item = None
        self._ph = ""               # empty-state line, drawn when no image
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignCenter)
        # frameless + unpainted: the ZoomBox card behind shows through,
        # so the image floats on a rounded white card
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QBrush(Qt.NoBrush))

    def set_placeholder(self, text):
        self._ph = text
        self.viewport().update()

    def drawBackground(self, painter, rect):
        if self._item is None and self._ph:
            # the painter arrives in scene coords; reset to device coords
            # or the text lands wherever the current zoom puts it
            painter.save()
            painter.resetTransform()
            painter.setPen(QColor("#b0b0b6"))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(
                self.viewport().rect().adjusted(16, 16, -16, -16),
                Qt.AlignCenter | Qt.TextWordWrap, self._ph)
            painter.restore()

    def set_image(self, path):
        return self.set_qpix(QPixmap(path))

    def set_qpix(self, pm):
        if pm.isNull():
            return False
        self._scene.clear()
        self._item = self._scene.addPixmap(pm)
        self._scene.setSceneRect(
            QRectF(0, 0, float(pm.width()), float(pm.height())))
        self.fit()
        return True

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._item is not None:
            self.fit()

    def fit(self):
        if self._item is None:
            return
        self.resetTransform()
        self.fitInView(self._item, Qt.KeepAspectRatio)

    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def mouseDoubleClickEvent(self, e):
        self.fit()
        super().mouseDoubleClickEvent(e)


class ZoomBox(QWidget):
    """Caption + ZoomView, the unit the layouts place.

    Painted as a rounded white card by the stylesheet - plain QWidget
    subclasses only honour QSS backgrounds with WA_StyledBackground."""

    def __init__(self, caption, placeholder="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)
        self.cap = QLabel(caption)
        self.cap.setObjectName("poeticCap")
        lay.addWidget(self.cap)
        self.view = ZoomView()
        self.view.set_placeholder(placeholder)
        lay.addWidget(self.view, 1)

    def set_image(self, path, caption=None):
        if caption:
            self.cap.setText(caption)
        return self.view.set_image(path)

    def set_qpix(self, pm, caption=None):
        if caption:
            self.cap.setText(caption)
        return self.view.set_qpix(pm)


class SquareSlot(QWidget):
    """Splitter cell that keeps its child the largest square that fits,
    centred - the reference/result frames read as square cards, matching
    the square canvases the engine plans on.

    Pins the child with a fixed size (its own sizeHint is smaller than
    the cell most of the time); the minimumSizeHint override below is
    what keeps this slot itself free to shrink or grow - without it the
    child's fixed minimum would dead-lock the splitter."""

    def __init__(self, box, parent=None):
        super().__init__(parent)
        self._box = box
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(box, 0, Qt.AlignCenter)

    def minimumSizeHint(self):
        return QSize(1, 1)      # the child's fixed size must not back-propagate

    def resizeEvent(self, e):
        s = max(0, min(self.width(), self.height()))
        self._box.setFixedSize(s, s)
        super().resizeEvent(e)


class Switch(QAbstractButton):
    """macOS-style toggle, painted solid - the workbench's eye button.

    The knob glides 140ms (OutCubic) and the track colour lerps
    grey->blue along the same progress value, so colour and motion read
    as one gesture. Pure QPainter: no image assets to bundle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._t = 0.0
        self._anim = QPropertyAnimation(self, b"knobT", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._glide)

    def _glide(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get_knob_t(self):
        return self._t

    def _set_knob_t(self, v):
        self._t = float(v)
        self.update()

    knobT = Property(float, _get_knob_t, _set_knob_t)

    def sizeHint(self):
        return QSize(40, 24)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = float(min(24, self.height()))
        track = QRectF(0.0, (self.height() - h) / 2, 40.0, h)
        t = self._t
        # #e9e9ea -> #0071e3, carried by the same value as the knob
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(round(233 - 233 * t), round(233 - 120 * t),
                          round(234 - 7 * t)))
        p.drawRoundedRect(track, h / 2, h / 2)
        d = h - 4.0
        x = track.left() + 2.0 + t * (40.0 - d - 4.0)
        if t < 0.5:
            p.setPen(QPen(QColor(0, 0, 0, 30), 1))
        else:
            p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, track.top() + 2.0, d, d))


class LayerRow(QWidget):
    """One layer line: an animated Switch + name over telemetry."""

    def __init__(self, name, sub, parent=None):
        super().__init__(parent)
        self.sw = Switch()
        self.sw.setChecked(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 6, 4)
        lay.setSpacing(10)
        lay.addWidget(self.sw)
        col = QVBoxLayout()
        col.setSpacing(0)
        lb1 = QLabel(name)
        lb1.setObjectName("layerName")
        lb2 = QLabel(sub)
        lb2.setObjectName("layerSub")
        col.addWidget(lb1)
        col.addWidget(lb2)
        lay.addLayout(col, 1)


class FrameTask(QObject):
    """定格取材: daub_frame.run on a worker thread. The extraction
    re-renders the moment with the raw daub.exe (seconds to tens of
    seconds on real plans) - it must never sit on the UI thread. One
    task at a time (the workbench gates on _frame_busy); the done
    signal marshals the outcome back onto the UI thread."""

    done = Signal(bool, str)   # ok, human report (status bar)

    def start(self, **kw):
        threading.Thread(target=self._work, kwargs=kw, daemon=True).start()

    def _work(self, kw):
        import daub_frame as dfr   # dev tree or bundled by build_gui.py
        buf = []
        try:
            res = dfr.run(log=buf.append, **kw)
            self.done.emit(True, "已定格 t=%gs · frame %d · %d/%d 笔 → %s"
                           % (res["t"], res["frame"], res["strokes"],
                              res["of"], res["frame_png"]))
        except Exception as e:
            log.warning("frame task failed: %s\n  tail: %s",
                        e, " ｜ ".join(buf[-8:]))
            why = "%s: %s" % (type(e).__name__, e)
            self.done.emit(False, "定格取材失败：%s" % why[-160:])


class Workbench(QWidget):
    """Layer panel + live editing of a plan copy.

    One EngineRunner, five modes: layers (initial --layers-dir pass
    that feeds the instant-composite cache), layer (single-layer
    re-preset render), preview (legacy whole-render edits - fallback
    when raw daub.exe is absent), export (png/kra to a real target),
    video (engine --timelapse, minutes). Mute/unmute never renders:
    the preview is recomposited from cached premultiplied layers in
    Qt. Edits that land mid-render coalesce: the latest state wins on
    the next round."""

    status_msg = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.d = None
        self.plan_path = None
        self.title = ""
        self.workdir = None
        # video reveal order hook (wired to MainWindow's option row);
        # default keeps the workbench usable standalone
        self.order_fn = lambda: "big_first"
        self.muted = set()
        self.presets = {}            # layer -> chosen preset (user edits)
        self.scales = {}             # layer -> size multiplier (size ruler)
        self._stats = []             # [(L, n, lo, hi, mix Counter)]
        self._pending = False
        self._busy_video = False
        self._frame_busy = False     # 定格取材 extraction in flight
        self._mode = None            # None | "preview" | "layers" | "layer"
        self._target = None          #        | "export" | "video"
        # instant-composite state (fed by one --layers-dir render)
        self.layers = []             # [{"name","img":QImage}] bottom-first
        self._cache_preset = {}      # layer -> preset its img was rendered with
        self._cache_scale = {}       # layer -> scale its img was rendered with
        self.paper = (255, 255, 255)
        self.size = (0, 0)
        self._layers_ready = False
        self._layers_done = False    # initial layers render settled (any way)
        self._layer_job = None       # (L, preset, scale) while a "layer" run
        self._composed = None        # last composite QImage (smoke inspects)
        self._tail = deque(maxlen=8)  # engine lines of the current run

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 8, 16, 16)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.lb_title = QLabel("（未载入计划 · drop a plan in the queue）")
        self.lb_title.setObjectName("wbTitle")
        top.addWidget(self.lb_title)
        top.addStretch(1)
        self.lb_status = QLabel("")
        self.lb_status.setObjectName("wbStatus")
        top.addWidget(self.lb_status)
        self.sp_frame = QDoubleSpinBox()
        self.sp_frame.setDecimals(1)
        self.sp_frame.setRange(0.0, 3600.0)
        self.sp_frame.setSingleStep(0.5)
        self.sp_frame.setValue(1.0)
        self.sp_frame.setSuffix(" s")
        self.sp_frame.setToolTip(
            "定格取材的时刻：视频时间轴＝笔数轴（层末有约 1s 停顿，"
            "片尾约 3s）")
        top.addWidget(self.sp_frame)
        fbox = QWidget()
        fh = QHBoxLayout(fbox)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(4)
        fh.addWidget(QLabel("定格格式"))
        self.ck_fcut = QCheckBox("MP4")
        self.ck_fkra = QCheckBox(".kra")
        self.ck_fpsd = QCheckBox(".psd")
        ftip = ("定格取材的配套导出（定格 PNG 恒出）：勾选哪些就随帧一并"
                "产出剪切视频 / 分层工程文件")
        for _c in (self.ck_fcut, self.ck_fkra, self.ck_fpsd):
            _c.setToolTip(ftip)
            fh.addWidget(_c)
        top.addWidget(fbox)
        for text, fn in (("出 PNG", self.export_png),
                         ("出 .kra", self.export_kra),
                         ("出 .psd", self.export_psd),
                         ("出视频", self.export_video),
                         ("定格取材", self.export_frame),
                         ("另存计划", self.save_plan),
                         ("打开文件夹", self.open_folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        lab = QLabel("图层 · 开关＝显示，点选后换笔")
        lab.setObjectName("fieldLab")
        ll.addWidget(lab)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._on_sel_changed)
        ll.addWidget(self.list, 1)
        lab2 = QLabel("选中层换笔（仅校准表内笔种）")
        lab2.setObjectName("fieldLab")
        ll.addWidget(lab2)
        self.combo = QComboBox()
        for _p in load_presets():
            self.combo.addItem(zh(_p))
            self.combo.setItemData(self.combo.count() - 1, _p,
                                   Qt.ToolTipRole)  # raw key on hover
        self.combo.currentTextChanged.connect(self._on_preset)
        ll.addWidget(self.combo)
        lab3 = QLabel("选中层笔画大小（原比例缩放 · 1.00＝复位）")
        lab3.setObjectName("fieldLab")
        ll.addWidget(lab3)
        self.sp_size = QDoubleSpinBox()
        self.sp_size.setDecimals(2)
        self.sp_size.setRange(0.50, 2.00)
        self.sp_size.setSingleStep(0.05)
        self.sp_size.setValue(1.00)
        self.sp_size.setSuffix(" ×")
        self.sp_size.setToolTip(
            "该层每根笔画的 size 原比例放大/缩小：压力里存的是宽度轮廓，"
            "乘一下整根笔等比变化，落笔位置不动。调回 1.00 即复位")
        self.sp_size.valueChanged.connect(self._on_scale)
        ll.addWidget(self.sp_size)
        split.addWidget(left)
        self.zoom = ZoomBox("编辑预览 · preview ✧",
                            "载入计划后，图层会长在这里 ✧")
        split.addWidget(self.zoom)
        split.setSizes([320, 680])
        lay.addWidget(split, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._render_edited)
        self.runner = EngineRunner(self)
        self.runner.line.connect(self._on_line)
        self.runner.done.connect(self._render_done)
        self.frame_task = FrameTask(self)
        self.frame_task.done.connect(self._frame_done)

    # ---- loading ----

    def load_plan(self, path):
        try:
            self.d = rt.load_plan(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "画坊", "计划载入失败：%s" % e)
            return False
        self.plan_path = os.path.abspath(path)
        stem = os.path.basename(self.plan_path)
        for suf in ("_plan.json", ".json"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        self.title = stem
        self.workdir = os.path.join(os.path.dirname(self.plan_path),
                                    stem + "_work")
        self.muted = set()
        self.presets = {}
        self.scales = {}
        self.layers = []
        self._cache_preset = {}
        self._cache_scale = {}
        self._layers_ready = False
        self._layers_done = False
        self._composed = None
        self._layer_job = None
        self.sp_size.blockSignals(True)
        self.sp_size.setValue(1.00)
        self.sp_size.blockSignals(False)
        self._rebuild_layers()
        QTimer.singleShot(0, self._render_layers)
        return True

    def _rebuild_layers(self):
        self.list.blockSignals(True)
        self.list.clear()
        mix = {}
        for s in self.d["strokes"]:
            mix.setdefault(s["layer"], Counter())[s.get("preset", "?")] += 1
        self._stats = [(L, n, lo, hi, mix.get(L, Counter()))
                       for L, n, lo, hi in rt.layer_stats(
                           self.d["strokes"])]
        for L, n, lo, hi, c in self._stats:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, L)
            it.setSizeHint(QSize(0, 48))
            self.list.addItem(it)
            row = LayerRow(L, self._sub(n, lo, hi, c))
            row.sw.toggled.connect(
                lambda on, layer=L: self._set_layer_visible(layer, on))
            self.list.setItemWidget(it, row)
        self.list.blockSignals(False)
        self.lb_title.setText("%s · %s 笔 · %d 层"
                              % (self.title,
                                 f"{len(self.d['strokes']):,}",
                                 len(self._stats)))
        if self._stats:
            self.list.setCurrentRow(0)
        self._sync_combo()

    @staticmethod
    def _sub(n, lo, hi, counter):
        mix = " + ".join("%s×%d" % (zh(p), c)
                         for p, c in counter.most_common(3))
        return "%d 笔 · %.1f–%.1fpx · %s" % (n, lo, hi, mix)

    def _cur_layer(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _dominant(self, L):
        for l, _n, _lo, _hi, c in self._stats:
            if l == L and c:
                return c.most_common(1)[0][0]
        return None

    def _sync_combo(self):
        L = self._cur_layer()
        self.combo.blockSignals(True)
        raw = self.presets.get(L) or self._dominant(L) or ""
        i = self.combo.findText(zh(raw))
        if i < 0:
            i = self.combo.findText(raw)   # legacy/typed raw value
        self.combo.setCurrentIndex(i if i >= 0 else -1)
        self.combo.blockSignals(False)
        self.sp_size.blockSignals(True)
        self.sp_size.setValue(float(self.scales.get(L) or 1.0))
        self.sp_size.blockSignals(False)

    # ---- edits ----

    def _set_layer_visible(self, L, on):
        if on:
            self.muted.discard(L)
        else:
            self.muted.add(L)
        if self._layers_ready:
            # silencing layers must be instant - the
            # per-layer pixels are already cached, just recomposite
            self._show_composite()
        else:
            self.timer.start()      # legacy whole-render path

    def set_layer_visible(self, idx, on):
        """Programmatic toggle (smoke/tests): flips the Switch's state
        and applies the edit, bypassing the animated signal path."""
        it = self.list.item(idx)
        if it is None:
            return
        row = self.list.itemWidget(it)
        row.sw.blockSignals(True)
        row.sw.setChecked(on)
        row.sw.blockSignals(False)
        self._set_layer_visible(it.data(Qt.UserRole), on)

    def _on_sel_changed(self):
        self._sync_combo()

    def _on_preset(self, text):
        L = self._cur_layer()
        if L is not None and text:
            self.presets[L] = norm(text)   # store the raw key
            if self._layers_ready:
                self._pump()
            else:
                self.timer.start()  # legacy whole-render path

    def _on_scale(self, v):
        L = self._cur_layer()
        if L is None:
            return
        if abs(v - 1.0) < 1e-9:
            self.scales.pop(L, None)   # 1.00 ＝ 复位，保持无编辑态
        else:
            self.scales[L] = float(v)
        if self._layers_ready:
            self._pump()
        else:
            self.timer.start()      # legacy whole-render path

    def _edited(self):
        return rt.apply_edits(self.d, self.muted, self.presets, self.scales)

    # ---- instant compositing ----
    #
    # One `daub render --layers-dir` writes every layer as premultiplied
    # BGRA (B,G,R,A - exactly Qt's ARGB32_Premultiplied memory order).
    # Qt SourceOver over those reproduces daub's own composite within
    # u8 rounding (+-1/255, verified by test + smoke), so mutes are a
    # repaint, and a re-preset re-renders only the touched layer.

    def _render_layers(self):
        if self.d is None or self._busy_video or self._mode:
            return
        daub, _src = resolve_daub()
        if not daub:
            self.lb_status.setText("无 raw daub.exe，退回整图重渲模式")
            QTimer.singleShot(0, self._render_edited)
            return
        png = os.path.join(self.workdir, "_edit.png")
        self._mode, self._target = "layers", png
        self.lb_status.setText("分层渲染中 · layers syncing ✧")
        self._tail.clear()
        self.runner.start(daub + ["render", self._write_full_json(),
                                  "--out", png,
                                  "--cal", INK_CALIB, "--tips", TIPS_JSON,
                                  "--layers-dir",
                                  os.path.join(self.workdir, "_layers")])

    def _write_full_json(self):
        os.makedirs(self.workdir, exist_ok=True)
        p = os.path.join(self.workdir, "_full.json")
        with open(p, "w") as fh:
            json.dump(self.d, fh)
        return p

    @staticmethod
    def _load_bgra(path, w, h):
        """Headerless premultiplied BGRA -> owned QImage, or None."""
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        if len(raw) != w * h * 4:
            return None
        img = QImage(raw, w, h, w * 4,
                     QImage.Format.Format_ARGB32_Premultiplied)
        return img.copy()   # deep-copy: raw leaves scope

    def _load_layers_cache(self, ldir):
        try:
            with open(os.path.join(ldir, "layers.json"),
                      encoding="utf-8") as fh:
                mf = json.load(fh)
            w, h = int(mf["width"]), int(mf["height"])
            layers = []
            for ent in mf["layers"]:
                img = self._load_bgra(os.path.join(ldir, ent["file"]), w, h)
                if img is None or img.isNull():
                    return False
                layers.append({"name": ent["name"], "img": img})
            self.layers = layers
            self._cache_preset = {ent["name"]: None for ent in mf["layers"]}
            self._cache_scale = {ent["name"]: None for ent in mf["layers"]}
            self.paper = tuple(mf["paper"])
            self.size = (w, h)
            return True
        except (OSError, ValueError, KeyError) as e:
            log.warning("layers cache read failed: %s", e)
            return False

    def _load_one_layer(self, ldir, L, preset, scale=1.0):
        """Swap a single layer's cached image after a re-preset / resize
        render. `preset`/`scale` are the ones LAUNCHED with (_layer_job),
        not the current control values - a change landing mid-render must
        stay stale so the pump re-renders it."""
        try:
            with open(os.path.join(ldir, "layers.json"),
                      encoding="utf-8") as fh:
                mf = json.load(fh)
            if (int(mf["width"]), int(mf["height"])) != self.size:
                return False
            for ent in mf["layers"]:
                if ent["name"] != L:
                    continue
                img = self._load_bgra(os.path.join(ldir, ent["file"]),
                                      *self.size)
                if img is None or img.isNull():
                    return False
                for e2 in self.layers:
                    if e2["name"] == L:
                        e2["img"] = img
                        break
                self._cache_preset[L] = preset
                self._cache_scale[L] = scale
                return True
            return False
        except (OSError, ValueError, KeyError) as e:
            log.warning("layer reload failed (%s): %s", L, e)
            return False

    def _stale_layers(self):
        """Layers whose cached pixels predate a user edit (re-preset or
        size-ruler move)."""
        return [ent["name"] for ent in self.layers
                if (self.presets.get(ent["name"])
                    != self._cache_preset.get(ent["name"]))
                or (self.scales.get(ent["name"])
                    != self._cache_scale.get(ent["name"]))]

    def _composite(self):
        w, h = self.size
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor(*self.paper))
        p = QPainter(img)
        for ent in self.layers:
            if ent["name"] not in self.muted:
                p.drawImage(0, 0, ent["img"])
        p.end()
        self._composed = img
        return img

    def _show_composite(self):
        vis = sum(n for L, n, *_ in self._stats if L not in self.muted)
        self.zoom.set_qpix(QPixmap.fromImage(self._composite()),
                           "编辑预览 · %s 笔 · 静默即时 ✦" % f"{vis:,}")

    def _pump(self):
        """Serially drain stale layer renders; no-op when busy (the
        finished slot re-pumps via _pending)."""
        if self.d is None or self._busy_video:
            return
        if self._mode is not None:
            self._pending = True
            return
        if not self._layers_ready:
            # fallback mode (no layer cache): edits go the whole-render way
            self._render_edited()
            return
        stale = self._stale_layers()
        if stale:
            self._render_layer(stale[0])
        else:
            self._show_composite()

    def _render_layer(self, L):
        daub, _src = resolve_daub()
        if not daub:
            self.lb_status.setText("无 raw daub.exe，退回整图重渲模式")
            self.timer.start()
            return
        # the layer's own content regardless of its mute state (mute
        # lives at composite time, so un-muting needs valid pixels)
        base = rt.apply_edits(self.d, set(), self.presets, self.scales)
        seq = [s for s in base["strokes"] if s["layer"] == L]
        d2 = dict(base, strokes=seq, count=len(seq))
        os.makedirs(self.workdir, exist_ok=True)
        p = os.path.join(self.workdir, "_layer.json")
        with open(p, "w") as fh:
            json.dump(d2, fh)
        scale = float(self.scales.get(L) or 1.0)
        self._layer_job = (L, self.presets.get(L), scale)
        self._mode, self._target = "layer", \
            os.path.join(self.workdir, "_layer.png")
        delta = self.presets.get(L) or "原笔种"
        if scale != 1.0:
            delta += " ×%.2f" % scale
        self.lb_status.setText("重渲 %s · %s ✧" % (L, delta))
        self._tail.clear()
        self.runner.start(daub + ["render", p, "--out", self._target,
                                  "--cal", INK_CALIB, "--tips", TIPS_JSON,
                                  "--layers-dir",
                                  os.path.join(self.workdir, "_layers_1")])

    # ---- rendering / exports ----

    def _write_edit_json(self):
        os.makedirs(self.workdir, exist_ok=True)
        p = os.path.join(self.workdir, "_edit.json")
        with open(p, "w") as fh:
            json.dump(self._edited(), fh)
        return p

    def _engine(self):
        eng, src = resolve_engine()
        if not eng:
            self.lb_status.setText(src)
        return eng

    def _render_edited(self):
        if self.d is None or self._busy_video:
            return
        if self._mode:
            # an edit landed mid-render (e.g. a mute during the initial
            # layers pass): replay it once that render settles instead
            # of silently dropping it
            self._pending = True
            return
        eng = self._engine()
        if not eng:
            return
        png = os.path.join(self.workdir, "_edit.png")
        os.makedirs(self.workdir, exist_ok=True)
        self._mode, self._target = "preview", png
        self.lb_status.setText("重渲中…")
        self._tail.clear()
        self.runner.start(eng + ["--render-only", self._write_edit_json(),
                                 png])

    def _on_line(self, text):
        t = text.strip()
        if t:
            self._tail.append(t)
        m = RX_LAYER.match(t)
        if m and self._mode == "video":
            self.lb_status.setText("视频渲染 frame %s/%s（层 %s）"
                                   % (m.group(3), m.group(4), m.group(1)))
        m = RX_VIDEO.match(t)
        if m and self._mode == "video":
            # % milestones also cover layer holds, where no layer-done
            # line fires - keeps the label moving through the tail
            self.lb_status.setText("视频导出 %s%%（frame %s/%s）"
                                   % (m.group(1), m.group(2), m.group(3)))

    def _render_done(self, code, _crashed):
        mode, target = self._mode, self._target
        self._mode, self._target = None, None
        ok = code == 0 and target is not None and \
            os.path.isfile(target) and os.path.getsize(target) > 0
        if not ok and mode is not None:
            # failures must be diagnosable: engine tail into the log
            # (the frozen fallback used to be silent)
            log.warning("wb render %s failed (rc=%s, crashed=%s): %s",
                        mode, code, _crashed, " ｜ ".join(self._tail))
        if mode == "layers":
            self._layers_done = True
            if ok and self._load_layers_cache(
                    os.path.join(self.workdir, "_layers")):
                self._layers_ready = True
                self._show_composite()
                self.lb_status.setText("预览已更新 · 静默即时 ✦")
            else:
                self.lb_status.setText("分层渲染失败（详见日志），退回整图重渲")
                QTimer.singleShot(0, self._render_edited)
            if self._pending:
                self._pending = False
                QTimer.singleShot(0, self._pump)
        elif mode == "layer":
            L, preset, scale = self._layer_job or (None, None, 1.0)
            if ok and L is not None and self._load_one_layer(
                    os.path.join(self.workdir, "_layers_1"), L, preset,
                    scale):
                self._show_composite()
                delta = zh(preset) if preset else "原笔种"
                if scale and scale != 1.0:
                    delta += " ×%.2f" % scale
                self.lb_status.setText("预览已更新（%s → %s）" % (L, delta))
            else:
                self.lb_status.setText("重渲失败（笔种需在校准表内）")
                QTimer.singleShot(0, self._render_edited)
            self._layer_job = None
            if self._pending:
                self._pending = False
                QTimer.singleShot(0, self._pump)
        elif mode == "preview":
            edit_png = os.path.join(self.workdir, "_edit.png")
            if ok:
                self.zoom.set_image(edit_png,
                                    "编辑预览（%d 笔）"
                                    % self._edited()["count"])
                self.lb_status.setText("预览已更新")
            else:
                self.lb_status.setText("重渲失败（详见日志）")
            if self._pending:
                self._pending = False
                QTimer.singleShot(0, self._render_edited)
        elif mode == "export":
            if ok:
                self.zoom.set_image(target, "已导出 %s"
                                    % os.path.basename(target))
                self.lb_status.setText("已导出 %s" % target)
                self.status_msg.emit("已导出 " + target)
            else:
                self.lb_status.setText("导出失败（详见日志）")
        elif mode == "video":
            self._busy_video = False
            if ok:
                self.lb_status.setText("已导出 %s" % target)
                self.status_msg.emit("已导出 " + target)
            else:
                # the engine tail says WHY (missing dir, bad plan, ffmpeg
                # ...) - never blame ffmpeg blindly again
                why = (self._tail[-1] if self._tail
                       else "") or ("rc=%s crashed=%s" % (code, _crashed))
                self.lb_status.setText("视频导出失败：%s" % why[-110:])

    def _export_dir(self):
        """Exports follow the batch queue's confirmed output dir (the
        workbench used to drop files next to the plan,
        silently ignoring the folder confirmed in the queue - not a
        race, two disjoint path rules). Plans opened standalone via
        打开计划 fall back to the plan's own folder when that's empty."""
        main = self.window()
        ed = getattr(main, "ed_out", None)
        d = ed.text().strip() if ed is not None else ""
        return d or os.path.dirname(self.plan_path)

    def _export_path(self, suffix, ext):
        base = os.path.join(self._export_dir(), self.title + suffix)
        p, n = base + ext, 0
        while os.path.exists(p):
            n += 1
            p = "%s-%d%s" % (base, n, ext)
        return p

    def _export(self, fmt):
        """fmt: None -> png, "kra" -> layered .kra, "psd" -> layered .psd."""
        if self.d is None:
            return
        if self._busy_video or self._mode:
            self.lb_status.setText("有渲染进行中，稍候")
            return
        eng = self._engine()
        if not eng:
            return
        ext = ".png" if fmt is None else "." + fmt
        target = self._export_path("_edit", ext)
        argv = eng + ["--render-only", self._write_edit_json(), target]
        if fmt:
            argv += ["--" + fmt, target]
        self._mode, self._target = "export", target
        self.lb_status.setText("导出中 %s…" % os.path.basename(target))
        self._tail.clear()
        self.runner.start(argv)

    def export_png(self):
        self._export(fmt=None)

    def export_kra(self):
        self._export(fmt="kra")

    def export_psd(self):
        self._export(fmt="psd")

    def export_video(self):
        if self.d is None:
            return
        if self._busy_video or self._mode:
            self.lb_status.setText("有渲染进行中，稍候")
            return
        eng = self._engine()
        if not eng:
            return
        if resolve_ffmpeg() is None:
            self.lb_status.setText(
                "未找到 ffmpeg（装了的话重启一次画坊即可；或设 DAUB_FFMPEG）")
            return
        target = self._export_path("_edit_timelapse", ".mp4")
        png = os.path.join(self.workdir, "_video_final.png")
        # the row's work dir may be gone (row deleted mid-session) -
        # daub fails loud on a missing output parent (os error 3)
        os.makedirs(self.workdir, exist_ok=True)
        self._busy_video = True
        self._mode, self._target = "video", target
        self.lb_status.setText("视频渲染中（逐帧 daub，需数分钟）…")
        self._tail.clear()
        argv = eng + ["--render-only", self._write_edit_json(),
                      png, "--timelapse", target]
        if self.order_fn() == "small_first":
            argv += ["--timelapse-order", "small_first"]
        self.runner.start(argv)

    def _newest_video(self):
        """Newest workbench timelapse for THIS plan: the export dir
        rides the queue's output folder, the plan's own folder is the
        fallback; -N collision suffixes included, watermark variants
        excluded (burned-in pixels would poison the verification)."""
        cands = []
        for d in {self._export_dir(),
                  os.path.dirname(self.plan_path or "")}:
            if d and os.path.isdir(d):
                cands += [p for p in glob.glob(os.path.join(
                    d, self.title + "_edit_timelapse*.mp4"))
                    if "_watermark" not in os.path.basename(p)]
        return max(cands, key=os.path.getmtime) if cands else None

    def export_frame(self):
        """定格取材: one moment of this plan's timelapse as png / cut
        mp4 / layered kra/psd / truncated plan. The workbench KNOWS its
        reveal order (the queue's option row drove the export), so the
        pixel duel is skipped - one engine render, not three."""
        if self.d is None:
            return
        if self._busy_video or self._mode or self._frame_busy:
            self.lb_status.setText("有渲染进行中，稍候")
            return
        eng, dnote = resolve_daub()
        if not eng:
            self.lb_status.setText(dnote)
            return
        ff = resolve_ffmpeg()
        if ff is None:
            self.lb_status.setText(
                "未找到 ffmpeg（定格取材以视频为对照；装了的话重启一次"
                "画坊即可，或设 DAUB_FFMPEG）")
            return
        video = self._newest_video()
        if video is None:
            self.lb_status.setText(
                "没有找到本计划的定格视频（先「出视频」，再定格取材）")
            return
        # snapshot the CURRENT edit: the user may keep editing while the
        # extraction runs (the edit timer rewrites _edit.json in place)
        snap = os.path.join(self.workdir, "_frame_plan.json")
        with open(snap, "w") as fh:
            json.dump(self._edited(), fh)
        outd = self._export_dir()
        os.makedirs(outd, exist_ok=True)
        if os.environ.get("DAUB_FFMPEG") != ff:
            os.environ["DAUB_FFMPEG"] = ff   # daub_frame rides rt's probe
        try:
            import PIL   # noqa: F401  pixel verification needs PIL+numpy
            verify = "auto"
        except ImportError:
            # frozen GUI bundles numpy, not PIL: ride the frame-count
            # pin alone (daub_frame logs the skip loudly)
            verify = "skip"
        self._frame_busy = True
        self.lb_status.setText(
            "定格取材中 t=%gs（引擎重渲该刻，真计划需数十秒）…"
            % self.sp_frame.value())
        self.frame_task.start(video=video, seconds=self.sp_frame.value(),
                              plan=snap, out_dir=outd,
                              order=self.order_fn(), daub=eng[0],
                              cal=INK_CALIB, tips=TIPS_JSON,
                              verify=verify,
                              do_cut=self.ck_fcut.isChecked(),
                              do_kra=self.ck_fkra.isChecked(),
                              do_psd=self.ck_fpsd.isChecked())

    def _frame_done(self, ok, msg):
        self._frame_busy = False
        self.lb_status.setText(msg)
        if ok:
            self.status_msg.emit(msg)

    def save_plan(self):
        if self.d is None:
            return
        target = self._export_path("_edit_plan", ".json")
        with open(target, "w") as fh:
            json.dump(self._edited(), fh)
        self.lb_status.setText("计划已另存 %s" % os.path.basename(target))
        self.status_msg.emit("计划已另存 " + target)

    def open_folder(self):
        if self.plan_path:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(self.plan_path)))

    def shutdown(self):
        self.runner.kill()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("画坊 · daub atelier")
        # explicit format: PySide6 6.8.3 silently ignores
        # QSettings.setDefaultFormat(IniFormat) - the two-arg
        # constructor still lands in NativeFormat (registry); naming
        # format+scope here is the only way the ini actually happens
        self.settings = QSettings(QSettings.Format.IniFormat,
                                  QSettings.Scope.UserScope,
                                  "daub", "daub_gui")
        self.jobs = []              # all rows, in table order
        self._stems = set()         # claimed stems (collision set)
        self.snap = LiveGrowth(self)
        self.snap.frame.connect(self._on_snap_frame)
        self.wb = Workbench()
        self.wb.order_fn = self._tl_order

        tabs = QTabWidget()
        tabs.addTab(self._build_queue_page(), "批量队列 · queue")
        tabs.addTab(self.wb, "图层工作台 · layers")
        self.tabs = tabs
        self.wb.status_msg.connect(
            lambda m: self.statusBar().showMessage(m, 8000))
        self.setCentralWidget(tabs)
        self.resize(1280, 800)
        self._ffmpeg_ok = resolve_ffmpeg() is not None
        self._load_settings()
        if not self._ffmpeg_ok:
            self.ck_mp4.setEnabled(False)
            self.cb_order.setEnabled(False)
            self.ck_mp4.setToolTip(
                "未找到 ffmpeg（装了的话重启一次画坊即可；或设 DAUB_FFMPEG）")

    # ---- UI construction ----

    def _build_queue_page(self):
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(16, 8, 16, 16)
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        for text, fn in (("添加图片", self.add_files),
                         ("添加文件夹", self.add_folder),
                         ("打开计划", self.open_plan),
                         ("清除完成", self.clear_done)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            r1.addWidget(b)
        r1.addStretch(1)
        lab = QLabel("输出目录")
        lab.setObjectName("fieldLab")
        r1.addWidget(lab)
        self.ed_out = QLineEdit()
        r1.addWidget(self.ed_out, 1)
        b = QPushButton("浏览")
        b.clicked.connect(self.pick_outdir)
        r1.addWidget(b)
        ll.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.ck_kra = QCheckBox("同时出 .kra")
        self.ck_psd = QCheckBox("同时出 .psd")
        self.ck_psd.setToolTip("分层 PSD：Photoshop / Clip Studio / "
                               "Affinity 直接打开")
        self.ck_mp4 = QCheckBox("出视频")
        self.ck_live = QCheckBox("实时预览")
        self.ck_live.setChecked(True)
        for w in (self.ck_kra, self.ck_psd, self.ck_mp4, self.ck_live):
            r2.addWidget(w)
        r2.addSpacing(12)
        lab = QLabel("视频顺序")
        lab.setObjectName("fieldLab")
        r2.addWidget(lab)
        self.cb_order = QComboBox()
        self.cb_order.addItem("由大到小", "big_first")
        self.cb_order.addItem("由小到大", "small_first")
        self.cb_order.setToolTip(
            "视频揭示顺序：由大到小 = 色块先铺、细节收尾（默认）；"
            "由小到大 = 细节先起稿、色块最后压场")
        self.cb_order.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.ck_mp4.toggled.connect(self.cb_order.setEnabled)
        r2.addWidget(self.cb_order)
        r2.addSpacing(12)
        lab = QLabel("画笔")
        lab.setObjectName("fieldLab")
        r2.addWidget(lab)
        self.cb_pen = QComboBox()
        self.cb_pen.setEditable(True)
        self.cb_pen.addItem("")       # empty = 自动
        for _p in load_presets():
            self.cb_pen.addItem(zh(_p))
            self.cb_pen.setItemData(self.cb_pen.count() - 1, _p,
                                    Qt.ToolTipRole)  # raw key on hover
        self.cb_pen.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        r2.addWidget(self.cb_pen)
        r2.addSpacing(12)
        lab = QLabel("并发")
        lab.setObjectName("fieldLab")
        r2.addWidget(lab)
        self.sp_conc = QSpinBox()
        self.sp_conc.setRange(1, 3)
        r2.addWidget(self.sp_conc)
        r2.addStretch(1)
        self.bt_start = QPushButton("开始")
        self.bt_start.setObjectName("primary")
        self.bt_start.clicked.connect(self.start_queue)
        r2.addWidget(self.bt_start)
        self.bt_cancel = QPushButton("取消选中")
        self.bt_cancel.clicked.connect(self.cancel_selected)
        r2.addWidget(self.bt_cancel)
        ll.addLayout(r2)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["图", "文件", "状态", "结果", "操作"])
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        # QStyle's default iconSize is 16px - the 48px thumbnails the
        # loader prepares have been silently shrunk to that all along
        self.table.setIconSize(QSize(48, 48))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for i, w in ((0, 60), (2, 250), (3, 190), (4, 104)):
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._on_sel_changed)
        self.table.cellDoubleClicked.connect(self._on_double)
        ll.addWidget(self.table, 1)

        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        psplit = QSplitter(Qt.Vertical)    # stack ref/result vertically
        psplit.setHandleWidth(12)
        self.ref_box = ZoomBox("参考图 · reference",
                               "参考图会落在这里 ✧")
        self.out_box = ZoomBox("成品 · watch it grow ✦",
                               "渲染开始后，画会在这里生长 ✦")
        psplit.addWidget(SquareSlot(self.ref_box))
        psplit.addWidget(SquareSlot(self.out_box))
        # equal halves regardless of the cards' sizeHints (longer
        # placeholder captions would otherwise buy a wider cell)
        psplit.setSizes([500, 500])
        rl.addWidget(psplit)
        split.addWidget(right)
        split.setSizes([640, 640])
        lay.addWidget(split)
        return page

    # ---- settings ----

    def _tl_order(self):
        """Video reveal order from the queue option row (legacy-safe)."""
        return self.cb_order.currentData() or "big_first"

    def _load_settings(self):
        s = self.settings
        self.ed_out.setText(s.value("out_dir", "", str))
        self.ck_kra.setChecked(s.value("do_kra", "false") in
                               (True, "true", "1"))
        self.ck_psd.setChecked(s.value("do_psd", "false") in
                               (True, "true", "1"))
        self.ck_mp4.setChecked(s.value("do_timelapse", "false") in
                               (True, "true", "1") and self._ffmpeg_ok)
        self.cb_order.setCurrentIndex(max(
            0, self.cb_order.findData(s.value("tl_order", "big_first", str))))
        self.ck_live.setChecked(s.value("do_live", "true") in
                                (True, "true", "1"))
        # stored value is the raw key - display its Chinese name
        self.cb_pen.setCurrentText(zh(s.value("pen", "", str)))
        self.sp_conc.setValue(int(s.value("concurrency", 1) or 1))
        self.wb.sp_frame.setValue(float(s.value("frame_s", 1.0) or 1.0))
        self.wb.ck_fcut.setChecked(s.value("frame_cut", "true") in
                                   (True, "true", "1"))
        self.wb.ck_fkra.setChecked(s.value("frame_kra", "true") in
                                   (True, "true", "1"))
        self.wb.ck_fpsd.setChecked(s.value("frame_psd", "true") in
                                   (True, "true", "1"))
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)

    def _save_settings(self):
        s = self.settings
        s.setValue("out_dir", self.ed_out.text().strip())
        s.setValue("do_kra", self.ck_kra.isChecked())
        s.setValue("do_psd", self.ck_psd.isChecked())
        s.setValue("do_timelapse", self.ck_mp4.isChecked())
        s.setValue("tl_order", self.cb_order.currentData() or "big_first")
        s.setValue("do_live", self.ck_live.isChecked())
        s.setValue("pen", norm(self.cb_pen.currentText()))
        s.setValue("concurrency", self.sp_conc.value())
        s.setValue("frame_s", self.wb.sp_frame.value())
        s.setValue("frame_cut", self.wb.ck_fcut.isChecked())
        s.setValue("frame_kra", self.wb.ck_fkra.isChecked())
        s.setValue("frame_psd", self.wb.ck_fpsd.isChecked())
        s.setValue("geometry", self.saveGeometry())

    # ---- adding rows ----

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "添加参考图", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*)")
        self._add_many(files)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "添加文件夹")
        if not d:
            return
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
        self._add_many([os.path.join(d, f) for f in sorted(os.listdir(d))
                        if f.lower().endswith(exts)])

    def open_plan(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "打开计划 JSON", "", "计划 (*.json)")
        ok = 0
        for path in paths:
            if self.wb.load_plan(path):
                ok += 1
        if ok:
            self.tabs.setCurrentWidget(self.wb)

    def _add_many(self, files):
        if not files:
            return
        out_dir = self.ed_out.text().strip()
        if not out_dir:
            QMessageBox.information(self, "画坊", "先选输出目录")
            return
        os.makedirs(out_dir, exist_ok=True)
        added = skipped = 0
        first_added = None
        for src in files:
            src = os.path.abspath(src)
            if not os.path.isfile(src):
                continue
            key = os.path.normcase(src)
            busy = any(os.path.normcase(j.src) == key and
                       j.status not in (ST_FAIL, ST_CANCEL)
                       for j in self.jobs)
            if busy:
                skipped += 1
                continue
            stem = self._unique_stem(src, out_dir)
            row = self.table.rowCount()
            self.table.insertRow(row)
            it = QTableWidgetItem(os.path.basename(src))
            it.setToolTip(src)
            self.table.setItem(row, 1, it)
            self.table.setItem(row, 2, QTableWidgetItem(ST_QUEUED))
            self.table.setItem(row, 3, QTableWidgetItem(""))
            self.table.setItem(row, 4, QTableWidgetItem(""))
            job = Job(src, out_dir, stem, self.ck_kra.isChecked(),
                      self.ck_mp4.isChecked(), self.ck_psd.isChecked(),
                      row)
            self.jobs.append(job)
            self._stems.add(os.path.normcase(os.path.join(out_dir, stem)))
            added += 1
            if first_added is None:
                first_added = job
        if added:
            QTimer.singleShot(30, self._load_thumbs)
            # a fresh queue with no selection would keep the ref/result
            # panes empty AND block the live-growth attach (it gates on
            # the row being selected) - select the first new row
            if self.table.selectionModel().currentIndex().row() < 0:
                self.table.selectRow(first_added.row)
        if skipped:
            self.statusBar().showMessage(
                "跳过 %d 个重复/进行中条目" % skipped, 5000)

    def _unique_stem(self, src, out_dir):
        # collisions decided at add time against deliverables on disk
        # (+ claimed stems); the plan JSON/workdir are interior
        # artifacts, safe to overwrite on a re-run
        base = os.path.splitext(os.path.basename(src))[0]
        n, stem = 0, base
        while True:
            here = os.path.normcase(os.path.join(out_dir, stem))
            clash = here in self._stems or any(
                os.path.exists(os.path.join(out_dir, stem + suf))
                for suf in (".png", ".kra", ".psd", "_timelapse.mp4"))
            if not clash:
                return stem
            n += 1
            stem = "%s-%d" % (base, n)

    def _load_thumbs(self):
        for j in self.jobs:
            if self.table.item(j.row, 0) is not None:
                continue
            rd = QImageReader(j.src)
            rd.setAutoTransform(True)
            rd.setScaledSize(QSize(96, 96))
            pm = QPixmap.fromImageReader(rd)
            if not pm.isNull():
                self.table.setItem(j.row, 0, QTableWidgetItem(
                    QIcon(pm.scaled(48, 48, Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)), ""))

    # ---- queue engine ----

    def start_queue(self):
        eng, src = resolve_engine()
        if not eng:
            QMessageBox.warning(self, "画坊", src)
            return
        self._start_next()

    def _start_next(self):
        eng, _ = resolve_engine()
        if not eng:
            return
        running = sum(1 for j in self.jobs if j.runner is not None)
        conc = self.sp_conc.value()
        for j in self.jobs:
            if running >= conc:
                break
            if j.status == ST_QUEUED:
                self._launch_job(j, eng)
                running += 1
                # started with no row selected -> nothing would show in
                # the result pane and the growth chain could not attach
                if self.table.selectionModel().currentIndex().row() < 0:
                    self.table.selectRow(j.row)

    def _launch_job(self, j, eng):
        pen = norm(self.cb_pen.currentText())
        argv = eng + [j.src, j.out_png]
        if pen:
            argv += ["--pen", pen]
        if j.kra_path:
            argv += ["--kra", j.kra_path]
        if j.psd_path:
            argv += ["--psd", j.psd_path]
        if j.mp4_path:
            argv += ["--timelapse", j.mp4_path]
            if self._tl_order() == "small_first":
                argv += ["--timelapse-order", "small_first"]
        os.makedirs(j.workdir, exist_ok=True)
        j.status = ST_RUN
        self._set_status(j, "启动中…")
        j.runner = EngineRunner(self)
        j.runner.line.connect(lambda t, jj=j: self._on_job_line(jj, t))
        j.runner.done.connect(
            lambda code, crash, jj=j: self._on_job_done(jj, code, crash))
        j.runner.start(argv)
        # live growth from the job's first second (not just post-plan)
        if self._row_selected(j) and self.ck_live.isChecked():
            QTimer.singleShot(0, lambda jj=j: self._maybe_snap(jj))

    def _set_status(self, j, text):
        self.table.item(j.row, 2).setText(text)

    def _set_result(self, j, text):
        self.table.item(j.row, 3).setText(text)

    def _on_job_line(self, j, text):
        t = text.strip()
        if not t:
            return
        j.tail.append(t)
        m = RX_BAND.match(t)
        if m:
            self._set_status(j, "规划 band %s-%s p%s: %s 笔 (%ss)"
                             % (m.group(1), m.group(2), m.group(3),
                                m.group(4), m.group(5)))
            return
        m = RX_BAND_T.match(t)
        if m:
            self._set_status(j, "band %s-%s 完成: %s 笔"
                             % (m.group(1), m.group(2), m.group(3)))
            return
        m = RX_PLAN_DONE.match(t)
        if m:
            j.strokes = int(m.group(1))
            j.planned = True
            self._set_status(j, "渲染中 %s 笔" % j.strokes)
            # growth chain self-stops at planned; the engine's own final
            # render (checked in _on_job_done) closes the show
            return
        m = RX_DAUB.match(t)
        if m:
            self._set_status(j, "渲染完成 %s 笔 (%ss)"
                             % (m.group(1), m.group(2)))
            return
        m = RX_LAYER.match(t)
        if m:
            self._set_status(j, "视频 frame %s/%s（层 %s）"
                             % (m.group(3), m.group(4), m.group(1)))
            return
        m = RX_VIDEO.match(t)
        if m:
            # holds between layers only emit the % milestone - without
            # this the status column freezes while frames repeat
            self._set_status(j, "视频渲染 %s%%（frame %s/%s）"
                             % (m.group(1), m.group(2), m.group(3)))
            return
        m = RX_TL_DONE.match(t)
        if m:
            self._set_status(j, "视频完成 %s 帧" % m.group(1))
            return
        if RX_DONE.match(t):
            return  # completion handled by _on_job_done's file gate

    def _row_selected(self, j):
        return self.table.selectionModel().currentIndex().row() == j.row

    def _maybe_snap(self, j):
        if j.runner is None or j.planned or j.cancelled:
            return
        if not self._row_selected(j) or not self.ck_live.isChecked():
            return
        if self.snap.active and self.snap.job is j:
            return
        self.snap.start(j)

    def _on_snap_frame(self, job, png, strokes):
        if self._row_selected(job) and self.snap.job is job:
            self.out_box.set_image(png, "生长中 · %s 笔 ✦" % f"{strokes:,}")

    def _on_sel_changed(self):
        idx = self.table.selectionModel().currentIndex().row()
        for j in self.jobs:
            if j.row == idx:
                self.ref_box.set_image(
                    j.src, "参考图 " + os.path.basename(j.src))
                if self.snap.active and self.snap.job is j:
                    return
                # leaving a row whose growth chain is mid-flight: show
                # its final now (the chain below is about to stop)
                prev = self.snap.job
                if self.snap.active and prev is not None and \
                        prev.status == ST_OK:
                    self.out_box.set_image(
                        prev.out_png,
                        "成品 " + os.path.basename(prev.out_png))
                if j.status == ST_OK and os.path.isfile(j.out_png):
                    self.out_box.set_image(
                        j.out_png, "成品 " + os.path.basename(j.out_png))
                    return
                # late retarget: a job that started while unselected
                # starts its chain once selected
                QTimer.singleShot(0, lambda jj=j: self._maybe_snap(jj))
                return
        self.snap.stop()

    def _on_double(self, row, _col):
        for j in self.jobs:
            if j.row == row:
                if os.path.isfile(j.plan_path):
                    if self.wb.load_plan(j.plan_path):
                        self.tabs.setCurrentWidget(self.wb)
                elif j.status == ST_RUN:
                    self.statusBar().showMessage("还没出计划，等规划完成",
                                                 4000)
                return

    def _on_job_done(self, j, code, crashed):
        j.runner = None
        if j.cancelled:
            j.status = ST_CANCEL
            self._set_status(j, "已取消")
        elif code == 0 and os.path.isfile(j.out_png) and \
                os.path.getsize(j.out_png) > 0 and self._plan_ok(j):
            j.status = ST_OK
            self._set_status(j, "✓")
            r = (["%s 笔" % f"{j.strokes:,}"] if j.strokes else [])
            if j.kra_path and os.path.isfile(j.kra_path):
                r.append("+kra")
            if j.psd_path and os.path.isfile(j.psd_path):
                r.append("+psd")
            if j.mp4_path and os.path.isfile(j.mp4_path):
                r.append("+视频")
            self._set_result(j, " ".join(r))
            self._add_open_button(j)
            if self._row_selected(j):
                # growth is over by definition here - the final wins
                self.out_box.set_image(j.out_png,
                                       "成品 " + os.path.basename(j.out_png))
        else:
            j.status = ST_FAIL
            self._set_status(j, "✗" + ("（崩溃）" if crashed else ""))
            tail = " ｜ ".join(list(j.tail)[-2:])[-200:]
            self._set_result(j, tail or "失败（详见日志）")
        if self.snap.job is j:
            self.snap.stop()       # growth ended with the job - final wins
        self._start_next()

    def _plan_ok(self, j):
        """✓ hard gate: the plan header must parse (a 0-byte or half-
        written png alone proves nothing)."""
        try:
            with open(j.plan_path) as fh:
                d = json.load(fh)
            return int(d["count"]) > 0 or len(d["strokes"]) > 0
        except Exception:
            return False

    def _add_open_button(self, j):
        b = QPushButton("工作台")
        b.clicked.connect(lambda _=False, jj=j: self._open_wb(jj))
        self.table.setCellWidget(j.row, 4, b)

    def _open_wb(self, j):
        if os.path.isfile(j.plan_path) and self.wb.load_plan(j.plan_path):
            self.tabs.setCurrentWidget(self.wb)

    def cancel_selected(self):
        idxs = {i.row() for i in
                self.table.selectionModel().selectedRows()}
        for j in self.jobs:
            if j.row in idxs and j.status == ST_RUN:
                j.cancelled = True
                j.runner.kill()
        if self.snap.active and any(
                j.cancelled and j.row in idxs for j in self.jobs):
            self.snap.stop()

    def clear_done(self):
        for j in list(self.jobs):
            if j.status in (ST_OK, ST_FAIL, ST_CANCEL):
                if self.snap.job is j:
                    self.snap.stop()
                self._cleanup_workdir(j)
                self._stems.discard(os.path.normcase(
                    os.path.join(j.out_dir, j.stem)))
                self.jobs.remove(j)
        self._rebuild_table()

    def _cleanup_workdir(self, j):
        shutil.rmtree(j.workdir, ignore_errors=True)

    def _rebuild_table(self):
        # rows are identified by index everywhere; removal rebuilds them
        widgets = [(j, self.table.cellWidget(j.row, 4)) for j in self.jobs]
        self.table.clearContents()
        self.table.setRowCount(len(self.jobs))
        for newrow, (j, w) in enumerate(widgets):
            j.row = newrow
            self.table.setItem(newrow, 1, QTableWidgetItem(
                os.path.basename(j.src)))
            self.table.item(newrow, 1).setToolTip(j.src)
            self.table.setItem(newrow, 2, QTableWidgetItem(j.status))
            self.table.setItem(newrow, 3, QTableWidgetItem(""))
            self.table.setItem(newrow, 4, QTableWidgetItem(""))
            if j.status == ST_OK:
                r = (["%s 笔" % f"{j.strokes:,}"] if j.strokes else [])
                if j.kra_path and os.path.isfile(j.kra_path):
                    r.append("+kra")
                if j.psd_path and os.path.isfile(j.psd_path):
                    r.append("+psd")
                if j.mp4_path and os.path.isfile(j.mp4_path):
                    r.append("+视频")
                self.table.item(newrow, 3).setText(" ".join(r))
            elif j.status == ST_FAIL:
                self.table.item(newrow, 3).setText(
                    " ｜ ".join(list(j.tail)[-2:])[-200:] or "失败")
            if w is not None and j.status == ST_OK:
                b = QPushButton("工作台")
                b.clicked.connect(
                    lambda _=False, jj=j: self._open_wb(jj))
                self.table.setCellWidget(newrow, 4, b)
        QTimer.singleShot(30, self._load_thumbs)

    # ---- misc ----

    def pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "输出目录",
                                             self.ed_out.text() or "")
        if d:
            self.ed_out.setText(d)

    def closeEvent(self, e):
        running = any(j.runner is not None and j.runner.running()
                      for j in self.jobs) or self.wb.runner.running()
        if running:
            r = QMessageBox.question(
                self, "画坊", "有任务运行中，确定退出？",
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.snap.stop()
        for j in self.jobs:
            if j.runner is not None:
                j.runner.kill()
        self.wb.shutdown()
        for j in self.jobs:
            self._cleanup_workdir(j)
        if self.wb.workdir:
            shutil.rmtree(self.wb.workdir, ignore_errors=True)
        self._save_settings()
        super().closeEvent(e)


def main():
    if "--selfcheck" in sys.argv[1:]:
        return selfcheck()
    setup_logging()
    install_crash_visibility()
    log.info("daub_gui start (frozen=%s, argv=%s)", FROZEN, sys.argv[1:])
    app = QApplication(sys.argv)
    app.setApplicationName("daub_gui")
    app.setOrganizationName("daub")
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI",
                   "Microsoft YaHei UI"])
    f.setPointSize(9)
    app.setFont(f)
    app.setStyleSheet(STYLESHEET)
    icon = os.path.join(sys._MEIPASS if FROZEN else HERE,
                        "data", "app_icon.ico")
    if os.path.isfile(icon):
        app.setWindowIcon(QIcon(icon))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("fatal")
        raise
