//! .kpp (Krita brush preset) parser for the tip-stamp engine.
//!
//! A Krita 5.x .kpp is a PNG whose main image IS the brush tip bitmap,
//! plus a `preset` metadata chunk: zlib-deflated XML in a zTXt chunk (or
//! plain tEXt for older presets), carrying every sensor curve the paint
//! engine uses. This module extracts the minimal parameter set the
//! tip-stamp path needs and flags everything it cannot reproduce
//! honestly (`unsupported`).
//!
//! Option semantics — measured, not guessed (real Krita brush deposits,
//! the calibration tables under tools/data):
//! - Krita serialises ALL sensor options into every preset; the gate
//!   that decides whether an option ACTUALLY drives the dab is a
//!   separate boolean, and the `*UseCurve` names are misleading:
//!     size    follows pressure iff `PressureSize` is true
//!     opacity follows pressure iff `OpacityUseCurve` is true
//!     flow    follows pressure iff `FlowUseCurve` is true
//!   Evidence: Basic-2 (PressureSize=false) deposits constant coverage
//!   width with alpha == pressure exactly; Basic-5 (PressureSize=true,
//!   opacity off) does the opposite. A stored curve on an INACTIVE
//!   option is dead data - Basic-2 carries a size curve it never uses.
//! - A missing `<curve>` inside an active sensor behaves as identity
//!   (opacity = pressure); observed presets store an explicit
//!   `0,0;1,1` curve anyway.
//!
//! Tip decode, two families (09-06, source-verified against
//! D:\krita-src 5.3):
//! - `predefined`/`png`/`gbr` tips: the PNG main image IS the tip
//!   bitmap; mask = `a * (255 - luma) / 255` (dark = opaque).
//! - `auto_brush` tips: the PNG is only an ICON. The real mask comes
//!   from the `<MaskGenerator>` XML (KisCircleMaskGenerator family) and
//!   is generated procedurally (tip.rs) - decoding the icon instead was
//!   the 09-06 gate corruption: valueAt returns TRANSPARENCY
//!   (0 = opaque centre), the icon's luma has no such meaning, and the
//!   soft (`id="soft"`) profile is a cubic-spline curve whose X axis is
//!   the SQUARED normalised radius (`norme(a,b) = a*a + b*b`, no sqrt).

use anyhow::{Context, Result};

pub const FLOW_GUARD_MIN: f64 = 0.9;

/// Procedural auto-brush tip parameters (Krita `MaskGenerator` attrs).
/// The profile is scale-invariant: coefficients normalise by the
/// requested dab diameter (`xcoef = 2/effW`), so nothing here depends
/// on the paint size except `definition_diameter` below.
#[derive(Clone, Debug)]
pub struct MaskGen {
    pub id: MaskGenId,
    pub hfade: f64,
    pub vfade: f64,
    /// `antialiasEdges` - also switches on 3x3 supersampling for dabs
    /// narrower than 10px (KisMaskGenerator::shouldSupersample).
    pub aa: bool,
    /// The curve generator bakes its spline lookup table ONCE, at
    /// `curveResolution = round(diameter * OVERSAMPLING)` from the
    /// DEFINITION diameter (setScale never rebuilds it) - so the table
    /// resolution, not the profile, follows this attr.
    pub definition_diameter: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum MaskGenId {
    /// id="default" - KisCircleMaskGenerator: opaque core out to
    /// hfade*d/2, then a falloff that is linear in the SQUARED
    /// normalised radius (parabolic in r).
    Circle,
    /// id="soft" - KisCurveCircleMaskGenerator: the softness_curve knots
    /// ARE the alpha profile, evaluated through a natural cubic spline
    /// (KisCubicSpline, C2 interior, natural ends).
    Soft(Vec<(f64, f64)>),
    /// id="gauss" - KisGaussCircleMaskGenerator: erf profile.
    Gauss,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompositeOp {
    Normal,
    Erase,
}

/// Piecewise-linear sensor curve with clamping; empty = identity.
#[derive(Clone, Debug, Default)]
pub struct Curve {
    pts: Vec<(f64, f64)>,
}

impl Curve {
    pub fn from_pairs(pts: Vec<(f64, f64)>) -> Self {
        Curve { pts }
    }

    pub fn is_identity(&self) -> bool {
        self.pts.len() == 2
            && self.pts[0].0.abs() < 1e-4
            && self.pts[0].1.abs() < 1e-4
            && (self.pts[1].0 - 1.0).abs() < 1e-4
            && (self.pts[1].1 - 1.0).abs() < 1e-4
    }

    /// Evaluate at pressure p; empty curve = identity (return p).
    pub fn at(&self, p: f64) -> f64 {
        if self.pts.is_empty() {
            return p;
        }
        if p <= self.pts[0].0 {
            return self.pts[0].1;
        }
        if p >= self.pts[self.pts.len() - 1].0 {
            return self.pts[self.pts.len() - 1].1;
        }
        for w in self.pts.windows(2) {
            if p <= w[1].0 {
                let u = (p - w[0].0) / (w[1].0 - w[0].0);
                return w[0].1 + u * (w[1].1 - w[0].1);
            }
        }
        self.pts[self.pts.len() - 1].1
    }

    /// Minimum value over the pressure domain [lo, hi] (curve endpoints
    /// included), for the flow guard.
    pub fn min_over(&self, lo: f64, hi: f64) -> Option<f64> {
        if self.pts.is_empty() {
            return None;
        }
        let lo_v = self.at(lo);
        let hi_v = self.at(hi);
        let inner = self
            .pts
            .iter()
            .filter(|&&(x, _)| lo <= x && x <= hi)
            .map(|&(_, y)| y);
        Some(inner.chain([lo_v, hi_v]).fold(f64::INFINITY, f64::min))
    }
}

/// Dab cadence, read from the preset's `<Brush>` element. Krita 5.2+
/// runtime only honours these two models: the per-stroke Spacing curve
/// option is gated by `PressureSpacing` (false in every pack preset),
/// so `SpacingValue`/`SpacingSensor` params are dead at paint time.
/// The formula is KisPaintOpUtils::effectiveSpacing:
///   auto   -> coeff * sqrt(diameter)      ("automatic" spacing)
///   linear -> spacing_frac * diameter
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SpacingModel {
    Linear(f64),
    Auto(f64),
}

impl Default for SpacingModel {
    fn default() -> Self {
        // KisBrush default when <Brush spacing> is absent
        SpacingModel::Linear(0.25)
    }
}

impl SpacingModel {
    /// Dab-to-dab step in px for a dab of diameter `d` (never below 0.5px).
    pub fn step(&self, d: f64) -> f64 {
        match *self {
            SpacingModel::Linear(f) => f * d,
            SpacingModel::Auto(c) => c * d.sqrt(),
        }
        .max(0.5)
    }
}

#[derive(Clone, Debug)]
pub struct KppPreset {
    /// Krita resource DB name ("d) Ink-3 Gpen") - the string plan JSONs
    /// carry. Self-description for diagnostics and gate reports; routing
    /// keys off the registry instead.
    #[allow(dead_code)]
    pub resource_name: String,
    #[allow(dead_code)]
    pub file_name: String,
    pub paintopid: String,
    pub composite: CompositeOp,
    pub tip_w: usize,
    pub tip_h: usize,
    /// 0..255 per tip pixel: how much ink one full-alpha dab deposits.
    /// Empty when `mask_gen` is set - auto tips are generated
    /// procedurally per sprite (tip.rs), never decoded from the icon.
    pub mask: Vec<u8>,
    /// Set for `auto_brush` presets: the procedural tip definition.
    pub mask_gen: Option<MaskGen>,
    pub size_active: bool,
    pub size_value: f64,
    pub size_curve: Curve,
    pub opacity_active: bool,
    pub opacity_value: f64,
    pub opacity_curve: Curve,
    pub flow_active: bool,
    pub flow_value: f64,
    pub flow_curve: Curve,
    /// Dab cadence model from the `<Brush>` element (T1.3 定案).
    pub spacing: SpacingModel,
    /// Set when this preset uses an engine feature v1 cannot reproduce;
    /// routing must keep such presets on the capsule path.
    pub unsupported: Option<String>,
}

impl KppPreset {
    /// Dab diameter in px at pressure p for a nominal stroke size.
    pub fn diameter_at(&self, size: f64, p: f64) -> f64 {
        let c = if self.size_active {
            self.size_curve.at(p)
        } else {
            1.0
        };
        (size * self.size_value * c).clamp(1.0, 512.0)
    }

    /// Per-dab flow multiplier at pressure p: the scalar that scales the
    /// tip mask in each dab's deposit (Krita: flow is the per-dab
    /// option; the tip engine accumulates flow*mask in log space).
    pub fn flow_at(&self, p: f64) -> f64 {
        if self.flow_active {
            (self.flow_value * self.flow_curve.at(p)).clamp(0.0, 1.0)
        } else {
            1.0
        }
    }

    /// Stroke-level opacity cap at pressure p. Measured, not guessed:
    /// ink_calib B2 deposits center alpha 0.298 at p=0.3 and 0.992 at
    /// p=1.0 (stroke opacity 1) — per-dab opacity accumulation would
    /// give ~0.95 at p=0.3, so opacity caps the stroke composite while
    /// flow*mask accumulates per dab (tip.rs).
    pub fn opacity_cap(&self, p: f64) -> f64 {
        let c = if self.opacity_active {
            self.opacity_curve.at(p)
        } else {
            1.0
        };
        (self.opacity_value * c).clamp(0.0, 1.0)
    }

    /// True when max-alpha merging reproduces this preset's deposit over
    /// the planner pressure domain [0.3, 1] - flow accumulation below
    /// that cannot be merged with max.
    pub fn flow_guard_ok(&self) -> bool {
        !self.flow_active
            || self.flow_curve.min_over(0.3, 1.0).map_or(true, |m| {
                m >= FLOW_GUARD_MIN || m >= FLOW_GUARD_MIN * 0.999
            })
    }

    /// Load and validate one .kpp. `resource_name` is the registry key
    /// (spaces); the internal `<Preset name>` is checked against it where
    /// present but not required to match byte-for-byte.
    pub fn load(path: &str, resource_name: &str) -> Result<KppPreset> {
        let raw = std::fs::read(path).with_context(|| format!("preset file {path}"))?;
        let xml = &preset_xml(&raw)
            .context("no 'preset' metadata chunk (zTXt/tEXt) in .kpp")?;

        let mut p = KppPreset {
            resource_name: resource_name.to_string(),
            file_name: std::path::Path::new(path)
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or(path)
                .to_string(),
            paintopid: xml_root_attr(xml, "paintopid").unwrap_or_default(),
            composite: CompositeOp::Normal,
            tip_w: 0,
            tip_h: 0,
            mask: Vec::new(),
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
            spacing: SpacingModel::default(),
            unsupported: None,
        };

        // --- admission filter (v1 engine capability) ---
        if p.paintopid != "paintbrush" {
            p.unsupported = Some(format!("paintopid={}", p.paintopid));
            return Ok(p);
        }
        match xml_param(xml, "CompositeOp").as_deref() {
            None | Some("normal") => {}
            Some("erase") => {
                p.composite = CompositeOp::Erase;
                p.unsupported = Some("composite=erase".into());
                return Ok(p);
            }
            Some(other) => {
                p.unsupported = Some(format!("composite={other}"));
                return Ok(p);
            }
        }
        for name in ["MaskingBrush/Enabled", "Texture/Pattern/Enabled",
                     "AirbrushOption/isAirbrushing"] {
            if xml_flag(xml, name) {
                p.unsupported = Some(feature_reason(name).into());
                return Ok(p);
            }
        }
        if let Some(rv) = xml_param(xml, "RatioValue") {
            if rv.trim().parse::<f64>().map(|v| (v - 1.0).abs() > 1e-6) == Ok(true) {
                p.unsupported = Some(format!("ratio={rv}"));
                return Ok(p);
            }
        }
        if let Some(pts) = xml_sensor(xml, "RatioSensor").1 {
            if !pts.is_empty() && !Curve::from_pairs(pts.clone()).is_identity() {
                p.unsupported = Some("ratio-sensor-curve".into());
                return Ok(p);
            }
        }
        let (rot_id, rot_pts) = xml_sensor(xml, "RotationSensor");
        if let Some(id) = rot_id {
            if id != "pressure" {
                p.unsupported = Some(format!("rotation-sensor={id}"));
                return Ok(p);
            }
        }
        if let Some(pts) = rot_pts {
            if !pts.is_empty() && !Curve::from_pairs(pts).is_identity() {
                // identity is Krita's default serialization and a no-op on
                // ratio=1 round tips; anything else varies the dab
                p.unsupported = Some("rotation-sensor-curve".into());
                return Ok(p);
            }
        }
        for (name, ok) in [("SoftnessValue", 1.0), ("DarkenValue", 1.0)] {
            if let Some(v) = xml_param(xml, name) {
                if v.trim().parse::<f64>().map(|f| (f - ok).abs() > 1e-6) == Ok(true) {
                    p.unsupported = Some(format!("{name}={v}"));
                    return Ok(p);
                }
            }
        }
        // Sensor models: the plan JSONs carry only (x, y, pressure), so a
        // preset is reproducible iff every deposit-driving sensor reads
        // pressure (or nothing). sensorslist composites speed/distance/
        // fade children; speed needs velocity we do not have.
        for name in ["SizeSensor", "OpacitySensor", "FlowSensor"] {
            let (id, _) = xml_sensor(xml, name);
            if let Some(id) = id {
                if id != "pressure" {
                    p.unsupported = Some(format!("{name}={id}"));
                    return Ok(p);
                }
            }
        }

        // --- tip definition ---
        // auto_brush tips are procedural (MaskGenerator XML); the PNG
        // main image is only an icon and MUST NOT be decoded as a mask.
        if xml_brush_attr(xml, "type").as_deref() == Some("auto_brush") {
            match parse_maskgen(xml) {
                Ok(mg) => p.mask_gen = Some(mg),
                Err(reason) => {
                    p.unsupported = Some(reason);
                    return Ok(p);
                }
            }
        } else {
            let (tw, th, mask) = decode_tip(&raw)
                .with_context(|| format!("decoding tip from {path}"))?;
            p.tip_w = tw;
            p.tip_h = th;
            p.mask = mask;
        }

        // --- option enable bits + curves ---
        p.size_active = xml_flag(xml, "PressureSize");
        p.size_value = xml_param(xml, "SizeValue")
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(1.0);
        p.size_curve = xml_curve(xml, "SizeSensor");
        p.opacity_active = xml_flag(xml, "OpacityUseCurve");
        p.opacity_value = xml_param(xml, "OpacityValue")
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(1.0);
        p.opacity_curve = xml_curve(xml, "OpacitySensor");
        p.flow_active = xml_flag(xml, "FlowUseCurve");
        p.flow_value = xml_param(xml, "FlowValue")
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(1.0);
        p.flow_curve = xml_curve(xml, "FlowSensor");

        // Dab cadence: <Brush useAutoSpacing/autoSpacingCoeff/spacing>.
        p.spacing = spacing_model_from_xml(xml);

        if !p.flow_guard_ok() {
            p.unsupported = Some("flow-buildup".into());
        }
        Ok(p)
    }
}

fn feature_reason(name: &str) -> String {
    match name {
        "MaskingBrush/Enabled" => "masking-brush".into(),
        "Texture/Pattern/Enabled" => "pattern-texture".into(),
        "AirbrushOption/isAirbrushing" => "airbrush".into(),
        _ => name.into(),
    }
}

// ---------------------------------------------------------------- PNG tip

fn decode_tip(raw: &[u8]) -> Result<(usize, usize, Vec<u8>)> {
    let mut dec = png::Decoder::new(std::io::Cursor::new(raw));
    dec.set_transformations(png::Transformations::EXPAND);
    let mut rdr = dec.read_info().context("png header")?;
    let (w, h) = (rdr.info().width as usize, rdr.info().height as usize);
    let mut buf = vec![0u8; rdr.output_buffer_size()];
    let info = rdr.next_frame(&mut buf).context("png frame")?;
    anyhow::ensure!(
        info.bit_depth == png::BitDepth::Eight,
        "tip bit depth {:?} unsupported",
        info.bit_depth
    );
    // EXPAND gives us gray / ga / rgb / rgba in 8-bit
    let samples = info.color_type.samples();
    anyhow::ensure!(
        matches!(info.color_type,
            png::ColorType::Grayscale | png::ColorType::GrayscaleAlpha
            | png::ColorType::Rgb | png::ColorType::Rgba),
        "tip color type {:?} unsupported",
        info.color_type
    );
    let n = w * h;
    let mut mask = vec![0u8; n];
    for i in 0..n {
        let px = &buf[i * samples..i * samples + samples];
        let (a, luma) = match samples {
            1 => (255u32, px[0] as u32),
            2 => (px[1] as u32, px[0] as u32),
            3 => (255u32, luma(px[0], px[1], px[2])),
            _ => (px[3] as u32, luma(px[0], px[1], px[2])),
        };
        mask[i] = ((a * (255 - luma) + 127) / 255) as u8;
    }
    Ok((w, h, mask))
}

fn luma(r: u8, g: u8, b: u8) -> u32 {
    (r as u32 * 299 + g as u32 * 587 + b as u32 * 114) / 1000
}

// ------------------------------------------------------------- .kpp chunks

fn read_chunks(mut data: &[u8]) -> impl Iterator<Item = (&[u8], &[u8])> {
    data = &data[8..]; // skip PNG signature; walk the chunk stream after it
    std::iter::from_fn(move || {
        if data.len() < 8 {
            return None;
        }
        let len = u32::from_be_bytes([data[0], data[1], data[2], data[3]]) as usize;
        if data.len() < 12 + len {
            return None;
        }
        let (ctype, rest) = data[4..].split_at(4);
        let (payload, tail) = rest.split_at(len);
        data = &tail[4..]; // skip CRC
        Some((ctype, payload))
    })
}

fn preset_xml(raw: &[u8]) -> Option<String> {
    for (ctype, payload) in read_chunks(raw) {
        match ctype {
            b"zTXt" => {
                let nul = payload.iter().position(|&b| b == 0)?;
                if &payload[..nul] != b"preset" || payload[nul + 1] != 0 {
                    continue;
                }
                let inflated = miniz_oxide::inflate::decompress_to_vec_zlib(
                    &payload[nul + 2..],
                )
                .ok()?;
                return Some(String::from_utf8_lossy(&inflated).into_owned());
            }
            b"tEXt" => {
                let nul = payload.iter().position(|&b| b == 0)?;
                if &payload[..nul] != b"preset" {
                    continue;
                }
                return Some(String::from_utf8_lossy(&payload[nul + 1..]).into_owned());
            }
            _ => {}
        }
    }
    None
}

// -------------------------------------------------------------- XML scanner

/// Raw inner text of `<param name="...">...</param>` for ANY attribute
/// order (Gpen writes `type` first, Basic-2 writes `name` first).
fn xml_param(xml: &str, name: &str) -> Option<String> {
    let mut from = 0;
    while let Some(i) = xml[from..].find("<param") {
        let start = from + i;
        let tag_end = start + xml[start..].find('>')?;
        let tag = &xml[start..tag_end];
        if let Some(val) = tag_attr(tag, "name") {
            if val == name {
                let body_start = tag_end + 1;
                let end = xml[body_start..].find("</param>")? + body_start;
                let mut text = xml[body_start..end].trim().to_string();
                if text.starts_with("<![CDATA[") && text.ends_with("]]>") {
                    text = text[9..text.len() - 3].trim().to_string();
                }
                return Some(text);
            }
        }
        from = tag_end;
    }
    None
}

fn tag_attr<'a>(tag: &'a str, key: &str) -> Option<&'a str> {
    let marker = format!("{key}=\"");
    let i = tag.find(&marker)? + marker.len();
    let rest = &tag[i..];
    Some(&rest[..rest.find('"')?])
}

fn xml_root_attr(xml: &str, key: &str) -> Option<String> {
    let start = xml.find("<Preset ")?;
    let end = start + xml[start..].find('>')?;
    tag_attr(&xml[start..end], key).map(|s| s.to_string())
}

/// Attr of the `<Brush ...>` element - the tip-level definition that
/// actually drives runtime spacing (spacing / useAutoSpacing /
/// autoSpacingCoeff / scale / BrushVersion).
fn xml_brush_attr(xml: &str, key: &str) -> Option<String> {
    let start = xml.find("<Brush")?;
    let end = start + xml[start..].find('>')?;
    tag_attr(&xml[start..end], key).map(|s| s.to_string())
}

/// Dab cadence from the preset XML. NOT the SpacingValue param -
/// KisKritaSensorPack reads the option's checkbox from `PressureSpacing`
/// (false in every pack preset), so the Spacing curve option never
/// applies at runtime; only these `<Brush>` attrs matter.
fn spacing_model_from_xml(xml: &str) -> SpacingModel {
    let auto = xml_brush_attr(xml, "useAutoSpacing")
        .map(|v| v.trim() == "1" || v.trim().eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if auto {
        SpacingModel::Auto(
            xml_brush_attr(xml, "autoSpacingCoeff")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(1.0),
        )
    } else {
        SpacingModel::Linear(
            xml_brush_attr(xml, "spacing")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(0.25),
        )
    }
}

fn xml_flag(xml: &str, name: &str) -> bool {
    xml_param(xml, name).map_or(false, |v| v.trim().eq_ignore_ascii_case("true"))
}

/// Parse the `<MaskGenerator .../>` element of an auto_brush preset
/// into the procedural tip definition, or an honest unsupported reason.
/// Defaults mirror KisMaskGenerator::fromXML.
fn parse_maskgen(xml: &str) -> std::result::Result<MaskGen, String> {
    let start = xml.find("<MaskGenerator").ok_or("auto-brush-no-maskgen")?;
    let tag_end = start + xml[start..].find('>').ok_or("maskgen-tag")?;
    let tag = &xml[start..tag_end];
    let attr = |k: &str| tag_attr(tag, k).map(|s| s.to_string());
    let f = |k: &str, dflt: f64| {
        attr(k)
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(dflt)
    };

    let gen_type = attr("type").unwrap_or_else(|| "circle".into());
    if gen_type != "circle" {
        return Err(format!("tip-shape={gen_type}"));
    }
    let spikes: i64 = attr("spikes")
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(2);
    if spikes != 2 {
        return Err(format!("spikes={spikes}"));
    }
    let ratio = f("ratio", 1.0);
    if (ratio - 1.0).abs() > 1e-6 {
        return Err(format!("tip-ratio={ratio}"));
    }

    let id = MaskGenId::from_attr(
        &attr("id").unwrap_or_else(|| "default".into()),
        &attr("softness_curve").unwrap_or_else(|| "0,0;1,1;".into()),
    )?;
    Ok(MaskGen {
        hfade: f("hfade", 0.0),
        vfade: f("vfade", 0.0),
        aa: attr("antialiasEdges")
            .map(|v| v.trim() != "0")
            .unwrap_or(false),
        definition_diameter: f("diameter", 100.0),
        id,
    })
}

impl MaskGenId {
    fn from_attr(id: &str, softness_curve: &str) -> std::result::Result<MaskGenId, String> {
        match id {
            "default" => Ok(MaskGenId::Circle),
            "soft" => {
                let pts = parse_curve_attr(softness_curve);
                if pts.len() < 2 {
                    return Err(format!("softness-curve-{}-pts", pts.len()));
                }
                Ok(MaskGenId::Soft(pts))
            }
            "gauss" => Ok(MaskGenId::Gauss),
            other => Err(format!("maskgen-id={other}")),
        }
    }
}

/// "0,0.495;0.25,0.198;1,0;" -> [(0.0, 0.495), ...]
fn parse_curve_attr(s: &str) -> Vec<(f64, f64)> {
    s.split(';')
        .filter(|t| !t.trim().is_empty())
        .filter_map(|pair| {
            let (x, y) = pair.split_once(',')?;
            Some((x.trim().parse().ok()?, y.trim().parse().ok()?))
        })
        .collect()
}

/// Sensor param text -> (sensor id, curve points). Missing `<curve>` = None.
fn xml_sensor(xml: &str, name: &str) -> (Option<String>, Option<Vec<(f64, f64)>>) {
    let text = match xml_param(xml, name) {
        Some(t) => t,
        None => return (None, None),
    };
    let id = {
        let marker = "id=\"";
        text.find(marker).map(|i| {
            let rest = &text[i + marker.len()..];
            rest[..rest.find('"').unwrap_or(0)].to_string()
        })
    };
    let pts = text.find("<curve>").map(|i| {
        let rest = &text[i + 7..];
        let body = &rest[..rest.find("</curve>").unwrap_or(0)];
        body.split(';')
            .filter(|s| !s.trim().is_empty())
            .filter_map(|pair| {
                let (x, y) = pair.split_once(',')?;
                Some((x.trim().parse().ok()?, y.trim().parse().ok()?))
            })
            .collect()
    });
    (id, pts)
}

fn xml_curve(xml: &str, name: &str) -> Curve {
    Curve::from_pairs(xml_sensor(xml, name).1.unwrap_or_default())
}

// ------------------------------------------------------------------ tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn curve_interpolates_and_clamps() {
        let c = Curve::from_pairs(vec![(0.0, 0.0), (0.35, 0.1), (1.0, 1.0)]);
        assert!((c.at(0.0) - 0.0).abs() < 1e-9);
        assert!((c.at(0.35) - 0.1).abs() < 1e-9);
        assert!((c.at(0.175) - 0.05).abs() < 1e-9);
        assert!((c.at(0.3) - 0.0857).abs() < 1e-3); // B5 size curve
        assert!((c.at(-0.5) - 0.0).abs() < 1e-9); // clamp low
        assert!((c.at(1.5) - 1.0).abs() < 1e-9); // clamp high
        assert!((c.min_over(0.3, 1.0).unwrap() - 0.0857).abs() < 1e-3);
    }

    #[test]
    fn empty_curve_is_identity() {
        let c = Curve::default();
        assert!((c.at(0.3) - 0.3).abs() < 1e-9);
        assert!((c.at(1.0) - 1.0).abs() < 1e-9);
        assert!(c.min_over(0.3, 1.0).is_none());
    }

    #[test]
    fn xml_param_is_attribute_order_agnostic() {
        let xml = r#"<Preset paintopid="paintbrush" name="x"> <param type="string" name="CompositeOp"><![CDATA[normal]]></param> <param name="SizeValue" type="string"><![CDATA[1]]></param> </Preset>"#;
        assert_eq!(xml_param(xml, "CompositeOp").as_deref(), Some("normal"));
        assert_eq!(xml_param(xml, "SizeValue").as_deref(), Some("1"));
        assert_eq!(xml_root_attr(xml, "paintopid").as_deref(), Some("paintbrush"));
        assert!(xml_flag(xml, "SizeUseMissing") == false);
    }

    #[test]
    fn xml_sensor_parses_curve_points() {
        let xml = r#"<param name="FlowSensor" type="string"><![CDATA[<!DOCTYPE params> <params id="pressure"> <curve>0,0;0.0361991,0.266332;1,1;</curve> </params> ]]></param>"#;
        let (id, pts) = xml_sensor(xml, "FlowSensor");
        assert_eq!(id.as_deref(), Some("pressure"));
        let pts = pts.unwrap();
        assert_eq!(pts.len(), 3);
        assert!((pts[1].1 - 0.266332).abs() < 1e-6);
        // empty sensor: no <curve> element
        let xml2 = r#"<param name="SizeSensor" type="string"><![CDATA[<!DOCTYPE params> <params id="pressure"/> ]]></param>"#;
        let (id2, pts2) = xml_sensor(xml2, "SizeSensor");
        assert_eq!(id2.as_deref(), Some("pressure"));
        assert!(pts2.is_none());
    }

    #[test]
    fn brush_element_drives_spacing_model() {
        // real Basic_circle shape: auto spacing, coeff 1, dead SpacingValue
        let auto = r#"<Preset paintopid="paintbrush"> <Brush type="auto_brush" useAutoSpacing="1" autoSpacingCoeff="1" spacing="0.1"> <MaskGenerator diameter="40"/> </Brush> <param name="SpacingValue"><![CDATA[1]]></param> <param name="PressureSpacing"><![CDATA[false]]></param> </Preset>"#;
        assert_eq!(spacing_model_from_xml(auto), SpacingModel::Auto(1.0));

        // real B2 shape: linear 0.1, no auto
        let lin = r#"<Preset paintopid="paintbrush"> <Brush useAutoSpacing="0" autoSpacingCoeff="1" spacing="0.1" BrushVersion="2"/> </Preset>"#;
        assert_eq!(spacing_model_from_xml(lin), SpacingModel::Linear(0.1));

        // absent <Brush spacing>: KisBrush default 0.25
        let bare = r#"<Preset paintopid="paintbrush"> <Brush useAutoSpacing="0"/> </Preset>"#;
        assert_eq!(spacing_model_from_xml(bare), SpacingModel::Linear(0.25));

        // step math: linear vs sqrt
        assert!((SpacingModel::Linear(0.1).step(64.0) - 6.4).abs() < 1e-9);
        assert!((SpacingModel::Auto(0.8).step(64.0) - 6.4).abs() < 1e-9);
        assert!((SpacingModel::Auto(0.8).step(32.0) - 4.525483399593904)
            .abs() < 1e-9);
    }

    #[test]
    fn option_semantics_match_measured_b2_b5() {
        // B2: size off, opacity on (identity) -> alpha == pressure
        let b2 = KppPreset {
            resource_name: "b) Basic-2 Opacity".into(),
            file_name: String::new(),
            paintopid: "paintbrush".into(),
            composite: CompositeOp::Normal,
            tip_w: 200,
            tip_h: 200,
            mask: vec![255; 200 * 200],
            mask_gen: None,
            size_active: false,
            size_value: 1.0,
            size_curve: Curve::default(),
            opacity_active: true,
            opacity_value: 1.0,
            opacity_curve: Curve::from_pairs(vec![(0.0, 0.0), (1.0, 1.0)]),
            flow_active: false,
            flow_value: 1.0,
            flow_curve: Curve::default(),
            spacing: SpacingModel::Linear(0.1), // <Brush spacing="0.1">
            unsupported: None,
        };
        assert!((b2.opacity_cap(0.3) - 0.3).abs() < 1e-9);
        assert!((b2.opacity_cap(1.0) - 1.0).abs() < 1e-9);
        assert!((b2.flow_at(0.3) - 1.0).abs() < 1e-9, "B2 flow is unity");
        assert!((b2.diameter_at(32.0, 0.3) - 32.0).abs() < 1e-9);
        assert!(b2.flow_guard_ok());

        // B5: size on (steep curve), opacity off, flow ramps to 1 by p=0.068
        let b5 = KppPreset {
            size_active: true,
            size_curve: Curve::from_pairs(vec![(0.0, 0.0), (0.35, 0.1), (1.0, 1.0)]),
            opacity_active: false,
            flow_active: true,
            flow_curve: Curve::from_pairs(vec![
                (0.0, 0.0),
                (0.036, 0.266),
                (0.068, 0.995),
                (1.0, 1.0),
            ]),
            ..b2.clone()
        };
        assert!(b5.flow_at(0.3) >= 0.99, "flow past ramp stays ~1");
        assert!((b5.flow_at(1.0) - 1.0).abs() < 1e-9);
        assert!((b5.opacity_cap(0.5) - 1.0).abs() < 1e-9, "B5 opacity off");
        assert!((b5.diameter_at(32.0, 0.3) - 32.0 * 0.0857).abs() < 1e-2);
        assert!(b5.flow_guard_ok());
    }

    // --- file-based tests: pin the real presets against the vendored
    // brush_tips (committed, drift-guarded against the private
    // originals by tools/check_assets.py) - they run on every clone

    fn tips_available() -> bool {
        std::path::Path::new("tools/data/brush_tips").is_dir()
    }

    #[test]
    fn parses_real_basic2_gpen_sumie() {
        if !tips_available() {
            eprintln!("skip: brush_tips/ not present");
            return;
        }
        let root = "tools/data/brush_tips";
        let b2 = KppPreset::load(
            &format!("{root}/Krita_4_Default_Resources/b)_Basic-2_Opacity.kpp"),
            "b) Basic-2 Opacity",
        )
        .unwrap();
        assert!(b2.unsupported.is_none());
        assert!(!b2.size_active, "B2 size must be off (measured constant width)");
        assert!(b2.opacity_active, "B2 opacity carries pressure");
        // auto tip: procedural, icon not decoded
        assert!(matches!(b2.mask_gen, Some(MaskGen { id: MaskGenId::Circle, .. })),
            "B2 tip is a default circle generator");
        assert!(b2.mask.is_empty(), "auto tips must not decode the PNG icon");

        let gpen = KppPreset::load(
            &format!("{root}/Krita_4_Default_Resources/d)_Ink-3_Gpen.kpp"),
            "d) Ink-3 Gpen",
        )
        .unwrap();
        assert!(gpen.unsupported.is_none(), "Gpen attrs come reversed");

        let sumi = KppPreset::load(
            &format!("{root}/Krita_4_Default_Resources/d)_Ink-8_Sumi-e.kpp"),
            "d) Ink-8 Sumi-e",
        )
        .unwrap();
        assert_eq!(
            sumi.unsupported.as_deref(),
            Some("paintopid=hairybrush"),
            "Sumi-e must stay out of the deterministic tip engine"
        );
    }

    #[test]
    fn parses_auto_tip_families_and_rejects_rect() {
        if !tips_available() {
            eprintln!("skip: brush_tips/ not present");
            return;
        }
        let root = "tools/data/brush_tips/Krita_3_Default_Resources";

        // Airbrush linear: soft curve, 4 knots, no AA, cr from dia=300
        let air = KppPreset::load(&format!("{root}/Airbrush_linear.kpp"), "Airbrush linear")
            .unwrap();
        assert!(air.unsupported.is_none(), "{:?}", air.unsupported);
        let knots = match &air.mask_gen.as_ref().unwrap().id {
            MaskGenId::Soft(k) => k.clone(),
            other => panic!("expected soft, got {other:?}"),
        };
        assert_eq!(knots.len(), 4);
        assert!((knots[0].1 - 0.495496).abs() < 1e-6, "{knots:?}");

        // Basic tip gaussian: gauss id
        let g = KppPreset::load(
            &format!("{root}/Basic_tip_gaussian.kpp"),
            "Basic tip gaussian",
        )
        .unwrap();
        assert!(g.unsupported.is_none(), "{:?}", g.unsupported);
        assert!(matches!(g.mask_gen.as_ref().unwrap().id, MaskGenId::Gauss));

        // Fill block: rect generator (ratio 0.5) stays honestly out
        let fb = KppPreset::load(&format!("{root}/Fill_block.kpp"), "Fill block").unwrap();
        assert_eq!(
            fb.unsupported.as_deref(),
            Some("tip-shape=rect"),
            "rect auto tips are out of v1 scope"
        );
    }
}
