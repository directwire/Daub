//! daub 渲染核库面（wasm/嵌入宿主用）：calib+brushlib+tip+preset+render
//! + kra/psd 打包。kra/psd 起初只在 bin 侧（CLI 表面），wasm 出浏览器
//! 导出后升入库树（纯 rust zip，wasm 同路）；bin 侧 main.rs 的 mod 声
//! 明照旧，两棵树编译互不影响。
//!
//! wasm 目标（T6）：`cargo check --lib --target wasm32-unknown-unknown`。

pub mod brushlib;
pub mod calib;
pub mod kra;
pub mod preset;
pub mod psd;
pub mod render;
pub mod tip;

#[cfg(target_arch = "wasm32")]
pub mod wasm;
