//! Parquet writer settings for each mosaicod serialization format.
//!
//! `Default`, `Ragged` and `Image` copy `mosaicod-rw/src/format.rs`. `Video` is
//! the format proposed for encoded video chunks; it does not exist in mosaicod
//! yet and would be `FORMAT_VIDEO = 4` in `proto/mosaico/v1/core.proto`.

use parquet::{
    basic::{Compression, ZstdLevel},
    file::properties::{EnabledStatistics, WriterProperties, WriterVersion},
    schema::types::ColumnPath,
};
use serde::{Deserialize, Serialize};

use crate::TIMESTAMP_COLUMN;

/// ZSTD level used by mosaicod for the `Ragged` and `Image` formats.
const ZSTD_LEVEL: i32 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum Format {
    /// Parquet 2.0 with the library defaults (no compression, dictionary on).
    Default,
    /// ZSTD level 5, no dictionary, statistics and bloom filter on `timestamp_ns` only.
    Ragged,
    /// Same writer settings as `Ragged`.
    Image,
    /// Proposed: like `Image`, but the `data` column is stored uncompressed.
    Video,
}

impl Format {
    pub fn name(self) -> &'static str {
        match self {
            Format::Default => "default",
            Format::Ragged => "ragged",
            Format::Image => "image",
            Format::Video => "video",
        }
    }

    pub fn writer_properties(self) -> WriterProperties {
        let ts = ColumnPath::from(TIMESTAMP_COLUMN);
        let zstd = Compression::ZSTD(ZstdLevel::try_new(ZSTD_LEVEL).expect("valid ZSTD level"));
        let builder = WriterProperties::builder().set_writer_version(WriterVersion::PARQUET_2_0);
        match self {
            Format::Default => builder.build(),
            Format::Ragged | Format::Image => builder
                .set_compression(zstd)
                .set_dictionary_enabled(false)
                .set_statistics_enabled(EnabledStatistics::None)
                .set_column_compression(ts.clone(), Compression::UNCOMPRESSED)
                .set_column_statistics_enabled(ts.clone(), EnabledStatistics::Page)
                .set_column_bloom_filter_enabled(ts, true)
                .build(),
            // Encoded video is already entropy coded: ZSTD only spends CPU on
            // it, and dictionary encoding never matches whole chunks.
            // `timestamp_ns` keeps the page statistics and bloom filter that
            // mosaicod uses for time range queries.
            Format::Video => builder
                .set_compression(Compression::UNCOMPRESSED)
                .set_dictionary_enabled(false)
                .set_statistics_enabled(EnabledStatistics::None)
                .set_column_statistics_enabled(ts.clone(), EnabledStatistics::Page)
                .set_column_bloom_filter_enabled(ts, true)
                .build(),
        }
    }
}
