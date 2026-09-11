//! Calibrated ink model. The tables under tools/data were fitted against
//! REAL Krita brush deposits on paper; this module is the render-side
//! twin of the planner's stroke simulator, so both agree
//! stroke-for-stroke.

use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::HashMap;

#[derive(Deserialize, Clone)]
pub struct Table {
    pub p: Vec<f64>,
    pub v: Vec<f64>,
}

#[derive(Deserialize, Clone)]
pub struct SizeCal {
    pub p: Vec<f64>,
    /// coverage width as a FRACTION of nominal size, by pressure
    pub w: Vec<f64>,
}

#[derive(Deserialize, Clone)]
pub struct PresetCal {
    pub sizes: HashMap<String, SizeCal>,
    pub alpha: Table,
}

#[derive(Deserialize, Clone)]
pub struct Calib {
    pub bg_sim: Option<[u8; 3]>,
    pub presets: HashMap<String, PresetCal>,
}

impl Calib {
    pub fn load(path: &str) -> Result<Self> {
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("calibration file {}", path))?;
        Self::from_str(&raw)
    }

    /// wasm/嵌入宿主入口：字符串进，不走 fs。
    pub fn from_str(raw: &str) -> Result<Self> {
        serde_json::from_str(raw).context("parsing ink calibration")
    }

    pub fn bg(&self) -> [u8; 3] {
        self.bg_sim.unwrap_or([232, 238, 208])
    }

    /// Registry membership probe: does the capsule fallback exist for
    /// this preset? Routing reports a loud error, never a silent skip,
    /// when a stroke has neither a tip engine nor a capsule calibration.
    pub fn has(&self, preset: &str) -> bool {
        self.presets.contains_key(preset)
    }

    fn preset(&self, name: &str) -> Result<&PresetCal> {
        self.presets
            .get(name)
            .with_context(|| format!("preset {:?} not in calibration", name))
    }

    /// Nearest calibrated size key, mirroring _sim_draw's
    /// `skey = min(sizes, key=|k - wmax|)`.
    ///
    /// `sizes` is a std HashMap whose iteration order is randomised per
    /// process: an exact tie (off-anchor sizes like 6/12/24 tie e.g.
    /// 4-vs-8) must NOT be decided by encounter order or renders stop
    /// being byte-reproducible. Python's dict iterates in insertion
    /// order (ascending in ink_calib.json), so `min()` there breaks
    /// ties toward the SMALLER key - mirror that exactly.
    fn size_key<'a>(&self, cal: &'a PresetCal, wmax: f64) -> Result<&'a SizeCal> {
        let mut best: Option<(&str, f64, f64)> = None; // (key, dist, key value)
        for k in cal.sizes.keys() {
            let kv: f64 = k.parse().with_context(|| format!("size key {k:?}"))?;
            let d = (kv - wmax).abs();
            let better = match best {
                None => true,
                Some((_, bd, bkv)) => d < bd || (d == bd && kv < bkv),
            };
            if better {
                best = Some((k, d, kv));
            }
        }
        let k = best.with_context(|| "empty size table")?.0;
        Ok(&cal.sizes[k])
    }

    /// Coverage-equivalent stroke width at midpoint pressure.
    pub fn width_at(&self, preset: &str, wmax: f64, p: f64) -> Result<f64> {
        let cal = self.preset(preset)?;
        let sc = self.size_key(cal, wmax)?;
        Ok((wmax * lerp(&sc.p, &sc.w, p)).max(1.0))
    }

    /// Effective deposit alpha at midpoint pressure (already times the
    /// stroke's per-stroke opacity, clamped to 1) - mirrors
    /// `min(1.0, lerp(alpha, p) * opacity)`.
    pub fn alpha_at(&self, preset: &str, p: f64, opacity: f64) -> Result<f64> {
        let cal = self.preset(preset)?;
        Ok((lerp(&cal.alpha.p, &cal.alpha.v, p) * opacity).min(1.0))
    }
}

pub fn lerp(ps: &[f64], vs: &[f64], p: f64) -> f64 {
    debug_assert_eq!(ps.len(), vs.len());
    if p <= ps[0] {
        return vs[0];
    }
    if p >= ps[ps.len() - 1] {
        return vs[vs.len() - 1];
    }
    for i in 1..ps.len() {
        if p <= ps[i] {
            let u = (p - ps[i - 1]) / (ps[i] - ps[i - 1]);
            return vs[i - 1] + u * (vs[i] - vs[i - 1]);
        }
    }
    vs[vs.len() - 1]
}

pub fn hex_rgb(s: &str) -> Result<[u8; 3]> {
    let s = s.trim_start_matches('#');
    anyhow::ensure!(s.len() == 6, "bad colour {s:?}");
    Ok([
        u8::from_str_radix(&s[0..2], 16)?,
        u8::from_str_radix(&s[2..4], 16)?,
        u8::from_str_radix(&s[4..6], 16)?,
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Regression (2026-09-07): size_key broke exact ties by HashMap
    /// encounter order, which is randomised per process - off-anchor
    /// sizes (6/12/24/48) flipped between neighbouring calibration
    /// tables and renders stopped being byte-reproducible. Ties must
    /// always resolve to the smaller key, like Python's dict-backed
    /// `min()` in the twin simulator.
    #[test]
    fn size_key_tie_breaks_to_smaller_key_regardless_of_hash_order() {
        fn calib() -> Calib {
            let json = r#"{"presets": {"b) T": {"sizes": {
                "4": {"p": [0.0, 1.0], "w": [0.4, 0.4]},
                "8": {"p": [0.0, 1.0], "w": [1.0, 1.0]}},
                "alpha": {"p": [0.0, 1.0], "v": [0.8, 0.8]}}}}"#;
            serde_json::from_str(json).unwrap()
        }
        // wmax=6.0 ties 4-vs-8 at distance 2.0; fresh HashMap per call
        // seeds fresh hash order, 200 draws cover both iteration orders.
        let near = |a: f64, b: f64| (a - b).abs() < 1e-9;
        for _ in 0..200 {
            let c = calib();
            // "4" table: width = 6.0 * 0.4 ≈ 2.4 (the "8" table would
            // give 6.0 - the old bug flips between the two)
            assert!(near(c.width_at("b) T", 6.0, 0.5).unwrap(), 2.4));
            // no-tie neighbour sanity: 5.0 is strictly nearer "4"
            assert!(near(c.width_at("b) T", 5.0, 0.5).unwrap(), 2.0));
        }
    }
}
