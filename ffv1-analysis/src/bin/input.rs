//! Measures the input images of a batch against FFV1, writing
//! `results/input.json`:
//!
//! - **the PNGs**: the size of each file, as the dataset has it;
//! - **the PNGs in FFV1**: every PNG, as it is (8-bit RGBA, no color
//!   conversion), one frame of a single FFV1 video in Matroska, as mosaicod's
//!   store optimizer turns an image sequence into a video. The video is then
//!   read back, and each frame checked against the pixels of its PNG;
//! - **the AV1 video** `prepare` wrote of the PNGs, `av1/<scenario>/<batch>.mkv`:
//!   its size, to compare with the analysis' FFV1 frames, which are its
//!   frames.
//!
//! The analysis' FFV1 frames are something else: the frames of the AV1 video
//! of the PNGs (see `prepare`), 8-bit YUV 4:2:0 that the AV1 encoding has
//! already simplified.

use std::{
    collections::hash_map::DefaultHasher,
    fs,
    hash::{Hash, Hasher},
    io::Cursor,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use ffmpeg_the_third::{Dictionary, Packet, Rational, codec, encoder, format, frame, util::format::Pixel};
use ffv1_analysis::{batch_name, decode_video, init, read_timestamps};
use serde::Serialize;

/// The sequence's frame rate: 29.97 fps, as `prepare`'s video.
const FRAME_RATE: Rational = Rational(30000, 1001);

/// FFV1's settings, as mosaicod's: version 3, 16 slices coded in parallel,
/// each with a CRC.
const OPTIONS: [(&str, &str); 3] = [("level", "3"), ("slices", "16"), ("slicecrc", "1")];
const SETTINGS: &str = "FFV1 version 3, 16 slices with a CRC each, 8-bit RGBA (`bgra`), one video in Matroska";

#[derive(Parser)]
#[command(about = "Measures the PNGs of a batch, and the same images as they are in one FFV1 video")]
struct Args {
    /// The batch: a `timestamps.csv` written by `scripts/build-timestamps.py`.
    #[arg(long, default_value = "datasets/regular/batch_060/timestamps.csv")]
    timestamps: PathBuf,
}

#[derive(Serialize)]
struct Report {
    /// The batch, as `datasets/<scenario>/<batch>`.
    input: String,
    width: u32,
    height: u32,
    frames: usize,
    settings: &'static str,
    /// All the PNG files.
    png_bytes: u64,
    /// The FFV1 video, the whole file.
    ffv1_bytes: u64,
    /// The AV1 video of `prepare`, the whole file.
    av1_bytes: u64,
    /// Encoding every frame, from the pixels in memory to the packets.
    encode_seconds: f64,
    /// Reading the whole video back, from the file to the frames in memory.
    decode_seconds: f64,
    images: Vec<Image>,
}

#[derive(Serialize)]
struct Image {
    png_bytes: u64,
    /// The FFV1 frame of the image, in the video.
    ffv1_bytes: u64,
}

/// A PNG's 8-bit RGBA pixels, row after row.
fn load_rgba(path: &Path) -> Result<(u32, u32, Vec<u8>)> {
    let bytes = fs::read(path).with_context(|| format!("reading {} (scripts/build-datasets.sh images)", path.display()))?;
    let mut reader = png::Decoder::new(Cursor::new(bytes)).read_info()?;
    let mut buffer = vec![0; reader.output_buffer_size().context("PNG too large")?];
    let info = reader.next_frame(&mut buffer)?;
    buffer.truncate(info.buffer_size());
    if (info.color_type, info.bit_depth) != (png::ColorType::Rgba, png::BitDepth::Eight) {
        bail!("{}: only 8-bit RGBA PNGs are measured, not {:?}", path.display(), (info.color_type, info.bit_depth));
    }
    Ok((info.width, info.height, buffer))
}

fn digest(pixels: &[u8]) -> u64 {
    let mut hasher = DefaultHasher::new();
    pixels.hash(&mut hasher);
    hasher.finish()
}

/// RGBA pixels into a `bgra` frame, whose rows may be padded.
fn to_frame(width: u32, height: u32, rgba: &[u8]) -> frame::Video {
    let mut frame = frame::Video::new(Pixel::BGRA, width, height);
    let stride = frame.stride(0);
    let row = 4 * width as usize;
    let data = frame.data_mut(0);
    for (y, source) in rgba.chunks_exact(row).enumerate() {
        let (targets, _) = data[y * stride..y * stride + row].as_chunks_mut::<4>();
        for (target, &[r, g, b, a]) in targets.iter_mut().zip(source.as_chunks::<4>().0) {
            *target = [b, g, r, a];
        }
    }
    frame
}

/// A decoded `bgra` frame's pixels, as RGBA.
fn to_rgba(frame: &frame::Video) -> Result<Vec<u8>> {
    ensure!(frame.format() == Pixel::BGRA, "decoded a {:?} frame, not bgra", frame.format());
    let (width, stride) = (frame.width() as usize, frame.stride(0));
    let data = frame.data(0);
    let mut rgba = Vec::with_capacity(4 * width * frame.height() as usize);
    for y in 0..frame.height() as usize {
        for &[b, g, r, a] in data[y * stride..y * stride + 4 * width].as_chunks::<4>().0 {
            rgba.extend_from_slice(&[r, g, b, a]);
        }
    }
    Ok(rgba)
}

fn main() -> Result<()> {
    let args = Args::parse();
    init()?;
    let images = read_timestamps(&args.timestamps)?;
    let batch = batch_name(&args.timestamps)?;
    let av1 = Path::new("av1").join(&batch).with_extension("mkv");
    let av1_bytes = fs::metadata(&av1)
        .with_context(|| format!("reading {} (cargo run --release --bin prepare)", av1.display()))?
        .len();
    let video = Path::new("output/input").join(&batch).with_extension("mkv");
    fs::create_dir_all(video.parent().context("a video without a folder")?)?;

    let codec = encoder::find_by_name("ffv1").context("this FFmpeg has no ffv1 encoder")?;
    let mut output = format::output(&video).with_context(|| format!("creating {}", video.display()))?;
    let period = FRAME_RATE.invert();
    let mut encoder = None;
    let mut stream_time_base = period;
    let mut report = Vec::with_capacity(images.len());
    let mut digests = Vec::with_capacity(images.len());
    let (mut width, mut height) = (0, 0);
    let mut encoding = Duration::ZERO;
    let mut written = 0usize;

    let mut receive = |encoder: &mut encoder::Video,
                       output: &mut format::context::Output,
                       time_base: Rational,
                       sizes: &mut Vec<u64>|
     -> Result<()> {
        let mut packet = Packet::empty();
        while encoder.receive_packet(&mut packet).is_ok() {
            sizes.push(packet.size() as u64);
            packet.set_pts(Some(written as i64));
            packet.set_dts(Some(written as i64));
            packet.set_duration(1);
            packet.set_stream(0);
            packet.rescale_ts(period, time_base);
            packet.write_interleaved(output)?;
            written += 1;
        }
        Ok(())
    };
    let mut packet_sizes = Vec::with_capacity(images.len());

    for (k, (path, _)) in images.iter().enumerate() {
        let (w, h, rgba) = load_rgba(path)?;
        let png_bytes = fs::metadata(path)?.len();
        if encoder.is_none() {
            (width, height) = (w, h);
            let mut config = codec::context::Context::new_with_codec(codec).encoder().video()?;
            config.set_width(w);
            config.set_height(h);
            config.set_format(Pixel::BGRA);
            config.set_time_base(period);
            config.set_frame_rate(Some(FRAME_RATE));
            config.set_threading(codec::threading::Config::kind(codec::threading::Type::Slice));
            if output.format().flags().contains(format::Flags::GLOBAL_HEADER) {
                config.set_flags(codec::Flags::GLOBAL_HEADER);
            }
            let mut options = Dictionary::new();
            for (name, value) in OPTIONS {
                options.set(name, value);
            }
            let opened = config.open_as_with(codec, options).context("opening the FFV1 encoder")?;
            let mut stream = output.add_stream(codec)?;
            stream.set_time_base(period);
            stream.copy_parameters_from_context(&opened);
            output.write_header()?;
            stream_time_base = output.stream(0).context("no output stream")?.time_base();
            encoder = Some(opened);
        }
        ensure!((w, h) == (width, height), "{} is {w}x{h}, the first image {width}x{height}", path.display());
        let encoder = encoder.as_mut().context("no encoder")?;
        digests.push(digest(&rgba));
        let mut frame = to_frame(w, h, &rgba);
        frame.set_pts(Some(k as i64));
        let started = Instant::now();
        encoder.send_frame(&frame)?;
        receive(encoder, &mut output, stream_time_base, &mut packet_sizes)?;
        encoding += started.elapsed();
        report.push(Image { png_bytes, ffv1_bytes: 0 });
        eprintln!("encoded {} ({}/{})", path.display(), k + 1, images.len());
    }
    let encoder = encoder.as_mut().context("no images")?;
    let started = Instant::now();
    encoder.send_eof()?;
    receive(encoder, &mut output, stream_time_base, &mut packet_sizes)?;
    encoding += started.elapsed();
    ensure!(written == images.len(), "the encoder output {written} packets for {} images", images.len());
    output.write_trailer()?;
    drop(output);
    for (image, bytes) in report.iter_mut().zip(&packet_sizes) {
        image.ffv1_bytes = *bytes;
    }

    // Read back, each frame against its PNG.
    let started = Instant::now();
    let mut k = 0;
    decode_video(&video, None, |frame| {
        let rgba = to_rgba(frame)?;
        ensure!(k < digests.len() && digest(&rgba) == digests[k], "frame {k} is not the pixels of its PNG");
        k += 1;
        Ok(())
    })?;
    let decode_seconds = started.elapsed().as_secs_f64();
    ensure!(k == images.len(), "{} decodes to {k} frames, not {}", video.display(), images.len());

    let report = Report {
        input: Path::new("datasets").join(&batch).display().to_string(),
        width,
        height,
        frames: images.len(),
        settings: SETTINGS,
        png_bytes: report.iter().map(|i| i.png_bytes).sum(),
        ffv1_bytes: fs::metadata(&video)?.len(),
        av1_bytes,
        encode_seconds: encoding.as_secs_f64(),
        decode_seconds,
        images: report,
    };
    let path = Path::new("results/input.json");
    fs::write(path, serde_json::to_string_pretty(&report)? + "\n")?;
    eprintln!(
        "{} PNGs: {} bytes; FFV1: {} bytes, every frame exact; wrote {}",
        report.frames,
        report.png_bytes,
        report.ffv1_bytes,
        path.display()
    );
    Ok(())
}
