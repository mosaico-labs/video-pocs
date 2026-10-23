# Image sequence timestamps

Capture times for the images of `datasets/ImageSequence/001/` (300 PNG
frames, 8192x4320), as a camera would record them.

The sequence was shot at 29.97 fps (exactly 30000/1001), so an ideal camera
captures frame *i* at *i* × 1001/30000 s after the first one, every
33.4 ms. `regular/` is that camera; every other scenario is one way a real
camera deviates from it, so that a constant frame rate no longer fits the
images. The last column is what the server makes of the whole scenario:

| Scenario | Images | What happens | Video |
|---|---|---|---|
| [`regular/`](regular/README.md) | 300 | Every frame, ideal timing | constant frame rate, 30000/1001 |
| [`frame-drops/`](frame-drops/README.md) | 283 | Some frames are never delivered | variable frame rate |
| [`jitter/`](jitter/README.md) | 300 | Every frame, each time off by a random amount | variable frame rate |
| [`duplicate-timestamps/`](duplicate-timestamps/README.md) | 300 | Some frames share a capture time | rejected |
| [`out-of-order/`](out-of-order/README.md) | 300 | Some times go backwards | rejected |
| [`rate-change/`](rate-change/README.md) | 210 | The frame rate switches, with jitter | variable frame rate |
| [`sparse/`](sparse/README.md) | 20 | A few frames far apart, with uneven gaps | variable frame rate |

`duplicate-timestamps/` and `out-of-order/` can't become a video at any
frame rate: two images at one time, or a time going back, have no place in
a video that shows each frame at its capture time. They test the server's
input checks instead (see their READMEs).

Each scenario folder has a README and one `batch_NNN/` folder per batch
size: `batch_001/` holds the first image, `batch_002/` the first two, and so
on up to all the images. Each batch has a `timestamps.csv` with one
`image,timestamp_ns` row per image.

## Rebuilding

The CSV files are in the repository. Rebuilding every scenario takes a few
seconds and needs the 300 frames of `datasets/ImageSequence/001/`
(`scripts/build-datasets.sh images` downloads them):

```sh
image-sequence/scripts/build-timestamps.py
```

Every scenario is deterministic, so every run writes the same files. The
deviations (which frames drop, how much jitter, ...) are constants at the
top of the script.
