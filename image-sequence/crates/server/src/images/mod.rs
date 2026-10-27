//! Turns an AV1 video back into a sequence of images, one per frame, to
//! compare the image formats the server could stream them to a client in:
//!
//! ```text
//! <video dir>/images/<format>/frame_<k>.<ext>   one image per frame
//! <video dir>/<topic>.images.json               sizes and times, see Report
//! ```
//!
//! The exact formats keep the frames as dav1d decodes them, 8-bit YUV
//! 4:2:0, with no conversion to RGB, and losslessly: each image is decoded
//! back right after it is written and must give the frame's exact bytes, or
//! the extraction fails. PNG and JPEG, for comparison, can't hold the frames
//! as they are: they are converted, losing as little as each allows (see
//! [`formats::color`]), and what each image read back differs from its frame is
//! measured instead ([`Loss`]). In every format, nothing is dropped or
//! repeated: one image per frame.
//!
//! The formats are the `formats` crate's, shared with the client.
//!
//! The formats run one after the other on each frame, so their times don't
//! overlap. The times are of one image at a time, each format with the
//! threads it uses on its own. They include writing and reading the file,
//! which the OS keeps in its cache: the disk's speed is not measured.
//!
//! Once every frame is measured one at a time, each format's whole sequence
//! is written and read back again [`formats::WORKERS`] images at a time,
//! every image checked again, to measure the time of the sequence
//! ([`Parallel`]): one image leaves most cores idle. The time of one image
//! doesn't change: only how many are done at once.

pub mod quality;

use std::{
    fs,
    path::{Path, PathBuf},
    time::Instant,
};

use anyhow::{Context, Result, bail};
use formats::{FORMATS, Format, I420, WORKERS, ffmpeg};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

/// How a plane of an image read back differs from its frame's.
#[derive(Serialize)]
pub struct PlaneLoss {
    /// Mean squared error, in 8-bit levels.
    pub mse: f64,
    /// The largest difference of a sample, in 8-bit levels.
    pub max_error: u8,
    /// How many samples differ.
    pub changed: u64,
    pub samples: u64,
}

/// How an image read back differs from its frame, plane by plane.
#[derive(Serialize)]
pub struct Loss {
    pub y: PlaneLoss,
    pub u: PlaneLoss,
    pub v: PlaneLoss,
}

impl Loss {
    fn between(frame: &I420, image: &I420) -> Self {
        let (w, h) = (frame.width() as usize, frame.height() as usize);
        let (luma, chroma) = (w * h, (w / 2) * (h / 2));
        let plane = |range: std::ops::Range<usize>| {
            let (mut squares, mut max_error, mut changed) = (0u64, 0u8, 0u64);
            for (a, b) in frame.samples()[range.clone()].iter().zip(&image.samples()[range.clone()]) {
                let error = a.abs_diff(*b);
                squares += u64::from(error) * u64::from(error);
                max_error = max_error.max(error);
                changed += u64::from(error != 0);
            }
            let samples = range.len() as u64;
            PlaneLoss { mse: squares as f64 / samples as f64, max_error, changed, samples }
        };
        Self {
            y: plane(0..luma),
            u: plane(luma..luma + chroma),
            v: plane(luma + chroma..luma + 2 * chroma),
        }
    }
}

/// One image written and read back.
#[derive(Serialize)]
pub struct ImageTimes {
    pub bytes: u64,
    /// From the frame in memory to the file written.
    pub encode_seconds: f64,
    /// From the file to the frame in memory.
    pub decode_seconds: f64,
    /// What the image lost, for a format that is not exact.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub loss: Option<Loss>,
}

/// The whole sequence of a format written, then read back, `workers`
/// ([`formats::WORKERS`]) images at a time.
#[derive(Serialize)]
pub struct Parallel {
    pub workers: usize,
    /// Writing all the images, wall time.
    pub encode_seconds: f64,
    /// Reading them all back, wall time.
    pub decode_seconds: f64,
}

/// The images of one format.
#[derive(Serialize)]
pub struct FormatReport {
    pub format: &'static str,
    /// Whether every image gave its frame back exactly (checked).
    pub exact: bool,
    pub settings: String,
    pub dir: String,
    pub bytes: u64,
    pub encode_seconds: f64,
    pub decode_seconds: f64,
    /// One per frame, in order.
    pub images: Vec<ImageTimes>,
    /// The sequence with several images at a time.
    pub parallel: Parallel,
}

/// Checks `image`, read back from `path`, against `frame`, the `k`-th: its
/// size, and its samples for an exact format, or what it lost otherwise.
fn check(format: &Format, frame: &I420, image: &I420, path: &Path, k: usize) -> Result<Option<Loss>> {
    if (image.width(), image.height()) != (frame.width(), frame.height()) {
        bail!("{} is {}x{}, not {}x{}", path.display(), image.width(), image.height(), frame.width(),
              frame.height());
    }
    Ok(match format.exact {
        true if image != frame => bail!("{} does not give back frame {k} exactly", path.display()),
        true => None,
        false => Some(Loss::between(frame, image)),
    })
}

/// Writes every frame as `format` into `dir`, [`WORKERS`] images at a
/// time, then reads them all back the same way, checking each.
fn in_parallel(format: &Format, dir: &Path, frames: &[I420]) -> Result<Parallel> {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(WORKERS).build()?;
    let path = |k: usize| dir.join(format.file_name(k));
    let started = Instant::now();
    pool.install(|| {
        frames.par_iter().enumerate().try_for_each(|(k, frame)| {
            (format.write)(frame, &path(k)).with_context(|| format!("writing frame {k} as {}", format.name))
        })
    })?;
    let encode_seconds = started.elapsed().as_secs_f64();
    let started = Instant::now();
    pool.install(|| {
        frames.par_iter().enumerate().try_for_each(|(k, frame)| -> Result<()> {
            let image = (format.read)(&path(k), frame.width(), frame.height())
                .with_context(|| format!("reading frame {k} back from {}", path(k).display()))?;
            check(format, frame, &image, &path(k), k).map(drop)
        })
    })?;
    Ok(Parallel { workers: WORKERS, encode_seconds, decode_seconds: started.elapsed().as_secs_f64() })
}

/// What [`extract`] writes to `<topic>.images.json`.
#[derive(Serialize)]
pub struct Report {
    pub video: String,
    pub video_bytes: u64,
    pub pixel_format: &'static str,
    pub width: u32,
    pub height: u32,
    pub frames: usize,
    /// The capture time of each frame, from the video: the first image's
    /// capture time plus the frame's timestamp, to the millisecond.
    pub capture_ns: Vec<i64>,
    /// Decoding the whole video with dav1d, file reading included.
    pub video_decode_seconds: f64,
    pub formats: Vec<FormatReport>,
}

/// Where the images of `video` go: `<video dir>/images/`.
pub fn images_dir(video: &Path) -> PathBuf {
    video.with_file_name("images")
}

/// The image of frame `k` of `video` in `format`.
pub fn image_path(video: &Path, format: &Format, k: usize) -> PathBuf {
    images_dir(video).join(format.name).join(format.file_name(k))
}

/// What later steps read back from `<topic>.images.json`: the frames' size
/// and capture times (from the video).
#[derive(Deserialize)]
pub struct Frames {
    pub width: u32,
    pub height: u32,
    pub capture_ns: Vec<i64>,
}

/// The frames of `video`, from the report [`extract`] wrote.
pub fn read_frames(video: &Path) -> Result<Frames> {
    let path = video.with_extension("images.json");
    let bytes = fs::read(&path).with_context(|| format!("reading {} (run --extract-images first)", path.display()))?;
    Ok(serde_json::from_slice(&bytes)?)
}

/// Decodes `video` with dav1d and writes every frame in every format of
/// [`FORMATS`], checking each image against the frame (or measuring what
/// it lost), then writes the report next to the video and returns it.
pub fn extract(video: &Path) -> Result<Report> {
    let started = Instant::now();
    let mut frames = Vec::new();
    let mut timestamps = Vec::new();
    let (input, time_base) = ffmpeg::decode_video(video, Some(ffmpeg::AV1_DECODER), |frame, pts| {
        frames.push(I420::from_frame(frame)?);
        timestamps.push(pts.context("decoded frame without timestamp")?);
        Ok(())
    })?;
    let video_decode_seconds = started.elapsed().as_secs_f64();
    let Some(first) = frames.first() else {
        bail!("{} has no frames", video.display());
    };
    let (width, height) = (first.width(), first.height());
    let start_ns = ffmpeg::capture_start_ns(&input, video)?;
    let capture_ns = timestamps.iter().map(|&pts| ffmpeg::capture_ns(start_ns, pts, time_base)).collect();

    let dirs: Vec<PathBuf> = FORMATS.iter().map(|f| images_dir(video).join(f.name)).collect();
    for dir in &dirs {
        fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    }
    let mut images: Vec<Vec<ImageTimes>> = FORMATS.iter().map(|_| Vec::new()).collect();
    for (k, frame) in frames.iter().enumerate() {
        for ((format, dir), times) in FORMATS.iter().zip(&dirs).zip(&mut images) {
            let path = dir.join(format.file_name(k));
            let encoding = Instant::now();
            (format.write)(frame, &path).with_context(|| format!("writing frame {k} as {}", format.name))?;
            let encode_seconds = encoding.elapsed().as_secs_f64();
            let decoding = Instant::now();
            let image = (format.read)(&path, width, height)
                .with_context(|| format!("reading frame {k} back from {}", path.display()))?;
            let decode_seconds = decoding.elapsed().as_secs_f64();
            let loss = check(format, frame, &image, &path, k)?;
            times.push(ImageTimes {
                bytes: fs::metadata(&path)?.len(),
                encode_seconds,
                decode_seconds,
                loss,
            });
        }
        eprintln!("frame {k}: written in {} formats", FORMATS.len());
    }
    let mut parallel = Vec::new();
    for (format, dir) in FORMATS.iter().zip(&dirs) {
        let pass = in_parallel(format, dir, &frames)?;
        eprintln!("{}: {} images, {} at a time, written in {:.2} s and read back in {:.2} s", format.name,
                  frames.len(), pass.workers, pass.encode_seconds, pass.decode_seconds);
        parallel.push(pass);
    }

    let report = Report {
        video: video.display().to_string(),
        video_bytes: fs::metadata(video)?.len(),
        pixel_format: "yuv420p",
        width,
        height,
        frames: frames.len(),
        capture_ns,
        video_decode_seconds,
        formats: FORMATS
            .iter()
            .zip(dirs)
            .zip(images)
            .zip(parallel)
            .map(|(((format, dir), images), parallel)| FormatReport {
                format: format.name,
                exact: format.exact,
                settings: (format.settings)(),
                dir: dir.display().to_string(),
                bytes: images.iter().map(|i| i.bytes).sum(),
                encode_seconds: images.iter().map(|i| i.encode_seconds).sum(),
                decode_seconds: images.iter().map(|i| i.decode_seconds).sum(),
                images,
                parallel,
            })
            .collect(),
    };
    let path = video.with_extension("images.json");
    fs::write(&path, serde_json::to_vec_pretty(&report)?).with_context(|| format!("writing {}", path.display()))?;
    Ok(report)
}
