//! Raw I420: the three planes one after the other, as they are. There is no
//! header: the reader needs the size from elsewhere (here, the report).

use std::{fs, path::Path};

use anyhow::Result;

use super::{Format, I420};

pub const FORMAT: Format = Format {
    name: "raw",
    extension: "yuv",
    exact: true,
    settings,
    write,
    read,
};

fn settings() -> String {
    "I420 planes, no header".to_string()
}

fn write(image: &I420, path: &Path) -> Result<()> {
    Ok(fs::write(path, &image.data)?)
}

fn read(path: &Path, width: u32, height: u32) -> Result<I420> {
    I420::from_planes(width, height, fs::read(path)?)
}
