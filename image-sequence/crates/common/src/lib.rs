//! Shared definitions for the image-sequence client, server and quality tool.
//!
//! - [`protocol`]: the mosaicod messages exchanged over Arrow Flight and the
//!   schema of the image record batches.
//! - [`images`]: reading a `timestamps.csv`, decoding the PNG images and
//!   converting them to the YUV 4:2:0 frames the video is encoded from.
//! - [`timing`]: whether the capture times make a constant- or a
//!   variable-frame-rate video.

use std::{
    fmt,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};

pub mod images;
pub mod protocol;
pub mod timing;

/// Matroska tag holding the capture time of the first image, in nanoseconds
/// since the Unix epoch. The video timestamps start at 0, so a frame was
/// captured at this time plus its timestamp.
pub const CAPTURE_START_TAG: &str = "CAPTURE_START_NS";

/// Topic every image sequence is uploaded to, inside its sequence.
pub const DEFAULT_TOPIC: &str = "camera";

/// The `image-sequence/` folder, root of this workspace.
pub fn workspace_dir() -> PathBuf {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    dir.canonicalize().unwrap_or(dir)
}

/// Where the server writes the videos and the quality tool its reports.
pub fn default_output_dir() -> PathBuf {
    workspace_dir().join("output")
}

/// The `timestamps.csv` used when none is given: the first 60 images of the
/// regular scenario.
pub fn default_timestamps() -> PathBuf {
    workspace_dir().join("datasets/regular/batch_060/timestamps.csv")
}

/// Sequence name for a `timestamps.csv`: its scenario and batch folders,
/// e.g. `regular/batch_060/timestamps.csv` becomes `regular_batch_060`.
pub fn sequence_name(timestamps: &Path) -> String {
    let mut parts = timestamps
        .parent()
        .into_iter()
        .flat_map(|batch| [batch.parent().and_then(Path::file_name), batch.file_name()])
        .flatten()
        .map(|part| part.to_string_lossy());
    let name = match (parts.next(), parts.next()) {
        (Some(scenario), Some(batch)) => format!("{scenario}_{batch}"),
        _ => "images".to_string(),
    };
    protocol::locator_part(&name)
}

/// Pixel format every video is encoded in, for now: 8-bit YUV 4:2:0. The
/// server's encoder and the quality tool's decoder also handle
/// [`PixelFormat::Yuv420p10le`], but it is not selectable.
pub const PIXEL_FORMAT: PixelFormat = PixelFormat::Yuv420p;

/// Pixel format of the AV1 video, as FFmpeg names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PixelFormat {
    /// YUV 4:2:0, 8 bits per sample.
    Yuv420p,
    /// YUV 4:2:0, 10 bits per sample (stored in 16, little-endian).
    Yuv420p10le,
}

impl PixelFormat {
    pub fn name(self) -> &'static str {
        match self {
            PixelFormat::Yuv420p => "yuv420p",
            PixelFormat::Yuv420p10le => "yuv420p10le",
        }
    }

    pub fn bit_depth(self) -> u8 {
        match self {
            PixelFormat::Yuv420p => 8,
            PixelFormat::Yuv420p10le => 10,
        }
    }
}

impl fmt::Display for PixelFormat {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// SVT-AV1 preset the server encodes with when none is given.
pub const DEFAULT_PRESET: u8 = 9;

/// SVT-AV1 constant rate factor the server encodes with when none is given.
pub const DEFAULT_CRF: u8 = 30;

/// Folder of a sequence under `output/` and `video-results/`: a name made by
/// [`sequence_name`] goes back to its scenario and batch folders
/// (`regular_batch_060` becomes `regular/batch_060`); any other name is
/// used as is.
pub fn sequence_dir(sequence: &str) -> PathBuf {
    match sequence.rsplit_once("_batch_") {
        Some((scenario, number)) if !scenario.is_empty() && !number.is_empty() => {
            Path::new(scenario).join(format!("batch_{number}"))
        }
        _ => PathBuf::from(sequence),
    }
}

/// Folder of one encoding of a sequence, so that videos encoded with
/// different settings sit side by side:
/// `<sequence dir>/preset_<preset>_crf_<crf>`, or
/// `<sequence dir>/preset_<preset>_lossless` without a CRF (lossless AV1,
/// where only the conversion to YUV 4:2:0 loses anything).
pub fn encoding_dir(sequence: &str, preset: u8, crf: Option<u8>) -> PathBuf {
    let quality = match crf {
        Some(crf) => format!("crf_{crf}"),
        None => "lossless".to_string(),
    };
    sequence_dir(sequence).join(format!("preset_{preset}_{quality}"))
}

/// Path of the video the server writes for `<sequence>/<topic>`:
/// `<output>/<encoding dir>/<topic>.mkv`, e.g.
/// `output/regular/batch_060/preset_9_crf_30/camera.mkv`.
pub fn video_path(output_dir: &Path, locator: &str, preset: u8, crf: Option<u8>) -> PathBuf {
    let (sequence, topic) = locator.split_once('/').unwrap_or((locator, DEFAULT_TOPIC));
    output_dir
        .join(encoding_dir(sequence, preset, crf))
        .join(format!("{topic}.mkv"))
}

/// The server's upload report, saved next to the video.
pub fn upload_report_path(video: &Path) -> PathBuf {
    video.with_extension("upload.json")
}

/// The capture times the client uploaded, one row per image in upload order
/// (one per frame of the video), saved next to the video as Parquet:
/// `<topic>.timestamps.parquet`.
pub fn timestamps_path(video: &Path) -> PathBuf {
    video.with_extension("timestamps.parquet")
}

/// The client's timing report, saved next to the video in the client's own
/// output folder.
pub fn client_report_path(video: &Path) -> PathBuf {
    video.with_extension("client.json")
}

/// The quality tool's per-frame metrics, saved next to the video.
pub fn quality_csv_path(video: &Path) -> PathBuf {
    video.with_extension("quality.csv")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sequence_names_go_back_to_their_folders() {
        let name = sequence_name(Path::new("datasets/regular/batch_060/timestamps.csv"));
        assert_eq!(name, "regular_batch_060");
        assert_eq!(sequence_dir(&name), Path::new("regular/batch_060"));
        assert_eq!(
            sequence_dir("frame-drops_batch_010"),
            Path::new("frame-drops/batch_010")
        );
        assert_eq!(sequence_dir("my-run"), Path::new("my-run"));
    }

    #[test]
    fn videos_are_grouped_by_encoding() {
        assert_eq!(
            video_path(Path::new("output"), "regular_batch_060/camera", 9, Some(30)),
            Path::new("output/regular/batch_060/preset_9_crf_30/camera.mkv")
        );
        assert_eq!(
            video_path(Path::new("output"), "regular_batch_060/camera", 9, None),
            Path::new("output/regular/batch_060/preset_9_lossless/camera.mkv")
        );
    }
}
