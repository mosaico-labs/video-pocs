#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""Charts and explains the image formats the analysis wrote the FFV1 frames
in (`cargo run --release`, see src/main.rs; the formats are in
src/formats.rs), each against FFV1, the reference, in the tracked results/
folder:

    images.json                  the analysis' report: every image's size and
                                 times, and what it lost
    images-quality.json          the analysis' butteraugli of every image of
                                 the formats that are not exact
    README.md                    the formats, how they were measured, each
                                 against FFV1 with the charts, what they show
    filesize-comparison.svg      the images of every format, in all, FFV1 first
    conversion-time.svg          converting an image from FFV1: reading the
                                 FFV1 frame, then writing the image
    encode-time-per-image.svg    writing one image, per format, FFV1 first
    decode-time-per-image.svg    reading one image back, per format, FFV1 first
    encode-time.svg              writing all the images, per format, one and
                                 several at a time, FFV1 first
    decode-time.svg              reading all the images back, the same way
    loss-rms.svg                 what each format lost (RMS error), over all
                                 the images
    butteraugli-max.svg          how far the images look from their frames,
    butteraugli-3norm.svg        butteraugli's max-norm and 3-norm, per format
    reference/ffv1/              FFV1: its README and charts, frame by frame
    lossless/README.md           raw, TIFF and AVIF, which keep the frames
                                 exactly, against FFV1
    lossy/README.md              PNG and JPEG, which convert them, against FFV1:
                                 what they lose, the measures of loss explained
    <group>/<format>/README.md   the format: what it is, its numbers against
                                 FFV1, and its charts, each explained
    <group>/<format>/filesize.svg                   the size of each image
    <group>/<format>/filesize-total.svg             all the images, against FFV1
    <group>/<format>/conversion-time-per-image.svg  converting each image from FFV1
    <group>/<format>/encode-time-per-image.svg      the time to write each image
    <group>/<format>/decode-time-per-image.svg      the time to read each image back
    <group>/<format>/encode-time.svg                writing all the images,
    <group>/<format>/decode-time.svg                reading them all back, against FFV1
    <group>/<format>/parallel.svg                   all the images, one and
                                                    several at a time
    <group>/<format>/loss-per-image.svg             lossy only: what each image lost
    <group>/<format>/butteraugli-max-per-image.svg     lossy only: butteraugli
    <group>/<format>/butteraugli-3norm-per-image.svg   of each image

The charts at the top compare every format with FFV1, a dashed line parting
FFV1 from the lossless group and that from the lossy one.

The exact formats hold the frames as they are, 8-bit YUV 4:2:0, losslessly:
the analysis checks each image against its frame. PNG and JPEG convert them,
losing as little as each allows; the analysis measures what each image lost
(RMS error) and how far each image looks from its frame (butteraugli); the
charts and the READMEs show both.

    uv run scripts/image_formats.py [--results DIR]
"""

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.transforms import blended_transform_factory, offset_copy  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent.parent
RESULTS = WORKSPACE / "results"
MIB = 1048576

# One visual system for every chart: light surface, recessive hairline grid,
# text in ink colors (never in a series color).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
# The bars of the per-frame charts.
RECEIVING = "#5a6e8c"
# The bands of what the eye sees, light (little lost) to dark (much lost).
BANDS = ["#f7f5ee", "#e8e2cf", "#d8cdac", "#c6b78b", "#b19f6d"]
# Validated categorical slots (dataviz's validate_palette.js).
PRESETS = ["#2a78d6", "#1f9e89", "#7a5cc7", "#e3a21a", "#d6456b", "#5a6e8c"]

plt.rcParams.update({
    "font.family": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK_SECONDARY,
    "axes.titlecolor": INK,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    # A faint dark grid, visible on the light background and on the darkest
    # band alike.
    "grid.color": INK,
    "grid.alpha": 0.14,
    "grid.linewidth": 0.75,
    "xtick.color": INK_SECONDARY,
    "ytick.color": INK_SECONDARY,
    "xtick.labelcolor": INK_SECONDARY,
    "ytick.labelcolor": INK_SECONDARY,
    "ytick.left": False,
    "lines.linewidth": 1.5,
    "lines.solid_capstyle": "round",
    "lines.solid_joinstyle": "round",
    "legend.frameon": False,
    "legend.fontsize": 8,
    "legend.labelcolor": INK_SECONDARY,
    # The results are tracked by git: the same data gives the same file.
    "svg.hashsalt": "ffv1-analysis",
})
# Bold text in the charts (see in_bold()) is math text: in the charts' own
# font, not math text's.
_FONT = font_manager.FontProperties(
    fname=font_manager.findfont(font_manager.FontProperties(family=plt.rcParams["font.family"]))).get_name()
plt.rcParams.update({"mathtext.fontset": "custom", "mathtext.rm": _FONT, "mathtext.bf": f"{_FONT}:bold"})


def in_bold(text):
    """`text` in bold inside a label, as math text."""
    return "$\\mathbf{" + text.replace(" ", "\\ ").replace("%", "\\%") + "}$"


def save(figure, path):
    figure.savefig(path, metadata={"Date": None}, bbox_inches="tight", pad_inches=0.2)
    plt.close(figure)


def subtitle(axes, text, y=1.03):
    axes.text(0, y, text, transform=axes.transAxes, color=INK_SECONDARY, fontsize=9)


@dataclass
class Loss:
    """A measure of loss and its bands of what the eye sees: (lower bound,
    label of the band from it up to the next bound), from 0."""

    name: str
    bands: list


def pixel_error(db):
    """An RMS error in levels of an 8-bit value, from a PSNR in dB."""
    return 255 * 10 ** (-db / 20)


# Where the eye stops seeing an RMS error: Afnan et al., "Subjective
# Assessment of Objective Image Quality Metrics Range Guaranteeing Visually
# Lossless Compression", Sensors 23(3):1297, 2023, doi:10.3390/s23031297,
# Table 4. 25 viewers flicker-compared 10 images with JPEG versions of
# falling quality, on a 4K display at 1:1, and the metric was taken where
# each viewer stopped seeing a difference. The study's PSNR on Y, U and V
# (50.64, 42.08 and 32.95 dB) at its best, mean and worst make four bands:
# below the strictest no viewer saw it on any image, above the loosest every
# viewer saw it on every image.
PIXEL_ERROR = Loss("pixel-error", [(0, "invisible in every test"), (pixel_error(50.64), "invisible on average"),
                                   (pixel_error(42.08), "visible on average"),
                                   (pixel_error(32.95), "visible in every test")])
# Google's three classes of a butteraugli max-norm (butteraugli README); the
# 1.0 boundary is its ButteraugliFuzzyClass's, the 2.0 one the crate's
# BUTTERAUGLI_BAD.
BUTTERAUGLI_MAX = Loss("butteraugli-max", [(0, "great"), (1, "acceptable"), (2, "not acceptable")])


def draw_bands(axes, loss, top):
    """Shades the bands of what the eye sees up to `top`, with a dashed
    line where one ends and the next starts."""
    bounds = [b for b, _ in loss.bands] + [float("inf")]
    for i, (bound, _) in enumerate(loss.bands):
        if bound >= top:
            break
        axes.axhspan(bound, min(bounds[i + 1], top), color=BANDS[i], zorder=0, linewidth=0)
        if i > 0:
            axes.axhline(bound, color=INK, linewidth=1.1, linestyle=(0, (4, 3)), zorder=1.5)


def band_key(loss):
    """One legend entry per band, with its range: every band is named, even
    one too thin to see."""
    bounds = [b for b, _ in loss.bands] + [None]
    return [Patch(facecolor=BANDS[i], edgecolor=MUTED, linewidth=0.5,
                  label=f"{bound:.3g} to {upper:.3g}: {name}" if upper is not None else f"above {bound:.3g}: {name}")
            for i, ((bound, name), upper) in enumerate(zip(loss.bands, bounds[1:]))]


# FFV1, the frames as the analysis gets them: every other format is written
# from it and compared with it.
REF = "ffv1"
# One color per format, in a fixed order (the validated categorical slots);
# FFV1, the reference, is the darkest neutral.
FORMATS = {
    "ffv1": ("FFV1", INK_SECONDARY),
    "raw": ("Raw I420", PRESETS[0]),
    "tiff": ("TIFF (YCbCr 4:2:0)", PRESETS[1]),
    "avif": ("AVIF (lossless)", PRESETS[2]),
    "png": ("PNG (16-bit RGB)", PRESETS[4]),
    # Validated with the slots before it (dataviz's validate_palette.js).
    "jpeg": ("JPEG (quality 100)", "#4f7a28"),
}


def tint(color, share=0.35):
    """`color` mixed with the surface, `share` of it kept: the lighter part
    of a bar, reading the FFV1 frame, in its format's own hue."""
    rgb = matplotlib.colors.to_rgb(color)
    surface = matplotlib.colors.to_rgb(SURFACE)
    return tuple(share * c + (1 - share) * s for c, s in zip(rgb, surface))


# What each format is, for its README.
ABOUT = {
    "ffv1": "The input: each frame of the video as one FFV1 frame (version 3, 16 slices coded in parallel) in its "
            "own Matroska file, with FFmpeg's own encoder and decoder. FFV1 is a lossless, intra-only video codec "
            "made for archiving; it keeps the YUV 4:2:0 frame and its color tags. It is not an image format: "
            "reading the files needs FFmpeg or another FFV1 decoder. Every other format is written from these "
            "frames and compared with them; to time FFV1's own writing, each frame is also written again as FFV1.",
    "raw": "The frame's three planes written as they are, Y then U then V (I420), with no header and no "
           "compression. Writing and reading an image are a copy to and from a file, and every image has the same "
           "size, width x height x 1.5 bytes. The reader needs the size and the color tags from elsewhere (here, "
           "`images.json`).",
    "tiff": "Each frame as a YCbCr TIFF with 4:2:0 subsampling, the planes as they are, Deflate compressed, "
            "written and read with FFmpeg's TIFF encoder and decoder. Every FFmpeg TIFF compression (none, "
            "PackBits, LZW, Deflate) gave the frame back exactly on a test frame; Deflate was the smallest (11.8 MB, "
            "against 53.1 MB with none, 46.8 MB with PackBits, 27.8 MB with LZW), so it is the one measured. YCbCr "
            "TIFF is part of the TIFF 6.0 standard, but many programs only read RGB TIFF. FFmpeg's encoder writes "
            "no `YCbCrCoefficients` tag, so by the standard's default the file says BT.601, not the frames' BT.709, "
            "and black at 15, not 16: the samples are exact, and FFmpeg reads them back as they are, but a reader "
            "that converts to RGB by the tags shows slightly wrong colors (Pillow: up to 11 levels on frame 0).",
    "avif": "Each frame encoded on its own by SVT-AV1 in lossless, still-picture mode, in an AVIF file written by "
            "FFmpeg's AVIF muxer, and read back with dav1d. AVIF is a standard image format: any AVIF reader opens "
            "these files, which keep the YUV 4:2:0 frame and its color tags (BT.709, limited range). Lossless AV1 "
            "is slow at this size, in both directions; the SVT-AV1 preset does not change a lossless image.",
    "png": "PNG holds only RGB, so each frame is converted to it, as closely as PNG allows. 16-bit RGB, so that "
           "rounding loses next to nothing, with each U and V sample repeated over the 2x2 pixels it covers, so "
           "that reading the image back and averaging each 2x2 block gives the sample again. What is lost is "
           "mostly the colors a YUV frame can hold but RGB can't, outside the RGB cube, clipped to it. Tagged "
           "sRGB, so any viewer shows it right; written and read with the `png` crate (one thread).",
    "jpeg": "A standard JFIF file any viewer shows in the right colors, as close to the frame as JPEG allows. "
            "JPEG's YCbCr is BT.601 in full range, so each frame is converted to it from BT.709 in limited range, "
            "then written at quality 100 (every quantization step 1, the least JPEG can lose) in 4:2:0, its U and "
            "V planes passed as they are, with no RGB or resampling in the encoder (`jpeg-encoder` crate). It is "
            "read back with FFmpeg's decoder, which also gives the 4:2:0 planes as they are, then converted back. "
            "What is lost: the rounding of the two conversions and of the DCT.",
}
# The bars of the per-frame charts: one neutral slate for every format,
# apart from the formats' own colors.
FRAME_BARS = RECEIVING


def bold(text):
    """`text` in bold, as in_bold(), spaced as plain text: math
    text adds space after a comma, and its period is followed by a wider
    space than a word's, so a period ends with a thin space instead, and a
    colon is spaced as a relation, so it is braced like the comma. A hyphen
    would be a minus sign: it is a Unicode hyphen instead."""
    text = text.replace(",", "{,}").replace(":", "{:}").replace("-", "\u2010")
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


def with_bands(axes, bands, top, below):
    """Shades `bands` (a Loss: its bands of what the eye sees) behind the bars up to `top`, showing where the first band
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


# mosaicod's default limit for one message it receives: an image uploaded in
# one message must fit in it.
MESSAGE_LIMIT = 50 * MIB

# What one DoGet message may carry when mosaicod serves a topic back
# (its GRPC_MSG_MAX_SIZE_BYTES).
DOWNLOAD_LIMIT = 512 * MIB


def message_text(largest):
    """Whether an image of `largest` bytes fits in one message, uploaded and
    served back."""
    up = "fits in" if largest <= MESSAGE_LIMIT else "is over"
    return (f"{up} the {MESSAGE_LIMIT / MIB:.0f} MiB a message may carry to mosaicod (its default "
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
        warm_text = ("" if not warm else "; frame 0 is a warm-up" if warm == 1
                     else f"; frames 0–{warm - 1} are a warm-up")
        spikes = ("single spikes (each image is timed once: see [the limits](../../README.md#each-image-is-timed-once))")
        later = "" if warm == len(slow) else f", and the others {spikes}" if warm else f"; {spikes}"
        text += (f" {'One frame takes' if len(slow) == 1 else f'{len(slow)} frames take'} over 1.5x the median: "
                 f"{frames}{warm_text}{later}.")
    else:
        text += " No frame takes over 1.5x the median: the time is steady from image to image."
    return text


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
    "  How visible it is: the charts shade bands for it, from a viewer study (Afnan et al., "
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
    "**Butteraugli** is Google's perceptual metric: it "
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


def rms_band(value):
    """The video results' band of an RMS error (see PIXEL_ERROR)."""
    return [name for bound, name in PIXEL_ERROR.bands if value >= bound][-1]


def butteraugli_class(score):
    """Google's class of a butteraugli max-norm."""
    return "great" if score < 1 else "acceptable" if score <= 2 else "not acceptable"


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




REFERENCE_GROUP = ("reference", "Reference", "the frames as the analysis gets them")


def group_of(f):
    return REFERENCE_GROUP if f["format"] == REF else GROUPS[f["exact"]]


def label_of(f):
    return FORMATS[f["format"]][0]


def color_of(f):
    return FORMATS[f["format"]][1]


def and_list(names):
    """`a`, `a and b`, `a, b and c`."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def vs_size(size, ref):
    """How a size compares with FFV1's: "2.9x larger", "1.3x smaller"."""
    if abs(size - ref) <= 0.005 * ref:
        return "the same"
    return f"{size / ref:.1f}x larger" if size > ref else f"{ref / size:.1f}x smaller"


def vs_time(seconds, ref):
    """How a time compares with FFV1's: "2.9x slower", "4.8x faster", or
    "the same" within 5%."""
    if abs(seconds - ref) <= 0.05 * ref:
        return "the same"
    return f"{seconds / ref:.1f}x slower" if seconds > ref else f"{ref / seconds:.1f}x faster"


def conversions(f, ref):
    """Converting each frame from FFV1 to `f`'s format, in seconds: reading
    the FFV1 frame, then writing the image."""
    return [r["decode_seconds"] + i["encode_seconds"] for r, i in zip(ref["images"], f["images"])]


def conversion_seconds(f, ref):
    """Converting all the frames from FFV1 to `f`'s format, one at a time."""
    return ref["decode_seconds"] + f["encode_seconds"]


# The two steps timed: (title word, what is timed, images.json key).
STEPS = {
    "encode": ("Writing", "written from the frame in memory, file included", "encode_seconds"),
    "decode": ("Reading", "read back into a frame in memory, file included", "decode_seconds"),
}


def total_chart(step, note, formats, ref, n, path, width, name=None, groups=None):
    """All the images of each of `formats` for `step`, one at a time and,
    hatched, several at a time, FFV1's first; each against FFV1's, the same
    way. `name` is the format's, in a chart of one format."""
    word, what, key = STEPS[step]
    bars, extras = [], []
    for f in formats:
        for bar in sequence_bars(f, key, label_of(f) if name is None else f"{label_of(f)}\n{n} images", color_of(f)):
            bars.append(bar)
            reference = ref[key] if len(bar) == 3 else ref["parallel"][key]
            extras.append("reference" if f is ref else vs_time(bar[1], reference))
    title = f"{name}: {word.lower()} all the images, against FFV1" if name else f"{word} all the images"
    bar_chart(title, f"{note}\nAll {n} images {what}, one at a time and, hatched, several at a time,\nagainst FFV1's "
              "the same way. Lower is faster.", "s", bars, path, 2, width=width, extras=extras, groups=groups)


def conversion_chart(note, formats, ref, n, path):
    """Converting an image from FFV1 into each of `formats`, mean of the
    images: reading the FFV1 frame, stacked under writing the image."""
    read = ms(ref["decode_seconds"]) / n
    writes = [ms(f["encode_seconds"]) / n for f in formats]
    figure, axes = plt.subplots(figsize=(1.6 * len(formats) + 2.2, 4.6))
    note = (f"{note}\nReading the FFV1 frame (the lighter part), then writing the image in the format, mean of the "
            f"{n} images. Lower is faster.")
    axes.set_title("Converting an image from FFV1", pad=14 + 12 * (note.count("\n") + 1))
    subtitle(axes, note)
    x = range(len(formats))
    axes.bar(x, [read] * len(formats), width=0.55, color=[tint(color_of(f)) for f in formats], zorder=2)
    axes.bar(x, writes, bottom=[read] * len(formats), width=0.55, color=[color_of(f) for f in formats], zorder=2)
    top = read + max(writes)
    for i, write in enumerate(writes):
        axes.annotate(f"{read + write:,.0f} ms\nwriting {write:,.0f} ms", (i, read + write), xytext=(0, 4),
                      textcoords="offset points", ha="center", va="bottom", color=INK, fontweight="bold")
    axes.set_xticks(x, [label_of(f) for f in formats])
    axes.set_ylim(0, top * 1.2)
    axes.set_ylabel("ms")
    axes.grid(axis="x", visible=False)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=9, length=0)
    draw_groups(axes, grouped_bars(formats), len(formats))
    axes.legend(handles=[Patch(facecolor=tint(INK_SECONDARY), label=f"lighter: reading the FFV1 frame, {read:.0f} ms"),
                         Patch(facecolor=INK_SECONDARY, label="darker: writing the image")], loc="upper left")
    save(figure, path)


def totals_table(step, formats, ref, n):
    """The rows of the table of all the images of each format against FFV1,
    FFV1 first."""
    word, _, key = STEPS[step]
    rows = [f"| | {word} all {n} images | Against FFV1 |", "|---|---|---|"]
    for f in formats:
        name = label_of(f)
        rows.append(f"| {'*' + name + ', the reference*' if f is ref else name} | {f[key]:.2f} s | "
                    f"{'–' if f is ref else vs_time(f[key], ref[key])} |")
        if f.get("parallel"):
            seconds, workers = f["parallel"][key], f["parallel"]["workers"]
            rows.append(f"| {name}, {workers} images at a time | {seconds:.2f} s | "
                        f"{'–' if f is ref else vs_time(seconds, ref['parallel'][key])} |")
    return rows


def butteraugli_table(formats):
    """The rows of the table of butteraugli per format."""
    rows = ["| Format | Max-norm, mean | Max-norm, worst image | 3-norm, mean |", "|---|---|---|---|"]
    for f in formats:
        if "butteraugli" in f:
            worst = max(range(len(f["butteraugli"])), key=lambda k: f["butteraugli"][k]["butteraugli_max"])
            rows.append(f"| {label_of(f)} | {butteraugli_of(f, 'butteraugli_max'):.4f} | "
                        f"{f['butteraugli'][worst]['butteraugli_max']:.4f} (frame {worst}) | "
                        f"{butteraugli_of(f, 'butteraugli_3norm'):.4f} |")
    return rows


def butteraugli_section(formats, more=None):
    """The README section on butteraugli, if measured."""
    if not any("butteraugli" in f for f in formats):
        return []
    # The full explanation once, in the lossy README; a line and a link elsewhere.
    terms = BUTTERAUGLI_TERMS if not more else (
        "**Butteraugli**, Google's perceptual metric, says how visible the difference between each image and its "
        "frame is: its max-norm is the worst spot (below 1.0 no visible difference, above 2.0 not acceptable), "
        f"its 3-norm the whole image. The [lossy README]({more}) explains both.")
    lines = ["### How far they look: butteraugli", "", terms, "",
             "The exact formats are their frames, byte for byte: their distance is 0, not measured.", "",
             *butteraugli_table(formats), ""]
    if more:
        lines += ["Each image's scores are in the lossy formats' folders.", ""]
    if not any(f["exact"] for f in formats):
        return lines
    return lines + ["![Butteraugli max-norm of each format](butteraugli-max.svg)", "",
                    "![Butteraugli 3-norm of each format](butteraugli-3norm.svg)", ""]


def butteraugli_finding(formats):
    """The finding on butteraugli, if measured: each lossy format's class on
    Google's scale."""
    lossy = [f for f in formats if "butteraugli" in f]
    if not lossy:
        return []
    parts = []
    for f in lossy:
        scores = [q["butteraugli_max"] for q in f["butteraugli"]]
        classes = sorted({butteraugli_class(v) for v in scores}, key=["great", "acceptable", "not acceptable"].index)
        parts.append(f"{label_of(f)} scores {butteraugli_of(f, 'butteraugli_max'):.4f} on average, "
                     f"{min(scores):.4f} to {max(scores):.4f} (max-norm): "
                     + (f"*{classes[0]}* for every image" if len(classes) == 1 else
                        "its images range from *" + "* to *".join([classes[0], classes[-1]]) + "*"))
    classes = {butteraugli_class(butteraugli_of(f, "butteraugli_max")) for f in lossy}
    spread_text = "; ".join(
        f"{label_of(f)} changes {sum(total_loss(f['images'])[p][3] for p in PLANES) / sum(i['loss'][p]['samples'] for i in f['images'] for p in PLANES):.2%} "
        f"of its samples, by at most {max(total_loss(f['images'])[p][1] for p in PLANES)} levels" for f in lossy)
    lines = [f"- **Butteraugli puts the lossy formats in {'different classes' if len(classes) > 1 else 'the same class'}**: "
             + "; ".join(parts)
             + ". Its max-norm is the single most visible spot of an image, so small errors spread over many "
             f"samples leave a visible spot somewhere, while a few changed samples don't ({spread_text})."]
    disagree = [f for f in lossy if rms_band(rms_error_of(f)).startswith("invisible")
                and butteraugli_class(butteraugli_of(f, "butteraugli_max")) != "great"]
    if disagree:
        lines.append(
            "- **The two metrics disagree on " + " and ".join(label_of(f) for f in disagree) + "**: by its "
            "RMS error, " + "; ".join(f"{levels(rms_error_of(f))} levels, it is *{rms_band(rms_error_of(f))}* in the "
                                     "viewer study" for f in disagree)
            + ", while butteraugli's max-norm calls it *" + butteraugli_class(butteraugli_of(disagree[0], "butteraugli_max"))
            + "*. They measure different things: the study's bands judge the error on average over the image, as "
            "its viewers saw whole images, while the max-norm judges the single worst spot, by a model of the eye "
            "meant to be strict. The truth is likely between: invisible to a viewer looking at the image, a "
            "difference a model or a close look at the right spot can find.")
    return lines


GENERATED = ("Generated by `scripts/image_formats.py` from the analysis' report (`cargo run --release`, "
             "[`images.json`]({up}images.json)); do not edit.")


def numbers_table(f, ref, raw, n):
    """The table of a format's numbers, against FFV1."""
    sizes = [i["bytes"] / MIB for i in f["images"]]
    vs_raw = "" if f is raw else f"; raw: {raw['bytes'] / f['bytes']:.1f}x smaller"
    read = ms(ref["decode_seconds"]) / n
    write = ms(f["encode_seconds"]) / n
    rows = ["| | All the images | Per image (mean) | Against |", "|---|---|---|---|",
            f"| Size | {f['bytes'] / MIB:,.1f} MiB | {sum(sizes) / n:.2f} MiB | FFV1: {vs_size(f['bytes'], ref['bytes'])} "
            f"({ref['bytes'] / MIB:,.1f} MiB){vs_raw} |"]
    if f is not ref:
        rows.append(f"| Converting from FFV1 | {conversion_seconds(f, ref):.2f} s | {read + write:.0f} ms | reading the "
                    f"FFV1 frame ({read:.0f} ms), then writing the image ({write:.0f} ms) |")
    rows += [f"| Writing (encoding) | {f['encode_seconds']:.2f} s | {write:.0f} ms | "
             + ("the reference |" if f is ref else f"FFV1: {vs_time(f['encode_seconds'], ref['encode_seconds'])} "
                                                  f"({ref['encode_seconds']:.2f} s) |"),
             f"| Reading (decoding) | {f['decode_seconds']:.2f} s | {ms(f['decode_seconds']) / n:.0f} ms | "
             + ("the reference |" if f is ref else f"FFV1: {vs_time(f['decode_seconds'], ref['decode_seconds'])} "
                                                  f"({ref['decode_seconds']:.2f} s) |"),
             "| Lost | " + ("nothing: every image is exact | | |" if f["exact"] else
                           f"RMS error {levels(rms_error_of(f))} levels | | FFV1: nothing |")]
    return rows


def format_readme(sequence, f, ref, raw, n):
    """The README of one format's folder: what it is, its numbers against
    FFV1, and its charts, each explained."""
    key = f["format"]
    label = label_of(f)
    sizes = [i["bytes"] / MIB for i in f["images"]]
    encodes = [ms(i["encode_seconds"]) for i in f["images"]]
    decodes = [ms(i["decode_seconds"]) for i in f["images"]]
    size_mean, _, (size_low, size_low_k), (size_high, size_high_k) = spread(sizes)
    is_ref = f is ref
    if size_high == size_low:
        size_text = (f"Every image is {size_mean:.2f} MiB: the format does not compress, so the size depends only "
                     "on the frame's width and height, not on what it shows.")
    else:
        size_text = (f"The images take {size_mean:.2f} MiB on average, from {size_low:.2f} MiB (frame "
                     f"{size_low_k}) to {size_high:.2f} MiB (frame {size_high_k}): a frame with more detail takes "
                     "more bytes.")
    part = ("The reference of [the comparison of the image formats](../../README.md): every other format is "
            "written from these frames." if is_ref else
            "One of the [" + group_of(f)[1].lower() + " formats](../README.md), in [the comparison of the image "
            "formats](../../README.md): each one written from the FFV1 frames, and compared with them.")
    lines = [
        f"# {label}: {sequence}",
        "",
        GENERATED.format(up="../../") + " " + part,
        "",
        "## The format",
        "",
        ABOUT[key],
        "",
        f"Settings: {f['settings']}. The files are `output/{key}/frame_<k>.*`, one per frame, {n} here, "
        + ("and each one was read back and checked byte for byte against its frame: none of them loses anything."
           if f["exact"] else
           "and each one was read back and compared with its frame: see [what it loses](#what-it-loses)."),
        "",
        "## Its numbers",
        "",
        *numbers_table(f, ref, raw, n),
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
    ]
    if not is_ref:
        converts = [ms(t) for t in conversions(f, ref)]
        lines += [
            "## Size of all the images, against FFV1",
            "",
            f"All {n} images, against the {n} FFV1 frames they were written from. They take {f['bytes'] / MIB:,.1f} "
            f"MiB, {vs_size(f['bytes'], ref['bytes'])} than FFV1's {ref['bytes'] / MIB:,.1f} MiB."
            .replace("the same than", "the same as"),
            "",
            f"![{label}: size of all the images, against FFV1](filesize-total.svg)",
            "",
            "## Converting each image from FFV1",
            "",
            "One bar per frame: the time to read its FFV1 frame and write its image. The dashed line is the mean.",
            "",
            time_story("Converting", converts).replace("Converting an image takes", "Converting an image from FFV1 takes"),
            "",
            f"![{label}: converting each image from FFV1](conversion-time-per-image.svg)",
            "",
        ]
    lines += [
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
    ]
    if not is_ref:
        lines += [
            "",
            "## All the images, against FFV1",
            "",
            f"The time for all {n} images, against FFV1's, the reference: the gray bars, first. Each way is "
            "compared with FFV1's the same way: one image at a time, and several at a time (hatched).",
            "",
            f"**Encoding.** Writing all {n} images takes {f['encode_seconds']:.2f} s, "
            f"{vs_time(f['encode_seconds'], ref['encode_seconds'])} than FFV1's {ref['encode_seconds']:.2f} s.",
            "",
            f"![{label}: encoding all the images, against FFV1](encode-time.svg)",
            "",
            f"**Decoding.** Reading all {n} images back takes {f['decode_seconds']:.2f} s, "
            f"{vs_time(f['decode_seconds'], ref['decode_seconds'])} than FFV1's {ref['decode_seconds']:.2f} s.",
            "",
            f"![{label}: decoding all the images, against FFV1](decode-time.svg)",
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
                    "files), or already uses every core on one image, gains nothing from several at a time, and the "
                    "images only compete for the cores and the memory's bandwidth."),
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
                  "[lossy README](../README.md#how-far-they-look-butteraugli).", "", *butteraugli_table([f]), ""]
        for norm, norm_key, what, file in NORMS:
            values = [q[norm_key] for q in f["butteraugli"]]
            mean, _, (low, low_k), (high, high_k) = spread(values)
            lines += [f"One bar per frame: the {norm} of its image, {what}. The dashed line is the mean.", "",
                      f"The {norm} is {mean:.4f} on average, from {low:.4f} (frame {low_k}) to {high:.4f} "
                      f"(frame {high_k}).", "", f"![{label}: butteraugli {norm} of each image]({file}-per-image.svg)",
                      ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def compared_table(formats, ref, raw, n, link="", loss=True):
    """The rows of the table of `formats` against FFV1, FFV1 first."""
    rows = ["| Format | Per image | vs FFV1 | vs raw | Converting from FFV1, per image | Writing, per image | "
            "Reading, per image |" + (" Lost (RMS error) |" if loss else ""),
            "|---|---|---|---|---|---|---|" + ("---|" if loss else "")]
    read = ms(ref["decode_seconds"]) / n
    for f in [ref, *formats]:
        name = label_of(f)
        if link:
            name = f"[{name}]({link.format(group=group_of(f)[0], key=f['format'])})"
        if f is ref:
            name = f"*{name}, the reference*"
        lost = "nothing (exact)" if f["exact"] else f"{levels(rms_error_of(f))} levels"
        vs_ref = "–" if f is ref else vs_size(f["bytes"], ref["bytes"])
        vs_raw = "–" if f is raw else f"{raw['bytes'] / f['bytes']:.1f}x smaller"
        convert = "–" if f is ref else f"{read + ms(f['encode_seconds']) / n:.0f} ms"
        rows.append(f"| {name} | {f['bytes'] / n / MIB:.1f} MiB | {vs_ref} | {vs_raw} | {convert} | "
                    f"{ms(f['encode_seconds']) / n:.0f} ms | {ms(f['decode_seconds']) / n:.0f} ms |"
                    + (f" {lost} |" if loss else ""))
    return rows


def group_readme(exact, sequence, data, ref, raw):
    """The README of a group's folder: what its formats have in common, and
    how they compare with FFV1."""
    folder, title, common = GROUPS[exact]
    n = data["frames"]
    formats = [f for f in data["formats"] if f["exact"] == exact and f is not ref]
    read = ms(ref["decode_seconds"]) / n
    lines = [
        f"# {title} formats: {sequence}",
        "",
        GENERATED.format(up="../") + " Part of [the comparison of the image formats](../README.md), whose charts "
        "show these formats next to the " + ("lossy ones." if exact else "lossless ones."),
        "",
    ]
    if exact:
        lines += [
            f"These formats {common}, 8-bit YUV 4:2:0, BT.709 in limited range, with no conversion, like FFV1, the "
            "reference they are written from. After writing each image, the analysis reads it back and checks it "
            f"byte for byte against its frame, and stops if one differs: every one of the {n} images of every "
            "format here is exact. They differ from FFV1 only in size and speed.",
            "",
        ]
    else:
        lines += [
            f"These formats {common}: PNG holds only RGB, and JPEG holds YCbCr with the BT.601 matrix in full range, "
            "while the frames are YUV 4:2:0, BT.709 in limited range. They are the image formats every viewer opens. "
            "After writing each image from its FFV1 frame, the analysis reads it back, converts it back to the "
            "frame's YUV, and measures how far it is from its frame.",
            "",
        ]
    lines += [*compared_table(formats, ref, raw, n, link="{key}/README.md", loss=not exact), "",
              f"Converting from FFV1 is reading the FFV1 frame ({read:.0f} ms) and writing the image. Each format's "
              "README has its charts, frame by frame: the size, the conversion, encoding and decoding time of each "
              "image, all the images against FFV1" + ("." if exact else ", and what each image lost."), ""]
    # FFV1's row links to its own folder.
    lines = [line.replace(f"[{label_of(ref)}]({REF}/README.md)", f"[{label_of(ref)}](../reference/{REF}/README.md)")
             for line in lines]

    if not exact:
        lines += ["## What they lose", "",
                  "How far the images read back are from their frames, over all the frames, plane by plane. The "
                  "measures:", "", LOSS_TERMS, ""]
        for f in formats:
            lines += [f"**[{label_of(f)}]({f['format']}/README.md)**", "", *loss_table(f), ""]
        lines += [line.replace("### ", "## ", 1) if line.startswith("### ") else line
                  for line in butteraugli_section(formats)]

    lines += ["## What it shows", ""]
    for f in formats:
        convert = read + ms(f["encode_seconds"]) / n
        line = (f"- **{label_of(f)}**: {f['bytes'] / n / MIB:.1f} MiB per image, {vs_size(f['bytes'], ref['bytes'])} "
                f"than FFV1; converting an image from FFV1 takes {convert:.0f} ms, reading it back "
                f"{ms(f['decode_seconds']) / n:.0f} ms ({vs_time(f['decode_seconds'], ref['decode_seconds'])} than "
                "FFV1).").replace("the same than", "the same as")
        if not f["exact"]:
            line += f" RMS error {levels(rms_error_of(f))} levels" + (
                f", butteraugli max-norm {butteraugli_of(f, 'butteraugli_max'):.4f} on average." if "butteraugli" in f
                else ".")
        lines.append(line)
    if exact:
        largest = max(i["bytes"] for i in raw["images"])
        lines += [f"- **Raw's {largest / MIB:.1f} MiB frames**: a frame {message_text(largest)}. The compressed "
                  "formats are well within both."]
    else:
        lines += butteraugli_finding(formats)
    lines += warm_up(formats, {k: v[0] for k, v in FORMATS.items()})
    return "\n".join(lines) + "\n"


def overview(sequence, data, ref, raw, others):
    """The README of results/: every format against FFV1."""
    w, h, n = data["width"], data["height"], data["frames"]
    formats = [ref, *others]
    read = ms(ref["decode_seconds"]) / n
    name = {key: label for key, (label, _) in FORMATS.items()}
    largest = {f["format"]: max(i["bytes"] for i in f["images"]) for f in others}
    too_large = [k for k in largest if largest[k] > MESSAGE_LIMIT]
    smaller = [f for f in others if f["bytes"] < ref["bytes"]]
    closest = min(others, key=lambda f: f["bytes"])
    by_conversion = sorted(others, key=lambda f: f["encode_seconds"])
    faster_read = [f for f in others if f["decode_seconds"] < ref["decode_seconds"]]
    lossy = [f for f in others if not f["exact"]]

    def files(f):
        return f"`frame_<k>.{ {'raw': 'yuv', 'ffv1': 'mkv', 'jpeg': 'jpg'}.get(f['format'], f['format'])}`"

    lines = [
        f"# {sequence}: FFV1 frames into other image formats",
        "",
        "Generated by `scripts/image_formats.py` from the analysis' reports (`cargo run --release`, "
        "[`images.json`](images.json) and [`images-quality.json`](images-quality.json)); do not edit.",
        "",
        f"The frames are stored as FFV1, one Matroska file per frame: {n} frames of {w}x{h}, 8-bit YUV 4:2:0, "
        "BT.709 in limited range. What does it cost to turn them into another image format, and what does it give? "
        "Each frame is converted from FFV1 into the other formats, and each format is compared with FFV1: the size "
        "of the images, the time to convert an image (reading the FFV1 frame, then writing the image), and the "
        "time to read it back. The formats are in two groups, each in its own folder with its own README:",
        "",
        "- **[Lossless](lossless/README.md)**: raw, TIFF and AVIF keep the frames **exactly**, like FFV1, with no "
        "conversion. The analysis reads every image back and checks it byte for byte against its frame: they lose "
        "nothing and differ from FFV1 only in size and speed.",
        "- **[Lossy](lossy/README.md)**: PNG holds only RGB, and JPEG holds YCbCr with another matrix (BT.601) and "
        "range (full), so neither can hold the frames as they are. Each frame is converted, losing as little as "
        "the format allows, and the analysis measures how far each image read back is from its frame.",
        "",
        "In every format, nothing is dropped or repeated: one image per frame. FFV1 has [its own "
        f"folder](reference/{REF}/README.md), and each format's folder has its own charts, frame by frame.",
        "",
        "## The formats",
        "",
        "| Format | Group | Files | Settings |",
        "|---|---|---|---|",
        *(f"| [{label_of(f)}]({group_of(f)[0]}/{f['format']}/README.md) | {group_of(f)[0]} | {files(f)} | "
          f"{f['settings']} |" for f in formats),
        "",
        "FFV1 and AVIF carry the frames' color tags: BT.709 matrix, limited range, BT.709 primaries, sRGB "
        "transfer. Raw has no header, so the reader needs the size and these tags from elsewhere (here, "
        "`images.json`). TIFF has the size, but FFmpeg tags its YCbCr as BT.601 (the standard's default) with black "
        "at 15: its samples are exact, but a reader that converts it to RGB by the tags shows slightly wrong colors "
        "([TIFF](lossless/tiff/README.md#the-format)). PNG is tagged sRGB, and JPEG's colors are JFIF's by "
        "definition (BT.601, full range), so any viewer shows both right.",
        "",
        "## How it was measured",
        "",
        f"The input is `{data['input']}/`: {n} FFV1 frames ({ref['bytes'] / MIB:,.1f} MiB), written by `prepare` "
        "(see the [README](../README.md#the-input)). The analysis read every frame, then, frame after frame, wrote "
        "it in each format, FFV1 again included, and read it back.",
        "",
        "- **Converting from FFV1** is reading the FFV1 frame, then writing the image: FFV1's decoding time plus "
        "the format's encoding time, frame by frame.",
        "- **Encoding** is from the frame in memory to the file written; **decoding** from the file to the frame "
        "in memory. For PNG and JPEG, both include the color conversion.",
        "- One image at a time, each format with the threads it uses on its own; the formats run one after the "
        "other, so their times don't overlap. Then each format's whole sequence again, "
        f"{WORKERS_TEXT(formats)} images at a time (the hatched bars).",
        "- Writing and reading the file are included, but the OS keeps the files in its cache: the disk's speed "
        "is not measured.",
        "",
        "To measure again, then redraw this folder, from `ffv1-analysis/`:",
        "",
        "```sh",
        "cargo run --release --bin prepare   # the FFV1 frames, if not there yet",
        "cargo run --release                 # images.json and images-quality.json",
        "uv run scripts/image_formats.py     # the charts and the READMEs",
        "```",
        "",
        "The images go to `output/<format>/` (not tracked: "
        f"{sum(f['bytes'] for f in formats) / 1024**3:.1f} GiB here).",
        "",
        "## Every format against FFV1",
        "",
        "In every chart, FFV1, the reference, comes first, in gray, and a dashed line parts it from the lossless "
        "formats and those from the lossy ones, named under the bars.",
        "",
        *compared_table(others, ref, raw, n, link="{group}/{key}/README.md"),
        "",
        "![Size of the images](filesize-comparison.svg)",
        "",
        f"Converting an image from FFV1 is reading the FFV1 frame, {read:.0f} ms, then writing the image:",
        "",
        "![Converting an image from FFV1](conversion-time.svg)",
        "",
        "![Encoding time per image](encode-time-per-image.svg)",
        "",
        "![Decoding time per image](decode-time-per-image.svg)",
        "",
        "### What each format loses",
        "",
        "FFV1 and the lossless formats lose nothing; the lossy ones lose a little, measured as an **RMS error** "
        "(root mean square): for every sample, the difference between the image read back and its frame, in 8-bit "
        "levels, squared, averaged, and square-rooted. It is the typical error of a sample, a large error weighing "
        "more than a small one; 1 is the smallest step of an 8-bit sample. The chart shades bands of how visible "
        "an RMS error is, from a viewer study (Afnan et al., 2023): below 0.75 levels no viewer saw it on any "
        "image, up to 2.01 the average viewer didn't, up to 5.74 the average viewer did, and above, every viewer "
        "did. The [lossy README](lossy/README.md#what-they-lose) has it plane by plane, with the other measures.",
        "",
        "![What each format lost](loss-rms.svg)",
        "",
        *butteraugli_section(others, "lossy/README.md#how-far-they-look-butteraugli"),
        "### All the images, against FFV1",
        "",
        f"Each format's {n} images, one at a time and, in the hatched bar next to it, {WORKERS_TEXT(formats)} at a "
        "time, each against FFV1's the same way.",
        "",
        *totals_table("encode", formats, ref, n),
        "",
        "![Writing all the images](encode-time.svg)",
        "",
        *totals_table("decode", formats, ref, n),
        "",
        "![Reading all the images](decode-time.svg)",
        "",
        "## What it shows",
        "",
        (f"- **No format is smaller than FFV1** ({ref['bytes'] / n / MIB:.1f} MiB per image): the closest, "
         f"{label_of(closest)}, is {closest['bytes'] / ref['bytes']:.1f}x its size, and raw "
         f"{raw['bytes'] / ref['bytes']:.1f}x." if not smaller else
         "- **Smaller than FFV1** (" + f"{ref['bytes'] / n / MIB:.1f} MiB per image): "
         + ", ".join(f"{label_of(f)} ({vs_size(f['bytes'], ref['bytes'])})" for f in smaller) + "."),
        f"- **Converting an image from FFV1** takes from {read + ms(by_conversion[0]['encode_seconds']) / n:.0f} ms "
        f"({label_of(by_conversion[0])}, mostly reading the FFV1 frame, {read:.0f} ms) to "
        f"{read + ms(by_conversion[-1]['encode_seconds']) / n:,.0f} ms ({label_of(by_conversion[-1])}): "
        + ", ".join(f"{label_of(f)} {read + ms(f['encode_seconds']) / n:,.0f} ms" for f in by_conversion[1:-1]) + ".",
        ("- **Only " + " and ".join(label_of(f) for f in faster_read)
         + f" read{'s' if len(faster_read) == 1 else ''} back faster than FFV1** "
         f"({ms(ref['decode_seconds']) / n:.0f} ms per image): "
         + ", ".join(f"{label_of(f)} {ms(f['decode_seconds']) / n:.0f} ms" for f in faster_read)
         + "; " + ", ".join(f"{label_of(f)} {ms(f['decode_seconds']) / n:.0f} ms" for f in others
                            if f not in faster_read) + "." if faster_read else
         f"- **No format reads back faster than FFV1** ({ms(ref['decode_seconds']) / n:.0f} ms per image)."),
        "- **" + and_list([label_of(f) for f in others if f["exact"]]) + " give the frames back exactly**, like "
        "FFV1; " + "; ".join(f"{label_of(f)} loses {levels(rms_error_of(f))} levels (RMS)" for f in lossy)
        + ". What they give in exchange is an image any viewer opens; see the [lossy "
        "README](lossy/README.md#what-it-shows).",
        *butteraugli_finding(others),
        (f"- **{', '.join(name[k] for k in too_large)} {'is' if len(too_large) == 1 else 'are'} over mosaicod's upload "
         f"limit**: a message to mosaicod carries at most {MESSAGE_LIMIT / MIB:.0f} MiB (its default), and "
         + ", ".join(f"{name[k]}'s largest image is {largest[k] / MIB:.1f} MiB" for k in too_large)
         + f". Served back, a `DoGet` message may carry up to {DOWNLOAD_LIMIT / MIB:.0f} MiB, so every format comes "
         "back one image per message at most.") if too_large else
        f"- **Every format fits in one message** to mosaicod (at most {MESSAGE_LIMIT / MIB:.0f} MiB).",
        "",
        "## Limits",
        "",
        "- One batch: frames with more detail make every format bigger and slower, and may change the ratios.",
        "- The files were read from the OS cache: from a disk or over the network, the larger formats (raw, PNG, "
        "TIFF) lose more time than the tables show.",
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
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, default=RESULTS, help="the folder of the reports [results/]")
    results = parser.parse_args().results
    data = json.loads((results / "images.json").read_text())
    measured = {q["format"]: q["images"] for q in json.loads((results / "images-quality.json").read_text())["formats"]}
    for f in data["formats"]:
        if f["format"] in measured:
            f["butteraugli"] = measured[f["format"]]
    by_key = {f["format"]: f for f in data["formats"]}
    ref, raw = by_key[REF], by_key["raw"]
    # FFV1 first, then the exact formats, then the converted ones, in FORMATS' order.
    others = sorted((f for f in data["formats"] if f is not ref),
                    key=lambda f: (not f["exact"], list(FORMATS).index(f["format"])))
    formats = [ref, *others]
    # ffv1/regular/batch_060 is regular_batch_060.
    batch = Path(data["input"])
    sequence = f"{batch.parent.name}_{batch.name}"
    n = data["frames"]
    context = f"{sequence}: {n} FFV1 frames of {data['width']}x{data['height']}, 8-bit YUV 4:2:0."

    sizes = [f["bytes"] for f in formats]
    bar_chart("Size of the images", f"{context}\nAll {n} images of each format, against the {n} FFV1 frames they "
              "were written from.", "MiB", [(label_of(f), f["bytes"] / MIB, color_of(f)) for f in formats],
              results / "filesize-comparison.svg", 0, width=1.6 * len(formats) + 1.8,
              extras=["the reference", *(vs_size(size, ref["bytes"]) for size in sizes[1:])],
              groups=grouped_bars(formats))
    conversion_chart(context, others, ref, n, results / "conversion-time.svg")
    for step, (word, what, key) in STEPS.items():
        bar_chart(f"{'Encoding' if step == 'encode' else 'Decoding'} time per image",
                  f"{context}\n" + bold(f"{what[0].upper()}{what[1:]}, mean of the {n} images. Lower is faster."), "ms",
                  [(label_of(f), ms(f[key]) / n, color_of(f)) for f in formats], results / f"{step}-time-per-image.svg",
                  0, extras=["the reference", *(vs_time(f[key], ref[key]) for f in others)], groups=grouped_bars(formats))
        total_chart(step, context, formats, ref, n, results / f"{step}-time.svg",
                    1.25 * (2 * len(formats) + 1) + 1.4, groups=grouped_bars_of_totals(formats))
    bar_chart("What each format lost", f"{context}\nRMS error of a sample, Y, U and V together, over all the "
              "images, in 8-bit levels. The exact formats lose nothing. Lower is closer.", "levels",
              [(label_of(f), 0 if f["exact"] else rms_error_of(f), color_of(f)) for f in others],
              results / "loss-rms.svg", 3, width=10,
              label=lambda value: "exact" if value == 0 else f"{levels(value)} levels", groups=grouped_bars(others),
              bands=PIXEL_ERROR)
    if all(butteraugli_of(f, "butteraugli_max") is not None for f in others):
        for norm, key, what, file in NORMS:
            bar_chart(f"How far the images look from their frames: butteraugli {norm}",
                      f"{context}\nButteraugli {norm}, {what}, mean of the {n} images. The exact formats are their "
                      "frames: 0. Lower is closer" + (": below 1.0, no visible difference." if key == "butteraugli_max"
                                                      else "."),
                      "", [(label_of(f), butteraugli_of(f, key), color_of(f)) for f in others],
                      results / f"{file}.svg", 4, width=10, groups=grouped_bars(others),
                      label=lambda value: "exact" if value == 0 else f"{value:.4f}",
                      bands=BUTTERAUGLI_MAX if key == "butteraugli_max" else None)

    for f in formats:
        name = label_of(f)
        # The format in bold, then its settings, under the batch's context.
        note = f"{context}\n{bold(name)}: {f['settings']}."
        folder = results / group_of(f)[0] / f["format"]
        folder.mkdir(parents=True, exist_ok=True)
        images = f["images"]
        frame_chart(f"{name}: size of each image", note, [i["bytes"] / MIB for i in images], "MiB", 2,
                    folder / "filesize.svg")
        frame_chart(f"{name}: encoding time of each image",
                    f"{note}\nFrom the frame in memory to the file written. Lower is faster.",
                    [ms(i["encode_seconds"]) for i in images], "ms", 0, folder / "encode-time-per-image.svg")
        frame_chart(f"{name}: decoding time of each image",
                    f"{note}\nFrom the file to the frame in memory. Lower is faster.",
                    [ms(i["decode_seconds"]) for i in images], "ms", 0, folder / "decode-time-per-image.svg")
        if f is not ref:
            bar_chart(f"{name}: size of all the images, against FFV1", f"{note}\nAll {n} images, against the {n} "
                      "FFV1 frames they were written from.", "MiB",
                      [("FFV1\nthe reference", ref["bytes"] / MIB, color_of(ref)),
                       (f"{name}\n{n} images", f["bytes"] / MIB, FRAME_BARS)], folder / "filesize-total.svg", 0,
                      width=6.0, extras=["", vs_size(f["bytes"], ref["bytes"])])
            frame_chart(f"{name}: converting each image from FFV1", f"{note}\nReading the FFV1 frame, then writing "
                        "the image. Lower is faster.", [ms(t) for t in conversions(f, ref)], "ms", 0,
                        folder / "conversion-time-per-image.svg")
            for step in STEPS:
                total_chart(step, note, [ref, f], ref, n, folder / f"{step}-time.svg", 9.2, name,
                            groups=[(0, 1, "FFV1, the reference"), (2, 3, name)])
        if f.get("parallel"):
            p, color = f["parallel"], color_of(f)
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
        (folder / "README.md").write_text(format_readme(sequence, f, ref, raw, n))

    for exact in (True, False):
        (results / GROUPS[exact][0] / "README.md").write_text(group_readme(exact, sequence, data, ref, raw))
    (results / "README.md").write_text(overview(sequence, data, ref, raw, others))
    print(f"wrote {results}")


def grouped_bars_of_totals(formats):
    """The groups of a totals chart: each format takes two bars, one at a
    time and several at a time."""
    spans = []
    for i, f in enumerate(formats):
        name = group_of(f)[0]
        width = 2 if f.get("parallel") else 1
        first = spans[-1][1] + 1 if spans else 0
        if spans and spans[-1][2] == name:
            spans[-1] = (spans[-1][0], spans[-1][1] + width, name)
        else:
            spans.append((first, first + width - 1, name))
    return spans


if __name__ == "__main__":
    main()
