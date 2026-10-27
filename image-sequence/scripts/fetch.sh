#!/usr/bin/env bash
# Reads a batch's images back from the server, as mosaicod's clients read a
# sequence, and times it, for every format: once with the server's DoGet
# streams LZ4-compressed, as mosaicod sends them, and once uncompressed.
#
#   image-sequence/scripts/fetch.sh                      # regular/batch_060, preset 5, CRF 18
#   PRESET=9 CRF=30 image-sequence/scripts/fetch.sh <timestamps.csv>
#
# Needs the batch's image topics (server --extract-images). Each run starts
# the server on loopback, fetches with the client, then stops the server.
# The client's reports go next to the video, as <topic>.fetch-lz4_frame.json
# and <topic>.fetch-none.json; logs to output/logs/<scenario>/<batch>/<encoding>/.
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKSPACE"
TIMESTAMPS="${1:-datasets/regular/batch_060/timestamps.csv}"
PRESET="${PRESET:-5}"
CRF="${CRF:-18}"
BATCH_DIR="$(dirname "$TIMESTAMPS")"
BATCH="$(basename "$(dirname "$BATCH_DIR")")/$(basename "$BATCH_DIR")"
LOGS="output/logs/$BATCH/preset_${PRESET}_crf_${CRF}"
mkdir -p "$LOGS"

cargo build --release --quiet

for compression in lz4 none; do
    echo "== fetching, DoGet compression: $compression"
    ./target/release/server --preset "$PRESET" --crf "$CRF" --do-get-compression "$compression" \
        > "$LOGS/fetch-server-$compression.log" 2>&1 &
    server=$!
    trap 'kill $server 2>/dev/null || true' EXIT
    for _ in $(seq 100); do
        grep -q '^listening on' "$LOGS/fetch-server-$compression.log" && break
        sleep 0.2
    done
    ./target/release/client --fetch --timestamps "$TIMESTAMPS" 2>&1 | grep -v probesize | tee "$LOGS/fetch-client-$compression.log"
    kill "$server"
    wait "$server" 2>/dev/null || true
    trap - EXIT
done
