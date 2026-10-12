//! Arrow Flight server: receives one `DoPut` per video and stores the chunks
//! as an Arrow IPC file named after the Flight descriptor.

use std::{fmt::Display, fs::File, io::BufWriter, net::SocketAddr, path::PathBuf, time::Instant};

use arrow_array::{Array, BinaryArray, RecordBatch, UInt32Array};
use arrow_flight::{
    Action, ActionType, Criteria, Empty, FlightData, FlightDescriptor, FlightInfo,
    HandshakeRequest, HandshakeResponse, PollInfo, PutResult, SchemaResult, Ticket,
    decode::{DecodedPayload, FlightDataDecoder},
    error::FlightError,
    flight_service_server::{FlightService, FlightServiceServer},
};
use arrow_ipc::writer::FileWriter;
use clap::Parser;
use futures::{
    StreamExt, TryStreamExt,
    stream::{self, BoxStream},
};
use poc1::{MAX_MESSAGE_SIZE, PutAck, wire_size};
use tonic::{Request, Response, Status, Streaming, transport::Server};

#[derive(Parser)]
#[command(about = "Arrow Flight server that stores uploaded video chunks")]
struct Args {
    /// Address to listen on.
    #[arg(long, default_value = "127.0.0.1:50051")]
    addr: SocketAddr,
    /// Directory where the received Arrow IPC files are written.
    #[arg(long, default_value = "received")]
    out_dir: PathBuf,
}

struct VideoFlightService {
    out_dir: PathBuf,
}

fn internal(error: impl Display) -> Status {
    Status::internal(error.to_string())
}

/// Rejects names that could escape the output directory.
fn validate_name(name: &str) -> Result<(), Status> {
    if name.is_empty() || name.starts_with('.') || name.contains(['/', '\\']) {
        return Err(Status::invalid_argument(format!(
            "invalid video name: {name:?}"
        )));
    }
    Ok(())
}

/// Checks that chunks arrive in order and adds them to the running CRC.
fn consume_batch(
    batch: &RecordBatch,
    ack: &mut PutAck,
    hasher: &mut crc32fast::Hasher,
) -> Result<(), Status> {
    let column = |name: &str| {
        batch
            .column_by_name(name)
            .ok_or_else(|| Status::invalid_argument(format!("missing column {name}")))
    };
    let indexes = column("chunk_index")?
        .as_any()
        .downcast_ref::<UInt32Array>()
        .ok_or_else(|| Status::invalid_argument("chunk_index must be UInt32"))?;
    let chunks = column("data")?
        .as_any()
        .downcast_ref::<BinaryArray>()
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
    async fn receive(&self, mut input: Streaming<FlightData>) -> Result<PutAck, Status> {
        let first = input
            .message()
            .await?
            .ok_or_else(|| Status::invalid_argument("empty DoPut stream"))?;
        let started = Instant::now();
        let name = first
            .flight_descriptor
            .as_ref()
            .and_then(|descriptor| descriptor.path.first())
            .cloned()
            .ok_or_else(|| Status::invalid_argument("DoPut needs a descriptor path"))?;
        validate_name(&name)?;

        let store_path = self.out_dir.join(format!("{name}.arrow"));
        let mut writer: Option<FileWriter<BufWriter<File>>> = None;
        let mut hasher = crc32fast::Hasher::new();
        let mut ack = PutAck {
            name: name.clone(),
            messages: 0,
            batches: 0,
            rows: 0,
            payload_bytes: 0,
            wire_bytes: 0,
            crc32: 0,
            receive_ms: 0.0,
            stored_path: store_path.display().to_string(),
            stored_bytes: 0,
        };

        let messages = stream::once(async { Ok(first) }).chain(input.map_err(FlightError::from));
        let mut decoder = FlightDataDecoder::new(messages);
        while let Some(decoded) = decoder.next().await {
            let decoded = decoded.map_err(Status::from)?;
            ack.messages += 1;
            ack.wire_bytes += wire_size(&decoded.inner);
            match decoded.payload {
                DecodedPayload::Schema(schema) => {
                    let file = File::create(&store_path).map_err(internal)?;
                    writer =
                        Some(FileWriter::try_new(BufWriter::new(file), &schema).map_err(internal)?);
                }
                DecodedPayload::RecordBatch(batch) => {
                    consume_batch(&batch, &mut ack, &mut hasher)?;
                    if let Some(writer) = writer.as_mut() {
                        writer.write(&batch).map_err(internal)?;
                    }
                }
                DecodedPayload::None => {}
            }
        }
        if let Some(mut writer) = writer {
            writer.finish().map_err(internal)?;
        }
        ack.receive_ms = started.elapsed().as_secs_f64() * 1e3;
        ack.crc32 = hasher.finalize();

        ack.stored_bytes = std::fs::metadata(&store_path).map_err(internal)?.len();
        eprintln!(
            "stored {name}: {} chunks, {} payload bytes, {} wire bytes in {:.1} ms -> {} ({} bytes)",
            ack.rows,
            ack.payload_bytes,
            ack.wire_bytes,
            ack.receive_ms,
            ack.stored_path,
            ack.stored_bytes
        );
        Ok(ack)
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

    async fn do_get(&self, _: Request<Ticket>) -> Result<Response<Self::DoGetStream>, Status> {
        Err(Status::unimplemented("do_get"))
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
    })
    .max_decoding_message_size(MAX_MESSAGE_SIZE)
    .max_encoding_message_size(MAX_MESSAGE_SIZE);

    eprintln!(
        "listening on {} (storing to {})",
        args.addr,
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
