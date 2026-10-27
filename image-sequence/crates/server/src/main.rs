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
//! The video holds every image, one frame each: nothing is dropped or
//! repeated. The server decides its frame rate once the last image is in
//! (see `common::timing`): it encodes while the images arrive, each frame
//! at its capture time (a variable frame rate), and if the capture times
//! all fit a constant frame rate it then rewrites the timestamps to it,
//! without re-encoding (see [`encoder::make_constant`]).
//!
//! mosaicod would store the images as Parquet files; here each `DoPut` is
//! encoded into a video while it is received (see [`encoder`]). Sequences,
//! sessions and topics are only kept in memory, so they are forgotten when
//! the server stops. An existing video is overwritten.
//!
//! With `--extract-images <video>` the server does not listen: it turns a
//! video it wrote back into images, one per frame, in every format of the
//! `formats` crate (the exact YUV 4:2:0 ones, and PNG and JPEG for
//! comparison), and compares their sizes and times (see [`images`]). With `--image-quality <video>` it then measures
//! with butteraugli the images of the formats that are not exact. With
//! `--store-images <video>` it stores each format's images as an image topic
//! (a Parquet file), which, listening, it serves back to clients as mosaicod
//! serves a topic: `GetFlightInfo`, then `DoGet` (see [`topics`]).

mod encoder;
mod images;
mod topics;

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
    timing::{FrameTiming, frame_timing},
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
    /// Instead of listening, write every frame of this video as an image in
    /// each format (the exact YUV 4:2:0 ones, PNG and JPEG), to
    /// `<video dir>/images/<format>/`, and their sizes and times to
    /// `<topic>.images.json` next to it.
    #[arg(long, value_name = "VIDEO")]
    extract_images: Option<PathBuf>,
    /// Instead of listening, measure with butteraugli how far the images of
    /// the formats that are not exact (written by --extract-images) are from
    /// the frames of this video, into `<topic>.images-quality.json`.
    #[arg(long, value_name = "VIDEO", conflicts_with = "extract_images")]
    image_quality: Option<PathBuf>,
    /// Instead of listening, store each format's images of this video
    /// (written by --extract-images) as an image topic, one Parquet file per
    /// format in `<video dir>/images/`, laid out as mosaicod stores one.
    #[arg(long, value_name = "VIDEO", conflicts_with_all = ["extract_images", "image_quality"])]
    store_images: Option<PathBuf>,
    /// IPC compression of the `DoGet` streams that serve image topics back:
    /// LZ4, as mosaicod, or none.
    #[arg(long, value_enum, default_value_t = DoGetCompression::Lz4)]
    do_get_compression: DoGetCompression,
}

#[derive(Clone, Copy, clap::ValueEnum)]
enum DoGetCompression {
    Lz4,
    None,
}

struct Session {
    sequence: String,
    finalized: bool,
}

struct Topic {
    /// Set when a `DoPut` starts writing it: a topic gets one upload.
    written: bool,
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
    /// The image topics served back (`GetFlightInfo`, `DoGet`).
    topics: topics::ImageTopics,
}

fn internal(error: impl Display) -> Status {
    Status::internal(format!("{error:#}"))
}

fn decode<M: Message + Default>(body: &[u8]) -> Result<M, Status> {
    M::decode(body).map_err(|e| Status::invalid_argument(format!("bad request body: {e}")))
}

/// Checks the capture time of image `index` (`None` if it has none), and
/// returns it: every frame is shown at its capture time, in order, one at a
/// time, so an image needs a time after the previous image's (`previous_ns`,
/// `None` for the first image). Two images at the same time (a resent or
/// coarse timestamp) or a time going back (swapped times, a clock reset)
/// are rejected.
fn check_capture_time(index: u64, timestamp_ns: Option<i64>, previous_ns: Option<i64>) -> Result<i64, Status> {
    let timestamp_ns =
        timestamp_ns.ok_or_else(|| Status::invalid_argument(format!("image {index} has no timestamp")))?;
    match previous_ns {
        Some(previous) if timestamp_ns <= previous => Err(Status::invalid_argument(format!(
            "image {index} has timestamp {timestamp_ns}, not after the previous one ({previous})"
        ))),
        _ => Ok(timestamp_ns),
    }
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
                if !common::protocol::valid_sequence_name(&request.locator) {
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
                        written: false,
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

    /// Checks that `cmd` names a topic of an open session, with its uuid,
    /// that no `DoPut` wrote or is writing it, and claims it.
    fn claim_topic(&self, cmd: &DoPutCmd) -> Result<(), Status> {
        let state = &mut *self.state.lock().unwrap();
        let topic = state
            .topics
            .get_mut(&cmd.locator)
            .filter(|t| t.uuid == cmd.topic_uuid)
            .ok_or_else(|| Status::not_found(format!("no topic {} with this uuid", cmd.locator)))?;
        match state.sessions.get(&topic.session_uuid) {
            Some(session) if !session.finalized => {}
            _ => return Err(Status::failed_precondition("the topic's session is finalized")),
        }
        if topic.written {
            return Err(Status::already_exists(format!("topic {} already has its data", cmd.locator)));
        }
        topic.written = true;
        Ok(())
    }

    /// Gives a topic back after a failed upload, so that it can be retried.
    fn release_topic(&self, locator: &str) {
        if let Some(topic) = self.state.lock().unwrap().topics.get_mut(locator) {
            topic.written = false;
        }
    }

    /// Receives the images of a `DoPut` after its first message and queues
    /// them for the encoder, checking each one: a sequence with an image
    /// that has no timestamp, or one not after the previous image's, is
    /// rejected.
    async fn receive_images(
        decoder: &mut FlightDataDecoder,
        writer: &VideoWriter,
        report: &mut UploadReport,
        captures: &mut Vec<i64>,
    ) -> Result<(), Status> {
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
                let previous_ns = (report.images > 0).then_some(report.last_timestamp_ns);
                let timestamp = timestamps.is_valid(row).then(|| timestamps.value(row));
                let timestamp_ns = check_capture_time(report.images, timestamp, previous_ns)?;
                if data.is_null(row) {
                    return Err(Status::invalid_argument(format!("image {} has no data", report.images)));
                }
                if formats.is_null(row) || formats.value(row) != "png" {
                    return Err(Status::invalid_argument(format!(
                        "unsupported format {:?}",
                        formats.is_valid(row).then(|| formats.value(row))
                    )));
                }
                if report.images == 0 {
                    report.first_timestamp_ns = timestamp_ns;
                }
                report.last_timestamp_ns = timestamp_ns;
                captures.push(timestamp_ns);
                report.images += 1;
                report.png_bytes += data.value(row).len() as u64;
                let image = Image {
                    timestamp_ns,
                    png: data.value(row).to_vec(),
                    received: Instant::now(),
                };
                if writer.send(image).await.is_err() {
                    return Ok(()); // the encoder failed: finish() returns why
                }
            }
        }
        Ok(())
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
        self.claim_topic(&cmd)?;
        let uploaded = self.upload(cmd.locator.clone(), decoder, started).await;
        if uploaded.is_err() {
            self.release_topic(&cmd.locator);
        }
        uploaded
    }

    /// The upload of a claimed topic, after its first message.
    async fn upload(
        &self,
        locator: String,
        mut decoder: FlightDataDecoder,
        started: Instant,
    ) -> Result<UploadReport, Status> {

        let path = common::video_path(&self.out_dir, &locator, self.settings.preset, self.settings.crf);
        if let Some(dir) = path.parent() {
            tokio::fs::create_dir_all(dir).await.map_err(internal)?;
        }
        let writer = VideoWriter::spawn(path.clone(), self.settings, started);
        let mut report = UploadReport {
            locator: locator.clone(),
            pixel_format: self.settings.pixel_format,
            preset: self.settings.preset,
            crf: self.settings.crf,
            images: 0,
            png_bytes: 0,
            width: 0,
            height: 0,
            first_timestamp_ns: 0,
            last_timestamp_ns: 0,
            timing: FrameTiming::Variable,
            video_path: path.display().to_string(),
            video_bytes: 0,
            seconds: 0.0,
            receive_seconds: 0.0,
            decode_seconds: 0.0,
            encode_seconds: 0.0,
            encode_wait_seconds: 0.0,
            frames: Vec::new(),
        };

        // Every capture time, to choose the frame rate once all are in.
        let mut captures = Vec::new();
        let received = Self::receive_images(&mut decoder, &writer, &mut report, &mut captures).await;
        report.receive_seconds = started.elapsed().as_secs_f64();

        // Encoded at a variable frame rate; made constant if no timing is lost.
        report.timing = frame_timing(&captures);
        let constant = match (&received, report.timing) {
            (Ok(()), FrameTiming::Constant { frame_rate }) => Some(frame_rate),
            _ => None,
        };
        let video = path.clone();
        // Always waits for the encoder, even after a rejection, so that its
        // threads are done with the file before it is removed below.
        let encoded = tokio::task::spawn_blocking(move || {
            let mut encoded = writer.finish()?;
            if let Some(frame_rate) = constant {
                encoded.video_bytes = encoder::make_constant(&video, frame_rate, encoded.frames)?;
            }
            anyhow::Ok(encoded)
        })
        .await
        // A panic is a failure like any other: the cleanup below runs.
        .unwrap_or_else(|error| Err(anyhow::anyhow!("the encoder panicked: {error}")));
        // A rejected or failed upload leaves no video: not one of part of
        // the sequence.
        let encoded = match (received, encoded) {
            (Ok(()), Ok(encoded)) if encoded.frames == report.images => encoded,
            (received, encoded) => {
                let _ = tokio::fs::remove_file(&path).await;
                // Nor the capture times of an earlier upload of the topic.
                let _ = tokio::fs::remove_file(common::timestamps_path(&path)).await;
                return Err(match (received, encoded) {
                    (Err(status), _) => status,
                    (Ok(()), Err(error)) => internal(error),
                    (Ok(()), Ok(encoded)) => Status::internal(format!(
                        "received {} images but encoded {} frames",
                        report.images, encoded.frames
                    )),
                });
            }
        };
        // The capture times as the client sent them, exact: the video keeps
        // them only to the millisecond. The image topics are made with them.
        let timestamps = common::timestamps_path(&path);
        let written = tokio::task::spawn_blocking(move || topics::write_timestamps(&timestamps, &captures))
            .await
            .unwrap_or_else(|error| Err(anyhow::anyhow!("writing the capture times panicked: {error}")));
        if let Err(error) = written {
            let _ = tokio::fs::remove_file(&path).await;
            return Err(internal(error));
        }
        report.width = encoded.width;
        report.height = encoded.height;
        report.video_bytes = encoded.video_bytes;
        report.seconds = started.elapsed().as_secs_f64();
        report.decode_seconds = encoded.decode_seconds;
        report.encode_seconds = encoded.encode_seconds;
        report.encode_wait_seconds = encoded.encode_wait_seconds;
        report.frames = encoded.timeline;
        tokio::fs::write(
            common::upload_report_path(&path),
            serde_json::to_vec_pretty(&report).map_err(internal)?,
        )
        .await
        .map_err(internal)?;
        eprintln!(
            "{}: {} images ({} PNG bytes, {}x{}) -> {} ({} bytes, {}) in {:.1} s \
             (encoding {:.1} s, of which {:.1} s waiting for frames; PNG decoding {:.1} s)",
            report.locator,
            report.images,
            report.png_bytes,
            report.width,
            report.height,
            report.video_path,
            report.video_bytes,
            report.timing,
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

    async fn get_flight_info(&self, request: Request<FlightDescriptor>) -> Result<Response<FlightInfo>, Status> {
        Ok(Response::new(self.topics.flight_info(request.into_inner())?))
    }

    async fn poll_flight_info(&self, _: Request<FlightDescriptor>) -> Result<Response<PollInfo>, Status> {
        Err(Status::unimplemented("poll_flight_info"))
    }

    async fn get_schema(&self, _: Request<FlightDescriptor>) -> Result<Response<SchemaResult>, Status> {
        Err(Status::unimplemented("get_schema"))
    }

    async fn do_get(&self, request: Request<Ticket>) -> Result<Response<Self::DoGetStream>, Status> {
        Ok(Response::new(self.topics.do_get(request.into_inner())?))
    }

    async fn do_exchange(&self, _: Request<Streaming<FlightData>>) -> Result<Response<Self::DoExchangeStream>, Status> {
        Err(Status::unimplemented("do_exchange"))
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    formats::ffmpeg::init()?;
    if let Some(video) = args.extract_images {
        let report = images::extract(&video)?;
        for format in &report.formats {
            eprintln!(
                "{:>4}: {} bytes, encoding {:.2} s, decoding {:.2} s ({})",
                format.format, format.bytes, format.encode_seconds, format.decode_seconds, format.dir
            );
        }
        return Ok(());
    }
    if let Some(video) = args.store_images {
        for (format, bytes, seconds) in topics::store(&video)? {
            eprintln!("{format:>4}: stored as an image topic, {bytes} bytes, in {seconds:.1} s");
        }
        return Ok(());
    }
    if let Some(video) = args.image_quality {
        for format in images::quality::measure(&video)?.formats {
            let n = format.images.len().max(1) as f64;
            eprintln!(
                "{:>4}: butteraugli max-norm {:.4} (mean), 3-norm {:.4} (mean), in {:.0} s",
                format.format,
                format.images.iter().map(|i| i.butteraugli_max).sum::<f64>() / n,
                format.images.iter().map(|i| i.butteraugli_3norm).sum::<f64>() / n,
                format.seconds
            );
        }
        return Ok(());
    }
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
        topics: topics::ImageTopics {
            out_dir: out_dir.clone(),
            preset: settings.preset,
            crf: settings.crf,
            compression: match args.do_get_compression {
                DoGetCompression::Lz4 => Some(arrow_ipc::CompressionType::LZ4_FRAME),
                DoGetCompression::None => None,
            },
        },
    })
    .max_decoding_message_size(MAX_DECODE_MESSAGE_SIZE)
    // As mosaicod: whatever the decode limit, a topic stays readable.
    .max_encoding_message_size(common::protocol::MAX_ENCODE_MESSAGE_SIZE);

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

#[cfg(test)]
mod tests {
    use super::*;

    /// The capture times of `datasets/<batch>/timestamps.csv`. Only the
    /// times: the images need not be on disk.
    fn capture_times(batch: &str) -> Vec<i64> {
        let csv = common::workspace_dir().join("datasets").join(batch).join("timestamps.csv");
        common::images::read_capture_times(&csv).unwrap().into_iter().map(|(_, t)| t).collect()
    }

    /// The images of `times` the server rejects, each checked against the
    /// image before it.
    fn rejected(times: &[i64]) -> Vec<u64> {
        (0..times.len())
            .filter(|&i| check_capture_time(i as u64, Some(times[i]), i.checked_sub(1).map(|p| times[p])).is_err())
            .map(|i| i as u64)
            .collect()
    }

    /// Where an upload of `times` stops: the first image rejected, if any.
    fn upload_stops_at(times: &[i64]) -> Option<u64> {
        let mut previous = None;
        for (i, &t) in times.iter().enumerate() {
            match check_capture_time(i as u64, Some(t), previous) {
                Ok(t) => previous = Some(t),
                Err(status) => {
                    assert_eq!(status.code(), tonic::Code::InvalidArgument);
                    assert!(status.message().contains(&format!("image {i} has timestamp {t}, not after")));
                    return Some(i as u64);
                }
            }
        }
        None
    }

    #[test]
    fn accepts_the_valid_scenarios() {
        for batch in ["regular/batch_300", "frame-drops/batch_283", "jitter/batch_300", "rate-change/batch_210",
                      "sparse/batch_020"] {
            assert_eq!(upload_stops_at(&capture_times(batch)), None, "{batch}");
        }
    }

    #[test]
    fn rejects_duplicate_timestamps() {
        // Frames 75, 150, 151 and 225 repeat the time of the frame before.
        assert_eq!(rejected(&capture_times("duplicate-timestamps/batch_300")), [75, 150, 151, 225]);
        // The upload stops at the first one; the batch before it is valid.
        assert_eq!(upload_stops_at(&capture_times("duplicate-timestamps/batch_300")), Some(75));
        assert_eq!(upload_stops_at(&capture_times("duplicate-timestamps/batch_076")), Some(75));
        assert_eq!(upload_stops_at(&capture_times("duplicate-timestamps/batch_075")), None);
    }

    #[test]
    fn rejects_times_going_back() {
        // Frame 91 swaps its time with frame 90's; from frame 180 the clock
        // is 500 ms behind.
        assert_eq!(rejected(&capture_times("out-of-order/batch_300")), [91, 180]);
        assert_eq!(upload_stops_at(&capture_times("out-of-order/batch_300")), Some(91));
        assert_eq!(upload_stops_at(&capture_times("out-of-order/batch_092")), Some(91));
        assert_eq!(upload_stops_at(&capture_times("out-of-order/batch_091")), None);
    }

    #[test]
    fn rejects_an_image_without_a_timestamp() {
        let status = check_capture_time(3, None, Some(100)).unwrap_err();
        assert_eq!(status.code(), tonic::Code::InvalidArgument);
        assert_eq!(status.message(), "image 3 has no timestamp");
        assert!(check_capture_time(0, None, None).is_err());
    }

    #[test]
    fn accepts_the_first_image_at_any_time() {
        assert_eq!(check_capture_time(0, Some(-5), None).unwrap(), -5);
        assert_eq!(check_capture_time(1, Some(101), Some(100)).unwrap(), 101);
    }
}
