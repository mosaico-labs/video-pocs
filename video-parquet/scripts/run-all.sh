#!/usr/bin/env bash
# Uploads every video under datasets/VideoChunks/ and keeps one JSON client report per folder.
#
# The client is run once for each folder that directly contains videos (for
# example datasets/VideoChunks/BigBuckBunny/1080p or datasets/VideoChunks/SEPE/480p/AV1). The client fails if
# the server did not receive every chunk and byte with a matching CRC32; after
# each folder the script also checks that the server stored a metadata.json
# and Parquet files for every video. Run it on one folder as an end-to-end test:
#   DATASETS=datasets/VideoChunks/BigBuckBunny/1080p OUT_DIR=/tmp/video-parquet-1080p video-parquet/scripts/run-all.sh
#
# OUT_DIR only gets data meant to be committed. The server stores each video as
# a mosaicod topic (metadata.json and Parquet files) in a temporary folder,
# emptied after each dataset folder and removed at the end, since the reports
# already record their sizes; set KEEP_DATA=1 to keep them in <video-parquet>/received
# instead (ignored by git, ~28 GB for every dataset).
#
# Environment variables:
#   DATASETS    root to scan                 (default: <repo>/datasets/VideoChunks)
#   OUT_DIR     where results are written    (default: <video-parquet>/results)
#   ADDR        server listen address        (default: 127.0.0.1:50051)
#   CLIENT_ARGS extra client flags, e.g. "--chunk-size 524288"
#   SERVER_ARGS extra server flags, e.g. "--format image"
#   KEEP_DATA   keep the received topics in <video-parquet>/received when set to 1
set -euo pipefail

POC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$POC_DIR/.." && pwd)"
DATASETS="${DATASETS:-$REPO_DIR/datasets/VideoChunks}"
OUT_DIR="${OUT_DIR:-$POC_DIR/results}"
ADDR="${ADDR:-127.0.0.1:50051}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/reports"
if [ "${KEEP_DATA:-0}" = 1 ]; then
  DATA_DIR="$POC_DIR/received"
  rm -rf "$DATA_DIR"
  mkdir -p "$DATA_DIR"
else
  DATA_DIR="$(mktemp -d)"
fi

cargo build --release --locked --manifest-path "$POC_DIR/Cargo.toml"

# shellcheck disable=SC2086
"$POC_DIR/target/release/server" --addr "$ADDR" --out-dir "$DATA_DIR" ${SERVER_ARGS:-} \
  2>"$OUT_DIR/server.log" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  [ "${KEEP_DATA:-0}" = 1 ] || rm -rf "$DATA_DIR"
}
trap cleanup EXIT

# Locator parts replace every character other than letters, digits and "-"
# with "_".
locator_part() { printf '%s' "$1" | tr -c 'A-Za-z0-9-' '_'; }
is_video() { case "$1" in *.mp4 | *.webm | *.mkv | *.mov | *.ivf) return 0 ;; *) return 1 ;; esac; }

status=0
while IFS= read -r dir; do
  if [ "$dir" = "$DATASETS" ]; then rel="$(basename "$dir")"; else rel="${dir#"$DATASETS"/}"; fi
  # "SEPE/4K Videos (4096x2160)/AV1" -> "SEPE_4K_Videos_4096x2160_AV1"
  # The slug is also the mosaicod sequence name, which cannot contain ".".
  slug="$(printf '%s' "$rel" | tr -c 'A-Za-z0-9-' '_' | tr -s '_' | sed 's/_$//')"
  echo "==> $rel"
  # shellcheck disable=SC2086
  if ! "$POC_DIR/target/release/client" \
    --server "http://$ADDR" \
    --dataset "$dir" \
    --sequence "$slug" \
    --report "$OUT_DIR/reports/$slug.json" \
    ${CLIENT_ARGS:-} >/dev/null; then
    echo "client failed for $rel" >&2
    status=1
  fi
  for video in "$dir"/*; do
    [ -f "$video" ] && is_video "$video" || continue
    topic="$DATA_DIR/$slug/$(locator_part "$(basename "$video")")"
    if [ ! -s "$topic/metadata.json" ] || ! ls "$topic"/data/*.parquet >/dev/null 2>&1; then
      echo "missing metadata.json or Parquet files: ${topic#"$DATA_DIR"/}" >&2
      status=1
    fi
  done
  [ "${KEEP_DATA:-0}" = 1 ] || find "$DATA_DIR" -mindepth 1 -delete
done < <(find "$DATASETS" -type f \( -name '*.mp4' -o -name '*.webm' -o -name '*.mkv' \
  -o -name '*.mov' -o -name '*.ivf' \) -exec dirname {} \; | sort -u)

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

# Results are committed: make paths relative to the repository, and drop the
# temporary data folder, so they don't carry local paths.
find "$OUT_DIR/reports" "$OUT_DIR/server.log" -type f \
  -exec sed -i.bak -e "s|$REPO_DIR/||g" -e "s|$DATA_DIR/||g" -e "s|$DATA_DIR|<temporary folder>|g" {} +
find "$OUT_DIR" -name '*.bak' -delete

if [ "$status" -eq 0 ]; then
  echo "all videos received and stored: reports in $OUT_DIR/reports"
else
  echo "some videos failed, see the errors above: reports in $OUT_DIR/reports" >&2
fi
exit "$status"
