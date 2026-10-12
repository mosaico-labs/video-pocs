//! Shared definitions for the poc1 Arrow Flight client and server.
//!
//! Every video is uploaded with its own `DoPut` call. The Flight descriptor
//! path carries the video file name, and the data is a stream of record
//! batches with this layout:
//!
//! | column        | type     | meaning                                  |
//! |---------------|----------|------------------------------------------|
//! | `chunk_index` | `UInt32` | position of the chunk inside the file    |
//! | `offset`      | `UInt64` | byte offset of the chunk inside the file |
//! | `data`        | `Binary` | raw bytes of the chunk                   |

use std::{collections::HashMap, sync::Arc};

use arrow_array::{BinaryArray, RecordBatch, UInt32Array, UInt64Array};
use arrow_buffer::{Buffer, OffsetBuffer, ScalarBuffer};
use arrow_flight::FlightData;
use arrow_schema::{ArrowError, DataType, Field, Schema, SchemaRef};
use prost::Message;
use serde::{Deserialize, Serialize};

/// gRPC message size limit for both peers. The tonic default (4 MiB) is too
/// small for large chunks.
pub const MAX_MESSAGE_SIZE: usize = 256 * 1024 * 1024;

/// Size of the gRPC length-prefixed message header.
const GRPC_FRAME_HEADER: u64 = 5;

/// Schema of the chunk batches. The video name and the original file size
/// travel as schema metadata.
pub fn chunk_schema(video: &str, file_size: u64) -> SchemaRef {
    let metadata = HashMap::from([
        ("video".to_string(), video.to_string()),
        ("file_size".to_string(), file_size.to_string()),
    ]);
    Arc::new(Schema::new_with_metadata(
        vec![
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
        let indexes = UInt32Array::from_iter_values((first..last).map(|i| i as u32));
        let byte_offsets =
            UInt64Array::from_iter_values((first..last).map(|i| (i * chunk_size) as u64));

        batches.push(RecordBatch::try_new(
            schema.clone(),
            vec![Arc::new(indexes), Arc::new(byte_offsets), Arc::new(chunks)],
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
    pub name: String,
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
    /// Arrow IPC file written by the server.
    pub stored_path: String,
    /// Size of the Arrow IPC file on disk.
    pub stored_bytes: u64,
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use arrow_array::Array;
    use arrow_ipc::{reader::FileReader, writer::FileWriter};

    use super::*;

    fn sample(len: usize) -> Vec<u8> {
        (0..len).map(|i| (i * 31 % 251) as u8).collect()
    }

    /// Concatenates the `data` column of the batches in order.
    fn reassemble<'a>(batches: impl IntoIterator<Item = &'a RecordBatch>) -> Vec<u8> {
        let mut out = Vec::new();
        for batch in batches {
            let data = batch
                .column(2)
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
                    b.column(0)
                        .as_any()
                        .downcast_ref::<UInt32Array>()
                        .unwrap()
                        .values()
                        .to_vec()
                })
                .collect();
            assert_eq!(indexes, (0..rows as u32).collect::<Vec<_>>());
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
    fn ipc_file_round_trip() {
        let bytes = sample(5_000);
        let schema = chunk_schema("video.webm", bytes.len() as u64);
        let batches =
            split_into_batches(&schema, &Buffer::from_vec(bytes.clone()), 700, 2).unwrap();

        let mut writer = FileWriter::try_new(Vec::new(), &schema).unwrap();
        batches
            .iter()
            .for_each(|batch| writer.write(batch).unwrap());
        let file = writer.into_inner().unwrap();

        let reader = FileReader::try_new(Cursor::new(file), None).unwrap();
        assert_eq!(reader.schema().metadata()["video"], "video.webm");
        let read: Vec<RecordBatch> = reader.collect::<Result<_, _>>().unwrap();
        assert_eq!(reassemble(&read), bytes);
    }
}
