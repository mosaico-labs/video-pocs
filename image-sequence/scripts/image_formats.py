#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""Charts the image formats the server turned a video back into (`server
--extract-images`, see crates/server/src/images/; the formats are in
crates/formats/) in the tracked
image-results/<scenario>/<batch>/preset_<P>_crf_<C>/server/ folder (the client's
results, scripts/fetch_results.py, are in client/ next to it), apart from the
video's results in video-results/:

    images.json                  a copy of the server's report
    README.md                    the formats, how they were measured, the sizes
                                 and times with the charts, and what they show
    filesize-comparison.svg      the images of every format, in all, against
                                 the input PNGs and the AV1 video
    encode-time-per-image.svg    writing one image, per format
    decode-time-per-image.svg    reading one image back, per format
    encode-time.svg              writing all the images, per format, against
                                 SVT-AV1 encoding the AV1 video
    decode-time.svg              reading all the images back, per format,
                                 against dav1d decoding the whole AV1 video
    lossless/README.md           the formats that keep the frames exactly: how
                                 they compare among themselves
    lossy/README.md              the formats that convert them (PNG, JPEG): what
                                 they lose, the measures of loss explained
    <group>/<format>/README.md   the format: what it is, its numbers, and its
                                 charts, each explained
    <group>/<format>/filesize.svg              the size of each image
    <group>/<format>/filesize-total.svg        all the images, against the
                                               input PNGs and the AV1 video
    <group>/<format>/encode-time-per-image.svg the time to write each image
    <group>/<format>/decode-time-per-image.svg the time to read each image back
    <group>/<format>/encode-time.svg           writing all the images, against
                                               SVT-AV1 encoding the AV1 video
    <group>/<format>/decode-time.svg           reading all the images back,
                                               against dav1d decoding the video
    <group>/<format>/parallel.svg              all the images, one
                                               and several at a time
    <group>/<format>/loss-per-image.svg        lossy only: what each image lost
    <group>/<format>/butteraugli-max-per-image.svg     lossy only: butteraugli
    <group>/<format>/butteraugli-3norm-per-image.svg   of each image
    images-quality.json          a copy of the server's butteraugli report
                                 (`server --image-quality`), if measured
    loss-rms.svg                 what each format lost (RMS error), over all
                                 the images
    butteraugli-max.svg          how far the images look from their frames,
    butteraugli-3norm.svg        butteraugli's max-norm and 3-norm, per format

The charts at the top compare every format, a dashed line parting the
lossless group from the lossy one.

The AV1 video's encoding time is the encoder's working time from the
server's upload report (video-results/.../upload.json, or output/ next to
the video): SVT-AV1 encoding the frames while they arrived, without its waits
for decoded PNGs.

The exact formats hold the frames as dav1d decodes them, 8-bit YUV 4:2:0,
losslessly: the server checks each image against its frame. PNG and JPEG,
for comparison, convert them, losing as little as each allows; the server
measures what each image lost (RMS error), and with `server --image-quality`
how far each image looks from its frame (butteraugli); the charts and the
READMEs show both.

    uv run scripts/image_formats.py [--timestamps CSV] [--preset P] [--crf C]

The charts share report.py's visual system.
"""

import argparse
import csv
import json
import math
import re
import shutil
from pathlib import Path

import report
from matplotlib.transforms import blended_transform_factory, offset_copy
from report import (BUTTERAUGLI_MAX, LOSSES, INK, INK_SECONDARY, MIB, MUTED, PRESETS, RECEIVING, SURFACE, TOPIC, WORKSPACE,
                    band_key,
                    draw_bands, in_bold, plt, save, subtitle)

# One color per format, in a fixed order (report.py's validated categorical
# slots); the AV1 video, a reference and not a format, is gray.
FORMATS = {
    "raw": ("Raw I420", PRESETS[0]),
    "zstd": ("zstd", PRESETS[1]),
    "avif": ("AVIF (lossless)", PRESETS[2]),
    "ffv1": ("FFV1", PRESETS[3]),
    "png": ("PNG (16-bit RGB)", PRESETS[4]),
    # Validated with the five before it (dataviz's validate_palette.js).
    "jpeg": ("JPEG (quality 100)", "#4f7a28"),
}
REFERENCE = MUTED
# What each format is, for its README.
ABOUT = {
    "raw": "The frame's three planes written as they are, Y then U then V (I420), with no header and no "
           "compression. Writing and reading an image are a copy to and from a file, and every image has the same "
           "size, width x height x 1.5 bytes. The reader needs the size and the color tags from elsewhere (here, "
           "`images.json`).",
    "zstd": "The raw I420 bytes (see [raw](../raw/README.md)) compressed with zstd at level 3, its default, on "
            "every core (zstd's multithreaded mode), and decompressed on one thread. zstd knows nothing about "
            "images, so it compresses less than an image codec, but it is fast and available almost everywhere. "
            "Like raw, the file has no header: the reader needs the size and the color tags from elsewhere.",
    "avif": "Each frame encoded on its own by SVT-AV1 in lossless, still-picture mode, in an AVIF file written by "
            "FFmpeg's AVIF muxer, and read back with dav1d, the AV1 decoder of the video. AVIF is a standard image "
            "format: any AVIF reader opens these files, which keep the YUV 4:2:0 frame and its color tags (BT.709, "
            "limited range). Lossless AV1 is slow at this size, in both directions; the SVT-AV1 preset does not "
            "change a lossless image.",
    "ffv1": "Each frame as one FFV1 frame (version 3, 16 slices coded in parallel) in its own Matroska file, "
            "with FFmpeg's own encoder and decoder. FFV1 is a lossless, intra-only video codec made for "
            "archiving; it keeps the YUV 4:2:0 frame and its color tags. It is not an image format: reading the "
            "files needs FFmpeg or another FFV1 decoder.",
    "png": "For comparison: PNG holds only RGB, so each frame is converted to it, as closely as PNG allows. "
           "16-bit RGB, so that rounding loses next to nothing, with each U and V sample repeated over the 2x2 "
           "pixels it covers, so that reading the image back and averaging each 2x2 block gives the sample "
           "again. What is lost is mostly the colors a YUV frame can hold but RGB can't, outside the RGB cube, "
           "clipped to it. Tagged sRGB, like the PNGs the video was made from, so any viewer shows it right; "
           "written and read with the `png` crate (one thread).",
    "jpeg": "For comparison: a standard JFIF file any viewer shows in the right colors, as close to the frame as "
            "JPEG allows. JPEG's YCbCr is BT.601 in full range, so each frame is converted to it from BT.709 in "
            "limited range, then written at quality 100 (every quantization step 1, the least JPEG can lose) in "
            "4:2:0, its U and V planes passed as they are, with no RGB or resampling in the encoder "
            "(`jpeg-encoder` crate). It is read back with FFmpeg's decoder, which also gives the 4:2:0 planes as "
            "they are, then converted back. What is lost: the rounding of the two conversions and of the DCT.",
}
# The bars of the per-frame charts: one neutral slate for every format,
# apart from the formats' own colors.
FRAME_BARS = RECEIVING


def bold(text):
    """`text` in bold, as report.py's in_bold(), spaced as plain text: math
    text adds space after a comma, and its period is followed by a wider
    space than a word's, so a period ends with a thin space instead. A
    hyphen would be a minus sign: it is a Unicode hyphen instead."""
    text = text.replace(",", "{,}").replace("-", "\u2010")
    return in_bold(re.sub(r"\.( |$)", lambda m: "{.}\\," if m.group(1) else "{.}", text))


def ms(seconds):
    return seconds * 1000


PLANES = ("y", "u", "v")


def total_loss(images):
    """The loss of all `images` together, plane by plane: (mse, largest
    error, share of samples changed, samples changed)."""
    out = {}
    for plane in PLANES:
        samples = sum(i["loss"][plane]["samples"] for i in images)
        out[plane] = (sum(i["loss"][plane]["mse"] * i["loss"][plane]["samples"] for i in images) / samples,
                      max(i["loss"][plane]["max_error"] for i in images),
                      sum(i["loss"][plane]["changed"] for i in images) / samples,
                      sum(i["loss"][plane]["changed"] for i in images))
    return out


def rms_error(loss):
    """The RMS error of a sample, Y, U and V together, in 8-bit levels, from
    one image's loss."""
    samples = sum(loss[p]["samples"] for p in PLANES)
    return (sum(loss[p]["mse"] * loss[p]["samples"] for p in PLANES) / samples) ** 0.5


def decimals(values):
    """Decimals that show the smallest nonzero value of `values` with two
    significant digits, at least 3."""
    nonzero = [v for v in values if v > 0]
    return max(3, 1 - math.floor(math.log10(min(nonzero)))) if nonzero else 3


def levels(value):
    """A loss in 8-bit levels, with enough decimals to show it."""
    return "0" if value == 0 else f"{value:.{decimals([value])}f}"


def rms_error_of(f):
    """The RMS error of a sample over all of a format's images."""
    loss = total_loss(f["images"])
    samples = {p: sum(i["loss"][p]["samples"] for i in f["images"]) for p in PLANES}
    return (sum(loss[p][0] * samples[p] for p in PLANES) / sum(samples.values())) ** 0.5


def psnr(mse):
    return "∞ (exact)" if mse == 0 else f"{10 * math.log10(255 ** 2 / mse):.2f} dB"


def loss_table(f):
    """The rows of the table of what a format's images lost, by plane."""
    loss = total_loss(f["images"])
    return ["| | Y | U | V |", "|---|---|---|---|",
            "| PSNR (higher is closer) | " + " | ".join(psnr(loss[p][0]) for p in PLANES) + " |",
            "| RMS error, 8-bit levels | " + " | ".join(levels(loss[p][0] ** 0.5) for p in PLANES) + " |",
            "| Largest error, 8-bit levels | " + " | ".join(str(loss[p][1]) for p in PLANES) + " |",
            "| Samples changed | " + " | ".join(changed(loss[p][3], loss[p][2]) for p in PLANES) + " |"]


def changed(count, share):
    """How many samples changed: a percentage, or "1 in N" when too few."""
    if count == 0:
        return "none"
    if share >= 1e-4:
        return f"{count:,} ({100 * share:.2f}%)"
    return f"{count:,} (1 in {round(1 / share):,})"


# The two steps compared with the AV1 video: (title word, what is timed for
# the images, the video's bar, what is timed for the video, images.json key).
STEPS = {
    "encode": ("Encoding", "written, each a file of its own, one at a time{hatched}",
               "AV1 video\n(SVT-AV1)",
               "SVT-AV1 encoding the AV1 video ({n} frames, encoder working time)", "encode_seconds"),
    "decode": ("Decoding", "decoded, each a file of its own, one at a time{hatched}, file reading included",
               "AV1 video\n(dav1d)",
               "dav1d decoding the whole AV1 video once ({n} frames, file reading included)", "decode_seconds"),
}


PARALLEL_HATCH = "///"


def speedup(one, many):
    """How several at a time compare with one at a time: "2.1x faster",
    "2.2x slower", or "the same" within 5%."""
    if abs(one - many) <= 0.05 * one:
        return "the same"
    return f"{one / many:.1f}x faster" if many < one else f"{many / one:.1f}x slower"


def WORKERS_TEXT(formats):
    """How many images at a time the parallel pass ran, e.g. "4"."""
    return " or ".join(sorted({str(f["parallel"]["workers"]) for f in formats if f.get("parallel")})) or "several"


def sequence_bars(f, key, label, color):
    """The bars of all of a format's images for `key` (encode_seconds or
    decode_seconds): one at a time, and, after it, hatched, several at a
    time if measured."""
    bars = [(label, f[key], color)]
    if f.get("parallel"):
        # Next to its format's bar, in its color: "4 at a time" says enough.
        bars.append((f"{f['parallel']['workers']} at a time", f["parallel"][key], color, PARALLEL_HATCH))
    return bars


def total_chart(step, note, video_seconds, bars, n, path, width, name=None, groups=None):
    """All the images of each of `bars` (label, seconds, color) against the
    AV1 video's `step`, the reference, drawn first. `name` is the format's,
    in a chart of one format."""
    word, images, video_label, video, _ = STEPS[step]
    images = images.format(hatched=" (hatched: several at a time)" if any(len(bar) > 3 for bar in bars) else "")
    bar_chart(f"{name}: {word.lower()} all the images" if name else f"{word} all the images",
              f"{note}\nAll {n} images {images},\nagainst {video.format(n=n)}. Lower is faster.", "s",
              [(video_label, video_seconds, REFERENCE), *bars], path, 2, width=width,
              extras=["reference", *(against_video(bar[1], video_seconds) for bar in bars)],
              groups=groups)


def upload_report(encoding):
    """The server's upload report of the video (video-results/, or output/
    next to the video)."""
    for path in (WORKSPACE / "video-results" / encoding / "upload.json",
                 WORKSPACE / "output" / encoding / f"{TOPIC}.upload.json"):
        if path.exists():
            return json.loads(path.read_text())
    raise SystemExit(f"no upload report for {encoding}: run scripts/run.sh first")


def against_input(size, input_size):
    """How a size compares with the input PNGs': "160x smaller", "1.6x larger"."""
    if size <= input_size:
        return f"{input_size / size:.0f}x smaller" if input_size / size >= 10 else f"{input_size / size:.1f}x smaller"
    return f"{size / input_size:.1f}x larger"


# The input PNGs, the images the video was made from: the darkest neutral, a
# reference like the video (gray).
INPUT = INK_SECONDARY
INPUT_LABEL = "Input PNGs"


def against_video(seconds, video_seconds):
    """How a time compares with the AV1 video's: "2.9x slower", "4.8x faster"."""
    if seconds >= video_seconds:
        return f"{seconds / video_seconds:.1f}x slower"
    return f"{video_seconds / seconds:.1f}x faster"


# The video results' pixel error, an RMS error in 8-bit levels like this
# script's: its bands, from a viewer study, say how visible an RMS error is.
PIXEL_ERROR = next(loss for loss in LOSSES if loss.name == "pixel-error")


def with_bands(axes, bands, top, below):
    """Shades `bands` (a report.py Loss: the video results' bands of what
    the eye sees) behind the bars up to `top`, showing where the first band
    ends unless the bars would shrink to nothing, and keys them `below`
    points under the axes. Returns the new top."""
    if top * 20 >= bands.bands[1][0]:
        top = max(top, bands.bands[1][0] * 1.25)
    draw_bands(axes, bands, top)
    axes.legend(handles=band_key(bands), loc="upper left", bbox_to_anchor=(0, 0),
                bbox_transform=offset_copy(axes.transAxes, axes.figure, y=-below, units="points"),
                ncol=len(bands.bands), borderaxespad=0, handlelength=1.5)
    return top


def bar_chart(title, note, unit, bars, path, digits, width=None, extras=None, label=None, groups=None,
              bands=None, pairs=None):
    """One bar per (label, value, color[, hatch]), with its value on top (as
    `label(value)` writes it, if given), and under it the bar's line of
    `extras`, if given. `groups` (first bar, last bar, name) are parted by
    dashed lines and named under the bars. `bands` (see with_bands) shade
    what the eye sees behind them."""
    figure, axes = plt.subplots(figsize=(width or 1.5 * len(bars) + 1.8, 4.6))
    # Room for the description, ~12 points a line.
    axes.set_title(title, pad=14 + 12 * (note.count("\n") + 1))
    subtitle(axes, note, y=1.03)
    labels, values, colors = zip(*(bar[:3] for bar in bars))
    top = max(values)
    # A bar too short to see is still drawn, ~2px high.
    # By position, not by label: two bars may share one ("4 at a time").
    drawn = axes.bar(range(len(bars)), [max(v, top * 0.006) for v in values], width=0.55, color=colors, zorder=2)
    axes.set_xticks(range(len(bars)), labels)
    # A fourth item hatches the bar: the same format, run another way.
    for patch, bar in zip(drawn, bars):
        if len(bar) > 3:
            patch.set_hatch(bar[3])
            patch.set_edgecolor(SURFACE)
            patch.set_linewidth(0)
    for x, value in enumerate(values):
        text = (label(value) if label else f"{value:,.{digits}f} {unit}") + (f"\n{extras[x]}" if extras and extras[x] else "")
        axes.annotate(text, (x, max(value, top * 0.006)), xytext=(0, 4), textcoords="offset points", ha="center",
                      va="bottom", color=INK, fontweight="bold")
    limit = top * (1.2 if extras else 1.12)
    if bands:
        limit = with_bands(axes, bands, limit, 62 if groups else 30)
    axes.set_ylim(0, limit)
    axes.set_ylabel(unit)
    axes.grid(axis="x", visible=False)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=9, length=0)
    draw_groups(axes, groups or [], len(bars), pairs)
    save(figure, path)


def draw_groups(axes, groups, count, pairs=None):
    """Under the bar labels (two lines at most): `pairs` (first bar, last
    bar, name), if given, each named once under its bars; then `groups`
    (first bar, last bar, name), a rule over each group's bars and its name,
    and a dashed line between groups. `count` is the number of bars."""
    under = blended_transform_factory(axes.transData, axes.transAxes)
    rule = -0.17
    if pairs:
        for first, last, name in pairs:
            axes.text((first + last) / 2, -0.115, name, transform=under, ha="center", va="top", color=INK,
                      fontsize=9)
        rule = -0.21
    for x in sorted({b for first, last, _ in groups for b in (first - 0.5, last + 0.5) if -0.5 < b < count - 0.5}):
        axes.axvline(x, color=MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=0)
    for first, last, name in groups:
        axes.plot([first - 0.38, last + 0.38], [rule, rule], color=MUTED, linewidth=1, transform=under,
                  clip_on=False)
        axes.text((first + last) / 2, rule - 0.02, name, transform=under, ha="center", va="top",
                  color=INK_SECONDARY, fontsize=9, fontweight="bold")


def frame_chart(title, note, values, unit, digits, path, bands=None):
    """One bar per frame, with its value in `unit`, the mean as a dashed
    line and the largest value labeled. `bands` (see with_bands) shade what
    the eye sees behind them."""
    # Wide enough for a label under every frame.
    figure, axes = plt.subplots(figsize=(max(7.6, 0.17 * len(values)), 3.6))
    # Room for the description, ~12 points a line.
    axes.set_title(title, pad=16 + 12 * (note.count("\n") + 1))
    subtitle(axes, note)
    frames = range(len(values))
    axes.bar(frames, values, width=0.8, color=FRAME_BARS, zorder=2)
    mean = sum(values) / len(values)
    axes.axhline(mean, color=INK, linewidth=1.1, linestyle=(0, (4, 3)), zorder=3)
    same = max(values) == min(values)
    suffix = f" {unit}" if unit else ""
    axes.annotate(f"every image {mean:.{digits}f}{suffix}" if same else f"mean {mean:.{digits}f}{suffix}", (1, mean),
                  xycoords=("axes fraction", "data"), xytext=(0, 4), textcoords="offset points", ha="right",
                  va="bottom", color=INK, fontsize=8)
    if not same:
        largest = max(frames, key=values.__getitem__)
        # Centered on its bar, unless that would run past an edge of the chart.
        edge = len(values) // 8
        ha, x = (("left", largest - 0.4) if largest < edge else ("right", largest + 0.4)
                 if largest >= len(values) - edge else ("center", largest))
        axes.annotate(f"{values[largest]:.{digits}f}{suffix} (frame {largest})", (x, values[largest]),
                      xytext=(0, 4), textcoords="offset points", ha=ha, va="bottom", color=INK, fontsize=8,
                      fontweight="bold")
    axes.set_xlabel("frame")
    axes.set_ylabel(unit)
    axes.set_xlim(-0.6, len(values) - 0.4)
    axes.set_xticks(frames)
    axes.tick_params(axis="x", labelsize=7, length=0)
    limit = max(values) * 1.15
    if bands:
        limit = with_bands(axes, bands, limit, 40)
    axes.set_ylim(0, limit)
    axes.grid(axis="x", visible=False)
    save(figure, path)


# The server's limit for one message (common::protocol::MAX_DECODE_MESSAGE_SIZE,
# mosaicod's default): an image sent in one message must fit in it.
MESSAGE_LIMIT = 50 * MIB

# What one DoGet message may carry when a topic is served back
# (common::protocol::MAX_ENCODE_MESSAGE_SIZE, mosaicod's GRPC_MSG_MAX_SIZE_BYTES).
DOWNLOAD_LIMIT = 512 * MIB


def message_text(largest):
    """Whether an image of `largest` bytes fits in one message, uploaded and
    served back."""
    up = "fits in" if largest <= MESSAGE_LIMIT else "is over"
    return (f"{up} the {MESSAGE_LIMIT / MIB:.0f} MiB a message may carry to the server (mosaicod's default "
            f"decode limit), and served back to a client it comes in one `DoGet` message, which may carry up to "
            f"{DOWNLOAD_LIMIT / MIB:.0f} MiB")


def warm_up(formats, name):
    """A line for each time (encoding or decoding) of a format whose first
    image took over 1.5x the median: a warm-up the mean includes."""
    lines = []
    for f in formats:
        for step, key in (("encode", "encode_seconds"), ("decode", "decode_seconds")):
            times = [ms(i[key]) for i in f["images"]]
            median = sorted(times)[len(times) // 2]
            slow = [t for t in times[:3] if t > 1.5 * median]
            if times[0] > 1.5 * median:
                lines.append(f"- {name[f['format']]}'s first images take longer to {step} "
                             f"({', '.join(f'{t:.0f}' for t in slow)} ms, against a median of {median:.0f} ms): "
                             "a warm-up, included in the means.")
    return lines


def totals_table(step, data, formats, name):
    """The rows of the table of all the images against the AV1 video, the
    video first."""
    word, *_, key = STEPS[step]
    video = data[f"video_{key}"]
    tool = "SVT-AV1" if step == "encode" else "dav1d"
    rows = [f"| | {word} all {data['frames']} images | Against the AV1 video |", "|---|---|---|",
            f"| *AV1 video ({tool}), the reference* | {video:.2f} s | – |"]
    for f in formats:
        rows.append(f"| {name[f['format']]} | {f[key]:.2f} s | {against_video(f[key], video)} |")
        if f.get("parallel"):
            seconds = f["parallel"][key]
            rows.append(f"| {name[f['format']]}, {f['parallel']['workers']} images at a time | {seconds:.2f} s | "
                        f"{against_video(seconds, video)} |")
    return rows


def spread(values):
    """Mean, median, (smallest, its frame), (largest, its frame)."""
    ordered = sorted(values)
    low = min(range(len(values)), key=values.__getitem__)
    high = max(range(len(values)), key=values.__getitem__)
    return sum(values) / len(values), ordered[len(values) // 2], (values[low], low), (values[high], high)


def time_story(word, values):
    """What a per-image time chart shows: its spread, and the frames that
    took over 1.5x the median."""
    mean, median, (low, low_k), (high, high_k) = spread(values)
    slow = [(k, t) for k, t in enumerate(values) if t > 1.5 * median]
    text = (f"{word} an image takes {mean:.0f} ms on average (median {median:.0f} ms), from {low:.0f} ms "
            f"(frame {low_k}) to {high:.0f} ms (frame {high_k}).")
    if slow:
        frames = ", ".join(f"{k} ({t:.0f} ms)" for k, t in slow)
        # The slow frames at the very start: a warm-up.
        warm = 0
        while warm < len(slow) and slow[warm][0] == warm:
            warm += 1
        warm_text = ("" if not warm else f"; frame 0 is a warm-up" if warm == 1
                     else f"; frames 0–{warm - 1} are a warm-up")
        spikes = ("single spikes (each image is timed once: see [the limits](../../README.md#each-image-is-timed-once))")
        later = "" if warm == len(slow) else f", and the others {spikes}" if warm else f"; {spikes}"
        text += (f" {'One frame takes' if len(slow) == 1 else f'{len(slow)} frames take'} over 1.5x the median: "
                 f"{frames}{warm_text}{later}.")
    else:
        text += " No frame takes over 1.5x the median: the time is steady from image to image."
    return text


def format_readme(sequence, settings, data, f, raw):
    """The README of one format's folder: what it is, its numbers, and its
    charts, each explained."""
    key, n = f["format"], data["frames"]
    label = FORMATS[key][0]
    sizes = [i["bytes"] / MIB for i in f["images"]]
    encodes = [ms(i["encode_seconds"]) for i in f["images"]]
    decodes = [ms(i["decode_seconds"]) for i in f["images"]]
    size_mean, _, (size_low, size_low_k), (size_high, size_high_k) = spread(sizes)
    video_encode, video_decode = data["video_encode_seconds"], data["video_decode_seconds"]
    vs_raw = "–" if f is raw else f"{raw['bytes'] / f['bytes']:.1f}x smaller"
    if size_high == size_low:
        size_text = (f"Every image is {size_mean:.2f} MiB: the format does not compress, so the size depends only "
                     "on the frame's width and height, not on what it shows.")
    else:
        size_text = (f"The images take {size_mean:.2f} MiB on average, from {size_low:.2f} MiB (frame "
                     f"{size_low_k}) to {size_high:.2f} MiB (frame {size_high_k}): a frame with more detail takes "
                     "more bytes.")
    lines = [
        f"# {label}: {sequence}, {settings}",
        "",
        "Generated by `scripts/image_formats.py` from the server's report (`server --extract-images`, "
        "[`images.json`](../../images.json)); do not edit. One of the [" + GROUPS[f["exact"]][1].lower()
        + " formats](../README.md), in [the comparison of the image formats](../../README.md).",
        "",
        "## The format",
        "",
        ABOUT[key],
        "",
        f"Settings: {f['settings']}. The files are `output/.../images/{key}/frame_<k>.*`, one per frame of the "
        f"video, {n} here, "
        + ("and each one was read back and checked byte for byte against its frame: none of them loses anything."
           if f["exact"] else
           "and each one was read back and compared with its frame: see [what it loses](#what-it-loses)."),
        "",
        "## Its numbers",
        "",
        "| | All the images | Per image (mean) | Against |",
        "|---|---|---|---|",
        f"| Size | {f['bytes'] / MIB:,.1f} MiB | {size_mean:.2f} MiB | the input PNGs: "
        f"{against_input(f['bytes'], data['input_bytes'])} ({data['input_bytes'] / MIB:,.1f} MiB); raw: {vs_raw}; "
        f"the AV1 video: {f['bytes'] / data['video_bytes']:.0f}x larger ({data['video_bytes'] / MIB:.1f} MiB) |",
        f"| Encoding | {f['encode_seconds']:.2f} s | {sum(encodes) / n:.0f} ms | the AV1 video: "
        f"{against_video(f['encode_seconds'], video_encode)} ({video_encode:.2f} s) |",
        f"| Decoding | {f['decode_seconds']:.2f} s | {sum(decodes) / n:.0f} ms | the AV1 video: "
        f"{against_video(f['decode_seconds'], video_decode)} ({video_decode:.2f} s) |",
        "| Lost | " + ("nothing: every image is exact | | |" if f["exact"] else
                      f"RMS error {levels(rms_error_of(f))} levels | | the exact formats: nothing |"),
        "",
        f"The largest image ({max(sizes):.2f} MiB) {message_text(max(i['bytes'] for i in f['images']))}.",
        "",
        "## Size of each image",
        "",
        "One bar per frame, its image's size; the dashed line is the mean.",
        "",
        size_text,
        "",
        f"![{label}: size of each image](filesize.svg)",
        "",
        "## Size of all the images, against the input and the video",
        "",
        f"All {n} images, against the {n} input PNGs the video was made from (8-bit RGBA, as uploaded) and the AV1 "
        f"video, the two references, first. The images take {f['bytes'] / MIB:,.1f} MiB, "
        f"{against_input(f['bytes'], data['input_bytes'])} than the input PNGs ({data['input_bytes'] / MIB:,.1f} "
        f"MiB) and {f['bytes'] / data['video_bytes']:.0f}x the AV1 video ({data['video_bytes'] / MIB:.1f} MiB), which "
        "is "
        f"{against_input(data['video_bytes'], data['input_bytes'])} than the input.",
        "",
        f"![{label}: size of all the images, against the input PNGs and the AV1 video](filesize-total.svg)",
        "",
        "## Encoding time of each image",
        "",
        "One bar per frame: the time to write its image, from the frame in memory to the file written. The dashed "
        "line is the mean.",
        "",
        time_story("Writing", encodes),
        "",
        f"![{label}: encoding time of each image](encode-time-per-image.svg)",
        "",
        "## Decoding time of each image",
        "",
        "One bar per frame: the time to read its image back, from the file to the frame in memory (the file is in "
        "the OS cache, so the disk's speed is not measured). The dashed line is the mean.",
        "",
        time_story("Reading", decodes),
        "",
        f"![{label}: decoding time of each image](decode-time-per-image.svg)",
        "",
        "## All the images, against the AV1 video",
        "",
        f"The time for all {n} images, against the same step on the AV1 video, the reference: the gray bar, first.",
        "",
        f"**Encoding.** Writing all {n} images takes {f['encode_seconds']:.2f} s, "
        f"{against_video(f['encode_seconds'], video_encode)} than the {video_encode:.2f} s SVT-AV1 worked to "
        "encode the video on the server (its working time, without its waits for decoded PNGs). The two do not do "
        f"the same work: SVT-AV1 compresses heavily and loses detail ({settings}), while every image here is "
        + ("an exact copy of a frame already decoded" if f["exact"] else "a frame already decoded, converted")
        + ". The comparison says what each step costs, not which is more efficient.",
        "",
        f"![{label}: encoding all the images, against the AV1 video](encode-time.svg)",
        "",
        f"**Decoding.** Reading all {n} images back takes {f['decode_seconds']:.2f} s, "
        f"{against_video(f['decode_seconds'], video_decode)} than the {video_decode:.2f} s dav1d takes to decode "
        "the whole AV1 video. The images are read one by one, each a file of its own; the video in one pass, its "
        "frames depending on each other, so a client wanting any single frame of the video must decode from the "
        "keyframe before it.",
        "",
        f"![{label}: decoding all the images, against the AV1 video](decode-time.svg)",
    ]
    if f.get("parallel"):
        p = f["parallel"]
        lines += [
            "",
            "## Several images at a time",
            "",
            f"Every format is also written and read back {p['workers']} images at a time (one image leaves most "
            f"cores idle): all {n} images, then all of them read back"
            + (" (each one checked byte for byte again)" if f["exact"] else "")
            + ". One image takes as long as before; what changes is the whole sequence"
            + (": it takes less." if p["encode_seconds"] < f["encode_seconds"] and p["decode_seconds"] < f["decode_seconds"]
               else ". Here it doesn't always take less: a format that barely uses the CPU (copying memory to "
                    "files) gains nothing from several at a time, and the images only compete for the memory's "
                    "bandwidth."),
            "",
            "| All the images | One at a time | " f"{p['workers']} at a time | Several at a time |",
            "|---|---|---|---|",
            f"| Encoding | {f['encode_seconds']:.2f} s | {p['encode_seconds']:.2f} s | "
            f"{speedup(f['encode_seconds'], p['encode_seconds'])} |",
            f"| Decoding | {f['decode_seconds']:.2f} s | {p['decode_seconds']:.2f} s | "
            f"{speedup(f['decode_seconds'], p['decode_seconds'])} |",
            "",
            f"![{label}: all the images, one at a time and {p['workers']} at a time](parallel.svg)",
        ]
    if not f["exact"]:
        errors = [rms_error(i["loss"]) for i in f["images"]]
        mean, _, (low, low_k), (high, high_k) = spread(errors)
        exact_images = sum(e == 0 for e in errors)
        lines += [
            "",
            "## What it loses",
            "",
            "How far the images read back are from their frames, over all the frames, plane by plane (the measures "
            "are explained in the [lossy README](../README.md#what-they-lose)):",
            "",
            *loss_table(f),
            "",
            "One bar per frame: the RMS error of a sample of its image, Y, U and V together. The dashed line is the "
            "mean.",
            "",
            f"An image is off by {levels(mean)} levels on average, from {levels(low)} (frame {low_k}) to "
            f"{levels(high)} (frame {high_k})"
            + (f"; {exact_images} of the {n} images give their frame back exactly." if exact_images else "."),
            "",
            f"![{label}: what each image lost](loss-per-image.svg)",
        ]
    if "butteraugli" in f:
        lines += ["", "## How far it looks: butteraugli", "", "Butteraugli and its two norms are explained in the "
                  "[lossy README](../README.md#how-far-they-look-butteraugli).", "",
                  *butteraugli_table([f], {key: label}, data["video_butteraugli"]), ""]
        for norm, norm_key, what, file in NORMS:
            values = [q[norm_key] for q in f["butteraugli"]]
            mean, _, (low, low_k), (high, high_k) = spread(values)
            lines += [f"One bar per frame: the {norm} of its image, {what}. The dashed line is the mean.", "",
                      f"The {norm} is {mean:.4f} on average, from {low:.4f} (frame {low_k}) to {high:.4f} "
                      f"(frame {high_k}).", "", f"![{label}: butteraugli {norm} of each image]({file}-per-image.svg)",
                      ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# The two groups of formats: (folder, name, what they have in common).
GROUPS = {
    True: ("lossless", "Lossless", "keep the frames exactly as dav1d decodes them"),
    False: ("lossy", "Lossy", "convert the frames, losing as little as the format allows"),
}

# What the measures of loss are, for the READMEs that show them.
LOSS_TERMS = (
    "- **RMS error** (root mean square): for every sample, the difference between the image read back and its "
    "frame, in 8-bit levels; the differences are squared, averaged, and the average square-rooted. It is the "
    "typical error of a sample, a large error weighing more than a small one: 0 means exact, and 1 is the "
    "smallest step of an 8-bit sample, so an RMS error of 0.25 is a quarter of a step.\n"
    "  How visible it is: the charts shade the video results' bands for it, from a viewer study (Afnan et al., "
    "*Sensors* 23(3):1297, 2023: 25 viewers flicker-compared images with JPEG versions of falling quality, and "
    "each one's threshold of seeing a difference was taken). Below 0.75 levels no viewer saw it on any image "
    "(*invisible in every test*), up to 2.01 the average viewer didn't (*invisible on average*), up to 5.74 the "
    "average viewer did (*visible on average*), and above, every viewer saw it on every image. They hold on "
    "average over an image: an RMS error says how big the error is overall, not where it is.\n"
    "- **PSNR** (peak signal-to-noise ratio): the same error on a logarithmic scale, against the largest value "
    "of a sample, 255: 10 x log10(255² / RMS²) decibels. Higher is closer; it is infinite for an exact plane, "
    "and every 6 dB more halves the RMS error.\n"
    "- **Largest error**: the sample furthest from its frame, in 8-bit levels.\n"
    "- **Samples changed**: how many samples differ from their frame at all, by any amount."
)


# Butteraugli, for the READMEs that show it.
BUTTERAUGLI_TERMS = (
    "**Butteraugli** is Google's perceptual metric, the one the quality tool also measures the video with: it "
    "models how the eye sees both images and finds, spot by spot, how visible their difference is. 0 means the "
    "two look identical. Unlike the RMS error, it weighs a difference by how visible it is: the same error "
    "counts more on a flat area than in fine texture, where the eye misses it. It compares RGB, so the frame and "
    "the image read back are both converted to linear RGB the same way, and differ only by what the format "
    "lost. Two norms sum up an image:\n"
    "\n"
    "- **Max-norm**: the most visible difference anywhere in the image, its worst spot. Google's classes: "
    "below 1.0 *great* (no visible difference), 1.0 to 2.0 *acceptable*, above 2.0 *not acceptable*.\n"
    "- **3-norm**: the differences over the whole image, a large one weighing more than a small one (libjxl's "
    "aggregate). Lower than the max-norm, it says how far the image is overall rather than at its worst spot."
)

# (name, images-quality.json key, title, file) of butteraugli's two norms.
NORMS = [("max-norm", "butteraugli_max", "worst spot of the image", "butteraugli-max"),
         ("3-norm", "butteraugli_3norm", "over the whole image", "butteraugli-3norm")]


def butteraugli_of(f, key):
    """The mean of a butteraugli norm over a format's images: 0 for an exact
    format, None if not measured."""
    if f["exact"]:
        return 0.0
    if "butteraugli" not in f:
        return None
    values = [i[key] for i in f["butteraugli"]]
    return sum(values) / len(values)


def butteraugli_table(formats, name, video):
    """The rows of the table of butteraugli per format, and the video's from
    its PNGs for scale."""
    rows = ["| Format | Max-norm, mean | Max-norm, worst image | 3-norm, mean |", "|---|---|---|---|"]
    for f in formats:
        if "butteraugli" in f:
            worst = max(range(len(f["butteraugli"])), key=lambda k: f["butteraugli"][k]["butteraugli_max"])
            rows.append(f"| {name[f['format']]} | {butteraugli_of(f, 'butteraugli_max'):.4f} | "
                        f"{f['butteraugli'][worst]['butteraugli_max']:.4f} (frame {worst}) | "
                        f"{butteraugli_of(f, 'butteraugli_3norm'):.4f} |")
    if video:
        rows.append(f"| *for scale: the AV1 video, against its source PNGs* | {video['butteraugli_max']:.4f} | "
                    f"{video['worst']:.4f} (frame {video['worst_k']}) | {video['butteraugli_3norm']:.4f} |")
    return rows


def video_butteraugli(encoding):
    """The video's butteraugli against its source PNGs, from the quality
    tool (video-results/.../quality.csv), if measured."""
    path = WORKSPACE / "video-results" / encoding / "quality.csv"
    if not path.exists():
        return None
    rows = list(csv.DictReader(path.open()))
    values = [float(r["butteraugli_max"]) for r in rows if r.get("butteraugli_max")]
    norms = [float(r["butteraugli_3norm"]) for r in rows if r.get("butteraugli_3norm")]
    if not values:
        return None
    worst = max(range(len(values)), key=values.__getitem__)
    return {"butteraugli_max": sum(values) / len(values), "butteraugli_3norm": sum(norms) / len(norms),
            "worst": values[worst], "worst_k": worst}


def butteraugli_section(formats, name, data, more=None):
    """The README section on butteraugli, if measured."""
    if not any("butteraugli" in f for f in formats):
        return []
    # The full explanation once, in the lossy README; a line and a link elsewhere.
    terms = BUTTERAUGLI_TERMS if not more else (
        "**Butteraugli**, Google's perceptual metric, says how visible the difference between each image and its "
        "frame is: its max-norm is the worst spot (below 1.0 no visible difference, above 2.0 not acceptable), "
        f"its 3-norm the whole image. The [lossy README]({more}) explains both.")
    lines = ["### How far they look: butteraugli", "", terms, "",
             "The exact formats are their frames, byte for byte: their distance is 0, not measured. For scale, the "
             "last row is what the AV1 video itself lost against the PNGs it was made from, measured by the quality "
             "tool the same way.", "", *butteraugli_table(formats, name, data["video_butteraugli"]), ""]
    if more:
        lines += ["Each image's scores are in the lossy formats' folders.", ""]
    if not any(f["exact"] for f in formats):
        return lines
    return lines + ["![Butteraugli max-norm of each format](butteraugli-max.svg)", "",
                    "![Butteraugli 3-norm of each format](butteraugli-3norm.svg)", ""]


def rms_band(value):
    """The video results' band of an RMS error (see PIXEL_ERROR)."""
    return [name for bound, name in PIXEL_ERROR.bands if value >= bound][-1]


def butteraugli_class(score):
    """Google's class of a butteraugli max-norm."""
    return "great" if score < 1 else "acceptable" if score <= 2 else "not acceptable"


def butteraugli_finding(formats, name, data):
    """The finding on butteraugli, if measured: each lossy format's class on
    Google's scale, and its loss next to the video's own."""
    lossy = [f for f in formats if "butteraugli" in f]
    if not lossy:
        return []
    video = data["video_butteraugli"]
    parts = []
    for f in lossy:
        scores = [q["butteraugli_max"] for q in f["butteraugli"]]
        mean = butteraugli_of(f, "butteraugli_max")
        classes = sorted({butteraugli_class(v) for v in scores}, key=["great", "acceptable", "not acceptable"].index)
        part = (f"{name[f['format']]} scores {mean:.4f} on average, {min(scores):.4f} to {max(scores):.4f} (max-norm): "
                + (f"*{classes[0]}* for every image" if len(classes) == 1 else
                   "its images range from *" + "* to *".join([classes[0], classes[-1]]) + "*"))
        if video:
            part += f", {mean / video['butteraugli_max']:.0%} of what the AV1 video lost against its source PNGs"
        parts.append(part)
    classes = {butteraugli_class(butteraugli_of(f, "butteraugli_max")) for f in lossy}
    spread_text = "; ".join(
        f"{name[f['format']]} changes {sum(total_loss(f['images'])[p][3] for p in PLANES) / sum(i['loss'][p]['samples'] for i in f['images'] for p in PLANES):.2%} "
        f"of its samples, by at most {max(total_loss(f['images'])[p][1] for p in PLANES)} levels" for f in lossy)
    lines = [f"- **Butteraugli puts the lossy formats in {'different classes' if len(classes) > 1 else 'the same class'}**: "
             + "; ".join(parts)
             + ". Its max-norm is the single most visible spot of an image, so small errors spread over many "
             f"samples leave a visible spot somewhere, while a few changed samples don't ({spread_text})."]
    disagree = [f for f in lossy if rms_band(rms_error_of(f)).startswith("invisible")
                and butteraugli_class(butteraugli_of(f, "butteraugli_max")) != "great"]
    if disagree:
        lines.append(
            "- **The two metrics disagree on " + " and ".join(name[f["format"]] for f in disagree) + "**: by its "
            "RMS error, " + "; ".join(f"{levels(rms_error_of(f))} levels, it is *{rms_band(rms_error_of(f))}* in the "
                                     "viewer study" for f in disagree)
            + ", while butteraugli's max-norm calls it *" + butteraugli_class(butteraugli_of(disagree[0], "butteraugli_max"))
            + "*. They measure different things: the study's bands judge the error on average over the image, as "
            "its viewers saw whole images, while the max-norm judges the single worst spot, by a model of the eye "
            "meant to be strict. The truth is likely between: invisible to a viewer looking at the image, a "
            "difference a model or a close look at the right spot can find.")
    return lines


def group_of(f):
    return GROUPS[f["exact"]]


def grouped_bars(formats, offset=0):
    """The groups of a chart whose bars are `formats` in order, after
    `offset` other bars: (first bar, last bar, label) for each group."""
    spans = []
    for i, f in enumerate(formats):
        label = f"{group_of(f)[0]}"
        if spans and spans[-1][2] == label:
            spans[-1] = (spans[-1][0], offset + i, label)
        else:
            spans.append((offset + i, offset + i, label))
    return spans


def slow_frames(formats, name):
    """For each format and step with frames over 1.5x the median, a line
    with how many and where: the third limit of the overview."""
    lines = []
    for f in formats:
        for word, key in (("writing", "encode_seconds"), ("reading", "decode_seconds")):
            times = [i[key] for i in f["images"]]
            median = sorted(times)[len(times) // 2]
            slow = [k for k, t in enumerate(times) if t > 1.5 * median]
            if slow:
                warm = 0
                while warm < len(slow) and slow[warm] == warm:
                    warm += 1
                kind = ("all a warm-up" if warm == len(slow) else "none a warm-up" if not warm
                        else f"{warm} a warm-up")
                lines.append(f"- [{name[f['format']]}]({group_of(f)[0]}/{f['format']}/README.md), {word}: "
                             f"{len(slow)} of {len(times)} images ({kind}).")
    return lines or ["- No image of any format took over 1.5x its format's median."]


def overview(sequence, settings, args, data):
    """The README of the encoding's folder: every format together."""
    w, h, n = data["width"], data["height"], data["frames"]
    formats = data["formats"]
    raw = next(f for f in formats if f["format"] == "raw")
    largest = {f["format"]: max(i["bytes"] for i in f["images"]) for f in formats}
    video_ms = ms(data["video_decode_seconds"]) / n
    name = {key: label for key, (label, _) in FORMATS.items()}
    too_large = [k for k in largest if largest[k] > MESSAGE_LIMIT]
    smallest = min((f for f in formats if f["exact"]), key=lambda f: f["bytes"])
    timestamps = args.timestamps.resolve().relative_to(WORKSPACE)
    video = Path(data["video"])
    video = video.relative_to(WORKSPACE) if video.is_absolute() else video

    def ratio(f):
        return "–" if f is raw else f"{raw['bytes'] / f['bytes']:.1f}x smaller"

    def files(f):
        return f"`frame_<k>.{ {'raw': 'yuv', 'zstd': 'yuv.zst', 'ffv1': 'mkv', 'jpeg': 'jpg'}.get(f['format'], f['format'])}`"

    lines = [
        f"# {sequence}, {settings}: the video back to images",
        "",
        "Generated by `scripts/image_formats.py` from the server's report (`server --extract-images`, copied here "
        "as `images.json`); do not edit.",
        "",
        "Which format should the server use to stream a video's frames back to a client as images? The frames are "
        "8-bit YUV 4:2:0, BT.709 in limited range. The formats compared are in two groups, each in its own folder "
        "with its own README:",
        "",
        "- **[Lossless](lossless/README.md)**: raw, zstd, AVIF and FFV1 keep the frames **exactly as dav1d "
        "decodes them**, with no conversion. The server reads every image back and checks it byte for byte "
        "against its frame: they lose nothing and differ only in size and speed.",
        "- **[Lossy](lossy/README.md)**, for comparison: PNG holds only RGB, and JPEG holds YCbCr with another "
        "matrix (BT.601) and range (full), so neither can hold the frames as they are. Each frame is converted, "
        "losing as little as the format allows, and the server measures how far each image read back is from its "
        "frame.",
        "",
        "In every format, nothing is dropped or repeated: one image per frame. This page compares all the formats "
        "together; each group's README compares its formats among themselves, and each format's folder has its "
        "own charts, frame by frame.",
        "",
        *([
            "## Choosing a format",
            "",
            "Every format against what decides the choice: size, decoding and encoding time and loss, measured "
            "here; and from public sources (see [format support](../../../../format-support/README.md)), which "
            "programs open the file as an image with no add-on and no code of one's own (of five: web browsers, "
            "Windows Photos, macOS Preview, Python's Pillow, FFmpeg), and how many websites use it: a program "
            "that needs an add-on, a player or code of one's own to open a format doesn't count. Each cell holds the "
            "value and is shaded by how "
            "the format ranks on it among the six: darkest the best, lightest the worst. There is no overall score: "
            "how much each criterion weighs is the reader's choice, and the last column says when each format is "
            "the one to pick.",
            "",
            "![Choosing a format](choosing-a-format.svg)",
            "",
        ] if all("butteraugli" in f or f["exact"] for f in formats) else []),
        "## The formats",
        "",
    ]
    for exact in (True, False):
        folder, title, common = GROUPS[exact]
        lines += [f"**{title}** ([`{folder}/`]({folder}/README.md)): they {common}.", "",
                  "| Format | Files | Settings |", "|---|---|---|",
                  *(f"| [{name[f['format']]}]({folder}/{f['format']}/README.md) | {files(f)} | {f['settings']} |"
                    for f in formats if f["exact"] == exact), ""]
    lines += [
        "AVIF and FFV1 carry the video's color tags: BT.709 matrix, limited range, BT.709 primaries, sRGB "
        "transfer. Raw and zstd have no header, so the reader needs the size and these tags from elsewhere (here, "
        "`images.json`; a client reading the images back gets the size in the topic's metadata). PNG is tagged sRGB, and JPEG's colors are JFIF's by definition (BT.601, full range), so "
        "any viewer shows both right.",
        "",
        "## How it was measured",
        "",
        f"The input is the video `{video}` ({data['video_bytes'] / MIB:.1f} MiB, {n} frames of {w}x{h}), encoded "
        f"by the server from `{timestamps}` with SVT-AV1 {settings}. The server decoded it with dav1d "
        f"({data['video_decode_seconds']:.1f} s, {video_ms:.0f} ms per frame), then, frame after frame, wrote "
        "the frame in each format and read it back.",
        "",
        "- **Encoding** is from the frame in memory to the file written; **decoding** from the file to the frame "
        "in memory. For PNG and JPEG, both include the color conversion.",
        "- One image at a time, each format with the threads it uses on its own; the formats run one after the "
        "other, so their times don't overlap. Then each format's whole sequence again, "
        f"{WORKERS_TEXT(formats)} images at a time (the hatched bars).",
        "- Writing and reading the file are included, but the OS keeps the files in its cache: the disk's speed "
        "is not measured.",
        "",
        f"To measure again, then redraw this folder (the video uploaded first with `PRESET={args.preset} "
        f"CRF={args.crf} scripts/run.sh {timestamps}`):",
        "",
        "```sh",
        "cargo build --release",
        f"./target/release/server --extract-images {video}",
        f"./target/release/server --image-quality {video}   # butteraugli, PNG and JPEG",
        f"uv run scripts/image_formats.py --timestamps {timestamps} --preset {args.preset} --crf {args.crf}",
        "```",
        "",
        "The client reading the images back is measured apart: see [its results](../client/README.md).",
        "",
        "The images go next to the video, to `images/<format>/` (not tracked: "
        f"{sum(f['bytes'] for f in formats) / 1024**3:.1f} GiB here).",
        "",
        "## Every format together",
        "",
        "In every chart, a dashed line parts the lossless formats from the lossy ones, named under the bars; the "
        "references (the input PNGs, the AV1 video) come first.",
        "",
        "| Format | Group | All the images | Per image | vs the input PNGs | vs raw | Encoding, per image | "
        "Decoding, per image | Lost (RMS error) |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| *Input PNGs, the video's source* | reference | {data['input_bytes'] / MIB:,.1f} MiB | "
        f"{data['input_bytes'] / n / MIB:.1f} MiB | – | – | – | – | – |",
        f"| *AV1 video* | reference | {data['video_bytes'] / MIB:,.1f} MiB | {data['video_bytes'] / n / MIB:.2f} MiB | "
        f"{against_input(data['video_bytes'], data['input_bytes'])} | "
        f"{raw['bytes'] / data['video_bytes']:.0f}x smaller | – | {video_ms:.0f} ms | – |",
    ]
    for f in formats:
        key = f["format"]
        lost = "nothing (exact)" if f["exact"] else f"{levels(rms_error_of(f))} levels"
        lines.append(f"| {name[key]} | {group_of(f)[0]} | {f['bytes'] / MIB:,.1f} MiB | {f['bytes'] / n / MIB:.1f} "
                     f"MiB | {against_input(f['bytes'], data['input_bytes'])} | {ratio(f)} | {ms(f['encode_seconds']) / n:.0f} ms | {ms(f['decode_seconds']) / n:.0f} "
                     f"ms | {lost} |")
    lines += [
        "",
        "The input PNGs are the images the video was made from (8-bit RGBA, as uploaded); the AV1 video holds "
        "them, lossily. Every format here holds the video's frames, not the PNGs.",
        "",
        "![Size of the images](filesize-comparison.svg)",
        "",
        "![Encoding time per image](encode-time-per-image.svg)",
        "",
        "![Decoding time per image](decode-time-per-image.svg)",
        "",
        "### What each format loses",
        "",
        "The lossless formats lose nothing; the lossy ones lose a little, measured as an **RMS error** (root mean "
        "square): for every sample, the difference between the image read back and its frame, in 8-bit levels, "
        "squared, averaged, and square-rooted. It is the typical error of a sample, a large error weighing more "
        "than a small one; 1 is the smallest step of an 8-bit sample. The chart shades the video results' bands of "
        "how visible an RMS error is, from a viewer study (Afnan et al., 2023): below 0.75 levels no viewer saw it "
        "on any image, up to 2.01 the average viewer didn't, up to 5.74 the average viewer did, and above, every "
        "viewer did. The [lossy README](lossy/README.md#what-they-lose) has it plane by plane, with the other "
        "measures.",
        "",
        "![What each format lost](loss-rms.svg)",
        "",
        *butteraugli_section(formats, name, data, "lossy/README.md#how-far-they-look-butteraugli"),
        "### All the images, against the AV1 video",
        "",
        "Each format's images, each a file of its own, one at a time and, in the hatched bar next to it, "
        f"{WORKERS_TEXT(formats)} at a time, against the AV1 video, its frames depending on each other. **Encoding**: the images written from the decoded frames, against SVT-AV1 encoding the video "
        "on the server while the PNGs arrived (the encoder's working time, without its waits for decoded PNGs). "
        "**Decoding**: the images read back, against dav1d decoding the whole video in one pass. File reading is "
        "included in both decodings.",
        "",
        *totals_table("encode", data, formats, name),
        "",
        "![Encoding all the images, against the AV1 video](encode-time.svg)",
        "",
        *totals_table("decode", data, formats, name),
        "",
        "![Decoding all the images, against the AV1 video](decode-time.svg)",
        "",
        "## What it shows",
        "",
        f"- **Among the lossless formats**, {name[smallest['format']]} is the smallest "
        f"({smallest['bytes'] / n / MIB:.1f} MiB per image) and fast in both directions; see the "
        "[lossless README](lossless/README.md#what-it-shows) for how they compare.",
        "- **The lossy formats don't save anything over it**: "
        + "; ".join(f"{name[f['format']]} is {f['bytes'] / smallest['bytes']:.1f}x its size and loses "
                    f"{levels(rms_error_of(f))} levels (RMS)" for f in formats if not f["exact"])
        + ". What they give is an image any viewer opens; see the [lossy README](lossy/README.md#what-it-shows).",
        *butteraugli_finding(formats, name, data),
        (f"- **{', '.join(name[k] for k in too_large)} {'is' if len(too_large) == 1 else 'are'} over the upload "
         f"limit**: a message to the server carries at most {MESSAGE_LIMIT / MIB:.0f} MiB (mosaicod's default), and "
         + ", ".join(f"{name[k]}'s largest image is {largest[k] / MIB:.1f} MiB" for k in too_large)
         + f". Served back, a `DoGet` message may carry up to {DOWNLOAD_LIMIT / MIB:.0f} MiB, so every format comes "
         "back one image per message at most ([the client's results](../client/README.md)).") if too_large else
        f"- **Every format fits in one message** to the server (at most {MESSAGE_LIMIT / MIB:.0f} MiB).",
        f"- **The video itself is still {smallest['bytes'] / data['video_bytes']:.0f}x smaller than the smallest "
        f"images**, and dav1d decodes a frame of it in {video_ms:.0f} ms, faster than any compressed format here. "
        "A client that can run dav1d is better served by the AV1 packets than by any of these images; images are "
        "worth it only for a client that can't decode AV1, or needs any frame on its own without the keyframe "
        "before it.",
        "",
        "## Limits",
        "",
        "- One batch, one encoding: a video with more detail (a lower CRF, a busier scene) makes every format "
        "bigger and slower, and may change the ratios.",
        "- The files were read from the OS cache: from a disk or over the network, the larger formats (raw, PNG, "
        "zstd) lose more time than the tables show.",
        "- Each image is timed once, on a machine doing other things: see below.",
        "",
        "### Each image is timed once",
        "",
        "Every time in these results is a single measurement: each image is written once and read back once, "
        f"and each format's {n} images make its totals. Nothing is repeated to average out the noise of a single "
        "run, so a time can be off for reasons that have nothing to do with the format:",
        "",
        "- **The machine is busy with something else** for a moment: another program, the OS indexing files, a "
        "backup. The image that runs at that moment takes longer.",
        "- **A warm-up at the start**: the first images of a format also pay for things done only once, such as "
        "loading the codec's code, allocating its buffers, or filling the CPU's caches.",
        "- **The machine's own state**: a laptop slows its CPU down when it gets hot, and the OS decides which "
        "cores run what; neither is the same from one run to the next.",
        "",
        "So a rerun gives slightly different times, more so for the shortest ones (a few milliseconds), where any "
        "pause is a large share. The sizes and the losses don't change: they are the same every run.",
        "",
        "To tell a slow image from a slow format, each format's README marks every frame whose time is over 1.5x "
        "the format's median, and calls the slow frames at the very start a warm-up and the others single spikes. "
        "Those frames are included in the means and totals, so one spike makes the mean a little higher than the "
        "median. In this run:",
        "",
        *slow_frames(formats, name),
        "",
        (lambda fast, slow: f"Comparisons between formats far apart ({name[fast['format']]}'s "
                            f"{ms(fast['decode_seconds']) / n:.0f} ms to read an image against {name[slow['format']]}'s "
                            f"{ms(slow['decode_seconds']) / n:.0f} ms) are safe; between formats within a few percent "
                            "of each other they are not, without repeating the run.")(
            *sorted([f for f in formats if f["format"] != "raw"], key=lambda f: f["decode_seconds"])[::len(formats) - 2]),
    ]
    return "\n".join(lines) + "\n"


def group_readme(exact, sequence, settings, data):
    """The README of a group's folder: what its formats have in common, and
    how they compare among themselves."""
    folder, title, common = GROUPS[exact]
    n = data["frames"]
    formats = [f for f in data["formats"] if f["exact"] == exact]
    every = data["formats"]
    raw = next(f for f in every if f["format"] == "raw")
    name = {key: label for key, (label, _) in FORMATS.items()}
    per_image = {f["format"]: f["bytes"] / n for f in formats}
    encode = {f["format"]: ms(f["encode_seconds"]) / n for f in formats}
    decode = {f["format"]: ms(f["decode_seconds"]) / n for f in formats}
    by_name = {f["format"]: f for f in formats}
    lines = [
        f"# {title} formats: {sequence}, {settings}",
        "",
        "Generated by `scripts/image_formats.py` from the server's report (`server --extract-images`, "
        "[`images.json`](../images.json)); do not edit. Part of [the comparison of the image formats](../README.md), "
        "whose charts show these formats next to the "
        + ("lossy ones." if exact else "lossless ones."),
        "",
    ]
    if exact:
        lines += [
            f"These formats {common}, 8-bit YUV 4:2:0, BT.709 in limited range, with no conversion. After writing "
            "each image, the server reads it back and checks it byte for byte against its frame, and stops if one "
            f"differs: every one of the {n} images of every format here is exact. They differ only in size and "
            "speed: their RMS error and their butteraugli distance are 0.",
            "",
        ]
    else:
        lines += [
            f"These formats {common}: PNG holds only RGB, and JPEG holds YCbCr with the BT.601 matrix in full range, "
            "while the frames are YUV 4:2:0, BT.709 in limited range. They are here for comparison, as the image "
            "formats every viewer opens. After writing each image, the server reads it back, converts it back to "
            "the frame's YUV, and measures how far it is from its frame.",
            "",
        ]
    lines += ["| Format | Per image | vs raw | Encoding, per image | Decoding, per image |"
              + ("" if exact else " Lost (RMS error) |"),
              "|---|---|---|---|---|" + ("" if exact else "---|")]
    for f in formats:
        key = f["format"]
        vs_raw = "–" if f is raw else f"{raw['bytes'] / f['bytes']:.1f}x smaller"
        lines.append(f"| [{name[key]}]({key}/README.md) | {per_image[key] / MIB:.1f} MiB | {vs_raw} | "
                     f"{encode[key]:.0f} ms | {decode[key]:.0f} ms |"
                     + ("" if exact else f" {levels(rms_error_of(f))} levels |"))
    lines += ["", "Each format's README has its charts, frame by frame: the size, encoding and decoding time of each "
              "image, all the images against the AV1 video" + ("." if exact else ", and what each image lost."), ""]

    if not exact:
        lines += [
            "## What they lose",
            "",
            "How far the images read back are from their frames, over all the frames, plane by plane. The "
            "measures:",
            "",
            LOSS_TERMS,
            "",
        ]
        for f in formats:
            lines += [f"**[{name[f['format']]}]({f['format']}/README.md)**", "", *loss_table(f), ""]
        lines += [line.replace("### ", "## ", 1) if line.startswith("### ") else line
                  for line in butteraugli_section(formats, name, data)]

    lines += ["## What it shows", ""]
    if exact:
        compressed = [f["format"] for f in formats if f["format"] != "raw"]
        smallest = min(compressed, key=per_image.get)
        fastest = min(compressed, key=decode.get)
        slowest = max(compressed, key=decode.get)
        largest = max(i["bytes"] for i in raw["images"])
        lines += [
            f"- **{name[smallest]} is the smallest**: {per_image[smallest] / MIB:.1f} MiB per image, "
            f"{raw['bytes'] / (per_image[smallest] * n):.1f}x smaller than raw, encoded in {encode[smallest]:.0f} ms "
            f"and decoded in {decode[smallest]:.0f} ms."
            + (" It is not an image format a client opens on its own, though: reading it takes FFmpeg (or another "
               "FFV1 decoder)." if smallest == "ffv1" else ""),
            f"- **{name[fastest]} decodes fastest** of the compressed formats ({decode[fastest]:.0f} ms)"
            f"{', with a decoder available almost everywhere' if fastest == 'zstd' else ''}, but its images are "
            f"{per_image[fastest] / per_image[smallest]:.1f}x the size of {name[smallest]}'s.",
            f"- **{name[slowest]} is by far the slowest**: {encode[slowest]:.0f} ms to encode and "
            f"{decode[slowest]:.0f} ms to decode one image, {decode[slowest] / decode[smallest]:.0f}x "
            f"{name[smallest]}'s decoding, for images {per_image[slowest] / per_image[smallest]:.1f}x as large."
            + (" Lossless AV1 coding at this size is simply expensive: in a separate test on one 8K frame (of "
               "`rate-change/batch_060/preset_9_crf_30`), splitting the image into 4x4 tiles, so that dav1d can "
               "decode them in parallel, barely sped up decoding (0.58 s to 0.52 s); the preset did not change the "
               "image (presets 8 and 10 gave the same size and time). Its advantage is being a standard image file "
               "that any AVIF reader opens." if slowest == "avif" else "")
            + (lambda p: f" One image keeps only a few cores busy, so {p['workers']} at a time write the whole "
                         f"sequence {by_name[slowest]['encode_seconds'] / p['encode_seconds']:.1f}x faster and read "
                         f"it back {by_name[slowest]['decode_seconds'] / p['decode_seconds']:.1f}x faster "
                         f"([details]({slowest}/README.md#several-images-at-a-time))."
               if p else "")(by_name[slowest].get("parallel")),
            f"- **Raw's {largest / MIB:.1f} MiB frames**: a frame {message_text(largest)}. The compressed "
            "formats are well within both.",
            *warm_up(formats, name),
        ]
    else:
        best = min((f for f in every if f["exact"]), key=lambda f: f["bytes"])
        for f in formats:
            key = f["format"]
            loss = total_loss(f["images"])
            lines.append(
                f"- **{name[key]}**: RMS error {levels(rms_error_of(f))} levels, at most "
                f"{max(loss[p][1] for p in PLANES)} level{'s' if max(loss[p][1] for p in PLANES) > 1 else ''} on a "
                f"sample; {per_image[key] / MIB:.1f} MiB per image, {f['bytes'] / best['bytes']:.1f}x "
                f"{name[best['format']]}'s, which loses nothing; {encode[key]:.0f} ms to encode and "
                f"{decode[key]:.0f} ms to decode."
                + (f" Butteraugli max-norm {butteraugli_of(f, 'butteraugli_max'):.4f} on average."
                   if "butteraugli" in f else ""))
        worse = all(f["bytes"] > best["bytes"] and f["encode_seconds"] > best["encode_seconds"]
                    and f["decode_seconds"] > best["decode_seconds"] for f in formats)
        lines += [
            (f"- **Neither saves anything over {name[best['format']]}**, the smallest lossless format: both are "
             "larger and slower in both directions, and lose a little." if worse else
             f"- **Against {name[best['format']]}**, the smallest lossless format, they trade size, speed and a "
             "little loss: see the table above.")
            + " Their advantage is being an image any viewer opens, in the right colors.",
            *butteraugli_finding(formats, name, data),
            *warm_up(formats, name),
        ]
    return "\n".join(lines) + "\n"


# The scorecard's shades, by rank: one hue, light (worst) to dark (best);
# the two darkest take white text.
RANK_SHADES = ["#eef3fb", "#d3e2f6", "#a9c7ee", "#7aa8e3", "#4b88d9", "#2a6cc0"]

# When each format is the one to choose, from its row of the scorecard.
CHOOSE_WHEN = {
    "raw": "Not to send: no decoding at all, but the largest by far,\nand the reader needs its own code.",
    "zstd": "Your own client, exact frames, fast both ways;\nzstd is everywhere, but the client converts YUV itself.",
    "avif": "An exact, standard image file is a must:\nmost browsers open it; slow at 8K, so encode\nand decode several images at a time.",
    "ffv1": "Your own client with FFmpeg: the smallest exact\nimages, fast both ways, but no browser or viewer.",
    "png": "Any viewer, practically exact: but large\nand the slowest to write.",
    "jpeg": "Any viewer, small and quick: but its loss is\nvisible by butteraugli, and it isn't exact.",
}


def shades(values, better):
    """The shade of each value: its place among the distinct values, best
    (`better` is min or max) the darkest, worst the lightest; equal values
    share a shade."""
    distinct = sorted(set(values), reverse=better is max)
    last = len(RANK_SHADES) - 1
    return [RANK_SHADES[last - round(distinct.index(v) * last / max(len(distinct) - 1, 1))] for v in values]


def choose_chart(data, path):
    """The scorecard to choose a format: one row per format, one column per
    criterion, measured here or from the format-support sources, each cell
    shaded by the format's rank on it."""
    from matplotlib.patches import Patch, Rectangle
    from format_support import PLACES, SUPPORT, WEB_USAGE
    from report import SURFACE

    formats = data["formats"]
    n = data["frames"]
    keys = [f["format"] for f in formats]
    # The programs that open each format with no add-on or code of one's own,
    # by short names (the subtitle gives the full ones).
    short = dict(zip(PLACES, ["browsers", "Windows", "macOS", "Pillow", "FFmpeg"]))
    opens = {k: [short[p] for p, (level, _) in zip(PLACES, SUPPORT[k]) if level == "built in"] for k in keys}

    def programs(k):
        names = opens[k]
        if len(names) == len(PLACES):
            return "all five"
        if len(names) <= 1:
            return f"{names[0]} only" if names else "none"
        half = (len(names) + 1) // 2
        return ", ".join(names[:half]) + ",\n" + ", ".join(names[half:])

    by_key = {f["format"]: f for f in formats}

    def in_sequence(f, key):
        """The time of an image in the sequence, ms: the faster of one at a
        time and several at a time (a format that gains nothing from several
        at a time, such as raw's copies, runs one at a time)."""
        return min(ms(f[key]), ms(f["parallel"][key]) if f.get("parallel") else math.inf) / n

    def timed(value, k, key):
        f = by_key[k]
        if not f.get("parallel"):
            return f"{value:,.0f} ms"
        # Always several at a time first, one alone second, whichever is
        # faster: the shade and the rank use the faster.
        one, many, workers = ms(f[key]) / n, ms(f["parallel"][key]) / n, f["parallel"]["workers"]
        return f"{many:,.0f} ms, {workers} at a time\n{one:,.0f} ms one alone"

    loss = {f["format"]: butteraugli_of(f, "butteraugli_max") or 0.0 for f in formats}
    # (title, values, how to write a format's value, the better of two values)
    criteria = [
        ("Size,\nper image", [f["bytes"] / n / MIB for f in formats], lambda v, k: f"{v:.1f} MiB", min),
        ("Decoding, per image\nin the sequence", [in_sequence(f, "decode_seconds") for f in formats],
         lambda v, k: timed(v, k, "decode_seconds"), min),
        ("Encoding, per image\nin the sequence", [in_sequence(f, "encode_seconds") for f in formats],
         lambda v, k: timed(v, k, "encode_seconds"), min),
        ("Loss, butteraugli\nmax-norm", [loss[k] for k in keys], lambda v, k: "exact" if v == 0 else f"{v:.2f}",
         min),
        ("Opens with no add-on\nor own code, in", [len(opens[k]) for k in keys], lambda v, k: programs(k), max),
        ("Websites\nusing it", [WEB_USAGE.get(k, 0.0) for k in keys], lambda v, k: f"{v:.1f}%" if v else "–", max),
    ]
    figure, axes = plt.subplots(figsize=(14.2, 5.4))
    axes.set_title("Choosing a format: each one against what matters", pad=86)
    parallel = [FORMATS[f["format"]][0] for f in formats if f.get("parallel")]
    parallel_text = ("" if not parallel else
                     f": the faster of one image at a time and {WORKERS_TEXT(formats)} at a time"
                     if len(parallel) == len(formats)
                     else f" ({' and '.join(parallel)}: several images at a time)")
    subtitle(axes, f"Measured on {data['frames']} frames of {data['width']}x{data['height']}: size, times and loss. The "
             "times are an image's share of the whole sequence"
             + parallel_text + ".\nFrom public "
             "sources (see format-support): the programs that open the file as an image with nothing added "
             "(browsers: web browsers, Windows: Windows Photos,\nmacOS: macOS Preview, Pillow: Python's image library, "
             "FFmpeg), and the websites using it. Each cell is shaded by how the format ranks on that criterion,\n"
             "darkest the best, lightest the worst; equal values share a shade.", y=1.12)
    columns = len(criteria)
    for column, (_, values, write, better) in enumerate(criteria):
        for row, (value, shade) in enumerate(zip(values, shades(values, better))):
            axes.add_patch(Rectangle((column + 0.04, row + 0.06), 0.92, 0.88, facecolor=shade, edgecolor="none"))
            text = write(value, keys[row])
            axes.text(column + 0.5, row + 0.5, text, ha="center", va="center", fontsize=8.5 if "\n" in text else 9.5,
                      fontweight="bold" if shade == RANK_SHADES[-1] else "normal",
                      color=SURFACE if shade in RANK_SHADES[-2:] else INK)
    for row, key in enumerate(keys):
        axes.text(columns + 0.12, row + 0.5, CHOOSE_WHEN[key], ha="left", va="center", fontsize=8.5,
                  color=INK_SECONDARY)
    # The line between the lossless formats and the lossy ones.
    first_lossy = next(i for i, f in enumerate(formats) if not f["exact"])
    axes.axhline(first_lossy, xmin=0, xmax=1, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    axes.set_xlim(0, columns + 2.9)
    axes.set_ylim(len(keys), 0)
    axes.set_xticks([c + 0.5 for c in range(columns)] + [columns + 1.35],
                    [title for title, *_ in criteria] + ["Choose it when"])
    axes.set_yticks([r + 0.5 for r in range(len(keys))],
                    [f"{FORMATS[k][0]}\n{'exact' if f['exact'] else 'converted'}" for k, f in zip(keys, formats)])
    axes.xaxis.tick_top()
    axes.tick_params(length=0, labelcolor=INK, labelsize=9, pad=6)
    axes.grid(False)
    for side in axes.spines.values():
        side.set_visible(False)
    axes.legend(handles=[Patch(facecolor=RANK_SHADES[-1], label="best of the six"),
                         Patch(facecolor=RANK_SHADES[0], edgecolor=MUTED, linewidth=0.5, label="worst of the six")],
                loc="upper left", bbox_to_anchor=(0, -0.02), ncol=2, borderaxespad=0)
    save(figure, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timestamps", type=Path, default=WORKSPACE / "datasets/regular/batch_060/timestamps.csv")
    parser.add_argument("--preset", type=int, default=5)
    parser.add_argument("--crf", type=int, default=18)
    args = parser.parse_args()

    sequence = report.sequence_name(args.timestamps)
    encoding = Path(report.sequence_dir(sequence)) / f"preset_{args.preset}_crf_{args.crf}"
    source = WORKSPACE / "output" / encoding / f"{TOPIC}.images.json"
    results = WORKSPACE / "image-results" / encoding / "server"
    results.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, results / "images.json")
    data = json.loads(source.read_text())
    upload = upload_report(encoding)
    # The encoder's working time: its time minus its waits for decoded PNGs.
    data["video_encode_seconds"] = upload["encode_seconds"] - upload["encode_wait_seconds"]
    data["input_bytes"] = upload["png_bytes"]
    quality = source.with_name(f"{TOPIC}.images-quality.json")
    if quality.exists():
        shutil.copyfile(quality, results / "images-quality.json")
        measured = {q["format"]: q["images"] for q in json.loads(quality.read_text())["formats"]}
        for f in data["formats"]:
            if f["format"] in measured:
                f["butteraugli"] = measured[f["format"]]
    data["video_butteraugli"] = video_butteraugli(encoding)
    references = {"encode": data["video_encode_seconds"], "decode": data["video_decode_seconds"]}
    n = data["frames"]
    settings = f"preset {args.preset} CRF {args.crf}"
    context = f"{sequence}, {settings}: {n} frames of {data['width']}x{data['height']}, 8-bit YUV 4:2:0."

    formats = data["formats"]
    bars = [(FORMATS[f["format"]][0], f["bytes"] / MIB, FORMATS[f["format"]][1]) for f in formats]
    sizes = [data["input_bytes"], data["video_bytes"], *(f["bytes"] for f in formats)]
    bar_chart("Size of the images", f"{context}\nAll {n} images of each format, against the {n} input PNGs the "
              "video was made from and the AV1 video.", "MiB",
              [(INPUT_LABEL, data["input_bytes"] / MIB, INPUT), ("AV1 video", data["video_bytes"] / MIB, REFERENCE),
               *bars], results / "filesize-comparison.svg", 0, width=1.6 * (len(formats) + 2) + 1.4,
              extras=["the input", *(against_input(size, data["input_bytes"]) for size in sizes[1:])],
              groups=[(0, 1, "references"), *grouped_bars(formats, 2)])
    bar_chart("Encoding time per image", f"{context}\n" + bold("From the frame in memory to the file written, "
              f"mean of the {n} images. Lower is faster."), "ms",
              [(FORMATS[f["format"]][0], ms(f["encode_seconds"]) / n, FORMATS[f["format"]][1]) for f in formats],
              results / "encode-time-per-image.svg", 0, groups=grouped_bars(formats))
    bar_chart("Decoding time per image", f"{context}\n" + bold("From the file to the frame in memory, mean of the "
              f"{n} images. Lower is faster."), "ms",
              [(FORMATS[f["format"]][0], ms(f["decode_seconds"]) / n, FORMATS[f["format"]][1]) for f in formats],
              results / "decode-time-per-image.svg", 0, groups=grouped_bars(formats))
    bar_chart("What each format lost", f"{context}\nRMS error of a sample, Y, U and V together, over all the "
              "images, in 8-bit levels. The exact formats lose nothing. Lower is closer.", "levels",
              [(FORMATS[f["format"]][0], 0 if f["exact"] else rms_error_of(f), FORMATS[f["format"]][1])
               for f in formats], results / "loss-rms.svg", 3, width=11,
              label=lambda value: "exact" if value == 0 else f"{levels(value)} levels", groups=grouped_bars(formats),
              bands=PIXEL_ERROR)
    # The RMS chart's name before butteraugli.
    (results / "loss.svg").unlink(missing_ok=True)
    if all(butteraugli_of(f, "butteraugli_max") is not None for f in formats):
        for norm, key, what, file in NORMS:
            bar_chart(f"How far the images look from their frames: butteraugli {norm}",
                      f"{context}\nButteraugli {norm}, {what}, mean of the {n} images. The exact formats are their "
                      "frames: 0. Lower is closer" + (": below 1.0, no visible difference." if key == "butteraugli_max"
                                                      else "."),
                      "", [(FORMATS[f["format"]][0], butteraugli_of(f, key), FORMATS[f["format"]][1]) for f in formats],
                      results / f"{file}.svg", 4, width=11, groups=grouped_bars(formats),
                      label=lambda value: "exact" if value == 0 else f"{value:.4f}",
                      bands=BUTTERAUGLI_MAX if key == "butteraugli_max" else None)
    for step, (*_, key) in STEPS.items():
        bars, exact = [], []
        for f in formats:
            for bar in sequence_bars(f, key, FORMATS[f["format"]][0], FORMATS[f["format"]][1]):
                bars.append(bar)
                exact.append(f)
        total_chart(step, context, references[step], bars, n, results / f"{step}-time.svg", 1.25 * (len(bars) + 1) + 1.4,
                    groups=grouped_bars(exact, 1))

    for f in formats:
        name = FORMATS[f["format"]][0]
        # The format in bold, then its settings, under the batch's context.
        note = f"{context}\n{bold(name)}: {f['settings']}."
        folder = results / group_of(f)[0] / f["format"]
        folder.mkdir(parents=True, exist_ok=True)
        # The format's folder before the groups.
        shutil.rmtree(results / f["format"], ignore_errors=True)
        images = f["images"]
        frame_chart(f"{name}: size of each image", note, [i["bytes"] / MIB for i in images], "MiB", 2,
                    folder / "filesize.svg")
        bar_chart(f"{name}: size of all the images", f"{note}\nAll {n} images, against the {n} input PNGs the "
                  "video was made from and the AV1 video.", "MiB",
                  [(INPUT_LABEL, data["input_bytes"] / MIB, INPUT), ("AV1 video", data["video_bytes"] / MIB, REFERENCE),
                   (f"{name}\n{n} images", f["bytes"] / MIB, FRAME_BARS)], folder / "filesize-total.svg", 0,
                  width=7.6, extras=["the input", *(against_input(size, data["input_bytes"])
                                                     for size in (data["video_bytes"], f["bytes"]))])
        frame_chart(f"{name}: encoding time of each image",
                    f"{note}\nFrom the frame in memory to the file written. Lower is faster.",
                    [ms(i["encode_seconds"]) for i in images], "ms", 0, folder / "encode-time-per-image.svg")
        frame_chart(f"{name}: decoding time of each image",
                    f"{note}\nFrom the file to the frame in memory. Lower is faster.",
                    [ms(i["decode_seconds"]) for i in images], "ms", 0, folder / "decode-time-per-image.svg")
        for step, (*_, key) in STEPS.items():
            total_chart(step, note, references[step], sequence_bars(f, key, f"{name}\n{n} images", FRAME_BARS), n,
                        folder / f"{step}-time.svg", 7.6 if not f.get("parallel") else 9.2, name)
        # The line chart of both times, before the bar charts.
        (folder / "time.svg").unlink(missing_ok=True)
        if f.get("parallel"):
            p, color = f["parallel"], FORMATS[f["format"]][1]
            one, many = "one at a time", f"{p['workers']} at a time"
            bar_chart(f"{name}: all the images, one at a time and {p['workers']} at a time",
                      f"{note}\nWall time to write all {n} images, then to read them all back. Lower is faster.\n"
                      "One image takes as long either way; several at a time use the cores one leaves idle.", "s",
                      [(f"Encoding\n{one}", f["encode_seconds"], FRAME_BARS), (f"Encoding\n{many}", p["encode_seconds"], color),
                       (f"Decoding\n{one}", f["decode_seconds"], FRAME_BARS), (f"Decoding\n{many}", p["decode_seconds"], color)],
                      folder / "parallel.svg", 2, width=8.4,
                      extras=["", speedup(f["encode_seconds"], p["encode_seconds"]), "",
                              speedup(f["decode_seconds"], p["decode_seconds"])],
                      groups=[(0, 1, "encoding"), (2, 3, "decoding")])
        if not f["exact"]:
            frame_chart(f"{name}: what each image lost", f"{note}\nRMS error of a sample, Y, U and V together, in "
                        "8-bit levels. Lower is closer.", [rms_error(i["loss"]) for i in images], "levels",
                        decimals([rms_error(i["loss"]) for i in images]), folder / "loss-per-image.svg",
                        PIXEL_ERROR)
            for norm, key, what, file in NORMS if "butteraugli" in f else []:
                frame_chart(f"{name}: butteraugli {norm} of each image", f"{note}\nButteraugli {norm}, {what}: how "
                            "far the image looks from its frame. Lower is closer.", [q[key] for q in f["butteraugli"]],
                            "", 4, folder / f"{file}-per-image.svg",
                            BUTTERAUGLI_MAX if key == "butteraugli_max" else None)
        raw = next(r for r in formats if r["format"] == "raw")
        (folder / "README.md").write_text(format_readme(sequence, settings, data, f, raw))

    for exact in (True, False):
        (results / GROUPS[exact][0] / "README.md").write_text(group_readme(exact, sequence, settings, data))
    if all("butteraugli" in f or f["exact"] for f in formats):
        choose_chart(data, results / "choosing-a-format.svg")
    (results / "README.md").write_text(overview(sequence, settings, args, data))
    print(f"wrote {results}")


if __name__ == "__main__":
    main()
