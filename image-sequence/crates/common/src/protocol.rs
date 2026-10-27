//! The subset of the mosaicod protocol used to upload an image topic:
//!
//! 1. `sequence_create`  (`DoAction`)
//! 2. `session_create`   (`DoAction`) -> session uuid
//! 3. `topic_create`     (`DoAction`) -> topic uuid, format = IMAGE
//! 4. `DoPut`            -> one record batch per image
//! 5. `session_finalize` (`DoAction`)
//!
//! and to read topics back, as mosaicod's read path does:
//!
//! 1. `GetFlightInfo` on a sequence (`GetFlightInfoCmd`) -> one endpoint per
//!    topic, its ticket a `TicketTopic` and its metadata a `TopicAppMetadata`
//! 2. `DoGet` with each ticket -> the topic's record batches, sorted by
//!    `timestamp_ns`
//!
//! The protobuf messages are mirrored by hand from
//! `proto/mosaico/v1/{requests,responses,flight,time}.proto`: the tags must
//! match.

use std::sync::Arc;

use arrow_schema::{DataType, Field, Schema, SchemaRef};
use serde::{Deserialize, Serialize};

pub const SEQUENCE_CREATE: &str = "sequence_create";
pub const SESSION_CREATE: &str = "session_create";
pub const TOPIC_CREATE: &str = "topic_create";
pub const SESSION_FINALIZE: &str = "session_finalize";

/// `core.proto` Format enum: 1 = DEFAULT, 2 = RAGGED, 3 = IMAGE.
pub const FORMAT_IMAGE: i32 = 3;

/// Largest message mosaicod accepts by default
/// (`DEFAULT_GRPC_MAX_DECODE_MESSAGE_SIZE`). An 8K PNG of the dataset is at
/// most ~33 MiB, so one image always fits in a message.
pub const MAX_DECODE_MESSAGE_SIZE: usize = 50 * 1024 * 1024;

/// Index column every mosaicod topic needs: the capture time of the image.
pub const TIMESTAMP_COLUMN: &str = "timestamp_ns";

/// Largest message mosaicod sends (`GRPC_MSG_MAX_SIZE_BYTES`), whatever its
/// decode limit, so that stored data stays readable: a client reading topics
/// accepts messages this large.
pub const MAX_ENCODE_MESSAGE_SIZE: usize = 512 * 1024 * 1024;

/// The size mosaicod aims for in each `DoGet` message
/// (`DEFAULT_GRPC_TARGET_ENCODE_MESSAGE_SIZE`): rows per batch are this over
/// the topic's average row size, and a batch larger than this is split by
/// rows. A single row (an image) is never split.
pub const TARGET_ENCODE_MESSAGE_SIZE: usize = 25 * 1024 * 1024;

/// Most rows in a `DoGet` batch (`DEFAULT_MAX_BATCH_SIZE`).
pub const MAX_BATCH_SIZE: usize = 8192;

/// The topic, in a sequence, of a video's frames as images of `format`:
/// `<topic>/images/<format>`, e.g. `camera/images/avif`.
pub fn image_topic(topic: &str, format: &str) -> String {
    format!("{topic}/images/{format}")
}

/// requests.proto: SequenceCreate
#[derive(Clone, PartialEq, prost::Message)]
pub struct SequenceCreate {
    #[prost(string, tag = "1")]
    pub locator: String,
    /// Raw UTF-8 JSON; empty = no metadata.
    #[prost(bytes = "vec", tag = "2")]
    pub user_metadata: Vec<u8>,
}

/// requests.proto: ResourceLocator (body of `session_create`)
#[derive(Clone, PartialEq, prost::Message)]
pub struct ResourceLocator {
    #[prost(string, tag = "1")]
    pub locator: String,
}

/// requests.proto: TopicCreate
#[derive(Clone, PartialEq, prost::Message)]
pub struct TopicCreate {
    #[prost(string, tag = "1")]
    pub locator: String,
    #[prost(string, tag = "2")]
    pub session_uuid: String,
    #[prost(int32, tag = "3")]
    pub serialization_format: i32,
    #[prost(string, tag = "4")]
    pub ontology_tag: String,
    #[prost(bytes = "vec", tag = "5")]
    pub user_metadata: Vec<u8>,
}

/// requests.proto: SessionUuid (body of `session_finalize`)
#[derive(Clone, PartialEq, prost::Message)]
pub struct SessionUuid {
    #[prost(string, tag = "1")]
    pub session_uuid: String,
}

/// responses.proto: SessionCreate
#[derive(Clone, PartialEq, prost::Message)]
pub struct SessionCreateResponse {
    #[prost(string, tag = "1")]
    pub uuid: String,
    #[prost(string, tag = "2")]
    pub locator: String,
}

/// responses.proto: ResourceUuid (response of `topic_create`)
#[derive(Clone, PartialEq, prost::Message)]
pub struct ResourceUuid {
    #[prost(string, tag = "1")]
    pub uuid: String,
}

/// flight.proto: DoPutCmd (payload of `FlightDescriptor.cmd` for `DoPut`)
#[derive(Clone, PartialEq, prost::Message)]
pub struct DoPutCmd {
    #[prost(string, tag = "1")]
    pub locator: String,
    #[prost(string, tag = "2")]
    pub topic_uuid: String,
}

/// time.proto: TimestampRange, start inclusive, end exclusive.
#[derive(Clone, PartialEq, prost::Message)]
pub struct TimestampRange {
    #[prost(int64, optional, tag = "1")]
    pub start_ns: Option<i64>,
    #[prost(int64, optional, tag = "2")]
    pub end_ns: Option<i64>,
}

/// flight.proto: GetFlightInfoCmd (payload of `FlightDescriptor.cmd` for
/// `GetFlightInfo`): a sequence or a topic, and an optional time range.
#[derive(Clone, PartialEq, prost::Message)]
pub struct GetFlightInfoCmd {
    #[prost(string, tag = "1")]
    pub locator: String,
    #[prost(message, optional, tag = "2")]
    pub timestamp_range: Option<TimestampRange>,
}

/// flight.proto: TicketTopic (the `Ticket` of a `DoGet`): one topic, and an
/// optional time range.
#[derive(Clone, PartialEq, prost::Message)]
pub struct TicketTopic {
    #[prost(string, tag = "1")]
    pub locator: String,
    #[prost(message, optional, tag = "2")]
    pub timestamp_range: Option<TimestampRange>,
}

/// flight.proto: TopicAppMetadataDataInfo.
#[derive(Clone, PartialEq, prost::Message)]
pub struct TopicAppMetadataDataInfo {
    #[prost(message, optional, tag = "1")]
    pub interval: Option<TimestampRange>,
    #[prost(uint64, tag = "2")]
    pub total_row_count: u64,
    #[prost(uint64, tag = "3")]
    pub total_bytes: u64,
    #[prost(uint64, tag = "4")]
    pub total_chunks_count: u64,
}

/// flight.proto: TopicAppMetadata (the `app_metadata` of a topic's
/// `FlightEndpoint`), the fields this server fills.
#[derive(Clone, PartialEq, prost::Message)]
pub struct TopicAppMetadata {
    #[prost(string, tag = "4")]
    pub locator: String,
    #[prost(int32, tag = "6")]
    pub serialization_format: i32,
    /// User metadata, raw JSON: here the image format and the video it was
    /// made from (see [`ImageTopicMetadata`]).
    #[prost(bytes = "vec", tag = "7")]
    pub user_metadata: Vec<u8>,
    #[prost(message, optional, tag = "8")]
    pub data_info: Option<TopicAppMetadataDataInfo>,
}

/// The user metadata of an image topic served back: which format, and the
/// encoding of the video the images were made from.
#[derive(Debug, Serialize, Deserialize)]
pub struct ImageTopicMetadata {
    pub format: String,
    /// The frames' size: the reader of a format without a header (raw,
    /// zstd) needs it.
    pub width: u32,
    pub height: u32,
    pub preset: u8,
    pub crf: Option<u8>,
}

/// Schema of the image topic: one row per image.
pub fn image_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        Field::new(TIMESTAMP_COLUMN, DataType::Int64, false),
        Field::new("format", DataType::Utf8, false),
        Field::new("data", DataType::Binary, false),
    ]))
}

/// What the server reports at the end of a `DoPut`, as JSON in the
/// `PutResult` metadata. mosaicod sends no such report: it is here to check
/// the upload and time the encoding.
#[derive(Debug, Serialize, Deserialize)]
pub struct UploadReport {
    pub locator: String,
    pub pixel_format: crate::PixelFormat,
    /// SVT-AV1 preset and constant rate factor; no CRF for a lossless
    /// video.
    pub preset: u8,
    pub crf: Option<u8>,
    /// Images received.
    pub images: u64,
    /// Total size of the PNG files received.
    pub png_bytes: u64,
    pub width: u32,
    pub height: u32,
    /// Capture time of the first and last image.
    pub first_timestamp_ns: i64,
    pub last_timestamp_ns: i64,
    /// How the video's frames are timed, as the server chose from the
    /// capture times. Reports from before it was there are variable.
    #[serde(default)]
    pub timing: crate::timing::FrameTiming,
    /// The video written by the server.
    pub video_path: String,
    pub video_bytes: u64,
    /// From the first image received to the video closed.
    pub seconds: f64,
    /// Upload time: from the first message received to the last image
    /// received. The server takes the images only as fast as it decodes
    /// them (a few are queued), so this is the upload as the pipeline lets
    /// it go, not the network alone.
    pub receive_seconds: f64,
    /// Time spent decoding the PNGs and converting them to YUV.
    pub decode_seconds: f64,
    /// Encoding wall time, from the first frame to the video closed
    /// (SVT-AV1 encodes on its own threads).
    pub encode_seconds: f64,
    /// Part of `encode_seconds` the encoder waited for decoded frames: near
    /// 0 when the encoder, not PNG decoding, is the bottleneck.
    pub encode_wait_seconds: f64,
    /// When each image went through the server, in order: what overlaps
    /// with what, and how long each one waited in the queues.
    #[serde(default)]
    pub frames: Vec<FrameTimes>,
}

/// When one image went through the server's pipeline, in seconds since the
/// first message of the `DoPut`. Between `received` and `decode_start` it
/// waits in the decode queue, between `decoded` and `encode_start` in the
/// encoder's queue.
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct FrameTimes {
    pub received: f64,
    pub decode_start: f64,
    pub decoded: f64,
    pub encode_start: f64,
    pub encoded: f64,
}

/// Characters mosaicod rejects in sequence and topic names.
const INVALID_LOCATOR_CHARS: &[char] = &['!', '"', '\'', '*', '£', '$', '%', '&', '.', ' ', ':'];

/// Whether `part` (a sequence name, or a topic name, which may contain `/`)
/// is a name mosaicod accepts.
fn valid_locator_part(part: &str) -> bool {
    !(part.is_empty()
        || part.starts_with('/')
        || part.ends_with('/')
        || part.contains("//")
        || part.chars().any(|c| !c.is_ascii() || c.is_ascii_control() || INVALID_LOCATOR_CHARS.contains(&c)))
}

/// Whether `name` is a sequence name mosaicod accepts (no `/`).
pub fn valid_sequence_name(name: &str) -> bool {
    valid_locator_part(name) && !name.contains('/')
}

/// Splits a `<sequence>/<topic>` locator, rejecting the characters mosaicod
/// rejects. The topic may itself contain `/`.
pub fn parse_topic_locator(locator: &str) -> Option<(&str, &str)> {
    let (sequence, topic) = locator.split_once('/')?;
    (valid_sequence_name(sequence) && valid_locator_part(topic)).then_some((sequence, topic))
}

/// Turns a name into a valid locator part: every character other than ASCII
/// letters, digits, `-` and `_` becomes `_`.
pub fn locator_part(name: &str) -> String {
    name.chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' })
        .collect()
}
