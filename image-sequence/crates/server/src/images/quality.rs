//! How the images of the formats that are not exact look against their
//! frames, with butteraugli: a perceptual distance, the same metric (and the
//! same two norms) the quality tool measures the video with. Runs after
//! [`super::extract`], on the images it wrote:
//!
//! ```text
//! <video dir>/<topic>.images-quality.json   butteraugli of each image, see Report
//! ```
//!
//! The exact formats are not measured: their images are their frames, byte
//! for byte, so their distance is 0. Butteraugli compares linear RGB: the
//! frame and the image read back are converted the same way (see
//! [`formats::color::to_linear_rgb`]), so they differ only by what the format
//! lost. At 8K it needs ~9 GiB, so one image at a time is measured, on every
//! core.

use std::{fs, path::Path, time::Instant};

use anyhow::{Context, Result, anyhow};
use butteraugli::{ButteraugliParams, Img, RGB, butteraugli_linear};
use serde::Serialize;

use formats::{FORMATS, I420, color, ffmpeg};

use super::image_path;

/// The butteraugli distance of one image from its frame.
#[derive(Serialize)]
pub struct ImageQuality {
    /// Max-norm: the most visible difference, anywhere in the image.
    pub butteraugli_max: f64,
    /// 3-norm: the differences over the whole image.
    pub butteraugli_3norm: f64,
}

/// The images of one format.
#[derive(Serialize)]
pub struct FormatQuality {
    pub format: &'static str,
    /// Measuring all its images, reading them included.
    pub seconds: f64,
    /// One per frame, in order.
    pub images: Vec<ImageQuality>,
}

/// What [`measure`] writes to `<topic>.images-quality.json`.
#[derive(Serialize)]
pub struct Report {
    pub video: String,
    pub frames: usize,
    pub formats: Vec<FormatQuality>,
}

fn linear(frame: &I420) -> Img<Vec<RGB<f32>>> {
    let pixels = color::to_linear_rgb(frame, &color::BT709_LIMITED).into_iter().map(|[r, g, b]| RGB::new(r, g, b));
    Img::new(pixels.collect(), frame.width() as usize, frame.height() as usize)
}

/// Decodes `video` with dav1d and measures every image of every format that
/// is not exact against its frame, then writes the report next to the video
/// and returns it.
pub fn measure(video: &Path) -> Result<Report> {
    let measured: Vec<_> = FORMATS.iter().filter(|f| !f.exact).collect();
    let mut formats: Vec<FormatQuality> =
        measured.iter().map(|f| FormatQuality { format: f.name, seconds: 0.0, images: Vec::new() }).collect();
    let mut k = 0;
    ffmpeg::decode_video(video, Some(ffmpeg::AV1_DECODER), |decoded, _| {
        let frame = I420::from_frame(decoded)?;
        let reference = linear(&frame);
        for (format, quality) in measured.iter().zip(&mut formats) {
            let started = Instant::now();
            let path = image_path(video, format, k);
            let image = (format.read)(&path, frame.width(), frame.height())
                .with_context(|| format!("reading {} (run --extract-images first)", path.display()))?;
            let scores = butteraugli_linear(reference.as_ref(), linear(&image).as_ref(), &ButteraugliParams::default())
                .map_err(|e| anyhow!("butteraugli on {}: {e:?}", path.display()))?;
            quality.seconds += started.elapsed().as_secs_f64();
            quality.images.push(ImageQuality { butteraugli_max: scores.score, butteraugli_3norm: scores.pnorm_3 });
            eprintln!("frame {k} {}: butteraugli max-norm {:.4}, 3-norm {:.4}", format.name, scores.score,
                      scores.pnorm_3);
        }
        k += 1;
        Ok(())
    })?;
    let report = Report { video: video.display().to_string(), frames: k, formats };
    let path = video.with_extension("images-quality.json");
    fs::write(&path, serde_json::to_vec_pretty(&report)?).with_context(|| format!("writing {}", path.display()))?;
    Ok(report)
}
