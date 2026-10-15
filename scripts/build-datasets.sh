#!/usr/bin/env bash
# Rebuilds datasets/ from the original sources, so no dataset file has to be
# stored in git. Run it once from anywhere; it can be stopped and run again:
# files that are already complete are skipped.
#
#   scripts/build-datasets.sh                  # everything (~65 GB to download)
#   scripts/build-datasets.sh bbb youtube      # only some datasets
#   scripts/build-datasets.sh --verify sepe    # also check the SHA-256 of each file
#
# Datasets (folders under datasets/):
#   bbb        VideoChunks/BigBuckBunny          test-videos.co.uk            ~350 MB
#   youtube    VideoChunks/YouTube               YouTube, with yt-dlp         ~2.8 GB
#   sequences  VideoSequences/AV1/4K             split of the YouTube Earth video (needs youtube)
#   sepe       VideoChunks/SEPE                  SEPE 8K, Google Drive        ~28 GB
#   uvg        VideoChunks/UltraVideoGroup       ultravideo.fi, .7z archive   ~3.8 GB (7 GB extracted)
#   images     ImageSequence/{001,002,003}       SEPE 8K PNG frames, Drive    ~24 GB
#
# --verify compares every file with scripts/datasets.sha256, the checksums of
# the files this README describes. YouTube can re-encode its videos, so a
# mismatch there means the video changed upstream, not a broken download.
#
# Requirements: curl, python3, ffmpeg/ffprobe, yt-dlp (youtube), and 7zz, 7z or
# uv (uvg: extracts the .7z archive, with `uvx py7zr` if no 7-Zip is installed).
#
# Environment variables:
#   DATASETS_DIR  where to build      (default: <repo>/datasets)
#   JOBS          parallel downloads  (default: 4)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASETS_DIR="${DATASETS_DIR:-$REPO_DIR/datasets}"
JOBS="${JOBS:-4}"
CHECKSUMS="$REPO_DIR/scripts/datasets.sha256"
ALL_TARGETS=(bbb youtube sequences sepe uvg images)

log() { printf '==> %s\n' "$*" >&2; }

# fetch URL DEST: downloads URL to DEST unless DEST already exists. The file is
# written to DEST.part first (resumed if present) and renamed when complete.
fetch() {
  local url="$1" dest="$2"
  [ -s "$dest" ] && return 0
  mkdir -p "$(dirname "$dest")"
  curl -sS --fail -L --retry 5 --retry-all-errors -C - -o "$dest.part" "$url"
  # Google Drive answers some errors (quota, permissions) with an HTML page.
  case "$(head -c 512 "$dest.part" | LC_ALL=C tr -d '\0' | LC_ALL=C tr '[:upper:]' '[:lower:]')" in
    *'<!doctype html'* | *'<html'*)
      echo "got an HTML page instead of a file: $url" >&2
      rm -f "$dest.part"
      return 1 ;;
  esac
  mv "$dest.part" "$dest"
}
export -f fetch

# fetch_all: reads "URL<NUL>DEST<NUL>" pairs on stdin and downloads them in parallel.
fetch_all() { xargs -0 -n 2 -P "$JOBS" "$BASH" -c 'fetch "$1" "$2"' _; }

# drive_list FOLDER_ID: prints "<relative path>\t<file id>" for every file in a
# public Google Drive folder and its subfolders, without logging in.
drive_list() {
  python3 -I - "$1" <<'EOF'
import html, re, sys, urllib.request

def walk(folder, prefix):
    url = f"https://drive.google.com/embeddedfolderview?id={folder}"
    page = urllib.request.urlopen(url).read().decode()
    entries = re.findall(r'<a href="https://drive\.google\.com/(drive/folders|file/d)/([\w-]+)[^"]*"[^>]*>'
                         r'.*?<div class="flip-entry-title">([^<]*)</div>', page, re.S)
    for kind, id_, name in entries:
        path = prefix + html.unescape(name)
        if kind == "drive/folders":
            walk(id_, path + "/")
        else:
            print(f"{path}\t{id_}")

walk(sys.argv[1], "")
EOF
}

# drive_fetch FOLDER_ID DEST_DIR [SED_SCRIPT]: downloads a Drive folder into
# DEST_DIR. SED_SCRIPT, if given, renames the relative paths first.
drive_fetch() {
  local folder="$1" dest="$2" rename="${3:-}" path id
  drive_list "$folder" | sed "$rename" | sort | while IFS=$'\t' read -r path id; do
    printf '%s\0%s\0' "https://drive.usercontent.google.com/download?id=$id&export=download&confirm=t" "$dest/$path"
  done | fetch_all
}

build_bbb() {
  log "Big Buck Bunny"
  local dir="$DATASETS_DIR/VideoChunks/BigBuckBunny" res codec size ext type
  # <resolution> <codec> <size in the file name>
  while read -r res codec size; do
    if [ "$codec" = vp9 ]; then ext=webm type=webm; else ext=mp4 type=mp4; fi
    printf '%s\0%s\0' \
      "https://test-videos.co.uk/vids/bigbuckbunny/$type/$codec/$res/Big_Buck_Bunny_${res}_10s_${size}.$ext" \
      "$dir/${res}p/Big_Buck_Bunny_${size}_${codec}.$ext"
  done <<'EOF' | fetch_all
360 av1 20MB
360 h264 30MB
360 h265 28MB
360 vp9 30MB
720 av1 30MB
720 h264 30MB
720 h265 30MB
720 vp9 30MB
1080 av1 30MB
1080 h264 30MB
1080 h265 30MB
1080 vp9 30MB
EOF
}

# youtube_fetch VIDEO_ID FORMAT DEST: downloads one stream as YouTube serves it
# (--fixup never: no remuxing, so the bytes are the original ones).
youtube_fetch() {
  local id="$1" format="$2" dest="$3"
  [ -s "$dest" ] && return 0
  mkdir -p "$(dirname "$dest")"
  yt-dlp -q --no-progress --fixup never -f "$format" -o "$dest" "https://www.youtube.com/watch?v=$id"
}

build_youtube() {
  log "YouTube"
  local dir="$DATASETS_DIR/VideoChunks/YouTube"
  # Formats: 701 = AV1 2160p60 HDR, 401 = AV1 2160p, 571 = AV1 4320p; video only.
  youtube_fetch zqpv2bySbr4 701 "$dir/AV1-4K/Earth_10_minutes.mp4"
  youtube_fetch m1jY2VLCRmY 401 "$dir/AV1-4K/Japan_in_4K.mp4"
  youtube_fetch m1jY2VLCRmY 571 "$dir/AV1-8K/Japan_in_8K.mp4"
}

# Splits the Earth video into ~200 MB files, as described in the README.
build_sequences() {
  log "Video Sequences"
  local src="$DATASETS_DIR/VideoChunks/YouTube/AV1-4K/Earth_10_minutes.mp4"
  local dir="$DATASETS_DIR/VideoSequences/AV1/4K" tmp cuts
  [ -s "$dir/Earth_000.mp4" ] && return 0
  [ -s "$src" ] || youtube_fetch zqpv2bySbr4 701 "$src"
  # Each cut is at the keyframe closest to the next 200 MB of video.
  cuts="$(ffprobe -v error -select_streams v:0 -show_entries packet=pts_time,size,flags -of csv=p=0 "$src" |
    awk -F, -v target=200000000 '
      $3 ~ /K/ { time[++n] = $1; bytes[n] = total }
      { total += $2 }
      END {
        next_cut = target
        for (i = 2; i <= n; i++) {
          if (bytes[i] < next_cut) continue
          k = (bytes[i] - next_cut < next_cut - bytes[i-1] || bytes[i-1] <= last) ? i : i - 1
          cuts = cuts sep time[k]; sep = ","
          last = bytes[k]; next_cut = last + target
        }
        print cuts
      }')"
  tmp="$(mktemp -d "$DATASETS_DIR/.sequences.XXXXXX")"
  ffmpeg -v error -i "$src" -c copy -f segment -segment_times "$cuts" -reset_timestamps 1 \
    -segment_format mp4 -segment_format_options movflags=+faststart "$tmp/Earth_%03d.mp4"
  mkdir -p "$dir"
  mv "$tmp"/Earth_*.mp4 "$dir/"
  rmdir "$tmp"
}

build_sepe() {
  log "SEPE 8K videos"
  # Drive folder of the encoded videos: "<res> Videos/<codec>/NNN_<codec>_<res>.mp4".
  # Renamed to the local folder names.
  drive_fetch 1EvDYl2AYktNyiXnseTfHFZziX5fjXQ3E "$DATASETS_DIR/VideoChunks/SEPE" '
    s|^480p Videos/|480p/|
    s|^720p Videos/|720p/|
    s|^1K Videos/|1K (2048x1080)/|
    s|^2K Videos/|2K (2731x1440)/|
    s|^4K Videos/|4K Videos (4096x2160)/|
    s|^8K Videos/|8K (8192x4320)/|'
}

build_uvg() {
  log "Ultra Video Group RiverBank (raw 4K)"
  local dir="$DATASETS_DIR/VideoChunks/UltraVideoGroup/4K-raw" archive tmp
  [ -s "$dir/raw.yuv" ] && return 0
  archive="$DATASETS_DIR/.RiverBank_3840x2160_50fps_420_8bit_YUV_RAW.7z"
  fetch https://ultravideo.fi/video/RiverBank_3840x2160_50fps_420_8bit_YUV_RAW.7z "$archive"
  tmp="$(mktemp -d "$DATASETS_DIR/.uvg.XXXXXX")"
  if command -v 7zz >/dev/null; then 7zz x -bd -o"$tmp" "$archive" >/dev/null
  elif command -v 7z >/dev/null; then 7z x -bd -o"$tmp" "$archive" >/dev/null
  else uvx --quiet --from py7zr py7zr x "$archive" "$tmp"
  fi
  mkdir -p "$dir"
  find "$tmp" -name '*.yuv' -exec mv {} "$dir/raw.yuv" \;
  rm -rf "$tmp" "$archive"
}

build_images() {
  log "SEPE 8K PNG frames"
  local seq id
  while read -r seq id; do
    drive_fetch "$id" "$DATASETS_DIR/ImageSequence/$seq"
  done <<'EOF'
001 1wX3zV3zAJcS7l59DykFUdKLYP6daCkDd
002 1J6EYEN6rjyiHzX5Hf9_wMHrQcRB5f1j3
003 1veJuOduzKV0UUrrx_Rk2zRwDhOp7UOHn
EOF
}

# verify TARGET: checks the files of one dataset against scripts/datasets.sha256.
verify() {
  local prefix
  case "$1" in
    bbb) prefix='VideoChunks/BigBuckBunny/' ;;
    youtube) prefix='VideoChunks/YouTube/' ;;
    sequences) prefix='VideoSequences/' ;;
    sepe) prefix='VideoChunks/SEPE/' ;;
    uvg) prefix='VideoChunks/UltraVideoGroup/' ;;
    images) prefix='ImageSequence/' ;;
  esac
  log "verifying $1"
  grep -F "  $prefix" "$CHECKSUMS" | (cd "$DATASETS_DIR" && shasum -a 256 -c --quiet -)
}

main() {
  local verify=0 targets=() t status=0
  for t in "$@"; do
    case "$t" in
      --verify) verify=1 ;;
      -h | --help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
      bbb | youtube | sequences | sepe | uvg | images) targets+=("$t") ;;
      *) echo "unknown dataset: $t (expected: ${ALL_TARGETS[*]})" >&2; exit 2 ;;
    esac
  done
  [ ${#targets[@]} -gt 0 ] || targets=("${ALL_TARGETS[@]}")
  mkdir -p "$DATASETS_DIR"
  for t in "${targets[@]}"; do "build_$t"; done
  if [ "$verify" = 1 ]; then
    for t in "${targets[@]}"; do verify "$t" || { echo "checksum mismatch in $t" >&2; status=1; }; done
  fi
  log "done: $DATASETS_DIR"
  exit "$status"
}

main "$@"
