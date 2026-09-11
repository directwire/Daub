//! daub - headless calibrated-brush renderer. A plan (a JSON catalog of
//! pressure-tagged strokes) in, a finished painting out: sub-second
//! whole-painting rasterisation.

use anyhow::{Context, Result};
use std::io::Write;
use std::time::Instant;

mod brushlib;
mod calib;
mod kra;
mod preset;
mod psd;
mod render;
mod tip;

/// Default assets: the vendored copies under tools/data first (every
/// clone of this repository is self-contained), the directory named by
/// `$DAUB_KRMCP_TOOLS` second (optional escape hatch pointing at an
/// upstream calibration checkout). First existing wins; nothing found
/// = capsule / no-registry fallback, never an error.
const DEFAULT_TIPS: &str = "tools/data/brush_lib.json";
const DEFAULT_CAL: &str = "tools/data/ink_calib.json";
const TIPS_BASENAME: &str = "brush_lib.json";
const CAL_BASENAME: &str = "ink_calib.json";

fn upstream_asset(basename: &str) -> Option<String> {
    std::env::var_os("DAUB_KRMCP_TOOLS").map(|dir| {
        std::path::Path::new(&dir)
            .join(basename)
            .to_string_lossy()
            .into_owned()
    })
}

fn default_asset(primary: &str, upstream: Option<String>) -> String {
    if std::path::Path::new(primary).is_file() || upstream.is_none() {
        primary.to_string()
    } else {
        upstream.unwrap()
    }
}

fn read_png_rgb(path: &str) -> Result<(Vec<u8>, usize, usize)> {
    let f = std::fs::File::open(path).with_context(|| path.to_string())?;
    let dec = png::Decoder::new(std::io::BufReader::new(f));
    let mut rdr = dec.read_info()?;
    let (w, h) = (rdr.info().width as usize, rdr.info().height as usize);
    let mut buf = vec![0u8; rdr.output_buffer_size()];
    let info = rdr.next_frame(&mut buf)?;
    let channels = info.color_type.samples();
    let mut rgb = vec![0u8; w * h * 3];
    match channels {
        3 => rgb.copy_from_slice(&buf[..w * h * 3]),
        4 => {
            for i in 0..w * h {
                // composite over white like typical viewers
                let a = buf[i * 4 + 3] as f32 / 255.0;
                for c in 0..3 {
                    rgb[i * 3 + c] = (buf[i * 4 + c] as f32 * a
                        + 255.0 * (1.0 - a)) as u8;
                }
            }
        }
        n => anyhow::bail!("unsupported channel count {n} in {path}"),
    }
    Ok((rgb, w, h))
}

fn write_png_rgb(path: &str, rgb: &[u8], w: usize, h: usize) -> Result<()> {
    if std::env::var("DAUB_PROBE").is_ok() {
        let mut hsh: u64 = 0xcbf29ce484222325;
        for &b in rgb {
            hsh ^= b as u64;
            hsh = hsh.wrapping_mul(0x100000001b3);
        }
        eprintln!("probe: pre-encode rgb fnv={hsh}");
    }
    let f = std::fs::File::create(path).with_context(|| path.to_string())?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(f), w as u32, h as u32);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    let mut wr = enc.write_header()?;
    wr.write_image_data(rgb)?;
    wr.finish()?;
    Ok(())
}

/// 24-bit uncompressed BMP: same pixels as the PNG path, no zlib -
/// the timelapse writes hundreds of frames and the deflate pass was
/// a real slice of every one. Bottom-up rows, BGR, padded to 4 bytes.
fn write_bmp_rgb(path: &str, rgb: &[u8], w: usize, h: usize) -> Result<()> {
    if std::env::var("DAUB_PROBE").is_ok() {
        let mut hsh: u64 = 0xcbf29ce484222325;
        for &b in rgb {
            hsh ^= b as u64;
            hsh = hsh.wrapping_mul(0x100000001b3);
        }
        eprintln!("probe: pre-encode rgb fnv={hsh}");
    }
    let pad = (4 - (w * 3) % 4) % 4;
    let row = w * 3 + pad;
    let data = row * h;
    let mut out = vec![0u8; 54 + data];
    out[0..2].copy_from_slice(b"BM");
    out[2..6].copy_from_slice(&((54 + data) as u32).to_le_bytes());
    out[10..14].copy_from_slice(&54u32.to_le_bytes());
    out[14..18].copy_from_slice(&40u32.to_le_bytes()); // BITMAPINFOHEADER
    out[18..22].copy_from_slice(&(w as i32).to_le_bytes());
    out[22..26].copy_from_slice(&(h as i32).to_le_bytes());
    out[26..28].copy_from_slice(&1u16.to_le_bytes());
    out[28..30].copy_from_slice(&24u16.to_le_bytes());
    out[34..38].copy_from_slice(&(data as u32).to_le_bytes());
    out[38..42].copy_from_slice(&2835i32.to_le_bytes()); // 72 dpi
    out[42..46].copy_from_slice(&2835i32.to_le_bytes());
    for y in 0..h {
        let src = (h - 1 - y) * w * 3;
        let dst = 54 + y * row;
        for x in 0..w {
            out[dst + x * 3] = rgb[src + x * 3 + 2];     // B
            out[dst + x * 3 + 1] = rgb[src + x * 3 + 1]; // G
            out[dst + x * 3 + 2] = rgb[src + x * 3];     // R
        }
    }
    std::fs::write(path, &out).with_context(|| path.to_string())
}

/// Resolve the tip registry per --tips semantics: an explicit path must
/// exist (fail loud); the default is used only when present.
fn resolve_tips(explicit: Option<&String>) -> Result<Option<String>> {
    match explicit {
        Some(p) => {
            anyhow::ensure!(
                std::path::Path::new(p).is_file(),
                "--tips registry {p} not found");
            Ok(Some(p.clone()))
        }
        None => {
            let p = default_asset(DEFAULT_TIPS, upstream_asset(TIPS_BASENAME));
            if std::path::Path::new(&p).is_file() {
                Ok(Some(p))
            } else {
                Ok(None)
            }
        }
    }
}

fn cmd_render(args: &[String]) -> Result<()> {
    let mut dir_path = None;
    let mut out = String::from("daub_out.png");
    let mut cal_path = default_asset(DEFAULT_CAL, upstream_asset(CAL_BASENAME));
    let mut threads = 0usize;
    let mut kra_out = String::new();
    let mut psd_out = String::new();
    let mut layers_dir = String::new();
    let mut tips_arg: Option<String> = None;
    let mut no_tips = false;
    let mut k = 0;
    while k < args.len() {
        match args[k].as_str() {
            "--out" => { out = args[k + 1].clone(); k += 2; }
            "--cal" => { cal_path = args[k + 1].clone(); k += 2; }
            "--threads" => { threads = args[k + 1].parse()?; k += 2; }
            "--kra" => { kra_out = args[k + 1].clone(); k += 2; }
            "--psd" => { psd_out = args[k + 1].clone(); k += 2; }
            "--layers-dir" => { layers_dir = args[k + 1].clone(); k += 2; }
            "--tips" => { tips_arg = Some(args[k + 1].clone()); k += 2; }
            // capsule reference render even where the registry would
            // route tips (regression harness)
            "--no-tips" => { no_tips = true; k += 1; }
            other => { dir_path = Some(other.to_string()); k += 1; }
        }
    }
    let dir_path = dir_path.context(
        "usage: daub render <strokes.json> [--out png] [--cal json] \
         [--threads N] [--kra file.kra] [--psd file.psd] \
         [--layers-dir dir] [--tips brush_lib.json]")?;
    if threads > 0 {
        rayon::ThreadPoolBuilder::new().num_threads(threads)
            .build_global().ok();
    }

    let t0 = Instant::now();
    let raw = std::fs::read_to_string(&dir_path)?;
    let dir: render::Directory = serde_json::from_str(&raw)
        .with_context(|| format!("parsing {}", dir_path))?;
    let n_strokes = dir.strokes.len();

    let tips = if no_tips {
        None
    } else {
        match resolve_tips(tips_arg.as_ref())? {
        Some(path) => {
            let lib = brushlib::BrushLib::load(&path)
                .with_context(|| format!("tip registry {path}"))?;
            Some(render::Tips::build(&dir, lib)
                .with_context(|| format!("building tip engines from {path}"))?)
        }
        None => None,
        }
    };
    let n_tips = tips.as_ref().map_or(0, |t| t.presets.len());
    let parse = t0.elapsed();

    let t1 = Instant::now();
    let cal = calib::Calib::load(&cal_path)?;
    let (paper, stack, (w, h)) = render::render(&dir, &cal, tips.as_ref())?;
    let raster = t1.elapsed();

    let tips_field = match &tips {
        Some(t) => format!(
            "{}dab/{}bake/{}degraded across {} tip preset(s)",
            t.dabs.load(std::sync::atomic::Ordering::Relaxed),
            t.bakes.load(std::sync::atomic::Ordering::Relaxed),
            t.degraded.load(std::sync::atomic::Ordering::Relaxed),
            n_tips),
        None => "off".to_string(),
    };

    let t2 = Instant::now();
    let img = render::composite(paper, &stack, (w, h));
    let comp = t2.elapsed();

    let t3 = Instant::now();
    // writer follows the --out extension: render-seq's concat lists must
    // stay homogeneous (one mp4 decoder probing), and the timelapse tail
    // rides this to emit a real BMP final frame
    if out.to_ascii_lowercase().ends_with(".bmp") {
        write_bmp_rgb(&out, &img, w, h)?;
    } else {
        write_png_rgb(&out, &img, w, h)?;
    }
    let wrote = t3.elapsed();

    if !layers_dir.is_empty() {
        let t5 = Instant::now();
        render::write_layers_dir(std::path::Path::new(&layers_dir),
                                 paper, &stack, (w, h))
            .with_context(|| format!("writing layers dir {layers_dir}"))?;
        println!("layers: wrote {layers_dir} ({} layers) in {:.2}s",
                 stack.len(), t5.elapsed().as_secs_f64());
    }

    if !kra_out.is_empty() {
        let t4 = Instant::now();
        let layers: Vec<kra::KraLayer> = stack.iter()
            .map(|(name, buf)| kra::KraLayer::from_layer_buf(name, buf))
            .collect();
        let doc_name = std::path::Path::new(&kra_out)
            .file_stem().and_then(|s| s.to_str()).unwrap_or("daub").to_string();
        kra::write_kra(&kra_out, &doc_name, w, h, Some(paper), layers)
            .with_context(|| format!("writing {kra_out}"))?;
        println!("kra: wrote {kra_out} ({} layers incl paper coat) in {:.2}s",
                 stack.len() + 1, t4.elapsed().as_secs_f64());
    }

    if !psd_out.is_empty() {
        let t6 = Instant::now();
        let layers: Vec<psd::PsdLayer> = stack.iter()
            .map(|(name, buf)| psd::PsdLayer::from_layer_buf(name, buf))
            .collect();
        psd::write_psd(&psd_out, w, h, Some(paper), layers)
            .with_context(|| format!("writing {psd_out}"))?;
        println!("psd: wrote {psd_out} ({} layers incl paper coat) in {:.2}s",
                 stack.len() + 1, t6.elapsed().as_secs_f64());
    }

    println!(
        "daub: {} strokes, {} layers, {}x{} -> {}  parse {:.2}s raster {:.2}s composite {:.2}s png {:.2}s total {:.2}s  tips: {}",
        n_strokes, stack.len(), w, h, out,
        parse.as_secs_f64(), raster.as_secs_f64(),
        comp.as_secs_f64(), wrote.as_secs_f64(), t0.elapsed().as_secs_f64(),
        tips_field
    );
    Ok(())
}

/// `daub render-seq <strokes.json> --out-dir dir --prefixes-file file`:
/// the timelapse fast path. One process parses the plan/tips/cal once
/// and emits every prefix frame through the incremental SeqRenderer;
/// frames are 24-bit BMP (lossless, no zlib) unless --pattern ends in
/// .png. ffmpeg consumes BMP in concat demuxer mode just the same.
///
/// `--pipe` swaps the file sink for raw rgb24 frames on stdout (logs
/// move to stderr): render_timelapse streams them straight into
/// ffmpeg's image2pipe demuxer, so the reveal video needs no ~8MB
/// frame files at all. `--tail-plan <plan.json>` appends one more raw
/// frame rendered from that plan - the small_first reveal's closing
/// frame is the ORIGINAL stroke order, not the seq plan's resort.
fn cmd_render_seq(args: &[String]) -> Result<()> {
    let mut dir_path = None;
    let mut out_dir = String::new();
    let mut prefixes: Vec<usize> = Vec::new();
    let mut prefixes_file = String::new();
    let mut pattern = String::from("f_%06d.bmp");
    let mut cal_path = default_asset(DEFAULT_CAL, upstream_asset(CAL_BASENAME));
    let mut threads = 0usize;
    let mut tips_arg: Option<String> = None;
    let mut no_tips = false;
    let mut pipe = false;
    let mut tail_plan = String::new();
    let mut k = 0;
    while k < args.len() {
        match args[k].as_str() {
            "--out-dir" => { out_dir = args[k + 1].clone(); k += 2; }
            "--prefixes" => {
                prefixes.extend(args[k + 1].split(',')
                    .map(|s| s.trim().parse::<usize>())
                    .collect::<Result<Vec<_>, _>>()?);
                k += 2;
            }
            "--prefixes-file" => { prefixes_file = args[k + 1].clone(); k += 2; }
            "--pattern" => { pattern = args[k + 1].clone(); k += 2; }
            "--cal" => { cal_path = args[k + 1].clone(); k += 2; }
            "--threads" => { threads = args[k + 1].parse()?; k += 2; }
            "--tips" => { tips_arg = Some(args[k + 1].clone()); k += 2; }
            "--no-tips" => { no_tips = true; k += 1; }
            "--pipe" => { pipe = true; k += 1; }
            "--tail-plan" => { tail_plan = args[k + 1].clone(); k += 2; }
            other => { dir_path = Some(other.to_string()); k += 1; }
        }
    }
    anyhow::ensure!(!out_dir.is_empty() || pipe,
        "usage: daub render-seq <strokes.json> --out-dir dir \
         (--prefixes \"1,2,3\" | --prefixes-file file) \
         [--pattern f_%06d.bmp] [--cal json] [--threads N] \
         [--tips brush_lib.json] | --pipe [--tail-plan plan.json]");
    anyhow::ensure!(pipe || pattern.contains("%06d"),
                    "--pattern must contain %06d");
    if !prefixes_file.is_empty() {
        let raw = std::fs::read_to_string(&prefixes_file)
            .with_context(|| prefixes_file.clone())?;
        for l in raw.lines() {
            let t = l.trim();
            if t.is_empty() {
                continue;
            }
            prefixes.push(t.parse().with_context(|| {
                format!("bad prefix {t:?} in {prefixes_file}")
            })?);
        }
    }
    anyhow::ensure!(!prefixes.is_empty(), "no prefixes given");
    if threads > 0 {
        rayon::ThreadPoolBuilder::new().num_threads(threads)
            .build_global().ok();
    }

    let t0 = Instant::now();
    let dir_path = dir_path.context("missing <strokes.json>")?;
    let raw = std::fs::read_to_string(&dir_path)?;
    let dir: render::Directory = serde_json::from_str(&raw)
        .with_context(|| format!("parsing {}", dir_path))?;
    let n = dir.strokes.len();
    {
        let mut prev = 0usize;
        for kk in &prefixes {
            anyhow::ensure!(*kk > prev,
                "prefixes must be strictly increasing (got {kk} after {prev})");
            anyhow::ensure!(*kk <= n, "prefix {kk} > {n} strokes");
            prev = *kk;
        }
    }
    // built over the FULL directory: every prefix's preset set is a
    // subset, and per-stroke routing only consults membership, so
    // bytes match a prefix-fresh build; the --tail-plan dir gets its
    // own build so its bytes match a plain `daub render` of it
    let make_tips = |d: &render::Directory| -> Result<Option<render::Tips>> {
        if no_tips {
            return Ok(None);
        }
        Ok(match resolve_tips(tips_arg.as_ref())? {
            Some(path) => {
                let lib = brushlib::BrushLib::load(&path)
                    .with_context(|| format!("tip registry {path}"))?;
                Some(render::Tips::build(d, lib)
                    .with_context(|| format!(
                        "building tip engines from {path}"))?)
            }
            None => None,
        })
    };
    let tips = make_tips(&dir)?;
    let n_tips = tips.as_ref().map_or(0, |t| t.presets.len());
    let cal = calib::Calib::load(&cal_path)?;

    // the tail frame is rendered from a second plan (the ORIGINAL
    // stroke order for the small_first splice); parse it up front so
    // its parse cost lands in the same bucket as the seq plan's
    let tail_dir = if tail_plan.is_empty() {
        None
    } else {
        anyhow::ensure!(pipe, "--tail-plan only makes sense with --pipe");
        let traw = std::fs::read_to_string(&tail_plan)
            .with_context(|| tail_plan.clone())?;
        Some(serde_json::from_str::<render::Directory>(&traw)
            .with_context(|| format!("parsing {tail_plan}"))?)
    };
    let parse = t0.elapsed();

    if !pipe {
        std::fs::create_dir_all(&out_dir)
            .with_context(|| format!("creating {out_dir}"))?;
    }
    let mut seq = render::SeqRenderer::new(&dir);
    let t1 = Instant::now();
    let tips_field = match &tips {
        Some(t) => format!(
            "{}dab/{}bake/{}degraded across {} tip preset(s)",
            t.dabs.load(std::sync::atomic::Ordering::Relaxed),
            t.bakes.load(std::sync::atomic::Ordering::Relaxed),
            t.degraded.load(std::sync::atomic::Ordering::Relaxed),
            n_tips),
        None => "off".to_string(),
    };
    // stdout stays pure rgb24 bytes in pipe mode: the probe hashes
    // live in the png/bmp writers, which this path never touches
    let mut out = std::io::stdout().lock();
    let mut frame_wh = (0usize, 0usize);
    for (i, &kk) in prefixes.iter().enumerate() {
        let tf = Instant::now();
        let (paper, stack, (w, h)) =
            seq.frame(&dir, kk, &cal, tips.as_ref())?;
        let img = render::composite(paper, &stack, (w, h));
        if pipe {
            if frame_wh == (0, 0) {
                frame_wh = (w, h);
            }
            anyhow::ensure!(frame_wh == (w, h),
                "frame {i} is {w}x{h}, stream started {0}x{1}",
                frame_wh.0, frame_wh.1);
            out.write_all(&img)
                .with_context(|| format!("writing frame {i} to stdout"))?;
            out.flush().ok();
            eprintln!("seq {}/{} k={kk} in {:.2}s", i + 1, prefixes.len(),
                      tf.elapsed().as_secs_f64());
            continue;
        }
        let name = pattern.replace("%06d", &format!("{i:06}"));
        let path = std::path::Path::new(&out_dir).join(&name);
        let ps = path.to_str().context("out path not utf-8")?;
        if name.ends_with(".bmp") {
            write_bmp_rgb(ps, &img, w, h)?;
        } else {
            write_png_rgb(ps, &img, w, h)?;
        }
        println!("seq {}/{} k={kk} {} in {:.2}s", i + 1, prefixes.len(),
                 ps, tf.elapsed().as_secs_f64());
    }
    if pipe {
        if let Some(tdir) = &tail_dir {
            let tt = Instant::now();
            let tn = tdir.strokes.len();
            anyhow::ensure!(tn > 0, "tail plan has no strokes");
            let ttips = make_tips(tdir)?;
            let mut tseq = render::SeqRenderer::new(tdir);
            let (paper, stack, (tw, th)) =
                tseq.frame(tdir, tn, &cal, ttips.as_ref())?;
            let timg = render::composite(paper, &stack, (tw, th));
            anyhow::ensure!(frame_wh == (0, 0) || frame_wh == (tw, th),
                "tail frame is {tw}x{th}, stream started {0}x{1}",
                frame_wh.0, frame_wh.1);
            out.write_all(&timg)
                .context("writing tail frame to stdout")?;
            out.flush().ok();
            eprintln!("tail: {} strokes in {:.2}s", tn,
                      tt.elapsed().as_secs_f64());
        }
        eprintln!("sequence: {} frames (+tail {}) piped in {:.2}s \
                   (parse {:.2}s)  tips: {}",
                  prefixes.len(), if tail_dir.is_some() { "yes" } else { "no" },
                  t1.elapsed().as_secs_f64(), parse.as_secs_f64(), tips_field);
        return Ok(());
    }
    println!("sequence: {} frames -> {}/ in {:.2}s (parse {:.2}s)  tips: {}",
             prefixes.len(), out_dir, t1.elapsed().as_secs_f64(),
             parse.as_secs_f64(), tips_field);
    Ok(())
}

fn cmd_compare(args: &[String]) -> Result<()> {
    anyhow::ensure!(args.len() >= 2,
                    "usage: daub compare <a.png> <b.png> [--region x0,y0,x1,y1]");
    let (a, b) = (args[0].clone(), args[1].clone());
    let (ia, wa, ha) = read_png_rgb(&a)?;
    let (ib, wb, hb) = read_png_rgb(&b)?;
    anyhow::ensure!((wa, ha) == (wb, hb),
                    "size mismatch {wa}x{ha} vs {wb}x{hb}");
    let mut box_ = (0usize, 0usize, wa, ha);
    let mut k = 2;
    while k < args.len() {
        if args[k] == "--region" {
            let v: Vec<usize> = args[k + 1].split(',')
                .map(|s| s.parse()).collect::<Result<_, _>>()?;
            box_ = (v[0], v[1], v[2].min(wa), v[3].min(ha));
            k += 2;
        } else { k += 1; }
    }
    let (x0, y0, x1, y1) = box_;
    let mut total = 0u64;
    let mut n = 0u64;
    let mut close60 = 0u64;
    let mut close120 = 0u64;
    for y in y0..y1 {
        for x in x0..x1 {
            let i = (y * wa + x) * 3;
            let s = (ia[i] as i32 - ib[i] as i32).abs()
                + (ia[i + 1] as i32 - ib[i + 1] as i32).abs()
                + (ia[i + 2] as i32 - ib[i + 2] as i32).abs();
            total += s as u64;
            if s < 60 { close60 += 1; }
            if s < 120 { close120 += 1; }
            n += 1;
        }
    }
    println!("mean|diff|={:.1}  within60={:.1}%  within120={:.1}%  ({}x{} px)",
             total as f64 / n as f64, 100.0 * close60 as f64 / n as f64,
             100.0 * close120 as f64 / n as f64, x1 - x0, y1 - y0);
    Ok(())
}

/// `daub presets [filter] [--tips path]`: registry + live .kpp probe.
/// One row per preset: engine admission, option enable bits, spacing
/// source, tip geometry and a measured 64px sprite bake time.
fn cmd_presets(args: &[String]) -> Result<()> {
    let mut tips_path = default_asset(DEFAULT_TIPS, upstream_asset(TIPS_BASENAME));
    let mut filter = String::new();
    let mut k = 0;
    while k < args.len() {
        match args[k].as_str() {
            "--tips" => { tips_path = args[k + 1].clone(); k += 2; }
            other => { filter = other.to_string(); k += 1; }
        }
    }
    let lib = brushlib::BrushLib::load(&tips_path)
        .with_context(|| format!("tip registry {tips_path}"))?;

    let mut names: Vec<&String> = lib.presets.keys().collect();
    names.sort();
    let mut shown = 0usize;
    println!("{:<28} {:<12} {:>3} {:>4} {:>9} {:<8} {:<22} {:<9} {:>10}",
             "preset", "paintopid", "tip", "gate", "spacing", "s/o/f",
             "unsupported", "tip", "bake64");
    for name in &names {
        if !filter.is_empty() && !name.contains(&filter) {
            continue;
        }
        let e = &lib.presets[*name];
        // live parse: the engine's own verdict (may disagree with the
        // registry probe - show the engine's reason when it does)
        let live = if std::path::Path::new(&e.kpp).is_file() {
            preset::KppPreset::load(&e.kpp, name).ok()
        } else {
            None
        };
        let engine_reason = live.as_ref().and_then(|p| p.unsupported.clone());
        let renderable = live.as_ref().filter(|p| p.unsupported.is_none());
        let (dims, bake, bits) = match renderable {
            Some(p) => {
                let mut cache = tip::SpriteCache::new(tip::SPRITE_CACHE_CAP);
                let t = Instant::now();
                cache.sprite(0, p, 64.0, 1.0);
                let ms = t.elapsed().as_secs_f64() * 1000.0;
                let b = format!("{:.2}ms", ms);
                let bits = format!("{}/{}/{}",
                                   if p.size_active { 'S' } else { '-' },
                                   if p.opacity_active { 'O' } else { '-' },
                                   if p.flow_active { 'F' } else { '-' });
                (format!("{}x{}", p.tip_w, p.tip_h), b, bits)
            }
            None => ("-".into(), "-".into(), "-/-/-".into()),
        };
        let reason = engine_reason.or_else(|| e.unsupported_reason.clone());
        // live cadence model from the .kpp itself (T1.3: `<Brush>` element
        // is the runtime truth); `f`/`a` suffix = linear/auto, `?` = the
        // registry's informational scalar (file not parseable here)
        let spacing_cell = match live.as_ref().map(|p| p.spacing) {
            Some(crate::preset::SpacingModel::Linear(f)) => {
                format!("{:.2}f", f)
            }
            Some(crate::preset::SpacingModel::Auto(c)) => {
                format!("{:.2}a", c)
            }
            None => format!("{:.2}?", e.spacing),
        };
        println!("{:<28} {:<12} {:>3} {:>4} {:>6}({:>2}) {:<8} {:<22} {:<9} {:>10}",
                 truncate(name, 28),
                 truncate(&e.extra.get("paintopid")
                          .and_then(|v| v.as_str().map(|s| s.to_string()))
                          .unwrap_or_else(|| live.as_ref()
                                          .map(|p| p.paintopid.clone())
                                          .unwrap_or_default()), 12),
                 if e.use_tips { "y" } else { "n" },
                 match &e.gate { Some(g) if g.pass => "PASS", Some(_) => "-", None => "-" },
                 spacing_cell,
                 truncate(&e.spacing_source, 2),
                 bits,
                 truncate(reason.as_deref().unwrap_or("-"), 22),
                 dims,
                 bake);
        shown += 1;
    }
    println!("\n{shown} preset(s) shown of {} registered ({tips_path})",
             lib.presets.len());
    Ok(())
}

fn truncate(s: &str, n: usize) -> String {
    // crude display-width guard: UTF-8 safe char truncation
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n - 1).collect::<String>() + "…"
    }
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(|s| s.as_str()) {
        Some("render") => cmd_render(&args[1..]),
        Some("render-seq") => cmd_render_seq(&args[1..]),
        Some("compare") => cmd_compare(&args[1..]),
        Some("presets") => cmd_presets(&args[1..]),
        _ => {
            eprintln!("daub - headless calibrated-brush renderer\n\n\
                commands:\n  \
                render <strokes.json> [--out png] [--cal json] [--threads N]\n\
                \x20   [--kra file.kra] [--layers-dir dir] [--tips brush_lib.json]\n  \
                render-seq <strokes.json> --out-dir dir\n\
                \x20   (--prefixes \"1,2,3\" | --prefixes-file file) [--pattern f_%06d.bmp]\n  \
                render-seq <strokes.json> --pipe [--tail-plan plan.json]\n\
                compare <a.png> <b.png> [--region x0,y0,x1,y1]\n  \
                presets [filter] [--tips brush_lib.json]");
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// img64's 1789-wide canvas is the real-world case that walks the
    /// 4-byte row padding (w*3 % 4 != 0) - untested until the timelapse
    /// started feeding these files to ffmpeg's concat demuxer.
    #[test]
    fn bmp_odd_width_rows_are_padded_bottom_up() {
        let (w, h) = (5usize, 3usize);
        // one distinct rgb triple per pixel, so row identity is checkable
        let rgb: Vec<u8> = (0..w * h)
            .flat_map(|i| [(i % 251) as u8, ((i * 7) % 251) as u8, ((i * 13) % 251) as u8])
            .collect();
        let path = std::env::temp_dir().join("daub_test_odd.bmp");
        let ps = path.to_str().unwrap();
        write_bmp_rgb(ps, &rgb, w, h).unwrap();
        let raw = std::fs::read(ps).unwrap();

        let pad = (4 - (w * 3) % 4) % 4;
        assert_eq!(pad, 1, "5px rows need 1 pad byte");
        let row = w * 3 + pad;
        assert_eq!(raw.len(), 54 + row * h);
        assert_eq!(&raw[0..2], b"BM");
        assert_eq!(i32::from_le_bytes(raw[18..22].try_into().unwrap()), w as i32);
        assert_eq!(i32::from_le_bytes(raw[22..26].try_into().unwrap()), h as i32);
        // bottom-up: file row 0 is the LAST rgb row, BGR-swapped...
        for (file_row, rgb_row) in (0..h).map(|y| (y, h - 1 - y)) {
            let dst = 54 + file_row * row;
            for x in 0..w {
                let s = (rgb_row * w + x) * 3;
                assert_eq!(raw[dst + x * 3], rgb[s + 2]);
                assert_eq!(raw[dst + x * 3 + 1], rgb[s + 1]);
                assert_eq!(raw[dst + x * 3 + 2], rgb[s]);
            }
            // ...and the padding bytes stay zero
            assert!(raw[dst + w * 3..dst + row].iter().all(|&b| b == 0));
        }
        std::fs::remove_file(ps).ok();
    }

    /// width already 4-aligned: no padding, file size is the plain sum.
    #[test]
    fn bmp_aligned_width_has_no_padding() {
        let (w, h) = (4usize, 2usize);
        let rgb = vec![77u8; w * h * 3];
        let path = std::env::temp_dir().join("daub_test_even.bmp");
        let ps = path.to_str().unwrap();
        write_bmp_rgb(ps, &rgb, w, h).unwrap();
        let raw = std::fs::read(ps).unwrap();
        assert_eq!(raw.len(), 54 + w * 3 * h);
        std::fs::remove_file(ps).ok();
    }
}
