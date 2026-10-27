//! The image formats a video's frames can be turned into, shared by the
//! server, which writes them and serves them, and the client, which reads
//! them back.
//!
//! Each format is a [`Format`] in its own file: a `write` and a `read`
//! function, and its names. [`FORMATS`] lists them. To add a format, write
//! its file and list it; to remove one, take it out of the list and delete
//! its file.
//!
//! The exact formats keep the frames as dav1d decodes them, 8-bit YUV 4:2:0,
//! with no conversion to RGB, and losslessly. PNG and JPEG, for comparison,
//! can't hold the frames as they are: they are converted, losing as little as
//! each allows (see [`color`]).

mod avif;
pub mod color;
pub mod ffmpeg;
mod ffv1;
mod jpeg;
mod png;
mod raw;
mod zstd;

use std::path::Path;

use anyhow::{Result, bail};
use ffmpeg_the_third::{frame, util::format::Pixel};

/// An image format for one 8-bit YUV 4:2:0 frame: its names and its two
/// functions, from the format's own file.
pub struct Format {
    /// The format's folder under `images/` and its name in the reports.
    pub name: &'static str,
    /// The extension of its files, without the dot.
    pub extension: &'static str,
    /// Whether an image gives its frame back exactly.
    pub exact: bool,
    /// What the reports say about the settings.
    pub settings: fn() -> String,
    /// Writes an image to a path.
    pub write: fn(&I420, &Path) -> Result<()>,
    /// Reads back the image at a path, a frame of the given width and
    /// height (for formats without a header).
    pub read: fn(&Path, u32, u32) -> Result<I420>,
}

/// Images written and read at the same time when a whole sequence is done at
/// once, for every format: one image leaves most cores idle (one AVIF image
/// keeps ~2 of 10 busy), and at 8K four at a time read AVIF ~3x faster for
/// ~2.8 GiB, while eight gained only ~20% more for twice the memory.
pub const WORKERS: usize = 4;

/// Every format, in this order.
pub const FORMATS: &[Format] =
    &[raw::FORMAT, zstd::FORMAT, avif::FORMAT, ffv1::FORMAT, png::FORMAT, jpeg::FORMAT];

impl Format {
    /// The file name of the image of frame `k`: `frame_<k>.<extension>`.
    pub fn file_name(&self, k: usize) -> String {
        format!("frame_{k:06}.{}", self.extension)
    }
}

/// The format named `name`, if there is one.
pub fn by_name(name: &str) -> Option<&'static Format> {
    FORMATS.iter().find(|f| f.name == name)
}

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
    /// the encoder, see [`ffmpeg::set_color_tags`]).
    fn to_frame(&self) -> frame::Video {
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
