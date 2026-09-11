//! .psd writer: hand-rolled, byte-deterministic, 8-bit RGB layered PSD.
//!
//! Layout verified against the Adobe "Photoshop File Formats Specification"
//! and cross-checked against two real readers' sources (psd-tools'
//! layer_and_mask.py, Krita 5's libs/psd/*.cpp). The four traps that most
//! easily produce a "corrupt-looking" file:
//!
//! - E1: layer channel data is PER-CHANNEL records (u16 compression + that
//!   channel's own row-count table + payload), while the merged image uses
//!   ONE count table for rows*channels after a single compression marker.
//! - E2: a layer record's channel `length` INCLUDES the 2-byte compression
//!   marker and the count table: 2 + 2*rows + payload (both readers derive
//!   the payload by subtracting; the spec is silent).
//! - E3: layer-record `flags` bit 1 is read as HIDDEN by every real reader
//!   (the spec's "bit 1 = visible" wording is a known doc bug), so flags
//!   must be 0. The `filler` byte after it must be 0 too - Krita aborts the
//!   record on a nonzero filler.
//! - E4: the layer-info section length must be EVEN (pad a 0x00 inside).
//!
//! Nothing in these sections is time-dependent, so determinism is free.
//! Layers are written bottom-first (first record = bottom layer; resource
//! 1024's "0 = bottom layer" is the in-spec evidence, and both readers
//! expose file order as stack order). Reuses `kra::KraLayer` (straight
//! alpha RGBA) so the same stack feeds both writers.

use crate::kra::{composite_over_transparent, KraLayer};
use anyhow::{Context, Result};

/// Same payload type as the .kra writer: straight-alpha RGBA, bottom-first
/// order as given.
pub type PsdLayer = KraLayer;

const RLE: u16 = 1;
const MAX_PSD_SIDE: usize = 30_000;

/// Whole file as bytes. `paper` prepends an opaque "paper" coat layer and
/// is also the flatten ground; `None` flattens over white. Layers are
/// bottom-first, exactly as `write_kra` takes them.
pub fn build_psd(
    w: usize,
    h: usize,
    paper: Option<[u8; 3]>,
    layers: Vec<KraLayer>,
) -> Result<Vec<u8>> {
    anyhow::ensure!(
        !layers.is_empty(),
        "refusing to write a PSD with no layers"
    );
    anyhow::ensure!(
        w >= 1 && h >= 1 && w <= MAX_PSD_SIDE && h <= MAX_PSD_SIDE,
        "canvas {w}x{h} outside PSD v1 range 1..={MAX_PSD_SIDE} (Krita's v1 \
         reader caps at 30000; depth stays 8-bit for the same reason)"
    );
    for lay in &layers {
        anyhow::ensure!(
            !lay.name.is_empty() && lay.name.is_ascii() && lay.name.len() <= 255,
            "layer name {:?} must be non-empty ASCII <= 255 bytes (Pascal \
             name field; no luni is written)",
            lay.name
        );
    }

    let mut full: Vec<KraLayer> = Vec::with_capacity(layers.len() + 1);
    if let Some(c) = paper {
        full.push(KraLayer::paper("paper", c, w, h));
    }
    full.extend(layers);
    anyhow::ensure!(
        full.len() <= 32767,
        "too many layers for a PSD i16 count"
    );

    let mut buf: Vec<u8> = Vec::with_capacity(w * h * 4);

    // [1] file header. channels counts the MERGED image only (opaque RGB
    // over paper); the layers each carry their own alpha channel.
    buf.extend_from_slice(b"8BPS");
    buf.extend_from_slice(&1u16.to_be_bytes());
    buf.extend_from_slice(&[0u8; 6]);
    buf.extend_from_slice(&3u16.to_be_bytes());
    buf.extend_from_slice(&(h as u32).to_be_bytes());
    buf.extend_from_slice(&(w as u32).to_be_bytes());
    buf.extend_from_slice(&8u16.to_be_bytes());
    buf.extend_from_slice(&3i16.to_be_bytes());

    // [2] color mode data: empty for RGB
    buf.extend_from_slice(&0u32.to_be_bytes());

    // [3] image resources: one ResolutionInfo record (72 dpi), the same
    // record shape Photoshop itself writes
    buf.extend_from_slice(&28u32.to_be_bytes());
    buf.extend_from_slice(b"8BIM");
    buf.extend_from_slice(&0x03EDu16.to_be_bytes());
    buf.extend_from_slice(&[0u8; 2]); // Pascal "" = two zero bytes
    buf.extend_from_slice(&16u32.to_be_bytes());
    for v in [
        0x0048_0000u32, // 72.0 fixed 16.16
        1,              // px/inch
        1,              // inches
        0x0048_0000,
        1,
        1,
    ] {
        match v {
            0x0048_0000 => buf.extend_from_slice(&v.to_be_bytes()),
            _ => buf.extend_from_slice(&(v as u16).to_be_bytes()),
        }
    }

    // [4] layer and mask information section
    let section_len_pos = buf.len();
    buf.extend_from_slice(&0u32.to_be_bytes()); // patched below
    let layer_info_len_pos = buf.len();
    buf.extend_from_slice(&0u32.to_be_bytes()); // patched below
    buf.extend_from_slice(&(full.len() as i16).to_be_bytes());

    // pass 1: layer records with placeholder channel lengths
    let mut chan_len_pos: Vec<usize> = Vec::with_capacity(full.len() * 4);
    for lay in &full {
        for v in [0i32, 0, h as i32, w as i32] {
            buf.extend_from_slice(&v.to_be_bytes()); // top left bottom right
        }
        buf.extend_from_slice(&4u16.to_be_bytes());
        for id in [0i16, 1, 2, -1] {
            buf.extend_from_slice(&id.to_be_bytes());
            chan_len_pos.push(buf.len());
            buf.extend_from_slice(&0u32.to_be_bytes()); // patched in pass 2
        }
        buf.extend_from_slice(b"8BIM");
        buf.extend_from_slice(b"norm");
        buf.push(255); // opacity
        buf.push(0); // clipping
        buf.push(0); // flags: bit 1 would HIDE the layer (E3)
        buf.push(0); // filler: Krita aborts the record on nonzero
        let name_field = pascal_pad4(&lay.name);
        buf.extend_from_slice(&((4 + 4 + name_field.len()) as u32).to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes()); // mask data length
        buf.extend_from_slice(&0u32.to_be_bytes()); // blending ranges length
        buf.extend_from_slice(&name_field);
    }

    // pass 2: per-layer, per-channel RLE records (E1/E2)
    for (li, lay) in full.iter().enumerate() {
        for (ci, ch) in [0usize, 1, 2, 3].into_iter().enumerate() {
            let mut rows: Vec<Vec<u8>> = Vec::with_capacity(h);
            for y in 0..h {
                rows.push(pack_bits(&channel_row(&lay.rgba, y, w, ch)));
            }
            let len: usize = 2 + 2 * rows.len()
                + rows.iter().map(|r| r.len()).sum::<usize>();
            let pos = chan_len_pos[li * 4 + ci];
            buf[pos..pos + 4].copy_from_slice(&(len as u32).to_be_bytes());
            buf.extend_from_slice(&RLE.to_be_bytes());
            for r in &rows {
                buf.extend_from_slice(&(r.len() as u16).to_be_bytes());
            }
            for r in &rows {
                buf.extend_from_slice(r);
            }
        }
    }

    // E4: layer-info length must be even
    let body_start = layer_info_len_pos + 4;
    if (buf.len() - body_start) % 2 == 1 {
        buf.push(0);
    }
    let layer_info_len = (buf.len() - body_start) as u32;
    buf[layer_info_len_pos..layer_info_len_pos + 4]
        .copy_from_slice(&layer_info_len.to_be_bytes());

    // global layer mask info: the field must exist, zero length is legal
    buf.extend_from_slice(&0u32.to_be_bytes());
    let section_len = (buf.len() - section_len_pos - 4) as u32;
    buf[section_len_pos..section_len_pos + 4]
        .copy_from_slice(&section_len.to_be_bytes());

    // [5] merged image: opaque RGB flattened over the paper ground, ONE
    // count table for h*3 rows, planar R then G then B
    let ground = paper.unwrap_or([255, 255, 255]);
    let merged = flatten(&full, ground, w, h);
    buf.extend_from_slice(&RLE.to_be_bytes());
    let mut rows: Vec<Vec<u8>> = Vec::with_capacity(h * 3);
    for ch in 0..3 {
        for y in 0..h {
            let mut row = Vec::with_capacity(w);
            for x in 0..w {
                row.push(merged[(y * w + x) * 3 + ch]);
            }
            rows.push(pack_bits(&row));
        }
    }
    for r in &rows {
        buf.extend_from_slice(&(r.len() as u16).to_be_bytes());
    }
    for r in &rows {
        buf.extend_from_slice(r);
    }

    Ok(buf)
}

/// Write a layered PSD to disk.
pub fn write_psd(
    path: &str,
    w: usize,
    h: usize,
    paper: Option<[u8; 3]>,
    layers: Vec<KraLayer>,
) -> Result<()> {
    let buf = build_psd(w, h, paper, layers)?;
    std::fs::write(path, &buf).with_context(|| path.to_string())
}

/// Standard Apple PackBits for one scanline. Runs >= 3 become replicate
/// packets (a run of 2 costs the same as 2 literals, so Photoshop/libpng
/// emit literals - we match that to keep the golden bytes stable). Packets
/// cap at 128 bytes either way; the 0x80 no-op is never emitted.
fn pack_bits(row: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(row.len() + row.len() / 128 + 1);
    let n = row.len();
    let mut i = 0;
    while i < n {
        let b = row[i];
        let mut run = 1usize;
        while i + run < n && row[i + run] == b && run < 128 {
            run += 1;
        }
        if run >= 3 {
            out.push((1i32 - run as i32) as u8); // -(run-1)
            out.push(b);
            i += run;
            continue;
        }
        // literal: gather until a run of >= 3 starts or 128 bytes are full
        let lit_start = i;
        let mut j = i;
        while j < n {
            let mut r = 1usize;
            while j + r < n && row[j + r] == row[j] && r < 3 {
                r += 1;
            }
            if r >= 3 {
                break;
            }
            j += 1;
            if j - lit_start == 128 {
                break;
            }
        }
        out.push((j - lit_start - 1) as u8);
        out.extend_from_slice(&row[lit_start..j]);
        i = j;
    }
    out
}

/// One channel's scanline out of interleaved straight-alpha RGBA.
/// ch: 0=R 1=G 2=B 3=A.
fn channel_row(rgba: &[u8], y: usize, w: usize, ch: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(w);
    for x in 0..w {
        out.push(rgba[(y * w + x) * 4 + ch]);
    }
    out
}

/// Opaque RGB composite of bottom-first straight-alpha layers over
/// `ground` (the merged preview; the layers are the truth).
fn flatten(layers: &[KraLayer], ground: [u8; 3], w: usize, h: usize) -> Vec<u8> {
    let rgba = composite_over_transparent(layers, w, h);
    let mut out = vec![0u8; w * h * 3];
    for i in 0..w * h {
        let a = rgba[i * 4 + 3] as f32 / 255.0;
        for c in 0..3 {
            let s = rgba[i * 4 + c] as f32;
            out[i * 3 + c] = (s * a + ground[c] as f32 * (1.0 - a)).round() as u8;
        }
    }
    out
}

/// Pascal string with the length byte counted, whole field padded to a
/// multiple of 4: "L1" -> 04 4C 31 00, "paper" -> 8 bytes.
fn pascal_pad4(name: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(name.len() + 4);
    out.push(name.len() as u8);
    out.extend_from_slice(name.as_bytes());
    while out.len() % 4 != 0 {
        out.push(0);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test-only PackBits decoder (the reference vectors double as its
    /// spec, so a broken encoder+decoder pair can't pass both).
    fn unpack_bits(data: &[u8], out_len: usize) -> Vec<u8> {
        let mut out = Vec::with_capacity(out_len);
        let mut i = 0;
        while i < data.len() {
            let ctl = data[i] as i8;
            i += 1;
            if ctl >= 0 {
                let n = ctl as usize + 1;
                out.extend_from_slice(&data[i..i + n]);
                i += n;
            } else if ctl != -128 {
                let n = (1 - ctl as i32) as usize;
                out.extend(std::iter::repeat(data[i]).take(n));
                i += 1;
            }
        }
        assert_eq!(out.len(), out_len, "round-trip length mismatch");
        out
    }

    fn from_hex(s: &str) -> Vec<u8> {
        s.split_whitespace()
            .map(|b| u8::from_str_radix(b, 16).unwrap())
            .collect()
    }

    /// Adversarial fixed rows (no RNG): boundaries around both packet
    /// caps, embedded short runs, noise.
    fn adversarial_rows() -> Vec<Vec<u8>> {
        let mut rows: Vec<Vec<u8>> = vec![
            vec![],
            vec![9],
            vec![0xAA; 2],
            vec![0xAA; 3],
            vec![7; 127],
            vec![7; 128],
            vec![7; 129],
            vec![7; 130],
            vec![7; 256],
            (0..4096).map(|i| (i * 31 + i / 7) as u8).collect(),
        ];
        rows.push(vec![5, 5, 9, 9, 9]); // run of 2 inside literals
        rows.push((0..300).map(|i| if i % 2 == 0 { 3 } else { 4 }).collect());
        rows
    }

    #[test]
    fn pack_bits_roundtrip_property() {
        for row in adversarial_rows() {
            assert_eq!(
                unpack_bits(&pack_bits(&row), row.len()),
                row,
                "round-trip failed for len {}", row.len()
            );
        }
    }

    #[test]
    fn pack_bits_reference_vectors() {
        let cases: Vec<(Vec<u8>, Vec<u8>)> = vec![
            (vec![0xAA; 3], vec![0xFE, 0xAA]),
            (vec![1, 2, 3], vec![0x02, 1, 2, 3]),
            (vec![7; 128], vec![0x81, 7]),
            (vec![7; 129], vec![0x81, 7, 0x00, 7]),
            (vec![7; 130], vec![0x81, 7, 0x01, 7, 7]),
            (vec![5, 5], vec![0x01, 5, 5]),
            (vec![], vec![]),
            (vec![9], vec![0x00, 9]),
        ];
        for (input, want) in cases {
            assert_eq!(pack_bits(&input), want, "input len {}", input.len());
        }
    }

    #[test]
    fn pack_bits_never_emits_noop() {
        for row in adversarial_rows() {
            let out = pack_bits(&row);
            // 0x80 may legitimately appear as literal DATA; walk control
            // positions only and forbid it there.
            let mut i = 0;
            while i < out.len() {
                let ctl = out[i] as i8;
                assert_ne!(ctl, -128, "no-op control byte at {i} (input len {})",
                           row.len());
                i += if ctl >= 0 { ctl as usize + 2 } else { 2 };
            }
        }
    }

    /// The hand-verified 2x2 single-layer file: the whole spec anchor.
    /// Input L1 rgba (straight alpha):
    ///   px(0,0)=C8 64 32 FF   px(1,0)=transparent
    ///   px(0,1)=01 02 03 64   px(1,1)=FA FB FC 40
    /// (alphas deliberately avoid the .5 f32 rounding ties). Merged over
    /// white, planar rows (w bytes per channel plane, NOT interleaved):
    ///   R y0=[C8 FF] y1=[9B FE]  G y0=[64 FF] y1=[9C FE]  B y0=[32 FF] y1=[9C FE]
    /// (px(0,1) blend: 155.39/155.78/156.17 -> 9B 9C 9C; px(1,1):
    /// 253.7/254.0/254.2 -> FE FE FE; px(1,0) transparent -> white).
    #[test]
    fn golden_2x2_single_layer() {
        let mut rgba = vec![0u8; 16];
        rgba[0..4].copy_from_slice(&[0xC8, 0x64, 0x32, 0xFF]);
        rgba[8..12].copy_from_slice(&[0x01, 0x02, 0x03, 0x64]);
        rgba[12..16].copy_from_slice(&[0xFA, 0xFB, 0xFC, 0x40]);
        let out = build_psd(2, 2, None, vec![KraLayer {
            name: "L1".into(),
            rgba,
        }])
        .unwrap();

        const GOLDEN: &str = "
            38 42 50 53 00 01 00 00 00 00 00 00 00 03
            00 00 00 02 00 00 00 02 00 08 00 03
            00 00 00 00
            00 00 00 1C
            38 42 49 4D 03 ED 00 00 00 00 00 10
            00 48 00 00 00 01 00 01 00 48 00 00 00 01 00 01
            00 00 00 80
            00 00 00 78
            00 01
            00 00 00 00 00 00 00 00 00 00 00 02 00 00 00 02
            00 04
            00 00 00 00 00 0C
            00 01 00 00 00 0C
            00 02 00 00 00 0C
            FF FF 00 00 00 0C
            38 42 49 4D 6E 6F 72 6D FF 00 00 00
            00 00 00 0C
            00 00 00 00 00 00 00 00
            02 4C 31 00
            00 01 00 03 00 03 01 C8 00 01 01 FA
            00 01 00 03 00 03 01 64 00 01 02 FB
            00 01 00 03 00 03 01 32 00 01 03 FC
            00 01 00 03 00 03 01 FF 00 01 64 40
            00 00 00 00
            00 01
            00 03 00 03 00 03 00 03 00 03 00 03
            01 C8 FF 01 9B FE
            01 64 FF 01 9C FE
            01 32 FF 01 9C FE
        ";
        let want = from_hex(GOLDEN);
        assert_eq!(out.len(), 226, "file size");
        // section anchors first, so a drift fails with a useful message
        assert_eq!(&out[26..30], &want[26..30], "color mode len @26");
        assert_eq!(&out[30..34], &want[30..34], "resources len @30 (= 28)");
        assert_eq!(&out[62..66], &want[62..66], "L&M section len @62 (= 128)");
        assert_eq!(&out[194..196], &want[194..196], "merged compression @194");
        assert_eq!(out, want, "golden bytes");
    }

    #[test]
    fn deterministic_two_calls_identical() {
        let mk = || {
            vec![
                KraLayer { name: "F1".into(), rgba: vec![10, 20, 30, 255, 0, 0, 0, 0, 40, 50, 60, 128, 1, 2, 3, 64].repeat(16) },
                KraLayer { name: "L26".into(), rgba: vec![0, 0, 0, 0, 200, 210, 220, 255, 5, 6, 7, 32, 90, 91, 92, 200].repeat(16) },
                KraLayer { name: "X1C".into(), rgba: vec![255u8, 0, 0, 128].repeat(16) },
            ]
        };
        let a = build_psd(4, 4, Some([250, 249, 248]), mk()).unwrap();
        let b = build_psd(4, 4, Some([250, 249, 248]), mk()).unwrap();
        assert_eq!(a, b);
    }

    /// First record = bottom layer, same order in records and in channel
    /// data. Distinct alpha per layer makes each layer's A-channel
    /// replicate packet searchable (3px rows = run of 3 -> [FE, a]).
    #[test]
    fn multi_layer_order_bottom_first() {
        let mut layers = Vec::new();
        for (name, a) in [("bottom", 100u8), ("mid", 150), ("top", 200)] {
            layers.push(KraLayer {
                name: name.into(),
                rgba: vec![1, 2, 3, a].repeat(9),
            });
        }
        let out = build_psd(3, 3, None, layers).unwrap();

        let pos_name = |n: &str| -> usize {
            let field = pascal_pad4(n);
            out.windows(field.len())
                .position(|w| w == field)
                .unwrap_or_else(|| panic!("name {n} not found"))
        };
        let pb = pos_name("bottom");
        let pm = pos_name("mid");
        let pt = pos_name("top");
        assert!(pb < pm && pm < pt, "record order not bottom-first");

        let pos_a_row = |a: u8| -> usize {
            let pkt = [0xFE, a]; // replicate packet: 3px row of `a` = run of 3
            out.windows(2)
                .skip(pt + pascal_pad4("top").len()) // channel-data region
                .position(|w| w == pkt)
                .map(|p| p + pt + pascal_pad4("top").len())
                .unwrap()
        };
        assert!(pos_a_row(100) < pos_a_row(150) && pos_a_row(150) < pos_a_row(200),
                "channel data order not bottom-first");
    }

    /// Test-local structure walker: parses the fixed sections back out of
    /// a built file so later tests can assert on real offsets instead of
    /// re-deriving arithmetic. Returns per-record channel data lengths.
    fn walk(buf: &[u8]) -> (usize, Vec<Vec<u32>>, usize) {
        let u16at = |p: usize| u16::from_be_bytes(buf[p..p + 2].try_into().unwrap());
        let u32at = |p: usize| u32::from_be_bytes(buf[p..p + 4].try_into().unwrap());
        let i16at = |p: usize| i16::from_be_bytes(buf[p..p + 2].try_into().unwrap());
        let i32at = |p: usize| i32::from_be_bytes(buf[p..p + 4].try_into().unwrap());

        assert_eq!(&buf[0..4], b"8BPS");
        let p = 26; // color mode data
        assert_eq!(u32at(p), 0);
        let p = p + 4;
        let res_len = u32at(p) as usize;
        let p = p + 4 + res_len; // layer & mask section
        let section_len = u32at(p) as usize;
        let section_start = p + 4;
        let li_len_pos = section_start;
        let li_len = u32at(li_len_pos) as usize;
        assert_eq!(li_len % 2, 0, "layer info length must be even (E4)");
        let mut p = li_len_pos + 4;
        let li_body = p;
        let n = i16at(p);
        assert!(n > 0, "layer count must be positive");
        let n = n as usize;
        p += 2;
        let mut chan_lens: Vec<Vec<u32>> = Vec::new();
        for _ in 0..n {
            let _rect = (i32at(p), i32at(p + 4), i32at(p + 8), i32at(p + 12));
            p += 16;
            let nch = u16at(p) as usize;
            p += 2;
            let mut lens = Vec::with_capacity(nch);
            for _ in 0..nch {
                let _id = i16at(p);
                lens.push(u32at(p + 2));
                p += 6;
            }
            p += 4 + 4 + 4; // 8BIM + blend + opacity/clipping/flags/filler
            let extra = u32at(p) as usize;
            p += 4 + extra;
            chan_lens.push(lens);
        }
        // channel data: skip via declared lengths
        for lens in &chan_lens {
            for l in lens {
                p += *l as usize;
            }
        }
        assert_eq!(p - li_body, li_len, "walked layer info != declared len");
        assert_eq!(u32at(p), 0, "global layer mask info length");
        p += 4;
        // section = 4 (li len field) + li_len + 4 (global mask len field)
        assert_eq!(section_len, li_len + 8, "section length accounting");
        (n, chan_lens, p) // p = merged image data start
    }

    #[test]
    fn header_and_positive_layer_count() {
        let layers = vec![
            KraLayer { name: "F1".into(), rgba: vec![1u8, 2, 3, 255].repeat(9) },
            KraLayer { name: "X1".into(), rgba: vec![9u8, 8, 7, 128].repeat(9) },
        ];
        let out = build_psd(3, 3, Some([1, 2, 3]), layers).unwrap();
        let (n, _, _) = walk(&out);
        assert_eq!(n, 3); // 2 layers + paper coat (Some(paper) prepends one)
        assert_eq!(&out[26..30], &[0, 0, 0, 0]);
        assert_eq!(u16::from_be_bytes(out[12..14].try_into().unwrap()), 3);
        assert_eq!(
            u32::from_be_bytes(out[14..18].try_into().unwrap()),
            3 // height
        );
        assert_eq!(
            u32::from_be_bytes(out[18..22].try_into().unwrap()),
            3 // width
        );
        assert_eq!(u16::from_be_bytes(out[22..24].try_into().unwrap()), 8);
        assert_eq!(i16::from_be_bytes(out[24..26].try_into().unwrap()), 3);
    }

    /// E2: the declared channel length includes the compression marker
    /// and the count table - verify against actual payload arithmetic.
    #[test]
    fn channel_length_includes_header() {
        let out = build_psd(2, 2, None, vec![KraLayer {
            name: "L1".into(),
            rgba: vec![200, 100, 50, 255, 0, 0, 0, 0, 1, 2, 3, 100, 250, 251, 252, 64],
        }])
        .unwrap();
        // golden offsets: records start after count @70; channel info at
        // record+18; first length at record+20
        let len = u32::from_be_bytes(out[92..96].try_into().unwrap());
        assert_eq!(len, 12, "len must be 2 + 2*rows + payload = 2+4+6");
    }

    #[test]
    fn merged_counts_rows_times_channels() {
        // one count table for h*3 rows in the merged section
        let layers = vec![KraLayer { name: "A1".into(), rgba: vec![1u8, 2, 3, 255].repeat(12) }];
        let out = build_psd(3, 2, None, layers).unwrap();
        let (_, _, img_start) = walk(&out);
        assert_eq!(
            u16::from_be_bytes(out[img_start..img_start + 2].try_into().unwrap()),
            1,
            "merged compression"
        );
        let table = img_start + 2;
        for k in 0..(2 * 3) {
            let cnt = u16::from_be_bytes(out[table + k * 2..table + k * 2 + 2]
                .try_into()
                .unwrap());
            assert!(cnt >= 1, "merged count {k} must cover a 3px row");
        }
        // payload starts after the table and must total the counted bytes
        let total: usize = (0..6)
            .map(|k| {
                u16::from_be_bytes(out[table + k * 2..table + k * 2 + 2]
                    .try_into()
                    .unwrap()) as usize
            })
            .sum();
        let payload = out.len() - (table + 12);
        assert_eq!(total, payload, "merged count table must match payload");
    }

    #[test]
    fn pascal_name_padding_multiple_of_four() {
        for n in ["F1", "L52", "X1C", "U30", "UT", "U_layer", "paper", "L1"] {
            let f = pascal_pad4(n);
            assert_eq!(f.len() % 4, 0, "name {n}");
            assert_eq!(f[0] as usize, n.len(), "name {n} length byte");
        }
        assert_eq!(pascal_pad4("L1"), vec![2, b'L', b'1', 0]);
        assert_eq!(pascal_pad4("paper").len(), 8);
        assert_eq!(pascal_pad4("U_layer").len(), 8);
    }

    #[test]
    fn rejects_empty_stack() {
        assert!(build_psd(4, 4, None, vec![]).is_err());
    }

    #[test]
    fn rejects_non_ascii_name() {
        assert!(build_psd(
            4,
            4,
            None,
            vec![KraLayer { name: "图层1".into(), rgba: vec![0; 64] }]
        )
        .is_err());
    }

    #[test]
    fn rejects_oversized_canvas() {
        let lay = KraLayer { name: "L1".into(), rgba: vec![0; 4] };
        assert!(build_psd(30001, 1, None, vec![lay]).is_err());
        let lay = KraLayer { name: "L1".into(), rgba: vec![0; 4] };
        assert!(build_psd(1, 0, None, vec![lay]).is_err());
    }
}
