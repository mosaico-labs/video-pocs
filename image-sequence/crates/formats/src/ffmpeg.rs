//! FFmpeg helpers shared by the formats that are a one-frame video file
//! (AVIF, FFV1), and by decoding the AV1 video itself.

use std::path::Path;

use anyhow::{Context, Result, bail};
use ffmpeg_the_third::{
    Dictionary, Packet, Rational, codec, color, decoder, encoder, format, frame, media, util::format::Pixel,
};

use super::I420;

/// The decoder of the AV1 video and of the AVIF images: dav1d.
pub const AV1_DECODER: &str = "libdav1d";

/// Initializes FFmpeg, keeping its own log to warnings and errors.
pub fn init() -> Result<()> {
    ffmpeg_the_third::init()?;
    ffmpeg_the_third::util::log::set_level(ffmpeg_the_third::util::log::Level::Warning);
    Ok(())
}

/// The capture time of a video's first frame, from its
/// [`common::CAPTURE_START_TAG`] tag, in nanoseconds since the Unix epoch.
pub fn capture_start_ns(input: &format::context::Input, path: &Path) -> Result<i64> {
    let tag = common::CAPTURE_START_TAG;
    Ok(input.metadata().get(tag).with_context(|| format!("{} has no {tag} tag", path.display()))?.parse()?)
}

/// A frame's capture time: the first frame's plus its `pts` in the stream's
/// `time_base`, in nanoseconds.
pub fn capture_ns(start_ns: i64, pts: i64, time_base: Rational) -> i64 {
    start_ns
        + (i128::from(pts) * 1_000_000_000 * i128::from(time_base.numerator())
            / i128::from(time_base.denominator())) as i64
}

/// Tags an encoder that is not open yet with the video's colors: the BT.709
/// matrix in limited range, and the PNGs' sRGB (BT.709 primaries with the
/// sRGB transfer curve).
pub fn set_color_tags(config: &mut encoder::video::Video) {
    config.set_colorspace(color::Space::BT709);
    config.set_color_range(color::Range::MPEG);
    // SAFETY: plain field writes on a context that is not open yet.
    unsafe {
        let context = config.as_mut_ptr();
        (*context).color_primaries = color::Primaries::BT709.into();
        (*context).color_trc = color::TransferCharacteristic::IEC61966_2_1.into();
    }
}

/// Encodes `image` alone with the FFmpeg encoder `codec_name` into `path`,
/// whose muxer FFmpeg picks from the extension.
pub fn write_one_frame(image: &I420, path: &Path, codec_name: &str, options: Dictionary) -> Result<()> {
    let codec = encoder::find_by_name(codec_name).with_context(|| format!("this FFmpeg has no {codec_name} encoder"))?;
    let mut output = format::output(path).with_context(|| format!("creating {}", path.display()))?;
    let mut config = codec::context::Context::new_with_codec(codec).encoder().video()?;
    config.set_width(image.width);
    config.set_height(image.height);
    config.set_format(Pixel::YUV420P);
    config.set_time_base(Rational(1, 1));
    config.set_frame_rate(Some(Rational(1, 1)));
    config.set_threading(codec::threading::Config::kind(codec::threading::Type::Slice));
    set_color_tags(&mut config);
    if output.format().flags().contains(format::Flags::GLOBAL_HEADER) {
        config.set_flags(codec::Flags::GLOBAL_HEADER);
    }
    let mut encoder = config
        .open_as_with(codec, options)
        .with_context(|| format!("opening the {codec_name} encoder"))?;
    let mut stream = output.add_stream(codec)?;
    stream.set_time_base(Rational(1, 1));
    stream.copy_parameters_from_context(&encoder);
    output.write_header()?;
    let stream_time_base = output.stream(0).context("no output stream")?.time_base();

    encoder.send_frame(&image.to_frame())?;
    encoder.send_eof()?;
    let mut packet = Packet::empty();
    let mut packets = 0;
    while encoder.receive_packet(&mut packet).is_ok() {
        packet.set_stream(0);
        packet.rescale_ts(Rational(1, 1), stream_time_base);
        packet.write_interleaved(&mut output)?;
        packets += 1;
    }
    if packets != 1 {
        bail!("{codec_name} output {packets} packets for one frame");
    }
    output.write_trailer()?;
    Ok(())
}

/// Decodes the one frame of `path`, with the decoder `decoder_name` or
/// FFmpeg's own for the codec. Its planes are copied as they are, if its
/// pixel format is one of `formats`.
pub fn read_one_frame(path: &Path, decoder_name: Option<&str>, formats: &[Pixel]) -> Result<I420> {
    let mut frames = Vec::new();
    decode_video(path, decoder_name, |frame, _| {
        frames.push(I420::from_frame_of(frame, formats)?);
        Ok(())
    })?;
    match <[I420; 1]>::try_from(frames) {
        Ok([image]) => Ok(image),
        Err(frames) => bail!("{} holds {} frames, not one", path.display(), frames.len()),
    }
}

/// Decodes every frame of the video stream of `path`, in presentation
/// order, handing each to `each` with its timestamp in the stream's time
/// base. Uses `decoder_name`, or FFmpeg's decoder for the codec.
pub fn decode_video(
    path: &Path,
    decoder_name: Option<&str>,
    mut each: impl FnMut(&frame::Video, Option<i64>) -> Result<()>,
) -> Result<(format::context::Input, Rational)> {
    let mut input = format::input(path).with_context(|| format!("opening {}", path.display()))?;
    let stream = input.streams().best(media::Type::Video).context("no video stream")?;
    let (index, time_base) = (stream.index(), stream.time_base());
    let mut context = codec::context::Context::from_parameters(stream.parameters())?;
    // Slice threads, as many as FFmpeg picks: what a one-frame file can use
    // (dav1d, for the AV1 video, uses its own threads either way).
    context.set_threading(codec::threading::Config::kind(codec::threading::Type::Slice));
    let mut decoder = match decoder_name {
        Some(name) => {
            let codec = decoder::find_by_name(name).with_context(|| format!("this FFmpeg has no {name} decoder"))?;
            context.decoder().open_as(codec)?.video()?
        }
        None => context.decoder().video()?,
    };
    let mut picture = frame::Video::empty();
    let mut receive = |decoder: &mut decoder::Video| -> Result<()> {
        while decoder.receive_frame(&mut picture).is_ok() {
            each(&picture, picture.pts())?;
        }
        Ok(())
    };
    for packet in input.packets() {
        let (stream, packet) = packet?;
        if stream.index() == index {
            decoder.send_packet(&packet)?;
            receive(&mut decoder)?;
        }
    }
    decoder.send_eof()?;
    receive(&mut decoder)?;
    Ok((input, time_base))
}
