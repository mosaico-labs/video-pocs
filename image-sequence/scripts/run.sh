#!/usr/bin/env bash
# Encodes one batch of images, measures the quality of the video and writes
# the results to results/<scenario>/<batch>/preset_<P>_crf_<C>/.
#
#   image-sequence/scripts/run.sh                      # regular/batch_060
#   image-sequence/scripts/run.sh <timestamps.csv>
#   PRESET=4 CRF=35 image-sequence/scripts/run.sh      # other encoder settings
#   LOSSLESS=1 image-sequence/scripts/run.sh           # lossless AV1: only the
#                                                      # YUV 4:2:0 conversion loses
#
# Starts the server, uploads the batch with the client, stops the server,
# then runs the quality tool and scripts/report.py (with uv). PRESET and CRF default to the
# server's (9 and 30); with LOSSLESS=1 the CRF is ignored and everything goes
# to preset_<P>_lossless/ instead. Logs go to
# output/logs/<scenario>/<batch>/<that folder>/.
#
# A lossless video decodes to the same frames at every preset, so its quality
# is the same too: when another preset's lossless video of the batch was
# measured already, and its frames are the same (dav1d, MD5 of each frame),
# its quality files are reused instead of measuring again (~7 min at 8K).
# Only a measured video is reused from, never one that reused another.
# Its AV1 decoding time is not reused: it is the other video's.
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKSPACE"
TIMESTAMPS="${1:-datasets/regular/batch_060/timestamps.csv}"
PRESET="${PRESET:-9}"
CRF="${CRF:-30}"
if [[ -n "${LOSSLESS:-}" ]]; then
    SETTINGS=(--preset "$PRESET" --lossless)
    NAME="preset_${PRESET}_lossless"
else
    SETTINGS=(--preset "$PRESET" --crf "$CRF")
    NAME="preset_${PRESET}_crf_${CRF}"
fi
# output/<scenario>/<batch>, from datasets/<scenario>/<batch>/timestamps.csv.
BATCH_DIR="$(dirname "$TIMESTAMPS")"
BATCH="$(basename "$(dirname "$BATCH_DIR")")/$(basename "$BATCH_DIR")"
OUTPUT="output/$BATCH"
LOGS="output/logs/$BATCH/$NAME"
mkdir -p "$LOGS"

cargo build --release --quiet

echo "== encoding"
./target/release/server "${SETTINGS[@]}" > "$LOGS/server.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
for _ in $(seq 100); do
    grep -q '^listening on' "$LOGS/server.log" && break
    sleep 0.2
done
./target/release/client --timestamps "$TIMESTAMPS" | tee "$LOGS/client.log"
kill "$server"
wait "$server" 2>/dev/null || true
trap - EXIT

# The MD5 of each decoded frame, without the header comments.
frame_hashes() {
    ffmpeg -nostdin -v error -c:v libdav1d -i "$1" -f framemd5 - | grep -v '^#'
}

reused=""
if [[ -n "${LOSSLESS:-}" ]]; then
    for other in "$OUTPUT"/preset_*_lossless; do
        [[ "$other" != "$OUTPUT/$NAME" && -f "$other/camera.quality.csv" && -f "$other/camera.mkv" ]] || continue
        # Only from a video that was measured: a reused one has no record of
        # the measurement, and two reusing each other would lose it for good.
        ! grep -q '"reused_from"' "$other/camera.quality.json" 2>/dev/null || continue
        echo "== checking the frames against $other"
        frame_hashes "$OUTPUT/$NAME/camera.mkv" > "$LOGS/frames.md5"
        frame_hashes "$other/camera.mkv" > "$LOGS/frames-other.md5"
        if cmp -s "$LOGS/frames.md5" "$LOGS/frames-other.md5"; then
            echo "   $(wc -l < "$LOGS/frames.md5" | tr -d ' ') frames identical: reusing its quality"
            cp "$other/camera.quality.csv" "$other/camera.quality.txt" "$OUTPUT/$NAME/"
            printf '{\n  "reused_from": "%s",\n  "frames": %d\n}\n' \
                "$(basename "$other")" "$(wc -l < "$LOGS/frames.md5")" > "$OUTPUT/$NAME/camera.quality.json"
            reused="$other"
        else
            echo "   the frames differ from $other: measuring"
        fi
        break
    done
fi
if [[ -z "$reused" ]]; then
    echo "== measuring quality"
    ./target/release/quality --timestamps "$TIMESTAMPS" "${SETTINGS[@]}" 2> "$LOGS/quality.log"
fi

uv run scripts/report.py --timestamps "$TIMESTAMPS" "${SETTINGS[@]}"
