//! The image formats a frame is written as, each losing as little as it
//! allows ([`FORMATS`], in this order):
//!
//! - **raw**: the three planes one after the other, no header: the reader
//!   needs the size from elsewhere (exact).
//! - **TIFF**: YCbCr 4:2:0, as FFmpeg's encoder writes `yuv420p`, Deflate
//!   compressed: the smallest of FFmpeg's compressions (none, PackBits, LZW,
//!   Deflate), all lossless (exact).
//! - **AVIF**: an AV1 still image from SVT-AV1 in lossless, still-picture
//!   mode, in FFmpeg's AVIF muxer, read back with dav1d (exact).
//! - **[`FFV1`]**: one lossless frame (version 3, sliced) in a Matroska
//!   file, with FFmpeg's encoder and decoder (exact).
//! - **PNG**: PNG holds only RGB, so 16-bit RGB (see [`color`]): the chroma
//!   repeated over its 2x2 block, so the way back averages it out again, and
//!   16 bits so that rounding loses next to nothing; what is lost is mostly
//!   the colors outside the RGB cube, clipped. Tagged sRGB, as the PNGs the
//!   video was made from.
//! - **JPEG**: a standard JFIF file any viewer shows in the right colors.
//!   JFIF's YCbCr is BT.601 in full range, so the frame is converted to it,
//!   then written at quality 100 (every quantization step 1) in 4:2:0, the U
//!   and V planes passed as they are, with no RGB or resampling in the
//!   encoder. Read back with FFmpeg's decoder, which also gives the 4:2:0
//!   planes as they are, then converted back. What is lost: the two
//!   conversions' rounding and the DCT's.

use std::{
    fs::{self, File},
    io::{BufWriter, Cursor},
    path::Path,
};

use anyhow::{Context, Result, ensure};
use ffmpeg_the_third::{Dictionary, util::format::Pixel};
use jpeg_encoder::{ColorType, Encoder, SamplingFactor};

use super::{
    AV1_DECODER, I420,
    color::{self, BT601_FULL, BT709_LIMITED},
    read_one_frame, write_one_frame,
};

/// An image format for one frame: its names and its two functions.
pub struct Format {
    /// The format's folder under `output/` and its name in the reports.
    pub name: &'static str,
    /// The extension of its files, without the dot.
    pub extension: &'static str,
    /// Whether an image gives its frame back exactly.
    pub exact: bool,
    /// What the reports say about the settings.
    pub settings: &'static str,
    /// Writes an image to a path.
    pub write: fn(&I420, &Path) -> Result<()>,
    /// Reads back the image at a path, a frame of the given width and
    /// height (for formats without a header).
    pub read: fn(&Path, u32, u32) -> Result<I420>,
}

impl Format {
    /// The file name of the image of frame `k`: `frame_<k>.<extension>`.
    pub fn file_name(&self, k: usize) -> String {
        format!("frame_{k:06}.{}", self.extension)
    }
}

/// Every format: the exact ones, then the converted ones.
pub const FORMATS: [Format; 6] = [RAW, TIFF, AVIF, FFV1, PNG, JPEG];

/// Images written and read at the same time when a whole sequence is done
/// at once: one image leaves most cores idle.
pub const WORKERS: usize = 4;

pub const FFV1: Format = Format {
    name: "ffv1",
    extension: "mkv",
    exact: true,
    settings: "FFV1 version 3, 16 slices, in Matroska",
    write: |image, path| {
        let mut options = Dictionary::new();
        options.set("level", "3");
        // Slices, each coded on its own thread.
        options.set("slices", "16");
        write_one_frame(image, path, "ffv1", options)
    },
    read: |path, _, _| read_one_frame(path, None, &[Pixel::YUV420P]),
};

const RAW: Format = Format {
    name: "raw",
    extension: "yuv",
    exact: true,
    settings: "I420 planes, no header",
    write: |image, path| Ok(fs::write(path, &image.data)?),
    read: |path, width, height| I420::from_planes(width, height, fs::read(path)?),
};

const TIFF: Format = Format {
    name: "tiff",
    extension: "tiff",
    exact: true,
    settings: "YCbCr 4:2:0 as it is, deflate compression; FFmpeg's encoder and decoder",
    write: |image, path| {
        let mut options = Dictionary::new();
        options.set("compression_algo", "deflate");
        write_one_frame(image, path, "tiff", options)
    },
    read: |path, _, _| read_one_frame(path, None, &[Pixel::YUV420P]),
};

const AVIF: Format = Format {
    name: "avif",
    extension: "avif",
    exact: true,
    // A lossless image is the same at every preset (checked at 8K with
    // presets 8 and 10: same size, same time).
    settings: "SVT-AV1 lossless, still picture, preset 8; dav1d",
    write: |image, path| {
        let mut options = Dictionary::new();
        options.set("preset", "8");
        options.set("svtav1-params", "lossless=1:avif=1");
        write_one_frame(image, path, "libsvtav1", options)
    },
    read: |path, _, _| read_one_frame(path, Some(AV1_DECODER), &[Pixel::YUV420P]),
};

const PNG: Format = Format {
    name: "png",
    extension: "png",
    exact: false,
    settings: "16-bit RGB, sRGB, chroma repeated over its 2x2 block; default compression",
    write: write_png,
    read: read_png,
};

fn write_png(image: &I420, path: &Path) -> Result<()> {
    let bytes: Vec<u8> = color::to_rgb16(image, &BT709_LIMITED).iter().flat_map(|s| s.to_be_bytes()).collect();
    let mut encoder = png::Encoder::new(BufWriter::new(File::create(path)?), image.width, image.height);
    encoder.set_color(png::ColorType::Rgb);
    encoder.set_depth(png::BitDepth::Sixteen);
    encoder.set_source_srgb(png::SrgbRenderingIntent::Perceptual);
    let mut writer = encoder.write_header()?;
    writer.write_image_data(&bytes)?;
    Ok(writer.finish()?)
}

fn read_png(path: &Path, width: u32, height: u32) -> Result<I420> {
    let mut reader = png::Decoder::new(Cursor::new(fs::read(path)?)).read_info()?;
    let mut buffer = vec![0; reader.output_buffer_size().context("PNG too large")?];
    let info = reader.next_frame(&mut buffer)?;
    ensure!(
        (info.color_type, info.bit_depth, info.width, info.height)
            == (png::ColorType::Rgb, png::BitDepth::Sixteen, width, height),
        "{} is not a 16-bit RGB image of {width}x{height}",
        path.display()
    );
    buffer.truncate(info.buffer_size());
    let rgb: Vec<u16> = buffer.as_chunks::<2>().0.iter().map(|&s| u16::from_be_bytes(s)).collect();
    Ok(color::from_rgb16(width, height, &rgb, &BT709_LIMITED))
}

/// 100: every quantization step 1, the least JPEG can lose.
const JPEG_QUALITY: u8 = 100;

const JPEG: Format = Format {
    name: "jpeg",
    extension: "jpg",
    exact: false,
    settings: "JFIF, BT.601 full range, 4:2:0, quality 100; FFmpeg's decoder",
    write: write_jpeg,
    read: |path, width, height| {
        let ycc = read_one_frame(path, None, &[Pixel::YUVJ420P, Pixel::YUV420P])?;
        ensure!((ycc.width, ycc.height) == (width, height), "{} is not {width}x{height}", path.display());
        Ok(color::convert(&ycc, &BT601_FULL, &BT709_LIMITED))
    },
};

fn write_jpeg(image: &I420, path: &Path) -> Result<()> {
    let ycc = color::convert(image, &BT709_LIMITED, &BT601_FULL);
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
    let mut encoder = Encoder::new_file(path, JPEG_QUALITY)?;
    encoder.set_sampling_factor(SamplingFactor::F_2_2);
    Ok(encoder.encode(&pixels, ycc.width.try_into()?, ycc.height.try_into()?, ColorType::Ycbcr)?)
}
