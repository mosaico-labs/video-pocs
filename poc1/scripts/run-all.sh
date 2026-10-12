#!/usr/bin/env bash
# Uploads every video under datasets/ and keeps one client report per folder.
#
# The client is run once for each folder that directly contains videos (for
# example datasets/1080p or datasets/LargeFiles/480p/AV1). OUT_DIR only gets
# data meant to be committed. The server stores the .arrow files in a
# temporary folder, emptied after each dataset folder and removed at the end,
# since the reports already record their sizes; set KEEP_ARROW=1 to keep them
# in <poc1>/received instead (ignored by git, ~28 GB for every dataset).
#
# Environment variables:
#   DATASETS    root to scan                 (default: <repo>/datasets)
#   OUT_DIR     where results are written    (default: <poc1>/results)
#   ADDR        server listen address        (default: 127.0.0.1:50051)
#   CLIENT_ARGS extra client flags, e.g. "--chunk-size 524288"
#   KEEP_ARROW  keep the received .arrow files in <poc1>/received when set to 1
set -euo pipefail

POC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$POC_DIR/.." && pwd)"
DATASETS="${DATASETS:-$REPO_DIR/datasets}"
OUT_DIR="${OUT_DIR:-$POC_DIR/results}"
ADDR="${ADDR:-127.0.0.1:50051}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/reports"
if [ "${KEEP_ARROW:-0}" = 1 ]; then
  ARROW_DIR="$POC_DIR/received"
  rm -rf "$ARROW_DIR"
  mkdir -p "$ARROW_DIR"
else
  ARROW_DIR="$(mktemp -d)"
fi

cargo build --release --locked --manifest-path "$POC_DIR/Cargo.toml"

"$POC_DIR/target/release/server" --addr "$ADDR" --out-dir "$ARROW_DIR" \
  2>"$OUT_DIR/server.log" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  [ "${KEEP_ARROW:-0}" = 1 ] || rm -rf "$ARROW_DIR"
}
trap cleanup EXIT

status=0
while IFS= read -r dir; do
  if [ "$dir" = "$DATASETS" ]; then rel="$(basename "$dir")"; else rel="${dir#"$DATASETS"/}"; fi
  # "LargeFiles/4K Videos (4096x2160)/AV1" -> "LargeFiles_4K_Videos_4096x2160_AV1"
  slug="$(printf '%s' "$rel" | tr -c 'A-Za-z0-9.-' '_' | tr -s '_' | sed 's/_$//')"
  echo "==> $rel"
  # shellcheck disable=SC2086
  if ! "$POC_DIR/target/release/client" \
    --server "http://$ADDR" \
    --dataset "$dir" \
    --report "$OUT_DIR/reports/$slug.json" \
    --markdown "$OUT_DIR/reports/$slug.md" \
    ${CLIENT_ARGS:-} >/dev/null; then
    echo "client failed for $rel" >&2
    status=1
  fi
  [ "${KEEP_ARROW:-0}" = 1 ] || find "$ARROW_DIR" -name '*.arrow' -delete
done < <(find "$DATASETS" -type f \( -name '*.mp4' -o -name '*.webm' -o -name '*.mkv' \
  -o -name '*.mov' -o -name '*.ivf' \) -exec dirname {} \; | sort -u)

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

# Results are committed: make paths relative to the repository, and drop the
# temporary .arrow folder, so they don't carry local paths.
find "$OUT_DIR/reports" "$OUT_DIR/server.log" -type f \
  -exec sed -i.bak -e "s|$REPO_DIR/||g" -e "s|$ARROW_DIR/||g" -e "s|$ARROW_DIR|<temporary folder>|g" {} +
find "$OUT_DIR" -name '*.bak' -delete

echo "reports in $OUT_DIR/reports"
exit "$status"
