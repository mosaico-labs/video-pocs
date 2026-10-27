//! Measures how much quality the AV1 video lost compared with the PNG
//! images it was encoded from.
//!
//! The video is decoded with FFmpeg (`libdav1d`), and each frame is compared
//! with its image from the `timestamps.csv`:
//!
//! - with the [`av_metrics`] metrics (PSNR, PSNR-HVS, MS-SSIM and
//!   CIEDE2000), in YUV 4:2:0 at the video's bit depth: the reference is the PNG converted with
//!   [`rgb_to_yuv420`], the same frame the encoder received. They measure
//!   what the AV1 encoding lost;
//! - with [`butteraugli`] and [`ssimulacra2`], between the decoded frame and
//!   the PNG itself, in linear RGB. They measure everything that was lost,
//!   including the conversion to YUV 4:2:0 (half-resolution color).
//!   Butteraugli is the only metric where lower is better.
//!
//! To tell the two losses apart, butteraugli and SSIMULACRA2 are also
//! computed between the PNG and the frame the encoder received (the PNG
//! converted to YUV 4:2:0, then back to RGB like a decoded frame), in the
//! `*_yuv420` columns: what the conversion alone lost, before any encoding.
//! It does not depend on the encoder settings, so it is computed once per
//! image and kept in `output/<scenario>/<batch>/yuv420.quality.json` for the
//! next videos of the batch (see [`ConversionCache`]).
//!
//! With `--yuv444-floor` it measures no video: only what converting the
//! images to YUV 4:4:4 (full-resolution color) would lose, the floor of a
//! lossless 4:4:4 video, which SVT-AV1 cannot encode. It is kept in
//! `output/<scenario>/<batch>/yuv444.quality.json`, the same format as the
//! 4:2:0 values.
//!
//! Every stage of the measuring is timed: each frame's log line gives its
//! breakdown, and the totals go to the summary and `<video>.quality.json`.
//!
//! It also checks that each frame's capture time (the [`common::CAPTURE_START_TAG`]
//! tag plus the frame timestamp) matches the CSV. FFmpeg's Matroska muxer
//! stores timestamps in milliseconds, so up to 0.5 ms of difference is
//! expected.
//!
//! Before measuring, it decodes the whole video once with nothing else
//! running, to time AV1 decoding alone (measuring interleaves decoding with
//! the metrics, which would hide part of it).
//!
//! Writes one row per frame to `<video>.quality.csv`, a summary to
//! `<video>.quality.txt`, and the decoding time and the time of each stage
//! to `<video>.quality.json`.

use std::{
    collections::{BTreeMap, HashMap},
    fmt::Write as _,
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, bail};
use av_metrics::video::{
    ChromaSampling, Frame, Pixel as FramePixel, PlanarMetrics,
    ciede::calculate_frame_ciede,
    psnr::calculate_frame_psnr,
    psnr_hvs::calculate_frame_psnr_hvs,
    ssim::calculate_frame_msssim,
};
use clap::Parser;
use common::{
    DEFAULT_CRF, DEFAULT_PRESET, DEFAULT_TOPIC,
    images::{Rgb8, TimedImage, Yuv420, load_png, read_timestamps, rgb_to_yuv420, rgb_to_yuv444},
    quality_csv_path, sequence_dir, sequence_name, video_path,
};
use serde::{Deserialize, Serialize};
use ffmpeg_the_third::{self as ffmpeg, codec, decoder, format, media, util::format::Pixel};
use rayon::prelude::*;
use butteraugli::{ButteraugliParams, Img, ImgVec, RGB, butteraugli_linear};
use ssimulacra2::{ColorPrimaries, LinearRgb, MatrixCoefficients, Rgb, TransferCharacteristic, Yuv, YuvConfig};

#[derive(Parser)]
#[command(about = "Compares an AV1 video with the PNG images it was encoded from")]
struct Args {
    /// The images the video was encoded from, and their capture times
    /// [default: image-sequence/datasets/regular/batch_060/timestamps.csv].
    #[arg(long)]
    timestamps: Option<PathBuf>,
    /// The video [default: the one the server writes for these timestamps
    /// and encoder settings, e.g.
    /// image-sequence/output/regular/batch_060/preset_9_crf_30/camera.mkv].
    #[arg(long)]
    video: Option<PathBuf>,
    /// SVT-AV1 preset the video was encoded with, to find it when `--video`
    /// is not given.
    #[arg(long, default_value_t = DEFAULT_PRESET)]
    preset: u8,
    /// SVT-AV1 CRF the video was encoded with, to find it when `--video` is
    /// not given.
    #[arg(long, default_value_t = DEFAULT_CRF)]
    crf: u8,
    /// The video was encoded losslessly (the server's `--lossless`), to find
    /// it when `--video` is not given.
    #[arg(long, conflicts_with = "crf")]
    lossless: bool,
    /// Measure one frame every N (8K frames take ~7 s each with 4 jobs).
    /// The capture times are checked on every frame.
    #[arg(long, default_value_t = 1, value_parser = clap::value_parser!(u64).range(1..))]
    step: u64,
    /// Frames measured at the same time. Butteraugli (~9 GiB at 8K) runs for
    /// one frame at a time; with 4 jobs the tool stays at ~15 GB resident.
    #[arg(long, default_value_t = 4, value_parser = clap::value_parser!(u64).range(1..))]
    jobs: u64,
    /// Measure what the conversion to YUV 4:2:0 alone loses again, instead
    /// of reusing it from `yuv420.quality.json`.
    #[arg(long)]
    refresh_conversion: bool,
    /// Measure no video: only what converting the images to 8-bit YUV 4:4:4
    /// alone loses, into `yuv444.quality.json` (reused unless
    /// `--refresh-conversion`).
    #[arg(long, conflicts_with_all = ["video", "crf", "lossless"])]
    yuv444_floor: bool,
}

/// What to measure, shared by every frame.
struct Measuring {
    conversion: ConversionCache,
}

const CHROMA: ChromaSampling = ChromaSampling::Cs420;

/// The decoded video's color format, for SSIMULACRA2 to convert it back to
/// RGB: `subsampling` 1 for 4:2:0 (half-resolution color), 0 for 4:4:4.
fn video_config(bit_depth: u8, subsampling: u8) -> YuvConfig {
    YuvConfig {
        bit_depth,
        subsampling_x: subsampling,
        subsampling_y: subsampling,
        full_range: false,
        matrix_coefficients: MatrixCoefficients::BT709,
        transfer_characteristics: TransferCharacteristic::SRGB,
        color_primaries: ColorPrimaries::BT709,
    }
}

/// Builds a [`Frame`] from three planes of raw samples (one byte each for
/// `u8`, two little-endian bytes for `u16`), given with their row stride in
/// bytes.
fn to_frame<T: FramePixel>(width: usize, height: usize, planes: [(&[u8], usize); 3]) -> Frame<T> {
    let mut frame = Frame::new_with_padding(width, height, CHROMA, 0);
    for (plane, (data, stride)) in frame.planes.iter_mut().zip(planes) {
        plane.copy_from_raw_u8(data, stride, size_of::<T>());
    }
    frame
}

/// The reference: the PNG converted to YUV, as the same sample type as the
/// decoded frame.
fn yuv_frame<T: FramePixel>(yuv: &Yuv420) -> Frame<T> {
    let planes = [(&yuv.y, yuv.width), (&yuv.u, yuv.width / 2), (&yuv.v, yuv.width / 2)];
    samples_frame(yuv.width, yuv.height, CHROMA, planes)
}

/// Builds a [`Frame`] from three planes of samples, given with their width.
fn samples_frame<T: FramePixel>(
    width: usize,
    height: usize,
    chroma: ChromaSampling,
    planes: [(&Vec<u16>, usize); 3],
) -> Frame<T> {
    let mut frame = Frame::new_with_padding(width, height, chroma, 0);
    for (plane, (samples, width)) in frame.planes.iter_mut().zip(planes) {
        for (row, source) in plane.rows_iter_mut().zip(samples.chunks_exact(width)) {
            row.iter_mut().zip(source).for_each(|(t, &s)| *t = T::cast_from(s));
        }
    }
    frame
}

/// A decoded frame: `yuv420p` (the videos written now) as 8-bit samples,
/// `yuv420p10le` as 10-bit ones.
enum Decoded {
    Eight(Frame<u8>),
    Ten(Frame<u16>),
}

impl Decoded {
    fn new(picture: &ffmpeg::frame::Video) -> Result<Self> {
        let (w, h) = (picture.width() as usize, picture.height() as usize);
        let planes = [0, 1, 2].map(|i| (picture.data(i), picture.stride(i)));
        match picture.format() {
            Pixel::YUV420P => Ok(Decoded::Eight(to_frame(w, h, planes))),
            Pixel::YUV420P10LE => Ok(Decoded::Ten(to_frame(w, h, planes))),
            other => bail!("expected yuv420p or yuv420p10le frames, not {other:?}"),
        }
    }

    /// Every metric for this frame, compared with `image`, and the time of
    /// each stage.
    fn measure(self, image: &TimedImage, measuring: &Measuring) -> Result<(Metrics, Timer)> {
        match self {
            Decoded::Eight(frame) => measure(image, frame, 8, measuring),
            Decoded::Ten(frame) => measure(image, frame, 10, measuring),
        }
    }
}

fn rgb_image(rgb: &Rgb8) -> Result<Rgb> {
    let data = rgb
        .data
        .as_chunks::<3>()
        .0
        .iter()
        .map(|px| [px[0], px[1], px[2]].map(|c| f32::from(c) / 255.0))
        .collect();
    Rgb::new(
        data,
        rgb.width,
        rgb.height,
        TransferCharacteristic::SRGB,
        ColorPrimaries::BT709,
    )
    .map_err(|e| anyhow!("{e:?}"))
}

/// Held while computing butteraugli, the metric that needs the most memory.
static BUTTERAUGLI: Mutex<()> = Mutex::new(());

/// For the color crates' errors, which only implement `Debug`.
fn debug_error(error: impl std::fmt::Debug) -> anyhow::Error {
    anyhow!("{error:?}")
}

/// Copies linear RGB pixels into the image type butteraugli reads.
fn linear_image(image: &LinearRgb) -> ImgVec<RGB<f32>> {
    let pixels = image.data().iter().map(|&[r, g, b]| RGB::new(r, g, b)).collect();
    Img::new(pixels, image.width(), image.height())
}

/// Adds a metric of each plane and their weighted average (Y counts 4x, as
/// it has 4x the samples), all as returned by av-metrics.
fn planar(metrics: &mut Metrics, name: &str, values: PlanarMetrics) {
    for (plane, value) in [("y", values.y), ("u", values.u), ("v", values.v), ("avg", values.avg)] {
        metrics.push((format!("{name}_{plane}"), value));
    }
}

/// The time of each stage of measuring a frame, in the order they ran.
#[derive(Default)]
struct Timer(Vec<(&'static str, Duration)>);

impl Timer {
    fn time<R>(&mut self, stage: &'static str, f: impl FnOnce() -> R) -> R {
        let started = Instant::now();
        let result = f();
        self.0.push((stage, started.elapsed()));
        result
    }

    fn total(&self) -> Duration {
        self.0.iter().map(|(_, d)| *d).sum()
    }
}

/// Bump when what [`Conversion`] measures changes, so that older cached
/// values are measured again.
const CONVERSION_VERSION: u32 = 1;

/// What the conversion to YUV 4:2:0 alone loses on one image: butteraugli
/// and SSIMULACRA2 between the PNG and the frame the encoder received.
#[derive(Clone, Serialize, Deserialize)]
struct Conversion {
    image: String,
    /// With the bit depth, tells whether the value is still valid: a changed
    /// PNG is measured again.
    png_bytes: u64,
    bit_depth: u8,
    ssimulacra2: f64,
    butteraugli_max: f64,
    butteraugli_3norm: f64,
}

/// The conversion values of a batch, by image, shared by the videos of the
/// batch: `output/<scenario>/<batch>/yuv420.quality.json`.
struct ConversionCache {
    path: PathBuf,
    entries: Mutex<HashMap<String, Conversion>>,
    reused: Mutex<usize>,
}

#[derive(Serialize, Deserialize)]
struct ConversionFile {
    version: u32,
    images: Vec<Conversion>,
}

impl ConversionCache {
    /// Reads the cache, or starts an empty one if it is missing, from another
    /// version, unreadable, or `refresh` is set.
    fn load(path: PathBuf, refresh: bool) -> Self {
        let entries = fs::read(&path)
            .ok()
            .filter(|_| !refresh)
            .and_then(|bytes| serde_json::from_slice::<ConversionFile>(&bytes).ok())
            .filter(|file| file.version == CONVERSION_VERSION)
            .map(|file| file.images.into_iter().map(|c| (c.image.clone(), c)).collect())
            .unwrap_or_default();
        Self {
            path,
            entries: Mutex::new(entries),
            reused: Mutex::new(0),
        }
    }

    fn get(&self, image: &TimedImage, png_bytes: u64, bit_depth: u8) -> Option<Conversion> {
        let found = self.entries.lock().unwrap().get(&image.image).cloned();
        let found = found.filter(|c| c.png_bytes == png_bytes && c.bit_depth == bit_depth);
        if found.is_some() {
            *self.reused.lock().unwrap() += 1;
        }
        found
    }

    fn insert(&self, conversion: Conversion) {
        self.entries.lock().unwrap().insert(conversion.image.clone(), conversion);
    }

    fn save(&self) -> Result<()> {
        let mut images: Vec<Conversion> = self.entries.lock().unwrap().values().cloned().collect();
        images.sort_by(|a, b| a.image.cmp(&b.image));
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir)?;
        }
        let file = ConversionFile {
            version: CONVERSION_VERSION,
            images,
        };
        fs::write(&self.path, serde_json::to_string_pretty(&file)?)?;
        Ok(())
    }
}

/// Every metric for one frame, as (column name, value), and the time of each
/// stage.
fn measure<T: FramePixel>(
    image: &TimedImage,
    decoded: Frame<T>,
    bit_depth: u8,
    measuring: &Measuring,
) -> Result<(Metrics, Timer)> {
    let cache = &measuring.conversion;
    let mut timer = Timer::default();
    let png_bytes = fs::metadata(&image.path)?.len();
    let cached = cache.get(image, png_bytes, bit_depth);
    let (rgb, yuv) = timer.time("PNG decoding, YUV 4:2:0", || -> Result<_> {
        let rgb = load_png(&image.path)?;
        let yuv = rgb_to_yuv420(&rgb, bit_depth)?;
        Ok((rgb, yuv))
    })?;
    let reference = yuv_frame::<T>(&yuv);
    let depth = usize::from(bit_depth);
    let boxed = |e: Box<dyn std::error::Error>| anyhow!("{e}");

    let mut metrics = Vec::new();
    let psnr = timer.time("PSNR", || calculate_frame_psnr(&reference, &decoded, depth, CHROMA));
    planar(&mut metrics, "psnr", psnr.map_err(boxed)?);
    let psnr_hvs = timer.time("PSNR-HVS", || calculate_frame_psnr_hvs(&reference, &decoded, depth, CHROMA));
    planar(&mut metrics, "psnr_hvs", psnr_hvs.map_err(boxed)?);
    let msssim = timer.time("MS-SSIM", || calculate_frame_msssim(&reference, &decoded, depth, CHROMA));
    planar(&mut metrics, "msssim_db", msssim.map_err(boxed)?);
    let ciede = timer.time("CIEDE2000", || calculate_frame_ciede(&reference, &decoded, depth, CHROMA));
    metrics.push(("ciede2000_db".into(), ciede.map_err(boxed)?));
    drop(reference);

    // Butteraugli and SSIMULACRA2 both compare in linear RGB: convert the
    // PNG and the decoded frame once, for both, and the frame the encoder
    // received if the conversion is not cached. It goes back to RGB the same
    // way as the decoded frame, so the two differ only by what AV1 lost.
    let to_linear = |frame: Frame<T>| -> Result<LinearRgb> {
        LinearRgb::try_from(Yuv::new(frame, video_config(bit_depth, 1)).map_err(debug_error)?).map_err(debug_error)
    };
    let (reference, distorted) = timer.time("linear RGB", || -> Result<_> {
        Ok((LinearRgb::try_from(rgb_image(&rgb)?).map_err(debug_error)?, to_linear(decoded)?))
    })?;
    let converted = match cached {
        Some(_) => None,
        None => Some(timer.time("linear RGB (conversion)", || to_linear(yuv_frame::<T>(&yuv)))?),
    };
    drop((rgb, yuv));

    // At 8K butteraugli needs ~9 GiB, so one frame at a time computes it
    // while the other frames go on with the other metrics.
    let butteraugli = |timer: &mut Timer, stage, wait_stage, distorted: &LinearRgb| {
        let waiting = Instant::now();
        let _one_at_a_time = BUTTERAUGLI.lock().unwrap_or_else(|e| e.into_inner());
        timer.0.push((wait_stage, waiting.elapsed()));
        timer.time(stage, || {
            butteraugli_linear(
                linear_image(&reference).as_ref(),
                linear_image(distorted).as_ref(),
                &ButteraugliParams::default(),
            )
        })
    };
    let video = butteraugli(&mut timer, "butteraugli", "waiting for butteraugli", &distorted)?;
    metrics.push(("butteraugli_max".into(), video.score));
    metrics.push(("butteraugli_3norm".into(), video.pnorm_3));

    // What the conversion to YUV 4:2:0 alone lost: cached, or measured now.
    let conversion = match (cached, converted) {
        (Some(conversion), _) => conversion,
        (None, Some(converted)) => {
            let scores = butteraugli(
                &mut timer,
                "butteraugli (conversion)",
                "waiting for butteraugli (conversion)",
                &converted,
            )?;
            let ssimulacra2 = timer.time("SSIMULACRA2 (conversion)", || {
                ssimulacra2::compute_frame_ssimulacra2(reference.clone(), converted)
            });
            let conversion = Conversion {
                image: image.image.clone(),
                png_bytes,
                bit_depth,
                ssimulacra2: ssimulacra2.map_err(debug_error)?,
                butteraugli_max: scores.score,
                butteraugli_3norm: scores.pnorm_3,
            };
            cache.insert(conversion.clone());
            conversion
        }
        (None, None) => unreachable!("the converted frame is made whenever nothing is cached"),
    };

    let video = timer.time("SSIMULACRA2", || ssimulacra2::compute_frame_ssimulacra2(reference, distorted));
    metrics.push(("ssimulacra2".into(), video.map_err(debug_error)?));

    metrics.push(("butteraugli_max_yuv420".into(), conversion.butteraugli_max));
    metrics.push(("butteraugli_3norm_yuv420".into(), conversion.butteraugli_3norm));
    metrics.push(("ssimulacra2_yuv420".into(), conversion.ssimulacra2));
    Ok((metrics, timer))
}

/// What the conversion to 8-bit YUV 4:4:4 alone loses on one image, the
/// same way as the 4:2:0 conversion in [`measure`]: cached, or measured now.
fn measure_yuv444(image: &TimedImage, cache: &ConversionCache) -> Result<Conversion> {
    const BIT_DEPTH: u8 = 8;
    let png_bytes = fs::metadata(&image.path)?.len();
    if let Some(conversion) = cache.get(image, png_bytes, BIT_DEPTH) {
        return Ok(conversion);
    }
    let rgb = load_png(&image.path)?;
    let yuv = rgb_to_yuv444(&rgb, BIT_DEPTH)?;
    let planes = [(&yuv.y, yuv.width), (&yuv.u, yuv.width), (&yuv.v, yuv.width)];
    let frame = samples_frame::<u8>(yuv.width, yuv.height, ChromaSampling::Cs444, planes);
    drop(yuv);
    let converted = LinearRgb::try_from(Yuv::new(frame, video_config(BIT_DEPTH, 0)).map_err(debug_error)?)
        .map_err(debug_error)?;
    let reference = LinearRgb::try_from(rgb_image(&rgb)?).map_err(debug_error)?;
    drop(rgb);
    let scores = {
        let _one_at_a_time = BUTTERAUGLI.lock().unwrap_or_else(|e| e.into_inner());
        butteraugli_linear(
            linear_image(&reference).as_ref(),
            linear_image(&converted).as_ref(),
            &ButteraugliParams::default(),
        )?
    };
    let ssimulacra2 = ssimulacra2::compute_frame_ssimulacra2(reference, converted).map_err(debug_error)?;
    let conversion = Conversion {
        image: image.image.clone(),
        png_bytes,
        bit_depth: BIT_DEPTH,
        ssimulacra2,
        butteraugli_max: scores.score,
        butteraugli_3norm: scores.pnorm_3,
    };
    cache.insert(conversion.clone());
    Ok(conversion)
}

/// `--yuv444-floor`: measures what the conversion to YUV 4:4:4 alone loses
/// on every `step`-th image, `jobs` at a time, and prints the means.
fn yuv444_floor(images: &[TimedImage], cache: &ConversionCache, step: u64, jobs: usize) -> Result<()> {
    let started = Instant::now();
    let selected: Vec<&TimedImage> = images.iter().step_by(step as usize).collect();
    let mut measured = Vec::new();
    for chunk in selected.chunks(jobs) {
        let results: Vec<Result<Conversion>> = chunk
            .par_iter()
            .map(|image| measure_yuv444(image, cache).with_context(|| format!("measuring {}", image.image)))
            .collect();
        for conversion in results {
            let conversion = conversion?;
            eprintln!(
                "{}: butteraugli max-norm {:.3}, 3-norm {:.3}, SSIMULACRA2 {:.2}",
                conversion.image, conversion.butteraugli_max, conversion.butteraugli_3norm, conversion.ssimulacra2
            );
            measured.push(conversion);
        }
    }
    cache.save()?;
    let mean = |f: fn(&Conversion) -> f64| measured.iter().map(f).sum::<f64>() / measured.len() as f64;
    println!(
        "conversion to YUV 4:4:4 alone, mean over {} images: butteraugli max-norm {:.3}, 3-norm {:.3}, SSIMULACRA2 {:.2}",
        measured.len(),
        mean(|c| c.butteraugli_max),
        mean(|c| c.butteraugli_3norm),
        mean(|c| c.ssimulacra2)
    );
    println!(
        "{} reused, written to {} in {:.1} s",
        *cache.reused.lock().unwrap(),
        cache.path.display(),
        started.elapsed().as_secs_f64()
    );
    Ok(())
}

/// The time of every stage, summed over the measured frames: (seconds,
/// frames). Frames are measured `--jobs` at a time, so the sum is more than
/// the wall time.
static STAGES: Mutex<BTreeMap<&'static str, (f64, usize)>> = Mutex::new(BTreeMap::new());

/// The metrics of one frame, as (column name, value).
type Metrics = Vec<(String, f64)>;

/// One decoded frame: its capture time and, if measured, its metrics.
struct Row {
    capture_ns: i64,
    metrics: Option<Metrics>,
}

/// Measures the queued frames in parallel and stores their metrics.
fn flush(
    pending: &mut Vec<(usize, Decoded)>,
    images: &[TimedImage],
    rows: &mut [Row],
    measuring: &Measuring,
) -> Result<()> {
    let results: Vec<Result<(usize, Metrics)>> = pending
        .par_drain(..)
        .map(|(n, decoded)| {
            let image = &images[n];
            let (metrics, timer) = decoded
                .measure(image, measuring)
                .with_context(|| format!("measuring frame {n} ({})", image.image))?;
            let value = |name: &str| metrics.iter().find(|(m, _)| m == name).map_or(f64::NAN, |m| m.1);
            let mut stages = String::new();
            for (stage, duration) in &timer.0 {
                let _ = write!(stages, ", {stage} {:.1}", duration.as_secs_f64());
            }
            eprintln!(
                "frame {n:>4}/{}: PSNR {:.2} dB, SSIMULACRA2 {:.2} ({:.1} s{stages})",
                images.len(),
                value("psnr_avg"),
                value("ssimulacra2"),
                timer.total().as_secs_f64()
            );
            let mut totals = STAGES.lock().unwrap();
            for (stage, duration) in timer.0 {
                let total = totals.entry(stage).or_default();
                total.0 += duration.as_secs_f64();
                total.1 += 1;
            }
            Ok((n, metrics))
        })
        .collect();
    for result in results {
        let (n, metrics) = result?;
        rows[n].metrics = Some(metrics);
    }
    Ok(())
}

/// The decoder used for every video: dav1d, through FFmpeg.
const DECODER: &str = formats::ffmpeg::AV1_DECODER;

/// Opens the video and a dav1d decoder for its video stream: returns them
/// with the stream's index and time base.
fn open_video(video: &Path) -> Result<(format::context::Input, decoder::Video, usize, ffmpeg::Rational)> {
    let input = format::input(video).with_context(|| format!("opening {}", video.display()))?;
    let stream = input.streams().best(media::Type::Video).context("no video stream")?;
    let (index, time_base) = (stream.index(), stream.time_base());
    let codec = decoder::find_by_name(DECODER).with_context(|| format!("this FFmpeg has no {DECODER} decoder"))?;
    let decoder = codec::context::Context::from_parameters(stream.parameters())?
        .decoder()
        .open_as(codec)?
        .video()?;
    Ok((input, decoder, index, time_base))
}

/// Decodes the whole video and discards the frames: returns the number of
/// frames and the seconds it took, reading the file included.
fn time_decoding(video: &Path) -> Result<(usize, f64)> {
    let started = Instant::now();
    let (mut input, mut decoder, index, _) = open_video(video)?;
    let mut picture = ffmpeg::frame::Video::empty();
    let mut frames = 0;
    for packet in input.packets() {
        let (stream, packet) = packet?;
        if stream.index() == index {
            decoder.send_packet(&packet)?;
            while decoder.receive_frame(&mut picture).is_ok() {
                frames += 1;
            }
        }
    }
    decoder.send_eof()?;
    while decoder.receive_frame(&mut picture).is_ok() {
        frames += 1;
    }
    Ok((frames, started.elapsed().as_secs_f64()))
}

/// Decodes the video and measures the frames, in presentation order,
/// `jobs` frames at a time.
fn decode_and_measure(
    video: &Path,
    images: &[TimedImage],
    step: u64,
    jobs: usize,
    measuring: &Measuring,
) -> Result<Vec<Row>> {
    let (mut input, mut decoder, index, time_base) = open_video(video)?;
    let start_ns = formats::ffmpeg::capture_start_ns(&input, video)?;

    let mut rows = Vec::new();
    let mut pending = Vec::new();
    let mut picture = ffmpeg::frame::Video::empty();
    let mut receive = |decoder: &mut decoder::Video, rows: &mut Vec<Row>, flush_all: bool| -> Result<()> {
        while decoder.receive_frame(&mut picture).is_ok() {
            let n = rows.len();
            let pts = picture.pts().context("decoded frame without timestamp")?;
            let capture_ns = formats::ffmpeg::capture_ns(start_ns, pts, time_base);
            rows.push(Row {
                capture_ns,
                metrics: None,
            });
            if n < images.len() && (n as u64).is_multiple_of(step) {
                pending.push((n, Decoded::new(&picture)?));
            }
            if pending.len() >= jobs {
                flush(&mut pending, images, rows, measuring)?;
            }
        }
        if flush_all {
            flush(&mut pending, images, rows, measuring)?;
        }
        Ok(())
    };

    for packet in input.packets() {
        let (stream, packet) = packet?;
        if stream.index() == index {
            decoder.send_packet(&packet)?;
            receive(&mut decoder, &mut rows, false)?;
        }
    }
    decoder.send_eof()?;
    receive(&mut decoder, &mut rows, true)?;
    Ok(rows)
}

fn write_csv(path: &Path, images: &[TimedImage], rows: &[Row]) -> Result<()> {
    let names: Vec<&str> = rows
        .iter()
        .find_map(|r| r.metrics.as_ref())
        .map(|m| m.iter().map(|(name, _)| name.as_str()).collect())
        .unwrap_or_default();
    let mut csv = format!(
        "frame,image,capture_ns,video_capture_ns,capture_error_ns,{}\n",
        names.join(",")
    );
    for (n, row) in rows.iter().enumerate() {
        let (image, expected) = images
            .get(n)
            .map_or(("", None), |i| (i.image.as_str(), Some(i.timestamp_ns)));
        let error = expected.map(|e| (row.capture_ns - e).to_string()).unwrap_or_default();
        let expected = expected.map(|e| e.to_string()).unwrap_or_default();
        write!(csv, "{n},{image},{expected},{},{error}", row.capture_ns)?;
        match &row.metrics {
            Some(metrics) => metrics.iter().try_for_each(|(_, value)| write!(csv, ",{value:.6}"))?,
            None => csv.push_str(&",".repeat(names.len())),
        }
        csv.push('\n');
    }
    fs::write(path, csv).with_context(|| format!("writing {}", path.display()))
}

fn summary(video: &Path, timestamps: &Path, images: &[TimedImage], rows: &[Row]) -> Result<String> {
    let mut text = String::new();
    let png_bytes: u64 = images
        .iter()
        .map(|i| fs::metadata(&i.path).map(|m| m.len()))
        .sum::<Result<_, _>>()?;
    let video_bytes = fs::metadata(video)?.len();
    writeln!(text, "video:      {}", video.display())?;
    writeln!(text, "timestamps: {}", timestamps.display())?;
    writeln!(
        text,
        "frames:     {} decoded, {} images in the CSV",
        rows.len(),
        images.len()
    )?;
    writeln!(
        text,
        "size:       {:.1} MiB of video for {:.1} MiB of PNG ({:.1}x smaller)",
        video_bytes as f64 / 1048576.0,
        png_bytes as f64 / 1048576.0,
        png_bytes as f64 / video_bytes as f64
    )?;
    if let [first, .., last] = images {
        // The span the frames cover: first to last capture, plus one frame interval.
        let span =
            (last.timestamp_ns - first.timestamp_ns) as f64 / 1e9 * images.len() as f64 / (images.len() - 1) as f64;
        writeln!(
            text,
            "bitrate:    {:.1} Mbit/s over {span:.2} s",
            video_bytes as f64 * 8.0 / span / 1e6
        )?;
    }
    let max_error = rows
        .iter()
        .zip(images)
        .map(|(row, image)| (row.capture_ns - image.timestamp_ns).abs())
        .max()
        .unwrap_or(0);
    writeln!(
        text,
        "capture times: largest difference from the CSV {:.3} ms (FFmpeg's Matroska timestamps are in ms)",
        max_error as f64 / 1e6
    )?;

    let measured: Vec<&Metrics> = rows.iter().filter_map(|r| r.metrics.as_ref()).collect();
    let Some(first) = measured.first() else {
        return Ok(text);
    };
    writeln!(text, "\n{} frames measured\n", measured.len())?;
    writeln!(text, "{:<16} {:>12} {:>12} {:>12}", "metric", "mean", "min", "max")?;
    for (i, (name, _)) in first.iter().enumerate() {
        let values: Vec<f64> = measured.iter().map(|m| m[i].1).collect();
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        let min = values.iter().copied().fold(f64::INFINITY, f64::min);
        let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        writeln!(text, "{name:<16} {mean:>12.4} {min:>12.4} {max:>12.4}")?;
    }

    // av-metrics' two video-level PSNRs: APSNR is the mean of the frame
    // PSNRs (the mean above), PSNR is computed from the mean squared error
    // of all the frames. Each frame's error relative to the peak is
    // 10^(-PSNR/10), so the peak cancels out.
    writeln!(text)?;
    for plane in ["y", "u", "v"] {
        let name = format!("psnr_{plane}");
        let errors: Vec<f64> = measured
            .iter()
            .filter_map(|m| m.iter().find(|(n, _)| *n == name))
            .map(|(_, psnr)| 10f64.powf(-psnr / 10.0))
            .collect();
        let mean_error = errors.iter().sum::<f64>() / errors.len() as f64;
        writeln!(text, "video PSNR {plane}: {:.4} dB", -10.0 * mean_error.log10())?;
    }
    Ok(text)
}

fn main() -> Result<()> {
    let args = Args::parse();
    formats::ffmpeg::init()?;

    let timestamps = args.timestamps.unwrap_or_else(common::default_timestamps);
    let images = read_timestamps(&timestamps)?;
    let sequence = sequence_name(&timestamps);
    let batch_output = common::default_output_dir().join(sequence_dir(&sequence));
    if args.yuv444_floor {
        let cache = ConversionCache::load(batch_output.join("yuv444.quality.json"), args.refresh_conversion);
        return yuv444_floor(&images, &cache, args.step, args.jobs as usize);
    }
    let video = args.video.unwrap_or_else(|| {
        video_path(
            &common::default_output_dir(),
            &format!("{sequence}/{DEFAULT_TOPIC}"),
            args.preset,
            (!args.lossless).then_some(args.crf),
        )
    });

    let started = Instant::now();
    let (frames, decode_seconds) = time_decoding(&video)?;
    eprintln!("decoded {frames} frames with {DECODER} in {decode_seconds:.2} s");
    // Before minutes of measuring: one frame per image, or nothing to compare.
    if frames != images.len() {
        bail!("{} has {frames} frames, but the CSV lists {} images", video.display(), images.len());
    }
    let measuring = Measuring {
        conversion: ConversionCache::load(batch_output.join("yuv420.quality.json"), args.refresh_conversion),
    };
    let rows = decode_and_measure(&video, &images, args.step, args.jobs as usize, &measuring)?;
    let cache = &measuring.conversion;
    cache.save()?;
    let reused = *cache.reused.lock().unwrap();
    if rows.len() != images.len() {
        bail!(
            "{} has {} frames, but the CSV lists {} images",
            video.display(),
            rows.len(),
            images.len()
        );
    }

    let csv = quality_csv_path(&video);
    write_csv(&csv, &images, &rows)?;
    let measured = rows.iter().filter(|r| r.metrics.is_some()).count();
    let mut text = summary(&video, &timestamps, &images, &rows)?;
    let stages = STAGES.lock().unwrap();
    writeln!(
        text,
        "\nconversion to YUV 4:2:0 alone: {reused} of {measured} frames reused from {}, {} measured",
        cache.path.display(),
        measured - reused
    )?;
    writeln!(
        text,
        "\ntime of each stage, summed over the frames (measured {} at a time):\n",
        args.jobs
    )?;
    writeln!(text, "{:<40} {:>10} {:>12}", "stage", "total s", "s per frame")?;
    for (stage, (seconds, count)) in stages.iter() {
        writeln!(text, "{stage:<40} {seconds:>10.1} {:>12.2}", seconds / *count as f64)?;
    }
    let txt = video.with_extension("quality.txt");
    fs::write(&txt, &text)?;
    let json = video.with_extension("quality.json");
    fs::write(
        &json,
        serde_json::to_string_pretty(&serde_json::json!({
            "decoder": DECODER,
            "frames": frames,
            "decode_seconds": decode_seconds,
            "measure_seconds": started.elapsed().as_secs_f64(),
            "jobs": args.jobs,
            "conversion_reused": reused,
            "stage_seconds": stages.iter().map(|(stage, (seconds, _))| (*stage, *seconds)).collect::<BTreeMap<_, _>>(),
        }))?,
    )?;
    println!("{text}");
    println!("per-frame metrics: {}", csv.display());
    println!("summary:           {}", txt.display());
    println!("decoding time:     {}", json.display());
    println!("took {:.1} s", started.elapsed().as_secs_f64());
    Ok(())
}
