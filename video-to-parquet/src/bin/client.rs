//! Arrow Flight client: uploads every video in a directory as a mosaicod
//! topic, with one `DoPut` per video, checks that the server received all the
//! data and compares the stored Parquet files with the original video.

use std::{
    fmt::Write as _,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, bail, ensure};
use arrow_buffer::Buffer;
use arrow_flight::{
    FlightClient, FlightData, FlightDescriptor, PutResult, encode::FlightDataEncoderBuilder,
    flight_service_client::FlightServiceClient,
};
use clap::Parser;
use futures::{TryStreamExt, stream};
use video_to_parquet::{
    DoPutCmd, MAX_DECODE_MESSAGE_SIZE, MAX_ENCODE_MESSAGE_SIZE, PutAck, arrow_ipc_file_size,
    chunk_schema, human_size, human_size_diff, locator_part, split_into_batches, wire_size,
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
    #[arg(long, default_value = "../datasets/VideoChunks/BigBuckBunny/1080p")]
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
}

#[derive(Serialize)]
struct VideoReport {
    file: String,
    locator: String,
    file_bytes: u64,
    /// Size of an Arrow IPC file (`.arrow`) holding the same batches, which is
    /// how video-to-parquet stored videos before following mosaicod.
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
    let batches = split_into_batches(
        &schema,
        &Buffer::from_vec(bytes),
        args.chunk_size,
        args.chunks_per_batch,
    )?;
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
        complete: problems.is_empty(),
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
        "## video-to-parquet: Arrow Flight upload
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

    let _ = writeln!(
        out,
        "### Stored size
"
    );
    let _ = writeln!(
        out,
        "`.arrow` is the Arrow IPC file video-to-parquet stored before following mosaicod, computed by \
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
        video_to_parquet::parse_topic_locator(&format!("{sequence}/topic")).is_some(),
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
    Ok(())
}
