//! Arrow Flight client: uploads every video in a directory as a mosaicod
//! topic, with one `DoPut` per video, checks that the server received all the
//! data and compares the stored Parquet files with the original video. Then
//! it downloads each video back with one `DoGet` and checks that it is
//! identical to the original.

use std::{
    fmt::Write as _,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, bail, ensure};
use arrow_buffer::Buffer;
use arrow_flight::{
    FlightClient, FlightData, FlightDescriptor, PutResult, Ticket, decode::DecodedPayload,
    encode::FlightDataEncoderBuilder, flight_service_client::FlightServiceClient,
};
use clap::Parser;
use futures::{StreamExt, TryStreamExt, stream};
use poc1::{
    DoPutCmd, GetAck, MAX_DECODE_MESSAGE_SIZE, MAX_ENCODE_MESSAGE_SIZE, PutAck, Reassembler,
    TicketTopic, arrow_ipc_file_size, chunk_schema, human_size, human_size_diff, locator_part,
    split_into_batches, wire_size,
};
use prost::Message;
use serde::Serialize;
use tonic::transport::{Channel, Endpoint};

#[derive(Parser, Serialize)]
#[command(about = "Uploads videos to the Arrow Flight server and measures each step")]
struct Args {
    /// Server URL.
    #[arg(long, default_value = "http://127.0.0.1:50051")]
    server: String,
    /// Directory with the videos to upload.
    #[arg(long, default_value = "../datasets/1080p")]
    dataset: PathBuf,
    /// Chunk size in bytes.
    #[arg(long, default_value_t = 1024 * 1024)]
    chunk_size: usize,
    /// Chunks per record batch. Each batch is one Flight message and one
    /// Parquet file on the server.
    #[arg(long, default_value_t = 1)]
    chunks_per_batch: usize,
    /// mosaicod sequence the videos are uploaded to. Default: the dataset
    /// folder name. Each video is a topic named after its file name.
    #[arg(long)]
    sequence: Option<String>,
    /// Largest message the server accepts, in bytes (mosaicod default:
    /// 50 MiB). A batch must fit in one message.
    #[arg(long, default_value_t = MAX_DECODE_MESSAGE_SIZE)]
    max_message_size: usize,
    /// How long to keep retrying the connection, in seconds.
    #[arg(long, default_value_t = 30)]
    connect_timeout: u64,
    /// Write the full report as JSON to this file.
    #[arg(long)]
    report: Option<PathBuf>,
    /// Also write every downloaded video to this folder, under its file
    /// name. Without it the downloads are only compared in memory.
    #[arg(long)]
    restore_dir: Option<PathBuf>,
}

#[derive(Serialize)]
struct VideoReport {
    file: String,
    locator: String,
    file_bytes: u64,
    /// Size of an Arrow IPC file (`.arrow`) holding the same batches, which is
    /// how poc1 stored videos before following mosaicod.
    arrow_bytes: u64,
    /// `.arrow` bytes on top of the video bytes, as a percentage of the video.
    arrow_overhead_pct: f64,
    /// Serialization format of the Parquet files.
    format: String,
    /// Total size of the Parquet files stored by the server.
    stored_bytes: u64,
    /// Parquet files stored by the server, one per record batch.
    stored_files: u64,
    /// Parquet bytes on top of the video bytes, as a percentage of the video.
    stored_overhead_pct: f64,
    stored_path: String,
    chunks_sent: u64,
    chunks_received: u64,
    bytes_received: u64,
    crc32_match: bool,
    /// The server received every chunk and every byte, with a matching CRC32.
    complete: bool,
    /// What differs between what was sent and what the server received.
    problems: Vec<String>,
    batches: u64,
    flight_messages: u64,
    /// Bytes sent over gRPC (protobuf + gRPC framing).
    wire_bytes: u64,
    read_ms: f64,
    chunk_ms: f64,
    encode_ms: f64,
    /// From the start of `DoPut` until the server acknowledged it.
    upload_ms: f64,
    /// From the first message the server got until the last Parquet file
    /// was written.
    server_receive_ms: f64,
    /// Server time spent encoding Parquet files.
    server_encode_ms: f64,
    /// Server time spent writing Parquet files to disk.
    server_store_ms: f64,
    /// The `DoGet` returned every chunk in order, byte-identical to the video.
    restored: bool,
    restored_chunks: u64,
    restored_bytes: u64,
    /// Flight messages received by the `DoGet` (batches and the server ack).
    download_messages: u64,
    /// Bytes received over gRPC by the `DoGet` (protobuf + gRPC framing).
    download_wire_bytes: u64,
    /// From the `DoGet` request until the last message was received and checked.
    download_ms: f64,
    /// Server time from the `DoGet` request until the last batch was sent.
    server_send_ms: f64,
    /// Server time spent reading the Parquet files from disk.
    server_read_ms: f64,
    /// Server time spent decoding the Parquet files into record batches.
    server_decode_ms: f64,
    /// Where the downloaded video was written, with `--restore-dir`.
    restored_path: Option<String>,
    /// Time spent writing the downloaded video, with `--restore-dir`.
    restore_write_ms: f64,
}

#[derive(Serialize)]
struct Totals {
    videos: usize,
    complete: usize,
    file_bytes: u64,
    arrow_bytes: u64,
    arrow_overhead_pct: f64,
    stored_bytes: u64,
    stored_files: u64,
    stored_overhead_pct: f64,
    wire_bytes: u64,
    read_ms: f64,
    chunk_ms: f64,
    encode_ms: f64,
    upload_ms: f64,
    server_receive_ms: f64,
    server_encode_ms: f64,
    server_store_ms: f64,
    restored: usize,
    download_wire_bytes: u64,
    download_ms: f64,
    server_send_ms: f64,
    server_read_ms: f64,
    server_decode_ms: f64,
}

#[derive(Serialize)]
struct Report<'a> {
    config: &'a Args,
    videos: Vec<VideoReport>,
    totals: Totals,
}

fn ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1e3
}

/// Extra bytes of `actual` over `base`, as a percentage of `base`.
fn overhead_pct(actual: u64, base: u64) -> f64 {
    (actual as f64 / base.max(1) as f64 - 1.0) * 100.0
}

async fn connect(url: &str, timeout: Duration) -> anyhow::Result<FlightClient> {
    let endpoint = Endpoint::from_shared(url.to_string())?;
    let deadline = Instant::now() + timeout;
    let channel: Channel = loop {
        match endpoint.connect().await {
            Ok(channel) => break channel,
            Err(error) if Instant::now() < deadline => {
                eprintln!("waiting for {url}: {error}");
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            Err(error) => return Err(error).context(format!("cannot connect to {url}")),
        }
    };
    let inner = FlightServiceClient::new(channel)
        .max_decoding_message_size(MAX_ENCODE_MESSAGE_SIZE)
        .max_encoding_message_size(MAX_ENCODE_MESSAGE_SIZE);
    Ok(FlightClient::new_from_inner(inner))
}

/// Encodes record batches as Flight messages. The first one carries the
/// schema and a CMD descriptor holding the `DoPutCmd`, as mosaicod expects.
async fn encode(
    locator: &str,
    batches: Vec<arrow_array::RecordBatch>,
) -> anyhow::Result<Vec<FlightData>> {
    // mosaicod checks the UUID against the topic created beforehand with
    // `do_action`; this server has no topics to create, so any UUID works.
    let cmd = DoPutCmd {
        locator: locator.to_string(),
        topic_uuid: uuid::Uuid::new_v4().to_string(),
    };
    // Batches are sized to fit one message (see `main`), so the encoder never
    // splits them: on mosaicod a split batch would become extra Parquet files.
    let messages = FlightDataEncoderBuilder::new()
        .with_max_flight_data_size(MAX_ENCODE_MESSAGE_SIZE)
        .with_flight_descriptor(Some(FlightDescriptor::new_cmd(cmd.encode_to_vec())))
        .build(stream::iter(batches.into_iter().map(Ok)))
        .try_collect()
        .await?;
    Ok(messages)
}

/// Sends pre-encoded messages with one `DoPut` and returns the server ack.
async fn upload(client: &mut FlightClient, messages: Vec<FlightData>) -> anyhow::Result<PutAck> {
    let results: Vec<PutResult> = client
        .do_put(stream::iter(messages.into_iter().map(Ok)))
        .await?
        .try_collect()
        .await?;
    let result = results.first().context("server sent no PutResult")?;
    Ok(serde_json::from_slice(&result.app_metadata)?)
}

/// What a `DoGet` returned, checked against the original video.
struct Download {
    reassembler: Reassembler,
    /// Every chunk matched the video at its offset.
    identical: bool,
    ack: Option<GetAck>,
    messages: u64,
    wire_bytes: u64,
    problems: Vec<String>,
}

/// Downloads a topic with one `DoGet` and compares each chunk with the bytes
/// of `original` at its offset. With `restored`, also copies the chunks into it.
async fn download(
    client: &mut FlightClient,
    locator: &str,
    original: &[u8],
    mut restored: Option<&mut Vec<u8>>,
) -> anyhow::Result<Download> {
    let ticket = TicketTopic {
        locator: locator.to_string(),
        timestamp_range: None,
    };
    let mut stream = client
        .do_get(Ticket::new(ticket.encode_to_vec()))
        .await?
        .into_inner();
    let mut out_of_order = false;
    let mut result = Download {
        reassembler: Reassembler::default(),
        identical: true,
        ack: None,
        messages: 0,
        wire_bytes: 0,
        problems: Vec::new(),
    };
    while let Some(decoded) = stream.next().await {
        let decoded = decoded?;
        result.messages += 1;
        result.wire_bytes += wire_size(&decoded.inner);
        match decoded.payload {
            DecodedPayload::RecordBatch(batch) => {
                if out_of_order {
                    // Nothing more to compare.
                    continue;
                }
                let metadata = batch.schema_ref().metadata();
                if result.reassembler.chunks == 0
                    && metadata.get("file_size") != Some(&original.len().to_string())
                {
                    result.problems.push(format!(
                        "downloaded schema has file_size {:?}, video has {} bytes",
                        metadata.get("file_size"),
                        original.len()
                    ));
                }
                let pushed = result.reassembler.push(&batch, |offset, chunk| {
                    let offset = offset as usize;
                    result.identical &= original.get(offset..offset + chunk.len()) == Some(chunk);
                    if let Some(restored) = restored.as_deref_mut() {
                        restored.extend_from_slice(chunk);
                    }
                });
                if let Err(problem) = pushed {
                    result.problems.push(format!("download: {problem}"));
                    out_of_order = true;
                }
            }
            DecodedPayload::None if !decoded.inner.app_metadata.is_empty() => {
                result.ack = Some(serde_json::from_slice(&decoded.inner.app_metadata)?);
            }
            DecodedPayload::None | DecodedPayload::Schema(_) => {}
        }
    }
    Ok(result)
}

async fn send_video(
    client: &mut FlightClient,
    path: &Path,
    sequence: &str,
    args: &Args,
) -> anyhow::Result<VideoReport> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .context("video file name is not UTF-8")?
        .to_string();
    let locator = format!("{sequence}/{}", locator_part(&name));

    let started = Instant::now();
    let bytes = tokio::fs::read(path)
        .await
        .with_context(|| format!("reading {}", path.display()))?;
    let read = started.elapsed();
    let file_bytes = bytes.len() as u64;
    let crc32 = crc32fast::hash(&bytes);

    let started = Instant::now();
    let schema = chunk_schema(&name, file_bytes);
    // Kept until the download, to compare it with the video.
    let original = Buffer::from_vec(bytes);
    let batches = split_into_batches(&schema, &original, args.chunk_size, args.chunks_per_batch)?;
    let chunk = started.elapsed();
    let chunks: u64 = batches.iter().map(|batch| batch.num_rows() as u64).sum();
    let arrow_bytes = arrow_ipc_file_size(&schema, &batches)?;

    let started = Instant::now();
    let sent_batches = batches.len() as u64;
    let messages = encode(&locator, batches).await?;
    let encode_time = started.elapsed();
    let flight_messages = messages.len() as u64;
    let wire_bytes: u64 = messages.iter().map(wire_size).sum();

    let started = Instant::now();
    let ack = upload(client, messages).await?;
    let upload_time = started.elapsed();

    let mut problems = Vec::new();
    if ack.locator != locator {
        problems.push(format!("server acknowledged {}", ack.locator));
    }
    if ack.rows != chunks {
        problems.push(format!("sent {chunks} chunks, server got {}", ack.rows));
    }
    if ack.payload_bytes != file_bytes {
        problems.push(format!(
            "sent {file_bytes} bytes, server got {}",
            ack.payload_bytes
        ));
    }
    let crc32_match = ack.crc32 == crc32;
    if !crc32_match {
        problems.push("CRC32 mismatch".to_string());
    }
    if ack.wire_bytes != wire_bytes {
        problems.push(format!(
            "sent {wire_bytes} wire bytes, server counted {}",
            ack.wire_bytes
        ));
    }
    if ack.stored_files != sent_batches {
        problems.push(format!(
            "sent {sent_batches} batches, server stored {} Parquet files",
            ack.stored_files
        ));
    }
    if ack.stored_bytes == 0 {
        problems.push("server stored no Parquet data".to_string());
    }
    let complete = problems.is_empty();

    let mut restored_video = args
        .restore_dir
        .as_ref()
        .map(|_| Vec::with_capacity(original.len()));
    let started = Instant::now();
    let got = download(
        client,
        &locator,
        original.as_slice(),
        restored_video.as_mut(),
    )
    .await?;
    let download_time = started.elapsed();
    let restored_chunks = got.reassembler.chunks;
    let restored_bytes = got.reassembler.bytes;
    problems.extend(got.problems.iter().cloned());
    if restored_chunks != chunks {
        problems.push(format!(
            "sent {chunks} chunks, downloaded {restored_chunks}"
        ));
    }
    if restored_bytes != file_bytes {
        problems.push(format!(
            "sent {file_bytes} bytes, downloaded {restored_bytes}"
        ));
    }
    if !got.identical || got.reassembler.crc32() != crc32 {
        problems.push("downloaded bytes differ from the video".to_string());
    }
    let server = got.ack.unwrap_or_else(|| {
        problems.push("server sent no DoGet ack".to_string());
        GetAck::default()
    });
    let restored = got.problems.is_empty()
        && restored_chunks == chunks
        && restored_bytes == file_bytes
        && got.identical
        && got.reassembler.crc32() == crc32;

    let (restored_path, restore_write) = match (&args.restore_dir, restored_video) {
        (Some(dir), Some(video)) => {
            let path = dir.join(&name);
            let started = Instant::now();
            tokio::fs::write(&path, video)
                .await
                .with_context(|| format!("writing {}", path.display()))?;
            (Some(path.display().to_string()), started.elapsed())
        }
        _ => (None, Duration::ZERO),
    };

    Ok(VideoReport {
        file: name,
        locator,
        file_bytes,
        arrow_bytes,
        arrow_overhead_pct: overhead_pct(arrow_bytes, file_bytes),
        format: ack.format.name().to_string(),
        stored_bytes: ack.stored_bytes,
        stored_files: ack.stored_files,
        stored_overhead_pct: overhead_pct(ack.stored_bytes, file_bytes),
        stored_path: ack.stored_path,
        chunks_sent: chunks,
        chunks_received: ack.rows,
        bytes_received: ack.payload_bytes,
        crc32_match,
        complete,
        problems,
        batches: ack.batches,
        flight_messages,
        wire_bytes,
        read_ms: ms(read),
        chunk_ms: ms(chunk),
        encode_ms: ms(encode_time),
        upload_ms: ms(upload_time),
        server_receive_ms: ack.receive_ms,
        server_encode_ms: ack.encode_ms,
        server_store_ms: ack.store_ms,
        restored,
        restored_chunks,
        restored_bytes,
        download_messages: got.messages,
        download_wire_bytes: got.wire_bytes,
        download_ms: ms(download_time),
        server_send_ms: server.send_ms,
        server_read_ms: server.read_ms,
        server_decode_ms: server.decode_ms,
        restored_path,
        restore_write_ms: ms(restore_write),
    })
}

fn list_videos(dir: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let mut videos: Vec<PathBuf> = std::fs::read_dir(dir)
        .with_context(|| format!("listing {}", dir.display()))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<Result<_, _>>()?;
    videos.retain(|path| {
        path.is_file()
            && matches!(
                path.extension().and_then(|ext| ext.to_str()),
                Some("mp4" | "webm" | "mkv" | "mov" | "ivf")
            )
    });
    videos.sort();
    if videos.is_empty() {
        bail!("no videos found in {}", dir.display());
    }
    Ok(videos)
}

/// Size difference with the video, e.g. `+38.1 KiB (+0.127%)`.
fn vs_video(stored: u64, video: u64, pct: f64) -> String {
    format!("{} ({pct:+.3}%)", human_size_diff(stored, video))
}

fn render_markdown(report: &Report) -> String {
    let mut out = String::new();
    let t = &report.totals;
    let _ = writeln!(
        out,
        "## poc1: Arrow Flight upload
"
    );
    let _ = writeln!(
        out,
        "Server `{}`, dataset `{}`, chunk size {}, {} chunk(s) per batch, \
         `{}` Parquet format (one file per batch).
",
        report.config.server,
        report.config.dataset.display(),
        human_size(report.config.chunk_size as u64),
        report.config.chunks_per_batch,
        report.videos.first().map_or("?", |v| v.format.as_str())
    );
    let verdict = if t.complete == t.videos { "✅" } else { "❌" };
    let _ = writeln!(
        out,
        "{verdict} **{}/{} videos received completely** (every chunk and byte, matching CRC32) \
         in {:.2} ms of upload time.
",
        t.complete, t.videos, t.upload_ms
    );
    let verdict = if t.restored == t.videos { "✅" } else { "❌" };
    let _ = writeln!(
        out,
        "{verdict} **{}/{} videos downloaded byte-identical** with `DoGet` in {:.2} ms of \
         download time.
",
        t.restored, t.videos, t.download_ms
    );

    let _ = writeln!(
        out,
        "### Stored size
"
    );
    let _ = writeln!(
        out,
        "`.arrow` is the Arrow IPC file poc1 stored before following mosaicod, computed by \
         the client from the same batches; Parquet is what the server stores now.
"
    );
    let _ = writeln!(
        out,
        "| File | Video | `.arrow` | `.arrow` vs video | Parquet | Parquet vs video | Parquet vs `.arrow` | Parquet files |"
    );
    let _ = writeln!(out, "|---|---:|---:|---:|---:|---:|---:|---:|");
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |",
            v.file,
            human_size(v.file_bytes),
            human_size(v.arrow_bytes),
            vs_video(v.arrow_bytes, v.file_bytes, v.arrow_overhead_pct),
            human_size(v.stored_bytes),
            vs_video(v.stored_bytes, v.file_bytes, v.stored_overhead_pct),
            human_size_diff(v.stored_bytes, v.arrow_bytes),
            v.stored_files
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | {} | {} | {} | {} | {} | {} | {} |
",
        human_size(t.file_bytes),
        human_size(t.arrow_bytes),
        vs_video(t.arrow_bytes, t.file_bytes, t.arrow_overhead_pct),
        human_size(t.stored_bytes),
        vs_video(t.stored_bytes, t.file_bytes, t.stored_overhead_pct),
        human_size_diff(t.stored_bytes, t.arrow_bytes),
        t.stored_files
    );

    let _ = writeln!(
        out,
        "### Received data
"
    );
    let _ = writeln!(
        out,
        "| File | Chunks received | Data received | CRC32 | Upload ms | Server receive ms |"
    );
    let _ = writeln!(out, "|---|---:|---:|:---:|---:|---:|");
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| {} `{}` | {}/{} | {} of {} | {} | {:.2} | {:.2} |",
            if v.complete { "✅" } else { "❌" },
            v.file,
            v.chunks_received,
            v.chunks_sent,
            human_size(v.bytes_received),
            human_size(v.file_bytes),
            if v.crc32_match {
                "match"
            } else {
                "**mismatch**"
            },
            v.upload_ms,
            v.server_receive_ms
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | | | | {:.2} | {:.2} |
",
        t.upload_ms, t.server_receive_ms
    );

    let _ = writeln!(
        out,
        "### Downloaded data
"
    );
    let _ = writeln!(
        out,
        "| File | Chunks downloaded | Data downloaded | Identical | Received | Download ms | Server send ms |"
    );
    let _ = writeln!(out, "|---|---:|---:|:---:|---:|---:|---:|");
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| {} `{}` | {}/{} | {} of {} | {} | {} | {:.2} | {:.2} |",
            if v.restored { "✅" } else { "❌" },
            v.file,
            v.restored_chunks,
            v.chunks_sent,
            human_size(v.restored_bytes),
            human_size(v.file_bytes),
            if v.restored { "yes" } else { "**no**" },
            human_size(v.download_wire_bytes),
            v.download_ms,
            v.server_send_ms
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | | | | {} | {:.2} | {:.2} |
",
        human_size(t.download_wire_bytes),
        t.download_ms,
        t.server_send_ms
    );

    let problems: Vec<_> = report
        .videos
        .iter()
        .flat_map(|v| v.problems.iter().map(move |p| (&v.file, p)))
        .collect();
    if !problems.is_empty() {
        let _ = writeln!(
            out,
            "### Problems
"
        );
        for (file, problem) in problems {
            let _ = writeln!(out, "- `{file}`: {problem}");
        }
        let _ = writeln!(out);
    }

    let _ = writeln!(
        out,
        "### Client steps
"
    );
    let _ = writeln!(
        out,
        "| File | Read ms | Chunk ms | Encode ms | Sent |
|---|---:|---:|---:|---:|"
    );
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| `{}` | {:.2} | {:.3} | {:.2} | {} |",
            v.file,
            v.read_ms,
            v.chunk_ms,
            v.encode_ms,
            human_size(v.wire_bytes)
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | {:.2} | {:.3} | {:.2} | {} |",
        t.read_ms,
        t.chunk_ms,
        t.encode_ms,
        human_size(t.wire_bytes)
    );

    let _ = writeln!(
        out,
        "
### Server steps
"
    );
    let _ = writeln!(
        out,
        "| File | Parquet encode ms | Parquet write ms |
|---|---:|---:|"
    );
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| `{}` | {:.2} | {:.2} |",
            v.file, v.server_encode_ms, v.server_store_ms
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | {:.2} | {:.2} |",
        t.server_encode_ms, t.server_store_ms
    );

    let _ = writeln!(
        out,
        "
### Server steps of the download
"
    );
    let _ = writeln!(
        out,
        "| File | Parquet read ms | Parquet decode ms |
|---|---:|---:|"
    );
    for v in &report.videos {
        let _ = writeln!(
            out,
            "| `{}` | {:.2} | {:.2} |",
            v.file, v.server_read_ms, v.server_decode_ms
        );
    }
    let _ = writeln!(
        out,
        "| **Total** | {:.2} | {:.2} |",
        t.server_read_ms, t.server_decode_ms
    );
    out
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    ensure!(args.chunk_size > 0, "--chunk-size must be at least 1");
    ensure!(
        args.chunks_per_batch > 0,
        "--chunks-per-batch must be at least 1"
    );
    ensure!(
        args.chunk_size.saturating_mul(args.chunks_per_batch) < i32::MAX as usize,
        "a batch must hold less than 2 GiB"
    );
    // Room for the Arrow IPC header, the other columns and the protobuf and
    // gRPC framing of each message.
    let batch_limit = args.max_message_size.saturating_sub(64 * 1024);
    ensure!(
        args.chunk_size.saturating_mul(args.chunks_per_batch) <= batch_limit,
        "--chunk-size x --chunks-per-batch must be at most {batch_limit} bytes to fit in one \
         {}-byte message (--max-message-size)",
        args.max_message_size
    );
    if let Some(dir) = &args.restore_dir {
        std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    }
    let sequence = match &args.sequence {
        Some(sequence) => sequence.clone(),
        None => locator_part(
            &args
                .dataset
                .canonicalize()
                .with_context(|| format!("resolving {}", args.dataset.display()))?
                .file_name()
                .context("dataset folder has no name")?
                .to_string_lossy(),
        ),
    };
    ensure!(
        poc1::parse_topic_locator(&format!("{sequence}/topic")).is_some(),
        "invalid sequence name {sequence:?}"
    );
    let videos = list_videos(&args.dataset)?;
    let mut client = connect(&args.server, Duration::from_secs(args.connect_timeout)).await?;

    let mut reports = Vec::with_capacity(videos.len());
    for path in &videos {
        reports.push(send_video(&mut client, path, &sequence, &args).await?);
    }

    let file_bytes = reports.iter().map(|v| v.file_bytes).sum();
    let arrow_bytes = reports.iter().map(|v| v.arrow_bytes).sum();
    let stored_bytes = reports.iter().map(|v| v.stored_bytes).sum();
    let totals = Totals {
        videos: reports.len(),
        complete: reports.iter().filter(|v| v.complete).count(),
        file_bytes,
        arrow_bytes,
        arrow_overhead_pct: overhead_pct(arrow_bytes, file_bytes),
        stored_bytes,
        stored_files: reports.iter().map(|v| v.stored_files).sum(),
        stored_overhead_pct: overhead_pct(stored_bytes, file_bytes),
        wire_bytes: reports.iter().map(|v| v.wire_bytes).sum(),
        read_ms: reports.iter().map(|v| v.read_ms).sum(),
        chunk_ms: reports.iter().map(|v| v.chunk_ms).sum(),
        encode_ms: reports.iter().map(|v| v.encode_ms).sum(),
        upload_ms: reports.iter().map(|v| v.upload_ms).sum(),
        server_receive_ms: reports.iter().map(|v| v.server_receive_ms).sum(),
        server_encode_ms: reports.iter().map(|v| v.server_encode_ms).sum(),
        server_store_ms: reports.iter().map(|v| v.server_store_ms).sum(),
        restored: reports.iter().filter(|v| v.restored).count(),
        download_wire_bytes: reports.iter().map(|v| v.download_wire_bytes).sum(),
        download_ms: reports.iter().map(|v| v.download_ms).sum(),
        server_send_ms: reports.iter().map(|v| v.server_send_ms).sum(),
        server_read_ms: reports.iter().map(|v| v.server_read_ms).sum(),
        server_decode_ms: reports.iter().map(|v| v.server_decode_ms).sum(),
    };
    let report = Report {
        config: &args,
        videos: reports,
        totals,
    };

    println!("{}", render_markdown(&report));
    if let Some(path) = &args.report {
        std::fs::write(path, serde_json::to_string_pretty(&report)?)
            .with_context(|| format!("writing {}", path.display()))?;
    }
    let t = &report.totals;
    ensure!(
        t.complete == t.videos,
        "the server did not receive all the data for {} of {} videos",
        t.videos - t.complete,
        t.videos
    );
    ensure!(
        t.restored == t.videos,
        "{} of {} videos did not download identical to the original",
        t.videos - t.restored,
        t.videos
    );
    Ok(())
}
