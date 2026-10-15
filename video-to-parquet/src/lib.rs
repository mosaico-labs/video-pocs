//! Shared definitions for the video-to-parquet Arrow Flight client and server.
//!
//! Every video is uploaded as one mosaicod topic with its own `DoPut` call.
//! The Flight descriptor is a command holding a [`DoPutCmd`], as in mosaicod,
//! and the data is a stream of record batches with this layout:
//!
//! | column         | type     | meaning                                  |
//! |----------------|----------|------------------------------------------|
//! | `timestamp_ns` | `Int64`  | index column required by mosaicod        |
//! | `chunk_index`  | `UInt32` | position of the chunk inside the file    |
//! | `offset`       | `UInt64` | byte offset of the chunk inside the file |
//! | `data`         | `Binary` | raw bytes of the chunk                   |

use std::{collections::HashMap, sync::Arc};

use arrow_array::{BinaryArray, Int64Array, RecordBatch, UInt32Array, UInt64Array};
use arrow_buffer::{Buffer, OffsetBuffer, ScalarBuffer};
use arrow_flight::FlightData;
use arrow_ipc::writer::FileWriter;
use arrow_schema::{ArrowError, DataType, Field, Schema, SchemaRef};
use parquet::{arrow::ArrowWriter, errors::ParquetError};
use prost::Message;
use serde::{Deserialize, Serialize};

pub mod format;
pub use format::Format;

/// Largest message mosaicod accepts by default
/// (`DEFAULT_GRPC_MAX_DECODE_MESSAGE_SIZE`).
pub const MAX_DECODE_MESSAGE_SIZE: usize = 50 * 1024 * 1024;

/// Largest message mosaicod sends (`GRPC_MSG_MAX_SIZE_BYTES`).
pub const MAX_ENCODE_MESSAGE_SIZE: usize = 512 * 1024 * 1024;

/// Index column every mosaicod topic must have, of type `Int64`.
pub const TIMESTAMP_COLUMN: &str = "timestamp_ns";

/// `FlightDescriptor.cmd` payload of a `DoPut`, as `DoPutCmd` in
/// `proto/mosaico/v1/flight.proto`.
#[derive(Clone, PartialEq, prost::Message)]
pub struct DoPutCmd {
    #[prost(string, tag = "1")]
    pub locator: String,
    #[prost(string, tag = "2")]
    pub topic_uuid: String,
}

/// Characters mosaicod rejects in sequence and topic names.
const INVALID_LOCATOR_CHARS: &[char] = &['!', '"', '\'', '*', '£', '$', '%', '&', '.', ' '];

fn has_invalid_symbols(value: &str, others: &[char]) -> bool {
    value.chars().any(|c| {
        !c.is_ascii()
            || c.is_ascii_control()
            || INVALID_LOCATOR_CHARS.contains(&c)
            || others.contains(&c)
    })
}

/// Splits a `<sequence>/<topic>` locator, with the checks of mosaicod's
/// `TopicLocator::from_str`. The topic may itself contain `/`.
pub fn parse_topic_locator(locator: &str) -> Option<(&str, &str)> {
    let (sequence, topic) = locator.split_once('/')?;
    let bad_sequence = sequence.is_empty() || has_invalid_symbols(sequence, &[':', '/']);
    let bad_topic = topic.is_empty()
        || topic.starts_with('/')
        || topic.ends_with('/')
        || topic.contains("//")
        || has_invalid_symbols(topic, &[':']);
    (!bad_sequence && !bad_topic).then_some((sequence, topic))
}

/// Turns a file or folder name into a valid locator part: every character
/// other than ASCII letters, digits, `-` and `_` becomes `_`.
pub fn locator_part(name: &str) -> String {
    name.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Size of the gRPC length-prefixed message header.
const GRPC_FRAME_HEADER: u64 = 5;

/// Schema of the chunk batches. The video name and the original file size
/// travel as schema metadata, which mosaicod keeps in the Parquet files.
pub fn chunk_schema(video: &str, file_size: u64) -> SchemaRef {
    let metadata = HashMap::from([
        ("video".to_string(), video.to_string()),
        ("file_size".to_string(), file_size.to_string()),
    ]);
    Arc::new(Schema::new_with_metadata(
        vec![
            Field::new(TIMESTAMP_COLUMN, DataType::Int64, false),
            Field::new("chunk_index", DataType::UInt32, false),
            Field::new("offset", DataType::UInt64, false),
            Field::new("data", DataType::Binary, false),
        ],
        metadata,
    ))
}

/// Splits `data` into `chunk_size` byte chunks, grouping `chunks_per_batch`
/// chunks in each record batch. The `data` columns are zero-copy slices of
/// `data`.
///
/// `timestamp_ns` is the chunk index. It only has to grow strictly, because
/// the mosaicod store optimizer sorts rows by it when it merges files: equal
/// values could reorder the chunks. Byte chunks don't map to presentation
/// times without demuxing the video.
pub fn split_into_batches(
    schema: &SchemaRef,
    data: &Buffer,
    chunk_size: usize,
    chunks_per_batch: usize,
) -> Result<Vec<RecordBatch>, ArrowError> {
    let total = data.len();
    let chunk_count = total.div_ceil(chunk_size);
    let mut batches = Vec::with_capacity(chunk_count.div_ceil(chunks_per_batch));

    for first in (0..chunk_count).step_by(chunks_per_batch) {
        let last = (first + chunks_per_batch).min(chunk_count);
        let start = first * chunk_size;
        let end = (last * chunk_size).min(total);

        let offsets: Vec<i32> = (first..last)
            .map(|i| (i * chunk_size - start) as i32)
            .chain(std::iter::once((end - start) as i32))
            .collect();
        let chunks = BinaryArray::try_new(
            OffsetBuffer::new(ScalarBuffer::from(offsets)),
            data.slice_with_length(start, end - start),
            None,
        )?;
        let timestamps = Int64Array::from_iter_values((first..last).map(|i| i as i64));
        let indexes = UInt32Array::from_iter_values((first..last).map(|i| i as u32));
        let byte_offsets =
            UInt64Array::from_iter_values((first..last).map(|i| (i * chunk_size) as u64));

        batches.push(RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(timestamps),
                Arc::new(indexes),
                Arc::new(byte_offsets),
                Arc::new(chunks),
            ],
        )?);
    }
    Ok(batches)
}

/// Bytes a `FlightData` message occupies in the gRPC stream: the protobuf
/// encoding plus the gRPC frame header. HTTP/2 and TCP framing are excluded.
pub fn wire_size(message: &FlightData) -> u64 {
    message.encoded_len() as u64 + GRPC_FRAME_HEADER
}

/// What the server received for one `DoPut`, returned as JSON in the
/// `PutResult` metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PutAck {
    /// Topic locator, `<sequence>/<topic>`.
    pub locator: String,
    /// Serialization format of the stored Parquet files.
    pub format: Format,
    /// `FlightData` messages received (schema + batches).
    pub messages: u64,
    pub batches: u64,
    /// Chunks received (one per row).
    pub rows: u64,
    /// Sum of the `data` column lengths.
    pub payload_bytes: u64,
    /// Bytes received, measured as in [`wire_size`].
    pub wire_bytes: u64,
    /// CRC32 of the chunks concatenated in `chunk_index` order.
    pub crc32: u32,
    /// From the first message to the end of the stream.
    pub receive_ms: f64,
    /// Time spent encoding batches into Parquet.
    pub encode_ms: f64,
    /// Time spent writing the Parquet files to disk.
    pub store_ms: f64,
    /// Topic folder written by the server.
    pub stored_path: String,
    /// Parquet files written, one per record batch.
    pub stored_files: u64,
    /// Total size of the Parquet files on disk.
    pub stored_bytes: u64,
}

/// Counts the bytes written to it and drops them.
#[derive(Default)]
struct ByteCounter(u64);

impl std::io::Write for ByteCounter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0 += buf.len() as u64;
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Size of the Arrow IPC file (`.arrow`) holding `batches`, which is how
/// video-to-parquet stored videos before following mosaicod. Nothing is kept in memory.
pub fn arrow_ipc_file_size(schema: &SchemaRef, batches: &[RecordBatch]) -> Result<u64, ArrowError> {
    let mut writer = FileWriter::try_new(ByteCounter::default(), schema)?;
    for batch in batches {
        writer.write(batch)?;
    }
    Ok(writer.into_inner()?.0)
}

/// Formats a byte count in the largest binary unit that keeps it at least 1,
/// e.g. `512 B`, `38.1 KiB`, `29.28 MiB`, `27.85 GiB`.
pub fn human_size(bytes: u64) -> String {
    const UNITS: [&str; 4] = ["KiB", "MiB", "GiB", "TiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64 / 1024.0;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    // KiB values are small differences: one decimal is enough.
    let decimals = if unit == 0 { 1 } else { 2 };
    format!("{value:.decimals$} {}", UNITS[unit])
}

/// [`human_size`] of `actual - base`, with its sign.
pub fn human_size_diff(actual: u64, base: u64) -> String {
    let sign = if actual >= base { '+' } else { '-' };
    format!("{sign}{}", human_size(actual.abs_diff(base)))
}

/// Encodes one record batch as a complete Parquet file, as mosaicod's
/// `InMemoryChunkEncoder` does for every batch it receives.
pub fn encode_parquet(batch: &RecordBatch, format: Format) -> Result<Vec<u8>, ParquetError> {
    let buffer = Vec::with_capacity(batch.get_array_memory_size());
    let mut writer =
        ArrowWriter::try_new(buffer, batch.schema(), Some(format.writer_properties()))?;
    writer.write(batch)?;
    writer.into_inner()
}

#[cfg(test)]
mod tests {
    use arrow_array::{Array, Int64Array};
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

    use super::*;

    fn sample(len: usize) -> Vec<u8> {
        (0..len).map(|i| (i * 31 % 251) as u8).collect()
    }

    /// Concatenates the `data` column of the batches in order.
    fn reassemble<'a>(batches: impl IntoIterator<Item = &'a RecordBatch>) -> Vec<u8> {
        let mut out = Vec::new();
        for batch in batches {
            let data = batch
                .column_by_name("data")
                .unwrap()
                .as_any()
                .downcast_ref::<BinaryArray>()
                .unwrap();
            data.iter()
                .for_each(|chunk| out.extend_from_slice(chunk.unwrap()));
        }
        out
    }

    #[test]
    fn split_covers_the_whole_file() {
        let bytes = sample(10_000);
        let schema = chunk_schema("video.mp4", bytes.len() as u64);
        for (chunk_size, per_batch) in [(1, 1), (999, 1), (999, 4), (10_000, 1), (20_000, 3)] {
            let batches = split_into_batches(
                &schema,
                &Buffer::from_vec(bytes.clone()),
                chunk_size,
                per_batch,
            )
            .unwrap();
            let rows: usize = batches.iter().map(RecordBatch::num_rows).sum();
            assert_eq!(rows, bytes.len().div_ceil(chunk_size));
            assert_eq!(reassemble(&batches), bytes);

            let indexes: Vec<u32> = batches
                .iter()
                .flat_map(|b| {
                    b.column_by_name("chunk_index")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<UInt32Array>()
                        .unwrap()
                        .values()
                        .to_vec()
                })
                .collect();
            assert_eq!(indexes, (0..rows as u32).collect::<Vec<_>>());

            // Strictly increasing, so sorting by it keeps the chunk order.
            let timestamps: Vec<i64> = batches
                .iter()
                .flat_map(|b| {
                    b.column_by_name(TIMESTAMP_COLUMN)
                        .unwrap()
                        .as_any()
                        .downcast_ref::<Int64Array>()
                        .unwrap()
                        .values()
                        .to_vec()
                })
                .collect();
            assert_eq!(timestamps, (0..rows as i64).collect::<Vec<_>>());
        }
    }

    #[test]
    fn empty_file_has_no_batches() {
        let schema = chunk_schema("empty.mp4", 0);
        assert!(
            split_into_batches(&schema, &Buffer::from_vec(Vec::<u8>::new()), 1024, 1)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn parquet_round_trip_in_every_format() {
        let bytes = sample(5_000);
        let schema = chunk_schema("video.webm", bytes.len() as u64);
        let batches =
            split_into_batches(&schema, &Buffer::from_vec(bytes.clone()), 700, 2).unwrap();

        for format in [
            Format::Default,
            Format::Ragged,
            Format::Image,
            Format::Video,
        ] {
            // One Parquet file per batch, as mosaicod stores them.
            let mut read = Vec::new();
            for batch in &batches {
                let file = encode_parquet(batch, format).unwrap();
                let builder =
                    ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::from(file)).unwrap();
                // Kept in the file, as mosaicod relies on for client metadata.
                assert_eq!(builder.schema().metadata()["video"], "video.webm");
                for batch in builder.build().unwrap() {
                    read.push(batch.unwrap());
                }
            }
            assert_eq!(reassemble(&read), bytes, "{format:?}");
        }
    }

    #[test]
    fn arrow_ipc_size_matches_a_written_file() {
        let bytes = sample(5_000);
        let schema = chunk_schema("video.webm", bytes.len() as u64);
        let batches =
            split_into_batches(&schema, &Buffer::from_vec(bytes.clone()), 700, 2).unwrap();
        let mut writer = FileWriter::try_new(Vec::new(), &schema).unwrap();
        batches.iter().for_each(|b| writer.write(b).unwrap());
        let file = writer.into_inner().unwrap();
        assert_eq!(
            arrow_ipc_file_size(&schema, &batches).unwrap(),
            file.len() as u64
        );
    }

    #[test]
    fn human_sizes() {
        assert_eq!(human_size(0), "0 B");
        assert_eq!(human_size(1023), "1023 B");
        assert_eq!(human_size(39_014), "38.1 KiB");
        assert_eq!(human_size(30_704_510), "29.28 MiB");
        assert_eq!(human_size(29_903_000_000), "27.85 GiB");
        assert_eq!(human_size_diff(1_000, 40_000), "-38.1 KiB");
        assert_eq!(human_size_diff(40_000, 1_000), "+38.1 KiB");
    }

    #[test]
    fn locators_follow_mosaicod_rules() {
        assert_eq!(
            parse_topic_locator("1080p/clip_mp4"),
            Some(("1080p", "clip_mp4"))
        );
        assert_eq!(parse_topic_locator("seq/a/b"), Some(("seq", "a/b")));
        for bad in [
            "clip",
            "/clip",
            "seq/",
            "seq//a",
            "seq/a/",
            "s.q/a",
            "seq/a.mp4",
            "seq/a b",
            "seq/a:b",
            "s:q/a",
        ] {
            assert_eq!(parse_topic_locator(bad), None, "{bad}");
        }
    }

    #[test]
    fn locator_part_is_always_valid() {
        let part = locator_part("4K Videos (4096x2160).mp4");
        assert_eq!(part, "4K_Videos__4096x2160__mp4");
        assert!(parse_topic_locator(&format!("{part}/{part}")).is_some());
    }
}
