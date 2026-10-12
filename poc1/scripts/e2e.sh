#!/usr/bin/env bash
# End-to-end test: starts the Flight server, uploads datasets/1080p with the
# client, checks that one Arrow IPC file per video was stored and publishes the
# measurements.
#
# Environment variables:
#   DATASET     videos to upload             (default: <repo>/datasets/1080p)
#   OUT_DIR     where results are written    (default: <poc1>/e2e-out)
#   ADDR        server listen address        (default: 127.0.0.1:50051)
#   CLIENT_ARGS extra client flags, e.g. "--chunk-size 524288 --chunks-per-batch 4"
#
# On GitHub Actions the Markdown summary is appended to $GITHUB_STEP_SUMMARY.
set -euo pipefail

POC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$POC_DIR/.." && pwd)"
DATASET="${DATASET:-$REPO_DIR/datasets/1080p}"
OUT_DIR="${OUT_DIR:-$POC_DIR/e2e-out}"
ADDR="${ADDR:-127.0.0.1:50051}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/received"

echo "::group::Build"
cargo build --release --locked --manifest-path "$POC_DIR/Cargo.toml"
echo "::endgroup::"

"$POC_DIR/target/release/server" --addr "$ADDR" --out-dir "$OUT_DIR/received" \
  2> >(tee "$OUT_DIR/server.log" >&2) &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true' EXIT

# The client retries the connection until the server is listening.
# shellcheck disable=SC2086
"$POC_DIR/target/release/client" \
  --server "http://$ADDR" \
  --dataset "$DATASET" \
  --report "$OUT_DIR/report.json" \
  --markdown "$OUT_DIR/summary.md" \
  ${CLIENT_ARGS:-}

# The client already checks chunk counts, byte counts and CRC32 against the
# server acknowledgements; here we check the stored files.
status=0
for video in "$DATASET"/*; do
  [ -f "$video" ] || continue
  stored="$OUT_DIR/received/$(basename "$video").arrow"
  if [ ! -s "$stored" ]; then
    echo "missing or empty: $stored" >&2
    status=1
  fi
done

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  cat "$OUT_DIR/summary.md" >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$status" -eq 0 ]; then
  echo "e2e passed: results in $OUT_DIR"
fi
exit "$status"
