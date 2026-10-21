#!/usr/bin/env python3
"""Associates each image of datasets/ImageSequence/001 with a capture time.

The sequence was shot at 30000/1001 fps (29.97), so an ideal camera captures
frame i at START_NS + i * 1001/30000 s. Each scenario starts from that ideal
timing and describes one way a real camera can deviate from it:

    regular               every frame, ideal timing
    frame-drops           some frames are never delivered (their PNG is left out)
    jitter                every frame, each time off by a random amount
    duplicate-timestamps  some frames get the same time as the previous frame
    out-of-order          some times go backwards (swapped pair, clock step back)
    rate-change           the frame rate switches between 29.97 and 14.985 fps, with jitter

Writes one folder per scenario and batch size under image-sequence/datasets/:
<scenario>/batch_001 holds the first image, batch_002 the first two, ... up to
all the images of the scenario. Each folder has a timestamps.csv with one row
per image:

    image,timestamp_ns
    datasets/ImageSequence/001/000_8K.png,1767225600000000000

- image: the PNG's path, relative to the repository root.
- timestamp_ns: the capture time, in nanoseconds since the Unix epoch.

Every scenario is deterministic, so running the script again gives the same
files. Run it from anywhere: image-sequence/scripts/build-timestamps.py
"""

import csv
import random
import shutil
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
IMAGES_DIR = REPO_DIR / "datasets" / "ImageSequence" / "001"
OUT_DIR = REPO_DIR / "image-sequence" / "datasets"

START_NS = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z
FPS_NUM, FPS_DEN = 30000, 1001
# Frames of datasets/ImageSequence/001, for the worked numbers in the READMEs
SEQUENCE_FRAMES = 300

# frame-drops: two single drops and a 0.5 s burst (15 frames)
DROPPED = {50, 120, *range(200, 215)}
# jitter: each time moves by up to this much, either way (frames are ~33.4 ms apart)
JITTER_NS = 3_000_000
JITTER_SEED = 1
# duplicate-timestamps: these frames get the previous frame's time
DUPLICATED = {75, 150, 151, 225}
# out-of-order: frames 90 and 91 swap times; from frame 180 the clock is 0.5 s behind
SWAPPED = (90, 91)
CLOCK_STEP_FRAME = 180
CLOCK_STEP_NS = -500_000_000
# rate-change: runs of this many images, alternately every source frame
# (29.97 fps) and every RATE_CHANGE_STEP-th one (14.985 fps), with jitter
RATE_CHANGE_RUN = 30
RATE_CHANGE_STEP = 2


def ideal_ns(index: int) -> int:
    # index * FPS_DEN / FPS_NUM seconds, rounded to the nearest nanosecond
    return START_NS + (index * FPS_DEN * 10**9 + FPS_NUM // 2) // FPS_NUM


def regular(images):
    return [(img, ideal_ns(i)) for i, img in enumerate(images)]


def frame_drops(images):
    return [(img, ideal_ns(i)) for i, img in enumerate(images) if i not in DROPPED]


def jitter(images):
    rng = random.Random(JITTER_SEED)
    return [(img, ideal_ns(i) + rng.randint(-JITTER_NS, JITTER_NS)) for i, img in enumerate(images)]


def duplicate_timestamps(images):
    rows = regular(images)
    for i in sorted(DUPLICATED):
        rows[i] = (rows[i][0], rows[i - 1][1])
    return rows


def out_of_order(images):
    rows = [(img, t + (CLOCK_STEP_NS if i >= CLOCK_STEP_FRAME else 0)) for i, (img, t) in enumerate(regular(images))]
    a, b = SWAPPED
    rows[a], rows[b] = (rows[a][0], rows[b][1]), (rows[b][0], rows[a][1])
    return rows


def rate_change_runs(frames: int):
    """The runs of rate-change over a sequence of `frames` frames, as (step,
    frame indices): alternately every frame and every RATE_CHANGE_STEP-th."""
    runs, start = [], 0
    while start < frames:
        step = 1 if len(runs) % 2 == 0 else RATE_CHANGE_STEP
        indices = list(range(start, frames, step))[:RATE_CHANGE_RUN]
        runs.append((step, indices))
        start = indices[-1] + step
    return runs


def rate_change(images):
    rng = random.Random(JITTER_SEED)
    return [(images[i], ideal_ns(i) + rng.randint(-JITTER_NS, JITTER_NS))
            for _, indices in rate_change_runs(len(images)) for i in indices]


def rate_change_table(frames: int) -> str:
    """How the runs of rate-change use the frames of the sequence, as a
    Markdown table."""
    runs = rate_change_runs(frames)
    rows = []
    for n, (step, indices) in enumerate(runs, 1):
        last = min(indices[-1] + step, frames) - 1
        rate = f"{FPS_NUM / FPS_DEN / step:.3f}".rstrip("0")
        rows.append(f"| {n} | {rate} fps | {indices[0]}–{last} | {len(indices)} | {last + 1 - indices[0] - len(indices)} |")
    images = sum(len(indices) for _, indices in runs)
    return "\n".join([
        "| Run | Rate | Frames of the sequence | Images | Frames skipped |",
        "|---|---|---|---|---|",
        *rows,
        f"| **Total** | | **{frames}** | **{images}** | **{frames - images}** |",
    ])


def ms(ns: int) -> str:
    return f"{ns / 1e6:.1f} ms"


FRAME_MS = ms(ideal_ns(1) - START_NS)
BURST = sorted(DROPPED)[2:]
RATE_CHANGE_IMAGES = sum(len(indices) for _, indices in rate_change_runs(SEQUENCE_FRAMES))

# Per scenario: how its times are built, and what its README says about it
SCENARIOS = {
    "regular": (regular, "Every frame, ideal timing", f"""\
The ideal camera: every frame is delivered, and frame *i* is captured exactly
*i* × 1001/30000 s after the first one, so frames are always {FRAME_MS} apart.

This matches how the sequence was shot: SEPE recorded it at 29.97 fps (exactly
30000/1001). A constant-frame-rate video describes these times exactly with a
start time and a frame rate, so the images convert into a video with no
timing information lost. It is the reference the other scenarios deviate
from."""),
    "frame-drops": (frame_drops, "Some frames are never delivered", f"""\
The camera fails to deliver some frames, so they are missing from the
sequence:

- two single drops: frames {sorted(DROPPED)[0]} and {sorted(DROPPED)[1]};
- a burst of {len(BURST)} consecutive frames ({BURST[0]}–{BURST[-1]}, about
  {ms(len(BURST) * (ideal_ns(1) - START_NS))}).

A dropped frame was never captured, so its PNG is left out, and the frames
that remain keep their real capture times. The image content and its time
stay consistent, as with a real camera, and the times show gaps where frames
are missing.

**Why it can't become a video as is:** a constant-frame-rate video assumes
one frame every {FRAME_MS}. Playing these images at that rate would make
everything after a gap happen too early. To build a video, the gaps have to
be filled (for example by repeating the last frame) or the frames re-timed,
and either way the original capture times are lost."""),
    "jitter": (jitter, "Every frame, each time off by a random amount", f"""\
Every frame is delivered, but each capture time is off by a random amount of
up to ±{ms(JITTER_NS)} from the ideal time, as when the camera's clock or the
moment the timestamp is taken isn't precise. The amounts come from a random
generator with a fixed seed ({JITTER_SEED}), so every run gives the same times.
The jitter is small compared with the {FRAME_MS} between frames, so times
still always increase.

**Why it can't become a video as is:** a constant-frame-rate video can only
place frames at evenly spaced times. Each frame has to be moved to the
nearest slot, so the real capture times are lost."""),
    "duplicate-timestamps": (duplicate_timestamps, "Some frames share a capture time", f"""\
Every frame is delivered, but frames {", ".join(map(str, sorted(DUPLICATED)))}
get the same capture time as the frame before them. Frames 150 and 151 are
consecutive, so frames 149, 150 and 151 all share one time. Each image is
still a different picture. This happens when a driver resends a timestamp, or
when the clock's resolution is coarser than the time between frames.

**Why it can't become a video as is:** two different pictures can't be shown
at the same instant. A video needs one of them dropped or moved to another
time, and nothing in the data says which time is the right one. After a
duplicate, the next frame comes twice as late as usual, so the spacing is
irregular too."""),
    "out-of-order": (out_of_order, "Some times go backwards", f"""\
Every frame is delivered, in capture order, but some times go backwards:

- frames {SWAPPED[0]} and {SWAPPED[1]} swap their times, so frame {SWAPPED[1]}
  looks as if it was captured before frame {SWAPPED[0]};
- from frame {CLOCK_STEP_FRAME} on, the clock is {ms(-CLOCK_STEP_NS)} behind,
  as after a clock reset or an NTP correction. Frames {CLOCK_STEP_FRAME}
  onwards get times that earlier frames already used.

**Why it can't become a video as is:** sorting the images by time no longer
gives the order they were captured in, and after the clock step two different
frames fall in the same half-second. A video needs one presentation order, so
one of the two orders (file order or time order) has to be trusted, and the
data alone doesn't say which."""),
    "rate-change": (rate_change, "The frame rate switches, with jitter", f"""\
The camera switches frame rate every {RATE_CHANGE_RUN} images, as auto-exposure
does when the light changes: {RATE_CHANGE_RUN} images at 29.97 fps (every
frame of the sequence), then {RATE_CHANGE_RUN} at {30 / 1.001 / RATE_CHANGE_STEP:.3f} fps (every
{RATE_CHANGE_STEP}nd frame, {ms(RATE_CHANGE_STEP * (ideal_ns(1) - START_NS))} apart), and so on. Each image keeps
the capture time of its frame, so the content matches the time, plus the
same jitter as `jitter/` (up to ±{ms(JITTER_NS)}, seed {JITTER_SEED}). Times always
increase, by at least ~27 ms. `batch_{2 * RATE_CHANGE_RUN:03d}/` is the
first switch: one run at each rate.

**Why it can't become a video as is:** no constant frame rate fits every
image in a slot of its own. At 29.97 fps the slower runs leave an empty slot
between their frames, which only a repeated frame can fill; at
{30 / 1.001 / RATE_CHANGE_STEP:.3f} fps two frames of the faster runs fall in each
slot, so one would be dropped; any other rate needs one or the other. Only a
variable-frame-rate video, each frame at its own time, holds exactly these
images.

**Why {RATE_CHANGE_IMAGES} images, not {SEQUENCE_FRAMES}:** a batch size is a number of images, and
a slow run skips frames. A fast run takes {RATE_CHANGE_RUN} images from {RATE_CHANGE_RUN} frames and
skips none; a slow run takes {RATE_CHANGE_RUN} images from {RATE_CHANGE_STEP * RATE_CHANGE_RUN} frames and skips
{(RATE_CHANGE_STEP - 1) * RATE_CHANGE_RUN}. The {SEQUENCE_FRAMES} frames of the sequence make {len(rate_change_runs(SEQUENCE_FRAMES))} runs:

{rate_change_table(SEQUENCE_FRAMES)}

As in `frame-drops/`, a frame that is skipped has no image, so the batches
stop at the number of images, not of frames."""),
}


def readme(name: str, title: str, body: str, rows) -> str:
    times = [t for _, t in rows]
    deltas = [b - a for a, b in zip(times, times[1:])]
    return f"""\
# {name}: {title}

{body}

## Numbers

| Images | Batches | Time between frames (min) | Time between frames (max) |
|---|---|---|---|
| {len(rows)} | {len(rows)} | {ms(min(deltas))} | {ms(max(deltas))} |

## Files

`batch_001/` holds the first image, `batch_002/` the first two, and so on up
to `batch_{len(rows):03d}/`, which holds all of them. Each folder has a
`timestamps.csv` with one row per image:

```
image,timestamp_ns
{rows[0][0]},{rows[0][1]}
```

- `image`: the PNG's path, relative to the repository root. The images come
  from `datasets/ImageSequence/001/` (8192x4320 PNGs) and are not copied.
- `timestamp_ns`: the capture time, in nanoseconds since the Unix epoch. The
  first ideal capture is at 2026-01-01T00:00:00Z.

This folder is generated by `image-sequence/scripts/build-timestamps.py`;
see [the overview](../README.md).
"""


def overview(counts) -> str:
    table = "\n".join(
        f"| [`{name}/`]({name}/README.md) | {counts[name]} | {title} |"
        for name, (_, title, _) in SCENARIOS.items()
    )
    return f"""\
# Image sequence timestamps

Datasets that associate each image of `datasets/ImageSequence/001/` (300 PNG
frames, 8192x4320) with a capture time, as a camera would record it.

The sequence was shot at 29.97 fps (exactly 30000/1001), so an ideal camera
captures frame *i* at *i* × 1001/30000 s after the first frame, one frame
every {FRAME_MS}. `regular/` is that ideal camera. The other folders each
describe one way a real camera deviates from it, and why the images can then
no longer become a constant-frame-rate video without losing timing
information.

| Scenario | Images | What happens |
|---|---|---|
{table}

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
"""


def main() -> None:
    images = sorted(IMAGES_DIR.glob("*.png"))
    if not images:
        raise SystemExit(f"no PNG images in {IMAGES_DIR}: run scripts/build-datasets.sh images")
    images = [img.relative_to(REPO_DIR).as_posix() for img in images]
    width = len(str(len(images)))

    counts = {}
    for name, (build, title, body) in SCENARIOS.items():
        rows = build(images)
        counts[name] = len(rows)
        scenario_dir = OUT_DIR / name
        shutil.rmtree(scenario_dir, ignore_errors=True)  # no stale batches from an earlier run
        for size in range(1, len(rows) + 1):
            batch_dir = scenario_dir / f"batch_{size:0{width}d}"
            batch_dir.mkdir(parents=True)
            with open(batch_dir / "timestamps.csv", "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["image", "timestamp_ns"])
                writer.writerows(rows[:size])
        (scenario_dir / "README.md").write_text(readme(name, title, body, rows))
        print(f"{name}: {len(rows)} batches")
    (OUT_DIR / "README.md").write_text(overview(counts))


if __name__ == "__main__":
    main()
