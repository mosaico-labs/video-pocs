//! Arrow Flight server that receives image topics with the mosaicod upload
//! flow (see `common::protocol`) and turns each one into an AV1 video:
//!
//! ```text
//! <out-dir>/<scenario>/<batch>/preset_<P>_crf_<C>/<topic>.mkv           the video
//! <out-dir>/<scenario>/<batch>/preset_<P>_crf_<C>/<topic>.upload.json   the upload report
//! ```
//!
//! The scenario and batch come from the sequence name (`regular_batch_060`
//! is written to `regular/batch_060/`, see `common::sequence_dir`), and the
//! last folder from the encoder settings, so encodings of the same batch
//! with different presets and CRFs don't overwrite each other. With
//! `--lossless` the last folder is `preset_<P>_lossless`.
//!
//! mosaicod would store the images as Parquet files; here each `DoPut` is
//! encoded into a video while it is received (see [`encoder`]). Sequences,
//! sessions and topics are only kept in memory, so they are forgotten when
//! the server stops. An existing video is overwritten.

mod encoder;

use std::{
    collections::{HashMap, HashSet},
    fmt::Display,
    net::SocketAddr,
    path::PathBuf,
    sync::Mutex,
    time::Instant,
};

use arrow_array::{Array, BinaryArray, Int64Array, RecordBatch, StringArray};
use arrow_flight::{
    Action, ActionType, Criteria, Empty, FlightData, FlightDescriptor, FlightInfo, HandshakeRequest, HandshakeResponse,
    PollInfo, PutResult, SchemaResult, Ticket,
    decode::{DecodedPayload, FlightDataDecoder},
    error::FlightError,
    flight_descriptor::DescriptorType,
    flight_service_server::{FlightService, FlightServiceServer},
};
use arrow_schema::{DataType, SchemaRef};
use clap::Parser;
use common::{
    DEFAULT_CRF, DEFAULT_PRESET, PIXEL_FORMAT,
    protocol::{
        DoPutCmd, FORMAT_IMAGE, MAX_DECODE_MESSAGE_SIZE, ResourceLocator, ResourceUuid, SEQUENCE_CREATE,
        SESSION_CREATE, SESSION_FINALIZE, SequenceCreate, SessionCreateResponse, SessionUuid, TIMESTAMP_COLUMN,
        TOPIC_CREATE, TopicCreate, UploadReport, parse_topic_locator,
    },
};
use encoder::{Image, Settings, VideoWriter};
use ffmpeg_the_third::Rational;
use futures::{StreamExt, TryStreamExt, stream, stream::BoxStream};
use prost::Message;
use anyhow::Context as _;
use tonic::{
    Request, Response, Status, Streaming,
    transport::{Server, server::TcpIncoming},
};

#[derive(Parser)]
#[command(about = "Arrow Flight server that encodes uploaded image topics into AV1 videos")]
struct Args {
    /// Address to listen on (mosaicod's default port).
    #[arg(long, default_value = "127.0.0.1:6726")]
    addr: SocketAddr,
    /// Directory where the videos are written [default: image-sequence/output].
    #[arg(long)]
    out_dir: Option<PathBuf>,
    /// SVT-AV1 preset: 0 (slowest, best) to 13 (fastest). For 4K and larger
    /// SVT-AV1 uses at most 9.
    #[arg(long, default_value_t = DEFAULT_PRESET, value_parser = clap::value_parser!(u8).range(0..=13))]
    preset: u8,
    /// SVT-AV1 constant rate factor: 1 (best) to 63 (smallest).
    #[arg(long, default_value_t = DEFAULT_CRF, value_parser = clap::value_parser!(u8).range(1..=63))]
    crf: u8,
    /// Encode losslessly instead of with a CRF: the only loss left is the
    /// conversion to YUV 4:2:0. The video goes to `preset_<P>_lossless/`.
    #[arg(long, conflicts_with = "crf")]
    lossless: bool,
    /// Nominal frame rate for SVT-AV1's rate control. The frames keep their
    /// capture times as timestamps.
    #[arg(long, default_value = "30000/1001", value_parser = encoder::parse_rate)]
    frame_rate: Rational,
}

struct Session {
    sequence: String,
    finalized: bool,
}

struct Topic {
    uuid: String,
    session_uuid: String,
}

/// What mosaicod would keep in its database.
#[derive(Default)]
struct State {
    sequences: HashSet<String>,
    sessions: HashMap<String, Session>,
    topics: HashMap<String, Topic>,
}

struct ImageFlightService {
    out_dir: PathBuf,
    settings: Settings,
    state: Mutex<State>,
}

fn internal(error: impl Display) -> Status {
    Status::internal(format!("{error:#}"))
}

fn decode<M: Message + Default>(body: &[u8]) -> Result<M, Status> {
    M::decode(body).map_err(|e| Status::invalid_argument(format!("bad request body: {e}")))
}

/// Checks the columns the server reads.
fn check_schema(schema: &SchemaRef) -> Result<(), Status> {
    for (name, data_type) in [
        (TIMESTAMP_COLUMN, DataType::Int64),
        ("format", DataType::Utf8),
        ("data", DataType::Binary),
    ] {
        let field = schema
            .field_with_name(name)
            .map_err(|_| Status::invalid_argument(format!("missing column {name}")))?;
        if *field.data_type() != data_type {
            return Err(Status::invalid_argument(format!("{name} must be {data_type}")));
        }
    }
    Ok(())
}

fn column<'a, T: 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T, Status> {
    batch
        .column_by_name(name)
        .and_then(|c| c.as_any().downcast_ref::<T>())
        .ok_or_else(|| Status::invalid_argument(format!("bad column {name}")))
}

impl ImageFlightService {
    fn action(&self, action: Action) -> Result<Option<Vec<u8>>, Status> {
        let state = &mut *self.state.lock().unwrap();
        match action.r#type.as_str() {
            SEQUENCE_CREATE => {
                let request: SequenceCreate = decode(&action.body)?;
                if request.locator.is_empty() || request.locator.contains('/') {
                    return Err(Status::invalid_argument(format!(
                        "bad sequence name {:?}",
                        request.locator
                    )));
                }
                if !state.sequences.insert(request.locator.clone()) {
                    return Err(Status::already_exists(format!(
                        "sequence {} already exists",
                        request.locator
                    )));
                }
                Ok(None)
            }
            SESSION_CREATE => {
                let request: ResourceLocator = decode(&action.body)?;
                if !state.sequences.contains(&request.locator) {
                    return Err(Status::not_found(format!("no sequence {}", request.locator)));
                }
                let uuid = uuid::Uuid::new_v4().to_string();
                state.sessions.insert(
                    uuid.clone(),
                    Session {
                        sequence: request.locator.clone(),
                        finalized: false,
                    },
                );
                Ok(Some(
                    SessionCreateResponse {
                        uuid,
                        locator: request.locator,
                    }
                    .encode_to_vec(),
                ))
            }
            TOPIC_CREATE => {
                let request: TopicCreate = decode(&action.body)?;
                let session = state
                    .sessions
                    .get(&request.session_uuid)
                    .filter(|s| !s.finalized)
                    .ok_or_else(|| Status::failed_precondition("no open session with this uuid"))?;
                match parse_topic_locator(&request.locator) {
                    Some((sequence, _)) if sequence == session.sequence => {}
                    _ => return Err(Status::invalid_argument(format!("bad topic {:?}", request.locator))),
                }
                if request.serialization_format != FORMAT_IMAGE {
                    return Err(Status::invalid_argument("this server only accepts IMAGE topics"));
                }
                if state.topics.contains_key(&request.locator) {
                    return Err(Status::already_exists(format!(
                        "topic {} already exists",
                        request.locator
                    )));
                }
                let uuid = uuid::Uuid::new_v4().to_string();
                state.topics.insert(
                    request.locator,
                    Topic {
                        uuid: uuid.clone(),
                        session_uuid: request.session_uuid,
                    },
                );
                Ok(Some(ResourceUuid { uuid }.encode_to_vec()))
            }
            SESSION_FINALIZE => {
                let request: SessionUuid = decode(&action.body)?;
                let session = state
                    .sessions
                    .get_mut(&request.session_uuid)
                    .ok_or_else(|| Status::not_found("no session with this uuid"))?;
                session.finalized = true;
                Ok(None)
            }
            other => Err(Status::unimplemented(format!("action {other}"))),
        }
    }

    /// Checks that `cmd` names a topic of an open session, with its uuid.
    fn check_topic(&self, cmd: &DoPutCmd) -> Result<(), Status> {
        let state = self.state.lock().unwrap();
        let topic = state
            .topics
            .get(&cmd.locator)
            .filter(|t| t.uuid == cmd.topic_uuid)
            .ok_or_else(|| Status::not_found(format!("no topic {} with this uuid", cmd.locator)))?;
        match state.sessions.get(&topic.session_uuid) {
            Some(session) if !session.finalized => Ok(()),
            _ => Err(Status::failed_precondition("the topic's session is finalized")),
        }
    }

    async fn receive(&self, input: Streaming<FlightData>) -> Result<UploadReport, Status> {
        let mut decoder = FlightDataDecoder::new(input.map_err(FlightError::from));
        let first = decoder
            .next()
            .await
            .ok_or_else(|| Status::invalid_argument("empty DoPut stream"))?
            .map_err(Status::from)?;
        let started = Instant::now();

        // The first message holds the DoPutCmd and the schema, as in mosaicod.
        let descriptor = first
            .inner
            .flight_descriptor
            .as_ref()
            .filter(|d| d.r#type() == DescriptorType::Cmd)
            .ok_or_else(|| Status::invalid_argument("DoPut needs a CMD descriptor holding a DoPutCmd"))?;
        let cmd: DoPutCmd = decode(&descriptor.cmd)?;
        let DecodedPayload::Schema(schema) = first.payload else {
            return Err(Status::invalid_argument("missing schema in the first message"));
        };
        check_schema(&schema)?;
        self.check_topic(&cmd)?;

        let path = common::video_path(&self.out_dir, &cmd.locator, self.settings.preset, self.settings.crf);
        if let Some(dir) = path.parent() {
            tokio::fs::create_dir_all(dir).await.map_err(internal)?;
        }
        let writer = VideoWriter::spawn(path.clone(), self.settings, started);
        let mut report = UploadReport {
            locator: cmd.locator.clone(),
            pixel_format: self.settings.pixel_format,
            preset: self.settings.preset,
            crf: self.settings.crf,
            images: 0,
            png_bytes: 0,
            width: 0,
            height: 0,
            first_timestamp_ns: 0,
            last_timestamp_ns: 0,
            video_path: path.display().to_string(),
            video_bytes: 0,
            seconds: 0.0,
            receive_seconds: 0.0,
            decode_seconds: 0.0,
            encode_seconds: 0.0,
            encode_wait_seconds: 0.0,
            frames: Vec::new(),
        };

        while let Some(decoded) = decoder.next().await {
            let DecodedPayload::RecordBatch(batch) = decoded.map_err(Status::from)?.payload else {
                return Err(Status::invalid_argument(
                    "only record batches may follow the first message",
                ));
            };
            let timestamps = column::<Int64Array>(&batch, TIMESTAMP_COLUMN)?;
            let formats = column::<StringArray>(&batch, "format")?;
            let data = column::<BinaryArray>(&batch, "data")?;
            for row in 0..batch.num_rows() {
                let timestamp_ns = timestamps.value(row);
                if formats.value(row) != "png" {
                    return Err(Status::invalid_argument(format!(
                        "unsupported format {:?}",
                        formats.value(row)
                    )));
                }
                // A video shows its frames in timestamp order, one at a time.
                if report.images > 0 && timestamp_ns <= report.last_timestamp_ns {
                    return Err(Status::invalid_argument(format!(
                        "image {} has timestamp {timestamp_ns}, not after the previous one ({})",
                        report.images, report.last_timestamp_ns
                    )));
                }
                if report.images == 0 {
                    report.first_timestamp_ns = timestamp_ns;
                }
                report.last_timestamp_ns = timestamp_ns;
                report.images += 1;
                report.png_bytes += data.value(row).len() as u64;
                let image = Image {
                    timestamp_ns,
                    png: data.value(row).to_vec(),
                    received: Instant::now(),
                };
                if writer.send(image).await.is_err() {
                    break; // the encoder failed: finish() below returns why
                }
            }
        }

        report.receive_seconds = started.elapsed().as_secs_f64();

        let encoded = tokio::task::spawn_blocking(move || writer.finish())
            .await
            .map_err(internal)?
            .map_err(internal)?;
        report.width = encoded.width;
        report.height = encoded.height;
        report.video_bytes = encoded.video_bytes;
        report.seconds = started.elapsed().as_secs_f64();
        report.decode_seconds = encoded.decode_seconds;
        report.encode_seconds = encoded.encode_seconds;
        report.encode_wait_seconds = encoded.encode_wait_seconds;
        report.frames = encoded.timeline;
        if encoded.frames != report.images {
            return Err(Status::internal(format!(
                "received {} images but encoded {} frames",
                report.images, encoded.frames
            )));
        }
        tokio::fs::write(
            common::upload_report_path(&path),
            serde_json::to_vec_pretty(&report).map_err(internal)?,
        )
        .await
        .map_err(internal)?;
        eprintln!(
            "{}: {} images ({} PNG bytes, {}x{}) -> {} ({} bytes) in {:.1} s \
             (encoding {:.1} s, of which {:.1} s waiting for frames; PNG decoding {:.1} s)",
            report.locator,
            report.images,
            report.png_bytes,
            report.width,
            report.height,
            report.video_path,
            report.video_bytes,
            report.seconds,
            report.encode_seconds,
            report.encode_wait_seconds,
            report.decode_seconds,
        );
        Ok(report)
    }
}

#[tonic::async_trait]
impl FlightService for ImageFlightService {
    type HandshakeStream = BoxStream<'static, Result<HandshakeResponse, Status>>;
    type ListFlightsStream = BoxStream<'static, Result<FlightInfo, Status>>;
    type DoGetStream = BoxStream<'static, Result<FlightData, Status>>;
    type DoPutStream = BoxStream<'static, Result<PutResult, Status>>;
    type DoExchangeStream = BoxStream<'static, Result<FlightData, Status>>;
    type DoActionStream = BoxStream<'static, Result<arrow_flight::Result, Status>>;
    type ListActionsStream = BoxStream<'static, Result<ActionType, Status>>;

    async fn do_put(&self, request: Request<Streaming<FlightData>>) -> Result<Response<Self::DoPutStream>, Status> {
        let report = self.receive(request.into_inner()).await?;
        let result = PutResult {
            app_metadata: serde_json::to_vec(&report).map_err(internal)?.into(),
        };
        Ok(Response::new(stream::once(async { Ok(result) }).boxed()))
    }

    async fn do_action(&self, request: Request<Action>) -> Result<Response<Self::DoActionStream>, Status> {
        let action = request.into_inner();
        eprintln!("{}", action.r#type);
        let results: Vec<_> = self
            .action(action)?
            .map(|body| Ok(arrow_flight::Result { body: body.into() }))
            .into_iter()
            .collect();
        Ok(Response::new(stream::iter(results).boxed()))
    }

    async fn list_actions(&self, _: Request<Empty>) -> Result<Response<Self::ListActionsStream>, Status> {
        Err(Status::unimplemented("list_actions"))
    }

    async fn handshake(
        &self,
        _: Request<Streaming<HandshakeRequest>>,
    ) -> Result<Response<Self::HandshakeStream>, Status> {
        Err(Status::unimplemented("handshake"))
    }

    async fn list_flights(&self, _: Request<Criteria>) -> Result<Response<Self::ListFlightsStream>, Status> {
        Err(Status::unimplemented("list_flights"))
    }

    async fn get_flight_info(&self, _: Request<FlightDescriptor>) -> Result<Response<FlightInfo>, Status> {
        Err(Status::unimplemented("get_flight_info"))
    }

    async fn poll_flight_info(&self, _: Request<FlightDescriptor>) -> Result<Response<PollInfo>, Status> {
        Err(Status::unimplemented("poll_flight_info"))
    }

    async fn get_schema(&self, _: Request<FlightDescriptor>) -> Result<Response<SchemaResult>, Status> {
        Err(Status::unimplemented("get_schema"))
    }

    async fn do_get(&self, _: Request<Ticket>) -> Result<Response<Self::DoGetStream>, Status> {
        Err(Status::unimplemented("do_get"))
    }

    async fn do_exchange(&self, _: Request<Streaming<FlightData>>) -> Result<Response<Self::DoExchangeStream>, Status> {
        Err(Status::unimplemented("do_exchange"))
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    encoder::init_ffmpeg()?;
    let out_dir = args.out_dir.unwrap_or_else(common::default_output_dir);
    std::fs::create_dir_all(&out_dir)?;

    let settings = Settings {
        preset: args.preset,
        crf: (!args.lossless).then_some(args.crf),
        frame_rate: args.frame_rate,
        pixel_format: PIXEL_FORMAT,
    };
    let service = FlightServiceServer::new(ImageFlightService {
        out_dir: out_dir.clone(),
        settings,
        state: Mutex::default(),
    })
    .max_decoding_message_size(MAX_DECODE_MESSAGE_SIZE);

    // Bind first, so "listening" is only printed once the port is ours: a
    // taken port fails here instead of after the message.
    let listener = tokio::net::TcpListener::bind(args.addr)
        .await
        .with_context(|| format!("binding {}", args.addr))?;
    let quality = match settings.crf {
        Some(crf) => format!("crf {crf}"),
        None => "lossless".to_string(),
    };
    eprintln!(
        "listening on {} (AV1 {} with SVT-AV1 preset {} {quality}, videos in {})",
        listener.local_addr()?,
        PIXEL_FORMAT,
        args.preset,
        out_dir.display()
    );
    Server::builder()
        .add_service(service)
        .serve_with_incoming(TcpIncoming::from(listener))
        .await?;
    Ok(())
}
