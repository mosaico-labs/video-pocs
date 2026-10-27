//! Turns the FFV1 frames `prepare` wrote into raw, TIFF, AVIF, PNG and JPEG
//! images, each losing as little as it allows, and measures every format
//! against FFV1, the reference:
//!
//! ```text
//! ffv1/regular/batch_060/frame_<k>.mkv   the input: one FFV1 frame per file, from `prepare`
//! output/<format>/frame_<k>.<ext>        the images written from them
//! results/images.json                    every image's size and times, and what it lost
//! results/images-quality.json            butteraugli of every image of the converted formats
//! ```
//!
//! First every FFV1 frame is read. Then, for each frame, and each format in
//! turn (FFV1 too, written again, as the reference): write the image (timed), read it
//! back (timed), and compare it with the frame. The exact formats must give
//! it back byte for byte; for the others, what each plane lost is measured
//! ([`Loss`]). Then each format's whole sequence again, [`WORKERS`] images
//! at a time ([`Parallel`]): one image leaves most cores idle. Last, the
//! images of the formats that are not exact are read back once more and
//! measured with butteraugli (max-norm and 3-norm) in linear RGB.
//!
//! The times include writing and reading the file, which the OS keeps in its
//! cache: the disk's speed is not measured. `scripts/image_formats.py` makes
//! the charts and READMEs of `results/` from these files.

use std::{
    fs,
    path::{Path, PathBuf},
    time::Instant,
};

use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use ffv1_analysis::{
    I420, butteraugli, color,
    formats::{FFV1, FORMATS, Format, WORKERS},
    init,
};
use rayon::prelude::*;
use serde::Serialize;

#[derive(Parser)]
#[command(about = "Turns FFV1 frames into raw, TIFF, AVIF, FFV1, PNG and JPEG, and measures every format")]
struct Args {
    /// The FFV1 frames, one per `.mkv` file.
    #[arg(long, default_value = "ffv1/regular/batch_060")]
    input: PathBuf,
    /// Where the images are written, one folder per format.
    #[arg(long, default_value = "output")]
    output: PathBuf,
    /// Where the reports are written.
    #[arg(long, default_value = "results")]
    results: PathBuf,
}

/// How a plane of an image read back differs from its frame's.
#[derive(Serialize)]
struct PlaneLoss {
    /// Mean squared error, in 8-bit levels.
    mse: f64,
    /// The largest difference of a sample, in 8-bit levels.
    max_error: u8,
    /// How many samples differ.
    changed: u64,
    samples: u64,
}

/// How an image read back differs from its frame, plane by plane.
#[derive(Serialize)]
struct Loss {
    y: PlaneLoss,
    u: PlaneLoss,
    v: PlaneLoss,
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
        Self { y: plane(0..luma), u: plane(luma..luma + chroma), v: plane(luma + chroma..luma + 2 * chroma) }
    }
}

/// One image written and read back.
#[derive(Serialize)]
struct ImageTimes {
    bytes: u64,
    /// From the frame in memory to the file written.
    encode_seconds: f64,
    /// From the file to the frame in memory.
    decode_seconds: f64,
    /// What the image lost, for a format that is not exact.
    #[serde(skip_serializing_if = "Option::is_none")]
    loss: Option<Loss>,
}

/// The whole sequence of a format written, then read back, `workers`
/// images at a time.
#[derive(Serialize)]
struct Parallel {
    workers: usize,
    /// Writing all the images, wall time.
    encode_seconds: f64,
    /// Reading them all back, wall time.
    decode_seconds: f64,
}

/// The images of one format.
#[derive(Serialize)]
struct FormatReport {
    format: &'static str,
    /// Whether every image gave its frame back exactly (checked).
    exact: bool,
    settings: &'static str,
    dir: String,
    bytes: u64,
    encode_seconds: f64,
    decode_seconds: f64,
    /// One per frame, in order.
    images: Vec<ImageTimes>,
    parallel: Parallel,
}

/// `results/images.json`.
#[derive(Serialize)]
struct Report {
    /// The FFV1 frames the images are written from.
    input: String,
    pixel_format: &'static str,
    width: u32,
    height: u32,
    frames: usize,
    formats: Vec<FormatReport>,
}

/// Butteraugli of one image against its frame.
#[derive(Serialize)]
struct Quality {
    butteraugli_max: f64,
    butteraugli_3norm: f64,
}

/// The images of one format that is not exact, measured.
#[derive(Serialize)]
struct FormatQuality {
    format: &'static str,
    /// Measuring them all, reading each back included.
    seconds: f64,
    images: Vec<Quality>,
}

/// `results/images-quality.json`.
#[derive(Serialize)]
struct QualityReport {
    input: String,
    frames: usize,
    formats: Vec<FormatQuality>,
}

/// Checks `image`, read back from `path`, against `frame`, the `k`-th: its
/// size, and its samples for an exact format, or what it lost otherwise.
fn check(format: &Format, frame: &I420, image: &I420, path: &Path, k: usize) -> Result<Option<Loss>> {
    ensure!(
        (image.width(), image.height()) == (frame.width(), frame.height()),
        "{} is {}x{}, not {}x{}",
        path.display(),
        image.width(),
        image.height(),
        frame.width(),
        frame.height()
    );
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

/// The FFV1 frames of `input`, in order.
fn input_frames(input: &Path) -> Result<Vec<PathBuf>> {
    let mut inputs: Vec<PathBuf> = fs::read_dir(input)
        .with_context(|| format!("reading {} (run `prepare` first)", input.display()))?
        .map(|entry| Ok(entry?.path()))
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .filter(|path| path.extension().is_some_and(|e| e == "mkv"))
        .collect();
    inputs.sort();
    Ok(inputs)
}

fn main() -> Result<()> {
    let args = Args::parse();
    init()?;

    let mut frames = Vec::new();
    for input in input_frames(&args.input)? {
        frames.push((FFV1.read)(&input, 0, 0).with_context(|| format!("reading {}", input.display()))?);
    }
    ensure!(!frames.is_empty(), "no .mkv frames in {}", args.input.display());
    let (width, height) = (frames[0].width(), frames[0].height());
    ensure!(
        frames.iter().all(|f| (f.width(), f.height()) == (width, height)),
        "the frames of {} are not all {width}x{height}",
        args.input.display()
    );
    eprintln!("{}: {} frames of {width}x{height}", args.input.display(), frames.len());

    let dirs: Vec<PathBuf> = FORMATS.iter().map(|f| args.output.join(f.name)).collect();
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
            times.push(ImageTimes { bytes: fs::metadata(&path)?.len(), encode_seconds, decode_seconds, loss });
        }
        eprintln!("frame {k}: written in {} formats", FORMATS.len());
    }
    let mut parallel = Vec::new();
    for (format, dir) in FORMATS.iter().zip(&dirs) {
        let pass = in_parallel(format, dir, &frames)?;
        eprintln!(
            "{}: {} images, {} at a time, written in {:.2} s and read back in {:.2} s",
            format.name,
            frames.len(),
            pass.workers,
            pass.encode_seconds,
            pass.decode_seconds
        );
        parallel.push(pass);
    }

    // Butteraugli of the formats that are not exact, one image at a time:
    // at 8K it needs ~9 GiB.
    let mut quality = Vec::new();
    for (format, dir) in FORMATS.iter().zip(&dirs).filter(|(f, _)| !f.exact) {
        let started = Instant::now();
        let mut scores = Vec::new();
        for (k, frame) in frames.iter().enumerate() {
            let image = (format.read)(&dir.join(format.file_name(k)), width, height)?;
            let (butteraugli_max, butteraugli_3norm) = butteraugli(
                color::to_linear_rgb(frame, &color::BT709_LIMITED),
                color::to_linear_rgb(&image, &color::BT709_LIMITED),
                width,
                height,
            )?;
            eprintln!("frame {k} {}: butteraugli max-norm {butteraugli_max:.4}, 3-norm {butteraugli_3norm:.4}", format.name);
            scores.push(Quality { butteraugli_max, butteraugli_3norm });
        }
        quality.push(FormatQuality { format: format.name, seconds: started.elapsed().as_secs_f64(), images: scores });
    }

    let report = Report {
        input: args.input.display().to_string(),
        pixel_format: "yuv420p",
        width,
        height,
        frames: frames.len(),
        formats: FORMATS
            .iter()
            .zip(dirs)
            .zip(images)
            .zip(parallel)
            .map(|(((format, dir), images), parallel)| FormatReport {
                format: format.name,
                exact: format.exact,
                settings: format.settings,
                dir: dir.display().to_string(),
                bytes: images.iter().map(|i| i.bytes).sum(),
                encode_seconds: images.iter().map(|i| i.encode_seconds).sum(),
                decode_seconds: images.iter().map(|i| i.decode_seconds).sum(),
                images,
                parallel,
            })
            .collect(),
    };
    let quality = QualityReport { input: report.input.clone(), frames: frames.len(), formats: quality };
    fs::create_dir_all(&args.results)?;
    fs::write(args.results.join("images.json"), serde_json::to_vec_pretty(&report)?)?;
    fs::write(args.results.join("images-quality.json"), serde_json::to_vec_pretty(&quality)?)?;
    eprintln!("wrote {}", args.results.display());
    Ok(())
}
