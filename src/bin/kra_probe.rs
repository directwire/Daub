//! Validation probe for the P2 .kra writer: decodes a real Krita .kra's
//! tiled layer data (LZF) and checks it against the exported merged image.
//! Answers the byte-layout question (RGBA vs BGRA vs planar) empirically.
//!
//! usage:
//!   kra_probe <file.kra>                        list layers
//!   kra_probe <file.kra> --layer layer2 [--out l.png]
//!   kra_probe <file.kra> --merged ref.png       test layout hypotheses

use anyhow::{bail, Context, Result};
use std::io::{BufReader, Read};

/// liblzf decode, semantics verified 1:1 against lzf-rust 0.1.0 raw::decoder.
/// Returns (bytes consumed, decoded buffer truncated to the written length).
fn lzf_decompress(input: &[u8], expected: usize) -> Result<(usize, Vec<u8>)> {
    let mut out = vec![0u8; expected];
    let mut ip = 0usize;
    let mut op = 0usize;
    while ip < input.len() {
        let ctrl = input[ip];
        ip += 1;
        if ctrl < 32 {
            let len = ctrl as usize + 1;
            if ip + len > input.len() || op + len > out.len() {
                bail!("literal overrun at ip={} op={}", ip, op);
            }
            out[op..op + len].copy_from_slice(&input[ip..ip + len]);
            ip += len;
            op += len;
            continue;
        }
        let mut len = (ctrl >> 5) as usize;
        let off_hi = ((ctrl & 0x1f) as usize) << 8;
        if len == 7 {
            if ip >= input.len() {
                bail!("truncated len extension");
            }
            len += input[ip] as usize;
            ip += 1;
        }
        if ip >= input.len() {
            bail!("truncated offset");
        }
        let off = off_hi | input[ip] as usize;
        ip += 1;
        let copy_len = len + 2;
        if off >= op {
            bail!("bad backref off={} op={}", off, op);
        }
        if op + copy_len > out.len() {
            bail!("match overrun: op={} +{} > {}", op, copy_len, out.len());
        }
        let mut r = op - off - 1;
        for _ in 0..copy_len {
            out[op] = out[r];
            op += 1;
            r += 1;
        }
    }
    out.truncate(op);
    Ok((ip, out))
}

struct Tiled {
    tw: usize,
    th: usize,
    tiles: Vec<(i64, i64, Vec<u8>, Vec<u8>)>, // x, y, compressed, decoded
}

fn parse_tiled(data: &[u8]) -> Result<Tiled> {
    let mut pos = 0usize;
    let (mut tw, mut th, mut ps) = (0usize, 0usize, 0usize);
    loop {
        let end = data[pos..]
            .iter()
            .position(|&b| b == b'\n')
            .context("unterminated header line")?
            + pos;
        let line = std::str::from_utf8(&data[pos..end])?;
        pos = end + 1;
        if let Some(rest) = line.strip_prefix("DATA ") {
            let _n: usize = rest.trim().parse().context("DATA count")?;
            break;
        } else if let Some(rest) = line.strip_prefix("TILEWIDTH ") {
            tw = rest.trim().parse()?;
        } else if let Some(rest) = line.strip_prefix("TILEHEIGHT ") {
            th = rest.trim().parse()?;
        } else if let Some(rest) = line.strip_prefix("PIXELSIZE ") {
            ps = rest.trim().parse()?;
        }
    }
    anyhow::ensure!(tw == 64 && th == 64 && ps == 4,
                    "unexpected tile geometry {tw}x{th} ps={ps}");
    let mut tiles = Vec::new();
    let mut anomalies = 0usize;
    while pos < data.len() {
        let end = match data[pos..].iter().position(|&b| b == b'\n') {
            Some(p) => p + pos,
            None => break,
        };
        let line = std::str::from_utf8(&data[pos..end])?;
        pos = end + 1;
        if line.trim().is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.split(',').collect();
        if parts.len() != 4 {
            bail!("bad tile record {line:?}");
        }
        let x: i64 = parts[0].trim().parse()?;
        let y: i64 = parts[1].trim().parse()?;
        let codec = parts[2];
        let csize: usize = parts[3].trim().parse()?;
        if codec != "LZF" || csize == 0 {
            println!("ODD RECORD {line:?} payload[0..8]={:02x?}",
                     &data[pos..(pos + 8).min(data.len())]);
        }
        if pos + csize > data.len() {
            bail!("tile {x},{y}: csize {csize} overruns stream");
        }
        // (token tracing happens on decode failure below)
        let payload = &data[pos..pos + csize];
        pos += csize;
        let want = tw * th * ps;
        // Krita tile payload = [flag][data]: flag 1 = LZF stream of the
        // channel-PLANARIZED tile, flag 0 = raw planarized data.
        let body = match payload[0] {
            1 => {
                let (consumed, raw) = lzf_decompress(&payload[1..], want)
                    .with_context(|| format!("tile {x},{y} csize={csize}"))?;
                if consumed != csize - 1 {
                    bail!("tile {x},{y}: consumed {consumed}/{csize} - desync");
                }
                raw
            }
            0 => {
                if csize != want + 1 {
                    bail!("tile {x},{y}: RAW flag but csize={csize}");
                }
                payload[1..].to_vec()
            }
            f => bail!("tile {x},{y}: unknown flag {f}"),
        };
        if body.len() != want {
            anomalies += 1;
            println!("NOTE tile {x},{y}: decoded {} bytes (expected {want}) - truncating",
                     body.len());
            let mut b = body;
            b.truncate(want);
            b.resize(want, 0);
            tiles.push((x, y, payload.to_vec(), b));
            continue;
        }
        tiles.push((x, y, payload.to_vec(), body));
    }
    if anomalies > 5 {
        println!("  ... {anomalies} oversized tiles total (truncated)");
    }
    Ok(Tiled { tw, th, tiles })
}

/// Assemble tiles into a w*h*4 RGBA canvas (row-major, straight alpha).
/// Tile bodies are channel-PLANAR (Krita linearizeColors); `swap`
/// selects RGBA vs BGRA plane order.
fn assemble(t: &Tiled, w: usize, h: usize, swap: bool) -> Result<Vec<u8>> {
    let mut canvas = vec![0u8; w * h * 4];
    for (tx, ty, _payload, raw) in &t.tiles {
        let np = t.tw * t.th;
        let mut tile = vec![0u8; np * 4];
        for i in 0..np {
            if swap {
                tile[i * 4] = raw[2 * np + i];
                tile[i * 4 + 1] = raw[np + i];
                tile[i * 4 + 2] = raw[i];
            } else {
                tile[i * 4] = raw[i];
                tile[i * 4 + 1] = raw[np + i];
                tile[i * 4 + 2] = raw[2 * np + i];
            }
            tile[i * 4 + 3] = raw[3 * np + i];
        }
        for ry in 0..t.th {
            let y = *ty as usize + ry;
            if y >= h {
                break;
            }
            for rx in 0..t.tw {
                let x = *tx as usize + rx;
                if x >= w {
                    break;
                }
                let dst = (y * w + x) * 4;
                let src = (ry * t.tw + rx) * 4;
                canvas[dst..dst + 4].copy_from_slice(&tile[src..src + 4]);
            }
        }
    }
    Ok(canvas)
}

// ---------- minimal maindoc.xml layer table ----------

struct LayerInfo {
    filename: String,
    name: String,
    visible: bool,
    opacity: f64,
}

fn attr<'a>(tag: &'a str, key: &str) -> Option<&'a str> {
    // leading space so "filename=" does not match key "name"
    let pat = format!(" {key}=\"");
    let i = tag.find(&pat)? + pat.len();
    let j = tag[i..].find('"')? + i;
    Some(&tag[i..j])
}

fn parse_layers(xml: &str) -> Result<Vec<LayerInfo>> {
    if xml.contains("<layers ") {
        bail!("group layers present - not supported by probe");
    }
    let mut out = Vec::new();
    let mut rest = xml;
    while let Some(i) = rest.find("<layer ") {
        let end = rest[i..].find('>').context("layer tag unterminated")? + i;
        let tag = &rest[i..end];
        out.push(LayerInfo {
            filename: attr(tag, "filename").unwrap_or_default().to_string(),
            name: attr(tag, "name").unwrap_or_default().to_string(),
            visible: attr(tag, "visible") != Some("0"),
            opacity: attr(tag, "opacity")
                .and_then(|s| s.parse::<f64>().ok())
                .unwrap_or(255.0)
                / 255.0,
        });
        rest = &rest[end..];
    }
    Ok(out)
}

// ---------- png io ----------

fn read_png_rgba(path: &str) -> Result<(Vec<u8>, usize, usize)> {
    let f = std::fs::File::open(path)?;
    let dec = png::Decoder::new(BufReader::new(f));
    let mut rdr = dec.read_info()?;
    let (w, h) = (rdr.info().width as usize, rdr.info().height as usize);
    let mut buf = vec![0u8; rdr.output_buffer_size()];
    let info = rdr.next_frame(&mut buf)?;
    let mut rgba = vec![0u8; w * h * 4];
    match info.color_type {
        png::ColorType::Rgba => rgba.copy_from_slice(&buf[..w * h * 4]),
        png::ColorType::Rgb => {
            for i in 0..w * h {
                rgba[i * 4..i * 4 + 3].copy_from_slice(&buf[i * 3..i * 3 + 3]);
                rgba[i * 4 + 3] = 255;
            }
        }
        ct => bail!("unsupported color type {ct:?} in {path}"),
    }
    Ok((rgba, w, h))
}

fn write_png_rgba(path: &str, rgba: &[u8], w: usize, h: usize) -> Result<()> {
    let f = std::fs::File::create(path)?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(f), w as u32, h as u32);
    enc.set_color(png::ColorType::Rgba);
    enc.set_depth(png::BitDepth::Eight);
    let mut wr = enc.write_header()?;
    wr.write_image_data(rgba)?;
    wr.finish()?;
    Ok(())
}

// ---------- comparison ----------

fn diff_stats(a: &[u8], b: &[u8]) -> (f64, f64, usize) {
    // premultiplied mean abs diff over RGB, and straight-rgb diff where
    // both alphas are >=250; returns (pm_mean, opaque_mean, n_opaque)
    let n = a.len() / 4;
    let mut pm = 0f64;
    let mut op = 0f64;
    let mut nop = 0usize;
    for i in 0..n {
        let (ar, ag, ab, aa) = (a[i * 4], a[i * 4 + 1], a[i * 4 + 2], a[i * 4 + 3]);
        let (br, bg, bb, ba) = (b[i * 4], b[i * 4 + 1], b[i * 4 + 2], b[i * 4 + 3]);
        let (fa, fb) = (aa as f64 / 255.0, ba as f64 / 255.0);
        pm += ((ar as f64 * fa - br as f64 * fb).abs()
            + (ag as f64 * fa - bg as f64 * fb).abs()
            + (ab as f64 * fa - bb as f64 * fb).abs())
            / 3.0;
        if aa >= 250 && ba >= 250 {
            op += ((ar as i32 - br as i32).abs()
                + (ag as i32 - bg as i32).abs()
                + (ab as i32 - bb as i32).abs()) as f64
                / 3.0;
            nop += 1;
        }
    }
    (pm / n as f64, if nop > 0 { op / nop as f64 } else { f64::NAN }, nop)
}

/// alpha-over composite of `layers` (bottom-first) into straight RGBA.
fn composite(layers: &[Vec<u8>], w: usize, h: usize, opacities: &[f64]) -> Vec<u8> {
    let mut acc = vec![0u8; w * h * 4];
    for (li, lay) in layers.iter().enumerate() {
        let lo = opacities[li];
        for i in 0..w * h {
            let sa = lay[i * 4 + 3] as f64 / 255.0 * lo;
            if sa <= 0.0 {
                continue;
            }
            let da = acc[i * 4 + 3] as f64 / 255.0;
            let oa = sa + da * (1.0 - sa);
            if oa <= 0.0 {
                continue;
            }
            for c in 0..3 {
                let s = lay[i * 4 + c] as f64;
                let d = acc[i * 4 + c] as f64;
                acc[i * 4 + c] = ((s * sa + d * da * (1.0 - sa)) / oa).round() as u8;
            }
            acc[i * 4 + 3] = (oa * 255.0).round() as u8;
        }
    }
    acc
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        bail!("usage: kra_probe <file.kra> [--layer layerN] [--out png] [--merged ref.png]");
    }
    let kra = &args[0];
    let mut layer: Option<String> = None;
    let mut out = String::new();
    let mut merged = String::new();
    let mut k = 1;
    while k < args.len() {
        match args[k].as_str() {
            "--layer" => { layer = Some(args[k + 1].clone()); k += 2; }
            "--out" => { out = args[k + 1].clone(); k += 2; }
            "--merged" => { merged = args[k + 1].clone(); k += 2; }
            o => bail!("unknown arg {o}"),
        }
    }

    let f = std::fs::File::open(kra)?;
    let mut ar = zip::ZipArchive::new(BufReader::new(f))?;

    // map layer filename -> zip path
    let mut layer_paths = std::collections::HashMap::new();
    for i in 0..ar.len() {
        let name = ar.by_index(i)?.name().to_string();
        if let Some(pos) = name.find("/layers/") {
            let base = &name[pos + 8..];
            if base.starts_with("layer") && !base.contains('/') {
                layer_paths.insert(base.to_string(), name);
            }
        }
    }

    let xml = {
        let mut s = String::new();
        ar.by_name("maindoc.xml")?.read_to_string(&mut s)?;
        s
    };
    let infos = parse_layers(&xml)?;

    // canvas size from the <IMAGE ...> tag
    let img_tag_i = xml.find("<IMAGE ").context("no IMAGE tag")?;
    let img_tag = &xml[img_tag_i..xml[img_tag_i..].find('>').context("IMAGE tag")? + img_tag_i];
    let cw: usize = attr(img_tag, "width").context("IMAGE width")?.parse()?;
    let ch: usize = attr(img_tag, "height").context("IMAGE height")?.parse()?;
    println!("canvas: {cw}x{ch}");
    println!("layers (maindoc order, TOP first):");
    for li in &infos {
        println!("  {:<8} vis={} op={:.2} {:?}",
                 li.filename, li.visible as u8, li.opacity, li.name);
    }

    // decode selected layer or all
    let targets: Vec<&LayerInfo> = match &layer {
        Some(name) => vec![infos.iter().find(|l| &l.filename == name)
            .with_context(|| format!("no layer named {name}"))?],
        None => infos.iter().collect(),
    };

    let mut canvases: Vec<(String, Vec<u8>)> = Vec::new();
    let (w, h) = (cw, ch);
    for li in &targets {
        let path = layer_paths.get(&li.filename)
            .with_context(|| format!("no zip entry for {}", li.filename))?;
        let data = {
            let mut buf = Vec::new();
            ar.by_name(path)?.read_to_end(&mut buf)?;
            buf
        };
        let t = parse_tiled(&data)?;
        println!("{}: {} tiles, csize total {} bytes", li.filename, t.tiles.len(),
                 t.tiles.iter().map(|p| p.2.len()).sum::<usize>()); // p.2 = compressed
        let cv = assemble(&t, w, h, false)?;
        let opaque = cv.chunks_exact(4).filter(|p| p[3] == 255).count();
        let painted = cv.chunks_exact(4).filter(|p| p[3] > 0).count();
        println!("   painted px: {} (alpha>0), {} (alpha=255)", painted, opaque);
        canvases.push((li.filename.clone(), cv));
    }
    if w == 0 {
        bail!("no tiles decoded");
    }

    if !out.is_empty() {
        write_png_rgba(&out, &canvases[0].1, w, h)?;
        println!("wrote {out}");
    }

    if merged.is_empty() {
        return Ok(());
    }

    // full-stack composite for each layout hypothesis vs merged
    let (mrgba, mw2, mh2) = read_png_rgba(&merged)?;
    anyhow::ensure!((mw2, mh2) == (w, h), "merged {}x{} vs tiles {w}x{h}", mw2, mh2);

    // re-decode ALL layers under each hypothesis (targets already holds
    // rgba-decoded stack when no --layer given)
    for (swap, label) in [(false, "RGBA"), (true, "BGRA")] {
        let mut stack = Vec::new();
        let mut ops = Vec::new();
        let mut vis = Vec::new();
        for li in infos.iter() {
            let path = layer_paths.get(&li.filename)
                .with_context(|| format!("no zip entry for {}", li.filename))?;
            let data = {
                let mut buf = Vec::new();
                ar.by_name(path)?.read_to_end(&mut buf)?;
                buf
            };
            let t = parse_tiled(&data)?;
            stack.push(assemble(&t, w, h, swap)?);
            ops.push(li.opacity);
            vis.push(li.visible);
        }
        // bottom-first = reverse of maindoc order
        let mut order: Vec<usize> = (0..stack.len()).collect();
        order.reverse();
        let layers: Vec<Vec<u8>> = order.iter().enumerate()
            .filter(|(i, _)| vis[*i])
            .map(|(_, j)| stack[*j].clone())
            .collect();
        let opacities: Vec<f64> = order.iter().enumerate()
            .filter(|(i, _)| vis[*i])
            .map(|(_, j)| ops[*j])
            .collect();
        let comp = composite(&layers, w, h, &opacities);
        let (pm, opq, nop) = diff_stats(&comp, &mrgba);
        println!("hypothesis {label:<6} premult mean={pm:7.2}   opaque-px mean={opq:7.2} (n={nop})");
    }
    Ok(())
}
