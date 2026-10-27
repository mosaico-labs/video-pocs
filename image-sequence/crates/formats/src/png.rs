//! PNG, for comparison: PNG holds only RGB, so the frame is converted, as
//! closely as PNG allows. 16-bit RGB (see [`super::color`]): the chroma
//! repeated over its 2x2 block, so the way back averages it out again, and
//! 16 bits so that rounding loses next to nothing; what is lost is mostly
//! the colors outside the RGB cube, clipped. Tagged sRGB, as the PNGs the
//! video was made from.

use std::{
    fs::{self, File},
    io::{BufWriter, Cursor},
    path::Path,
};

use anyhow::{Context, Result, ensure};

use super::{
    Format, I420,
    color::{BT709_LIMITED, from_rgb16, to_rgb16},
};

pub const FORMAT: Format = Format {
    name: "png",
    extension: "png",
    exact: false,
    settings,
    write,
    read,
};

fn settings() -> String {
    "16-bit RGB, sRGB, chroma repeated over its 2x2 block; default compression".to_string()
}

fn write(image: &I420, path: &Path) -> Result<()> {
    let bytes: Vec<u8> = to_rgb16(image, &BT709_LIMITED).iter().flat_map(|s| s.to_be_bytes()).collect();
    let mut encoder = ::png::Encoder::new(BufWriter::new(File::create(path)?), image.width, image.height);
    encoder.set_color(::png::ColorType::Rgb);
    encoder.set_depth(::png::BitDepth::Sixteen);
    encoder.set_source_srgb(::png::SrgbRenderingIntent::Perceptual);
    let mut writer = encoder.write_header()?;
    writer.write_image_data(&bytes)?;
    Ok(writer.finish()?)
}

fn read(path: &Path, width: u32, height: u32) -> Result<I420> {
    let mut reader = ::png::Decoder::new(Cursor::new(fs::read(path)?)).read_info()?;
    let mut buffer = vec![0; reader.output_buffer_size().context("PNG too large")?];
    let info = reader.next_frame(&mut buffer)?;
    ensure!(
        (info.color_type, info.bit_depth, info.width, info.height)
            == (::png::ColorType::Rgb, ::png::BitDepth::Sixteen, width, height),
        "{} is not a 16-bit RGB image of {width}x{height}",
        path.display()
    );
    buffer.truncate(info.buffer_size());
    let rgb: Vec<u16> = buffer.as_chunks::<2>().0.iter().map(|&s| u16::from_be_bytes(s)).collect();
    Ok(from_rgb16(width, height, &rgb, &BT709_LIMITED))
}
