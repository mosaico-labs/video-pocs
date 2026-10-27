//! JPEG, for comparison: a standard JFIF file any viewer shows in the right
//! colors, as close to the frame as JPEG allows. JFIF's YCbCr is BT.601 in
//! full range, so the frame is converted to it (see [`super::color`]), then
//! written at quality 100 (every quantization step 1) in 4:2:0, the U and V
//! planes passed as they are, with no RGB or resampling in the encoder. Read
//! back with FFmpeg's decoder, which also gives the 4:2:0 planes as they
//! are, then converted back. What is lost: the two conversions' rounding and
//! the DCT's.

use std::path::Path;

use anyhow::{Result, ensure};
use ffmpeg_the_third::util::format::Pixel;
use jpeg_encoder::{ColorType, Encoder, SamplingFactor};

use super::{
    Format, I420,
    color::{BT601_FULL, BT709_LIMITED, convert},
    ffmpeg::read_one_frame,
};

/// 100: every quantization step 1, the least JPEG can lose.
const QUALITY: u8 = 100;

pub const FORMAT: Format = Format {
    name: "jpeg",
    extension: "jpg",
    exact: false,
    settings,
    write,
    read,
};

fn settings() -> String {
    format!("JFIF, BT.601 full range, 4:2:0, quality {QUALITY}; FFmpeg's decoder")
}

fn write(image: &I420, path: &Path) -> Result<()> {
    let ycc = convert(image, &BT709_LIMITED, &BT601_FULL);
    let (w, h) = (ycc.width as usize, ycc.height as usize);
    let (luma, chroma) = ycc.data.split_at(w * h);
    let (u, v) = chroma.split_at((w / 2) * (h / 2));
    // The encoder takes YCbCr a pixel at a time and keeps the top-left one
    // of each 2x2 block (its default): U and V repeated over their block go
    // in as they are.
    let mut pixels = Vec::with_capacity(3 * w * h);
    for y in 0..h {
        for x in 0..w {
            let c = (y / 2) * (w / 2) + x / 2;
            pixels.extend_from_slice(&[luma[y * w + x], u[c], v[c]]);
        }
    }
    let mut encoder = Encoder::new_file(path, QUALITY)?;
    encoder.set_sampling_factor(SamplingFactor::F_2_2);
    Ok(encoder.encode(&pixels, ycc.width.try_into()?, ycc.height.try_into()?, ColorType::Ycbcr)?)
}

fn read(path: &Path, width: u32, height: u32) -> Result<I420> {
    let ycc = read_one_frame(path, None, &[Pixel::YUVJ420P, Pixel::YUV420P])?;
    ensure!((ycc.width, ycc.height) == (width, height), "{} is not {width}x{height}", path.display());
    Ok(convert(&ycc, &BT601_FULL, &BT709_LIMITED))
}
