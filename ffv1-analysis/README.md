# ffv1-analysis

Takes 8K frames stored one per lossless FFV1 file and converts each into
**raw**, **TIFF**, **AVIF**, **PNG** and **JPEG**, each losing as little as
the format allows, then compares every format with FFV1: the size of the
images, the time to convert an image from FFV1 (reading the FFV1 frame, then
writing the image), the time to read it back, one image at a time and 4 at a
time, and what it loses against the frame (plane by plane, then with
butteraugli). Results, with charts: [results/README.md](results/README.md).

## The input

60 frames of 8192x4320, one FFV1 `.mkv` file each (~292 MB), built here from
the SEPE 8K PNGs of `datasets/ImageSequence/001/`, in three steps. The PNGs
and the AV1 video are only the way to the FFV1 frames: nothing of them is
measured.

1. **Capture times**: [`scripts/build-timestamps.py`](scripts/build-timestamps.py)
   gives each PNG the time an ideal 29.97 fps camera (exactly 30000/1001)
   captures it, and writes [`datasets/regular/`](datasets/regular/README.md):
   `batch_001/` holds the first image, `batch_002/` the first two, and so on
   up to `batch_300/`, each a `timestamps.csv` with one `image,timestamp_ns`
   row per image. The analysis uses `batch_060/`, the first 60 images.
2. **AV1 video**: `prepare` encodes the batch into
   `av1/regular/batch_060.mkv`: each PNG converted to 8-bit YUV
   4:2:0 (BT.709, limited range, the U and V samples averaged over 2x2
   pixels), then encoded with SVT-AV1, preset 5, CRF 18, at a constant
   30000/1001 frame rate.
3. **FFV1 frames**: `prepare` decodes the video with dav1d and writes every
   frame as a lossless FFV1 image, `ffv1/regular/batch_060/frame_<k>.mkv`,
   each read back and checked against its frame.

So the frames are exactly what the AV1 decoder gives: **8-bit YUV 4:2:0,
BT.709, limited range**. The video, and so its 60 frames, are the same,
packet for packet, as the one image-sequence's server writes for
`regular/batch_060` at preset 5, CRF 18.

`prepare` waits 5 s before sending SVT-AV1 the last frame. Without the
pause, the PNGs come faster than the encoder works through them, and the
last frame's packet changes from one run to the next: of 9 runs, 4 gave a
different one (149,508 bytes instead of 152,684), the other 59 packets
always the same. With the encoder caught up, as when the frames come at the
pace of an upload, it is always the server's: 12 of 12 runs with a pause of
1 to 8 s before the last frame. Why SVT-AV1 depends on the timing is not
known; the pause is what makes the video the same every time.

## The formats

FFV1 (version 3, 16 slices, in Matroska) is the reference: every other
format is written from its frames and compared with it. The frame is YUV
4:2:0. Formats that can hold it as it is give it back exactly, like FFV1; the
others have to convert it, and lose as little as they can:

| Format | How the frame is written | Lossless? |
|---|---|---|
| raw | The Y, U and V planes as they are, no header | yes |
| TIFF | YCbCr with 4:2:0 subsampling, as FFmpeg's encoder writes `yuv420p`, Deflate compressed | yes |
| AVIF | SVT-AV1 in lossless, still-picture mode, in FFmpeg's AVIF muxer, read back with dav1d | yes |
| PNG | 16-bit RGB: PNG holds no YUV. 16 bits keep the rounding negligible, and each chroma value is repeated over its 2x2 block so the way back recovers it | almost: only the colors outside the RGB range are clipped |
| JPEG | Quality 100 in 4:2:0, the planes passed to the encoder as they are, after converting them to JFIF's color space (BT.601, full range) so any viewer shows the right colors | no: the color conversion's rounding and the DCT lose a little |

- **TIFF**: every FFmpeg TIFF compression (none, PackBits, LZW, Deflate) gave
  the frame back exactly on a test frame; Deflate was the smallest (11.8 MB,
  against 53.1 MB with none, 46.8 MB with PackBits, 27.8 MB with LZW), so it
  is the one measured. FFmpeg tags its YCbCr as BT.601 (the TIFF standard's
  default) with black at 15: the samples are exact, but a reader that
  converts the file to RGB by its tags shows slightly wrong colors.
- **JPEG**: lossless JPEG exists, but almost nothing reads it, so the
  standard baseline JPEG is used, at its best quality.

## What is measured

The analysis first reads every FFV1 frame. Then, for each frame, and each
format in turn, FFV1 too (written again, to time its own writing), one image
at a time:

- **Size**: the file written, against the FFV1 frame's.
- **Encoding**: writing the image from the frame, file included.
- **Converting from FFV1**: reading the FFV1 frame, then writing the image.
- **Decoding**: reading the file back into a frame.
- **Loss**: the exact formats must give the frame back byte for byte, or the
  analysis stops. For PNG and JPEG it measures, plane by plane, the RMS
  error, the largest error and how many samples changed; then butteraugli,
  in linear RGB: max-norm (the most visible difference anywhere) and 3-norm
  (the differences over the whole image). Under 1, a difference is
  generally not visible; over 2, it usually is.

Then each format's whole sequence again, 4 images at a time: one image
leaves most cores idle.

The files are written to and read from the OS cache, so the times are the
formats' own work, not the disk's.

## What it shows

From the 60 frames of `regular/batch_060` ([results/README.md](results/README.md),
with every chart and each format's own README), against FFV1's 4.9 MiB per
image, 45 ms to read and 61 ms to write:

- **No format is smaller than FFV1**: AVIF, the closest, is 1.3x its size,
  JPEG 1.8x, TIFF 2.9x, PNG 5.5x and raw 10.4x.
- **Converting an image from FFV1** takes 49 ms to raw (nearly all of it
  reading the FFV1 frame), 322 ms to JPEG, 595 ms to AVIF, 1,043 ms to TIFF
  and 2,955 ms to PNG. 4 at a time, TIFF and PNG write the whole sequence
  about 3x faster.
- **Only raw reads back faster than FFV1** (3 ms); TIFF takes 71 ms, JPEG
  124 ms, PNG 246 ms and AVIF 539 ms.
- **Raw, TIFF and AVIF are exact**, like FFV1. PNG loses 0.00068 levels
  (RMS), butteraugli max-norm 0.19, far from visible; JPEG loses 0.240
  levels, butteraugli max-norm 2.32, the level where a difference starts to
  be visible somewhere in an 8K image.
- **Raw is over the 50 MiB a message to mosaicod may carry** (50.6 MiB per
  image).

## Running

Needs Rust, FFmpeg 9.0 with the `libsvtav1` encoder and the `libdav1d`
decoder, `pkg-config` to find FFmpeg's libraries, [`uv`](https://docs.astral.sh/uv/) for the
charts, and the PNGs
(`scripts/build-datasets.sh images` from the repository root). From
`ffv1-analysis/`:

```sh
scripts/build-timestamps.py                     # datasets/regular/ (already in the repository)
cargo run --release --bin prepare               # the AV1 video and its FFV1 frames: ~1 minute, ~10 GiB of memory
cargo run --release                             # images.json and images-quality.json: ~20 minutes, ~9 GiB of memory for butteraugli
uv run scripts/image_formats.py                 # the charts and the READMEs of results/
```

`prepare` takes another batch with `--timestamps
datasets/regular/batch_<N>/timestamps.csv` (and `--preset`, `--crf`), and
the analysis takes it with `--input ffv1/regular/batch_<N>`. The capture times must fit a constant frame
rate, as `regular`'s do.

They write:

```text
av1/regular/batch_060.mkv                  the AV1 video (git-ignored)
ffv1/regular/batch_060/frame_<k>.mkv       its frames, one FFV1 file each (git-ignored)
output/<format>/frame_<k>.<ext>            the images (git-ignored)
results/images.json                        every image's size, times and loss
results/images-quality.json                butteraugli of every PNG and JPEG image
results/README.md and the rest of results/ the charts and READMEs, from scripts/image_formats.py
```
