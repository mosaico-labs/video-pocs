# Video POCs

Proofs of concept for managing videos inside
[mosaicod](https://github.com/mosaico-labs/mosaico): how videos should be
serialized and stored in mosaicod, and how they are decoded and transcoded.
The videos are in **AV1**, **H.264**, **H.265 (HEVC)** and **VP9**, from 360p
up to 8K.

To inspect a video and extract its information run:

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt \
  datasets/VideoToChunks/BigBuckBunny/1080p/Big_Buck_Bunny_30MB_av1.mp4
```

- `-v error`: print only errors, not ffprobe's banner and log. **Optional**: it
  only keeps the output clean.
- `-select_streams v:0`: look only at the first video stream. A file can also
  hold audio or subtitle streams. **Removable** if the file has only one video stream.
- `-show_entries stream=...`: print only these fields. Without it, ffprobe
  prints no fields at all (`-v error` also hides its summary), so **mandatory**.
  - `codec_name`: the compression format (`av1`, `h264`, `hevc`, `vp9`).
  - `width`, `height`: frame size in pixels.
  - `r_frame_rate`: frames per second, as a fraction (`30/1`, `60000/1001` = 59.94).
  - `pix_fmt`: how each pixel's colors are stored, e.g. `yuv420p` (8-bit) or
    `yuv420p10le` (10-bit).

## Datasets

The data is organized by how a video is handed to mosaicod:

| Dataset | What it is |
|---|---|
| [Image Sequence](#image-sequence) | Temporally ordered images or frames |
| [Video Sequences](#video-sequences) | Ordered, self-contained video files |
| [Video To Chunks](#video-to-chunks) | One video file, cut into temporal chunks |

Git-ignored folders are too large for the repository: download them separately
from the provided links and place them at the same path.

### Image Sequence

> Temporally ordered images or frames.

A sequence of PNG images, all with the same resolution and pixel format.

The frames come from the
[SEPE 8K dataset](https://github.com/talshoura/SEPE-8K-Dataset), which
publishes every sequence as lossless PNG frames in addition to the encoded
videos (see [Video To Chunks](#sepe-8k-not-tracked)).

| Folder | Frames | Resolution | Pixel format | Size | Git |
|---|---|---|---|---|---|
| `datasets/ImageSequence/001/` | 300 (`000_8K.png`–`299_8K.png`) | 8192x4320 | `rgba` | ~9.3 GiB | first 50 frames (`000`–`049`, ~1.7 GB) tracked, the rest ignored |
| `datasets/ImageSequence/002/` | 300 (`000_8K.png`–`299_8K.png`) | 8192x4320 | `rgba` | ~5.9 GiB | ignored |
| `datasets/ImageSequence/003/` | 300 (`000_8K.png`–`299_8K.png`) | 8192x4320 | `rgba` | ~7.0 GiB | ignored |

Each folder is one SEPE sequence, one PNG per frame. The sequence was shot at
29.97 frames per second (exactly 30000/1001), so its 300 frames are 10 seconds
of video. A PNG file stores only a picture, not when it is shown, so the frame
rate has to be given again to play the frames as a video, for example:

```sh
ffplay -framerate 30000/1001 -i datasets/ImageSequence/001/%03d_8K.png
```

`%03d` matches the 3-digit frame number in the file names.

### Video Sequences

> Ordered video content or sequences.

A series of self-contained video files, of any length. Each video is decodable
on its own, and the videos are coherent with each other, so they can be joined
into one video without re-encoding.

**`datasets/VideoSequences/AV1/4K/Earth_000.mp4`–`Earth_010.mp4`** (not tracked, too large for GitHub; rebuild them with the commands below):
the YouTube video `Earth_10_minutes.mp4` from
[Video To Chunks](#youtube-not-tracked) (AV1, 3840x2160, 59.94 fps, 10-bit
`yuv420p10le`, HDR PQ, 614.73 s, 2.13 GB, no audio), split into 11 files of
about 200 MB each.

How they were built:

1. The cut points were chosen on file size, not on duration: for every 200 MB
   of video, the keyframe closest to that boundary. So each file starts on a
   keyframe and decodes on its own. Keyframes are up to 7 s apart (~24 MB),
   so the files are 186–223 MB (33–68 s); the last one holds the remaining
   114 MB.
2. The video was split with ffmpeg's segment muxer, copying the stream
   (`-c copy`), with no **transcoding**:

   ```sh
   ffmpeg -i Earth_10_minutes.mp4 -c copy -f segment \
     -segment_times 63.496767,126.960167,183.183000,239.939700,295.461833,354.520833,410.309900,464.997867,524.056867,582.148233 \
     -reset_timestamps 1 \
     -segment_format mp4 -segment_format_options movflags=+faststart \
     Earth_%03d.mp4
   ```

   A video is stored as compressed frames. Most frames only store what changed
   since earlier frames. A **keyframe** is a complete picture, so decoding can
   start only there. **Transcoding** means decoding and compressing again: it
   is slow and loses quality. Copying keeps the compressed frames as they are.

   - `-i Earth_10_minutes.mp4`: the input file.
   - `-c copy`: copy the compressed frames instead of transcoding. **Required**.
   - `-f segment`: write the output as several files instead of one. **Required**.
   - `-segment_times`: where to cut, in seconds. ffmpeg cuts at the first
     keyframe at or after each time. These times are exactly the chosen
     keyframes (see below). **Required**.
   - `-reset_timestamps 1`: each file's clock starts at 0, so each one plays as
     a normal video on its own. **Required for independent files**.
   - `-segment_format mp4`: the file format (container) of each piece. **Optional**:
     ffmpeg guesses it from the `.mp4` extension.
   - `-segment_format_options movflags=+faststart`: an MP4 file has an index
     that says where each frame is in the file, and a player must read it
     before showing anything. ffmpeg writes the file from start to end, one
     frame after the other, and learns each frame's position only once it has
     written it. So by default it writes the index last, at the end of the
     file. With `faststart`, once everything is written, ffmpeg does one more
     pass that moves the index to the beginning of the file. A player reading
     the file over a network can then start right away instead of first
     fetching the end. **Optional**: it does not change the frames.
   - `Earth_%03d.mp4`: output names. `%03d` becomes the 3-digit file number,
     starting from `000`.

   The cut times come from the video itself:

   1. List every frame of the video, in file order, with its time, its size
      in bytes and whether it is a keyframe.
   2. Walk through the frames, adding up their sizes. This gives, for each
      keyframe, its position in the file (the bytes before it).
   3. The first cut should be at 200 MB (200,000,000 bytes). A cut can only
      be at a keyframe, so pick the keyframe whose position is closest to
      200 MB: the last one before it or the first one after it.
   4. The next cut should be 200 MB after the chosen keyframe. Pick the
      closest keyframe again, and repeat until the end of the video.

   In pseudocode:

   ```
   keyframes = [(time, position) of every keyframe]   # position = bytes before it
   target = 200 MB
   last_cut = 0
   for each keyframe K, from the second one:
       if position(K) < target: continue                # target not reached yet
       P = the keyframe before K
       if P is at least as close to target as K, and P is after last_cut:
           cut = P
       else:
           cut = K
       add time(cut) to the cut list
       last_cut = position(cut)
       target = last_cut + 200 MB
   ```

   The commands that ran it: `ffprobe` for step 1, `awk` for steps 2–4.

   <details>
   <summary>Commands</summary>

   ```sh
   ffprobe -v error -select_streams v:0 -show_entries packet=pts_time,size,flags \
     -of csv=p=0 Earth_10_minutes.mp4 > frames.csv   # one "time,bytes,flags" line per frame; K = keyframe
   awk -F, -v target=200000000 '
     $3 ~ /K/ { time[++n] = $1; bytes[n] = total }   # each keyframe: its time, and the bytes before it
     { total += $2 }
     END {
       next_cut = target
       for (i = 2; i <= n; i++) {
         if (bytes[i] < next_cut) continue
         # this keyframe or the previous one, whichever is closer to next_cut
         k = (bytes[i] - next_cut < next_cut - bytes[i-1] || bytes[i-1] <= last) ? i : i - 1
         cuts = cuts sep time[k]; sep = ","
         last = bytes[k]; next_cut = last + target
       }
       print cuts
     }' frames.csv
   ```

   </details>

To join them back into one video, also without re-encoding:

```sh
cd datasets/VideoSequences/AV1/4K
for f in Earth_*.mp4; do echo "file '$f'"; done > list.txt
ffmpeg -f concat -i list.txt -c copy Earth_rebuilt.mp4
```

- The `for` loop writes `list.txt`, one `file 'Earth_NNN.mp4'` line per piece,
  in order.
- `-f concat -i list.txt`: read the files listed in `list.txt` as one
  continuous input. **Required**.
- `-c copy`: copy the compressed frames instead of transcoding. **Required**.
- `Earth_rebuilt.mp4`: the output file.

The joined video shows exactly the same pictures as the source: same 36,847
frames, same duration, and decoding both gives identical frames. The file is
still not byte-for-byte identical, for one reason.
In AV1, each frame's data is made of small blocks (OBUs).
The source file starts every frame with a 2-byte block, the *temporal delimiter*,
that only marks where a new frame
begins. Inside an MP4 file the file's index already marks that, so the MP4
rules say to leave this block out. ffmpeg follows them when it writes the
pieces, so every frame of the pieces is 2 bytes smaller.
Removing the same blocks from the source
(`ffmpeg ... -c copy -bsf:v filter_units=remove_types=2`)
gives exactly the same data as the joined video.

### Video To Chunks

> A video represented as temporal chunks.

One logical video file, cut into temporal byte chunks to be stored in
mosaicod. The temporal byte chunks are valid videos inside the storage.

#### Big Buck Bunny (tracked)

10-second clips at 30 fps, 8-bit `yuv420p`, in
`datasets/VideoToChunks/BigBuckBunny/<resolution>/`, downloaded from
[test-videos.co.uk](https://test-videos.co.uk/). The size in each file name
(e.g. `30MB`) is the target size of the encode.

| Resolution | AV1 | H.264 | H.265 | VP9 |
|---|---|---|---|---|
| 360p (640x360) | `Big_Buck_Bunny_20MB_av1.mp4` | `Big_Buck_Bunny_30MB_h264.mp4` | `Big_Buck_Bunny_28MB_h265.mp4` | `Big_Buck_Bunny_30MB_vp9.webm` |
| 720p (1280x720) | `Big_Buck_Bunny_30MB_av1.mp4` | `Big_Buck_Bunny_30MB_h264.mp4` | `Big_Buck_Bunny_30MB_h265.mp4` | `Big_Buck_Bunny_30MB_vp9.webm` |
| 1080p (1920x1080) | `Big_Buck_Bunny_30MB_av1.mp4` | `Big_Buck_Bunny_30MB_h264.mp4` | `Big_Buck_Bunny_30MB_h265.mp4` | `Big_Buck_Bunny_30MB_vp9.webm` |

Lossless sources of Big Buck Bunny:

- https://sciop.net/datasets/blender-bbb
- https://archive.org/download/AV-LOSSLESS-TEST-MATERIAL/1920x1080/

#### YouTube (not tracked)

AV1 videos downloaded from YouTube, video stream only (no audio), in
`datasets/VideoToChunks/YouTube/AV1-<resolution>/`:

| File | Source | Resolution | Frame rate | Pixel format | Duration | Size |
|---|---|---|---|---|---|---|
| `AV1-4K/Earth_10_minutes.mp4` | [Earth in 10 Minutes 4K HDR](https://www.youtube.com/watch?v=zqpv2bySbr4) | 3840x2160 | 59.94 fps | `yuv420p10le`, HDR PQ | 614.73 s | 2.13 GB |
| `AV1-4K/Japan_in_4K.mp4` | [Japan in 8K](https://www.youtube.com/watch?v=m1jY2VLCRmY) | 3840x2160 | 23.976 fps | `yuv420p`, BT.709 | 141.10 s | 185 MB |
| `AV1-8K/Japan_in_8K.mp4` | [Japan in 8K](https://www.youtube.com/watch?v=m1jY2VLCRmY) | 7680x4320 | 23.976 fps | `yuv420p`, BT.709 | 141.10 s | 447 MB |

The two Japan files are the same YouTube video, downloaded at two
resolutions. The folder is git-ignored because its files are larger than
GitHub accepts. The Earth video is also split into ~200 MB files in
[Video Sequences](#video-sequences).

#### SEPE 8K (not tracked)

`datasets/VideoToChunks/SEPE/` (~28 GiB) is the
[SEPE 8K dataset](https://github.com/talshoura/SEPE-8K-Dataset#bitrates-used-for-each-video-codec-and-resolution-combination):
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

#### Ultra Video Group 4K raw (not tracked)

`datasets/VideoToChunks/UltraVideoGroup/4K-raw/raw.yuv` (~7 GiB) is an
uncompressed 4K YUV sequence from the
[Ultra Video Group dataset](https://ultravideo.fi/dataset.html). A `.yuv` file
is only the pixels, one frame after the other, with no header. So a tool has
to be told the frame size, the pixel format and the frame rate. The right
values are on the sequence's page on the Ultra Video Group site, and two can
be checked on the file itself:

- **Frame size and pixel format**: decode one frame with the chosen values. If
  they are wrong, the picture is garbage (wrong colors, slanted or repeated
  stripes). Here 3840x2160 `yuv420p` (8-bit) gives a correct picture, while
  `yuv420p10le` (10-bit) does not.
- **Frame count**: the file size divided by the size of one frame. A 3840x2160
  `yuv420p` frame is 3840 × 2160 × 1.5 = 12,441,600 bytes, and the file is
  7,464,960,000 bytes, so it holds exactly 600 frames.

  The 1.5 is the bytes per pixel of `yuv420p`. It stores a picture as three
  planes, each value taking 1 byte (8-bit):
  - **Y** (brightness): one value per pixel, so 3840 × 2160 values.
  - **U** and **V** (color): the eye notices color detail less than brightness
    detail, so color is stored at half the width and half the height (that is
    what `420` means). Each is 1920 × 1080, a quarter of the pixels.

  Per pixel that is 1 + ¼ + ¼ = 1.5 bytes. A 10-bit format (`yuv420p10le`)
  uses 2 bytes per value, so 3 bytes per pixel.

The frame rate cannot be read from the file; take it from the sequence's
page. With 50 fps, for example:

```sh
ffplay -f rawvideo -video_size 3840x2160 -pixel_format yuv420p -framerate 50 \
  datasets/VideoToChunks/UltraVideoGroup/4K-raw/raw.yuv
```

#### Other sources (not used)

Other test videos that could be added:

- [Xiph.org media](https://media.xiph.org/): uncompressed `.y4m` and `.yuv`
  sequences (raw frames, not compressed).
- [Ultra Video Group](https://ultravideo.fi/dataset.html): the same 4K
  sequences as `raw.yuv`, also published already compressed in H.265.

## Proofs of concept

| POC | Description |
|---|---|
| [video-to-parquet](video-to-parquet/README.md) | Splits each video into chunks and uploads it to a server with its own Arrow Flight `DoPut` call, following mosaicod's upload protocol and storage (one Parquet file per record batch, with a proposed `video` serialization format). Checks that the server received all the data, how long the upload took and how the stored Parquet files compare in size with the video. Results: [video-to-parquet/results/RESULTS.md](video-to-parquet/results/RESULTS.md). |
