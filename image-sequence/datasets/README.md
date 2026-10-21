# Image sequence timestamps

Datasets that associate each image of `datasets/ImageSequence/001/` (300 PNG
frames, 8192x4320) with a capture time, as a camera would record it.

The sequence was shot at 29.97 fps (exactly 30000/1001), so an ideal camera
captures frame *i* at *i* × 1001/30000 s after the first frame, one frame
every 33.4 ms. `regular/` is that ideal camera. The other folders each
describe one way a real camera deviates from it, and why the images can then
no longer become a constant-frame-rate video without losing timing
information.

| Scenario | Images | What happens |
|---|---|---|
| [`regular/`](regular/README.md) | 300 | Every frame, ideal timing |
| [`frame-drops/`](frame-drops/README.md) | 283 | Some frames are never delivered |
| [`jitter/`](jitter/README.md) | 300 | Every frame, each time off by a random amount |
| [`duplicate-timestamps/`](duplicate-timestamps/README.md) | 300 | Some frames share a capture time |
| [`out-of-order/`](out-of-order/README.md) | 300 | Some times go backwards |

Each scenario folder has its own README, and one `batch_NNN/` folder per
batch size: `batch_001/` holds the first image, `batch_002/` the first two,
and so on up to all the images of the scenario. Each batch has a
`timestamps.csv` with one `image,timestamp_ns` row per image.

## Rebuilding

The CSV files are in the repository. To rebuild every scenario (it takes a
few seconds, and needs all 300 frames of `datasets/ImageSequence/001/`,
downloaded by `scripts/build-datasets.sh images`):

```sh
image-sequence/scripts/build-timestamps.py
```

Every scenario is deterministic, so the files are the same on every run. The
deviations (which frames drop, how much jitter, ...) are constants at the top
of the script.
