//! Reads a sequence's image topics back from the server, as mosaicod's
//! clients read a sequence, and times it:
//!
//! 1. `GetFlightInfo` on the sequence (`GetFlightInfoCmd`) -> one endpoint
//!    per image topic (one per format), with its ticket and its metadata;
//! 2. `DoGet` with each ticket -> the topic's record batches, one row per
//!    image in capture order.
//!
//! For each topic the client times the `DoGet` (when each image arrives,
//! from the call) and counts what came over the wire, then saves each image
//! as a file and decodes it with the `formats` crate's reader: one at a time,
//! then [`formats::WORKERS`] at a time. Every image of an exact format must
//! decode to the frame raw's image of the same capture time decodes to, or
//! the fetch fails; for PNG and JPEG, only their size is checked. Every
//! image's `timestamp_ns` must be the capture time the client uploaded for
//! it (its `timestamps.csv`), to the nanosecond, or the fetch fails.
//!
//! The report goes to
//! `<out-dir>/<scenario>/<batch>/preset_<P>_crf_<C>/<topic>.fetch-<compression>.json`
//! and the images to `.../fetched/<format>/`, overwritten by the next fetch.
//! The compression is the IPC compression the client saw in the batches
//! (`lz4_frame` or `none`), as the server chose it (`--do-get-compression`).

use std::{
    collections::hash_map::DefaultHasher,
    fs,
    hash::{Hash, Hasher},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};

use anyhow::{Context, Result, bail, ensure};
use arrow_array::{Array, BinaryArray, Int64Array};
use arrow_flight::{
    FlightData, FlightDescriptor, decode::{DecodedPayload, FlightDataDecoder}, error::FlightError,
    flight_service_client::FlightServiceClient,
};
use common::protocol::{
    GetFlightInfoCmd, ImageTopicMetadata, MAX_ENCODE_MESSAGE_SIZE, TIMESTAMP_COLUMN, TopicAppMetadata,
};
use formats::{I420, WORKERS};
use futures::{StreamExt, TryStreamExt};
use prost::Message;
use rayon::prelude::*;
use serde::Serialize;
use tonic::transport::Channel;

/// One image as the client got it.
#[derive(Serialize)]
pub struct FetchedImage {
    pub timestamp_ns: i64,
    pub bytes: u64,
    /// When its batch was decoded, from the `DoGet` call.
    pub arrival_seconds: f64,
    /// Writing it to a file.
    pub save_seconds: f64,
    /// Reading the file back to a frame, one image at a time.
    pub decode_seconds: f64,
}

/// One topic (one format) read back.
#[derive(Serialize)]
pub struct FetchedTopic {
    pub locator: String,
    pub format: String,
    pub exact: bool,
    /// The IPC compression of its batches: `lz4_frame` or `none`.
    pub compression: String,
    /// The image bytes received (the `data` column).
    pub image_bytes: u64,
    /// Everything the `FlightData` messages carried: IPC headers and bodies.
    pub wire_bytes: u64,
    pub messages: u64,
    /// Rows (images) in each record batch, in order.
    pub batch_rows: Vec<usize>,
    /// From the `DoGet` call to the first image decoded from the stream.
    pub first_image_seconds: f64,
    /// From the `DoGet` call to the end of the stream.
    pub receive_seconds: f64,
    pub save_seconds: f64,
    /// Reading back every image, one at a time.
    pub decode_seconds: f64,
    /// Reading back every image, `workers` at a time (wall time).
    pub decode_parallel_seconds: f64,
    pub workers: usize,
    /// Whether every image decoded to raw's frame (exact formats, when raw
    /// was fetched first); `None` when not checked.
    pub matches_raw: Option<bool>,
    /// Every image's `timestamp_ns` is the capture time uploaded for it:
    /// always true in a report, as a mismatch fails the fetch. It records
    /// that the check ran.
    pub timestamps_as_uploaded: bool,
    pub images: Vec<FetchedImage>,
}

/// What a fetch writes to `<topic>.fetch-<compression>.json`.
#[derive(Serialize)]
pub struct FetchReport {
    pub sequence: String,
    pub preset: u8,
    pub crf: Option<u8>,
    pub width: u32,
    pub height: u32,
    pub get_flight_info_seconds: f64,
    pub topics: Vec<FetchedTopic>,
}

/// The size of a `FlightData` message's payloads, and the compression of
/// its record batch, if it holds one.
fn inspect(data: &FlightData) -> (u64, Option<String>) {
    let bytes = (data.data_header.len() + data.data_body.len() + data.app_metadata.len()) as u64;
    let compression = arrow_ipc::root_as_message(&data.data_header).ok().and_then(|message| {
        message.header_as_record_batch().map(|batch| match batch.compression() {
            Some(compression) => format!("{:?}", compression.codec()).to_lowercase(),
            None => "none".to_string(),
        })
    });
    (bytes, compression)
}

/// A hash of a frame's samples, to compare frames without keeping them.
fn frame_hash(frame: &I420) -> u64 {
    let mut hasher = DefaultHasher::new();
    frame.samples().hash(&mut hasher);
    hasher.finish()
}

/// The topic's images from one `DoGet`, with the stream's counts.
struct Received {
    images: Vec<(i64, Vec<u8>, f64)>,
    wire_bytes: u64,
    messages: u64,
    batch_rows: Vec<usize>,
    compression: Option<String>,
    first_image_seconds: f64,
    receive_seconds: f64,
}

async fn do_get(client: &mut FlightServiceClient<Channel>, ticket: arrow_flight::Ticket) -> Result<Received> {
    let started = Instant::now();
    let stream = client.do_get(ticket).await?.into_inner();
    // Counts the messages as they arrive, before the decoder.
    let counts = Arc::new(Mutex::new((0u64, 0u64, None::<String>)));
    let counted = {
        let counts = counts.clone();
        stream.map_err(FlightError::from).inspect_ok(move |data| {
            let (bytes, compression) = inspect(data);
            let mut counts = counts.lock().unwrap();
            counts.0 += bytes;
            counts.1 += 1;
            if counts.2.is_none() {
                counts.2 = compression;
            }
        })
    };
    let mut decoder = FlightDataDecoder::new(counted);
    let (mut images, mut batch_rows, mut first) = (Vec::new(), Vec::new(), None);
    while let Some(decoded) = decoder.next().await {
        let DecodedPayload::RecordBatch(batch) = decoded?.payload else { continue };
        let arrival = started.elapsed().as_secs_f64();
        first.get_or_insert(arrival);
        let times = batch
            .column_by_name(TIMESTAMP_COLUMN)
            .and_then(|c| c.as_any().downcast_ref::<Int64Array>())
            .context("no timestamp_ns column")?;
        let data = batch
            .column_by_name("data")
            .and_then(|c| c.as_any().downcast_ref::<BinaryArray>())
            .context("no binary data column")?;
        for row in 0..batch.num_rows() {
            images.push((times.value(row), data.value(row).to_vec(), arrival));
        }
        batch_rows.push(batch.num_rows());
    }
    let receive_seconds = started.elapsed().as_secs_f64();
    let (wire_bytes, messages, compression) = counts.lock().unwrap().clone();
    Ok(Received {
        images,
        wire_bytes,
        messages,
        batch_rows,
        compression,
        first_image_seconds: first.unwrap_or(receive_seconds),
        receive_seconds,
    })
}

/// Each frame's capture time and the hash of its samples.
type FrameHashes = Vec<(i64, u64)>;

/// A topic's images saved and decoded: each image's report, the wall time
/// of decoding them several at a time, whether they matched raw's frames,
/// and their frame hashes.
type Decoded = (Vec<FetchedImage>, f64, Option<bool>, FrameHashes);

/// Saves and decodes a topic's images, checking an exact format's against
/// `raw` (raw's frame hashes by capture time).
fn save_and_decode(
    format: &'static formats::Format,
    metadata: &ImageTopicMetadata,
    received: &Received,
    dir: &Path,
    raw: Option<&[(i64, u64)]>,
) -> Result<Decoded> {
    fs::create_dir_all(dir)?;
    let path = |k: usize| dir.join(format.file_name(k));
    let mut images = Vec::new();
    let mut hashes = Vec::new();
    for (k, (timestamp_ns, bytes, arrival)) in received.images.iter().enumerate() {
        let saving = Instant::now();
        fs::write(path(k), bytes)?;
        let save_seconds = saving.elapsed().as_secs_f64();
        let decoding = Instant::now();
        let frame = (format.read)(&path(k), metadata.width, metadata.height)
            .with_context(|| format!("decoding {}", path(k).display()))?;
        let decode_seconds = decoding.elapsed().as_secs_f64();
        ensure!(
            (frame.width(), frame.height()) == (metadata.width, metadata.height),
            "{} decodes to {}x{}, not {}x{}",
            path(k).display(),
            frame.width(),
            frame.height(),
            metadata.width,
            metadata.height
        );
        if format.exact {
            hashes.push((*timestamp_ns, frame_hash(&frame)));
        }
        images.push(FetchedImage {
            timestamp_ns: *timestamp_ns,
            bytes: bytes.len() as u64,
            arrival_seconds: *arrival,
            save_seconds,
            decode_seconds,
        });
    }
    let matches_raw = match (format.exact, raw) {
        (true, Some(raw)) if format.name != "raw" => {
            if hashes != raw {
                bail!("{} images don't decode to raw's frames", format.name);
            }
            Some(true)
        }
        _ => None,
    };
    // The same files again, `WORKERS` at a time.
    let pool = rayon::ThreadPoolBuilder::new().num_threads(WORKERS).build()?;
    let started = Instant::now();
    pool.install(|| {
        (0..images.len()).into_par_iter().try_for_each(|k| -> Result<()> {
            (format.read)(&path(k), metadata.width, metadata.height).map(drop)
        })
    })?;
    Ok((images, started.elapsed().as_secs_f64(), matches_raw, hashes))
}

/// Fetches every image topic of `sequence` and writes the report under
/// `out_dir`.
pub async fn run(url: &str, sequence: &str, out_dir: &Path, uploaded: &[i64]) -> Result<PathBuf> {
    let channel =
        Channel::from_shared(url.to_owned())?.connect().await.with_context(|| format!("connecting to {url}"))?;
    // As mosaicod's clients: accept the largest message the server sends.
    let mut client = FlightServiceClient::new(channel).max_decoding_message_size(MAX_ENCODE_MESSAGE_SIZE);

    // 1. GetFlightInfo on the sequence -> one endpoint per topic
    let started = Instant::now();
    let cmd = GetFlightInfoCmd { locator: sequence.to_owned(), timestamp_range: None };
    let info = client.get_flight_info(FlightDescriptor::new_cmd(cmd.encode_to_vec())).await?.into_inner();
    let get_flight_info_seconds = started.elapsed().as_secs_f64();
    println!("{sequence}: {} image topics", info.endpoint.len());

    let mut topics = Vec::new();
    let mut raw_hashes: Option<FrameHashes> = None;
    let mut first_metadata: Option<ImageTopicMetadata> = None;
    for endpoint in info.endpoint {
        let app = TopicAppMetadata::decode(endpoint.app_metadata.as_ref())?;
        let metadata: ImageTopicMetadata = serde_json::from_slice(&app.user_metadata)?;
        let format = formats::by_name(&metadata.format).with_context(|| format!("unknown format {}", metadata.format))?;
        let ticket = endpoint.ticket.context("an endpoint without a ticket")?;

        // 2. DoGet -> the topic's images
        let received = do_get(&mut client, ticket).await?;
        let times: Vec<i64> = received.images.iter().map(|(t, _, _)| *t).collect();
        ensure!(
            times == uploaded,
            "{}'s timestamps are not the {} capture times uploaded (first received {:?}, uploaded {:?})",
            format.name,
            uploaded.len(),
            times.first(),
            uploaded.first()
        );
        let encoding = common::encoding_dir(sequence, metadata.preset, metadata.crf);
        let dir = out_dir.join(&encoding).join("fetched").join(format.name);
        let (images, decode_parallel_seconds, matches_raw, hashes) =
            save_and_decode(format, &metadata, &received, &dir, raw_hashes.as_deref())?;
        if format.name == "raw" {
            raw_hashes = Some(hashes);
        }
        let topic = FetchedTopic {
            locator: app.locator,
            format: format.name.to_string(),
            exact: format.exact,
            compression: received.compression.clone().unwrap_or_else(|| "none".into()),
            image_bytes: images.iter().map(|i| i.bytes).sum(),
            wire_bytes: received.wire_bytes,
            messages: received.messages,
            batch_rows: received.batch_rows.clone(),
            first_image_seconds: received.first_image_seconds,
            receive_seconds: received.receive_seconds,
            save_seconds: images.iter().map(|i| i.save_seconds).sum(),
            decode_seconds: images.iter().map(|i| i.decode_seconds).sum(),
            decode_parallel_seconds,
            workers: WORKERS,
            matches_raw,
            timestamps_as_uploaded: true,
            images,
        };
        println!(
            "{:>4}: {} images, {:.1} MiB ({:.1} MiB on the wire, {}), received in {:.2} s (first after {:.3} s), \
             decoded in {:.2} s ({:.2} s {} at a time){}",
            topic.format,
            topic.images.len(),
            topic.image_bytes as f64 / 1048576.0,
            topic.wire_bytes as f64 / 1048576.0,
            topic.compression,
            topic.receive_seconds,
            topic.first_image_seconds,
            topic.decode_seconds,
            topic.decode_parallel_seconds,
            WORKERS,
            if topic.matches_raw == Some(true) { ", same frames as raw" } else { "" }
        );
        first_metadata.get_or_insert(metadata);
        topics.push(topic);
    }
    let metadata = first_metadata.context("the sequence has no image topics")?;
    let compression = topics.first().map_or("none".to_string(), |t| t.compression.clone());
    let report = FetchReport {
        sequence: sequence.to_owned(),
        preset: metadata.preset,
        crf: metadata.crf,
        width: metadata.width,
        height: metadata.height,
        get_flight_info_seconds,
        topics,
    };
    let path = out_dir
        .join(common::encoding_dir(sequence, metadata.preset, metadata.crf))
        .join(format!("{}.fetch-{compression}.json", common::DEFAULT_TOPIC));
    fs::write(&path, serde_json::to_vec_pretty(&report)?)?;
    Ok(path)
}
