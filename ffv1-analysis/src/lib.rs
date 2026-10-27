//! What `prepare` and the analysis share: the 8-bit YUV 4:2:0 frame
//! ([`I420`]), FFmpeg's helpers to encode and decode one, the color
//! conversions ([`color`]) and the image formats ([`formats`]).

pub mod color;
pub mod formats;

use std::path::Path;

use anyhow::{Context, Result, anyhow, bail};
use butteraugli::{ButteraugliParams, Img, RGB, butteraugli_linear};
use ffmpeg_the_third::{
    Dictionary, Packet, Rational, codec, color as ffcolor, decoder, encoder, format, frame, media,
    util::format::Pixel,
};

/// The decoder of the AV1 video and of the AVIF images: dav1d.
pub const AV1_DECODER: &str = "libdav1d";

/// One 8-bit YUV 4:2:0 frame, its planes packed with no padding.
#[derive(PartialEq, Eq)]
pub struct I420 {
    width: u32,
    height: u32,
    data: Vec<u8>,
}

impl I420 {
    fn size(width: u32, height: u32) -> usize {
        let (w, h) = (width as usize, height as usize);
        w * h + 2 * (w / 2) * (h / 2)
    }

    pub fn width(&self) -> u32 {
        self.width
    }

    pub fn height(&self) -> u32 {
        self.height
    }

    /// The Y, U and V planes, one after the other, with no padding.
    pub fn samples(&self) -> &[u8] {
        &self.data
    }

    /// A frame from its planes, packed as I420: fails unless `data` is
    /// exactly the size of a `width`x`height` frame.
    fn from_planes(width: u32, height: u32, data: Vec<u8>) -> Result<Self> {
        let size = Self::size(width, height);
        if data.len() != size {
            bail!("{} bytes are not the {size} of a {width}x{height} frame", data.len());
        }
        Ok(Self { width, height, data })
    }

    /// The (width, height) of each plane: Y, then U and V at half size.
    fn planes(width: u32, height: u32) -> [(usize, usize); 3] {
        let (w, h) = (width as usize, height as usize);
        [(w, h), (w / 2, h / 2), (w / 2, h / 2)]
    }

    /// Copies a decoded `yuv420p` frame, whose rows may be padded.
    pub fn from_frame(frame: &frame::Video) -> Result<Self> {
        Self::from_frame_of(frame, &[Pixel::YUV420P])
    }

    /// Copies a decoded frame whose pixel format is one of `formats`, all
    /// 8-bit 4:2:0 with three planes, as it is: whatever its matrix or range.
    fn from_frame_of(frame: &frame::Video, formats: &[Pixel]) -> Result<Self> {
        if !formats.contains(&frame.format()) {
            bail!("the frame is {:?}, not one of {formats:?}", frame.format());
        }
        let (width, height) = (frame.width(), frame.height());
        let mut data = Vec::with_capacity(Self::size(width, height));
        for (index, (w, h)) in Self::planes(width, height).into_iter().enumerate() {
            let stride = frame.stride(index);
            let plane = frame.data(index);
            for row in 0..h {
                data.extend_from_slice(&plane[row * stride..row * stride + w]);
            }
        }
        Ok(Self { width, height, data })
    }

    /// An FFmpeg frame holding a copy, its pts 0 (the color tags are set on
    /// the encoder, see [`set_color_tags`]).
    pub fn to_frame(&self) -> frame::Video {
        let mut frame = frame::Video::new(Pixel::YUV420P, self.width, self.height);
        let mut source = self.data.as_slice();
        for (index, (w, h)) in Self::planes(self.width, self.height).into_iter().enumerate() {
            let stride = frame.stride(index);
            let plane = frame.data_mut(index);
            for row in 0..h {
                let (line, rest) = source.split_at(w);
                plane[row * stride..row * stride + w].copy_from_slice(line);
                source = rest;
            }
        }
        frame.set_pts(Some(0));
        frame
    }
}

/// Butteraugli of `distorted` against `reference`, both linear RGB (0 to 1)
/// of a `width`x`height` image: (max-norm, 3-norm). At 8K it needs ~9 GiB.
pub fn butteraugli(reference: Vec<[f32; 3]>, distorted: Vec<[f32; 3]>, width: u32, height: u32) -> Result<(f64, f64)> {
    let image = |pixels: Vec<[f32; 3]>| -> Img<Vec<RGB<f32>>> {
        Img::new(pixels.into_iter().map(|[r, g, b]| RGB::new(r, g, b)).collect(), width as usize, height as usize)
    };
    let (reference, distorted) = (image(reference), image(distorted));
    let scores = butteraugli_linear(reference.as_ref(), distorted.as_ref(), &ButteraugliParams::default())
        .map_err(|e| anyhow!("butteraugli: {e:?}"))?;
    Ok((scores.score, scores.pnorm_3))
}

/// Initializes FFmpeg, keeping its own log to errors: reading a one-frame
/// file, the demuxer warns that it can't estimate a frame rate, and the
/// image2 muxer that a TIFF's name has no sequence pattern.
pub fn init() -> Result<()> {
    ffmpeg_the_third::init()?;
    ffmpeg_the_third::util::log::set_level(ffmpeg_the_third::util::log::Level::Error);
    Ok(())
}

/// Tags an encoder that is not open yet with the frames' colors: the BT.709
/// matrix in limited range, and the PNGs' sRGB (BT.709 primaries with the
/// sRGB transfer curve).
pub fn set_color_tags(config: &mut encoder::video::Video) {
    config.set_colorspace(ffcolor::Space::BT709);
    config.set_color_range(ffcolor::Range::MPEG);
    // SAFETY: plain field writes on a context that is not open yet.
    unsafe {
        let context = config.as_mut_ptr();
        (*context).color_primaries = ffcolor::Primaries::BT709.into();
        (*context).color_trc = ffcolor::TransferCharacteristic::IEC61966_2_1.into();
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

/// Decodes the one frame of `path` with the decoder `decoder_name` or
/// FFmpeg's own for the codec. Its planes are copied as they are, if its
/// pixel format is one of `formats`.
pub fn read_one_frame(path: &Path, decoder_name: Option<&str>, formats: &[Pixel]) -> Result<I420> {
    let mut frames = Vec::new();
    decode_video(path, decoder_name, |frame| {
        frames.push(I420::from_frame_of(frame, formats)?);
        Ok(())
    })?;
    match <[I420; 1]>::try_from(frames) {
        Ok([image]) => Ok(image),
        Err(frames) => bail!("{} holds {} frames, not one", path.display(), frames.len()),
    }
}

/// Decodes every frame of the video stream of `path`, in presentation
/// order, handing each to `each`. Uses `decoder_name`, or FFmpeg's decoder
/// for the codec.
pub fn decode_video(
    path: &Path,
    decoder_name: Option<&str>,
    mut each: impl FnMut(&frame::Video) -> Result<()>,
) -> Result<()> {
    let mut input = format::input(path).with_context(|| format!("opening {}", path.display()))?;
    let stream = input.streams().best(media::Type::Video).context("no video stream")?;
    let index = stream.index();
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
            each(&picture)?;
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
    Ok(())
}
