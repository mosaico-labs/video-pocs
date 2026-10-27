//! Uploads the images of a `timestamps.csv` as one mosaicod image topic,
//! with mosaicod's upload flow:
//!
//! 1. `sequence_create`
//! 2. `session_create`   -> session uuid
//! 3. `topic_create`     -> topic uuid (write key), format = IMAGE
//! 4. `DoPut`            -> one PNG per record batch, with its capture time
//! 5. `session_finalize` -> locks the session
//!
//! The images are read from disk while they are sent, so the whole sequence
//! (~9 GiB for 300 8K PNGs) is never in memory.
//!
//! The client times its own side of the upload and writes it to
//! `<out-dir>/<scenario>/<batch>/preset_<P>_crf_<C>/<topic>.client.json`,
//! apart from the server's report: the two can run in different places.
//!
//! With `--fetch`, the client instead reads the sequence's image topics back
//! from the server (one per format, stored with `server --store-images`), as
//! mosaicod's clients read a sequence, and times it (see [`fetch`]).
//!
//! Each image travels in one gRPC message, which the server rejects above
//! [`MAX_DECODE_MESSAGE_SIZE`] (50 MiB, mosaicod's default). The client checks
//! every image's size before connecting (see [`check_image_sizes`]), so an
//! image that is too large stops the upload before anything is sent.

mod fetch;

use std::{
    fmt::Write as _,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use arrow_array::{BinaryArray, Int64Array, RecordBatch, StringArray};
use arrow_flight::{
    Action, FlightClient, FlightDescriptor, encode::FlightDataEncoderBuilder, error::FlightError,
    flight_service_client::FlightServiceClient,
};
use arrow_schema::SchemaRef;
use clap::Parser;
use common::{
    DEFAULT_TOPIC,
    images::{TimedImage, read_capture_times, read_timestamps},
    protocol::{
        DoPutCmd, FORMAT_IMAGE, MAX_DECODE_MESSAGE_SIZE, ResourceLocator, ResourceUuid,
        SEQUENCE_CREATE, SESSION_CREATE, SESSION_FINALIZE, SequenceCreate, SessionCreateResponse, SessionUuid,
        TOPIC_CREATE, TopicCreate, UploadReport, image_schema,
    },
    sequence_name,
};
use futures::{StreamExt, TryStreamExt, stream};
use prost::Message;
use tonic::transport::Channel;

#[derive(Parser)]
#[command(about = "Uploads an image sequence to the server as a mosaicod image topic")]
struct Args {
    /// Server address (mosaicod's default port).
    #[arg(long, default_value = "http://127.0.0.1:6726")]
    url: String,
    /// The images to upload and their capture times
    /// [default: image-sequence/datasets/regular/batch_060/timestamps.csv].
    #[arg(long)]
    timestamps: Option<PathBuf>,
    /// Sequence name [default: scenario and batch folders, e.g. regular_batch_060].
    #[arg(long)]
    sequence: Option<String>,
    /// Topic name, inside the sequence.
    #[arg(long, default_value = DEFAULT_TOPIC)]
    topic: String,
    /// Directory where the client's timing report is written
    /// [default: image-sequence/output].
    #[arg(long)]
    out_dir: Option<PathBuf>,
    /// Instead of uploading, read the sequence's image topics back (one per
    /// format), as mosaicod's clients read a sequence, and time it.
    #[arg(long)]
    fetch: bool,
}

/// The client's side of `DoPut`, filled in while the images are read.
#[derive(Default)]
struct Sending {
    /// Time spent reading the PNGs from disk.
    read: Duration,
    /// When the last image was handed to gRPC.
    last_image: Option<Instant>,
}

/// Room kept in a message for everything other than the PNG bytes: the
/// Arrow IPC header, the timestamp and format columns, buffer padding and
/// the protobuf framing of `FlightData`. They take 721 bytes; the rest is
/// margin. The `image_message_fits` test checks it.
const MESSAGE_OVERHEAD: u64 = 64 * 1024;

/// Largest PNG the client sends: with its overhead it fits in one message.
const MAX_IMAGE_BYTES: u64 = MAX_DECODE_MESSAGE_SIZE as u64 - MESSAGE_OVERHEAD;

/// Fails, naming every image larger than [`MAX_IMAGE_BYTES`], if any of
/// them would not fit in one message.
fn check_image_sizes(images: &[TimedImage]) -> Result<()> {
    let mut too_large = String::new();
    for image in images {
        let size = std::fs::metadata(&image.path)
            .with_context(|| format!("reading {}", image.path.display()))?
            .len();
        if size > MAX_IMAGE_BYTES {
            write!(too_large, "\n  {} ({:.1} MiB)", image.image, size as f64 / 1048576.0)?;
        }
    }
    if !too_large.is_empty() {
        bail!(
            "these images are larger than {MAX_IMAGE_BYTES} bytes ({:.1} MiB), so they would not fit in one \
             message of at most {MAX_DECODE_MESSAGE_SIZE} bytes, the server's limit:{too_large}",
            MAX_IMAGE_BYTES as f64 / 1048576.0
        );
    }
    Ok(())
}

/// Sends a `DoAction` and returns the body of the last result (if any).
async fn do_action(client: &mut FlightClient, ty: &str, body: &impl Message) -> Result<Option<Vec<u8>>> {
    let action = Action::new(ty, body.encode_to_vec());
    let results: Vec<_> = client.do_action(action).await?.try_collect().await?;
    Ok(results
        .into_iter()
        .rfind(|body| !body.is_empty())
        .map(|body| body.to_vec()))
}

/// Wraps one PNG in a single-row record batch.
fn png_batch(schema: SchemaRef, timestamp_ns: i64, png: &[u8]) -> Result<RecordBatch, FlightError> {
    Ok(RecordBatch::try_new(
        schema,
        vec![
            Arc::new(Int64Array::from(vec![timestamp_ns])),
            Arc::new(StringArray::from(vec!["png"])),
            Arc::new(BinaryArray::from(vec![png])),
        ],
    )?)
}

/// Reads one image and wraps it in a single-row record batch.
async fn image_batch(schema: SchemaRef, image: TimedImage) -> Result<RecordBatch, FlightError> {
    let png = tokio::fs::read(&image.path)
        .await
        .map_err(|e| FlightError::ExternalError(format!("reading {}: {e}", image.path.display()).into()))?;
    png_batch(schema, image.timestamp_ns, &png)
}

/// Uploads the images into the topic: the first message carries the
/// `DoPutCmd` and the schema, then one record batch per image follows. The
/// encoder never splits a single-row batch, so each image is one message.
/// gRPC asks for the next image only when it has room to send it, so the
/// last image is handed over about when the others have been sent.
async fn do_put(
    client: &mut FlightClient,
    locator: &str,
    topic_uuid: &str,
    images: Vec<TimedImage>,
    sending: Arc<Mutex<Sending>>,
) -> Result<UploadReport> {
    let cmd = DoPutCmd {
        locator: locator.to_owned(),
        topic_uuid: topic_uuid.to_owned(),
    };
    let schema = image_schema();
    let batches = stream::iter(images).then(move |image| {
        let (schema, sending) = (schema.clone(), sending.clone());
        async move {
            let started = Instant::now();
            let batch = image_batch(schema, image).await;
            let mut sending = sending.lock().unwrap();
            sending.read += started.elapsed();
            sending.last_image = Some(Instant::now());
            batch
        }
    });
    let flight_data = FlightDataEncoderBuilder::new()
        .with_flight_descriptor(Some(FlightDescriptor::new_cmd(cmd.encode_to_vec())))
        .build(batches);

    let results: Vec<_> = client.do_put(flight_data).await?.try_collect().await?;
    let last = results.last().context("the server sent no upload report")?;
    Ok(serde_json::from_slice(&last.app_metadata)?)
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let started = Instant::now();
    let timestamps = args.timestamps.unwrap_or_else(common::default_timestamps);
    if args.fetch {
        let sequence = args.sequence.unwrap_or_else(|| sequence_name(&timestamps));
        let out_dir = args.out_dir.unwrap_or_else(common::default_output_dir);
        // The capture times this client uploaded, to check the ones it gets back.
        // Only the times: the images need not be on this machine.
        let uploaded: Vec<i64> = read_capture_times(&timestamps)?.into_iter().map(|(_, t)| t).collect();
        let report = fetch::run(&args.url, &sequence, &out_dir, &uploaded).await?;
        println!("client fetch times: {}", report.display());
        return Ok(());
    }
    let images = read_timestamps(&timestamps)?;
    check_image_sizes(&images)?;
    let sequence = args.sequence.unwrap_or_else(|| sequence_name(&timestamps));
    let topic = format!("{sequence}/{}", args.topic);

    let channel = Channel::from_shared(args.url.clone())?
        .connect()
        .await
        .with_context(|| format!("connecting to {}", args.url))?;
    let mut client = FlightClient::new_from_inner(
        FlightServiceClient::new(channel).max_decoding_message_size(MAX_DECODE_MESSAGE_SIZE),
    );

    // 1. sequence_create -> empty response
    let metadata = serde_json::json!({ "timestamps": timestamps.display().to_string() });
    do_action(
        &mut client,
        SEQUENCE_CREATE,
        &SequenceCreate {
            locator: sequence.clone(),
            user_metadata: metadata.to_string().into_bytes(),
        },
    )
    .await?;
    println!("sequence created: {sequence}");

    // 2. session_create -> SessionCreate { uuid, locator }
    let body = do_action(&mut client, SESSION_CREATE, &ResourceLocator { locator: sequence })
        .await?
        .context("session_create returned no body")?;
    let session = SessionCreateResponse::decode(body.as_slice())?;
    println!("session created: {} (uuid {})", session.locator, session.uuid);

    // 3. topic_create -> ResourceUuid { uuid }  (this uuid is the DoPut write key)
    let body = do_action(
        &mut client,
        TOPIC_CREATE,
        &TopicCreate {
            locator: topic.clone(),
            session_uuid: session.uuid.clone(),
            serialization_format: FORMAT_IMAGE,
            ontology_tag: "image".into(),
            user_metadata: br#"{"encoding":"png"}"#.to_vec(),
        },
    )
    .await?
    .context("topic_create returned no body")?;
    let topic_uuid = ResourceUuid::decode(body.as_slice())?.uuid;
    println!("topic created: {topic} (uuid {topic_uuid})");

    // 4. DoPut: one image per record batch
    let count = images.len();
    println!("uploading {count} images from {}", timestamps.display());
    let put_started = Instant::now();
    let setup_seconds = (put_started - started).as_secs_f64();
    let sending = Arc::new(Mutex::new(Sending::default()));
    let report = do_put(&mut client, &topic, &topic_uuid, images, sending.clone()).await?;
    let reported = Instant::now();
    let seconds = (reported - put_started).as_secs_f64();
    let Sending { read, last_image } = *sending.lock().unwrap();
    let last_image = last_image.unwrap_or(put_started);
    println!(
        "uploaded {} images ({:.1} MiB of PNG, {}x{}) in {seconds:.1} s",
        report.images,
        report.png_bytes as f64 / 1048576.0,
        report.width,
        report.height,
    );
    println!(
        "server wrote {} ({}, {:.1} MiB, {:.1}x smaller than the PNGs)",
        report.video_path,
        report.pixel_format,
        report.video_bytes as f64 / 1048576.0,
        report.png_bytes as f64 / report.video_bytes.max(1) as f64,
    );
    println!(
        "server received the images in {:.1} s; encoding took {:.1} s ({:.1} s waiting for decoded PNGs), \
         PNG decoding {:.1} s",
        report.receive_seconds, report.encode_seconds, report.encode_wait_seconds, report.decode_seconds
    );

    // 5. session_finalize -> empty response, the session is now locked
    do_action(
        &mut client,
        SESSION_FINALIZE,
        &SessionUuid {
            session_uuid: session.uuid,
        },
    )
    .await?;
    println!("session finalized: {}", session.locator);

    // The client's own times, apart from the server's (see the module docs).
    let out_dir = args.out_dir.unwrap_or_else(common::default_output_dir);
    let path = common::client_report_path(&common::video_path(&out_dir, &topic, report.preset, report.crf));
    std::fs::create_dir_all(path.parent().context("client report without a folder")?)?;
    let times = serde_json::json!({
        "images": report.images,
        "png_bytes": report.png_bytes,
        "setup_seconds": setup_seconds,
        "read_seconds": read.as_secs_f64(),
        "send_seconds": (last_image - put_started).as_secs_f64(),
        "wait_seconds": (reported - last_image).as_secs_f64(),
        "finalize_seconds": reported.elapsed().as_secs_f64(),
        "seconds": started.elapsed().as_secs_f64(),
    });
    std::fs::write(&path, serde_json::to_string_pretty(&times)?)?;
    println!("client times: {}", path.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{fs, path::Path};

    use super::*;

    /// A folder of files of the given sizes, removed when dropped. The files
    /// are sparse: only their size matters to the check.
    struct Images {
        dir: PathBuf,
        images: Vec<TimedImage>,
    }

    impl Images {
        fn new(name: &str, sizes: &[u64]) -> Self {
            let dir = std::env::temp_dir().join(format!("image-sequence-client-{name}-{}", std::process::id()));
            fs::create_dir_all(&dir).unwrap();
            let images = sizes
                .iter()
                .enumerate()
                .map(|(i, &size)| {
                    let image = format!("{i:03}.png");
                    let path = dir.join(&image);
                    fs::File::create(&path).unwrap().set_len(size).unwrap();
                    TimedImage {
                        image,
                        path,
                        timestamp_ns: i as i64,
                    }
                })
                .collect();
            Self { dir, images }
        }
    }

    impl Drop for Images {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn images_within_the_limit_pass() {
        let images = Images::new("within", &[1024, 33 * 1024 * 1024, MAX_IMAGE_BYTES]);
        check_image_sizes(&images.images).unwrap();
    }

    #[test]
    fn images_over_the_limit_are_named() {
        let images = Images::new("over", &[1024, MAX_IMAGE_BYTES + 1, 2048, 60 * 1024 * 1024]);
        let error = check_image_sizes(&images.images).unwrap_err().to_string();
        assert!(error.contains("001.png"), "{error}");
        assert!(error.contains("003.png (60.0 MiB)"), "{error}");
        assert!(!error.contains("000.png") && !error.contains("002.png"), "{error}");
    }

    #[test]
    fn missing_image_is_an_error() {
        let mut images = Images::new("missing", &[1024]);
        images.images[0].path = Path::new(&images.dir).join("nothing.png");
        assert!(check_image_sizes(&images.images).is_err());
    }

    /// The largest image the check lets through is encoded in a single
    /// message that the server accepts, and the real overhead stays well
    /// below `MESSAGE_OVERHEAD`.
    #[tokio::test]
    async fn image_message_fits() {
        let png = vec![0xAB; MAX_IMAGE_BYTES as usize];
        let batch = png_batch(image_schema(), 1_767_225_600_000_000_000, &png).unwrap();
        let messages: Vec<_> = FlightDataEncoderBuilder::new()
            .build(stream::iter([Ok(batch)]))
            .try_collect()
            .await
            .unwrap();

        // The schema message, then the batch: one message, not split.
        assert_eq!(messages.len(), 2);
        let size = messages[1].encoded_len();
        assert!(size <= MAX_DECODE_MESSAGE_SIZE, "{size} bytes");
        let overhead = size as u64 - MAX_IMAGE_BYTES;
        assert!(overhead < MESSAGE_OVERHEAD / 8, "overhead {overhead} bytes");
    }
}
