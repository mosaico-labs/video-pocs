//! Builds the input of the analysis from a batch of the dataset:
//!
//! ```text
//! datasets/<scenario>/<batch>/timestamps.csv   the PNGs and their capture times
//! av1/<scenario>/<batch>.mkv                   the AV1 video
//! ffv1/<scenario>/<batch>/frame_<k>.mkv        each of its frames, one FFV1 file each
//! ```
//!
//! 1. **AV1**: each PNG is converted to 8-bit YUV 4:2:0 (BT.709, limited
//!    range, see [`color::from_rgb8`]) and encoded with SVT-AV1 at a constant
//!    30000/1001 frame rate: frame *k* at *k* frame periods. The capture
//!    times must fit that rate, as `regular`'s do. The encoder gets
//!    [`PAUSE_BEFORE_LAST`] before the last frame, so that every run gives
//!    the same video.
//! 2. **FFV1**: the video is decoded with dav1d and every frame written as a
//!    lossless FFV1 image, then read back and checked: one image per frame,
//!    none dropped or repeated.
//!
//! The PNGs and the video are only the way to the FFV1 frames: the analysis
//! measures nothing of them.

use std::{
    fs,
    io::Cursor,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use ffmpeg_the_third::{Dictionary, Packet, Rational, codec, encoder, format};
use ffv1_analysis::{AV1_DECODER, I420, color, decode_video, formats::FFV1, init, set_color_tags};

/// The sequence's frame rate: 29.97 fps.
const FRAME_RATE: Rational = Rational(30000, 1001);

/// How long SVT-AV1 gets to catch up before the last frame: if the last
/// frame comes while the encoder is still busy with the earlier ones, its
/// packet changes from one run to the next (the first ones never do). With
/// the encoder caught up, as when the frames come at the pace of an upload,
/// it is always the same: 1 to 8 s all gave it, in 12 runs of 12.
const PAUSE_BEFORE_LAST: Duration = Duration::from_secs(5);

/// The encoder's time base: timestamps are in nanoseconds, like the capture
/// times.
const NANOSECONDS: Rational = Rational(1, 1_000_000_000);

#[derive(Parser)]
#[command(about = "Encodes a batch of the dataset into an AV1 video, then writes each of its frames as FFV1")]
struct Args {
    /// The batch: a `timestamps.csv` written by `scripts/build-timestamps.py`.
    #[arg(long, default_value = "datasets/regular/batch_060/timestamps.csv")]
    timestamps: PathBuf,
    /// SVT-AV1 preset: 0 (slowest, best) to 13 (fastest).
    #[arg(long, default_value_t = 5, value_parser = clap::value_parser!(u8).range(0..=13))]
    preset: u8,
    /// SVT-AV1 constant rate factor: 1 (best) to 63 (smallest).
    #[arg(long, default_value_t = 18, value_parser = clap::value_parser!(u8).range(1..=63))]
    crf: u8,
}

/// `<scenario>/<batch>`, from `.../<scenario>/<batch>/timestamps.csv`.
fn batch_name(timestamps: &Path) -> Result<PathBuf> {
    let batch = timestamps.parent().context("timestamps.csv without a batch folder")?;
    let scenario = batch.parent().and_then(Path::file_name).context("a batch without a scenario folder")?;
    Ok(Path::new(scenario).join(batch.file_name().context("a batch without a name")?))
}

/// The rows of an `image,timestamp_ns` CSV: each PNG, resolved from the
/// repository root, and its capture time in nanoseconds.
fn read_timestamps(csv: &Path) -> Result<Vec<(PathBuf, i64)>> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let text = fs::read_to_string(csv).with_context(|| format!("reading {}", csv.display()))?;
    let mut lines = text.lines();
    ensure!(lines.next() == Some("image,timestamp_ns"), "{}: expected the header image,timestamp_ns", csv.display());
    let rows: Vec<(PathBuf, i64)> = lines
        .enumerate()
        .map(|(i, line)| {
            let (image, timestamp) =
                line.split_once(',').with_context(|| format!("{} line {}: expected image,timestamp_ns", csv.display(), i + 2))?;
            Ok((root.join(image), timestamp.parse()?))
        })
        .collect::<Result<_>>()?;
    ensure!(!rows.is_empty(), "{} has no images", csv.display());
    Ok(rows)
}

/// Reads an 8-bit RGB or RGBA PNG (the alpha dropped) as a frame.
fn load_png(path: &Path) -> Result<I420> {
    let bytes = fs::read(path).with_context(|| format!("reading {} (scripts/build-datasets.sh images)", path.display()))?;
    let mut reader = png::Decoder::new(Cursor::new(bytes)).read_info()?;
    let mut buffer = vec![0; reader.output_buffer_size().context("PNG too large")?];
    let info = reader.next_frame(&mut buffer)?;
    buffer.truncate(info.buffer_size());
    let channels = match (info.color_type, info.bit_depth) {
        (png::ColorType::Rgb, png::BitDepth::Eight) => 3,
        (png::ColorType::Rgba, png::BitDepth::Eight) => 4,
        other => bail!("{}: only 8-bit RGB and RGBA PNGs are supported, not {other:?}", path.display()),
    };
    color::from_rgb8(info.width, info.height, channels, &buffer)
}

/// Encodes the PNGs of `timestamps` into `video`, at [`FRAME_RATE`]: all of
/// them converted first, then encoded. Returns the number of frames.
fn encode(timestamps: &Path, video: &Path, preset: u8, crf: u8) -> Result<usize> {
    let images = read_timestamps(timestamps)?;
    let first_ns = images[0].1;
    // Frame k must be k frame periods after the first, to the millisecond
    // the video stores.
    for (k, (image, t)) in images.iter().enumerate() {
        let ideal = k as i64 * 1_000_000_000 * i64::from(FRAME_RATE.1) / i64::from(FRAME_RATE.0);
        ensure!(
            (t - first_ns - ideal).abs() <= 500_000,
            "{} is not at {FRAME_RATE} fps after the first image",
            image.display()
        );
    }
    let mut frames: Vec<I420> = Vec::new();
    for (image, _) in &images {
        let yuv = load_png(image)?;
        if let Some(first) = frames.first() {
            ensure!(
                (yuv.width(), yuv.height()) == (first.width(), first.height()),
                "{} is {}x{}, the first image {}x{}",
                image.display(),
                yuv.width(),
                yuv.height(),
                first.width(),
                first.height()
            );
        }
        frames.push(yuv);
        eprintln!("converted {} ({}/{})", image.display(), frames.len(), images.len());
    }

    let (width, height) = (frames[0].width(), frames[0].height());
    let codec = encoder::find_by_name("libsvtav1").context("this FFmpeg has no libsvtav1 encoder")?;
    let mut output = format::output(video).with_context(|| format!("creating {}", video.display()))?;
    let mut config = codec::context::Context::new_with_codec(codec).encoder().video()?;
    config.set_width(width);
    config.set_height(height);
    config.set_format(ffmpeg_the_third::util::format::Pixel::YUV420P);
    config.set_time_base(NANOSECONDS);
    config.set_frame_rate(Some(FRAME_RATE));
    set_color_tags(&mut config);
    if output.format().flags().contains(format::Flags::GLOBAL_HEADER) {
        config.set_flags(codec::Flags::GLOBAL_HEADER);
    }
    let mut options = Dictionary::new();
    options.set("preset", preset.to_string());
    options.set("crf", crf.to_string());
    let mut encoder = config.open_as_with(codec, options).context("opening the SVT-AV1 encoder")?;

    let period = FRAME_RATE.invert();
    let mut stream = output.add_stream(codec)?;
    stream.set_time_base(period);
    stream.copy_parameters_from_context(&encoder);
    stream.set_avg_frame_rate(FRAME_RATE);
    stream.set_rate(FRAME_RATE);
    output.write_header()?;
    let stream_time_base = output.stream(0).context("no output stream")?.time_base();

    // An AV1 encoder outputs one packet per frame shown, in the order they
    // are shown, so the k-th packet is frame k, at k frame periods.
    let mut written = 0i64;
    let mut receive = |encoder: &mut encoder::Video, output: &mut format::context::Output| -> Result<()> {
        let mut packet = Packet::empty();
        while encoder.receive_packet(&mut packet).is_ok() {
            packet.set_pts(Some(written));
            packet.set_dts(Some(written));
            packet.set_duration(1);
            packet.set_stream(0);
            packet.rescale_ts(period, stream_time_base);
            packet.write_interleaved(output)?;
            written += 1;
        }
        Ok(())
    };
    for (k, (yuv, (_, t))) in frames.iter().zip(&images).enumerate() {
        let mut frame = yuv.to_frame();
        frame.set_pts(Some(t - first_ns));
        if k + 1 == frames.len() {
            thread::sleep(PAUSE_BEFORE_LAST);
        }
        encoder.send_frame(&frame)?;
        receive(&mut encoder, &mut output)?;
    }
    encoder.send_eof()?;
    receive(&mut encoder, &mut output)?;
    ensure!(written as usize == frames.len(), "the encoder output {written} packets for {} frames", frames.len());
    output.write_trailer()?;
    Ok(frames.len())
}

/// Decodes `video` with dav1d and writes each frame as FFV1 into `dir`,
/// checking that each image reads back as its frame. Returns the number of
/// frames.
fn extract(video: &Path, dir: &Path) -> Result<usize> {
    let mut k = 0;
    decode_video(video, Some(AV1_DECODER), |decoded| {
        let frame = I420::from_frame(decoded)?;
        let path = dir.join(FFV1.file_name(k));
        (FFV1.write)(&frame, &path).with_context(|| format!("writing {}", path.display()))?;
        let image = (FFV1.read)(&path, frame.width(), frame.height())
            .with_context(|| format!("reading {}", path.display()))?;
        ensure!(image == frame, "{} does not give back frame {k} exactly", path.display());
        k += 1;
        Ok(())
    })?;
    Ok(k)
}

fn main() -> Result<()> {
    let args = Args::parse();
    init()?;
    let batch = batch_name(&args.timestamps)?;
    let video = Path::new("av1").join(&batch).with_extension("mkv");
    let dir = Path::new("ffv1").join(&batch);
    fs::create_dir_all(video.parent().context("a video without a folder")?)?;
    // No stale frames from a longer batch.
    if dir.exists() {
        fs::remove_dir_all(&dir).with_context(|| format!("removing {}", dir.display()))?;
    }
    fs::create_dir_all(&dir)?;

    let started = Instant::now();
    let frames = encode(&args.timestamps, &video, args.preset, args.crf)?;
    eprintln!(
        "wrote {} ({frames} frames, preset {}, CRF {}, {} bytes) in {:.1} s",
        video.display(),
        args.preset,
        args.crf,
        fs::metadata(&video)?.len(),
        started.elapsed().as_secs_f64()
    );
    let started = Instant::now();
    let extracted = extract(&video, &dir)?;
    ensure!(extracted == frames, "{} decodes to {extracted} frames, not the {frames} encoded", video.display());
    eprintln!("wrote {extracted} FFV1 frames to {} in {:.1} s", dir.display(), started.elapsed().as_secs_f64());
    Ok(())
}
