//! Lossless AVIF: an AV1 still image from SVT-AV1 in still-picture mode, in
//! FFmpeg's AVIF muxer. Decoded with dav1d, as the video is.

use std::path::Path;

use anyhow::Result;
use ffmpeg_the_third::{Dictionary, util::format::Pixel};

use super::{
    Format, I420,
    ffmpeg::{AV1_DECODER, read_one_frame, write_one_frame},
};

/// SVT-AV1 preset. A lossless image is the same at every preset (checked at
/// 8K with presets 8 and 10: same size, same time).
const PRESET: u8 = 8;

pub const FORMAT: Format = Format {
    name: "avif",
    extension: "avif",
    exact: true,
    settings,
    write,
    read,
};

fn settings() -> String {
    format!("SVT-AV1 lossless, still picture, preset {PRESET}; dav1d")
}

fn write(image: &I420, path: &Path) -> Result<()> {
    let mut options = Dictionary::new();
    options.set("preset", PRESET.to_string());
    options.set("svtav1-params", "lossless=1:avif=1");
    write_one_frame(image, path, "libsvtav1", options)
}

fn read(path: &Path, _width: u32, _height: u32) -> Result<I420> {
    read_one_frame(path, Some(AV1_DECODER), &[Pixel::YUV420P])
}
