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
    sparse                a few frames far apart, with uneven gaps (frames 0, 10, 30, ...)

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
# sparse: the gaps between kept frames, in frames, taken in turn
SPARSE_GAPS = (10, 20)


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


def sparse_frames(frames: int) -> list[int]:
    """The frames sparse keeps: 0, then each SPARSE_GAPS[k] frames after the
    previous one, in turn."""
    kept, index = [], 0
    while index < frames:
        kept.append(index)
        index += SPARSE_GAPS[(len(kept) - 1) % len(SPARSE_GAPS)]
    return kept


def sparse(images):
    return [(images[i], ideal_ns(i)) for i in sparse_frames(len(images))]


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
SPARSE_FRAMES = sparse_frames(SEQUENCE_FRAMES)
# The smallest gap between two images of rate-change (its jitter makes it
# less than a frame period).
_RATE_CHANGE_TIMES = [t for _, t in rate_change(list(range(SEQUENCE_FRAMES)))]
RATE_CHANGE_MIN_GAP_NS = min(b - a for a, b in zip(_RATE_CHANGE_TIMES, _RATE_CHANGE_TIMES[1:]))

# Per scenario: how its times are built, and what its README says about it
SCENARIOS = {
    "regular": (regular, "Every frame, ideal timing", f"""\
The ideal camera: every frame is delivered, and frame *i* is captured exactly
*i* × 1001/30000 s after the first one, so frames are always {FRAME_MS} apart.
This is how SEPE shot the sequence: at 29.97 fps, exactly 30000/1001.

A start time and a frame rate describe these times exactly, so the server
encodes them as a constant-frame-rate video (30000/1001) without losing
timing. It is the reference the other scenarios deviate from."""),
    "frame-drops": (frame_drops, "Some frames are never delivered", f"""\
The camera fails to deliver some frames:

- two single drops: frames {sorted(DROPPED)[0]} and {sorted(DROPPED)[1]};
- a burst of {len(BURST)} consecutive frames ({BURST[0]}–{BURST[-1]}, about
  {ms(len(BURST) * (ideal_ns(1) - START_NS))}).

A dropped frame has no PNG, and the frames that remain keep their real
capture times, so the times show a gap wherever frames are missing.

**Why no constant frame rate fits:** at one frame every {FRAME_MS}, every
frame after a gap would play too early. A constant-frame-rate video needs
the gaps filled (by repeating a frame) or the frames moved, and loses the
capture times either way. The server encodes it at a variable frame rate,
each frame at its capture time."""),
    "jitter": (jitter, "Every frame, each time off by a random amount", f"""\
Every frame is delivered, but each capture time is off the ideal one by a
random amount of up to ±{ms(JITTER_NS)}, as when the camera's clock, or the
moment it takes the timestamp, is not precise. The amounts come from a
random generator with a fixed seed ({JITTER_SEED}), so every run gives the same
times. The jitter is small next to the {FRAME_MS} between frames, so the
times always increase.

**Why no constant frame rate fits:** a constant frame rate places frames at
evenly spaced times, so every frame would move to the nearest slot and lose
its capture time. The server encodes it at a variable frame rate, each frame
at its capture time."""),
    "duplicate-timestamps": (duplicate_timestamps, "Some frames share a capture time", f"""\
Every frame is delivered, but frames {", ".join(map(str, sorted(DUPLICATED)))}
get the capture time of the frame before them; 150 and 151 are consecutive,
so frames 149, 150 and 151 share one time. Each image is still a different
picture. This happens when a driver resends a timestamp, or when the clock
is coarser than the time between frames. The frame after a duplicate comes
two frame periods after the shared time ({ms(2 * (ideal_ns(1) - START_NS))}), or three after
149–151 ({ms(3 * (ideal_ns(1) - START_NS))}).

**Why it can't become a video:** two different pictures can't be shown at
the same instant, and nothing in the data says which of them, if any, has
the right time."""),
    "out-of-order": (out_of_order, "Some times go backwards", f"""\
Every frame is delivered, in capture order, but some times go backwards:

- frames {SWAPPED[0]} and {SWAPPED[1]} swap their times, so frame {SWAPPED[1]}
  looks as if it was captured before frame {SWAPPED[0]};
- from frame {CLOCK_STEP_FRAME} on, the clock is {ms(-CLOCK_STEP_NS)} behind,
  as after a clock reset or an NTP correction: frame {CLOCK_STEP_FRAME} gets about the
  time of frame {round((CLOCK_STEP_FRAME * (ideal_ns(1) - START_NS) + CLOCK_STEP_NS) / (ideal_ns(1) - START_NS))}, which came half a second earlier.

**Why it can't become a video:** a video shows its frames in one order, and
sorting these images by time no longer gives the order they were captured
in. One of the two orders, the upload's or the times', would have to be
trusted, and the data alone doesn't say which."""),
    "rate-change": (rate_change, "The frame rate switches, with jitter", f"""\
The camera switches frame rate every {RATE_CHANGE_RUN} images, as auto-exposure
does when the light changes: {RATE_CHANGE_RUN} images at 29.97 fps (every frame of the
sequence), then {RATE_CHANGE_RUN} at {30 / 1.001 / RATE_CHANGE_STEP:.3f} fps (every {RATE_CHANGE_STEP}nd frame, {ms(RATE_CHANGE_STEP * (ideal_ns(1) - START_NS))}
apart), and so on. Each image keeps the capture time of its frame, so the
content matches the time, plus the same jitter as `jitter/` (up to
±{ms(JITTER_NS)}, seed {JITTER_SEED}). The times always increase, by at least
{ms(RATE_CHANGE_MIN_GAP_NS)}. `batch_{2 * RATE_CHANGE_RUN:03d}/` holds the first switch: one run at each rate.

**Why no constant frame rate fits:** at 29.97 fps the slow runs leave an
empty slot between their frames, which only a repeated frame can fill; at
{30 / 1.001 / RATE_CHANGE_STEP:.3f} fps two frames of a fast run fall in each slot, so one would be
dropped; any other rate needs one or the other. The server encodes it at a
variable frame rate, each frame at its capture time.

**Why {RATE_CHANGE_IMAGES} images, not {SEQUENCE_FRAMES}:** a batch size is a number of images, and a
slow run takes {RATE_CHANGE_RUN} images from {RATE_CHANGE_STEP * RATE_CHANGE_RUN} frames, skipping {(RATE_CHANGE_STEP - 1) * RATE_CHANGE_RUN}. The {SEQUENCE_FRAMES} frames of the
sequence make {len(rate_change_runs(SEQUENCE_FRAMES))} runs:

{rate_change_table(SEQUENCE_FRAMES)}

**Is this realistic?** The mechanism is. When the light drops, a camera
needs a longer exposure than one frame period ({FRAME_MS} at 29.97 fps), and
many cameras then lower their frame rate to get it, through the "auto
exposure priority" control (`exposure_auto_priority` on Linux webcams; on
Windows a device may for example go from 30 to 25 fps, see
[Microsoft's documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/stream/ksproperty-cameracontrol-auto-exposure-priority)).
Other cameras keep a fixed frame rate and darken the image instead. In
robotics the frame rate also changes when a driver publishes more slowly
under CPU or USB load, when the sensor switches modes, or with triggered
capture. Two things are simplified here:

- the switches are regular, every {RATE_CHANGE_RUN} images between the same two rates; a
  real camera changes when the light does, often in steps (30, 25, 20 fps,
  ...) rather than straight to half the rate;
- the images come from a sequence shot at a constant 29.97 fps. A camera
  that slows down for a longer exposure also takes different pictures
  (brighter, more motion blur, often less noise), which changes the video
  size too."""),
    "sparse": (sparse, "A few frames far apart, with uneven gaps", f"""\
The camera keeps only a few frames, far apart and unevenly: frame 0, then
one {SPARSE_GAPS[0]} frames later, then one {SPARSE_GAPS[1]} frames after that, and so on in turn
({", ".join(map(str, SPARSE_FRAMES[:6]))}, ... up to {SPARSE_FRAMES[-1]}). Each image keeps the ideal capture time of
its frame, so the gaps are {ms(SPARSE_GAPS[0] * (ideal_ns(1) - START_NS))} and {ms(SPARSE_GAPS[1] * (ideal_ns(1) - START_NS))}. This is what a camera
records when it saves a frame only now and then: on an event, or on a slow
timer. With {SEQUENCE_FRAMES} frames in the sequence and {len(SPARSE_GAPS)} kept in every {SPARSE_GAPS[0] + SPARSE_GAPS[1]}, it has
{len(SPARSE_FRAMES)} images; `batch_003/` (frames {", ".join(map(str, SPARSE_FRAMES[:3]))}) is the smallest uneven case.

**Why no constant frame rate fits:** one slot every {ms(SPARSE_GAPS[0] * (ideal_ns(1) - START_NS))} fits the short
gaps, but each long one leaves an empty slot that only a repeated frame can
fill; one slot every {ms((SPARSE_GAPS[0] + SPARSE_GAPS[1]) // 2 * (ideal_ns(1) - START_NS))} (the mean gap) gives each image a slot of its own,
but moves frames by {ms((SPARSE_GAPS[1] - SPARSE_GAPS[0]) // 2 * (ideal_ns(1) - START_NS))}. The server encodes it at a variable frame rate,
each frame at its capture time; `batch_002/`, with a single gap, is still
evenly spaced and becomes constant (3000/1001)."""),
}


def video_ms(ns: int) -> int:
    """A time from the first capture as the video stores it: FFmpeg's
    Matroska muxer rounds to the millisecond."""
    return (ns + 500_000) // 1_000_000


def in_the_video(rows) -> str:
    """How the server turns the whole batch into a video: each frame's
    timestamp, and how long it is shown."""
    first = rows[0][1]
    stamps = [video_ms(t - first) for _, t in rows]
    # The last frame lasts as long as the gap before it.
    last_gap_ns = rows[-1][1] - rows[-2][1]
    shown = [b - a for a, b in zip(stamps, stamps[1:])] + [video_ms(last_gap_ns)]
    table = "\n".join(
        f"| {i} | {Path(image).name.split('_')[0]} | {(t - first) / 1e6:.3f} | {stamp} | "
        + (f"**{length}** (the gap before it)" if i == len(rows) - 1 else f"{length}") + " |"
        for i, ((image, t), stamp, length) in enumerate(zip(rows, stamps, shown)))
    last = len(rows) - 1
    frame = int(Path(rows[-1][0]).name.split("_")[0])
    return f"""\
## In the video

The server encodes `batch_{len(rows):03d}/` at a variable frame rate: one frame per
image, each at its capture time, with no frame rate declared (see
[Constant or variable frame rate](../../README.md#constant-or-variable-frame-rate)).
The first capture time, {first} ns, is written once as the
`CAPTURE_START_NS` tag, and each frame's timestamp is its capture time minus
that one, rounded to the millisecond (FFmpeg's Matroska muxer stores
milliseconds), so at most 0.5 ms off:

| # | Image | Capture time − first (ms, from the CSV) | Video timestamp (ms) | Shown for (ms) |
|---|---|---|---|---|
{table}

- The image name is the frame's number in the 29.97 fps sequence, so its
  capture time is that number of frame periods after image 000: image {frame:03d}
  is {frame} × 1001/30000 s = {(rows[-1][1] - first) / 1e6:.3f} ms after it.
- Frames 0 to {last - 1} each last until the next one's timestamp. The file holds
  no duration for them: with no `DefaultDuration` in the track, Matroska
  defines a frame's duration as the gap to the next frame.
- Frame {last} has no next frame, so the server gives it the gap before it:
  {(rows[-1][1] - first) / 1e6:.3f} − {(rows[-2][1] - first) / 1e6:.3f} = {last_gap_ns / 1e6:.3f} ms, {video_ms(last_gap_ns)} ms rounded. The file holds it as
  the video's length, the segment `Duration`: {stamps[-1]} + {video_ms(last_gap_ns)} = {stamps[-1] + video_ms(last_gap_ns)} ms, the
  {(stamps[-1] + video_ms(last_gap_ns)) / 1000:.3f} s that `ffprobe` reports.
"""


def server_check(test: str):
    """For a scenario the server must reject: which images it rejects, and
    from which batch. `test` is the server's unit test of it."""
    def section(rows) -> str:
        times = [t for _, t in rows]
        bad = [i for i in range(1, len(times)) if times[i] <= times[i - 1]]
        table = "\n".join(
            f"| {i} | `batch_{i + 1:03d}/` | `image {i} has timestamp {times[i]}, not after the previous one "
            f"({times[i - 1]})` |" for i in bad)
        return f"""\
## Checking the server's input

This scenario is not encoded: it checks that the server rejects what it
can't turn into a video. The server rejects an image whose capture time is
not after the previous image's, with gRPC `INVALID_ARGUMENT`, and leaves no
video (see [Capture times](../../README.md#capture-times)). An upload stops at
the first such image: `batch_{bad[0]:03d}/` and smaller are valid, and every batch from
`batch_{bad[0] + 1:03d}/` on is rejected at image {bad[0]}.

Every image the server would reject, each checked against the one before:

| Image | First in | The server's error |
|---|---|---|
{table}

The server's unit test `{test}`
([`crates/server/src/main.rs`](../../crates/server/src/main.rs), `cargo test -p
server`) checks that exactly these images of `batch_{len(rows):03d}/` are rejected, that
`batch_{bad[0]:03d}/` is accepted and that `batch_{bad[0] + 1:03d}/` is not.
"""
    return section


# Per scenario: a section of its README computed from its rows, after Files.
SECTIONS = {
    "duplicate-timestamps": server_check("rejects_duplicate_timestamps"),
    "out-of-order": server_check("rejects_times_going_back"),
    "sparse": in_the_video,
}


def readme(name: str, title: str, body: str, rows) -> str:
    times = [t for _, t in rows]
    deltas = [b - a for a, b in zip(times, times[1:])]
    section = f"\n{SECTIONS[name](rows)}" if name in SECTIONS else ""
    return f"""\
# {name}: {title}

{body}

## Numbers

| Images | Time between frames (min) | Time between frames (max) |
|---|---|---|
| {len(rows)} | {ms(min(deltas))} | {ms(max(deltas))} |

## Files

`batch_001/` holds the first image, `batch_002/` the first two, and so on up
to `batch_{len(rows):03d}/`, which holds all of them. Each folder has a
`timestamps.csv` with one row per image:

```
image,timestamp_ns
{rows[0][0]},{rows[0][1]}
```

- `image`: the PNG's path, relative to the repository root, in
  `datasets/ImageSequence/001/` (8192x4320 PNGs, not copied here).
- `timestamp_ns`: the capture time, in nanoseconds since the Unix epoch; the
  ideal first capture is at 2026-01-01T00:00:00Z.
{section}
This folder is generated by `image-sequence/scripts/build-timestamps.py`;
see [the overview](../README.md).
"""


# What the server makes of each scenario's full batch.
VIDEO = {
    "regular": "constant frame rate, 30000/1001",
    "duplicate-timestamps": "rejected",
    "out-of-order": "rejected",
}


def overview(counts) -> str:
    table = "\n".join(
        f"| [`{name}/`]({name}/README.md) | {counts[name]} | {title} | {VIDEO.get(name, 'variable frame rate')} |"
        for name, (_, title, _) in SCENARIOS.items()
    )
    return f"""\
# Image sequence timestamps

Capture times for the images of `datasets/ImageSequence/001/` (300 PNG
frames, 8192x4320), as a camera would record them.

The sequence was shot at 29.97 fps (exactly 30000/1001), so an ideal camera
captures frame *i* at *i* × 1001/30000 s after the first one, every
{FRAME_MS}. `regular/` is that camera; every other scenario is one way a real
camera deviates from it, so that a constant frame rate no longer fits the
images. The last column is what the server makes of the whole scenario:

| Scenario | Images | What happens | Video |
|---|---|---|---|
{table}

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
