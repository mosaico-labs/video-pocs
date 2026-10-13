//! Arrow Flight server: receives one `DoPut` per video and stores it the way
//! mosaicod stores a topic: one Parquet file per record batch. `DoGet` streams
//! a topic back from its Parquet files, as mosaicod does.
//!
//! ```text
//! <out-dir>/<sequence>/<topic>/metadata.json
//! <out-dir>/<sequence>/<topic>/data/00000.parquet, 00001.parquet, ...
//! ```
//!
//! mosaicod names the topic folder `tp_<ULID>` and keeps the locator in its
//! database; here the folder is named after the locator so it can be found.

use std::{
    fmt::Display,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};

use arrow_array::{Array, BinaryArray, RecordBatch, UInt32Array};
use arrow_flight::{
    Action, ActionType, Criteria, Empty, FlightData, FlightDescriptor, FlightInfo,
    HandshakeRequest, HandshakeResponse, PollInfo, PutResult, SchemaResult, Ticket,
    decode::{DecodedFlightData, DecodedPayload, FlightDataDecoder},
    encode::FlightDataEncoderBuilder,
    error::FlightError,
    flight_descriptor::DescriptorType,
    flight_service_server::{FlightService, FlightServiceServer},
};
use arrow_ipc::{CompressionType, writer::IpcWriteOptions};
use arrow_schema::{DataType, SchemaRef};
use clap::Parser;
use futures::{
    StreamExt, TryStreamExt,
    stream::{self, BoxStream},
};
use parquet::{arrow::arrow_reader::ParquetRecordBatchReaderBuilder, errors::ParquetError};
use poc1::{
    DoPutCmd, Format, GetAck, MAX_DECODE_MESSAGE_SIZE, MAX_ENCODE_MESSAGE_SIZE, PutAck,
    TARGET_ENCODE_MESSAGE_SIZE, TIMESTAMP_COLUMN, TicketTopic, TimestampRange, encode_parquet,
    parse_topic_locator, slice_to_range, wire_size,
};
use prost::Message;
use serde::Serialize;
use tonic::{Request, Response, Status, Streaming, transport::Server};

#[derive(Parser)]
#[command(about = "Arrow Flight server that stores uploaded video chunks as mosaicod does")]
struct Args {
    /// Address to listen on.
    #[arg(long, default_value = "127.0.0.1:50051")]
    addr: SocketAddr,
    /// Directory where the topic folders are written.
    #[arg(long, default_value = "received")]
    out_dir: PathBuf,
    /// Serialization format of the Parquet files. In mosaicod it is chosen
    /// when the topic is created.
    #[arg(long, value_enum, default_value_t = Format::Video)]
    format: Format,
    /// Largest message accepted, in bytes (mosaicod default: 50 MiB).
    #[arg(long, default_value_t = MAX_DECODE_MESSAGE_SIZE)]
    max_decode_message_size: usize,
    /// Compression of the Arrow IPC messages `DoGet` sends (mosaicod: always
    /// LZ4).
    #[arg(long, value_enum, default_value_t = GetCompression::Lz4)]
    get_compression: GetCompression,
}

#[derive(Clone, Copy, clap::ValueEnum)]
enum GetCompression {
    Lz4,
    None,
}

struct VideoFlightService {
    out_dir: PathBuf,
    format: Format,
    get_compression: GetCompression,
}

/// Content of `metadata.json`, a reduced version of mosaicod's
/// `JsonTopicMetadata`.
#[derive(Serialize)]
struct TopicMetadata<'a> {
    locator: &'a str,
    topic_uuid: &'a str,
    serialization_format: Format,
    ontology_tag: &'static str,
    created_at: u64,
}

fn internal(error: impl Display) -> Status {
    Status::internal(error.to_string())
}

/// Reads the command and the schema from the first message, as mosaicod's
/// `extract_command_and_schema_from_header_message` does.
fn header(first: DecodedFlightData) -> Result<(DoPutCmd, SchemaRef), Status> {
    let descriptor = first
        .inner
        .flight_descriptor
        .as_ref()
        .ok_or_else(|| Status::invalid_argument("missing flight descriptor"))?;
    if descriptor.r#type() != DescriptorType::Cmd {
        return Err(Status::invalid_argument(
            "unsupported descriptor: DoPut needs a CMD descriptor holding a DoPutCmd",
        ));
    }
    let cmd = DoPutCmd::decode(descriptor.cmd.as_ref())
        .map_err(|e| Status::invalid_argument(format!("bad DoPutCmd: {e}")))?;
    match first.payload {
        DecodedPayload::Schema(schema) => Ok((cmd, schema)),
        _ => Err(Status::invalid_argument(
            "missing schema in the first message",
        )),
    }
}

/// mosaicod's `check_schema`, plus the columns this server reads.
fn check_schema(schema: &SchemaRef) -> Result<(), Status> {
    for (name, data_type) in [
        (TIMESTAMP_COLUMN, DataType::Int64),
        ("chunk_index", DataType::UInt32),
        ("data", DataType::Binary),
    ] {
        let field = schema
            .field_with_name(name)
            .map_err(|_| Status::invalid_argument(format!("missing column {name}")))?;
        if *field.data_type() != data_type {
            return Err(Status::invalid_argument(format!(
                "{name} must be {data_type}"
            )));
        }
    }
    Ok(())
}

/// Checks that chunks arrive in order and adds them to the running CRC.
fn consume_batch(
    batch: &RecordBatch,
    ack: &mut PutAck,
    hasher: &mut crc32fast::Hasher,
) -> Result<(), Status> {
    let indexes = batch
        .column_by_name("chunk_index")
        .and_then(|c| c.as_any().downcast_ref::<UInt32Array>())
        .ok_or_else(|| Status::invalid_argument("chunk_index must be UInt32"))?;
    let chunks = batch
        .column_by_name("data")
        .and_then(|c| c.as_any().downcast_ref::<BinaryArray>())
        .ok_or_else(|| Status::invalid_argument("data must be Binary"))?;

    for (index, chunk) in indexes.values().iter().zip(chunks.iter()) {
        if u64::from(*index) != ack.rows {
            return Err(Status::invalid_argument(format!(
                "chunk {index} arrived, expected {}",
                ack.rows
            )));
        }
        let chunk = chunk.unwrap_or_default();
        hasher.update(chunk);
        ack.rows += 1;
        ack.payload_bytes += chunk.len() as u64;
    }
    ack.batches += 1;
    Ok(())
}

impl VideoFlightService {
    async fn receive(&self, input: Streaming<FlightData>) -> Result<PutAck, Status> {
        let mut decoder = FlightDataDecoder::new(input.map_err(FlightError::from));
        let first = decoder
            .next()
            .await
            .ok_or_else(|| Status::invalid_argument("empty DoPut stream"))?
            .map_err(Status::from)?;
        let started = Instant::now();
        let first_wire_bytes = wire_size(&first.inner);
        let (cmd, schema) = header(first)?;
        let (sequence, topic) = parse_topic_locator(&cmd.locator)
            .ok_or_else(|| Status::invalid_argument(format!("bad locator: {:?}", cmd.locator)))?;
        cmd.topic_uuid
            .parse::<uuid::Uuid>()
            .map_err(|_| Status::invalid_argument(format!("bad uuid: {:?}", cmd.topic_uuid)))?;
        check_schema(&schema)?;

        // mosaicod only accepts data for an empty topic.
        let topic_dir = self.out_dir.join(sequence).join(topic);
        if topic_dir.exists() {
            return Err(Status::already_exists(format!(
                "topic {} already has data",
                cmd.locator
            )));
        }
        let data_dir = topic_dir.join("data");
        tokio::fs::create_dir_all(&data_dir)
            .await
            .map_err(internal)?;
        let metadata = TopicMetadata {
            locator: &cmd.locator,
            topic_uuid: &cmd.topic_uuid,
            serialization_format: self.format,
            ontology_tag: "video_chunk",
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(internal)?
                .as_nanos() as u64,
        };
        tokio::fs::write(
            topic_dir.join("metadata.json"),
            serde_json::to_vec_pretty(&metadata).map_err(internal)?,
        )
        .await
        .map_err(internal)?;

        let mut hasher = crc32fast::Hasher::new();
        let mut ack = PutAck {
            locator: cmd.locator.clone(),
            format: self.format,
            messages: 1,
            batches: 0,
            rows: 0,
            payload_bytes: 0,
            wire_bytes: first_wire_bytes,
            crc32: 0,
            receive_ms: 0.0,
            encode_ms: 0.0,
            store_ms: 0.0,
            stored_path: topic_dir.display().to_string(),
            stored_files: 0,
            stored_bytes: 0,
        };

        while let Some(decoded) = decoder.next().await {
            let decoded = decoded.map_err(Status::from)?;
            ack.messages += 1;
            ack.wire_bytes += wire_size(&decoded.inner);
            let DecodedPayload::RecordBatch(batch) = decoded.payload else {
                // mosaicod: `unsupported_stream_message`.
                return Err(Status::invalid_argument(
                    "only record batches may follow the first message",
                ));
            };
            consume_batch(&batch, &mut ack, &mut hasher)?;

            // As mosaicod: every batch becomes a whole Parquet file, encoded
            // in memory on the blocking pool and then written to the store.
            let encode_started = Instant::now();
            let format = self.format;
            let buffer = tokio::task::spawn_blocking(move || encode_parquet(&batch, format))
                .await
                .map_err(internal)?
                .map_err(internal)?;
            ack.encode_ms += encode_started.elapsed().as_secs_f64() * 1e3;

            let store_started = Instant::now();
            let path = data_dir.join(format!("{:05}.parquet", ack.stored_files));
            ack.stored_bytes += buffer.len() as u64;
            tokio::fs::write(&path, buffer).await.map_err(internal)?;
            ack.store_ms += store_started.elapsed().as_secs_f64() * 1e3;
            ack.stored_files += 1;
        }
        ack.receive_ms = started.elapsed().as_secs_f64() * 1e3;
        ack.crc32 = hasher.finalize();

        eprintln!(
            "stored {}: {} chunks, {} payload bytes, {} wire bytes in {:.1} ms -> {} ({} {} files, {} bytes)",
            ack.locator,
            ack.rows,
            ack.payload_bytes,
            ack.wire_bytes,
            ack.receive_ms,
            ack.stored_path,
            ack.stored_files,
            ack.format.name(),
            ack.stored_bytes
        );
        Ok(ack)
    }
}

/// IPC message header of type `NONE`: a Flight message that carries only
/// `app_metadata`, which decoders pass on without data.
fn metadata_only_header() -> Vec<u8> {
    let mut fbb = flatbuffers::FlatBufferBuilder::new();
    let mut message = arrow_ipc::MessageBuilder::new(&mut fbb);
    message.add_version(arrow_ipc::MetadataVersion::V5);
    message.add_header_type(arrow_ipc::MessageHeader::NONE);
    message.add_bodyLength(0);
    let message = message.finish();
    fbb.finish(message, None);
    fbb.finished_data().to_vec()
}

/// Parquet files of a topic, in the order they were written.
async fn parquet_files(data_dir: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    let mut entries = tokio::fs::read_dir(data_dir).await?;
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        // `00000.parquet`, `00001.parquet`, ...: past 99999 the names get
        // longer, so they are sorted by number, not by name.
        if let Some(number) = path
            .extension()
            .filter(|ext| *ext == "parquet")
            .and_then(|_| path.file_stem()?.to_str()?.parse::<u64>().ok())
        {
            files.push((number, path));
        }
    }
    files.sort();
    Ok(files.into_iter().map(|(_, path)| path).collect())
}

/// Reads one Parquet file into record batches, keeping the rows inside
/// `range`, and adds the work to `ack`.
async fn read_parquet(
    path: PathBuf,
    range: TimestampRange,
    ack: Arc<Mutex<GetAck>>,
) -> Result<Vec<RecordBatch>, Status> {
    let read_started = Instant::now();
    let file = tokio::fs::read(&path).await.map_err(internal)?;
    let read_ms = read_started.elapsed().as_secs_f64() * 1e3;

    let decode_started = Instant::now();
    let batches = tokio::task::spawn_blocking(move || -> Result<Vec<RecordBatch>, ParquetError> {
        let builder = ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::from(file))?;
        // The reader drops the schema metadata (video name and file size)
        // from its batches; mosaicod keeps it, so it is put back.
        let schema = builder.schema().clone();
        builder
            .build()?
            .map(|batch| Ok(batch?.with_schema(schema.clone())?))
            .collect()
    })
    .await
    .map_err(internal)?
    .map_err(internal)?;
    let decode_ms = decode_started.elapsed().as_secs_f64() * 1e3;

    let batches: Vec<RecordBatch> = batches
        .iter()
        .filter_map(|batch| slice_to_range(batch, &range))
        .collect();
    let mut ack = ack.lock().unwrap();
    ack.files += 1;
    ack.read_ms += read_ms;
    ack.decode_ms += decode_ms;
    for batch in &batches {
        ack.batches += 1;
        ack.rows += batch.num_rows() as u64;
        if let Some(chunks) = batch
            .column_by_name("data")
            .and_then(|c| c.as_any().downcast_ref::<BinaryArray>())
        {
            ack.payload_bytes += chunks
                .iter()
                .map(|c| c.map_or(0, <[u8]>::len) as u64)
                .sum::<u64>();
        }
    }
    Ok(batches)
}

impl VideoFlightService {
    /// Streams a topic back from its Parquet files, one file at a time, as
    /// record batches followed by a message carrying the [`GetAck`].
    async fn send(
        &self,
        ticket: Ticket,
    ) -> Result<BoxStream<'static, Result<FlightData, Status>>, Status> {
        let started = Instant::now();
        let ticket = TicketTopic::decode(ticket.ticket.as_ref())
            .map_err(|e| Status::invalid_argument(format!("bad TicketTopic: {e}")))?;
        let (sequence, topic) = parse_topic_locator(&ticket.locator).ok_or_else(|| {
            Status::invalid_argument(format!("bad locator: {:?}", ticket.locator))
        })?;
        let files = parquet_files(&self.out_dir.join(sequence).join(topic).join("data"))
            .await
            .map_err(|_| Status::not_found(format!("topic {} not found", ticket.locator)))?;
        let range = ticket.timestamp_range.unwrap_or_default();
        let ack = Arc::new(Mutex::new(GetAck {
            locator: ticket.locator.clone(),
            ..GetAck::default()
        }));

        let batches = {
            let ack = ack.clone();
            stream::iter(files)
                .then(move |path| read_parquet(path, range, ack.clone()))
                .map_ok(|batches| stream::iter(batches.into_iter().map(Ok::<_, Status>)))
                .try_flatten()
                .map_err(FlightError::from)
        };
        // As mosaicod: LZ4 frames, and messages of up to 25 MiB.
        let options = match self.get_compression {
            GetCompression::Lz4 => IpcWriteOptions::default()
                .try_with_compression(Some(CompressionType::LZ4_FRAME))
                .map_err(internal)?,
            GetCompression::None => IpcWriteOptions::default(),
        };
        let data = FlightDataEncoderBuilder::new()
            .with_options(options)
            .with_max_flight_data_size(TARGET_ENCODE_MESSAGE_SIZE)
            .build(batches)
            .map_err(Status::from);

        // Polled only once every batch has been sent. mosaicod sends no such
        // message; clients that only read record batches skip it.
        let trailer = stream::once(async move {
            let mut ack = ack.lock().unwrap().clone();
            ack.send_ms = started.elapsed().as_secs_f64() * 1e3;
            eprintln!(
                "sent {}: {} chunks, {} payload bytes from {} Parquet files in {:.1} ms \
                 (read {:.1} ms, decode {:.1} ms)",
                ack.locator,
                ack.rows,
                ack.payload_bytes,
                ack.files,
                ack.send_ms,
                ack.read_ms,
                ack.decode_ms
            );
            Ok(FlightData {
                data_header: metadata_only_header().into(),
                app_metadata: serde_json::to_vec(&ack).map_err(internal)?.into(),
                ..FlightData::default()
            })
        });
        Ok(data.chain(trailer).boxed())
    }
}

#[tonic::async_trait]
impl FlightService for VideoFlightService {
    type HandshakeStream = BoxStream<'static, Result<HandshakeResponse, Status>>;
    type ListFlightsStream = BoxStream<'static, Result<FlightInfo, Status>>;
    type DoGetStream = BoxStream<'static, Result<FlightData, Status>>;
    type DoPutStream = BoxStream<'static, Result<PutResult, Status>>;
    type DoExchangeStream = BoxStream<'static, Result<FlightData, Status>>;
    type DoActionStream = BoxStream<'static, Result<arrow_flight::Result, Status>>;
    type ListActionsStream = BoxStream<'static, Result<ActionType, Status>>;

    async fn do_put(
        &self,
        request: Request<Streaming<FlightData>>,
    ) -> Result<Response<Self::DoPutStream>, Status> {
        let ack = self.receive(request.into_inner()).await?;
        let result = PutResult {
            app_metadata: serde_json::to_vec(&ack).map_err(internal)?.into(),
        };
        Ok(Response::new(stream::once(async { Ok(result) }).boxed()))
    }

    async fn do_action(
        &self,
        _: Request<Action>,
    ) -> Result<Response<Self::DoActionStream>, Status> {
        Err(Status::unimplemented("do_action"))
    }

    async fn list_actions(
        &self,
        _: Request<Empty>,
    ) -> Result<Response<Self::ListActionsStream>, Status> {
        Err(Status::unimplemented("list_actions"))
    }

    async fn handshake(
        &self,
        _: Request<Streaming<HandshakeRequest>>,
    ) -> Result<Response<Self::HandshakeStream>, Status> {
        Err(Status::unimplemented("handshake"))
    }

    async fn list_flights(
        &self,
        _: Request<Criteria>,
    ) -> Result<Response<Self::ListFlightsStream>, Status> {
        Err(Status::unimplemented("list_flights"))
    }

    async fn get_flight_info(
        &self,
        _: Request<FlightDescriptor>,
    ) -> Result<Response<FlightInfo>, Status> {
        Err(Status::unimplemented("get_flight_info"))
    }

    async fn poll_flight_info(
        &self,
        _: Request<FlightDescriptor>,
    ) -> Result<Response<PollInfo>, Status> {
        Err(Status::unimplemented("poll_flight_info"))
    }

    async fn get_schema(
        &self,
        _: Request<FlightDescriptor>,
    ) -> Result<Response<SchemaResult>, Status> {
        Err(Status::unimplemented("get_schema"))
    }

    async fn do_get(
        &self,
        request: Request<Ticket>,
    ) -> Result<Response<Self::DoGetStream>, Status> {
        Ok(Response::new(self.send(request.into_inner()).await?))
    }

    async fn do_exchange(
        &self,
        _: Request<Streaming<FlightData>>,
    ) -> Result<Response<Self::DoExchangeStream>, Status> {
        Err(Status::unimplemented("do_exchange"))
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    std::fs::create_dir_all(&args.out_dir)?;

    let service = FlightServiceServer::new(VideoFlightService {
        out_dir: args.out_dir.clone(),
        format: args.format,
        get_compression: args.get_compression,
    })
    .max_decoding_message_size(args.max_decode_message_size)
    .max_encoding_message_size(MAX_ENCODE_MESSAGE_SIZE);

    eprintln!(
        "listening on {} (storing {} Parquet files to {})",
        args.addr,
        args.format.name(),
        args.out_dir.display()
    );
    Server::builder()
        .add_service(service)
        .serve_with_shutdown(args.addr, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
