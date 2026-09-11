//! .kra writer: writes rendered layers as a Krita-loadable document.
//!
//! Format reverse-engineered from a real Krita 5.3.3 save and verified by
//! byte-exact round-trip (kra_probe: BGRA hypothesis premult diff = 0.00
//! over 4M pixels against Krita's own mergedimage.png):
//!
//! - zip: `mimetype` FIRST entry, STORED, "application/x-krita"
//! - `maindoc.xml`: `<layer>` tags in TOP-first stack order
//! - `<DocName>/layers/layerN`: tiled stream - header lines
//!   `VERSION 2 / TILEWIDTH 64 / TILEHEIGHT 64 / PIXELSIZE 4 / DATA <n>`,
//!   then per tile `x,y,LZF,csize\n` followed by a one-byte flag
//!   (1 = LZF stream, 0 = raw) and the CHANNEL-PLANARIZED tile
//!   (Krita linearizeColors: four 64x64 planes in B, G, R, A order);
//!   csize counts flag + payload
//! - `layerN.defaultpixel` = 4 zero bytes, `layerN.icc` + `annotations/icc`
//!   = the sRGB profile
//!
//! Krita's in-tree LZF ("lzff") is format-compatible with liblzf, so the
//! `lzf-rust` crate's encoder output decodes fine in Krita.

use crate::render::LayerBuf;
use anyhow::{Context, Result};
use std::io::Write;

const SRGB_ICC: &[u8] = include_bytes!("krita_srgb.icc");
const TILE: usize = 64;
const TILE_BYTES: usize = TILE * TILE * 4;

/// A layer ready for writing: straight-alpha RGBA, w*h*4, bottom-first
/// order as given.
pub struct KraLayer {
    pub name: String,
    pub rgba: Vec<u8>,
}

impl KraLayer {
    /// Convert a rendered daub layer buffer (RGB accumulated, A = max dab
    /// alpha - i.e. premultiplied colour) into straight-alpha RGBA so the
    /// layer looks in Krita exactly like it does in daub's own composite.
    pub fn from_layer_buf(name: &str, buf: &LayerBuf) -> KraLayer {
        let mut rgba = buf.rgba.clone();
        for px in rgba.chunks_exact_mut(4) {
            let a = px[3];
            if a > 0 && a < 255 {
                let af = a as f32 / 255.0;
                for c in 0..3 {
                    px[c] = ((px[c] as f32 / af).round().min(255.0)) as u8;
                }
            }
        }
        KraLayer { name: name.to_string(), rgba }
    }

    /// Solid paper coat (bottom layer for documents that rely on the
    /// renderer's ground colour).
    pub fn paper(name: &str, color: [u8; 3], w: usize, h: usize) -> KraLayer {
        let mut rgba = vec![0u8; w * h * 4];
        for px in rgba.chunks_exact_mut(4) {
            px[..3].copy_from_slice(&color);
            px[3] = 255;
        }
        KraLayer { name: name.to_string(), rgba }
    }
}

/// RGBA-interleaved tile at (tx,ty) -> four BGRA channel planes,
/// zero-filled outside the canvas (Krita tiles are always full 64x64).
fn planarize_tile(rgba: &[u8], w: usize, h: usize, tx: usize, ty: usize) -> Vec<u8> {
    let np = TILE * TILE;
    let mut planes = vec![0u8; TILE_BYTES];
    for ry in 0..TILE {
        let y = ty + ry;
        if y >= h {
            break;
        }
        for rx in 0..TILE {
            let x = tx + rx;
            if x >= w {
                break;
            }
            let si = (y * w + x) * 4;
            let di = ry * TILE + rx;
            planes[di] = rgba[si + 2]; // B
            planes[np + di] = rgba[si + 1]; // G
            planes[2 * np + di] = rgba[si]; // R
            planes[3 * np + di] = rgba[si + 3]; // A
        }
    }
    planes
}

/// Full tiled stream for one layer.
fn layer_stream(rgba: &[u8], w: usize, h: usize) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    let mut n = 0usize;
    let mut comp = vec![0u8; lzf_rust::max_compressed_size(TILE_BYTES)];
    for ty in (0..h).step_by(TILE) {
        for tx in (0..w).step_by(TILE) {
            let planar = planarize_tile(rgba, w, h, tx, ty);
            let clen = lzf_rust::compress(&planar, &mut comp)
                .context("lzf compress tile")?;
            if (clen as usize) < TILE_BYTES {
                write!(body, "{tx},{ty},LZF,{}\n", clen + 1)?;
                body.push(1); // COMPRESSED_DATA_FLAG
                body.extend_from_slice(&comp[..clen]);
            } else {
                write!(body, "{tx},{ty},LZF,{}\n", TILE_BYTES + 1)?;
                body.push(0); // RAW_DATA_FLAG
                body.extend_from_slice(&planar);
            }
            n += 1;
        }
    }
    let mut out = format!(
        "VERSION 2\nTILEWIDTH {TILE}\nTILEHEIGHT {TILE}\nPIXELSIZE 4\nDATA {n}\n"
    )
    .into_bytes();
    out.extend_from_slice(&body);
    Ok(out)
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn layer_xml(idx: usize, name: &str, selected: bool) -> String {
    let sel = if selected { "true" } else { "false" };
    let uuid = format!("{{00000000-0000-0000-0000-{idx:012}}}");
    format!(
        "   <layer onionskin=\"0\" intimeline=\"0\" channelflags=\"\" \
channellockflags=\"1111\" locked=\"0\" filename=\"layer{idx}\" \
colorspacename=\"RGBA\" name=\"{}\" opacity=\"255\" collapsed=\"0\" \
visible=\"1\" colorlabel=\"0\" selected=\"{sel}\" uuid=\"{uuid}\" x=\"0\" \
nodetype=\"paintlayer\" compositeop=\"normal\" y=\"0\"/>\n",
        xml_escape(name)
    )
}

/// Alpha-over composite of straight-alpha layers (bottom-first) over
/// transparent, returned as straight-alpha RGBA.
pub fn composite_over_transparent(layers: &[KraLayer], w: usize, h: usize) -> Vec<u8> {
    let mut acc = vec![0u8; w * h * 4];
    for lay in layers {
        for i in 0..w * h {
            let sa = lay.rgba[i * 4 + 3] as f32 / 255.0;
            if sa <= 0.0 {
                continue;
            }
            let da = acc[i * 4 + 3] as f32 / 255.0;
            let oa = sa + da * (1.0 - sa);
            if oa <= 0.0 {
                continue;
            }
            for c in 0..3 {
                let s = lay.rgba[i * 4 + c] as f32;
                let d = acc[i * 4 + c] as f32;
                acc[i * 4 + c] = ((s * sa + d * da * (1.0 - sa)) / oa).round() as u8;
            }
            acc[i * 4 + 3] = (oa * 255.0).round() as u8;
        }
    }
    acc
}

fn encode_png_rgba(rgba: &[u8], w: usize, h: usize) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut enc = png::Encoder::new(&mut out, w as u32, h as u32);
    enc.set_color(png::ColorType::Rgba);
    enc.set_depth(png::BitDepth::Eight);
    let mut wr = enc.write_header()?;
    wr.write_image_data(rgba)?;
    wr.finish()?;
    Ok(out)
}

/// Nearest-neighbour downscale for the 256x256 preview.png.
fn shrink_rgba(rgba: &[u8], w: usize, h: usize, tw: usize, th: usize) -> Vec<u8> {
    let mut out = vec![0u8; tw * th * 4];
    for ty in 0..th {
        let sy = ty * h / th;
        for tx in 0..tw {
            let sx = tx * w / tw;
            let di = (ty * tw + tx) * 4;
            let si = (sy * w + sx) * 4;
            out[di..di + 4].copy_from_slice(&rgba[si..si + 4]);
        }
    }
    out
}

/// Write a Krita-loadable .kra. `layers` is bottom-first; a paper coat is
/// prepended if `paper` is Some. Also writes mergedimage.png (full-size
/// composite) and preview.png (256x256).
pub fn write_kra(
    path: &str,
    doc_name: &str,
    w: usize,
    h: usize,
    paper: Option<[u8; 3]>,
    layers: Vec<KraLayer>,
) -> Result<()> {
    let bytes = build_kra(doc_name, w, h, paper, layers)?;
    std::fs::write(path, bytes).with_context(|| path.to_string())?;
    Ok(())
}

/// Whole .kra as bytes - the wasm surface hands this straight to a Blob
/// download. Byte-identical to write_kra: same zip code path, only the
/// sink differs (memory instead of file).
pub fn build_kra(
    doc_name: &str,
    w: usize,
    h: usize,
    paper: Option<[u8; 3]>,
    layers: Vec<KraLayer>,
) -> Result<Vec<u8>> {
    let mut full: Vec<KraLayer> = Vec::new();
    if let Some(c) = paper {
        full.push(KraLayer::paper("paper", c, w, h));
    }
    full.extend(layers);
    anyhow::ensure!(!full.is_empty(), "refusing to write a document with no layers");

    let merged = composite_over_transparent(&full, w, h);

    let buf = std::io::Cursor::new(Vec::with_capacity(1 << 20));
    let mut zw = zip::ZipWriter::new(buf);
    // create_system（version-made-by 高字节）默认取编译目标系统：wasm32
    // 写 Unix(3)、Windows 写 FAT(0)、Linux 写 Unix——内容全同而容器字节
    // 不通。钉死一个值，native/Windows/Linux/wasm 四方逐字节同源。
    let stored = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Stored)
        .system(zip::System::Unix);
    let deflated = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .system(zip::System::Unix);

    // mimetype MUST be the first entry, stored
    zw.start_file("mimetype", stored)?;
    zw.write_all(b"application/x-krita")?;

    // maindoc.xml - layers TOP-first = reverse of `full`
    let mut maindoc = String::new();
    maindoc.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    maindoc.push_str("<!DOCTYPE DOC PUBLIC '-//KDE//DTD krita 2.0//EN' \
'http://www.calligra.org/DTD/krita-2.0.dtd'>\n");
    maindoc.push_str(&format!(
        "<DOC xmlns=\"http://www.calligra.org/DTD/krita\" syntaxVersion=\"2.0\" \
editor=\"Krita\" kritaVersion=\"5.3.3\">\n"
    ));
    maindoc.push_str(&format!(
        " <IMAGE name=\"{}\" height=\"{h}\" colorspacename=\"RGBA\" y-res=\"120\" \
width=\"{w}\" x-res=\"120\" mime=\"application/x-kra\" description=\"\" \
profile=\"sRGB-elle-V2-srgbtrc.icc\">\n",
        xml_escape(doc_name)
    ));
    maindoc.push_str("  <layers>\n");
    for (i, li) in full.iter().enumerate().rev() {
        maindoc.push_str(&layer_xml(i + 2, &li.name, i + 1 == full.len()));
    }
    maindoc.push_str("  </layers>\n </IMAGE>\n</DOC>\n");
    zw.start_file("maindoc.xml", deflated)?;
    zw.write_all(maindoc.as_bytes())?;

    // documentinfo.xml mirrors what Krita 5.3.3 writes. The earlier
    // theory that Krita "rejects minimal docinfo" was a harness
    // artefact: the bridge's open() returned a viewless document that
    // Krita collected immediately, so every clean file looked rejected
    // while files that tripped load warnings stayed alive. Dates must
    // still be non-empty (KoDocumentInfo QDateTime parse), and they are
    // fixed rather than wall-clock to keep output deterministic.
    let mut docinfo = String::new();
    docinfo.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    docinfo.push_str("<!DOCTYPE document-info PUBLIC '-//KDE//DTD document-info 1.1//EN' \
'http://www.calligra.org/DTD/document-info-1.1.dtd'>\n");
    docinfo.push_str("<document-info xmlns=\"http://www.calligra.org/DTD/document-info\">\n \
 <about>\n");
    docinfo.push_str(&format!("  <title>{}</title>\n", xml_escape(doc_name)));
    docinfo.push_str("  <description></description>\n  <subject></subject>\n");
    docinfo.push_str("  <abstract><![CDATA[]]></abstract>\n  <keyword></keyword>\n");
    docinfo.push_str("  <initial-creator>daub</initial-creator>\n");
    docinfo.push_str("  <editing-cycles>1</editing-cycles>\n");
    docinfo.push_str("  <editing-time>0</editing-time>\n");
    // fixed (not wall-clock) so output stays deterministic; empty dates
    // fail KoDocumentInfo's QDateTime parse and abort the load
    docinfo.push_str("  <date>2026-01-01T00:00:00</date>\n");
    docinfo.push_str("  <creation-date>2026-01-01T00:00:00</creation-date>\n");
    docinfo.push_str("  <language></language>\n  <license></license>\n </about>\n");
    docinfo.push_str(" <author>\n  <full-name></full-name>\n");
    docinfo.push_str("  <creator-first-name></creator-first-name>\n");
    docinfo.push_str("  <creator-last-name></creator-last-name>\n");
    docinfo.push_str("  <initial></initial>\n  <author-title></author-title>\n");
    docinfo.push_str("  <position></position>\n  <company></company>\n </author>\n");
    docinfo.push_str("</document-info>\n");
    zw.start_file("documentinfo.xml", deflated)?;
    zw.write_all(docinfo.as_bytes())?;

    zw.start_file("preview.png", deflated)?;
    let prev = shrink_rgba(&merged, w, h, 256, 256);
    zw.write_all(&encode_png_rgba(&prev, 256, 256)?)?;

    zw.start_file("mergedimage.png", deflated)?;
    zw.write_all(&encode_png_rgba(&merged, w, h)?)?;

    let icc_path = format!("{doc_name}/annotations/icc");
    zw.start_file(icc_path.as_str(), deflated)?;
    zw.write_all(SRGB_ICC)?;

    for (i, li) in full.iter().enumerate() {
        let fname = format!("layer{}", i + 2);
        let base = format!("{doc_name}/layers/{fname}");
        zw.start_file(base.as_str(), deflated)?;
        zw.write_all(&layer_stream(&li.rgba, w, h)?)?;
        zw.start_file(format!("{base}.defaultpixel").as_str(), deflated)?;
        zw.write_all(&[0u8; 4])?;
        zw.start_file(format!("{base}.icc").as_str(), deflated)?;
        zw.write_all(SRGB_ICC)?;
    }

    let out = zw.finish()?;
    Ok(out.into_inner())
}
