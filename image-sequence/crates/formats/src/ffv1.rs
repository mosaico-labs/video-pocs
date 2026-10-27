//! FFV1: one lossless frame (version 3, sliced) in a Matroska file, with
//! FFmpeg's own encoder and decoder.

use std::path::Path;

use anyhow::Result;
use ffmpeg_the_third::{Dictionary, util::format::Pixel};

use super::{
    Format, I420,
    ffmpeg::{read_one_frame, write_one_frame},
};

/// Slices, each coded on its own thread.
const SLICES: u32 = 16;

pub const FORMAT: Format = Format {
    name: "ffv1",
    extension: "mkv",
    exact: true,
    settings,
    write,
    read,
};

fn settings() -> String {
    format!("FFV1 version 3, {SLICES} slices, in Matroska")
}

fn write(image: &I420, path: &Path) -> Result<()> {
    let mut options = Dictionary::new();
    options.set("level", "3");
    options.set("slices", SLICES.to_string());
    write_one_frame(image, path, "ffv1", options)
}

fn read(path: &Path, _width: u32, _height: u32) -> Result<I420> {
    read_one_frame(path, None, &[Pixel::YUV420P])
}
