/* daub web replay — plan JSON 的浏览器逐笔回放器（笔模 v2）
 *
 * 诚实边界（只报数、不吹收敛）：JS 笔模是 daub 校准渲染器
 * 的近似。v2 模型=最朴素公式（宽=size×端点压力、alpha=笔常量），是
 * 三个候选同 harness 实测的胜者（mean|diff| vs daub.exe 真渲：朴素
 * 端点 6.75 ≪ 家族参数 9.89 ≪ 朴素中点 14.61）——规划器已把每笔的
 * size/opacity 调好，额外整形帮倒忙。保真度由 _smoke/_check_replay.py
 * 量化入库 fidelity_baseline.json；逐像素忠实的路径是 render.rs 的
 * WASM 移植（backlog T6）。
 *
 * z 序契约（W11 统一尺子）：计划数组序即绘制序——F1 床在最前，其余全局
 * 宽→窄，小笔永远后画（=在上）。回放器不做任何重排，直接顺序重放。
 *
 * 双表面：drawStroke（canvas2d，交互回放）与 renderSVG（SVG 串行化，
 * 无头保真度采集+矢量导出）共享同一套数学。headless 的 canvas2d 在
 * 大计划下软件光栅腐败（显示黑、读回零，T2 实证），SVG 4 万元素稳定。
 *
 * 零依赖、零构建：一个 <script> 就能跑，file:// 直开。
 */
(function () {
  "use strict";

  // 笔模 v2（T2 数据裁决）：宽 = size × 端点压力，透明度 = 该笔 opacity
  // 常量，无家族整形。三种候选同 harness 实测（512 样例 vs daub.exe
  // 真渲 mean|diff|）：朴素端点 6.75 < 家族参数 9.89 < 朴素中点 14.61
  // ——规划器已把 size/opacity 调好，额外整形只会帮倒忙。
  function modelWidth(s, p) {
    return Math.max(0.4, s.size * p);
  }

  // 计划 color 契约 = 十六进制字符串 "#rrggbb"（daub 计划头同款）；
  // [r,g,b] 数组兜底。曾因直接当数组拼 rgb() 产出非法色串，strokeStyle
  // 静默保持默认黑——整幅回放全黑（T2 抓获并修死）。
  function colorParse(c) {
    if (typeof c === "string" && c.charCodeAt(0) === 35 /* # */) {
      const h = c.length === 7 ? c
        : "#" + c.slice(1).split("").map((ch) => ch + ch).join("");
      return [parseInt(h.slice(1, 3), 16),
              parseInt(h.slice(3, 5), 16),
              parseInt(h.slice(5, 7), 16)];
    }
    return Array.isArray(c) ? c : [0, 0, 0];
  }

  function cssColor(c) {
    return "rgb(" + colorParse(c).join(",") + ")";
  }

  // 一笔 = 逐段折线，圆帽圆角；宽=端点压力、alpha=笔常量（见笔模 v2）。
  function drawStroke(ctx, s) {
    const pts = s.points;
    if (!pts || pts.length < 2) return;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = cssColor(s.color);
    ctx.globalAlpha = Math.min(1, s.opacity);
    let px = pts[0][0], py = pts[0][1];
    for (let i = 1; i < pts.length; i++) {
      const x = pts[i][0], y = pts[i][1];
      ctx.lineWidth = modelWidth(s, pts[i][2]);
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(x, y);
      ctx.stroke();
      px = x; py = y;
    }
    ctx.globalAlpha = 1;
  }

  // SVG 序列化器：与 drawStroke 同一套家族数学（宽锥/透明度锥/软边双
  // 遍），逐段 <line>。用途①无头冒烟的保真度采集——headless 的
  // canvas2d 在 ~500-2000 笔后软件光栅腐败（显示黑、读回全零，T2
  // 实证），SVG 光栅 4 万元素实测稳定；②矢量导出表面。
  function renderSVG(doc, opts) {
    opts = opts || {};
    const W = doc.canvas[0], H = doc.canvas[1];
    const out = ['<svg xmlns="http://www.w3.org/2000/svg" width="' + W
      + '" height="' + H + '" viewBox="0 0 ' + W + " " + H + '">'];
    if (doc.bg) out.push('<rect width="' + W + '" height="' + H
      + '" fill="rgb(' + colorParse(doc.bg).join(",") + ')"/>');
    const muted = opts.muted || null;
    for (const s of doc.strokes) {
      if (muted && muted.has(s.layer)) continue;
      const pts = s.points;
      if (!pts || pts.length < 2) continue;
      const col = cssColor(s.color);
      const a = Math.min(1, s.opacity);
      let px = pts[0][0], py = pts[0][1];
      for (let i = 1; i < pts.length; i++) {
        const x = pts[i][0], y = pts[i][1];
        const w = modelWidth(s, pts[i][2]);
        out.push('<line x1="' + px.toFixed(1) + '" y1="' + py.toFixed(1)
          + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1)
          + '" stroke="' + col + '" stroke-width="' + w.toFixed(1)
          + '" stroke-opacity="' + a.toFixed(3)
          + '" stroke-linecap="round"/>');
        px = x; py = y;
      }
    }
    out.push("</svg>");
    return out.join("");
  }

  class PlanPlayer {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.listeners = {};
      this.reset();
    }

    reset() {
      this.doc = null;
      this.strokes = [];
      this.layers = [];
      this.muted = new Set();
      this.i = 0;               // 已画笔数（游标）
      this.playing = false;
      this.speed = 1;
      this.segmentsPerFrame = 400;   // 播放节流：按段数推进
      this.totalSegments = 0;
      this._raf = null;
    }

    load(doc) {
      if (!doc || !doc.strokes || !doc.canvas) {
        throw new Error("not a daub plan (need canvas + strokes)");
      }
      this.reset();
      this.doc = doc;
      this.canvas.width = doc.canvas[0];
      this.canvas.height = doc.canvas[1];
      this.strokes = doc.strokes;
      this.totalSegments = 0;
      const seen = [];
      for (const s of this.strokes) {
        this.totalSegments += Math.max(1, (s.points ? s.points.length : 1) - 1);
        if (seen.indexOf(s.layer) < 0) seen.push(s.layer);   // 首现序=层栈序
      }
      this.layers = seen;
      this.redraw(0);
      this.emit("loaded", { count: this.strokes.length, layers: this.layers,
                            canvas: doc.canvas });
    }

    // 层静音：Set(可见层名)。静音=整层从重放中剔除，不重排其余。
    setVisible(visibleSet) {
      this.muted = new Set(
        this.layers.filter((l) => visibleSet && !visibleSet.has(l)));
      this.redraw(this.i);
    }

    // 重放前 n 笔（seek 与静音都走这里）。bg 铺底严格按计划头。
    redraw(n) {
      const W = this.canvas.width, H = this.canvas.height;
      const ctx = this.ctx;
      ctx.save();
      ctx.globalAlpha = 1;
      ctx.fillStyle = this.doc && this.doc.bg
        ? "rgb(" + this.doc.bg.join(",") + ")" : "#ffffff";
      ctx.fillRect(0, 0, W, H);
      const t0 = performance.now();
      for (let k = 0; k < n; k++) {
        const s = this.strokes[k];
        if (this.muted.has(s.layer)) continue;
        drawStroke(ctx, s);
      }
      ctx.restore();
      this.i = n;
      this.emit("progress", { drawn: n, total: this.strokes.length,
                              ms: performance.now() - t0 });
    }

    // 播放节拍：默认全程 ~45s（1x），按段数推进让 F1 床与细节匀速展开。
    segmentsPerSecond() {
      return this.totalSegments / 45;
    }

    play() {
      if (this.playing || !this.doc) return;
      if (this.i >= this.strokes.length) this.redraw(0);
      this.playing = true;
      this.emit("state", { playing: true });
      // 双驱动：真人看 = RAF 平滑；无头虚拟时间 = setTimeout（headless
      // 的 virtual-time 预算下 RAF 只跑一两帧，timer 才可靠——T3 实测）。
      const schedule = this.useTimeoutLoop
        ? (fn) => { this._raf = setTimeout(fn, 0); }
        : (fn) => { this._raf = requestAnimationFrame(fn); };
      const cancel = this.useTimeoutLoop
        ? () => clearTimeout(this._raf)
        : () => cancelAnimationFrame(this._raf);
      const step = () => {
        if (!this.playing) return;
        const budget = Math.max(4,
          Math.round(this.segmentsPerSecond() * this.speed / 60));
        let used = 0;
        const start = this.i;
        while (this.i < this.strokes.length && used < budget) {
          const s = this.strokes[this.i];
          used += Math.max(1, (s.points ? s.points.length : 1) - 1);
          if (!this.muted.has(s.layer)) drawStroke(this.ctx, s);
          this.i += 1;
        }
        if (this.i > start) this.emit("progress", { drawn: this.i,
                                                    total: this.strokes.length });
        if (this.i >= this.strokes.length) {
          this.playing = false;
          this.emit("state", { playing: false, done: true });
          return;
        }
        schedule(step);
      };
      schedule(step);
      this._cancel = cancel;
    }

    pause() {
      this.playing = false;
      if (this._cancel) this._cancel();
      this._raf = null;
      this.emit("state", { playing: false });
    }

    seek(fraction) {
      const wasPlaying = this.playing;
      this.pause();
      this.redraw(Math.max(0, Math.min(1, fraction)) * this.strokes.length | 0);
      if (wasPlaying) this.play();
    }

    // 浏览器内出分享视频：captureStream → webm。走一遍真实播放。
    async exportWebm(onProgress) {
      if (!this.doc) throw new Error("no plan loaded");
      if (typeof MediaRecorder === "undefined") {
        throw new Error("MediaRecorder unavailable");
      }
      const stream = this.canvas.captureStream(30);
      const mime = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
        ? "video/webm;codecs=vp9" : "video/webm";
      const rec = new MediaRecorder(stream, { mimeType: mime,
                                              videoBitsPerSecond: 8e6 });
      const chunks = [];
      rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      const stopped = new Promise((res) => { rec.onstop = res; });
      rec.start();
      this.pause();
      this.redraw(0);
      await new Promise((res) => {
        const onP = (st) => { if (onProgress) onProgress(st); };
        this.on("progress", onP);
        this.on("state", (st) => {
          if (st.done) { this.off("progress", onP); res(); }
        });
        this.speed = this.speed;      // 1x 语义交给上层控制
        this.play();
      });
      rec.stop();
      await stopped;
      return new Blob(chunks, { type: "video/webm" });
    }

    on(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
    off(ev, fn) {
      const a = this.listeners[ev] || [];
      const k = a.indexOf(fn);
      if (k >= 0) a.splice(k, 1);
    }
    emit(ev, data) {
      (this.listeners[ev] || []).slice().forEach((fn) => fn(data));
    }
  }

  // 供演示页与冒烟探针使用
  window.PlanPlayer = PlanPlayer;
  window.daubReplay = { PlanPlayer, drawStroke, renderSVG, colorParse };
})();
