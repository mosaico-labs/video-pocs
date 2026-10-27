//! A video's frames as image topics, read back by a client as mosaicod's
//! read path serves a topic:
//!
//! ```text
//! <video dir>/images/<format>.parquet   one image topic per format (store)
//! ```
//!
//! **Uploading**: the capture times the client sends (the `timestamp_ns` of
//! each image) are saved as they are, one row per image in upload order, in
//! `<topic>.timestamps.parquet` next to the video ([`write_timestamps`]).
//!
//! **Storing** ([`store`], at the end of `server --extract-images`): each
//! format's images, just written, go into one Parquet file laid out as
//! mosaicod stores an image topic: the rows of the topic's schema
//! (`timestamp_ns`, `format`, `data`) in capture order, `timestamp_ns` a copy
//! of the uploaded times (frame k is image k: the video drops and repeats
//! none), not the video's own, which are only to the millisecond. Parquet 2.0, the
//! image bytes ZSTD level 5 with no dictionary and no statistics, the
//! timestamps uncompressed with page statistics and a bloom filter, and a row
//! group closed whenever ~70 MiB are buffered (mosaicod's in-memory encoding
//! buffer).
//!
//! **Reading** (the server's `GetFlightInfo` and `DoGet`): a sequence's topic
//! `<topic>/images/<format>` (see [`common::protocol::image_topic`]):
//!
//! - [`ImageTopics::flight_info`]: one endpoint per topic (every format
//!   stored, for a sequence), its ticket a `TicketTopic` and its metadata a
//!   `TopicAppMetadata`, as mosaicod answers;
//! - [`ImageTopics::do_get`]: the topic's rows read from its Parquet file,
//!   in batches of as many rows as fit mosaicod's target message size (25
//!   MiB) at the topic's average row size, and encoded with Arrow Flight's
//!   encoder as mosaicod does: a batch over the target is split by rows, but
//!   one row (an image) is never split, and the IPC data is LZ4-compressed,
//!   or not (`--do-get-compression`, to measure what LZ4 costs).
//!
//! mosaicod reads the Parquet files with DataFusion, sorted by
//! `timestamp_ns`; one file written in capture order is read here in order
//! with the Parquet reader, which gives the same rows without a query engine.

use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};

use anyhow::{Context, Result};
use arrow_array::{Array, BinaryArray, Int64Array, RecordBatch, RecordBatchReader, StringArray};
use arrow_flight::{
    FlightData, FlightDescriptor, FlightEndpoint, FlightInfo, Ticket, encode::FlightDataEncoderBuilder,
    error::FlightError,
};
use arrow_ipc::{CompressionType, writer::IpcWriteOptions};
use common::{
    DEFAULT_TOPIC,
    protocol::{
        FORMAT_IMAGE, GetFlightInfoCmd, ImageTopicMetadata, MAX_BATCH_SIZE, TARGET_ENCODE_MESSAGE_SIZE, TIMESTAMP_COLUMN,
        TicketTopic, TimestampRange, TopicAppMetadata, TopicAppMetadataDataInfo, image_schema, image_topic,
        parse_topic_locator,
    },
};
use formats::{FORMATS, Format};
use futures::{StreamExt, stream::BoxStream};
use parquet::{
    arrow::{ArrowWriter, ProjectionMask, arrow_reader::ParquetRecordBatchReaderBuilder},
    basic::{Compression, ZstdLevel},
    file::properties::{EnabledStatistics, WriterProperties, WriterVersion},
    schema::types::ColumnPath,
};
use prost::Message;
use tonic::Status;

use crate::{
    images::{image_path, images_dir, read_frames},
    internal,
};

/// mosaicod's in-memory Parquet encoding buffer
/// (`DEFAULT_PARQUET_IN_MEMORY_ENCODING_BUFFER_SIZE`): a row group is closed
/// once this much is buffered.
const ENCODING_BUFFER: usize = 70 * 1024 * 1024;

/// mosaicod's compression of the image column.
const IMAGE_ZSTD_LEVEL: i32 = 5;

/// The Parquet file of `video`'s images in `format`.
pub fn parquet_path(video: &Path, format: &Format) -> PathBuf {
    images_dir(video).join(format!("{}.parquet", format.name))
}

/// Writes a Parquet file at `path` through `write`, which fills the writer:
/// first to `<path>.tmp`, renamed to `path` only once complete, so a failure
/// part-way leaves no file to serve (the temporary one is removed).
fn write_parquet(
    path: &Path,
    schema: arrow_schema::SchemaRef,
    write: impl FnOnce(&mut ArrowWriter<File>) -> Result<()>,
) -> Result<()> {
    let temporary = path.with_extension("parquet.tmp");
    let written = (|| -> Result<()> {
        let mut writer = ArrowWriter::try_new(File::create(&temporary)?, schema, Some(writer_properties()?))?;
        write(&mut writer)?;
        writer.close()?;
        Ok(fs::rename(&temporary, path)?)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    written.with_context(|| format!("writing {}", path.display()))
}

/// Writes the uploaded capture times to `path`: a `timestamp_ns` column,
/// one row per image, with mosaicod's settings for that column.
pub fn write_timestamps(path: &Path, captures: &[i64]) -> Result<()> {
    let schema = Arc::new(arrow_schema::Schema::new(vec![arrow_schema::Field::new(
        TIMESTAMP_COLUMN,
        arrow_schema::DataType::Int64,
        false,
    )]));
    write_parquet(path, schema.clone(), |writer| {
        Ok(writer.write(&RecordBatch::try_new(schema, vec![Arc::new(Int64Array::from(captures.to_vec()))])?)?)
    })
}

/// The capture times uploaded for the video, from [`write_timestamps`].
fn read_timestamps(path: &Path) -> Result<Vec<i64>> {
    let file = File::open(path).with_context(|| {
        format!("opening {} (the upload's capture times: upload the batch again)", path.display())
    })?;
    let mut captures = Vec::new();
    for batch in ParquetRecordBatchReaderBuilder::try_new(file)?.build()? {
        let batch = batch?;
        let times = batch
            .column_by_name(TIMESTAMP_COLUMN)
            .and_then(|c| c.as_any().downcast_ref::<Int64Array>())
            .context("no Int64 timestamp_ns column")?;
        captures.extend_from_slice(times.values());
    }
    Ok(captures)
}

/// How far an uploaded capture time may be from its frame's time in the
/// video: half the video's unit of time (a millisecond).
const VIDEO_ROUNDING_NS: i64 = common::timing::TIMESTAMP_UNIT_NS / 2;

/// mosaicod's writer properties for an image topic.
fn writer_properties() -> Result<WriterProperties> {
    let timestamps = ColumnPath::from(TIMESTAMP_COLUMN);
    Ok(WriterProperties::builder()
        .set_writer_version(WriterVersion::PARQUET_2_0)
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(IMAGE_ZSTD_LEVEL)?))
        .set_dictionary_enabled(false)
        .set_statistics_enabled(EnabledStatistics::None)
        .set_column_compression(timestamps.clone(), Compression::UNCOMPRESSED)
        .set_column_statistics_enabled(timestamps.clone(), EnabledStatistics::Page)
        .set_column_bloom_filter_enabled(timestamps, true)
        .build())
}

/// Writes every format's images of `video` (from `--extract-images`) into
/// one Parquet file per format, their `timestamp_ns` copied from the
/// uploaded capture times, and returns, per format, the file's size and the
/// seconds it took. Fails unless there is one uploaded time per frame, each
/// within the video's rounding of the frame's time.
pub fn store(video: &Path) -> Result<Vec<(&'static str, u64, f64)>> {
    let frames = read_frames(video)?.capture_ns;
    let captures = read_timestamps(&common::timestamps_path(video))?;
    anyhow::ensure!(
        captures.len() == frames.len(),
        "{} capture times were uploaded for {} frames",
        captures.len(),
        frames.len()
    );
    for (k, (&uploaded, &frame)) in captures.iter().zip(&frames).enumerate() {
        anyhow::ensure!(
            (uploaded - frame).abs() <= VIDEO_ROUNDING_NS,
            "frame {k} is at {frame} ns in the video, but image {k} was uploaded at {uploaded} ns"
        );
    }
    let mut stored = Vec::new();
    for format in FORMATS {
        let started = Instant::now();
        let path = parquet_path(video, format);
        write_parquet(&path, image_schema(), |writer| {
            for (k, &capture_ns) in captures.iter().enumerate() {
                let image = image_path(video, format, k);
                let bytes = fs::read(&image).with_context(|| format!("reading {}", image.display()))?;
                writer.write(&RecordBatch::try_new(
                    image_schema(),
                    vec![
                        Arc::new(Int64Array::from(vec![capture_ns])),
                        Arc::new(StringArray::from(vec![format.name])),
                        Arc::new(BinaryArray::from(vec![bytes.as_slice()])),
                    ],
                )?)?;
                if writer.in_progress_size() >= ENCODING_BUFFER {
                    writer.flush()?;
                }
            }
            Ok(())
        })?;
        stored.push((format.name, fs::metadata(&path)?.len(), started.elapsed().as_secs_f64()));
    }
    Ok(stored)
}

/// The last `timestamp_ns` of `batch`.
fn last_timestamp(batch: &RecordBatch) -> Option<i64> {
    let times = batch.column_by_name(TIMESTAMP_COLUMN)?.as_any().downcast_ref::<Int64Array>()?;
    times.values().last().copied()
}

/// The rows of `batch` whose `timestamp_ns` is in `range` (start inclusive,
/// end exclusive), the rows being in capture order.
fn in_range(batch: RecordBatch, range: Option<&TimestampRange>) -> RecordBatch {
    let Some(range) = range else { return batch };
    let Some(times) = batch.column_by_name(TIMESTAMP_COLUMN).and_then(|c| c.as_any().downcast_ref::<Int64Array>())
    else {
        return batch;
    };
    let times = times.values();
    let start = range.start_ns.map_or(0, |s| times.partition_point(|&t| t < s));
    let end = range.end_ns.map_or(times.len(), |e| times.partition_point(|&t| t < e));
    batch.slice(start, end.saturating_sub(start))
}

/// The image topics of the videos under `out_dir` encoded with one preset
/// and CRF (the server's), served back to clients.
pub struct ImageTopics {
    pub out_dir: PathBuf,
    pub preset: u8,
    pub crf: Option<u8>,
    /// IPC compression of `DoGet` streams: LZ4, as mosaicod, or none.
    pub compression: Option<CompressionType>,
}

impl ImageTopics {
    /// The format and Parquet file of a topic locator
    /// (`<sequence>/<topic>/images/<format>`).
    fn resolve(&self, locator: &str) -> Result<(&'static Format, PathBuf), Status> {
        let bad = || Status::not_found(format!("no image topic {locator}"));
        let (sequence, topic) = parse_topic_locator(locator).ok_or_else(bad)?;
        let format = FORMATS.iter().find(|f| image_topic(DEFAULT_TOPIC, f.name) == topic).ok_or_else(bad)?;
        let path = parquet_path(&self.video(sequence), format);
        if !path.exists() {
            return Err(Status::not_found(format!("{locator} is not stored (run --extract-images)")));
        }
        Ok((format, path))
    }

    fn video(&self, sequence: &str) -> PathBuf {
        common::video_path(&self.out_dir, &format!("{sequence}/{DEFAULT_TOPIC}"), self.preset, self.crf)
    }

    /// One endpoint per topic, as mosaicod's `GetFlightInfo`: for a sequence,
    /// every format stored; for a topic, that topic.
    pub fn flight_info(&self, descriptor: FlightDescriptor) -> Result<FlightInfo, Status> {
        let cmd = GetFlightInfoCmd::decode(descriptor.cmd.as_ref())
            .map_err(|e| Status::invalid_argument(format!("bad GetFlightInfoCmd: {e}")))?;
        let locators: Vec<String> = if cmd.locator.contains('/') {
            vec![cmd.locator.clone()]
        } else {
            FORMATS
                .iter()
                .filter(|f| parquet_path(&self.video(&cmd.locator), f).exists())
                .map(|f| format!("{}/{}", cmd.locator, image_topic(DEFAULT_TOPIC, f.name)))
                .collect()
        };
        if locators.is_empty() {
            return Err(Status::not_found(format!("no image topics in {} (run --extract-images)", cmd.locator)));
        }
        // Every topic first: an unknown one is not found, before anything is read.
        let topics = locators
            .into_iter()
            .map(|locator| self.resolve(&locator).map(|(format, path)| (locator, format, path)))
            .collect::<Result<Vec<_>, Status>>()?;
        let mut info = FlightInfo::new().try_with_schema(&image_schema()).map_err(internal)?.with_descriptor(descriptor);
        // The frames' size, from the extraction's report.
        let sequence = cmd.locator.split('/').next().unwrap_or_default();
        let frames = read_frames(&self.video(sequence)).map_err(internal)?;
        let (width, height) = (frames.width, frames.height);
        let (mut rows, mut bytes) = (0, 0);
        for (locator, format, path) in topics {
            let file_bytes = fs::metadata(&path).map_err(internal)?.len();
            let (count, first, last) = timestamps(&path).map_err(internal)?;
            rows += count;
            bytes += file_bytes;
            let metadata = TopicAppMetadata {
                locator: locator.clone(),
                serialization_format: FORMAT_IMAGE,
                user_metadata: serde_json::to_vec(&ImageTopicMetadata {
                    format: format.name.to_string(),
                    width,
                    height,
                    preset: self.preset,
                    crf: self.crf,
                })
                .map_err(internal)?,
                data_info: Some(TopicAppMetadataDataInfo {
                    interval: Some(TimestampRange { start_ns: Some(first), end_ns: Some(last + 1) }),
                    total_row_count: count,
                    total_bytes: file_bytes,
                    total_chunks_count: 1,
                }),
            };
            let ticket = TicketTopic { locator, timestamp_range: cmd.timestamp_range.clone() };
            info = info.with_endpoint(
                FlightEndpoint::new()
                    .with_ticket(Ticket { ticket: ticket.encode_to_vec().into() })
                    .with_app_metadata(metadata.encode_to_vec()),
            );
        }
        Ok(info.with_total_records(rows as i64).with_total_bytes(bytes as i64))
    }

    /// The topic's rows, as mosaicod's `DoGet`: read from its Parquet file
    /// in batches sized for the target message size, and encoded.
    pub fn do_get(&self, ticket: Ticket) -> Result<BoxStream<'static, Result<FlightData, Status>>, Status> {
        let ticket = TicketTopic::decode(ticket.ticket.as_ref())
            .map_err(|e| Status::invalid_argument(format!("bad TicketTopic: {e}")))?;
        let (_, path) = self.resolve(&ticket.locator)?;
        let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(&path).map_err(internal)?).map_err(internal)?;
        // mosaicod's rows per batch: its target message size over the
        // topic's average row size (in memory, uncompressed).
        let metadata = builder.metadata();
        let rows = metadata.file_metadata().num_rows().max(1) as usize;
        let row_bytes = metadata.row_groups().iter().map(|g| g.total_byte_size() as usize).sum::<usize>() / rows;
        let batch_size = (TARGET_ENCODE_MESSAGE_SIZE / row_bytes.max(1)).clamp(1, MAX_BATCH_SIZE);
        let reader = builder.with_batch_size(batch_size).build().map_err(internal)?;
        let schema = reader.schema();

        // Read on a thread of its own, a batch or two ahead of the encoder.
        let (sender, receiver) = tokio::sync::mpsc::channel::<Result<RecordBatch, FlightError>>(2);
        let range = ticket.timestamp_range;
        tokio::task::spawn_blocking(move || {
            // A panic reading the file reaches the client as an error, not as
            // a stream that just ends early.
            let read = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                for batch in reader {
                    let batch = batch.map_err(|e| FlightError::ExternalError(e.into()));
                    // Rows are in capture order: once past the range's end, stop.
                    let past = matches!((&batch, range.as_ref().and_then(|r| r.end_ns)), (Ok(b), Some(end))
                        if last_timestamp(b).is_some_and(|t| t >= end));
                    let batch = batch.map(|b| in_range(b, range.as_ref()));
                    if !matches!(&batch, Ok(b) if b.num_rows() == 0) && sender.blocking_send(batch).is_err() {
                        return; // the client went away
                    }
                    if past {
                        return;
                    }
                }
            }));
            if read.is_err() {
                let _ = sender.blocking_send(Err(FlightError::ProtocolError("reading the topic panicked".into())));
            }
        });
        let batches = futures::stream::unfold(receiver, |mut receiver| async {
            receiver.recv().await.map(|batch| (batch, receiver))
        });
        let options = IpcWriteOptions::default().try_with_compression(self.compression).map_err(internal)?;
        Ok(FlightDataEncoderBuilder::new()
            .with_schema(schema)
            .with_options(options)
            .with_max_flight_data_size(TARGET_ENCODE_MESSAGE_SIZE)
            .build(batches)
            .map(|data| data.map_err(Status::from))
            .boxed())
    }
}

/// How many rows the topic file at `path` holds, and its first and last
/// `timestamp_ns`.
fn timestamps(path: &Path) -> Result<(u64, i64, i64)> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(path)?)?;
    let mask = ProjectionMask::columns(builder.parquet_schema(), [TIMESTAMP_COLUMN]);
    let (mut count, mut first, mut last) = (0u64, i64::MAX, i64::MIN);
    for batch in builder.with_projection(mask).build()? {
        let batch = batch?;
        let times = batch.column(0).as_any().downcast_ref::<Int64Array>().context("timestamp_ns is not Int64")?;
        count += times.len() as u64;
        if !times.is_empty() {
            first = first.min(times.value(0));
            last = last.max(times.value(times.len() - 1));
        }
    }
    Ok((count, first, last))
}
