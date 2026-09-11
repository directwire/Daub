//! Parallel stroke-directory renderer.
//!
//! Semantics mirror real Krita where it matters and the calibrated sim
//! everywhere else:
//! - layer stack = first-seen order of `layer` in the strokes array
//!   (planners create layers in paint order, coarse bands first);
//! - WITHIN a stroke, overlapping dabs do not accumulate (opacity
//!   brush): dabs merge into a per-stroke scratch mask by max-alpha,
//!   then the whole stroke composites once;
//! - ACROSS strokes and across layers, normal alpha-over accumulation.
//!
//! Layers render independently (rayon across layers, strict stroke
//! order within a layer), then composite over the paper ground.
//!
//! Engine routing (Phase 1): a stroke goes through the tip-stamp engine
//! iff its layer is in the registry's `layers_tips` AND its preset is
//! registered, tips-eligible AND the truth gate passed. Everything else
//! renders through the calibrated capsule path - so band/v5 baselines
//! stay byte-identical until a gate deliberately flips. A preset with
//! neither a tip engine nor a capsule calibration fails loud.

use crate::brushlib::BrushLib;
use crate::calib::{hex_rgb, Calib};
use crate::preset::{KppPreset, SpacingModel};
use crate::tip;
use anyhow::{Context, Result};
#[cfg(not(target_arch = "wasm32"))]
use rayon::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Deserialize)]
pub struct Directory {
    pub canvas: (u32, u32),
    /// Plan-level ground colour (stroke_engine.plan writes the
    /// reference's border median). The Krita paint path has always
    /// created its canvas with this colour; preferring it here aligns
    /// the headless render with that truth path - paper-skip strata
    /// (near-ground fields the planner leaves to the ground) otherwise
    /// show the fixed pale paper instead, reading as ghost patches on
    /// photo references. Falls back to the calibration's bg_sim.
    #[serde(default)]
    pub bg: Option<[u8; 3]>,
    pub strokes: Vec<Stroke>,
}

#[derive(Deserialize)]
pub struct Stroke {
    pub points: Vec<[f64; 3]>, // x, y, pressure
    pub size: f64,
    pub color: String,
    pub layer: String,
    pub preset: String,
    pub opacity: f64,
}

pub struct LayerBuf {
    pub rgba: Vec<u8>, // RGB + A (A=0 unpainted)
}

// composite() takes owned or borrowed buffers; std has no reflexive
// AsRef blanket (unlike Borrow) - this one plus core's `&T` forwarding
// impl cover both.
impl AsRef<LayerBuf> for LayerBuf {
    fn as_ref(&self) -> &LayerBuf { self }
}

/// One scratch-masked stroke, ready to composite exactly once.
pub(crate) struct Stamp {
    pub(crate) x0: usize,
    pub(crate) y0: usize,
    pub(crate) w: usize,
    pub(crate) h: usize,
    pub(crate) alpha: Vec<u8>,
}

impl Stamp {
    /// One-shot stroke composite (opacity semantics) into the layer.
    /// The single code path both engines share, so .kra layer
    /// invariants (premultiplied RGB, A = max dab alpha) hold for both.
    pub(crate) fn composite(&self, buf: &mut LayerBuf, w: usize, rgb: [u8; 3]) {
        for y in 0..self.h {
            for x in 0..self.w {
                let a8 = self.alpha[y * self.w + x];
                if a8 == 0 {
                    continue;
                }
                let a = a8 as f32 / 255.0;
                let i = ((y + self.y0) * w + x + self.x0) * 4;
                for c in 0..3 {
                    let d = buf.rgba[i + c] as f32;
                    buf.rgba[i + c] = (d * (1.0 - a) + rgb[c] as f32 * a).round() as u8;
                }
                buf.rgba[i + 3] = buf.rgba[i + 3].max(a8);
            }
        }
    }
}

/// Clamped stroke bbox with `pad` margins; (minx, miny, maxx, maxy)
/// with maxx/maxy exclusive, empty when the stroke falls off-canvas.
pub(crate) fn stroke_bbox(points: &[[f64; 3]], pad: f64, w: usize, h: usize)
              -> (usize, usize, usize, usize) {
    let minx = (points.iter().map(|p| p[0]).fold(f64::INFINITY, f64::min)
                - pad).floor().max(0.0) as usize;
    let maxx = ((points.iter().map(|p| p[0]).fold(f64::NEG_INFINITY, f64::max)
                + pad).ceil() as usize + 1).min(w);
    let miny = (points.iter().map(|p| p[1]).fold(f64::INFINITY, f64::min)
                - pad).floor().max(0.0) as usize;
    let maxy = ((points.iter().map(|p| p[1]).fold(f64::NEG_INFINITY, f64::max)
                + pad).ceil() as usize + 1).min(h);
    (minx, miny, maxx, maxy)
}

/// Tip-engine context: the registry plus the parsed presets this
/// directory actually needs. Shared read-only across rayon layers;
/// per-layer sprite caches live in the layer closures.
pub struct Tips {
    pub lib: BrushLib,
    /// resource name -> (stable compact id, parsed preset), first-seen
    /// stroke order. Only presets that can actually render tips land
    /// here (registered + eligible + gate passed).
    pub presets: HashMap<String, (u32, KppPreset)>,
    pub dabs: AtomicU64,
    pub bakes: AtomicU64,
    pub degraded: AtomicU64,
}

impl Tips {
    /// Parse the .kpp of every tips-eligible preset used on a tip layer
    /// in `dir`. Unused registrations stay unparsed; a broken file for
    /// a preset we NEED is a hard error (no silent fallback).
    pub fn build(dir: &Directory, lib: BrushLib) -> Result<Tips> {
        let mut presets = HashMap::new();
        // engine-rejected registrations: skip re-parse and warn once, not
        // once per stroke (Ink-2 on a real plan fired this 200+ times)
        let mut rejected: HashMap<String, String> = HashMap::new();
        let mut id = 0u32;
        for s in &dir.strokes {
            if !lib.layer_tips(&s.layer) || presets.contains_key(&s.preset) {
                continue;
            }
            if rejected.contains_key(&s.preset) {
                continue;
            }
            let Some(e) = lib.entry(&s.preset) else {
                // on a tip layer but unregistered: only renderable if a
                // capsule calibration exists - checked at stroke time
                continue;
            };
            if !e.tips_ok() {
                continue;
            }
            let mut p = KppPreset::load(&e.kpp, &s.preset)
                .with_context(|| format!("tip preset '{}'", s.preset))?;
            // Registry probe measurement refines the cadence when it is
            // actually measured (never the 0.25 schema default); the Auto
            // model stays structural - a scalar cannot express sqrt(d).
            if !matches!(p.spacing, SpacingModel::Auto(_))
                && matches!(e.spacing_source.as_str(), "probe" | "measured")
            {
                p.spacing = SpacingModel::Linear(e.spacing);
            }
            if let Some(reason) = p.unsupported {
                // registry admitted it, the engine disagrees - the registry
                // probe was blind to something (e.g. reversed-attribute
                // params). Never render through an unmodelled engine.
                eprintln!("daub: preset '{}' registry tips-ok but engine \
                           rejects ({reason}) - capsule", s.preset);
                rejected.insert(s.preset.clone(), reason);
                continue;
            }
            presets.insert(s.preset.clone(), (id, p));
            id += 1;
        }
        Ok(Tips {
            lib,
            presets,
            dabs: AtomicU64::new(0),
            bakes: AtomicU64::new(0),
            degraded: AtomicU64::new(0),
        })
    }

    /// Effective cadence model for a tip preset (registry override is
    /// baked in at build time).
    #[allow(dead_code)]
    fn spacing(&self, name: &str) -> SpacingModel {
        self.presets.get(name).map(|p| p.1.spacing).unwrap_or_default()
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Engine {
    Capsule,
    Tip,
}

/// Per-stroke engine choice (layer-level degradation is applied on top
/// by the caller when the dab circuit breaker trips).
fn engine_for(s: &Stroke, tips: Option<&Tips>) -> Engine {
    match tips {
        Some(t) if t.lib.layer_tips(&s.layer)
            && t.presets.contains_key(&s.preset) => Engine::Tip,
        _ => Engine::Capsule,
    }
}

fn stamp_capsule(scratch: &mut [u8], sw: usize, sh: usize,
                 x1: f64, y1: f64, x2: f64, y2: f64, r: f64, a: u8) {
    // hard-edged capsule (PIL's line+caps equivalent, aliased like the
    // real brush's aliased core); distance-to-segment per bbox pixel
    let minx = (x1.min(x2) - r).floor().max(0.0) as usize;
    let maxx = ((x1.max(x2) + r).ceil() as usize + 1).min(sw);
    let miny = (y1.min(y2) - r).floor().max(0.0) as usize;
    let maxy = ((y1.max(y2) + r).ceil() as usize + 1).min(sh);
    if minx >= maxx || miny >= maxy {
        return;
    }
    let dx = x2 - x1;
    let dy = y2 - y1;
    let len2 = dx * dx + dy * dy;
    let r2 = r * r;
    for y in miny..maxy {
        let py = y as f64 + 0.5;
        let row = &mut scratch[y * sw..y * sw + sw];
        for x in minx..maxx {
            let px = x as f64 + 0.5;
            let t = if len2 > 0.0 {
                (((px - x1) * dx + (py - y1) * dy) / len2).clamp(0.0, 1.0)
            } else {
                0.0
            };
            let ex = px - (x1 + t * dx);
            let ey = py - (y1 + t * dy);
            if ex * ex + ey * ey <= r2 && row[x] < a {
                row[x] = a;
            }
        }
    }
}

fn render_stroke(buf: &mut LayerBuf, w: usize, h: usize, s: &Stroke,
                 cal: &Calib) -> Result<()> {
    anyhow::ensure!(s.points.len() >= 2, "single-point stroke (engine pit)");
    let rgb = hex_rgb(&s.color)?;
    // stroke bbox with the widest segment's radius as padding
    let mut pad = 1.0f64;
    for p in &s.points {
        let wf = cal.width_at(&s.preset, s.size, p[2])
            .with_context(|| format!("preset {}", s.preset))?;
        pad = pad.max(wf * 0.5);
    }
    let (minx, miny, maxx, maxy) = stroke_bbox(&s.points, pad, w, h);
    if minx >= maxx || miny >= maxy {
        return Ok(());
    }
    let bw = maxx - minx;
    let bh = maxy - miny;
    let mut scratch = vec![0u8; bw * bh];
    for seg in s.points.windows(2) {
        let pm = (seg[0][2] + seg[1][2]) / 2.0;
        let wd = cal.width_at(&s.preset, s.size, pm)
            .with_context(|| format!("preset {}", s.preset))?;
        let a = cal.alpha_at(&s.preset, pm, s.opacity)
            .with_context(|| format!("preset {}", s.preset))?;
        let a8 = (a * 255.0).round() as u8;
        if a8 == 0 {
            continue;
        }
        stamp_capsule(&mut scratch, bw, bh,
                      seg[0][0] - minx as f64, seg[0][1] - miny as f64,
                      seg[1][0] - minx as f64, seg[1][1] - miny as f64,
                      wd / 2.0, a8);
    }
    Stamp { x0: minx, y0: miny, w: bw, h: bh, alpha: scratch }
        .composite(buf, w, rgb);
    Ok(())
}

/// Render the directory: returns per-layer buffers in stack order
/// (bottom first) plus the paper ground. Parallelism: the caller
/// configures the rayon global pool; layers render concurrently, strict
/// stroke order within each layer. `tips` enables the tip engine where
/// the registry allows it.
pub fn render(dir: &Directory, cal: &Calib, tips: Option<&Tips>)
              -> Result<([u8; 3], Vec<(String, LayerBuf)>, (usize, usize))> {
    render_prefix(dir, dir.strokes.len(), cal, tips)
}

/// Render only the first `k` strokes - the timelapse sequence renders
/// many prefixes of one directory, so the plan is parsed once and each
/// frame is just a shorter slice. Byte-identical to rendering a
/// truncated copy of the directory: same array order, layer stack =
/// first-seen order over the slice (later layers simply don't exist
/// yet, exactly as if the plan had ended there).
pub fn render_prefix(dir: &Directory, k: usize, cal: &Calib, tips: Option<&Tips>)
              -> Result<([u8; 3], Vec<(String, LayerBuf)>, (usize, usize))> {
    let strokes = &dir.strokes[..k.min(dir.strokes.len())];
    let (w, h) = (dir.canvas.0 as usize, dir.canvas.1 as usize);
    let mut order: Vec<String> = Vec::new();
    for s in strokes {
        if !order.contains(&s.layer) {
            order.push(s.layer.clone());
        }
    }
    let paper = dir.bg.unwrap_or_else(|| cal.bg());
    let pools: Vec<(String, Vec<&Stroke>)> = order.iter().map(|name| {
        (name.clone(), strokes.iter().filter(|s| &s.layer == name)
         .collect::<Vec<_>>())
    }).collect();

    // wasm32 无线程：rayon par_iter 运行时会 panic（T6），顺序回退；
    // native 保持跨层并行，两条路径共用同一闭包，逐位一致。
    let render_pool = |pool: &(String, Vec<&Stroke>)| -> Result<(String, LayerBuf)> {
        let (name, strokes) = pool;
        let mut buf = LayerBuf { rgba: vec![0u8; w * h * 4] };
        let tip_layer = layer_tip_decision(strokes, tips, name);
        render_pool_strokes(&mut buf, w, h, name, strokes, tip_layer,
                            tips, cal)?;
        Ok((name.clone(), buf))
    };
    #[cfg(target_arch = "wasm32")]
    let mut rendered: Vec<(String, LayerBuf)> =
        pools.iter().map(render_pool).collect::<Result<Vec<_>>>()?;
    #[cfg(not(target_arch = "wasm32"))]
    let mut rendered: Vec<(String, LayerBuf)> =
        pools.par_iter().map(render_pool).collect::<Result<Vec<_>>>()?;

    // keep bottom-first stack order (rayon scrambles completion order)
    rendered.sort_by_key(|(n, _)| order.iter().position(|x| x == n).unwrap());
    Ok((paper, rendered, (w, h)))
}

/// The circuit-breaker decision for one layer's stroke set: tip engine
/// iff the layer stays within the dab budget. Shared by the fresh
/// render and the sequence renderer so both degrade identically.
fn layer_tip_decision(strokes: &[&Stroke], tips: Option<&Tips>, name: &str)
                      -> bool {
    let Some(t) = tips else { return false };
    let mut est = 0u64;
    for s in strokes {
        if let Some((_, p)) = t.presets.get(&s.preset) {
            est += tip::estimate_dabs(s, p);
        }
    }
    let ok = est <= t.lib.max_dabs();
    if !ok {
        t.degraded.fetch_add(1, Ordering::Relaxed);
        eprintln!("daub: layer '{name}' needs {est} dabs (> budget {}), \
                   degraded to capsule", t.lib.max_dabs());
    }
    ok
}

/// The per-layer stroke loop, shared verbatim by the fresh render and
/// the sequence renderer: rasterise `strokes` onto `buf` in order.
/// Appending later strokes onto a buffer that already holds an earlier
/// slice is byte-identical to rendering the whole slice at once (each
/// stroke composites exactly once, strictly in array order).
fn render_pool_strokes(buf: &mut LayerBuf, w: usize, h: usize,
                       name: &str, strokes: &[&Stroke], tip_layer: bool,
                       tips: Option<&Tips>, cal: &Calib) -> Result<()> {
    let mut cache = tip::SpriteCache::new(tip::SPRITE_CACHE_CAP);
    for s in strokes.iter() {
        let dabs = if tip_layer && engine_for(s, tips) == Engine::Tip {
            let t = tips.expect("engine Tip implies tips present");
            let (id, p) = &t.presets[&s.preset];
            tip::render_stroke(buf, w, h, s, p, *id, &mut cache)?
        } else {
            if tip_layer {
                // tip layer, capsule stroke: the calibration must
                // exist or this is the loud-failure case
                if !cal.has(&s.preset) {
                    anyhow::bail!(
                        "preset '{}' on tip layer '{name}' has no tip \
                         engine and no capsule calibration",
                        s.preset);
                }
            }
            render_stroke(buf, w, h, s, cal)?;
            0
        };
        if dabs > 0 {
            if let Some(t) = tips {
                t.dabs.fetch_add(dabs, Ordering::Relaxed);
            }
        }
    }
    if let Some(t) = tips {
        t.bakes.fetch_add(cache.bakes, Ordering::Relaxed);
    }
    Ok(())
}

/// Incremental prefix-sequence renderer (the timelapse fast path): one
/// plan parse, and per frame only layers whose stroke count grew do
/// any raster work. Layers are independent by construction, so a
/// buffer that no new stroke touches stands; within a layer, appending
/// the new strokes is byte-identical to re-rendering the slice (see
/// `render_pool_strokes`). The circuit breaker is re-evaluated on every
/// growth over the prefix-limited stroke set - the same inputs a fresh
/// `render_prefix` would see - so a budget crossing flips to capsule
/// with a full layer re-render, exactly like the fresh path.
pub struct SeqRenderer {
    order: Vec<String>,
    /// per pool, ascending stroke indices into `strokes`
    idx: Vec<Vec<usize>>,
    bufs: Vec<Option<LayerBuf>>,
    counts: Vec<usize>,
    tip_mode: Vec<bool>,
    w: usize,
    h: usize,
    k: usize,
}

impl SeqRenderer {
    pub fn new(dir: &Directory) -> SeqRenderer {
        let mut order: Vec<String> = Vec::new();
        for s in &dir.strokes {
            if !order.contains(&s.layer) {
                order.push(s.layer.clone());
            }
        }
        let mut idx: Vec<Vec<usize>> = vec![Vec::new(); order.len()];
        for (i, s) in dir.strokes.iter().enumerate() {
            let p = order.iter().position(|n| n == &s.layer).unwrap();
            idx[p].push(i);
        }
        let n = order.len();
        SeqRenderer {
            w: dir.canvas.0 as usize,
            h: dir.canvas.1 as usize,
            order,
            idx,
            bufs: (0..n).map(|_| None).collect(),
            counts: vec![0; n],
            tip_mode: vec![false; n],
            k: 0,
        }
    }

    /// Render prefix `k` (`strokes[..k]`). Prefixes must be
    /// non-decreasing - the timelapse sequence only ever grows, and
    /// layer buffers are only ever appended to.
    pub fn frame(&mut self, dir: &Directory, k: usize, cal: &Calib,
                 tips: Option<&Tips>)
                 -> Result<([u8; 3], Vec<(String, &LayerBuf)>, (usize, usize))> {
        assert!(k >= self.k, "sequence prefixes must not move backwards");
        self.k = k;
        let (w, h) = (self.w, self.h);
        for p in 0..self.order.len() {
            let want = self.idx[p].partition_point(|&i| i < k);
            if want == self.counts[p] {
                continue; // this layer didn't grow - buffer stands
            }
            let name = self.order[p].clone();
            let upto: Vec<&Stroke> = self.idx[p][..want].iter()
                .map(|&i| &dir.strokes[i]).collect();
            let tip_layer = layer_tip_decision(&upto, tips, &name);
            let flipped = tip_layer != self.tip_mode[p];
            if flipped || self.bufs[p].is_none() {
                // breaker flip needs a clean slate (the fresh path would
                // have rasterised everything through the new engine)
                self.bufs[p] = Some(LayerBuf { rgba: vec![0u8; w * h * 4] });
                self.counts[p] = 0;
            }
            let from = self.counts[p];
            let append: Vec<&Stroke> = self.idx[p][from..want].iter()
                .map(|&i| &dir.strokes[i]).collect();
            render_pool_strokes(self.bufs[p].as_mut().unwrap(), w, h,
                                &name, &append, tip_layer, tips, cal)?;
            self.counts[p] = want;
            self.tip_mode[p] = tip_layer;
        }
        let paper = dir.bg.unwrap_or_else(|| cal.bg());
        let stack: Vec<(String, &LayerBuf)> = self.order.iter().enumerate()
            .filter(|(p, _)| self.counts[*p] > 0)
            .map(|(p, name)| (name.clone(),
                              self.bufs[p].as_ref().unwrap()))
            .collect();
        Ok((paper, stack, (w, h)))
    }
}

/// Composite the layer stack over the paper ground into an RGB image.
/// Generic over owned or borrowed layer buffers (AsRef's reflexive +
/// forwarding impls cover both) so the sequence renderer can hand out
/// references without per-frame clones.
pub fn composite<L>(paper: [u8; 3], stack: &[(String, L)],
                    (w, h): (usize, usize)) -> Vec<u8>
where L: std::convert::AsRef<LayerBuf> {
    let mut out = vec![0u8; w * h * 3];
    for i in 0..w * h {
        let mut px = paper;
        for (_, layer) in stack {
            let layer = layer.as_ref();
            let a = layer.rgba[i * 4 + 3];
            if a == 0 {
                continue;
            }
            let af = a as f32 / 255.0;
            for c in 0..3 {
                px[c] = (px[c] as f32 * (1.0 - af)
                         + layer.rgba[i * 4 + c] as f32 * af).round() as u8;
            }
        }
        out[i * 3..i * 3 + 3].copy_from_slice(&px);
    }
    out
}

/// Per-layer composite material for GUI-side instant compositing (the
/// workbench's mute toggles repaint from these instead of re-rendering).
/// `composite` weights each layer's stored RGB by its pixel alpha; the
/// .bgra files here carry that product pre-multiplied in Qt's
/// Format_ARGB32_Premultiplied memory order (B,G,R,A little-endian), so
/// QPainter's SourceOver reproduces the exact same formula per pixel.
/// layers.json lists the stack bottom-first + the paper ground colour.
pub fn write_layers_dir(out_dir: &std::path::Path, paper: [u8; 3],
                        stack: &[(String, LayerBuf)], (w, h): (usize, usize))
                        -> Result<()> {
    std::fs::create_dir_all(out_dir)
        .with_context(|| format!("creating {}", out_dir.display()))?;
    let mut layers = Vec::new();
    for (idx, (name, buf)) in stack.iter().enumerate() {
        let file = format!("layer{:02}.bgra", idx);
        let mut pm = vec![0u8; buf.rgba.len()];
        for i in 0..w * h {
            let r = buf.rgba[i * 4] as u32;
            let g = buf.rgba[i * 4 + 1] as u32;
            let b = buf.rgba[i * 4 + 2] as u32;
            let a = buf.rgba[i * 4 + 3] as u32;
            pm[i * 4] = ((b * a + 127) / 255) as u8;
            pm[i * 4 + 1] = ((g * a + 127) / 255) as u8;
            pm[i * 4 + 2] = ((r * a + 127) / 255) as u8;
            pm[i * 4 + 3] = a as u8;
        }
        std::fs::write(out_dir.join(&file), &pm)
            .with_context(|| format!("writing {}", file))?;
        layers.push(serde_json::json!({"name": name, "file": file}));
    }
    let manifest = serde_json::json!({
        "width": w, "height": h, "paper": paper, "layers": layers,
    });
    std::fs::write(out_dir.join("layers.json"), manifest.to_string())
        .with_context(|| "writing layers.json")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(strokes: Vec<Stroke>, w: u32, h: u32) -> Directory {
        Directory { canvas: (w, h), bg: None, strokes }
    }

    fn stroke(points: Vec<[f64; 3]>, layer: &str, preset: &str) -> Stroke {
        Stroke {
            points,
            size: 32.0,
            color: "#ff0000".into(),
            layer: layer.into(),
            preset: preset.into(),
            opacity: 1.0,
        }
    }

    fn flat_preset(v: u8) -> KppPreset {
        crate::tip::tests::flat_preset(v)
    }

    fn lib_with(preset_json: &str, kpp: &str) -> BrushLib {
        // hand-built registry without touching disk files
        // ({{{x}}} renders as "{" + x + "}" through format!)
        let json = format!(r#"{{
            "version": 1, "tip_root": ".", "layers_tips": ["X1"],
            "max_dabs": 100000, "presets": {{{preset_json}}} }}"#);
        let mut lib: BrushLib = serde_json::from_str(&json).unwrap();
        if let Some(e) = lib.presets.get_mut("t) Test Flat") {
            e.kpp = kpp.into();
        }
        lib
    }

    #[test]
    fn routing_prefers_tips_only_where_allowed() {
        // engine_for: layer gate + parsed preset gate
        let mut lib = lib_with(
            r#""t) Test Flat": {"kpp": "", "use_tips": true,
               "supported": true,
               "gate": {"pass": true, "measured": null}}"#, "");
        lib.layer_set = ["X1".to_string()].into_iter().collect();
        let mut presets = HashMap::new();
        presets.insert("t) Test Flat".to_string(),
                       (0u32, flat_preset(255)));
        let tips = Tips {
            lib,
            presets,
            dabs: AtomicU64::new(0),
            bakes: AtomicU64::new(0),
            degraded: AtomicU64::new(0),
        };
        let on = stroke(vec![[0.0, 0.0, 1.0], [10.0, 0.0, 1.0]], "X1", "t) Test Flat");
        let other_layer = stroke(vec![[0.0, 0.0, 1.0]; 2], "U3", "t) Test Flat");
        let other_preset = stroke(vec![[0.0, 0.0, 1.0]; 2], "X1", "b) Basic-5 Size");
        assert_eq!(engine_for(&on, Some(&tips)), Engine::Tip);
        assert_eq!(engine_for(&other_layer, Some(&tips)), Engine::Capsule,
                   "layer not in layers_tips");
        assert_eq!(engine_for(&other_preset, Some(&tips)), Engine::Capsule,
                   "preset not tips-parsed (unregistered or gate unpassed)");
        assert_eq!(engine_for(&on, None), Engine::Capsule, "no registry -> capsule");
    }

    #[test]
    fn capsule_bytes_unchanged_by_routing_wiring() {
        // a stroke with no tips anywhere must rasterise identically to
        // the pure-capsule code path
        let cal = Calib::load("tools/data/ink_calib.json").unwrap();
        let d = dir(vec![stroke(vec![[10.0, 10.0, 0.5], [40.0, 10.0, 0.8]],
                                 "X1", "b) Basic-2 Opacity")],
                    64, 64);
        let (paper, stack, (w, h)) = render(&d, &cal, None).unwrap();
        let img = composite(paper, &stack, (w, h));
        let with_tips_empty = Tips {
            lib: lib_with("", ""),
            presets: HashMap::new(),
            dabs: AtomicU64::new(0),
            bakes: AtomicU64::new(0),
            degraded: AtomicU64::new(0),
        };
        let (paper2, stack2, _) = render(&d, &cal, Some(&with_tips_empty)).unwrap();
        let img2 = composite(paper2, &stack2, (w, h));
        assert_eq!(img, img2, "registry present but unused must not change bytes");
        // and the capsule path actually painted
        let bg = cal.bg();
        assert!(img.chunks_exact(3).any(|p| p[0] != bg[0] || p[1] != bg[1]
                                              || p[2] != bg[2]),
                "stroke left a mark");
    }

    #[test]
    fn tip_render_matches_direct_engine_call() {
        // Tips-enabled render of a real .kpp must produce the same
        // layer bytes as calling tip::render_stroke by hand - proves
        // the shared Stamp composite didn't drift between the paths.
        let tip_kpp = "tools/data/brush_tips/\
Krita_4_Default_Resources/b)_Basic-2_Opacity.kpp";
        let cal = Calib::load("tools/data/ink_calib.json").unwrap();
        let p = KppPreset::load(tip_kpp, "b) Basic-2 Opacity").unwrap();
        let mut lib = lib_with(
            r#""b) Basic-2 Opacity": {"kpp": "", "use_tips": true,
               "supported": true, "gate": {"pass": true, "measured": null}}"#,
            "");
        lib.layer_set = ["X1".to_string()].into_iter().collect();
        let mut presets = HashMap::new();
        presets.insert("b) Basic-2 Opacity".to_string(), (0u32, p));
        let mut tips = Tips {
            lib,
            presets,
            dabs: AtomicU64::new(0),
            bakes: AtomicU64::new(0),
            degraded: AtomicU64::new(0),
        };
        // registry must let B2 through the gate for this test
        tips.lib.presets.get_mut("b) Basic-2 Opacity").unwrap()
            .gate = Some(crate::brushlib::Gate { pass: true, measured: None });

        let s = Stroke {
            points: vec![[8.0, 8.0, 0.5], [40.0, 8.0, 0.9]],
            size: 24.0,
            color: "#0000ff".into(),
            layer: "X1".into(),
            preset: "b) Basic-2 Opacity".into(),
            opacity: 0.9,
        };
        let d = dir(vec![s], 64, 64);
        let (_, stack, (w, h)) = render(&d, &cal, Some(&tips)).unwrap();
        assert!(tips.dabs.load(Ordering::Relaxed) > 0, "tip engine ran");

        // hand-driven reference
        let (_, p2) = &tips.presets["b) Basic-2 Opacity"];
        let mut ref_buf = LayerBuf { rgba: vec![0; w * h * 4] };
        let mut cache = crate::tip::SpriteCache::new(crate::tip::SPRITE_CACHE_CAP);
        let s2 = Stroke {
            points: vec![[8.0, 8.0, 0.5], [40.0, 8.0, 0.9]],
            size: 24.0,
            color: "#0000ff".into(),
            layer: "X1".into(),
            preset: "b) Basic-2 Opacity".into(),
            opacity: 0.9,
        };
        crate::tip::render_stroke(&mut ref_buf, w, h, &s2, p2, 0,
                                  &mut cache).unwrap();
        assert_eq!(stack.last().unwrap().1.rgba, ref_buf.rgba,
                   "routed tip render == direct engine call");
    }

    #[test]
    fn legacy_capsule_bytes_unchanged() {
        // The 7 ink_calib presets are the frozen capsule contract: the
        // v4/v5 golden plans and every shipped render hash these bytes.
        // Any drift here is a downstream-visible regression - fix the
        // cause, never re-pin the constant. (Regenerate values with
        // DAUB_GOLDEN_PROBE=1 cargo test --release legacy_capsule.)
        let cal_path = "tools/data/ink_calib.json";
        let cal = Calib::load(cal_path).unwrap();
        const PRESETS: [&str; 7] = [
            "b) Airbrush Soft", "b) Basic-2 Opacity", "b) Basic-5 Size",
            "d) Ink-1 Precision", "d) Ink-2 Fineliner",
            "d) Ink-3 Gpen", "d) Ink-8 Sumi-e",
        ];
        const GOLDEN: [u64; 7] = [
            9684658257400602621, 3488573270287576413,
            2611066821847433857, 9719699360501084765,
            7198030459663494225, 12057300905100989453,
            14112388331560306221,
        ];

        let mut hashes = Vec::new();
        let probe = std::env::var("DAUB_GOLDEN_PROBE").is_ok();
        for (i, name) in PRESETS.iter().enumerate() {
            let mut buf = LayerBuf { rgba: vec![0; 256 * 256 * 4] };
            for (j, (size, pts, op)) in [
                (8.0f64,
                 vec![[24.0, 40.0, 0.3], [232.0, 40.0, 0.3]], 1.0f64),
                (32.0,
                 vec![[24.0, 96.0, 0.7], [232.0, 96.0, 0.7]], 1.0),
                (16.0,
                 vec![[24.0, 152.0, 1.0], [128.0, 184.0, 1.0],
                      [232.0, 152.0, 1.0]], 1.0),
                (64.0,
                 vec![[24.0, 216.0, 1.0], [232.0, 216.0, 0.4]], 0.5),
            ].iter().enumerate() {
                let mut s = stroke(pts.clone(), "X1", name);
                s.size = *size;
                s.opacity = *op;
                s.color = match j {
                    0 => "#ff0000",
                    1 => "#00ff00",
                    2 => "#0000ff",
                    _ => "#102030",
                }.into();
                render_stroke(&mut buf, 256, 256, &s, &cal)
                    .unwrap_or_else(|e| panic!("{name}: {e}"));
            }
            let mut h: u64 = 0xcbf29ce484222325;
            for b in &buf.rgba {
                h ^= *b as u64;
                h = h.wrapping_mul(0x100000001b3);
            }
            hashes.push(h);
            if !probe {
                assert_eq!(h, GOLDEN[i],
                           "capsule bytes changed for '{name}'");
            }
        }
        if probe {
            println!("GOLDEN: {:?}", hashes);
        }
    }

    #[test]
    fn layers_dir_writes_premul_bgra_and_manifest() {
        let d = std::env::temp_dir().join("daub_test_layers_dir");
        let _ = std::fs::remove_dir_all(&d);
        let mk = |rgba: [u8; 4]| LayerBuf { rgba: rgba.to_vec() };
        // two 1-pixel layers, bottom -> top
        let stack = vec![
            ("bottom".to_string(), mk([255, 0, 0, 128])), // red @ 50%
            ("top".to_string(), mk([0, 255, 0, 255])),    // green opaque
        ];
        write_layers_dir(&d, [10, 20, 30], &stack, (1, 1)).unwrap();

        // Qt ARGB32_Premultiplied little-endian = B,G,R,A premultiplied
        let pm = std::fs::read(d.join("layer00.bgra")).unwrap();
        assert_eq!(pm, vec![0, 0, 128, 128]); // 255*128/255 rounds to 128
        let pm = std::fs::read(d.join("layer01.bgra")).unwrap();
        assert_eq!(pm, vec![0, 255, 0, 255]);

        let mf: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(d.join("layers.json")).unwrap()
        ).unwrap();
        assert_eq!(mf["width"], 1);
        assert_eq!(mf["height"], 1);
        assert_eq!(mf["paper"], serde_json::json!([10, 20, 30]));
        assert_eq!(mf["layers"][0]["name"], "bottom");
        assert_eq!(mf["layers"][0]["file"], "layer00.bgra");
        assert_eq!(mf["layers"][1]["name"], "top");
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn premul_layers_recompose_to_composite() {
        // the whole point of --layers-dir: compositing the premultiplied
        // bgra files bottom->top with the source-over formula must
        // reproduce composite() pixel-for-pixel on a mix of alphas
        let stack: Vec<(String, LayerBuf)> = [("a".to_string(),
            LayerBuf { rgba: vec![200, 40, 60, 128] }), ("b".to_string(),
            LayerBuf { rgba: vec![10, 220, 30, 255] })]
            .into_iter().collect();
        let paper = [37, 99, 150];
        let want = composite(paper, &stack, (1, 1));

        let mut px = paper;
        for (_, l) in &stack {
            let a = l.rgba[3] as u32;
            if a == 0 { continue; }
            let af = a as f32 / 255.0;
            for c in 0..3 {
                // Qt source-over on the premultiplied file byte; the
                // u8 premul quantisation vs composite's f32 term can
                // shift the result by at most 1 grey level per layer
                let src = ((l.rgba[c] as u32 * a + 127) / 255) as f32;
                px[c] = (src + px[c] as f32 * (1.0 - af)).round() as u8;
            }
        }
        for c in 0..3 {
            assert!((px[c] as i32 - want[c] as i32).abs() <= 1,
                    "channel {c}: got {} want {}", px[c], want[c]);
        }
    }

    #[test]
    fn seq_frames_equal_fresh_prefix_renders() {
        // the timelapse fast path must be byte-identical to rendering
        // every prefix fresh - same stack, same order, same bytes - on
        // an interleaved multi-layer plan, in three regimes: tip mode
        // with headroom (pure appends), a dab budget that trips the
        // circuit breaker mid-sequence (full capsule re-render at the
        // flip, capsule appends after), and no registry at all.
        let cal_path = "tools/data/ink_calib.json";
        let cal = Calib::load(cal_path).unwrap();
        // register a REAL calibrated preset as the tip engine so the
        // breaker-flip capsule fallback finds its calibration (what a
        // production registry guarantees for every tip preset)
        let mk_tips = |max_dabs: u64| -> Tips {
            let json = format!(r#"{{
                "version": 1, "tip_root": ".", "layers_tips": ["L1"],
                "max_dabs": {max_dabs}, "presets": {{"b) Basic-2 Opacity":
                {{"kpp": "", "use_tips": true, "supported": true,
                "gate": {{"pass": true, "measured": null}}}}}}}}"#);
            let mut lib: BrushLib = serde_json::from_str(&json).unwrap();
            lib.layer_set = ["L1".to_string()].into_iter().collect();
            let mut presets = HashMap::new();
            presets.insert("b) Basic-2 Opacity".to_string(),
                           (0u32, flat_preset(255)));
            Tips {
                lib,
                presets,
                dabs: AtomicU64::new(0),
                bakes: AtomicU64::new(0),
                degraded: AtomicU64::new(0),
            }
        };
        let tips_hi = mk_tips(1_000_000);  // never flips: tip appends
        let tips_lo = mk_tips(3);          // flips early: capsule appends

        // interleaved first-seen order L0, L1, L2, then more L0/L1:
        // a prefix's first-seen order is the full order filtered to
        // layers present, which is exactly what SeqRenderer must emit
        let strokes = vec![
            stroke(vec![[10.0, 10.0, 0.5], [40.0, 12.0, 0.8]], "L0",
                   "b) Basic-2 Opacity"),
            stroke(vec![[20.0, 20.0, 0.9], [60.0, 24.0, 0.7]], "L1",
                   "b) Basic-2 Opacity"),
            stroke(vec![[30.0, 30.0, 1.0], [70.0, 32.0, 0.6]], "L2",
                   "b) Basic-2 Opacity"),
            stroke(vec![[15.0, 50.0, 0.8], [55.0, 52.0, 0.9]], "L1",
                   "b) Basic-2 Opacity"),
            stroke(vec![[12.0, 60.0, 0.7], [52.0, 62.0, 0.8]], "L0",
                   "b) Basic-2 Opacity"),
            // tip layer, unregistered preset: capsule via the cal gate
            stroke(vec![[18.0, 70.0, 0.6], [58.0, 72.0, 0.9]], "L1",
                   "d) Ink-3 Gpen"),
            stroke(vec![[25.0, 80.0, 0.9], [65.0, 82.0, 0.7]], "L2",
                   "b) Basic-2 Opacity"),
        ];
        let d = dir(strokes, 96, 96);
        for tips_ref in [Some(&tips_hi), Some(&tips_lo), None] {
            let mut seq = SeqRenderer::new(&d);
            for k in 1..=d.strokes.len() {
                let (p1, s1, dim1) =
                    render_prefix(&d, k, &cal, tips_ref).unwrap();
                let (p2, s2, dim2) =
                    seq.frame(&d, k, &cal, tips_ref).unwrap();
                assert_eq!(p1, p2, "k={k}");
                assert_eq!(dim1, dim2, "k={k}");
                assert_eq!(s1.len(), s2.len(), "k={k} stack size");
                for ((n1, b1), (n2, b2)) in s1.iter().zip(s2.iter()) {
                    assert_eq!(n1, n2, "k={k}");
                    assert_eq!(b1.rgba, b2.rgba, "k={k} layer {n1}");
                }
            }
        }
    }
}
