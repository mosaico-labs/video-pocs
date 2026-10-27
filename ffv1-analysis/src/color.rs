//! Color conversions. The frames are YUV 4:2:0 with the BT.709 matrix in
//! limited range, encoded from 8-bit RGB PNGs ([`from_rgb8`]). The formats
//! that can't hold them as they are need converting: PNG holds RGB, JPEG
//! holds YCbCr with the BT.601 matrix in full range. Each conversion loses
//! as little as it can:
//!
//! - a U or V sample is repeated over the 2x2 pixels it covers, and the way
//!   back averages each 2x2 block, so a sample that went out comes back;
//! - the math is in `f64` and nothing is clipped before the target's integer
//!   samples (8-bit YCbCr, or 16-bit RGB, which loses only the colors outside
//!   the RGB cube).

use anyhow::{Result, ensure};
use rayon::prelude::*;

use super::I420;

/// A YCbCr encoding: a matrix (its luma weights of red and blue) and a range.
pub struct Ycc {
    kr: f64,
    kb: f64,
    /// Full range (Y 0–255, U and V 0–255 around 128) or limited (Y 16–235,
    /// U and V 16–240).
    full: bool,
}

/// The frames': BT.709 in limited range.
pub const BT709_LIMITED: Ycc = Ycc { kr: 0.2126, kb: 0.0722, full: false };

/// JPEG's (JFIF): BT.601 in full range.
pub const BT601_FULL: Ycc = Ycc { kr: 0.299, kb: 0.114, full: true };

impl Ycc {
    fn scales(&self) -> (f64, f64, f64) {
        // (luma offset, luma scale, chroma scale), in 8-bit levels.
        if self.full { (0.0, 255.0, 255.0) } else { (16.0, 219.0, 224.0) }
    }

    /// The RGB (0 to 1, not clipped) of a Y, U, V sample.
    fn rgb(&self, y: u8, u: u8, v: u8) -> [f64; 3] {
        let (offset, luma, chroma) = self.scales();
        let yn = (f64::from(y) - offset) / luma;
        let cb = (f64::from(u) - 128.0) / chroma;
        let cr = (f64::from(v) - 128.0) / chroma;
        let r = yn + 2.0 * (1.0 - self.kr) * cr;
        let b = yn + 2.0 * (1.0 - self.kb) * cb;
        let g = (yn - self.kr * r - self.kb * b) / (1.0 - self.kr - self.kb);
        [r, g, b]
    }

    /// The Y, U, V (8-bit levels, not rounded) of an RGB color.
    fn ycc(&self, [r, g, b]: [f64; 3]) -> [f64; 3] {
        let (offset, luma, chroma) = self.scales();
        let yn = self.kr * r + (1.0 - self.kr - self.kb) * g + self.kb * b;
        let cb = (b - yn) / (2.0 * (1.0 - self.kb));
        let cr = (r - yn) / (2.0 * (1.0 - self.kr));
        [offset + luma * yn, 128.0 + chroma * cb, 128.0 + chroma * cr]
    }
}

fn level(value: f64) -> u8 {
    value.round().clamp(0.0, 255.0) as u8
}

/// Builds a frame two rows at a time, in parallel: `pixel(x, y)` gives the
/// Y, U, V (8-bit levels) of a pixel, U and V averaged over each 2x2 block.
fn build(width: u32, height: u32, pixel: impl Fn(usize, usize) -> [f64; 3] + Sync) -> I420 {
    let (w, h) = (width as usize, height as usize);
    let (cw, ch) = (w / 2, h / 2);
    let mut data = vec![0u8; I420::size(width, height)];
    let (luma, chroma) = data.split_at_mut(w * h);
    let (u_plane, v_plane) = chroma.split_at_mut(cw * ch);
    luma.par_chunks_mut(2 * w)
        .zip(u_plane.par_chunks_mut(cw).zip(v_plane.par_chunks_mut(cw)))
        .enumerate()
        .for_each(|(row, (y_rows, (u_row, v_row)))| {
            for cx in 0..cw {
                let (mut cb, mut cr) = (0.0, 0.0);
                for (dy, dx) in [(0, 0), (0, 1), (1, 0), (1, 1)] {
                    let x = 2 * cx + dx;
                    let [y, u, v] = pixel(x, 2 * row + dy);
                    y_rows[dy * w + x] = level(y);
                    cb += u;
                    cr += v;
                }
                u_row[cx] = level(cb / 4.0);
                v_row[cx] = level(cr / 4.0);
            }
        });
    I420 { width, height, data }
}

/// The Y, U, V samples of pixel (x, y): U and V those of its 2x2 block.
fn samples(frame: &I420, x: usize, y: usize) -> (u8, u8, u8) {
    let (w, h) = (frame.width as usize, frame.height as usize);
    let c = (y / 2) * (w / 2) + x / 2;
    let chroma = w * h;
    (frame.data[y * w + x], frame.data[chroma + c], frame.data[chroma + (w / 2) * (h / 2) + c])
}

/// An 8-bit RGB image, `channels` samples a pixel (3, or 4 with an alpha
/// that is dropped), as a frame in the frames' encoding: the PNGs as the
/// video is encoded from them. The math is in `f32`, U and V the mean of the
/// 2x2 pixels they cover. The width and height must be even.
pub fn from_rgb8(width: u32, height: u32, channels: usize, rgb: &[u8]) -> Result<I420> {
    const KR: f32 = 0.2126;
    const KB: f32 = 0.0722;
    const KG: f32 = 1.0 - KR - KB;
    ensure!(width.is_multiple_of(2) && height.is_multiple_of(2), "YUV 4:2:0 needs an even size, not {width}x{height}");
    let (w, h) = (width as usize, height as usize);
    ensure!(rgb.len() == w * h * channels, "{} bytes are not a {width}x{height} image", rgb.len());
    let sample = |value: f32| value.round().clamp(0.0, 255.0) as u8;
    let cw = w / 2;
    let mut data = vec![0u8; I420::size(width, height)];
    let (luma, chroma) = data.split_at_mut(w * h);
    let (u_plane, v_plane) = chroma.split_at_mut(cw * (h / 2));
    luma.par_chunks_mut(2 * w)
        .zip(u_plane.par_chunks_mut(cw).zip(v_plane.par_chunks_mut(cw)))
        .enumerate()
        .for_each(|(row, (y_rows, (u_row, v_row)))| {
            for cx in 0..cw {
                let (mut cb, mut cr) = (0.0, 0.0);
                for (dy, dx) in [(0, 0), (0, 1), (1, 0), (1, 1)] {
                    let x = 2 * cx + dx;
                    let pixel = &rgb[((2 * row + dy) * w + x) * channels..];
                    let [r, g, b] = [pixel[0], pixel[1], pixel[2]].map(|c| f32::from(c) / 255.0);
                    let yn = KR * r + KG * g + KB * b;
                    y_rows[dy * w + x] = sample(16.0 + 219.0 * yn);
                    cb += (b - yn) / (2.0 * (1.0 - KB));
                    cr += (r - yn) / (2.0 * (1.0 - KR));
                }
                u_row[cx] = sample(128.0 + 224.0 * cb / 4.0);
                v_row[cx] = sample(128.0 + 224.0 * cr / 4.0);
            }
        });
    Ok(I420 { width, height, data })
}

/// `frame`, in `from`'s encoding, converted to `to`'s, through RGB.
pub fn convert(frame: &I420, from: &Ycc, to: &Ycc) -> I420 {
    build(frame.width, frame.height, |x, y| {
        let (luma, u, v) = samples(frame, x, y);
        to.ycc(from.rgb(luma, u, v))
    })
}

/// `frame`, in `from`'s encoding, as 16-bit RGB, three samples a pixel, the
/// colors outside the RGB cube clipped to it.
pub fn to_rgb16(frame: &I420, from: &Ycc) -> Vec<u16> {
    let w = frame.width as usize;
    let mut rgb = vec![0u16; 3 * w * frame.height as usize];
    rgb.par_chunks_mut(3 * w).enumerate().for_each(|(y, row)| {
        for (x, pixel) in row.as_chunks_mut::<3>().0.iter_mut().enumerate() {
            let (luma, u, v) = samples(frame, x, y);
            for (sample, value) in pixel.iter_mut().zip(from.rgb(luma, u, v)) {
                *sample = (value.clamp(0.0, 1.0) * 65535.0).round() as u16;
            }
        }
    });
    rgb
}

/// An sRGB value (0 to 1) as linear light: the sRGB transfer curve undone.
fn linear(c: f64) -> f32 {
    (if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) }) as f32
}

/// `frame`, in `from`'s encoding, as linear RGB (0 to 1), for butteraugli:
/// clipped to the RGB cube, then the sRGB transfer curve undone.
pub fn to_linear_rgb(frame: &I420, from: &Ycc) -> Vec<[f32; 3]> {
    let (w, h) = (frame.width as usize, frame.height as usize);
    (0..w * h)
        .into_par_iter()
        .map(|i| {
            let (luma, u, v) = samples(frame, i % w, i / w);
            from.rgb(luma, u, v).map(|c| linear(c.clamp(0.0, 1.0)))
        })
        .collect()
}

/// 16-bit RGB, three samples a pixel, as a frame in `to`'s encoding.
pub fn from_rgb16(width: u32, height: u32, rgb: &[u16], to: &Ycc) -> I420 {
    let w = width as usize;
    build(width, height, |x, y| {
        let pixel = &rgb[3 * (y * w + x)..][..3];
        to.ycc([0, 1, 2].map(|c| f64::from(pixel[c]) / 65535.0))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A 4x2 frame of the frames' encoding, inside the RGB cube.
    fn frame() -> I420 {
        let data = [[60, 70, 80, 90, 100, 110, 120, 130].as_slice(), &[120, 125], &[130, 135]].concat();
        I420 { width: 4, height: 2, data }
    }

    #[test]
    fn rgb16_gives_the_frame_back() {
        let frame = frame();
        let rgb = to_rgb16(&frame, &BT709_LIMITED);
        assert!(from_rgb16(4, 2, &rgb, &BT709_LIMITED) == frame);
    }

    #[test]
    fn a_matrix_and_back_stays_within_a_level() {
        let frame = frame();
        let back = convert(&convert(&frame, &BT709_LIMITED, &BT601_FULL), &BT601_FULL, &BT709_LIMITED);
        assert!(frame.data.iter().zip(&back.data).all(|(a, b)| a.abs_diff(*b) <= 1));
    }
}
