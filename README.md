# Video POCs

Test videos and proofs of concept for video work: decoding, transcoding,
transport and codec comparisons across **AV1**, **H.264**, **H.265 (HEVC)**
and **VP9**, from 360p up to 8K.

## Repository layout

```
datasets/
├── 360p/          # Big Buck Bunny, 640x360   (tracked in git)
├── 1080p/         # Big Buck Bunny, 1920x1080 (tracked in git)
└── LargeFiles/    # 480p–8K clips, raw YUV    (git-ignored, download locally)
poc1/              # Rust Arrow Flight client/server for chunked video upload (mosaicod-compatible)
```

## Proofs of concept

| POC | Description |
|---|---|
| [poc1](poc1/README.md) | Splits each video into chunks and uploads it to a server with its own Arrow Flight `DoPut` call, following mosaicod's upload protocol and storage (one Parquet file per record batch, with a proposed `video` serialization format). Checks that the server received all the data, how long the upload took and how the stored Parquet files compare in size with the video. Results: [poc1/results/RESULTS.md](poc1/results/RESULTS.md). |

## Datasets

### Big Buck Bunny (tracked)

10-second clips at 30 fps, 8-bit `yuv420p`, sourced from
[test-videos.co.uk](https://test-videos.co.uk/). The size in each file name
(e.g. `30MB`) is the target size of the encode.

| Resolution | AV1 | H.264 | H.265 | VP9 |
|---|---|---|---|---|
| 360p (640x360) | `Big_Buck_Bunny_20MB_av1.mp4` | `Big_Buck_Bunny_30MB_h264.mp4` | `Big_Buck_Bunny_28MB_h265.mp4` | `Big_Buck_Bunny_30MB_vp9.webm` |
| 1080p (1920x1080) | `Big_Buck_Bunny_30MB_av1.mp4` | `Big_Buck_Bunny_30MB_h264.mp4` | `Big_Buck_Bunny_30MB_h265.mp4` | `Big_Buck_Bunny_30MB_vp9.webm` |

test-videos.co.uk also provides 720p versions, which are not included here.

Lossless sources of Big Buck Bunny:

- https://sciop.net/datasets/blender-bbb
- https://archive.org/download/AV-LOSSLESS-TEST-MATERIAL/1920x1080/

### Large files (not tracked)

`datasets/LargeFiles/` is listed in `.gitignore` because it is about 35 GiB.
Download it separately and place it at that path.

**SEPE 8K dataset** from
[talshoura/SEPE-8K-Dataset](https://github.com/talshoura/SEPE-8K-Dataset#bitrates-used-for-each-video-codec-and-resolution-combination):
40 sequences (`001`–`040`), each 10 seconds at 29.97 fps, 8-bit `yuv420p`.
Every sequence is encoded in AV1, H.264 and HEVC at six resolutions:

| Folder | Resolution | File pattern | Folder size (all 120 videos) |
|---|---|---|---|
| `480p/{AV1,H264,HEVC}/` | 910x480 | `NNN_<codec>_480p.mp4` | ~500 MiB |
| `720p/{AV1,H264,HEVC}/` | 1365x720 (HEVC: 1366x720) | `NNN_<codec>_720p.mp4` | ~750 MiB |
| `1K (2048x1080)/{AV1,H264,HEVC}/` | 2048x1080 | `NNN_<codec>_1K.mp4` | ~1.1 GiB |
| `2K (2731x1440)/{AV1,H264,HEVC}/` | 2731x1440 (HEVC: 2732x1440) | `NNN_<codec>_2K.mp4` | ~2.3 GiB |
| `4K Videos (4096x2160)/{AV1,H264,HEVC}/` | 4096x2160 | `NNN_<codec>_4K.mp4` | ~5.1 GiB |
| `8K (8192x4320)/{AV1,H264,HEVC}/` | 8192x4320 | `NNN_<codec>_8K.mp4` | ~18 GiB |

`<codec>` is one of `av1`, `h264` or `hevc`. The HEVC encodes round the odd
720p and 2K widths up to an even number.

**4K raw**: `4K-Raw/raw.yuv` (~7 GiB) is an uncompressed 4K YUV sequence from
the [Ultra Video Group dataset](https://ultravideo.fi/dataset.html). A `.yuv`
file has no header, so tools need to be told its geometry, pixel format and
frame rate, for example:

```sh
ffplay -f rawvideo -video_size 3840x2160 -pixel_format yuv420p -framerate 50 datasets/LargeFiles/4K-Raw/raw.yuv
```

Check these values against the sequence's page on the Ultra Video Group site.

### Other sources

- [Xiph.org media](https://media.xiph.org/): uncompressed `.y4m` and `.yuv`
  sequences (raw frames, no codec).
- [Ultra Video Group](https://ultravideo.fi/dataset.html): 4K sequences as raw
  YUV and H.265.

## Inspecting a file

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt \
  datasets/1080p/Big_Buck_Bunny_30MB_av1.mp4
```
