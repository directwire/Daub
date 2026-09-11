//! wasm32 表面（T6）：plan JSON + 校准 JSON 字符串进 → RGBA 像素 /
//! KRA / PSD 字节出。逐像素与 daub.exe 同核（同一 render.rs），浏览器
//! 内消灭 JS 笔模的 7.66 近似差。tip 引擎不进 wasm（.kpp 需 fs 读文
//! 件）：W11 带层计划的笔全部走胶囊校准路径，与 daub.exe 默认带层计划
//! 逐像素同路。
#![allow(clippy::unused_unit)]

use crate::render::{Directory, LayerBuf};
use wasm_bindgen::prelude::*;

fn jerr(e: anyhow::Error) -> JsValue {
    JsValue::from_str(&format!("{e:#}"))
}

/// 一次渲染的产物：宽高 + RGBA 行主序缓冲。
#[wasm_bindgen]
pub struct Rendered {
    width: u32,
    height: u32,
    data: Vec<u8>,
    rgb: Vec<u8>,
}

#[wasm_bindgen]
impl Rendered {
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    /// RGBA8 行主序；JS 侧 new ImageData(new Uint8ClampedArray(data), w, h)。
    #[wasm_bindgen(getter)]
    pub fn data(&self) -> Vec<u8> {
        self.data.clone()
    }

    /// 不带 alpha 通道的变体（省 1/4 内存），BMP/PNG 编码器直接吃。
    #[wasm_bindgen(getter)]
    pub fn data_rgb(&self) -> Vec<u8> {
        self.rgb.clone()
    }
}

/// render() 栈 → 合成 RGB → 补 alpha → Rendered（三条出口共用）。
fn composited(paper: [u8; 3], stack: &[(String, LayerBuf)], w: usize, h: usize)
              -> Rendered {
    let img = crate::render::composite(paper, stack, (w, h));
    // composite 出 RGB8；浏览器 ImageData 要 RGBA，这里补 alpha=255。
    let mut data = vec![255u8; w * h * 4];
    for i in 0..w * h {
        data[i * 4..i * 4 + 3].copy_from_slice(&img[i * 3..i * 3 + 3]);
    }
    Rendered {
        width: w as u32,
        height: h as u32,
        data,
        rgb: img,
    }
}

fn parse_plan_cal(plan_json: &str, cal_json: &str)
                  -> Result<(Directory, crate::calib::Calib), JsValue> {
    let dir: Directory = serde_json::from_str(plan_json)
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    let cal = crate::calib::Calib::from_str(cal_json).map_err(jerr)?;
    Ok((dir, cal))
}

fn render_full(plan_json: &str, cal_json: &str)
    -> Result<([u8; 3], Vec<(String, LayerBuf)>, (usize, usize)), JsValue> {
    let (dir, cal) = parse_plan_cal(plan_json, cal_json)?;
    crate::render::render(&dir, &cal, None).map_err(jerr)
}

/// 真渲入口：`plan_json` = 计划文件全文（头键多出 reference/seed 等会被
/// serde 静默忽略，契约见 docs/DOWNSTREAM_GUIDE.md §2）；`cal_json` =
/// ink_calib.json 全文。任何错误以字符串抛给 JS，不吞。
#[wasm_bindgen]
pub fn render_plan_rgba(plan_json: &str, cal_json: &str) -> Result<Rendered, JsValue> {
    let (paper, stack, (w, h)) = render_full(plan_json, cal_json)?;
    Ok(composited(paper, &stack, w, h))
}

/// 真值回放器：计划解析一次，前 k 笔逐帧出真值。每帧与「截断计划整
/// 渲」逐字节同路（render_prefix 契约），timelapse 走的同一条快路径。
#[wasm_bindgen]
pub struct TruthPlayer {
    dir: Directory,
    cal: crate::calib::Calib,
    strokes: usize,
}

#[wasm_bindgen]
impl TruthPlayer {
    #[wasm_bindgen(constructor)]
    pub fn new(plan_json: &str, cal_json: &str) -> Result<TruthPlayer, JsValue> {
        let (dir, cal) = parse_plan_cal(plan_json, cal_json)?;
        let strokes = dir.strokes.len();
        Ok(TruthPlayer { dir, cal, strokes })
    }

    /// 总笔数（回放进度条上界）。
    #[wasm_bindgen(getter)]
    pub fn stroke_count(&self) -> usize {
        self.strokes
    }

    /// 前 k 笔真值帧（k 超界自动夹到总数）。
    pub fn frame(&self, k: usize) -> Result<Rendered, JsValue> {
        let (paper, stack, (w, h)) =
            crate::render::render_prefix(&self.dir, k, &self.cal, None)
                .map_err(jerr)?;
        Ok(composited(paper, &stack, w, h))
    }
}

/// KRA 全文件字节（Krita 直开）——与 native write_kra 逐字节同路，仅
/// 落点从文件换成内存。doc_name 进 maindoc / documentinfo。
#[wasm_bindgen]
pub fn build_kra_bytes(plan_json: &str, cal_json: &str, doc_name: &str)
                       -> Result<Vec<u8>, JsValue> {
    let (paper, stack, (w, h)) = render_full(plan_json, cal_json)?;
    let layers: Vec<crate::kra::KraLayer> = stack.iter()
        .map(|(name, buf)| crate::kra::KraLayer::from_layer_buf(name, buf))
        .collect();
    crate::kra::build_kra(doc_name, w, h, Some(paper), layers).map_err(jerr)
}

/// PSD 全文件字节（Photoshop / Clip Studio / Affinity 直开）。
#[wasm_bindgen]
pub fn build_psd_bytes(plan_json: &str, cal_json: &str) -> Result<Vec<u8>, JsValue> {
    let (paper, stack, (w, h)) = render_full(plan_json, cal_json)?;
    let layers: Vec<crate::kra::KraLayer> = stack.iter()
        .map(|(name, buf)| crate::kra::KraLayer::from_layer_buf(name, buf))
        .collect();
    crate::psd::build_psd(w, h, Some(paper), layers).map_err(jerr)
}
