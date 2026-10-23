#!/usr/bin/env bash
# End-to-end test of the client and the server on the first 60 frames of the
# SEPE sequence 001 (regular/batch_060): builds the workspace, starts the
# server, uploads the frames with the client, then checks the video the
# server wrote. Runs the same locally and in CI
# (.github/workflows/image-sequence.yml).
#
#   image-sequence/scripts/e2e.sh
#
# Frames missing from datasets/ImageSequence/001/ are downloaded from the
# dataset's Google Drive folder (~2 GB for the 60). Everything else is
# written under output/ (not tracked by git). Needs ffprobe (part of FFmpeg).
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(dirname "$WORKSPACE")"
cd "$WORKSPACE"

# Google Drive folder of sequence 001 (see scripts/build-datasets.sh).
DRIVE_FOLDER="1wX3zV3zAJcS7l59DykFUdKLYP6daCkDd"
TIMESTAMPS="datasets/regular/batch_060/timestamps.csv"
# Where the server writes it with its default settings (preset 9, CRF 30).
VIDEO_DIR="output/regular/batch_060/preset_9_crf_30"
WORK="output/e2e"
SERVER_LOG="$WORK/server.log"
rm -rf "$WORK" "$VIDEO_DIR"
mkdir -p "$WORK"

server=""
# On exit: stop the server, and on failure show its log, which is otherwise
# only in a file.
finish() {
    local status=$?
    [ -n "$server" ] && kill "$server" 2>/dev/null || true
    if [ "$status" -ne 0 ] && [ -s "$SERVER_LOG" ]; then
        echo "== server log" >&2
        grep -v '^Svt\[info\]' "$SERVER_LOG" >&2 || true
    fi
    exit "$status"
}
trap finish EXIT

echo "== input"
# The CSV's images (datasets/ImageSequence/001/NNN_8K.png) that are missing,
# with their Drive file ids, downloaded 4 at a time. -r: GNU xargs would
# otherwise run curl once, without a URL, when none is missing.
python3 -I - "$REPO" "$TIMESTAMPS" "$DRIVE_FOLDER" <<'EOF' | xargs -0 -r -n 2 -P 4 sh -c '
    curl -sS --fail -L --retry 5 --retry-all-errors -o "$1.part" "$0" &&
    if head -c 512 "$1.part" | grep -qi "<html"; then
        echo "got an HTML page instead of $1" >&2; rm -f "$1.part"; exit 1
    fi && mv "$1.part" "$1" && echo "downloaded $1"'
import csv, html, os, re, sys, urllib.request

repo, timestamps, folder = sys.argv[1:]
images = [row["image"] for row in csv.DictReader(open(timestamps))]
missing = [image for image in images if not os.path.isfile(os.path.join(repo, image))]
if missing:
    page = urllib.request.urlopen(f"https://drive.google.com/embeddedfolderview?id={folder}").read().decode()
    ids = {html.unescape(name): id_ for id_, name in re.findall(
        r'<a href="https://drive\.google\.com/file/d/([\w-]+)[^"]*"[^>]*>.*?'
        r'<div class="flip-entry-title">([^<]*)</div>', page, re.S)}
    for image in missing:
        name = os.path.basename(image)
        if name not in ids:
            sys.exit(f"{name} is not in the Drive folder {folder}")
        os.makedirs(os.path.dirname(os.path.join(repo, image)), exist_ok=True)
        url = f"https://drive.usercontent.google.com/download?id={ids[name]}&export=download&confirm=t"
        sys.stdout.write(f"{url}\0{os.path.join(repo, image)}\0")
print(f"{len(images) - len(missing)} of {len(images)} frames already present", file=sys.stderr)
EOF

echo "== build"
cargo build --release --quiet

echo "== server"
./target/release/server > "$SERVER_LOG" 2>&1 &
server=$!
# Wait until it listens. If it exits first (e.g. the port is taken by
# another server), fail instead of uploading to whatever holds the port.
for _ in $(seq 150); do
    grep -q '^listening on' "$SERVER_LOG" && break
    if ! kill -0 "$server" 2>/dev/null; then
        echo "the server exited before listening" >&2
        exit 1
    fi
    sleep 0.2
done
if ! grep -q '^listening on' "$SERVER_LOG"; then
    echo "the server did not start listening within 30 s" >&2
    exit 1
fi

echo "== client"
./target/release/client --timestamps "$TIMESTAMPS"
kill "$server"
wait "$server" 2>/dev/null || true
server=""

echo "== checks"
# Codec, size and packet count of the video, read without decoding it.
PROBE="$(ffprobe -v error -select_streams v:0 -count_packets \
    -show_entries stream=codec_name,width,height,nb_read_packets -of csv=p=0 "$VIDEO_DIR/camera.mkv")"
python3 - "$VIDEO_DIR" "$TIMESTAMPS" "$PROBE" <<'EOF'
import csv, json, os, sys

video_dir, timestamps, probe = sys.argv[1:]
codec, width, height, packets = probe.split(",")
width, height, packets = int(width), int(height), int(packets)
captures = [int(row["timestamp_ns"]) for row in csv.DictReader(open(timestamps))]
frames = len(captures)
video = os.path.join(video_dir, "camera.mkv")
failures = []

def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)

check(codec == "av1", f"the video is AV1 ({codec})")
check((width, height) == (8192, 4320), f"the video is 8192x4320 ({width}x{height})")
check(packets == frames, f"the video holds {frames} frames ({packets})")

upload = json.load(open(os.path.join(video_dir, "camera.upload.json")))
check(upload["images"] == frames, f"the server received {frames} images ({upload['images']})")
check(upload["pixel_format"] == "yuv420p", f"the server encoded yuv420p ({upload['pixel_format']})")
# The regular batch's capture times are evenly spaced: no timing is lost at a constant rate.
timing = upload.get("timing", {})
check(timing == {"mode": "constant", "frame_rate": {"num": 30000, "den": 1001}},
      f"the video is at the constant frame rate 30000/1001 ({timing})")
check(captures and (upload["first_timestamp_ns"], upload["last_timestamp_ns"]) == (captures[0], captures[-1]),
      "the server kept the first and last capture times")
check(upload["video_bytes"] == os.path.getsize(video), "the reported video size matches the file")

if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all checks passed")
EOF
