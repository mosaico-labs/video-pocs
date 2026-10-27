//! The images of a sequence: reading `timestamps.csv`, decoding PNG and
//! converting RGB to the YUV 4:2:0 frames the video is encoded from (and to
//! YUV 4:4:4, only to measure what that conversion loses).
//!
//! The server and the quality tool both convert with [`rgb_to_yuv420`], so
//! the quality tool compares the decoded video with exactly the frames the
//! encoder received.

use std::{
    fs,
    io::Cursor,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;

/// One row of a `timestamps.csv`.
#[derive(Debug, Clone)]
pub struct TimedImage {
    /// The image path as written in the CSV, relative to the repository root.
    pub image: String,
    /// The same path, resolved on disk.
    pub path: PathBuf,
    /// Capture time, in nanoseconds since the Unix epoch.
    pub timestamp_ns: i64,
}

/// The rows of an `image,timestamp_ns` CSV written by `build-timestamps.py`,
/// as they are: no image needs to exist (see [`read_timestamps`]).
pub fn read_capture_times(csv: &Path) -> Result<Vec<(String, i64)>> {
    let text = fs::read_to_string(csv).with_context(|| format!("reading {}", csv.display()))?;
    let mut lines = text.lines();
    ensure!(
        lines.next() == Some("image,timestamp_ns"),
        "{}: expected the header image,timestamp_ns",
        csv.display()
    );
    let rows: Vec<(String, i64)> = lines
        .enumerate()
        .map(|(i, line)| {
            let (image, timestamp) = line
                .split_once(',')
                .with_context(|| format!("{} line {}: expected image,timestamp_ns", csv.display(), i + 2))?;
            let timestamp = timestamp
                .parse()
                .with_context(|| format!("{} line {}: bad timestamp {timestamp:?}", csv.display(), i + 2))?;
            Ok((image.to_string(), timestamp))
        })
        .collect::<Result<_>>()?;
    Ok(rows)
}

/// Reads an `image,timestamp_ns` CSV written by `build-timestamps.py`.
///
/// The image paths are relative to the repository root, which is found as
/// the closest folder above the CSV that holds the first image.
pub fn read_timestamps(csv: &Path) -> Result<Vec<TimedImage>> {
    let rows = read_capture_times(csv)?;
    let first = &rows
        .first()
        .with_context(|| format!("{} has no images", csv.display()))?
        .0;

    let csv = csv
        .canonicalize()
        .with_context(|| format!("resolving {}", csv.display()))?;
    let root = csv
        .ancestors()
        .find(|dir| dir.join(first).is_file())
        .with_context(|| format!("{first} not found above {}: build the datasets first", csv.display()))?;

    Ok(rows
        .into_iter()
        .map(|(image, timestamp_ns)| TimedImage {
            path: root.join(&image),
            image,
            timestamp_ns,
        })
        .collect())
}

/// An 8-bit RGB image, 3 bytes per pixel, rows without padding.
pub struct Rgb8 {
    pub width: usize,
    pub height: usize,
    pub data: Vec<u8>,
}

/// Decodes an 8-bit RGB or RGBA PNG. The alpha channel is dropped: the
/// dataset images are opaque, and the video has no alpha.
pub fn decode_png(bytes: &[u8]) -> Result<Rgb8> {
    let mut reader = png::Decoder::new(Cursor::new(bytes)).read_info()?;
    let mut buffer = vec![0; reader.output_buffer_size().context("PNG too large")?];
    let info = reader.next_frame(&mut buffer)?;
    ensure!(
        info.bit_depth == png::BitDepth::Eight,
        "only 8-bit PNGs are supported, not {:?}",
        info.bit_depth
    );
    let (width, height) = (info.width as usize, info.height as usize);
    buffer.truncate(info.buffer_size());

    let data = match info.color_type {
        png::ColorType::Rgb if info.line_size == width * 3 => buffer,
        png::ColorType::Rgba => buffer
            .chunks_exact(info.line_size)
            .flat_map(|row| {
                row[..width * 4]
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .flat_map(|px| [px[0], px[1], px[2]])
            })
            .collect(),
        other => bail!("only RGB and RGBA PNGs are supported, not {other:?}"),
    };
    Ok(Rgb8 { width, height, data })
}

/// Reads and decodes a PNG file.
pub fn load_png(path: &Path) -> Result<Rgb8> {
    let bytes = fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    decode_png(&bytes).with_context(|| format!("decoding {}", path.display()))
}

/// A YUV 4:2:0 frame of 8 or 10 bits per sample: full-size Y plane, U and
/// V at half the width and half the height. Samples are stored in `u16`
/// whatever the bit depth, and rows have no padding.
pub struct Yuv420 {
    pub width: usize,
    pub height: usize,
    pub bit_depth: u8,
    pub y: Vec<u16>,
    pub u: Vec<u16>,
    pub v: Vec<u16>,
}

// BT.709 luma coefficients.
const KR: f32 = 0.2126;
const KB: f32 = 0.0722;
const KG: f32 = 1.0 - KR - KB;

/// Converts RGB to YUV 4:2:0 with the BT.709 matrix in limited range, the
/// usual format of SDR video: at 8 bits Y is 16–235 and U, V 16–240; at 10
/// bits the same ranges times 4 (Y 64–940, U, V 64–960).
///
/// Each U and V sample is the average of the 2x2 pixels it covers. The
/// width and height must be even.
pub fn rgb_to_yuv420(rgb: &Rgb8, bit_depth: u8) -> Result<Yuv420> {
    let (width, height) = (rgb.width, rgb.height);
    ensure!(
        width % 2 == 0 && height % 2 == 0,
        "YUV 4:2:0 needs an even size, not {width}x{height}"
    );
    ensure!(
        matches!(bit_depth, 8 | 10),
        "only 8 and 10 bits are supported, not {bit_depth}"
    );
    let sample = sampler(bit_depth);

    let chroma_width = width / 2;
    let mut y = vec![0; width * height];
    let mut u = vec![0; chroma_width * (height / 2)];
    let mut v = vec![0; chroma_width * (height / 2)];

    // Each task converts two image rows: their Y rows and one U and V row.
    y.par_chunks_mut(2 * width)
        .zip(u.par_chunks_mut(chroma_width).zip(v.par_chunks_mut(chroma_width)))
        .enumerate()
        .for_each(|(pair, (y_rows, (u_row, v_row)))| {
            for cx in 0..chroma_width {
                let (mut cb, mut cr) = (0.0, 0.0);
                for (dy, dx) in [(0, 0), (0, 1), (1, 0), (1, 1)] {
                    let x = 2 * cx + dx;
                    let (luma, pb, pr) = ycbcr(&rgb.data[((2 * pair + dy) * width + x) * 3..][..3]);
                    y_rows[dy * width + x] = sample(16.0 + 219.0 * luma);
                    cb += pb;
                    cr += pr;
                }
                u_row[cx] = sample(128.0 + 224.0 * cb / 4.0);
                v_row[cx] = sample(128.0 + 224.0 * cr / 4.0);
            }
        });
    Ok(Yuv420 {
        width,
        height,
        bit_depth,
        y,
        u,
        v,
    })
}

/// A YUV 4:4:4 frame: like [`Yuv420`], but U and V at full size, one
/// sample per pixel.
pub struct Yuv444 {
    pub width: usize,
    pub height: usize,
    pub bit_depth: u8,
    pub y: Vec<u16>,
    pub u: Vec<u16>,
    pub v: Vec<u16>,
}

/// Converts RGB to YUV 4:4:4 with the same matrix and range as
/// [`rgb_to_yuv420`], without averaging the color: what a 4:4:4 video would
/// be encoded from. No video is: SVT-AV1 encodes only 4:2:0. The quality
/// tool uses it to measure what this conversion alone loses.
pub fn rgb_to_yuv444(rgb: &Rgb8, bit_depth: u8) -> Result<Yuv444> {
    ensure!(
        matches!(bit_depth, 8 | 10),
        "only 8 and 10 bits are supported, not {bit_depth}"
    );
    let sample = sampler(bit_depth);
    let (y, (u, v)): (Vec<u16>, (Vec<u16>, Vec<u16>)) = rgb
        .data
        .par_chunks_exact(3)
        .map(|pixel| {
            let (luma, cb, cr) = ycbcr(pixel);
            (
                sample(16.0 + 219.0 * luma),
                (sample(128.0 + 224.0 * cb), sample(128.0 + 224.0 * cr)),
            )
        })
        .unzip();
    Ok(Yuv444 {
        width: rgb.width,
        height: rgb.height,
        bit_depth,
        y,
        u,
        v,
    })
}

/// One RGB pixel as BT.709 luma (0 to 1) and color differences (−0.5 to 0.5).
fn ycbcr(pixel: &[u8]) -> (f32, f32, f32) {
    let [r, g, b] = [pixel[0], pixel[1], pixel[2]].map(|c| f32::from(c) / 255.0);
    let luma = KR * r + KG * g + KB * b;
    (luma, (b - luma) / (2.0 * (1.0 - KB)), (r - luma) / (2.0 * (1.0 - KR)))
}

/// Rounds an 8-bit-scale value to a sample of `bit_depth` bits.
fn sampler(bit_depth: u8) -> impl Fn(f32) -> u16 + Sync {
    let scale = f32::from(1u16 << (bit_depth - 8));
    let max = f32::from((1u16 << bit_depth) - 1);
    move |value| (value * scale).round().clamp(0.0, max) as u16
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yuv444_keeps_the_color_of_each_pixel() {
        // Two pixels in a row: red and blue. 4:2:0 averages their color,
        // 4:4:4 keeps both, and both agree on the luma.
        let rgb = Rgb8 {
            width: 2,
            height: 2,
            data: [[255, 0, 0], [0, 0, 255], [255, 0, 0], [0, 0, 255]].concat(),
        };
        let full = rgb_to_yuv444(&rgb, 8).unwrap();
        let half = rgb_to_yuv420(&rgb, 8).unwrap();
        assert_eq!(full.y, half.y);
        assert_eq!(full.u.len(), 4);
        assert_ne!(full.v[0], full.v[1]);
        // Rounded once from the mean, not from the two rounded samples.
        let mean = (i32::from(full.v[0]) + i32::from(full.v[1])) / 2;
        assert!((i32::from(half.v[0]) - mean).abs() <= 1);
    }
}
