# poc1: video upload over Arrow Flight

A Rust client and server built on [Arrow Flight](https://arrow.apache.org/docs/format/Flight.html).
The client reads every video in a directory (by default `datasets/1080p`),
splits it into chunks and uploads each video with its **own `DoPut` call**.
The server stores each upload as an Arrow IPC file, `<video name>.arrow`.

## Repository layout

```
poc1/
├── Cargo.toml, Cargo.lock
├── src/
│   ├── lib.rs                 # chunk schema, splitting into record batches, server ack
│   └── bin/
│       ├── client.rs          # reads videos, uploads them, checks and reports
│       └── server.rs          # receives DoPut calls, writes .arrow files
├── scripts/
│   ├── e2e.sh                 # end-to-end test on datasets/1080p
│   ├── run-all.sh             # uploads every video under datasets/
│   ├── flatten.py             # merges the reports into results/videos.csv
│   ├── results_md.py          # writes results/RESULTS.md and results/upload-time.png
│   └── per_video_charts.py    # one size chart per video in results/per-video/
├── results/                   # latest full run (committed)
│   ├── RESULTS.md             # summary, findings and tables
│   ├── upload-time.png        # upload time per video
│   ├── videos.csv             # one row per video
│   ├── reports/               # client report per dataset folder (.json, .md)
│   ├── per-video/             # video vs .arrow size chart per video, with README.md index
│   └── server.log
├── e2e-out/                   # e2e.sh output (git-ignored)
├── received/                  # .arrow files kept by run-all.sh with KEEP_ARROW=1 (git-ignored)
└── target/                    # Cargo build output (git-ignored)
```

## Data layout

The Flight descriptor path is the video file name. Each record batch holds
`--chunks-per-batch` chunks:

| Column | Type | Meaning |
|---|---|---|
| `chunk_index` | `UInt32` | Position of the chunk in the file |
| `offset` | `UInt64` | Byte offset of the chunk in the file |
| `data` | `Binary` | Raw bytes of the chunk |

The video name and original file size are stored as schema metadata.
Chunks hold the encoded bytes exactly as they are on disk. The video is not
decoded or demuxed into frames.

## What is measured

For each video the server sends back what it received: chunk count, byte
counts, a CRC32 of the reassembled data, how long it took and the size of the
`.arrow` file it wrote. The client compares this with what it sent and
reports:

| Metric | What it covers |
|---|---|
| Video / `.arrow` size | Size of the original video and of the stored Arrow IPC file, and how much bigger the `.arrow` file is |
| Received | Whether the server got every chunk and every byte with a matching CRC32 |
| Upload | From the start of `DoPut` until the server acknowledges it |
| Server receive | From the first message the server gets until the `.arrow` file is written |
| Read, Chunk, Encode | Client steps before the upload: loading the file, splitting it into record batches (zero-copy slices) and serializing them into Flight messages |

The client writes every report before exiting. If any video was not
received completely, the summary lists what differs and the client exits with
an error.

## Running

```sh
cd poc1
cargo run --release --bin server -- --addr 127.0.0.1:50051 --out-dir received
# in another terminal
cargo run --release --bin client -- --dataset ../datasets/1080p --report report.json --markdown summary.md
```

Run `--help` on either binary for all options (`--chunk-size`,
`--chunks-per-batch`, ...).

## End-to-end test

A quick check that client and server work together, on the 4 videos in
`datasets/1080p` (about 120 MB). Use it after changing the code; use
[Running on every dataset](#running-on-every-dataset) to measure everything.

```sh
poc1/scripts/e2e.sh
```

The script:

1. builds both binaries in release mode;
2. starts the server, which stores the `.arrow` files in `poc1/e2e-out/received/`;
3. runs the client on `datasets/1080p`. The client fails if the server did
   not receive every chunk and byte with a matching CRC32;
4. checks that a non-empty `.arrow` file was stored for each video;
5. stops the server and prints `e2e passed`, or exits with an error.

Everything goes to `poc1/e2e-out/` (ignored by git), which is cleared at the
start of each run:

| File | Content |
|---|---|
| `summary.md` | The client's Markdown summary, also printed to the terminal |
| `report.json` | The full client report |
| `server.log` | The server's log, one line per stored video |
| `received/` | The `.arrow` files the server wrote |

It accepts these environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DATASET` | `datasets/1080p` | Folder of videos to upload |
| `OUT_DIR` | `poc1/e2e-out/` | Where the files above go |
| `ADDR` | `127.0.0.1:50051` | Server listen address |
| `CLIENT_ARGS` | none | Extra client flags |

For example, to test 512 KiB chunks with 4 chunks per record batch:

```sh
CLIENT_ARGS="--chunk-size 524288 --chunks-per-batch 4" poc1/scripts/e2e.sh
```

The [`poc1` workflow](../.github/workflows/poc1.yml) runs the unit tests and
this script on GitHub Actions for every pull request and every push to `main`
that touches `poc1/` or `datasets/1080p/`. It adds `summary.md` to the job
page and uploads `summary.md`, `report.json` and `server.log` as the `poc1-e2e`
artifact.

## Running on every dataset

This is how the committed [`results/`](results/) folder is produced: every
video under `datasets/` is uploaded, and the measurements are turned into a
summary and charts.

**Requirements:** Rust (to build the client and server), Python 3 and
[`uv`](https://docs.astral.sh/uv/). `uv` installs `matplotlib` for the chart
scripts on first run. `datasets/LargeFiles` is optional: without it, only the
tracked `datasets/360p` and `datasets/1080p` videos are uploaded.

Run the four steps from the repository root, in order:

```sh
# 1. Upload every video and write one report per dataset folder
poc1/scripts/run-all.sh

# 2. Merge the reports into one CSV, one row per video
python3 poc1/scripts/flatten.py

# 3. Write the summary and the upload time chart
uv run poc1/scripts/results_md.py

# 4. Optional: one video vs .arrow size chart per video
uv run poc1/scripts/per_video_charts.py
```

| Step | Script | Writes to `poc1/results/` |
|---|---|---|
| 1 | `run-all.sh` | `reports/<folder>.json` and `.md`, `server.log` |
| 2 | `flatten.py` | `videos.csv` |
| 3 | `results_md.py` | `RESULTS.md`, `upload-time.png` |
| 4 | `per_video_charts.py` | `per-video/<dataset>/<resolution>/<codec>/<video>.png`, `per-video/README.md` |

Step 1 deletes `poc1/results/` before it starts, so always run the later
steps after it. Then commit `poc1/results/`: it holds only data meant for the
repository, and the paths in the reports are relative to the repository.

**How step 1 works.** `run-all.sh` builds the binaries, starts the server once
and runs the client on each folder that directly holds videos, for example
`datasets/1080p` or `datasets/LargeFiles/480p/AV1`. A client failure on one
folder is reported and the script moves on to the next, exiting with an error
at the end. The server writes the `.arrow` files to a temporary folder that is
emptied after each dataset folder and removed at the end, because the reports
already record their sizes.

Step 1 accepts these environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DATASETS` | `datasets/` | Folder to scan for videos |
| `OUT_DIR` | `poc1/results/` | Where the reports go |
| `ADDR` | `127.0.0.1:50051` | Server listen address |
| `CLIENT_ARGS` | none | Extra client flags, e.g. `"--chunk-size 524288"` |
| `KEEP_ARROW` | `0` | `1` keeps the `.arrow` files in `poc1/received/` (ignored by git; about 28 GB with `LargeFiles`) |

For example, to try 512 KiB chunks on the 1080p videos only, writing to a
separate folder so the committed results stay untouched:

```sh
DATASETS=datasets/1080p OUT_DIR=/tmp/poc1-1080p CLIENT_ARGS="--chunk-size 524288" \
  poc1/scripts/run-all.sh
python3 poc1/scripts/flatten.py /tmp/poc1-1080p
uv run poc1/scripts/results_md.py /tmp/poc1-1080p
```

## Notes

Client and server both run on the same machine, so upload times measure
loopback, not a real network. To test a real link, run the server on another
host and point `--server` at it.
