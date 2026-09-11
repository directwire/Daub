//! Tip-stamp engine: renders real Krita presets by stamping their
//! .kpp tip bitmaps along each stroke.
//!
//! Per dab: pressure interpolates along the segment, dab diameter comes
//! from the preset's size option (enable bit + curve). The per-dab
//! deposit is the dab's FULL alpha `flow(p) * opacity(p) * stroke_op`
//! against the mask, accumulated in LOG-DEPTH space — this is measured
//! Krita semantics, not a modelling choice:
//!
//! - real Krita composites overlapping dabs source-over, and same-colour
//!   source-over is additive in D = -ln(1-alpha);
//! - the per-dab alpha is the whole of flow x opacity x stroke opacity:
//!   the 09-06 gate shows Basic circle peaking at exactly 1-(1-p)^2
//!   (0.506/0.753/0.910/0.976 for p=0.3/0.5/0.7/0.85, every size >= 16)
//!   - two overlapping dabs of per-dab alpha p. At stroke opacity 0.3
//!   the bridge still peaks ABOVE 0.3 (0.584 = 1-0.7^2 at size 64), so
//!   opacity is NOT a stroke-level cap and NOT a post-multiplier: both
//!   cap-after-accumulate and cap-before-multiplied-deposits undershoot
//!   every such cell (the 09-06 evening FAIL rows, bias -0.15..-0.35).
//!   The pixel alpha is simply 1 - prod(1 - x_i * mask_i) over the dabs
//!   covering it, x_i the full per-dab alpha.
//!
//! Fixed point: D is Q12 (units 1/4096) in u16 sprites and u32 scratch.
//! Integer addition is exactly commutative/associative, so dab merge
//! order never changes a byte - determinism survives any parallel
//! schedule. The stroke converts D -> alpha once, then composites
//! through the shared Stamp (same contract as the capsule path, .kra
//! invariants untouched).
//!
//! Sprite cache: one baked deposit sprite per (preset, diameter bucket,
//! flow bucket). Diameter buckets quantise log2(diameter) in 1/8 stops,
//! so a sprite's linear scale is within 2^(±1/16) ≈ ±4.4% of the
//! requested dab (area ≈ ±9%) - inside the truth gate's 12% width
//! budget, and it caps unique sprites per preset at ~90 across the
//! whole [1,512] range. Flow buckets quantise to 1/32; presets without
//! an active flow option always land in the f=1.0 bucket. The cache is
//! layer-local (layers render on rayon threads), single-threaded within
//! a layer, so access order - and therefore LRU behaviour - is fully
//! deterministic.

use crate::preset::{KppPreset, MaskGen, MaskGenId};
use crate::render::{stroke_bbox, LayerBuf, Stamp, Stroke};
use anyhow::Result;
use std::collections::{HashMap, VecDeque};

pub const SPRITE_CACHE_CAP: usize = 128 * 1024 * 1024; // 128MB
pub const MAX_DAB_DIAMETER: f64 = 512.0;

/// Log-depth fixed point: dep = round(-ln(1 - f*a) * DEP_SHIFT).
const DEP_SHIFT: f64 = 4096.0;
/// Deposit clamp: alpha >= 1 - e^-7.99976 = 0.99966 -> 255 when rounded.
const DEP_MAX: u16 = 32767;
/// Flow buckets per unit (1/32); f=1.0 -> bucket 32.
const FLOW_BUCKETS: f64 = 32.0;

fn deposit_u16(f: f64, m: u8) -> u16 {
    let a = f * (m as f64 / 255.0);
    if a >= 0.9995 {
        return DEP_MAX;
    }
    let d = -(1.0 - a).ln() * DEP_SHIFT;
    if d >= DEP_MAX as f64 {
        DEP_MAX
    } else {
        d.round() as u16
    }
}

// ------------------------------------------------- procedural auto tips
//
// Mask generation ported 1:1 from the Krita 5.3 sources (D:\krita-src),
// every conversion matching the C++ semantics that survive into the
// stamped dab alpha:
//   - generators return TRANSPARENCY (u8, double->quint8 truncation);
//     the scalar applicator then stamps alpha = (255 - value) * random
//     (kis_brush_mask_scalar_applicator.h, random == 1 here).
//   - `norme(a,b) = a*a + b*b` - the "distance" fed to the curve
//     generator is the SQUARED normalised radius (no sqrt).
//   - supersampling: 3x3 iff antialiasEdges && effW < 10, averaged with
//     INTEGER division (kis_brush_mask_scalar_applicator.h).
//   - sample lattice: x - centerX, centerX = hotspot - 0.5 + subPixel;
//     canonical subPixel 0.5 puts samples on x - d/2
//     (kis_auto_brush.cpp, hotspot = dstWidth/2).

/// erf via Abramowitz & Stegun 7.1.26 (|eps| <= 1.5e-7, x86-64 has no
/// libm erf and Krita itself pulls boost::erf on Windows).
fn erf(x: f64) -> f64 {
    let a = x.abs();
    let t = 1.0 / (1.0 + 0.327_591_1 * a);
    let poly = t
        * (0.254_829_592
            + t * (-0.284_496_736
                + t * (1.421_413_741
                    + t * (-1.453_152_027 + t * 1.061_405_429))));
    let e = 1.0 - poly * (-(a * a)).exp();
    if x < 0.0 {
        -e
    } else {
        e
    }
}

/// Natural cubic spline through `knots`: per-interval monomial
/// coefficients of s(x) = a*x^3 + b*x^2 + c*x + d, C2 interior,
/// natural ends (s''=0). Same system KisCubicSpline solves with Eigen;
/// Gaussian elimination with partial pivoting (n is tiny).
fn cubic_spline_coeffs(knots: &[(f64, f64)]) -> Vec<[f64; 4]> {
    let n = knots.len();
    if n == 1 {
        return vec![[0.0, 0.0, 0.0, knots[0].1]];
    }
    if n == 2 {
        // KisCubicSpline special-cases two points to a straight line
        let c = (knots[1].1 - knots[0].1) / (knots[1].0 - knots[0].0);
        let d = knots[0].1 - c * knots[0].0;
        return vec![[0.0, 0.0, c, d]];
    }
    let m = (n - 1) * 4;
    let mut a = vec![0.0f64; m * m];
    let mut b = vec![0.0f64; m];
    let mut row = 0usize;
    let put = |a: &mut Vec<f64>, row: usize, col: usize, v: f64| {
        a[row * m + col] = v;
    };
    let mut px = knots[0].0;
    let mut py = knots[0].1;
    for i in 0..n - 1 {
        let base = i * 4;
        put(&mut a, row, base, px * px * px);
        put(&mut a, row, base + 1, px * px);
        put(&mut a, row, base + 2, px);
        put(&mut a, row, base + 3, 1.0);
        b[row] = py;
        row += 1;
        px = knots[i + 1].0;
        py = knots[i + 1].1;
        put(&mut a, row, base, px * px * px);
        put(&mut a, row, base + 1, px * px);
        put(&mut a, row, base + 2, px);
        put(&mut a, row, base + 3, 1.0);
        b[row] = py;
        row += 1;
    }
    put(&mut a, row, 0, 6.0 * knots[0].0);
    put(&mut a, row, 1, 2.0);
    row += 1;
    let l = m - 4;
    put(&mut a, row, l, 6.0 * knots[n - 1].0);
    put(&mut a, row, l + 1, 2.0);
    row += 1;
    for i in 1..n - 1 {
        let x = knots[i].0;
        let base = i * 4;
        // first-derivative continuity (no corner flags in curve strings)
        put(&mut a, row, base - 4, 3.0 * x * x);
        put(&mut a, row, base - 3, 2.0 * x);
        put(&mut a, row, base - 2, 1.0);
        put(&mut a, row, base, -3.0 * x * x);
        put(&mut a, row, base + 1, -2.0 * x);
        put(&mut a, row, base + 2, -1.0);
        row += 1;
        // second-derivative continuity
        put(&mut a, row, base - 4, 6.0 * x);
        put(&mut a, row, base - 3, 2.0);
        put(&mut a, row, base, -6.0 * x);
        put(&mut a, row, base + 1, -2.0);
        row += 1;
    }

    // Gaussian elimination with partial pivoting
    for col in 0..m {
        let piv = (col..m).fold(col, |best, r| if a[r * m + col].abs() > a[best * m + col].abs() { r } else { best });
        if piv != col {
            for c in 0..m {
                a.swap(col * m + c, piv * m + c);
            }
            b.swap(col, piv);
        }
        let d = a[col * m + col];
        for r in (col + 1)..m {
            let f = a[r * m + col] / d;
            if f == 0.0 {
                continue;
            }
            for c in col..m {
                a[r * m + c] -= f * a[col * m + c];
            }
            b[r] -= f * b[col];
        }
    }
    let mut x = vec![0.0f64; m];
    for r in (0..m).rev() {
        let mut s = b[r];
        for c in (r + 1)..m {
            s -= a[r * m + c] * x[c];
        }
        x[r] = s / a[r * m + r];
    }
    (0..n - 1)
        .map(|i| [x[i * 4], x[i * 4 + 1], x[i * 4 + 2], x[i * 4 + 3]])
        .collect()
}

fn spline_eval(coeffs: &[[f64; 4]], knots: &[(f64, f64)], x: f64) -> f64 {
    // KisCubicCurve::Data::value clamps x to the knot span, then
    // KisCubicSpline::getValue picks the interval by first x greater.
    let x = x.clamp(knots[0].0, knots[knots.len() - 1].0);
    let mut it = coeffs.len() - 1;
    for (i, w) in knots.iter().enumerate().skip(1) {
        if x < w.0 {
            it = i - 1;
            break;
        }
    }
    let c = &coeffs[it];
    c[0] * x * x * x + c[1] * x * x + c[2] * x + c[3]
}

/// The soft generator's lookup table: floatTransfer(curveResolution+2)
/// of the curve, curveResolution frozen at construction time from the
/// DEFINITION diameter (kis_curve_circle_mask_generator.cpp ctor).
struct SoftCurve {
    cr: f64,
    data: Vec<f64>,
}

impl SoftCurve {
    fn new(knots: &[(f64, f64)], definition_diameter: f64) -> SoftCurve {
        let cr = (definition_diameter.max(1.0) * 4.0).round(); // OVERSAMPLING=4
        let coeffs = cubic_spline_coeffs(knots);
        let data = (0..cr as usize + 2)
            .map(|i| spline_eval(&coeffs, knots, i as f64 / cr).clamp(0.0, 1.0))
            .collect();
        SoftCurve { cr, data }
    }

    /// Transparency u8 at `dist` (squared normalised radius):
    /// lerp the table, return (1 - alpha) * 255 truncated.
    fn value(&self, dist: f64) -> u8 {
        let distance = dist * self.cr;
        let i = distance as usize; // quint16 truncation in Krita
        let fr = distance - i as f64;
        let a = (1.0 - fr) * self.data[i] + fr * self.data[i + 1];
        ((1.0 - a) * 255.0) as u8
    }
}

fn sqr(v: f64) -> f64 {
    v * v
}

/// KisCircleMaskGenerator::valueAt (id="default").
fn circle_transparency(x: f64, y: f64, w: f64, hf: f64, vf: f64, aa: bool) -> u8 {
    let yr0 = y.abs();
    let n = sqr(x * 2.0 / w) + sqr(yr0 * 2.0 / w);
    if n > 1.0 {
        return 255;
    }
    // "+1.0 to ensure correct antialiasing on the border" - in PIXELS,
    // applied to the fade term only.
    let (xr, yr) = if aa {
        (x.abs() + 1.0, yr0 + 1.0)
    } else {
        (x.abs(), yr0)
    };
    let xfc = if hf == 0.0 { 1.0 } else { 2.0 / (hf * w) };
    let yfc = if vf == 0.0 { 1.0 } else { 2.0 / (vf * w) };
    let nf = sqr(xr * xfc) + sqr(yr * yfc);
    if nf < 1.0 {
        return 0;
    }
    // double -> quint8 truncation; n == nf produces NaN, same as the
    // x86 conversion (0)
    (255.0 * n * (nf - 1.0) / (nf - n)) as u8
}

/// KisCurveCircleMaskGenerator::valueAt (id="soft"). Note hfade/vfade
/// are UNUSED here - the curve is the whole profile.
fn curve_transparency(sc: &SoftCurve, x: f64, y: f64, w: f64, aa: bool) -> u8 {
    let dist = sqr(x * 2.0 / w) + sqr(y.abs() * 2.0 / w);
    // KisAntialiasingFadeMaker1D::setSquareNormCoeffs: radius 1,
    // fadeStart = ((1-xc)+(1-yc))/2)^2
    if dist > 1.0 {
        return 255;
    }
    if aa {
        let xf = (1.0 - 2.0 / w).max(0.0);
        let yf = (1.0 - 2.0 / w).max(0.0);
        let fs = sqr(0.5 * (xf + yf));
        if dist > fs {
            let fv = sc.value(fs) as f64;
            let coeff = (255.0 - fv) / (1.0 - fs);
            return (fv + (dist - fs) * coeff) as u8;
        }
    }
    sc.value(dist)
}

/// KisGaussCircleMaskGenerator (id="gauss"): dist is the TRUE pixel
/// radius (sqrt(norme)), fade collapses hfade/vfade to one isotropic
/// coefficient. Returns the Private::value transparency, also used for
/// the AA fade table (m_baseFade.value bypasses needFade).
fn gauss_transparency_at(dist: f64, distfactor: f64, center: f64, alphafactor: f64) -> u8 {
    let z = dist * distfactor;
    // quint8 truncation of a possibly negative product -> 0, matching
    // the x86 double->u8 conversion
    let ret = (alphafactor * (erf(z + center) - erf(z - center))) as u8;
    255 - ret
}

fn gauss_transparency(x: f64, y: f64, w: f64, hf: f64, vf: f64, aa: bool) -> u8 {
    let mut fade = 1.0 - (hf + vf) / 2.0;
    if fade == 0.0 {
        fade = 1e-6;
    } else if fade == 1.0 {
        fade = 1.0 - 1e-6;
    }
    let sq2 = std::f64::consts::SQRT_2;
    let center = 2.5 * (6761.0 * fade - 10000.0) / (sq2 * 6761.0 * fade);
    let alphafactor = 255.0 / (2.0 * erf(center));
    let distfactor = sq2 * 12500.0 / (6761.0 * fade * w / 2.0);
    let radius = w / 2.0;
    let dist = (x * x + y * y).sqrt();
    if dist > radius {
        return 255;
    }
    if aa {
        // setRadius(radius): fadeStart = radius - 1, linear ramp over
        // the last pixel, seeded from the generator's own value
        let fs = (radius - 1.0).max(0.0);
        if dist > fs {
            let fv = gauss_transparency_at(fs, distfactor, center, alphafactor) as f64;
            let coeff = (255.0 - fv) / (radius - fs);
            return (fv + (dist - fs) * coeff) as u8;
        }
    }
    gauss_transparency_at(dist, distfactor, center, alphafactor)
}

/// Bake one auto-tip deposit sprite of d x d at flow f.
fn bake_auto(mg: &MaskGen, d: usize, f: f64) -> Sprite {
    let soft = match &mg.id {
        MaskGenId::Soft(knots) => Some(SoftCurve::new(knots, mg.definition_diameter)),
        _ => None,
    };
    // KisMaskGenerator::shouldSupersample: AA && effW < 10 -> 3x3
    let ss: usize = if mg.aa && d < 10 { 3 } else { 1 };
    let ssf = ss as f64;
    let cx = d as f64 / 2.0; // canonical subPixel 0.5 -> centerX = d/2
    let mut dep = vec![0u16; d * d];
    for j in 0..d {
        for i in 0..d {
            let mut sum = 0i64; // Krita accumulates quint8 into int
            for sy in 0..ss {
                for sx in 0..ss {
                    let x = i as f64 + sx as f64 / ssf - cx;
                    let y = j as f64 + sy as f64 / ssf - cx;
                    let t = match (&mg.id, soft.as_ref()) {
                        (MaskGenId::Circle, _) => {
                            circle_transparency(x, y, d as f64, mg.hfade, mg.vfade, mg.aa)
                        }
                        (MaskGenId::Soft(_), Some(sc)) => {
                            curve_transparency(sc, x, y, d as f64, mg.aa)
                        }
                        (MaskGenId::Gauss, _) => {
                            gauss_transparency(x, y, d as f64, mg.hfade, mg.vfade, mg.aa)
                        }
                        _ => unreachable!("soft without its curve table"),
                    };
                    sum += t as i64;
                }
            }
            // value /= samplearea is INTEGER division in Krita
            let value = if ss > 1 { (sum / (ss * ss) as i64) as u8 } else { sum as u8 };
            let alpha8 = 255 - value; // scalar applicator, random = 1.0
            dep[j * d + i] = deposit_u16(f, alpha8);
        }
    }
    Sprite { d, dep }
}

// ------------------------------------------------------------ sprite cache

pub struct Sprite {
    pub d: usize,
    /// d*d row-major Q12 log-deposit (see module docs). Alpha-encoding
    /// happens at bake so the stamp loop is pure integer adds.
    pub dep: Vec<u16>,
}

#[derive(Default)]
pub struct SpriteCache {
    map: HashMap<u64, Sprite>,
    lru: VecDeque<u64>,
    bytes: usize,
    cap_bytes: usize,
    pub hits: u64,
    pub bakes: u64,
}

/// log2(d) quantised to 1/8 stops; d=64 -> bucket 48, d=1 -> bucket 0.
fn bucket_of(d: f64) -> u32 {
    ((d.max(1.0).min(MAX_DAB_DIAMETER).log2() * 8.0).round() as i64).max(0) as u32
}

fn bucket_diameter(bucket: u32) -> usize {
    let d = 2.0f64.powf(bucket as f64 / 8.0).round();
    (d.max(1.0).min(MAX_DAB_DIAMETER)) as usize
}

impl SpriteCache {
    pub fn new(cap_bytes: usize) -> SpriteCache {
        SpriteCache {
            cap_bytes: cap_bytes.max(1024),
            ..SpriteCache::default()
        }
    }

    fn key(preset_key: u32, bucket: u32, flow_bucket: u32) -> u64 {
        ((preset_key as u64) << 32) | ((bucket as u64) << 8) | flow_bucket as u64
    }

    pub fn sprite(&mut self, preset_key: u32, preset: &KppPreset, d: f64,
                  flow: f64) -> &Sprite {
        let b = bucket_of(d);
        let fb = (flow.clamp(0.0, 1.0) * FLOW_BUCKETS).round() as u32;
        let k = Self::key(preset_key, b, fb);
        if self.map.contains_key(&k) {
            self.hits += 1;
            self.touch(k);
            return &self.map[&k];
        }
        let d_target = bucket_diameter(b);
        let spr = bake(preset, d_target, fb as f64 / FLOW_BUCKETS);
        self.insert(k, spr);
        &self.map[&k]
    }

    fn insert(&mut self, k: u64, spr: Sprite) {
        let sz = spr.dep.len() * 2;
        self.map.insert(k, spr);
        self.lru.push_back(k);
        self.bytes += sz;
        self.bakes += 1;
        while self.bytes > self.cap_bytes {
            let Some(evict) = self.lru.pop_front() else { break };
            if evict == k {
                // newest must survive even if it alone busts the soft cap
                self.lru.push_back(evict);
                break;
            }
            if let Some(s) = self.map.remove(&evict) {
                self.bytes -= s.dep.len() * 2;
            }
        }
    }

    fn touch(&mut self, k: u64) {
        if let Some(pos) = self.lru.iter().position(|&x| x == k) {
            self.lru.remove(pos);
            self.lru.push_back(k);
        }
    }

    #[cfg(test)]
    pub fn bytes(&self) -> usize {
        self.bytes
    }
}

/// Area-average resample of the tip mask to a d x d square, then
/// log-encode at flow `f`. Handles both down- and upscaling exactly
/// (piecewise-constant box). Auto-brush presets never reach here - they
/// bake procedurally from the MaskGenerator parameters.
fn bake(preset: &KppPreset, d_target: usize, f: f64) -> Sprite {
    if let Some(mg) = &preset.mask_gen {
        return bake_auto(mg, d_target, f);
    }
    let (tw, th) = (preset.tip_w, preset.tip_h);
    let sx = tw as f64 / d_target as f64;
    let sy = th as f64 / d_target as f64;
    let mut dep = vec![0u16; d_target * d_target];
    for j in 0..d_target {
        let y0 = (j as f64 * sy).min(th as f64);
        let y1 = ((j + 1) as f64 * sy).min(th as f64);
        let iy0 = y0.floor() as usize;
        let iy1 = (y1.ceil() as usize).min(th);
        for i in 0..d_target {
            let x0 = (i as f64 * sx).min(tw as f64);
            let x1 = ((i + 1) as f64 * sx).min(tw as f64);
            let ix0 = x0.floor() as usize;
            let ix1 = (x1.ceil() as usize).min(tw);
            let mut acc = 0.0f64;
            let mut area = 0.0f64;
            for yy in iy0..iy1 {
                let wy = (y1.min(yy as f64 + 1.0) - y0.max(yy as f64)).max(0.0);
                if wy == 0.0 {
                    continue;
                }
                let row = yy * tw;
                for xx in ix0..ix1 {
                    let wx = (x1.min(xx as f64 + 1.0) - x0.max(xx as f64)).max(0.0);
                    if wx == 0.0 {
                        continue;
                    }
                    acc += preset.mask[row + xx] as f64 * wx * wy;
                    area += wx * wy;
                }
            }
            let m = if area > 0.0 { (acc / area).round() as u8 } else { 0 };
            dep[j * d_target + i] = deposit_u16(f, m);
        }
    }
    Sprite { d: d_target, dep }
}

// ------------------------------------------------------------ dab walking

/// One dab placement: centre + diameter + the pressure the walk saw
/// there (drives the sprite's effective-alpha bucket).
struct Dab {
    x: f64,
    y: f64,
    p: f64,
    d: f64,
}

/// Walk a segment at the preset's spacing cadence, carrying the
/// residual into the next segment so the cadence is global, not
/// per-segment. `out` receives one dab per stamp.
fn walk_segment(seg: ([f64; 3], [f64; 3]), size: f64, preset: &KppPreset,
                residual: &mut f64, out: &mut Vec<Dab>) {
    let (a, b) = seg;
    let dx = b[0] - a[0];
    let dy = b[1] - a[1];
    let len = (dx * dx + dy * dy).sqrt();
    if len <= 0.0 {
        return; // zero-length: nothing to space along
    }
    let mut t = *residual;
    while t <= len {
        let u = t / len;
        let p = a[2] + (b[2] - a[2]) * u;
        let d = preset.diameter_at(size, p);
        if d >= 1.0 {
            out.push(Dab { x: a[0] + dx * u, y: a[1] + dy * u, p, d });
        }
        t += preset.spacing.step(d.max(1.0));
    }
    *residual = t - len;
}

/// Accumulate one sprite centred on (cx, cy) into the stroke scratch.
/// Pure integer adds: exactly commutative, so merge order is free.
fn blit(dacc: &mut [u32], sw: usize, sh: usize, cx: f64, cy: f64,
        spr: &Sprite) {
    let d = spr.d;
    let left = (cx - d as f64 / 2.0).round() as i64;
    let top = (cy - d as f64 / 2.0).round() as i64;
    for yy in 0..d {
        let sy = top + yy as i64;
        if sy < 0 || sy >= sh as i64 {
            continue;
        }
        let srow = &spr.dep[yy * d..yy * d + d];
        let base = sy as usize * sw;
        for xx in 0..d {
            let dv = srow[xx] as u32;
            if dv == 0 {
                continue;
            }
            let sx = left + xx as i64;
            if sx < 0 || sx >= sw as i64 {
                continue;
            }
            let idx = base + sx as usize;
            dacc[idx] += dv;
        }
    }
}

/// Render one stroke through the tip engine. Same scratch + one-shot
/// composite contract as the capsule path (render.rs), so .kra layer
/// invariants are untouched. Returns the number of dabs stamped.
pub fn render_stroke(buf: &mut LayerBuf, w: usize, h: usize, s: &Stroke,
                     preset: &KppPreset, preset_key: u32,
                     cache: &mut SpriteCache) -> Result<u64> {
    anyhow::ensure!(s.points.len() >= 2, "single-point stroke (engine pit)");
    if s.opacity <= 0.0 {
        return Ok(0);
    }
    let rgb = crate::calib::hex_rgb(&s.color)?;
    let mut pad = 1.0f64;
    for p in &s.points {
        pad = pad.max(preset.diameter_at(s.size, p[2]) * 0.5);
    }
    let (minx, miny, maxx, maxy) = stroke_bbox(&s.points, pad, w, h);
    if minx >= maxx || miny >= maxy {
        return Ok(0);
    }
    let bw = maxx - minx;
    let bh = maxy - miny;

    let mut dacc = vec![0u32; bw * bh];

    let mut dabs: Vec<Dab> = Vec::new();
    let mut residual = 0.0f64;
    for seg in s.points.windows(2) {
        walk_segment((seg[0], seg[1]), s.size, preset,
                     &mut residual, &mut dabs);
    }
    for dab in &dabs {
        // the dab's full alpha goes INTO the accumulation (see module
        // docs: bridge peaks are 1-(1-p)^n in the dab overlap count)
        let f = preset.flow_at(dab.p) * preset.opacity_cap(dab.p)
            * s.opacity;
        let spr = cache.sprite(preset_key, preset, dab.d, f);
        blit(&mut dacc, bw, bh, dab.x - minx as f64, dab.y - miny as f64,
             spr);
    }

    // Stroke end: log-depth -> alpha. Pure alpha-over - no stroke-level
    // cap, the bridge has none (op-0.3 rows peak above 0.3).
    let mut scratch = vec![0u8; bw * bh];
    for (i, &dd) in dacc.iter().enumerate() {
        if dd == 0 {
            continue;
        }
        let a = 1.0 - (-(dd as f64) / DEP_SHIFT).exp();
        scratch[i] = (a * 255.0).round().clamp(0.0, 255.0) as u8;
    }

    Stamp { x0: minx, y0: miny, w: bw, h: bh, alpha: scratch }
        .composite(buf, w, rgb);
    Ok(dabs.len() as u64)
}

/// Upper-bound dab estimate for a stroke (circuit-breaker input):
/// segment arc length divided by the minimum spacing step over the
/// segment (uses the mid pressure, which is what the walk sees most).
pub fn estimate_dabs(s: &Stroke, preset: &KppPreset) -> u64 {
    let mut n = 0.0f64;
    for seg in s.points.windows(2) {
        let dx = seg[1][0] - seg[0][0];
        let dy = seg[1][1] - seg[0][1];
        let len = (dx * dx + dy * dy).sqrt();
        if len <= 0.0 {
            continue;
        }
        let pm = (seg[0][2] + seg[1][2]) / 2.0;
        let d = preset.diameter_at(s.size, pm).max(1.0);
        n += len / preset.spacing.step(d);
    }
    n.ceil() as u64
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::preset::{CompositeOp, Curve, MaskGen};

    pub(crate) fn flat_preset(v: u8) -> KppPreset {
        // 64x64 fully-opaque round-equivalent tip (constant mask)
        KppPreset {
            resource_name: "test flat".into(),
            file_name: String::new(),
            paintopid: "paintbrush".into(),
            composite: CompositeOp::Normal,
            tip_w: 64,
            tip_h: 64,
            mask: vec![v; 64 * 64],
            mask_gen: None,
            size_active: false,
            size_value: 1.0,
            size_curve: Curve::default(),
            opacity_active: false,
            opacity_value: 1.0,
            opacity_curve: Curve::default(),
            flow_active: false,
            flow_value: 1.0,
            flow_curve: Curve::default(),
            spacing: crate::preset::SpacingModel::Linear(0.25),
            unsupported: None,
        }
    }

    fn stroke(points: Vec<[f64; 3]>, size: f64, opacity: f64) -> Stroke {
        Stroke {
            points,
            size,
            color: "#ff0000".into(),
            layer: "X1".into(),
            preset: "test flat".into(),
            opacity,
        }
    }

    #[test]
    fn bake_identity_at_native_size() {
        let mut p = flat_preset(0);
        // diagonal gradient mask
        for y in 0..64 {
            for x in 0..64 {
                p.mask[y * 64 + x] = ((x + y) * 4) as u8;
            }
        }
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 64.0, 1.0).clone();
        assert_eq!(spr.d, 64, "bucket(64)=48 -> native 64x64 sprite");
        for i in [0usize, 1, 64 * 63, 64 * 64 - 1] {
            assert_eq!(spr.dep[i], deposit_u16(1.0, p.mask[i]),
                       "dep is the log-encode of the mask at f=1");
        }
    }

    #[test]
    fn bake_flat_mask_any_scale_is_flat() {
        let p = flat_preset(200);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let want = deposit_u16(1.0, 200);
        for d in [1.0, 3.7, 16.0, 45.3, 200.0, 511.7] {
            let spr = cache.sprite(0, &p, d, 1.0);
            assert!(spr.dep.iter().all(|&m| m == want), "d={d}");
        }
    }

    #[test]
    fn flow_buckets_change_deposit() {
        let p = flat_preset(255);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let f1 = cache.sprite(0, &p, 32.0, 1.0).dep[0];
        let f095 = cache.sprite(0, &p, 32.0, 0.95).dep[0];
        assert!(f095 < f1,
                "lower flow deposits less: {f095} vs {f1}");
        // f=0.99 rounds into the f=1.0 bucket: same baked sprite
        let mut cache2 = SpriteCache::new(SPRITE_CACHE_CAP);
        let a = cache2.sprite(0, &p, 32.0, 1.0).dep.clone();
        let b = cache2.sprite(0, &p, 32.0, 0.99).dep.clone();
        assert_eq!(a, b, "1/32 bucketing keeps near-unity flow exact");
        assert_eq!(cache2.bakes, 1);
    }

    #[test]
    fn sprite_scale_error_within_bucket_budget() {
        let p = flat_preset(255);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        for d in [1.0, 2.0, 8.0, 31.0, 64.0, 300.0, 512.0] {
            let spr = cache.sprite(0, &p, d, 1.0);
            let err = (spr.d as f64 - d).abs() / d;
            assert!(err <= 0.045, "d={d} sprite={} err={err:.3}", spr.d);
        }
    }

    #[test]
    fn lru_eviction_respects_cap() {
        let p = flat_preset(255);
        let mut cache = SpriteCache::new(4096); // tiny: forces eviction
        let mut distinct = std::collections::HashSet::new();
        for d in 1..64 {
            let dd = d as f64 * 8.0;
            distinct.insert(bucket_of(dd));
            cache.sprite(0, &p, dd, 1.0);
        }
        assert!(cache.bytes() <= 4096 + 512 * 512 * 2, // cap + one in-flight sprite
                "bytes={} must stay near cap", cache.bytes());
        assert_eq!(cache.bakes as usize, distinct.len(),
                   "one bake per distinct bucket, collisions excluded");
    }

    #[test]
    fn single_dab_cadence_and_residual_carry() {
        let mut p = flat_preset(255);
        p.spacing = crate::preset::SpacingModel::Linear(0.25);
        let mut dabs = Vec::new();
        let mut residual = 0.0;
        let seg = ([0.0, 0.0, 1.0], [100.0, 0.0, 1.0]);
        walk_segment(seg, 64.0, &p, &mut residual, &mut dabs);
        // step = 0.25*64 = 16 -> dabs at x = 0,16,...,96  => 7 dabs
        assert_eq!(dabs.len(), 7);
        assert!((residual - (112.0 - 100.0)).abs() < 1e-9);
        let before = dabs.len();
        let seg2 = ([100.0, 0.0, 1.0], [104.0, 0.0, 1.0]);
        walk_segment(seg2, 64.0, &p, &mut residual, &mut dabs);
        // residual 12 enters a 4px segment: t never reaches the loop,
        // carry shrinks by the segment length -> 12 - 4 = 8
        assert_eq!(dabs.len(), before);
        assert!((residual - 8.0).abs() < 1e-9);
    }

    #[test]
    fn auto_spacing_sqrt_model() {
        // Krita automatic spacing: step = coeff * sqrt(d)
        let mut p = flat_preset(255);
        p.spacing = crate::preset::SpacingModel::Auto(0.8);
        let mut dabs = Vec::new();
        let mut residual = 0.0;
        // d=64: step 6.4px over 64px -> 11 dabs (t=0..64 in 6.4 steps)
        walk_segment(([0.0, 0.0, 1.0], [64.0, 0.0, 1.0]), 64.0,
                     &p, &mut residual, &mut dabs);
        assert_eq!(dabs.len(), 11);
    }

    #[test]
    fn render_is_deterministic_and_correct_width() {
        let mut p = flat_preset(255);
        p.spacing = crate::preset::SpacingModel::Linear(0.25);
        let mut c1 = SpriteCache::new(SPRITE_CACHE_CAP);
        let mut c2 = SpriteCache::new(SPRITE_CACHE_CAP);
        let s = stroke(vec![[32.0, 32.0, 1.0], [96.0, 32.0, 1.0]], 32.0, 1.0);
        let mut b1 = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        let mut b2 = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        render_stroke(&mut b1, 128, 128, &s, &p, 0, &mut c1).unwrap();
        render_stroke(&mut b2, 128, 128, &s, &p, 0, &mut c2).unwrap();
        assert_eq!(b1.rgba, b2.rgba, "deterministic bytes");

        // coverage: a 32px-wide solid stroke on y=32 -> row 32 fully red
        let px = |x: usize, y: usize| {
            let i = (y * 128 + x) * 4;
            (b1.rgba[i], b1.rgba[i + 3])
        };
        let (r, a) = px(64, 32);
        assert_eq!((r, a), (255, 255));
        // width: sprite rows are the half-open span [cy-16, cy+16)
        // -> canvas rows 16..=47 covered, 15/48 not
        assert!(px(64, 16).1 > 0 && px(64, 47).1 > 0);
        assert_eq!(px(64, 15).1, 0);
        assert_eq!(px(64, 48).1, 0);
    }

    #[test]
    fn overlap_accumulates_like_krita_not_max() {
        // Measured semantics pin (09-06 gate failure root cause): the
        // stroke interior saturates to the opacity cap, because real
        // Krita accumulates overlapping per-dab deposits. Max-merge
        // froze the interior at the mask peak (218/255 = 0.855) and
        // failed every size-64 gate cell.
        let mut p = flat_preset(218); // B2's mask peak
        p.opacity_active = true;
        p.opacity_curve = Curve::from_pairs(vec![(0.0, 0.0), (1.0, 1.0)]);
        p.spacing = crate::preset::SpacingModel::Linear(0.1);
        let s = stroke(vec![[32.0, 32.0, 1.0], [96.0, 32.0, 1.0]], 64.0, 1.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let mut buf = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        render_stroke(&mut buf, 128, 128, &s, &p, 0, &mut cache).unwrap();
        let alpha_at = |x: usize, y: usize| buf.rgba[(y * 128 + x) * 4 + 3];
        // interior: ~10 overlapping dabs of 0.855 -> 1-(1-.855)^10 ~= 1.0
        assert_eq!(alpha_at(64, 32), 255,
                   "interior must saturate (218 under the old max-merge)");
        // x=127 sits 31px past the last dab centre (96) and 37px past
        // the one before: exactly ONE un-overlapped dab, mask peak
        // alpha 0.855*255 ~= 218
        let tail = alpha_at(127, 32);
        assert!(tail > 150 && tail < 240,
                "un-overlapped dab keeps the mask peak, got {tail}");
    }

    #[test]
    fn pressure_gradation_drives_width_and_alpha() {
        // size-active preset (B5 semantics): width follows pressure
        let mut p = flat_preset(255);
        p.size_active = true;
        p.size_curve = Curve::from_pairs(vec![(0.0, 0.0), (1.0, 1.0)]);
        let s = stroke(vec![[10.0, 64.0, 0.2], [118.0, 64.0, 1.0]], 64.0, 1.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let mut buf = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        render_stroke(&mut buf, 128, 128, &s, &p, 0, &mut cache).unwrap();
        let alpha_at = |x: usize, y: usize| buf.rgba[(y * 128 + x) * 4 + 3];
        let col_span = |x: usize| {
            let lo = (0..128).find(|&y| alpha_at(x, y) > 0);
            let hi = (0..128).rev().find(|&y| alpha_at(x, y) > 0);
            match (lo, hi) {
                (Some(a), Some(b)) => b - a + 1,
                _ => 0,
            }
        };
        let thin = col_span(16); // near the low-pressure end
        let fat = col_span(112); // near the high-pressure end
        assert!(fat > thin * 2, "width must track pressure: thin={thin} fat={fat}");

        // opacity-active preset (B2 semantics): the STROKE cap follows
        // pressure, width stays constant
        let mut p2 = flat_preset(255);
        p2.opacity_active = true;
        p2.opacity_curve = Curve::from_pairs(vec![(0.0, 0.0), (1.0, 1.0)]);
        let mut buf2 = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        let mut cache2 = SpriteCache::new(SPRITE_CACHE_CAP);
        render_stroke(&mut buf2, 128, 128, &s, &p2, 0, &mut cache2).unwrap();
        let alpha_at2 = |x: usize, y: usize| buf2.rgba[(y * 128 + x) * 4 + 3];
        let max_a = |x: usize| (0..128).map(|y| alpha_at2(x, y)).max().unwrap();
        assert!(max_a(16) < max_a(112),
                "cap must track pressure: {} vs {}", max_a(16), max_a(112));
        // ...while width stays ~constant (both >= half the fat width)
        let span2 = |x: usize| {
            let lo = (0..128).find(|&y| alpha_at2(x, y) > 0);
            let hi = (0..128).rev().find(|&y| alpha_at2(x, y) > 0);
            match (lo, hi) {
                (Some(a), Some(b)) => b - a + 1,
                _ => 0,
            }
        };
        assert!(span2(16) >= span2(112) / 2,
                "width must NOT track pressure on B2: {} vs {}", span2(16), span2(112));
    }

    #[test]
    fn stroke_opacity_accumulates_per_dab_no_cap() {
        // measured 09-06: the bridge peaks at 1-(1-op)^n in the dab
        // overlap count even at stroke opacity 0.3 (0.584 = 1-0.7^2 at
        // size 64) - opacity rides INSIDE each dab, there is no
        // stroke-level cap. Flat mask, spacing 0.1 -> many overlapping
        // dabs of alpha 0.3 -> the interior saturates toward 1.0.
        let mut p = flat_preset(255);
        p.spacing = crate::preset::SpacingModel::Linear(0.1);
        let s = stroke(vec![[32.0, 32.0, 1.0], [96.0, 32.0, 1.0]], 64.0, 0.3);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let mut buf = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        render_stroke(&mut buf, 128, 128, &s, &p, 0, &mut cache).unwrap();
        let a = buf.rgba[(32 * 128 + 64) * 4 + 3];
        assert!(a >= 249, "saturated interior of 0.3-alpha dabs, got {}", a);
        // and a single isolated dab must read op, not op squared
        let s1 = stroke(vec![[32.0, 32.0, 1.0], [36.0, 32.0, 1.0]], 64.0, 0.3);
        let mut buf2 = LayerBuf { rgba: vec![0; 128 * 128 * 4] };
        render_stroke(&mut buf2, 128, 128, &s1, &p, 0, &mut cache).unwrap();
        let a1 = buf2.rgba[(32 * 128 + 32) * 4 + 3];
        assert!((a1 as i32 - 77).abs() <= 3,
                "one dab of opacity 0.3 = 77, got {}", a1);
    }

    #[test]
    fn estimate_dabs_tracks_arc_and_spacing() {
        let mut p = flat_preset(255);
        p.spacing = crate::preset::SpacingModel::Linear(0.25);
        let s = stroke(vec![[0.0, 0.0, 1.0], [100.0, 0.0, 1.0]], 64.0, 1.0);
        assert_eq!(estimate_dabs(&s, &p), 7); // 100/16 -> ceil
        p.spacing = crate::preset::SpacingModel::Linear(2.0);
        assert_eq!(estimate_dabs(&s, &p), 1); // sparse
    }

    // ------------------------------------------------ procedural tips

    fn auto_preset(id: MaskGenId, hf: f64, vf: f64, aa: bool, dia: f64) -> KppPreset {
        let mut p = flat_preset(255);
        p.mask.clear();
        p.tip_w = 0;
        p.tip_h = 0;
        p.mask_gen = Some(MaskGen {
            id,
            hfade: hf,
            vfade: vf,
            aa,
            definition_diameter: dia,
        });
        p
    }

    fn alpha_at(spr: &Sprite, x: i64, y: i64) -> u8 {
        // undo the log encoding for a readable assertion
        let dep = spr.dep[y as usize * spr.d + x as usize];
        let a = 1.0 - (-(dep as f64) / DEP_SHIFT).exp();
        (a * 255.0).round() as u8
    }

    #[test]
    fn circle_hard_tip_geometry() {
        // PixelArt Round semantics: hf=vf=0 -> xfc=yfc=1, hard edge, no AA
        let p = auto_preset(MaskGenId::Circle, 0.0, 0.0, false, 31.07);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 8.0, 1.0).clone();
        assert_eq!(spr.d, 8);
        // centre opaque, rim transparent
        for yy in 0..8i64 {
            eprintln!("row {yy}: {}", (0..8).map(|xx| format!("{:3}", alpha_at(&spr, xx, yy))).collect::<Vec<_>>().join(""));
        }
        assert_eq!(alpha_at(&spr, 4, 4), 255);
        assert_eq!(alpha_at(&spr, 0, 4), 0, "n == 1 at the rim -> transparent");
        // lattice x = i - 4: pixel 7 -> x=3, n=0.5625, nf=9
        // t = 255*0.5625*8/8.4375 = 136 -> alpha 119
        assert_eq!(alpha_at(&spr, 7, 4), 119, "255 - 136");
        // pixel 2 -> x=-2: n=0.25, nf=4 -> t=51 -> alpha 204
        assert_eq!(alpha_at(&spr, 2, 4), 204);
    }

    #[test]
    fn circle_fade_band_is_parabolic() {
        // hf=0.5, w=64: opaque core to r=16, closed form
        // t = 255*(n - hf^2)/(1 - hf^2) with n = (r/32)^2
        let p = auto_preset(MaskGenId::Circle, 0.5, 0.5, false, 5.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 64.0, 1.0).clone();
        assert_eq!(spr.d, 64);
        // centre sample lattice: x - 32; opaque core: nf < 1 -> |x| < 16
        assert_eq!(alpha_at(&spr, 32, 32), 255);
        assert_eq!(alpha_at(&spr, 47, 32), 255, "x offset 15 inside the core");
        // lattice x = i - 32 = -24 -> n = (24/32)^2 = 0.5625, nf = 2.25
        // t = 255*0.5625*1.25/1.6875 = 106 -> alpha 149
        assert_eq!(alpha_at(&spr, 8, 32), 149, "parabolic band value");
        assert_eq!(alpha_at(&spr, 56, 32), 149, "symmetric");
    }

    #[test]
    fn soft_tip_uses_spline_through_knots_and_ignores_fades() {
        // Airbrush linear: curve "0,0.495496;0.253012,0.198198;
        // 0.506024,0.0726474;1,0", definition diameter 300
        let knots = vec![
            (0.0, 0.495496),
            (0.253012, 0.198198),
            (0.506024, 0.0726474),
            (1.0, 0.0),
        ];
        // hfade/vfade nonzero must NOT leak into the soft profile
        let p = auto_preset(MaskGenId::Soft(knots.clone()), 0.7, 0.7, false, 300.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 64.0, 1.0).clone();
        // centre: curve(0) = 0.495496 -> transparency (1-c)*255 = 128.65 -> 128
        assert_eq!(alpha_at(&spr, 32, 32), 127);
        // spline interpolates each knot (table lerp => +-1 slack):
        // knot (0.253012, 0.198198) -> transparency 204.6 -> 204/205
        let dist = 0.253012f64;
        let px = (dist.sqrt() * 32.0).round() as i64; // dist IS squared radius
        let a = alpha_at(&spr, 32 + px, 32);
        let want = ((1.0 - 0.198198) * 255.0) as u8;
        assert!(
            (a as i32 - (255 - want as i32)).abs() <= 1,
            "knot interpolation off: {a} vs {want}"
        );
        // near the rim the curve has decayed to ~0 alpha (no AA here)
        assert!(alpha_at(&spr, 63, 32) < 15, "rim must be near-transparent");
        // the same preset with different hfade bakes byte-identical
        let p2 = auto_preset(MaskGenId::Soft(knots), 0.0, 0.0, false, 300.0);
        let mut cache2 = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr2 = cache2.sprite(0, &p2, 64.0, 1.0).clone();
        assert_eq!(spr.dep, spr2.dep, "fades must not affect soft tips");
    }

    #[test]
    fn gauss_tip_opaque_centre_transparent_rim() {
        // Basic tip gaussian: hf=0 vf=0.5 -> fade=0.75, aa on
        let p = auto_preset(MaskGenId::Gauss, 0.0, 0.5, true, 42.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 32.0, 1.0).clone();
        // alphafactor*(erf(c)-erf(-c)) == 255 exactly at the centre
        assert_eq!(alpha_at(&spr, 16, 16), 255);
        assert_eq!(alpha_at(&spr, 16 + 16, 16), 0, "beyond radius -> 255");
        assert_eq!(alpha_at(&spr, 16 - 16, 16), 0);
    }

    #[test]
    fn aa_small_dabs_supersample_3x3() {
        // AA brush at d=8 < 10 must supersample (int division), and the
        // result must differ from the un-supersampled bake
        let p = auto_preset(MaskGenId::Circle, 0.9, 0.9, true, 30.0);
        let mut cache = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr = cache.sprite(0, &p, 8.0, 1.0).clone();
        // centre of a hf=0.9 tip is deep inside the opaque core
        assert_eq!(alpha_at(&spr, 4, 4), 255);
        // (0,0) is the corner: all 9 subsamples outside -> 0
        assert_eq!(alpha_at(&spr, 0, 0), 0);
        // non-AA bake of the same preset differs on the rim ring
        let p2 = auto_preset(MaskGenId::Circle, 0.9, 0.9, false, 30.0);
        let mut cache2 = SpriteCache::new(SPRITE_CACHE_CAP);
        let spr2 = cache2.sprite(0, &p2, 8.0, 1.0).clone();
        assert_ne!(spr.dep, spr2.dep, "supersampling must change the rim");
    }

    #[test]
    fn bake_auto_deterministic_and_reuses_cache() {
        let p = auto_preset(MaskGenId::Soft(vec![(0.0, 0.5), (1.0, 0.0)]), 0.0, 0.0, false, 50.0);
        let mut c1 = SpriteCache::new(SPRITE_CACHE_CAP);
        let mut c2 = SpriteCache::new(SPRITE_CACHE_CAP);
        let a = c1.sprite(0, &p, 32.0, 1.0).dep.clone();
        let b = c2.sprite(0, &p, 32.0, 1.0).dep.clone();
        assert_eq!(a, b, "procedural bake is pure math - must be identical");
        let hits_before = c1.hits;
        let bakes_before = c1.bakes;
        c1.sprite(0, &p, 32.0, 1.0);
        assert_eq!(c1.bakes, bakes_before);
        assert_eq!(c1.hits, hits_before + 1);
    }
}
