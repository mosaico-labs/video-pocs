//! Raw I420 (see [`super::raw`]) compressed with zstd, on every core.

use std::{fs, path::Path};

use anyhow::Result;

use super::{Format, I420};

/// zstd level: 3 is zstd's default.
const LEVEL: i32 = 3;

pub const FORMAT: Format = Format {
    name: "zstd",
    extension: "yuv.zst",
    exact: true,
    settings,
    write,
    read,
};

fn settings() -> String {
    format!("I420 planes, zstd level {LEVEL}, multithreaded")
}

fn write(image: &I420, path: &Path) -> Result<()> {
    let mut compressor = ::zstd::bulk::Compressor::new(LEVEL)?;
    let workers = std::thread::available_parallelism().map_or(1, |n| n.get() as u32);
    compressor.set_parameter(::zstd::stream::raw::CParameter::NbWorkers(workers))?;
    Ok(fs::write(path, compressor.compress(&image.data)?)?)
}

fn read(path: &Path, width: u32, height: u32) -> Result<I420> {
    let data = ::zstd::bulk::decompress(&fs::read(path)?, I420::size(width, height))?;
    I420::from_planes(width, height, data)
}
