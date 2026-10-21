//! Turns the received PNG images into an AV1 video in a Matroska (`.mkv`)
//! file, with FFmpeg, the SVT-AV1 encoder (`libsvtav1`), and FFmpeg's
//! Matroska muxer.
//!
//! The work runs on two threads, so decoding the next PNGs overlaps with
//! encoding the previous frames. The decode thread decodes up to
//! [`DECODE_BATCH`] queued images in parallel to keep up with the encoder;
//! at 8K it is still the slower of the two (the upload report's
//! `encode_wait_seconds` shows how long the encoder waited for it):
//!
//! ```text
//! DoPut ──Image──▶ decode thread ──Yuv420──▶ encode thread ──▶ <locator>.mkv
//!                  PNG → RGB → YUV 4:2:0      SVT-AV1 + Matroska muxer
//! ```
//!
//! The video is 8-bit YUV 4:2:0 (`yuv420p`), the server's only pixel format
//! for now. [`Settings::pixel_format`] also handles 10-bit (`yuv420p10le`),
//! but the server does not let it be chosen.
//!
//! The first image's capture time is written as the [`CAPTURE_START_TAG`]
//! tag, and each frame's timestamp is its capture time minus that one.
//! FFmpeg's Matroska muxer stores timestamps in milliseconds, so the capture
//! times come back rounded to the millisecond.

use std::{
    path::PathBuf,
    sync::mpsc,
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, Context, Result};
use common::{
    images::{decode_png, rgb_to_yuv420, Yuv420},
    protocol::FrameTimes,
    PixelFormat, CAPTURE_START_TAG,
};
use ffmpeg_the_third::{
    self as ffmpeg, codec, color, encoder, format, frame, util::format::Pixel, Dictionary, Packet,
    Rational,
};
use rayon::prelude::*;
use tokio::sync::mpsc as async_mpsc;

/// The encoder's time base: timestamps are in nanoseconds, like the capture
/// times.
const NANOSECONDS: Rational = Rational(1, 1_000_000_000);

/// Most images decoded at the same time.
const DECODE_BATCH: usize = 4;

/// Encoder settings, from the server command line.
#[derive(Clone, Copy)]
pub struct Settings {
    /// SVT-AV1 preset: 0 (slowest, best) to 13 (fastest).
    pub preset: u8,
    /// SVT-AV1 constant rate factor: 1 (best) to 63 (smallest). `None`
    /// encodes losslessly: the decoded frames are the YUV 4:2:0 frames the
    /// encoder received, so the conversion to YUV 4:2:0 is the only loss.
    pub crf: Option<u8>,
    /// Nominal frame rate. SVT-AV1 needs one for its rate control; the
    /// frame timestamps are the capture times, whatever this value is.
    pub frame_rate: Rational,
    pub pixel_format: PixelFormat,
}

/// FFmpeg's name for a pixel format.
fn ffmpeg_pixel(format: PixelFormat) -> Pixel {
    match format {
        PixelFormat::Yuv420p => Pixel::YUV420P,
        PixelFormat::Yuv420p10le => Pixel::YUV420P10LE,
    }
}

/// One image as received by `DoPut`.
pub struct Image {
    pub timestamp_ns: i64,
    pub png: Vec<u8>,
    /// When it was received, for [`FrameTimes`].
    pub received: Instant,
}

/// One decoded image, on its way to the encoder.
struct Frame {
    timestamp_ns: i64,
    yuv: Yuv420,
    received: Instant,
    decode_start: Instant,
    decoded: Instant,
}

/// What the encode thread reports once the video is closed.
pub struct Encoded {
    pub width: u32,
    pub height: u32,
    pub frames: u64,
    pub video_bytes: u64,
    /// Time spent decoding PNGs and converting them to YUV.
    pub decode_seconds: f64,
    /// Encode thread wall time, from the first frame received to the file
    /// closed. SVT-AV1 encodes on its own threads, so this is the encoding
    /// time when the encoder is the bottleneck.
    pub encode_seconds: f64,
    /// Part of `encode_seconds` spent waiting for the next decoded frame.
    /// Near 0 when the encoder is the bottleneck.
    pub encode_wait_seconds: f64,
    /// When each frame went through the queues, the decoder and the encoder.
    pub timeline: Vec<FrameTimes>,
}

/// A video being written: images go in with [`VideoWriter::send`], and
/// [`VideoWriter::finish`] closes the file.
pub struct VideoWriter {
    images: async_mpsc::Sender<Image>,
    decode: JoinHandle<Result<Duration>>,
    encode: JoinHandle<Result<Encoded>>,
}

impl VideoWriter {
    /// Starts the threads. `started` is the time [`FrameTimes`] count from.
    pub fn spawn(path: PathBuf, settings: Settings, started: Instant) -> Self {
        // Small queues: an 8K image is ~33 MiB as PNG and ~50-100 MiB as YUV.
        let (images, mut image_rx) = async_mpsc::channel::<Image>(DECODE_BATCH);
        let (frame_tx, frame_rx) = mpsc::sync_channel::<Frame>(2);

        let bit_depth = settings.pixel_format.bit_depth();
        // Decoding an 8K PNG takes longer than encoding a frame, so the images
        // waiting in the queue are decoded in parallel (and sent on in order).
        let decode = thread::spawn(move || {
            let mut busy = Duration::ZERO;
            while let Some(first) = image_rx.blocking_recv() {
                let mut batch = vec![first];
                while batch.len() < DECODE_BATCH {
                    match image_rx.try_recv() {
                        Ok(image) => batch.push(image),
                        Err(_) => break,
                    }
                }
                let decode_start = Instant::now();
                let frames: Vec<Result<(i64, Yuv420, Instant)>> = batch
                    .into_par_iter()
                    .map(|image| {
                        let rgb = decode_png(&image.png).context("decoding a received PNG")?;
                        Ok((image.timestamp_ns, rgb_to_yuv420(&rgb, bit_depth)?, image.received))
                    })
                    .collect();
                let decoded = Instant::now();
                busy += decoded - decode_start;
                for frame in frames {
                    let (timestamp_ns, yuv, received) = frame?;
                    let frame = Frame {
                        timestamp_ns,
                        yuv,
                        received,
                        decode_start,
                        decoded,
                    };
                    if frame_tx.send(frame).is_err() {
                        return Ok(busy); // the encode thread stopped: its error is the one to report
                    }
                }
            }
            Ok(busy)
        });
        let encode = thread::spawn(move || encode(path, settings, frame_rx, started));
        Self {
            images,
            decode,
            encode,
        }
    }

    /// Queues an image. Fails if the threads stopped, and then
    /// [`VideoWriter::finish`] returns the reason.
    pub async fn send(&self, image: Image) -> Result<()> {
        self.images
            .send(image)
            .await
            .map_err(|_| anyhow!("the video encoder stopped"))
    }

    /// Encodes the queued images and closes the video. Blocks until done.
    pub fn finish(self) -> Result<Encoded> {
        drop(self.images);
        let decoded = self
            .decode
            .join()
            .map_err(|_| anyhow!("the decode thread panicked"))?;
        let encoded = self
            .encode
            .join()
            .map_err(|_| anyhow!("the encode thread panicked"))?;
        let decoded = decoded?;
        Ok(Encoded {
            decode_seconds: decoded.as_secs_f64(),
            ..encoded?
        })
    }
}

/// Copies a YUV 4:2:0 frame into an FFmpeg frame, whose rows may be padded:
/// one byte per sample in `yuv420p`, two little-endian bytes in
/// `yuv420p10le`. The color tags are set once on the encoder (see
/// [`encode`]), which SVT-AV1 reads them from.
fn ffmpeg_frame(yuv: &Yuv420, format: PixelFormat) -> frame::Video {
    let mut frame = frame::Video::new(ffmpeg_pixel(format), yuv.width as u32, yuv.height as u32);
    for (index, (plane, width)) in [
        (&yuv.y, yuv.width),
        (&yuv.u, yuv.width / 2),
        (&yuv.v, yuv.width / 2),
    ]
    .into_iter()
    .enumerate()
    {
        let stride = frame.stride(index);
        let data = frame.data_mut(index);
        for (row, source) in plane.chunks_exact(width).enumerate() {
            let target = &mut data[row * stride..];
            match format {
                PixelFormat::Yuv420p => target
                    .iter_mut()
                    .zip(source)
                    .for_each(|(t, s)| *t = *s as u8),
                PixelFormat::Yuv420p10le => target
                    .as_chunks_mut::<2>()
                    .0
                    .iter_mut()
                    .zip(source)
                    .for_each(|(t, s)| t.copy_from_slice(&s.to_le_bytes())),
            }
        }
    }
    frame
}

/// Sends every packet the encoder has ready to the muxer.
fn write_packets(
    encoder: &mut encoder::Video,
    output: &mut format::context::Output,
    stream_time_base: Rational,
) -> Result<()> {
    let mut packet = Packet::empty();
    while encoder.receive_packet(&mut packet).is_ok() {
        packet.set_stream(0);
        packet.rescale_ts(NANOSECONDS, stream_time_base);
        packet.write_interleaved(output)?;
    }
    Ok(())
}

fn encode(
    path: PathBuf,
    settings: Settings,
    frames: mpsc::Receiver<Frame>,
    upload_started: Instant,
) -> Result<Encoded> {
    // The size of the video is the size of the first image.
    let Ok(first) = frames.recv() else {
        bail!("no images received");
    };
    let (width, height) = (first.yuv.width as u32, first.yuv.height as u32);
    let first_ns = first.timestamp_ns;
    let started = Instant::now();
    let mut wait = Duration::ZERO;
    let seconds = |t: Instant| (t - upload_started).as_secs_f64();
    let mut timeline = Vec::new();

    let codec =
        encoder::find_by_name("libsvtav1").context("this FFmpeg has no libsvtav1 encoder")?;
    let mut output =
        format::output(&path).with_context(|| format!("creating {}", path.display()))?;
    let global_header = output
        .format()
        .flags()
        .contains(format::Flags::GLOBAL_HEADER);

    let mut config = codec::context::Context::new_with_codec(codec)
        .encoder()
        .video()?;
    config.set_width(width);
    config.set_height(height);
    config.set_format(ffmpeg_pixel(settings.pixel_format));
    config.set_time_base(NANOSECONDS);
    config.set_frame_rate(Some(settings.frame_rate));
    config.set_colorspace(color::Space::BT709);
    config.set_color_range(color::Range::MPEG);
    // SAFETY: plain field writes on a context that is not open yet.
    unsafe {
        let context = config.as_mut_ptr();
        // The PNGs are sRGB: BT.709 primaries with the sRGB transfer curve.
        (*context).color_primaries = color::Primaries::BT709.into();
        (*context).color_trc = color::TransferCharacteristic::IEC61966_2_1.into();
    }
    if global_header {
        config.set_flags(codec::Flags::GLOBAL_HEADER);
    }
    let mut options = Dictionary::new();
    options.set("preset", settings.preset.to_string());
    match settings.crf {
        Some(crf) => options.set("crf", &crf.to_string()),
        None => options.set("svtav1-params", "lossless=1"),
    }
    let mut encoder = config
        .open_as_with(codec, options)
        .context("opening the SVT-AV1 encoder")?;

    let mut stream = output.add_stream(codec)?;
    stream.set_time_base(NANOSECONDS);
    stream.copy_parameters_from_context(&encoder);
    output
        .metadata_mut()
        .set(CAPTURE_START_TAG, first_ns.to_string());
    output.write_header()?;
    // The muxer picks its own time base (milliseconds for Matroska).
    let stream_time_base = output.stream(0).context("no output stream")?.time_base();

    let mut count = 0u64;
    // The first frame's encoding includes opening the encoder.
    let (mut next, mut taken) = (Some(first), started);
    while let Some(Frame {
        timestamp_ns,
        yuv,
        received,
        decode_start,
        decoded,
    }) = next
    {
        if (yuv.width as u32, yuv.height as u32) != (width, height) {
            bail!(
                "image {count} is {}x{}, the video is {width}x{height}",
                yuv.width,
                yuv.height
            );
        }
        let mut frame = ffmpeg_frame(&yuv, settings.pixel_format);
        frame.set_pts(Some(timestamp_ns - first_ns));
        encoder.send_frame(&frame)?;
        write_packets(&mut encoder, &mut output, stream_time_base)?;
        let waiting = Instant::now();
        timeline.push(FrameTimes {
            received: seconds(received),
            decode_start: seconds(decode_start),
            decoded: seconds(decoded),
            encode_start: seconds(taken),
            encoded: seconds(waiting),
        });
        count += 1;
        next = frames.recv().ok();
        taken = Instant::now();
        wait += taken - waiting;
    }
    encoder.send_eof()?;
    write_packets(&mut encoder, &mut output, stream_time_base)?;
    output.write_trailer()?;
    drop(output); // closes the file

    Ok(Encoded {
        width,
        height,
        frames: count,
        video_bytes: std::fs::metadata(&path)?.len(),
        decode_seconds: 0.0, // filled in by VideoWriter::finish
        encode_seconds: started.elapsed().as_secs_f64(),
        encode_wait_seconds: wait.as_secs_f64(),
        timeline,
    })
}

/// Parses a frame rate written as `30000/1001` or `30`.
pub fn parse_rate(value: &str) -> Result<Rational, String> {
    let (num, den) = value.split_once('/').unwrap_or((value, "1"));
    match (num.parse::<i32>(), den.parse::<i32>()) {
        (Ok(num), Ok(den)) if num > 0 && den > 0 => Ok(Rational(num, den)),
        _ => Err(format!(
            "expected a frame rate like 30000/1001, not {value:?}"
        )),
    }
}

/// Initializes FFmpeg, keeping its own log to warnings and errors.
pub fn init_ffmpeg() -> Result<()> {
    ffmpeg::init()?;
    ffmpeg::util::log::set_level(ffmpeg::util::log::Level::Warning);
    Ok(())
}
