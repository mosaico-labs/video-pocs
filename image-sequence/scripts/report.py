#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""Collects the results of one encoding of a batch into the tracked
video-results/<scenario>/<batch>/preset_<P>_crf_<C>/ folder (the folders of
output/, the files renamed and grouped):

    client.json, upload.json,  copies of what the client, the server and the
    quality.csv, quality.json  quality tool wrote next to the video
    summary.md                 what the video lost, the times, the sizes and
                               the raw tables, with the charts
    video-quality/             loss-overview.svg, every measure of loss at
                               once against its visibility thresholds, and
                               <name>/<name>.svg, one folder per measure:
                               the loss of each frame
    client-time/               client-time.svg, the client's side of the
                               upload
    server-time/               server-time.svg, receiving, PNG decoding and
                               encoding on the server, and server-timeline.svg,
                               every frame through its queues
    client-server-time/        client-server-time.svg, both sides on one
                               time axis
    filesize-comparison/       filesize-comparison.svg, the size of the PNG
                               images against the video

The client and the server run in different places, so their times are
charted apart, even though they overlap: the server encodes while the
client sends.

Every quality chart shows a LOSS: how far each frame is from its PNG, where
0 means identical and higher means more was lost. The metrics' own scores
(dB, 0-100 scores) are turned into units that say how much: pixel error in
levels of 255, % of similarity lost, color difference in ΔE, SSIMULACRA2
points lost. The thresholds of what the eye sees are drawn as bands: each
metric's own scale where it has one, otherwise where viewers stopped seeing
the loss in a published study (see visibility_bands). SSIMULACRA2 and
butteraugli also show what the conversion to YUV 4:2:0 alone lost (the
`*_yuv420` columns), before AV1, and, if measured with `quality
--yuv444-floor` (kept in video-results/<scenario>/<batch>/yuv444.quality.json),
what the conversion to YUV 4:4:4 alone loses: the floor of a lossless 4:4:4
video, which SVT-AV1 cannot encode.

Run it after the server and the quality tool:

    uv run scripts/report.py [--timestamps CSV] [--preset P] [--crf C | --lossless]
    uv run scripts/report.py [--timestamps CSV] --compare-only
    uv run scripts/report.py --compare-datasets --preset P
    uv run scripts/report.py --redraw --preset P

--compare-only rewrites only presets-crfs/, the comparison of the batch's
encodings, from video-results/: one chart per measure against the CRF, one line
per preset.

--compare-datasets rewrites only video-results/dataset-comparisons/: the encodings
with preset P of every batch in video-results/, one line per batch, with sizes
and times also per frame, since the batches have different numbers of
images.

--redraw draws every chart and summary of video-results/ again from the copies
there (after a change to the charts, also for encodings whose videos are
gone from output/), then presets-crfs/ of every batch and
dataset-comparisons/ with preset P.

A lossless video goes to preset_<P>_lossless/: its only loss is the
conversion to YUV 4:2:0. It has no CRF, so the comparison charts show it in
a panel of its own on the right, on its own scale.

uv installs matplotlib on the first run.
"""

import argparse
import csv
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.transforms import offset_copy  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent.parent
# The server's defaults (common::DEFAULT_PRESET and DEFAULT_CRF).
DEFAULT_PRESET = 9
DEFAULT_CRF = 30
TOPIC = "camera"
MIB = 1048576


# From a metric's score to a loss. PSNR is 10·log10(peak² / MSE), so the RMS
# error relative to the peak is 10^(−PSNR/20): times 255 it is in levels of an
# 8-bit value, whatever the bit depth. av-metrics gives MS-SSIM in dB,
# −10·log10(1 − MS-SSIM), and CIEDE2000 as 45 − 20·log10(mean ΔE00).
def pixel_error(db):
    return 255 * 10 ** (-db / 20)


def similarity_lost(db):
    return 100 * 10 ** (-db / 10)


def delta_e(db):
    return 10 ** ((45 - db) / 20)


def points_lost(score):
    return 100 - score


def same(value):
    return value


# Where the eye stops seeing the loss, for the metrics with no visibility
# scale of their own (PSNR, PSNR-HVS, MS-SSIM): Afnan et al., "Subjective
# Assessment of Objective Image Quality Metrics Range Guaranteeing Visually
# Lossless Compression", Sensors 23(3):1297, 2023, doi:10.3390/s23031297,
# Table 4. 25 viewers flicker-compared 10 images with JPEG versions of
# falling quality, on a 4K display at 1:1, and the metric was taken where
# each viewer stopped seeing a difference. Its best, mean and worst value over images and viewers make
# four bands, the same for any content and resolution: below the loss at the
# strictest threshold no viewer saw it on any image, above the loss at the
# loosest one every viewer saw it on every image.
def visibility_bands(strictest, mean, loosest):
    return [(0, "invisible in every test"), (strictest, "invisible on average"),
            (mean, "visible on average"), (loosest, "visible in every test")]


@dataclass
class Loss:
    """One measure of loss, computed from a column of quality.csv."""

    name: str
    column: str
    title: str
    unit: str
    to_loss: Callable[[float], float]
    # What the eye sees, as (lower bound, label of the band from it up to
    # the next bound), from 0. Empty when the metric has no such thresholds.
    bands: list = field(default_factory=list)
    # The column of what the conversion to YUV 4:2:0 alone lost, if measured.
    conversion: str | None = None
    # What it measures, for the tables.
    about: str = ""

    @property
    def conversion_444(self):
        """The column of what the conversion to YUV 4:4:4 alone lost, next
        to the 4:2:0 one, if that is measured (see Run.add_yuv444)."""
        return self.conversion.replace("_yuv420", "_yuv444") if self.conversion else None


LOSSES = [
    # The study's PSNR is on Y, U and V: 50.64, 42.08 and 32.95 dB.
    Loss("pixel-error", "psnr_avg", "Pixel error (from PSNR, Y, U and V)", "RMS error, levels of 255",
         pixel_error, bands=visibility_bands(pixel_error(50.64), pixel_error(42.08), pixel_error(32.95)),
         about="average error of a sample; 1 is the smallest step of an 8-bit value"),
    # The study's PSNR-HVS is on Y only: 51.82, 44.65 and 37.85 dB.
    Loss("visible-pixel-error", "psnr_hvs_y", "Brightness error weighted by visibility (from PSNR-HVS of Y)",
         "RMS error, levels of 255", pixel_error,
         bands=visibility_bands(pixel_error(51.82), pixel_error(44.65), pixel_error(37.85)),
         about="the brightness error in the details the eye sees best"),
    # The study's MS-SSIM is on Y only: 0.9998, 0.9970 and 0.9882, so
    # 0.02, 0.30 and 1.18 % lost.
    Loss("msssim", "msssim_db_y", "Structure lost over scales (1 − MS-SSIM of Y)", "% of MS-SSIM lost",
         similarity_lost, bands=visibility_bands(0.02, 0.30, 1.18),
         about="edges and textures of the brightness, from fine to coarse"),
    Loss("color", "ciede2000_db", "Color difference (CIEDE2000)", "mean ΔE00", delta_e,
         bands=[(0, "not perceptible"), (1, "perceptible on close look"), (2, "perceptible at a glance"),
                (10, "different colors")],
         about="the average color shift a person would see"),
    Loss("ssimulacra2", "ssimulacra2", "SSIMULACRA2 points lost (100 − score)", "points lost of 100",
         points_lost,
         bands=[(0, "visually lossless"), (10, "high quality"), (30, "medium quality"), (50, "low quality"),
                (70, "very low quality")],
         conversion="ssimulacra2_yuv420", about="perceived quality, against the PNG"),
    Loss("butteraugli-max", "butteraugli_max", "Butteraugli, worst spot of the frame", "max-norm", same,
         # Google's three classes (butteraugli README); the 1.0 boundary is
         # its ButteraugliFuzzyClass's, the 2.0 one the crate's BUTTERAUGLI_BAD.
         bands=[(0, "great"), (1, "acceptable"), (2, "not acceptable")],
         conversion="butteraugli_max_yuv420", about="how visible the worst difference is"),
]

# One visual system for every chart: light surface, recessive hairline grid,
# 2px lines, text in ink colors (never in a series color).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
# Categorical slots 1 and 2: the video, and the YUV 4:2:0 conversion alone.
LINE = "#2a78d6"
CONVERSION = "#eb6834"
# What the YUV 4:4:4 conversion alone would lose, in the comparison of a
# batch's encodings: next to the 4:2:0 conversion's orange line and the
# presets' diamonds, so no hue at all (dark ink, dashed).
CONVERSION_444 = INK
# The PNG images, against the video (LINE) in the file size chart.
PNG = "#1f9e89"
# The server timeline: an image waiting in a queue, decoded (PNG), waiting
# for the encoder (WAITING), encoded (LINE).
QUEUED = "#d2d0c6"
WAITING = "#e3a21a"
# The client and server chart: the client sending, the server receiving
# and then finishing; decoding and encoding keep PNG and LINE.
SENDING = "#7a5cc7"
RECEIVING = "#5a6e8c"
FINISHING = "#9aa7bd"
# The bands of what the eye sees, light (little lost) to dark (much lost).
BANDS = ["#f7f5ee", "#e8e2cf", "#d8cdac", "#c6b78b", "#b19f6d"]
# One color per preset in the comparison of a batch's encodings.
PRESETS = ["#2a78d6", "#1f9e89", "#7a5cc7", "#e3a21a", "#d6456b", "#5a6e8c"]
VIDEO_LABEL = "the video (YUV 4:2:0 + AV1)"
CONVERSION_LABEL = "YUV 4:2:0 conversion alone"
CONVERSION_444_LABEL = "YUV 4:4:4 conversion alone (no video: SVT-AV1 encodes only 4:2:0)"

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
    "svg.hashsalt": "image-sequence",
})
# Bold values in the keys (see in_bold()) are math text: in the charts' own
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


def sequence_name(timestamps):
    """common::sequence_name: regular/batch_060/timestamps.csv becomes
    regular_batch_060."""
    batch = timestamps.resolve().parent
    return re.sub(r"[^A-Za-z0-9-]", "_", f"{batch.parent.name}_{batch.name}")


def sequence_dir(sequence):
    """common::sequence_dir: regular_batch_060 becomes regular/batch_060."""
    scenario, sep, number = sequence.rpartition("_batch_")
    return Path(scenario, f"batch_{number}") if sep and scenario and number else Path(sequence)


def band_of(loss, value):
    """The label of the band `value` falls in, or None."""
    label = None
    for bound, name in loss.bands:
        if value >= bound:
            label = name
    return label


@dataclass
class Run:
    upload: dict
    # Column name -> per-frame values (None where the frame was not measured).
    metrics: dict
    frames: list
    max_capture_error_ns: int
    # The quality tool's AV1 decoding time (quality.json), if it was timed.
    decoding: dict
    # The client's own times (client.json), if it wrote them.
    client: dict
    # The image of each frame, as in quality.csv.
    images: list = field(default_factory=list)

    @classmethod
    def read(cls, upload_path, quality_path, client_path):
        upload = json.loads(upload_path.read_text())
        client = json.loads(client_path.read_text()) if client_path.exists() else {}
        decoding_path = quality_path.with_suffix(".json")
        decoding = json.loads(decoding_path.read_text()) if decoding_path.exists() else {}
        with open(quality_path, newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows or "capture_error_ns" not in rows[0]:
            raise SystemExit(f"{quality_path}: unexpected header")
        names = list(rows[0])[5:]
        metrics = {n: [float(r[n]) if r[n] else None for r in rows] for n in names}
        errors = [abs(int(r["capture_error_ns"])) for r in rows if r["capture_error_ns"]]
        return cls(upload, metrics, [int(r["frame"]) for r in rows], max(errors, default=0), decoding, client,
                   [r["image"] for r in rows])

    def add_yuv444(self, path):
        """What the conversion to YUV 4:4:4 alone loses on each frame's
        image, from the batch's yuv444.quality.json (quality --yuv444-floor),
        as the `*_yuv444` columns next to the `*_yuv420` ones. The same for
        every encoding of the batch: no video is involved."""
        if not path.exists():
            return
        by_image = {i["image"]: i for i in json.loads(path.read_text())["images"]}
        for key in ("ssimulacra2", "butteraugli_max", "butteraugli_3norm"):
            values = [by_image.get(image, {}).get(key) for image in self.images]
            if any(v is not None for v in values):
                self.metrics[f"{key}_yuv444"] = values

    def series(self, name):
        return [(f, v) for f, v in zip(self.frames, self.metrics.get(name, [])) if v is not None]

    def losses(self, loss, column=None):
        """(frame, loss) of the measured frames; `column` defaults to the
        video's."""
        return [(f, loss.to_loss(v)) for f, v in self.series(column or loss.column)]

    def mean_loss(self, loss, column=None):
        values = [v for _, v in self.losses(loss, column)]
        return sum(values) / len(values) if values else None

    def worst_loss(self, loss):
        """(frame, loss) of the frame that lost the most."""
        return max(self.losses(loss), key=lambda p: p[1], default=None)

    @property
    def video_mib(self):
        return self.upload["video_bytes"] / MIB

    @property
    def png_mib(self):
        return self.upload["png_bytes"] / MIB

    @property
    def ratio(self):
        return self.upload["png_bytes"] / self.upload["video_bytes"]

    @property
    def lossless(self):
        return self.upload["crf"] is None

    @property
    def settings(self):
        if self.lossless:
            return f"SVT-AV1 preset {self.upload['preset']}, lossless"
        return f"SVT-AV1 preset {self.upload['preset']} and CRF {self.upload['crf']}"

    @property
    def client_times(self):
        """(label, seconds) of the client's stages, one after the other, so
        they add up to its total; empty if it wrote no client.json (runs
        from before it did). The client reads each PNG just before handing
        it to gRPC, so sending is reading plus the rest."""
        c = self.client
        if not c:
            return []
        return [("Setup\n(CSV, connect,\ncreate topic)", c["setup_seconds"]),
                ("Reading\nthe PNGs\nfrom disk", c["read_seconds"]),
                ("Sending\nto the server", c["send_seconds"] - c["read_seconds"]),
                ("Waiting for\nthe server\nto finish", c["wait_seconds"]),
                ("Finalizing\nthe session", c["finalize_seconds"])]

    @property
    def server_times(self):
        """(label, seconds) of the server's stages. They overlap: it decodes
        and encodes while the images arrive. The encoder's time is split in
        working and idle, which add up to its wall time."""
        u = self.upload
        return [("Receiving\nthe images\n(first message\nto last image)", u.get("receive_seconds")),
                ("Decoding\nthe PNGs\nto YUV 4:2:0", u["decode_seconds"]),
                ("AV1 encoding\n(encoder\nworking)", u["encode_seconds"] - u["encode_wait_seconds"]),
                ("Encoder idle,\nwaiting for\ndecoded PNGs", u["encode_wait_seconds"])]

    def mbit_per_s(self):
        """Bitrate over the span the frames cover: first to last capture plus
        one frame interval."""
        u, n = self.upload, self.upload["images"]
        span = (u["last_timestamp_ns"] - u["first_timestamp_ns"]) / 1e9 * n / (n - 1) if n >= 2 else 0
        return u["video_bytes"] * 8 / span / 1e6 if span > 0 else None


def subtitle(axes, text, y=1.03):
    axes.text(0, y, text, transform=axes.transAxes, color=INK_SECONDARY, fontsize=9)


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


def loss_chart(run, loss, path):
    """The loss of each frame, with the bands of what the eye sees and, if
    measured, what the YUV 4:2:0 conversion alone lost."""
    video = run.losses(loss)
    conversion = run.losses(loss, loss.conversion) if loss.conversion else []
    conversion_444 = run.losses(loss, loss.conversion_444) if loss.conversion else []
    # A lossless video loses only the conversion: its line would hide the
    # conversion's, so it is dashed to let the conversion show through.
    same = bool(conversion) and video == conversion
    # The metrics with no conversion column compare with the YUV frames the
    # encoder received, which a lossless video keeps exactly: its line lies
    # on 0 (av-metrics' 100 dB or inf), drawn over the axis to be seen.
    nothing = bool(video) and run.lossless and not loss.conversion
    figure, axes = plt.subplots(figsize=(7.6, 3.6))
    lines = ["Each frame of the video against its PNG.",
             "**0 = identical, higher = more lost.**",
             "Shaded bands: how visible the loss is, darker = more visible (key below);"
             if loss.bands else "This measure has no published visibility thresholds, so no bands.",
             *(["dashed lines: where one band ends and the next starts."] if loss.bands else []),
             *(["The video loses only the conversion: its line (dashed) lies on the conversion's."] if same else []),
             *(["The video loses nothing here (lossless): its line lies on 0."] if nothing else []),
             *(["Dashed black: what the YUV 4:4:4 conversion alone loses (no video: SVT-AV1 encodes only 4:2:0)."]
               if conversion_444 else []),
             f"{run.settings}."]
    axes.set_title(loss.title, pad=14 + 11 * len(lines))
    for i, line in enumerate(reversed(lines)):
        bold = line.startswith("**")
        axes.annotate(line.strip("*"), (0, 1.03), xycoords="axes fraction", xytext=(0, 11 * i),
                      textcoords="offset points", color=INK if bold else INK_SECONDARY, fontsize=9,
                      fontweight="bold" if bold else "normal")
    if video:
        values = [v for _, v in video + conversion + conversion_444]
        top = max(values) * 1.15 or 1
        # Always show where "can't be seen" ends, for scale.
        if len(loss.bands) > 1:
            top = max(top, loss.bands[1][0] * 1.25)
        draw_bands(axes, loss, top)
        handles = []
        if conversion:
            handles += axes.plot(*zip(*conversion), color=CONVERSION, label=CONVERSION_LABEL, zorder=2)
        if conversion_444:
            handles += axes.plot(*zip(*conversion_444), color=CONVERSION_444, label=CONVERSION_444_LABEL, zorder=2.1,
                                 linestyle=(0, (4, 3)), linewidth=1.2)
        handles += axes.plot(*zip(*video), color=LINE, label=VIDEO_LABEL, zorder=3 if nothing else 2,
                             linestyle=(0, (5, 5)) if same else "-", clip_on=not nothing,
                             linewidth=3 if nothing else None)
        # No worst frame when none lost anything.
        if not nothing:
            worst = max(video, key=lambda p: p[1])
            axes.plot(*worst, "o", color=LINE, markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5,
                      zorder=3)
            left = worst[0] > 0.6 * video[-1][0]
            axes.annotate(f"worst: frame {worst[0]}, {worst[1]:.2f}", worst, xytext=(-8 if left else 8, 0),
                          textcoords="offset points", ha="right" if left else "left", va="center", color=INK,
                          fontsize=8, zorder=4)
        axes.set_xlim(0, max(video[-1][0], 1))
        axes.set_ylim(0, top)
        axes.legend(handles=handles + band_key(loss), loc="upper left", bbox_to_anchor=(0, -0.16), ncol=1,
                    borderaxespad=0, handlelength=1.5)
    axes.set_xlabel("frame")
    axes.set_ylabel(loss.unit)
    axes.grid(axis="x", visible=False)
    save(figure, path)


def overview_chart(run, path):
    """Every measure of loss at once, each on its own scale: the mean of the
    video (bar), what the conversion alone lost (thin bar), the worst frame
    (tick), over the bands of what the eye sees, each named inside. What the
    YUV 4:4:4 conversion alone lost, if measured: a dashed outline under it."""
    has_444 = any(run.losses(loss, loss.conversion_444) for loss in LOSSES if loss.conversion)
    lines = [f"Mean of {run.upload['images']} frames, {run.settings}. Each row is one measure, on its own scale.",
             "**0 = identical, higher = more lost.**",
             "Shaded bands: how visible the loss is, from light (invisible) to dark (clearly visible), "
             "named inside each band where it fits (every band is in the measure's own chart).",
             "Dashed lines: where one band ends and the next starts. A row without bands has no published "
             "visibility thresholds.",
             "On the right of each row: the band the mean falls in, the worst frame, and what the conversion "
             "alone lost" + (" (4:2:0, then 4:4:4)." if has_444 else ".")]
    # The key, two to a row, filled column by column: the video and the
    # worst frame on the left, the conversions on the right.
    keys = [Patch(color=LINE, label=f"mean, {VIDEO_LABEL}"), Line2D([], [], color=INK, linewidth=1.5, label="worst frame"),
            Patch(color=CONVERSION, label=f"mean, {CONVERSION_LABEL}"),
            *([Patch(facecolor="none", edgecolor=CONVERSION_444, linestyle=(0, (4, 3)),
                     label=f"mean, {CONVERSION_444_LABEL}")] if has_444 else [])]
    key_rows = (len(keys) + 1) // 2
    head = 0.42 + 0.17 * len(lines) + 0.17 * key_rows + 0.13  # inches: title, description, key
    height = 0.95 * len(LOSSES) + head
    figure, rows = plt.subplots(len(LOSSES), 1, figsize=(9.6, height))
    figure.text(0.02, 1 - 0.1 / height, "How much the video lost against the PNG images", ha="left", va="top",
                fontsize=13, fontweight="bold", color=INK)
    for i, line in enumerate(lines):
        bold = line.startswith("**")
        figure.text(0.02, 1 - (0.42 + 0.17 * i) / height, line.strip("*"), va="top", fontsize=9,
                    color=INK if bold else INK_SECONDARY, fontweight="bold" if bold else "normal")
    for axes, loss in zip(rows, LOSSES):
        mean, worst = run.mean_loss(loss), run.worst_loss(loss)
        conversion = run.mean_loss(loss, loss.conversion) if loss.conversion else None
        conversion_444 = run.mean_loss(loss, loss.conversion_444) if loss.conversion else None
        axes.set_title(f"{loss.title}  ·  {loss.unit}", fontsize=9, pad=4)
        axes.set_yticks([])
        axes.spines["bottom"].set_visible(False)
        axes.grid(axis="y", visible=False)
        axes.tick_params(axis="x", labelsize=7.5, length=0, pad=1)
        if mean is None:
            axes.set_xticks([])
            axes.set_ylim(-0.5, 0.5)
            axes.text(0, 0, "not measured", color=MUTED, va="center", fontsize=8)
            continue
        top = max(worst[1], mean) * 1.08
        if len(loss.bands) > 1:
            top = max(top, loss.bands[1][0] * 1.25)
        axes.set_xlim(0, top)
        axes.set_ylim(-0.62 if has_444 else -0.5, 0.75)
        width_pt = 9.6 * 72 * 0.94
        for i, (bound, name) in enumerate(loss.bands):
            if bound >= top:
                break
            upper = min(loss.bands[i + 1][0] if i + 1 < len(loss.bands) else top, top)
            axes.axvspan(bound, upper, color=BANDS[i], zorder=0, linewidth=0)
            if i > 0:
                axes.axvline(bound, color=INK, linewidth=1.1, linestyle=(0, (4, 3)), zorder=1.5)
            # The name in the band, above the bars, if it fits (~3.6 pt a character).
            if (upper - bound) / top * width_pt >= 3.6 * len(name) + 6:
                axes.text(bound + top * 0.004, 0.62, name, ha="left", va="center", fontsize=6.5, color=INK_SECONDARY,
                          zorder=1)
        axes.barh(0.08, mean, height=0.36, color=LINE, zorder=2)
        if conversion is not None:
            axes.barh(-0.26, conversion, height=0.16, color=CONVERSION, zorder=2)
        if conversion_444 is not None:
            axes.barh(-0.47, conversion_444, height=0.14, facecolor="none", edgecolor=CONVERSION_444,
                      linestyle=(0, (4, 3)), linewidth=1, zorder=2)
        axes.plot([worst[1]] * 2, [-0.18, 0.34], color=INK, linewidth=1.5, zorder=3)
        band = band_of(loss, mean)
        text = f"mean {mean:.2f}" + (f" → {band}" if band else "") + f"   worst {worst[1]:.2f} (frame {worst[0]})"
        if conversion is not None:
            text += f"   conversion alone {conversion:.2f}"
        if conversion_444 is not None:
            text += f", 4:4:4 {conversion_444:.2f}"
        axes.text(1, 1.02, text, transform=axes.transAxes, ha="right", va="bottom", fontsize=7.5, color=INK)
    legend_y = 1 - (0.42 + 0.17 * len(lines) + 0.02) / height
    figure.legend(handles=keys, loc="upper left", bbox_to_anchor=(0.015, legend_y), ncol=2, fontsize=8)
    figure.tight_layout(rect=(0, 0, 1, 1 - head / height), h_pad=1.2)
    save(figure, path)


def filesize_chart(run, path):
    u = run.upload
    figure, axes = plt.subplots(figsize=(5.6, 4.6))
    axes.set_title(f"The video is {run.ratio:.0f}x smaller than the images", pad=40)
    labels, values, colors = ["PNG images", "AV1 video"], [run.png_mib, run.video_mib], [PNG, LINE]
    # The description, one line per bar in its color.
    axes.legend(handles=[Patch(color=PNG, label=f"PNG images: {u['images']} of {u['width']}x{u['height']}"),
                         Patch(color=LINE, label=f"AV1 video: {run.settings}")],
                loc="lower left", bbox_to_anchor=(0, 1.0), borderaxespad=0.2, fontsize=9, handlelength=1,
                handleheight=1, labelcolor=INK_SECONDARY)
    # A bar too short to see is still drawn, ~2px high.
    heights = [max(v, run.png_mib * 0.006) for v in values]
    axes.bar(labels, heights, width=0.5, color=colors)
    for x, (h, text) in enumerate(zip(heights, [f"{run.png_mib:.1f} MiB", f"{run.video_mib:.2f} MiB"])):
        axes.annotate(text, (x, h), xytext=(0, 4), textcoords="offset points", ha="center", va="bottom",
                      color=INK, fontweight="bold")
    axes.set_ylim(0, run.png_mib * 1.12)
    axes.set_ylabel("MiB")
    axes.grid(axis="x", visible=False)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=10, length=0)
    save(figure, path)


def busy_spans(frames, start, end):
    """The merged (start, length) spans when some frame was between
    `start` and `end`: when the decoder or the encoder was busy."""
    spans = []
    for a, b in sorted((f[start], f[end]) for f in frames):
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return [(a, b - a) for a, b in spans]


def client_server_chart(run, path):
    """The client's and the server's times on one axis, to show how they
    fit together. The axis starts with the upload: the client's DoPut and
    the server's first message, microseconds apart on one machine, the
    network latency apart on two. The client's setup comes before it and
    is left out."""
    c, u = run.client, run.upload
    sent = c["send_seconds"]
    report = sent + c["wait_seconds"]
    received, closed = u["receive_seconds"], u["seconds"]
    frames = u.get("frames", [])
    lanes = [
        ("Client", [(0, sent, SENDING, "reading and sending the images"),
                    (sent, report, QUEUED, "waiting for the server")]),
        ("Server", [(0, received, RECEIVING, "receiving the images"), (received, closed, FINISHING, "finishing")]),
        ("Server:\nPNG decoding", [(a, a + w, PNG, "") for a, w in busy_spans(frames, "decode_start", "decoded")]),
        ("Server:\nAV1 encoding", [(a, a + w, LINE, "") for a, w in busy_spans(frames, "encode_start", "encoded")]),
    ]
    lines = [f"{run.settings}.",
             f"The client's setup ({seconds(c['setup_seconds'])}) comes before the upload and is not shown here.",
             "**Client and server both start with the upload.**",
             "The server decodes and encodes while the client sends,",
             "so the client waits only for what is left after its last image.",
             "On loopback (one machine) the two starts coincide; on two machines the server's rows",
             "shift right by the network latency."]
    figure, axes = plt.subplots(figsize=(9.6, 5.0))
    axes.set_title(f"Client and server: the upload of {u['images']} frames, {report:.1f} s",
                   pad=16 + 11 * len(lines))
    for i, line in enumerate(reversed(lines)):
        bold = line.startswith("**")
        axes.annotate(line.strip("*"), (0, 1.03), xycoords="axes fraction", xytext=(0, 11 * i),
                      textcoords="offset points", color=INK if bold else INK_SECONDARY, fontsize=9,
                      fontweight="bold" if bold else "normal")
    for row, (_, segments) in enumerate(lanes):
        for start, end, color, label in segments:
            axes.broken_barh([(start, end - start)], (row - 0.32, 0.64), color=color, linewidth=0)
            if label:
                axes.text((start + end) / 2, row, f"{label}\n{seconds(end - start)}", ha="center", va="center",
                          fontsize=8, color=SURFACE if color in (SENDING, RECEIVING) else INK)
    # When the last image leaves the client, reaches the server, and the
    # report reaches the client: the client's wait, split in two.
    for x in (sent, received, report):
        axes.axvline(x, color=INK, linewidth=1.0, linestyle=(0, (4, 3)), zorder=0)
    y = -0.62
    for start, end in [(sent, received), (received, report)]:
        axes.annotate("", (start, y), (end, y), arrowprops={"arrowstyle": "<->", "color": INK, "linewidth": 0.8})
    axes.text(sent, y - 0.08, f"last image in flight {seconds(received - sent)} ", ha="right", va="bottom",
              fontsize=7.5, color=INK)
    axes.text((received + report) / 2, y - 0.08, f"server finishing {seconds(report - received)}", ha="center",
              va="bottom", fontsize=7.5, color=INK)
    axes.set_yticks(range(len(lanes)), [name for name, _ in lanes])
    axes.set_ylim(len(lanes) - 0.4, -0.9)
    axes.set_xlim(0, max(report, closed) * 1.02)
    axes.set_xlabel("seconds since the upload started")
    axes.grid(axis="y", visible=False)
    axes.tick_params(axis="y", colors=INK, labelcolor=INK, labelsize=9)
    save(figure, path)


def timeline_chart(run, path):
    """Each image through the server, one row per frame on a shared time
    axis: waiting in the decode queue, decoding, waiting for the encoder,
    encoding. Rows that overlap in time ran at the same time."""
    u, frames = run.upload, run.upload["frames"]
    figure, axes = plt.subplots(figsize=(9.6, 0.09 * len(frames) + 2.6))
    axes.set_title(f"Server timeline: {u['images']} frames through the queues, the decoder and the encoder",
                   pad=48)
    subtitle(axes, f"{run.settings}. One row per frame, in seconds since the first message.\n"
             f"PNG decoding runs up to 4 frames at a time; the encoder takes them one by one, in order.\n"
             f"After the last frame, the encoder flushes and the file is closed.")
    steps = [("received", "decode_start", QUEUED, "waiting in the decode queue"),
             ("decode_start", "decoded", PNG, "decoding the PNG to YUV 4:2:0"),
             ("decoded", "encode_start", WAITING, "decoded, waiting for the encoder"),
             ("encode_start", "encoded", LINE, "encoder busy with it")]
    for row, frame in enumerate(frames):
        for start, end, color, _ in steps:
            axes.broken_barh([(frame[start], frame[end] - frame[start])], (row - 0.4, 0.8), color=color,
                             linewidth=0)
    for x, label in [(u["receive_seconds"], "last image received"), (u["seconds"], "video closed")]:
        axes.axvline(x, color=INK, linewidth=1.0, linestyle=(0, (4, 3)))
        # Along the line, at the top: the early frames are all on the left.
        axes.annotate(f"{label}, {x:.1f} s", (x, 0.99), xycoords=axes.get_xaxis_transform(), xytext=(-3, 0),
                      textcoords="offset points", rotation=90, ha="right", va="top", fontsize=7.5,
                      color=INK_SECONDARY)
    axes.set_xlim(0, u["seconds"] * 1.02)
    axes.set_ylim(len(frames) - 0.5, -0.5)
    axes.set_xlabel("seconds since the first message")
    axes.set_ylabel("frame")
    axes.grid(axis="y", visible=False)
    axes.legend(handles=[Patch(color=color, label=label) for _, _, color, label in steps], loc="upper left",
                bbox_to_anchor=(0, -0.08 - 2.0 / (0.09 * len(frames) + 2.6) * 0.3), ncol=2, borderaxespad=0)
    save(figure, path)


def stage_chart(run, title, note, stages, total, path):
    """The time of each stage of one side of the upload, with its total."""
    figure, axes = plt.subplots(figsize=(1.45 * len(stages) + 1.6, 4.6))
    lines = [f"{run.settings}."] + note.split("\n")
    axes.set_title(f"{title}: {total:.1f} s for {run.upload['images']} frames", pad=12 + 11 * len(lines))
    # One line at a time, from the bottom up; a line in **...** is bold.
    for i, line in enumerate(reversed(lines)):
        bold = line.startswith("**") and line.endswith("**")
        axes.annotate(line.strip("*"), (0, 1.03), xycoords="axes fraction", xytext=(0, 11 * i),
                      textcoords="offset points", color=INK if bold else INK_SECONDARY, fontsize=9,
                      fontweight="bold" if bold else "normal")
    labels, times = [label for label, _ in stages], [t for _, t in stages]
    top = max([t for t in times if t is not None], default=1)
    axes.bar(labels, [t or 0 for t in times], width=0.5, color=LINE)
    for x, t in enumerate(times):
        text = "not recorded" if t is None else seconds(t)
        axes.annotate(text, (x, t or 0), xytext=(0, 4), textcoords="offset points", ha="center",
                      va="bottom", color=INK if t is not None else MUTED, fontweight="bold" if t else "normal")
    axes.set_ylim(0, top * 1.2)
    axes.set_ylabel("seconds")
    axes.grid(axis="x", visible=False)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=8, length=0)
    save(figure, path)


def seconds(value):
    """A time, in milliseconds when it would round to 0.0 s."""
    if value is None:
        return "–"
    return f"{value * 1000:.1f} ms" if value < 0.1 else f"{value:.1f} s"


def number(value):
    return "–" if value is None else f"{value:.2f}"


def summary(sequence, run):
    u = run.upload
    images = "image" if u["images"] == 1 else "images"
    bitrate = run.mbit_per_s()
    has_444 = any(name.endswith("_yuv444") for name in run.metrics)
    md = [
        f"# {sequence}: AV1 encoding results\n",
        f"Generated by `scripts/report.py` from the client's, the server's and the quality tool's reports, "
        f"copied next to it; do not edit. {u['images']} {images} of "
        f"{u['width']}x{u['height']}, {run.png_mib:.1f} MiB of PNG, encoded as `{u['pixel_format']}` "
        f"with {run.settings}.\n",
        "## What the video lost\n",
        "Every frame of the video compared with its PNG, as a loss: 0 means identical, higher means more "
        "was lost. *4:2:0 conversion alone* is what turning the PNG into YUV 4:2:0 (half-resolution color) "
        "loses before any encoding; the video's loss includes it."
        + (" *4:4:4 conversion alone* is what turning it into YUV 4:4:4 (full-resolution color) would lose: the "
           "floor of a lossless 4:4:4 video, which SVT-AV1 cannot encode, measured without encoding and so the same "
           "for every encoding of the batch.\n" if has_444 else "\n"),
        "![How much the video lost against the PNG images](video-quality/loss-overview.svg)\n",
        "| Measure | Unit | Mean | Worst frame | 4:2:0 conversion alone | " + ("4:4:4 conversion alone | " if has_444 else "")
        + "The mean is | What it measures |",
        "|---|---|---|---|---|" + ("---|" if has_444 else "") + "---|---|",
    ]
    for loss in LOSSES:
        mean, worst = run.mean_loss(loss), run.worst_loss(loss)
        conversion = run.mean_loss(loss, loss.conversion) if loss.conversion else None
        conversion_444 = run.mean_loss(loss, loss.conversion_444) if loss.conversion else None
        md.append(
            f"| {loss.title} | {loss.unit} | {number(mean)} | "
            f"{'–' if worst is None else f'{worst[1]:.2f} (frame {worst[0]})'} | {number(conversion)} | "
            + (f"{number(conversion_444)} | " if has_444 else "")
            + f"{(band_of(loss, mean) if mean is not None else None) or '–'} | {loss.about} |"
        )
    md.append("")
    md += [f"![{loss.title}](video-quality/{loss.name}/{loss.name}.svg)\n" for loss in LOSSES if run.losses(loss)]
    decode = run.decoding.get("decode_seconds")
    if run.decoding.get("reused_from"):
        md.append(f"Not measured again: a lossless video decodes to the same frames at every preset, and these "
                  f"are the same as `{run.decoding['reused_from']}`'s (MD5 of each frame decoded with dav1d), "
                  f"so the quality is that one's. Its AV1 decoding time was not reused.\n")
    elif decode:
        md.append(f"The quality tool decoded the whole video with dav1d in {decode:.1f} s "
                  f"({u['images'] / decode:.1f} frames/s), file reading included.\n")
    md += ["## Client time\n",
           "The client's side of the upload, measured where it runs. The stages run one after the other and "
           "add up to the total.\n"]
    if run.client_times:
        md += [
            "| " + " | ".join(label.replace("\n", " ") for label, _ in run.client_times) + " | Total |",
            "|---" * (len(run.client_times) + 1) + "|",
            "| " + " | ".join(seconds(t) for _, t in run.client_times) + f" | {seconds(run.client['seconds'])} |\n",
            "![Client time](client-time/client-time.svg)\n",
        ]
    else:
        md.append("Not recorded: the client of this run wrote no `client.json`.\n")
    md += [
        "## Server time\n",
        "The server's side, measured where it runs. The stages overlap: it decodes and encodes the images "
        f"while they arrive. The encoder working and idle add up to its {seconds(u['encode_seconds'])}.\n",
        "| " + " | ".join(label.replace("\n", " ") for label, _ in run.server_times) + " | Total | Encoding speed |",
        "|---" * (len(run.server_times) + 2) + "|",
        "| " + " | ".join(seconds(t) for _, t in run.server_times)
        + f" | {seconds(u['seconds'])} | {u['images'] / u['encode_seconds']:.2f} frames/s |\n",
        "![Server time](server-time/server-time.svg)\n",
    ]
    if u.get("frames"):
        md += [
            "Each frame through the server: waiting in the decode queue, decoding, waiting for the encoder, "
            "encoding. Frames that overlap in time ran at the same time.\n",
            "![Server timeline](server-time/server-timeline.svg)\n",
        ]
    md += [
        *([
            "## Client and server together\n",
            "The client's and the server's times on one axis. The server decodes and encodes while the client "
            "sends, so the client waits only for the last image in flight and the server finishing.\n",
            "![Client and server together](client-server-time/client-server-time.svg)\n",
        ] if run.client_times else []),
        "## File size\n",
        "| Video size | Bitrate | Smaller than PNG |",
        "|---|---|---|",
        f"| {run.video_mib:.2f} MiB | {f'{bitrate:.1f} Mbit/s' if bitrate else '–'} | "
        f"{run.ratio:.{1 if run.ratio < 10 else 0}f}x |\n",
        "![Size of the PNG images against the video](filesize-comparison/filesize-comparison.svg)\n",
        "## Raw metric values\n",
        "The metrics' own scores, over the measured frames. Higher is better for every metric except "
        "`butteraugli_*`, where lower is better. `*_yuv420`: the PNG against itself converted to YUV 4:2:0"
        + (", `*_yuv444`: to YUV 4:4:4" if has_444 else "") + ", "
        f"without encoding. Capture times read back within {run.max_capture_error_ns / 1e6:.3f} ms of the CSV.\n",
        "| Metric | Mean | Min | Max |",
        "|---|---|---|---|",
    ]
    for name in run.metrics:
        values = [v for _, v in run.series(name)]
        if values:
            md.append(f"| `{name}` | {sum(values) / len(values):.3f} | {min(values):.3f} | {max(values):.3f} |")
        else:
            md.append(f"| `{name}` | – | – | – |")
    md.append("")
    return "\n".join(md)


def encodings(batch_dir):
    """Every encoding of a batch in video-results/, ordered by preset then CRF,
    the lossless one first."""
    runs = []
    for folder in [*batch_dir.glob("preset_*_crf_*"), *batch_dir.glob("preset_*_lossless")]:
        upload, quality = folder / "upload.json", folder / "quality.csv"
        if upload.exists() and quality.exists():
            runs.append(Run.read(upload, quality, folder / "client.json"))
    return sorted(runs, key=lambda r: (r.upload["preset"], -1 if r.lossless else r.upload["crf"]))


# The lossless encodings' marker.
LOSSLESS_MARKER = "D"


def yuv444_floors(batch_dir):
    """What the conversion to YUV 4:4:4 alone loses, the mean over the
    images, by the 4:2:0 conversion's column (butteraugli_max_yuv420 -> the
    mean of butteraugli_max); empty if it was not measured."""
    path = batch_dir / "yuv444.quality.json"
    if not path.exists():
        return {}
    images = json.loads(path.read_text())["images"]
    return {f"{key}_yuv420": sum(i[key] for i in images) / len(images)
            for key in ("butteraugli_max", "butteraugli_3norm", "ssimulacra2")} if images else {}


# The two butteraugli measures that compare encodings. The 3-norm has no
# visibility scale but ranks the settings of one batch; the max-norm says
# whether the worst spot is visible.
BUTTERAUGLI_MAX = Loss("butteraugli-max", "butteraugli_max", "", "butteraugli max-norm", same,
                       bands=next(loss.bands for loss in LOSSES if loss.name == "butteraugli-max"),
                       conversion="butteraugli_max_yuv420")
BUTTERAUGLI_3NORM = Loss("butteraugli-3norm", "butteraugli_3norm", "", "butteraugli 3-norm", same,
                         conversion="butteraugli_3norm_yuv420")

# Every line has its own color, the same in every chart, and its own
# marker and line style, so that lines are told apart without color too.
# One color per preset in presets-crfs/, the same in every chart: blue,
# crimson, dark yellow pass every check of validate_palette.js --pairs all
# as a set (yellow is below 3:1 on the surface, and every preset is also
# named in the keys and the size table). Then green and violet, for more.
PRESET_COLORS = ["#2a78d6", "#d6456b", "#c98500", "#008300", "#6250d6"]
MARKERS = ["o", "s", "v", "P", "X"]
LINESTYLES = ["-", (0, (5, 2)), (0, (1, 1.5)), (0, (6, 2, 1, 2)), (0, (3, 1))]


def short_number(value):
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"


def fitted_limits(values):
    """Axis limits fitted to `values`, not from 0, so that close values
    separate. At least ±5% of the values around them: a difference of a
    fraction of a percent (the lossless sizes) would otherwise fill the
    panel. Never below 0 for values that are not."""
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.12, max(abs(lo), abs(hi)) * 0.05) or 1
    return (max(lo - pad, 0) if lo >= 0 else lo - pad), hi + pad


@dataclass
class Group:
    """The encodings of one line: a preset of a batch, or a dataset."""

    label: str
    short: str
    runs: list
    color: str
    # What the batch's conversion to YUV 4:4:4 alone loses, by the 4:2:0
    # column (see yuv444_floors), if measured.
    yuv444: dict = field(default_factory=dict)


@dataclass
class Measure:
    """One value of an encoding, charted against its CRF."""

    name: str
    title: str
    unit: str
    value: Callable
    better: str
    about: str
    # The loss whose visibility bands to draw, if any.
    bands: Loss | None = None
    # A second value of each lossy encoding, written in a table under the
    # CRF axis, one row per line: (what it is, the value as text).
    table: tuple | None = None
    # Horizontal lines in the lossless panel: (label, value, color, dashed).
    floors: list = field(default_factory=list)
    # Each group's own floor, a mark on its row of the lossless panel: the
    # 4:2:0 column whose 4:4:4 value to show, from Group.yuv444.
    row_floor: str | None = None


def coincide(a, b):
    """Whether two lines of (crf, value) points lie within 1% of each other
    at every CRF they share."""
    b = dict(b)
    shared = [(y, b[x]) for x, y in a if x in b]
    return bool(shared) and all(abs(y - z) <= 0.01 * max(abs(y), abs(z), 1e-12) for y, z in shared)


def draw_bands_x(axes, loss, right):
    """draw_bands, on the x axis."""
    bounds = [b for b, _ in loss.bands] + [float("inf")]
    for i, (bound, _) in enumerate(loss.bands):
        if bound >= right:
            break
        axes.axvspan(bound, min(bounds[i + 1], right), color=BANDS[i], zorder=0, linewidth=0)
        if i > 0:
            axes.axvline(bound, color=INK, linewidth=1.1, linestyle=(0, (4, 3)), zorder=1.5)


def draw_measure(figure, measure, groups, context):
    """One measure, a chart of its own that can be cut out of its figure:
    its title, what the lines are, the lossy encodings against their CRF
    (on a scale fitted to them) with their key, and the lossless encodings
    on the right, on a scale of their own, each diamond with its label and
    its key."""
    grid = figure.add_gridspec(1, 2, width_ratios=[3.2, 1.6])
    axes, side = figure.add_subplot(grid[0]), figure.add_subplot(grid[1])
    colors = [group.color for group in groups]
    lossy = [sorted((r.upload["crf"], measure.value(r), r) for r in g.runs
                    if not r.lossless and measure.value(r) is not None) for g in groups]
    points = [[(x, y) for x, y, _ in line] for line in lossy]
    # The first line, if another lies on it: wide and pale underneath, so
    # that both show.
    under = len(groups) > 1 and any(coincide(points[0], other) for other in points[1:])
    figure.suptitle(f"{measure.title}  ·  {measure.better}", x=0.01, ha="left", fontsize=12, fontweight="bold",
                    color=INK)
    notes = [*context, measure.about,
             "Scale fitted to the values, not from 0 (spanning at least ±5% around them): read them on the axis.",
             *([f"Wide pale line: {groups[0].label}, with others lying on it."] if under else [])]
    # One line at a time, from the bottom up; a line in **...** is bold.
    # The title's padding keeps their room: out of the layout, the long
    # lines would make it narrow the plot.
    axes.set_title(" ", pad=6 + 12 * len(notes))
    for i, line in enumerate(reversed(notes)):
        bold = line.startswith("**")
        axes.annotate(line.strip("*"), (0, 1), xycoords="axes fraction", xytext=(0, 8 + 12 * i),
                      textcoords="offset points", fontsize=8.5, color=INK if bold else INK_SECONDARY,
                      fontweight="bold" if bold else "normal").set_in_layout(False)
    handles, values = [], []
    for n, (group, line, color) in enumerate(zip(groups, lossy, colors)):
        if not line:
            continue
        style = {"color": color, "marker": MARKERS[n % len(MARKERS)], "linestyle": LINESTYLES[n % len(LINESTYLES)],
                 "markersize": 7, "linewidth": 1.8, "markeredgecolor": SURFACE, "markeredgewidth": 1.2,
                 "zorder": 3}
        if n == 0 and under:
            style.update(linewidth=7, alpha=0.35, markersize=11, linestyle="-", solid_capstyle="round", zorder=2)
        handles += axes.plot([x for x, _, _ in line], [y for _, y, _ in line], label=group.label, **style)
        values += [y for _, y, _ in line]
    crfs = sorted({x for line in points for x, _ in line})
    if values:
        bottom, top = fitted_limits(values)
        if measure.bands:
            draw_bands(axes, measure.bands, top)
            handles += band_key(measure.bands)
        axes.set_ylim(bottom, top)
    if crfs:
        axes.set_xticks(crfs, [str(c) for c in crfs])
        # Room on the left for the table's markers, before the first value.
        axes.set_xlim(crfs[0] - (2.5 if measure.table else 1.5), crfs[-1] + 1.5)
    axes.set_xlabel("CRF (lower = higher quality, bigger video)")
    axes.set_ylabel(measure.unit)
    axes.grid(axis="x", visible=False)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=9)
    # The table under the CRF axis: one row per line, its marker and name on
    # the left, one value per CRF under its tick.
    below = 34  # points under the axis: the ticks and the CRF label
    if measure.table:
        what, text = measure.table
        below += 4
        axes.annotate(what, (0, 0), xycoords="axes fraction", xytext=(0, -below), textcoords="offset points",
                      ha="left", va="top", fontsize=8, color=INK_SECONDARY, fontweight="bold")
        for n, (group, line, color) in enumerate(zip(groups, lossy, colors)):
            if not line:
                continue
            below += 11
            y = -below - 1
            axes.annotate(group.short, (axes.get_xlim()[0], 0), xycoords=("data", "axes fraction"), xytext=(-4, y),
                          textcoords="offset points", ha="right", va="top", fontsize=7.5, color=INK)
            axes.plot([axes.get_xlim()[0]], [0], MARKERS[n % len(MARKERS)], color=color, markersize=5,
                      transform=offset_copy(axes.get_xaxis_transform(), fig=axes.figure, x=4, y=y - 3.5,
                                            units="points"), clip_on=False, zorder=3)
            for x, _, r in line:
                axes.annotate(text(r), (x, 0), xycoords=("data", "axes fraction"), xytext=(0, y),
                              textcoords="offset points", ha="center", va="top", fontsize=7.5, color=INK)
        below += 8
    axes.legend(handles=handles, loc="upper left", ncol=2, borderaxespad=0, fontsize=8, title="lossy encodings",
                title_fontsize=8, alignment="left",
                bbox_to_anchor=(0, 0), bbox_transform=offset_copy(axes.transAxes, fig=axes.figure, y=-below - 8,
                                                                  units="points"))
    # The lossless encodings: one row each, a diamond at its value with the
    # value beside it.
    rows = [(group, color, r) for group, color in zip(groups, colors) for r in group.runs
            if r.lossless and measure.value(r) is not None]
    side.grid(axis="y", visible=False)
    side_handles = []
    if rows:
        found = [measure.value(r) for _, _, r in rows]
        marks = [(i, group.yuv444.get(measure.row_floor)) for i, (group, _, _) in enumerate(rows)
                 if measure.row_floor and group.yuv444.get(measure.row_floor) is not None]
        left, right = fitted_limits(found + [value for _, value, _, _ in measure.floors] + [v for _, v in marks])
        # Room for the values: right of the diamonds, or left of them where
        # each row has its 4:4:4 mark on the right.
        if marks:
            left -= (right - left) * 0.5
        else:
            right += (right - left) * 0.5
        if measure.bands:
            draw_bands_x(side, measure.bands, right)
        for i, (group, color, r) in enumerate(rows):
            value = measure.value(r)
            side_handles += side.plot(value, i, LOSSLESS_MARKER, color=color, markersize=9, markeredgecolor=SURFACE,
                                      markeredgewidth=1.2, zorder=3, linestyle="none",
                                      label=group.label + (f": {in_bold(measure.table[1](r))}" if measure.table else ""))
            # The unit is the panel's axis label.
            side.annotate(short_number(value), (value, i), xytext=(-9 if marks else 9, 0),
                          textcoords="offset points", va="center", ha="right" if marks else "left", fontsize=8, color=INK, zorder=4,
                          bbox={"boxstyle": "square,pad=0.15", "facecolor": SURFACE, "edgecolor": "none",
                                "alpha": 0.85})
        if marks:
            for i, value in marks:
                side.plot(value, i, "|", color=CONVERSION_444, markersize=16, markeredgewidth=1.6, zorder=2.5)
                # Its value beside it: it falls between the axis ticks.
                side.annotate(short_number(value), (value, i), xytext=(6, 0), textcoords="offset points",
                              va="center", fontsize=7.5, color=INK_SECONDARY, zorder=4)
            side_handles.append(Line2D([], [], marker="|", linestyle="none", color=CONVERSION_444, markersize=12,
                                       markeredgewidth=1.6, label=CONVERSION_444_LABEL + ", for each dataset"))
        # Their values are in the key, in bold: they fall between the axis ticks.
        for label, value, color, dashed in measure.floors:
            side_handles.append(side.axvline(value, color=color, linewidth=1.5, zorder=1.6,
                                             label=f"{label}: {in_bold(short_number(value))}",
                                             linestyle=(0, (4, 3)) if dashed else "-"))

        side.set_xlim(left, right)
        side.set_yticks(range(len(rows)), [group.short for group, _, _ in rows])
        side.set_ylim(len(rows) - 0.5, -0.5)
        side.tick_params(axis="y", colors=INK, labelcolor=INK, labelsize=8.5)
        side.legend(handles=side_handles, loc="upper left", ncol=1, borderaxespad=0, fontsize=8,
                    title="lossless encodings", title_fontsize=8, alignment="left", bbox_to_anchor=(0, 0),
                    bbox_transform=offset_copy(side.transAxes, fig=side.figure, y=-42, units="points"))
    else:
        side.set_yticks([])
        side.text(0.5, 0.5, "no lossless encoding", transform=side.transAxes, ha="center", color=MUTED)
    side.set_xlabel(measure.unit)


def measure_height(measure, groups):
    """The height of a measure's chart, in inches: the same plot area for
    every measure, plus its table and its keys."""
    entries = max(len(groups) + (len(measure.bands.bands) if measure.bands else 0), len(groups) + 2)
    return 3.9 + (0.17 * len(groups) + 0.45 if measure.table else 0) + 0.15 * ((entries + 1) // 2)


def measure_figures(measures, groups, context, folder, stacks):
    """Every measure as a chart of its own (<name>.svg), and `stacks`:
    (file, title, measures) figures of several measures, one above the
    other, each still a chart of its own so that any one can be cut out."""
    for measure in measures:
        figure = plt.figure(figsize=(9.6, measure_height(measure, groups)), layout="constrained")
        draw_measure(figure, measure, groups, context)
        save(figure, folder / f"{measure.name}.svg")
    for name, title, stack in stacks:
        heights = [measure_height(measure, groups) for measure in stack]
        figure = plt.figure(figsize=(9.6, sum(heights) + 0.5), layout="constrained")
        figure.suptitle(title, x=0.01, ha="left", fontsize=14, fontweight="bold", color=INK)
        for sub, measure in zip(figure.subfigures(len(stack), 1, hspace=0.04, height_ratios=heights), stack):
            draw_measure(sub, measure, groups, context)
        save(figure, folder / name)


def quality_measures(size, floors=None, row_floors=False):
    """The two butteraugli measures, with each encoding's video size in the
    table under the CRF axis: `size` is (what it is, its text for a run).
    `floors(loss)`: the lossless panel's lines for a loss; `row_floors`:
    each group's own 4:4:4 conversion, a mark on its row, instead."""
    floors = floors or (lambda loss: [])
    return [
        Measure("butteraugli-max", "Butteraugli max-norm, the worst spot of each frame", "max-norm",
                lambda r: r.mean_loss(BUTTERAUGLI_MAX), "lower is better",
                "Mean over the frames. Shaded bands: how visible the worst spot is (key below). "
                "Under the CRF axis: each encoding's video size.",
                bands=BUTTERAUGLI_MAX, table=size, floors=floors(BUTTERAUGLI_MAX),
                row_floor=BUTTERAUGLI_MAX.conversion if row_floors else None),
        Measure("butteraugli-3norm", "Butteraugli 3-norm, the whole frame", "3-norm",
                lambda r: r.mean_loss(BUTTERAUGLI_3NORM), "lower is better",
                "Mean over the frames. No visibility scale: it ranks encodings of the same images. "
                "Under the CRF axis: each encoding's video size.",
                table=size, floors=floors(BUTTERAUGLI_3NORM),
                row_floor=BUTTERAUGLI_3NORM.conversion if row_floors else None),
    ]


def presets_crfs_summary(sequence, runs, yuv444=None):
    """The README of presets-crfs/: its charts, and every encoding of the
    batch in one table. `yuv444`: what the YUV 4:4:4 conversion alone
    loses, by column, if measured."""
    f420 = [f for f in (r.mean_loss(BUTTERAUGLI_MAX, BUTTERAUGLI_MAX.conversion) for r in runs) if f is not None]
    md = [
        f"# {sequence}: presets and CRFs compared\n",
        f"Generated by `scripts/report.py` from every encoding in `video-results/`; do not edit. "
        f"{len(runs)} encodings. Every chart is one measure against the CRF, one line per SVT-AV1 preset, on a "
        "scale fitted to the values (not from 0). The lossless encodings, which have no CRF and lose only the "
        "YUV 4:2:0 conversion, are on the right, on a scale of their own, with their values in bold in the key. "
        "Every chart has its own title, key and axes, so that it can be used on its own. Lower is better "
        "everywhere.\n",
        *([f"In the lossless panels of the quality charts, the solid orange line is the YUV 4:2:0 conversion and "
           f"the dashed black one the YUV 4:4:4 conversion: the floor of a lossless 4:4:4 video, measured without "
           f"encoding, as SVT-AV1 encodes only 4:2:0. 4:4:4 loses no less than 4:2:0 here (butteraugli max-norm "
           f"{yuv444[BUTTERAUGLI_MAX.conversion]:.2f} against "
           f"{sum(f420) / len(f420):.2f}): the PNGs carry almost no color detail finer than 2x2 pixels, so both "
           f"floors are mostly the rounding to 8-bit limited range.\n"] if yuv444 and f420 else []),
        "## Every setting at once\n",
        "Video size, butteraugli max-norm and 3-norm, and the encoder's working time, one above the other.\n",
        "![Presets and CRFs: size, quality and encoding time](compare-presets-data.svg)\n",
        "## Video size\n",
        "![Video size against CRF](video-size.svg)\n",
        "## Butteraugli max-norm\n",
        "The worst spot of each frame, over its visibility bands: whether the worst differences can be seen. "
        "The table under the CRF axis gives each encoding's video size.\n",
        "![Butteraugli max-norm against CRF](butteraugli-max.svg)\n",
        "## Butteraugli 3-norm\n",
        "The whole frame. It has no visibility scale: it ranks the settings of this batch, not encodings of "
        "different batches.\n",
        "![Butteraugli 3-norm against CRF](butteraugli-3norm.svg)\n",
        "## Encoding time\n",
        "The encoder's wall time, which also counts waiting for decoded PNGs ([`encoder-wall-time.svg`]"
        "(encoder-wall-time.svg)), and its working time, the AV1 encoding itself ([`encoder-working-time.svg`]"
        "(encoder-working-time.svg)), one above the other. One run per encoding: a single run can be off by "
        "~10%.\n",
        "![Encoding time against CRF](encode-time.svg)\n",
        "## Every encoding\n",
        "Means over the frames; *4:2:0 conversion alone* is the floor of every encoding"
        + ("; *4:4:4 conversion alone*, of a lossless 4:4:4 video.\n" if yuv444 else ".\n"),
        "| Preset | CRF | Video size | Smaller than PNG | Butteraugli max-norm | Butteraugli 3-norm "
        "| Encoder working | Details |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        u = r.upload
        name = f"preset_{u['preset']}_" + ("lossless" if r.lossless else f"crf_{u['crf']}")
        md.append(
            f"| {u['preset']} | {'lossless' if r.lossless else u['crf']} | {r.video_mib:.1f} MiB | {r.ratio:.{1 if r.ratio < 10 else 0}f}x | "
            f"{number(r.mean_loss(BUTTERAUGLI_MAX))} | {number(r.mean_loss(BUTTERAUGLI_3NORM))} | "
            f"{seconds(u['encode_seconds'] - u['encode_wait_seconds'])} | [summary](../{name}/summary.md) |")
    floors = [[f for f in (r.mean_loss(loss, loss.conversion) for r in runs) if f is not None]
              for loss in (BUTTERAUGLI_MAX, BUTTERAUGLI_3NORM)]
    if all(floors):
        md.append(f"| 4:2:0 conversion alone | | | | {sum(floors[0]) / len(floors[0]):.2f} "
                  f"| {sum(floors[1]) / len(floors[1]):.2f} | | |")
    if yuv444:
        md.append(f"| 4:4:4 conversion alone | | | | {yuv444[BUTTERAUGLI_MAX.conversion]:.2f} "
                  f"| {yuv444[BUTTERAUGLI_3NORM.conversion]:.2f} | | |")
    md.append("")
    return "\n".join(md)


def source_frames(batch):
    """The frame numbers of the sequence a batch's images are, from its
    timestamps.csv (…/010_8K.png is frame 10)."""
    with open(WORKSPACE / "datasets" / batch / "timestamps.csv", newline="") as f:
        return [int(re.search(r"(\d+)_[^/]*\.png$", r["image"]).group(1)) for r in csv.DictReader(f)]


def timing_text(upload):
    """The frame rate the server chose, from upload.json."""
    timing = upload.get("timing")
    if not timing:
        return "not recorded"
    if timing["mode"] == "constant":
        rate = timing["frame_rate"]
        return f"constant {rate['num']}/{rate['den']}"
    return timing["mode"]


def per_frame_mib(run):
    return run.video_mib / run.upload["images"]


def working_ms_per_frame(run):
    u = run.upload
    return (u["encode_seconds"] - u["encode_wait_seconds"]) / u["images"] * 1000


def datasets_summary(preset, datasets, yuv444s):
    """The README of dataset-comparisons/: which images each dataset is,
    the charts, and every encoding in one table. `yuv444s`: by batch, what
    its YUV 4:4:4 conversion alone loses (see yuv444_floors)."""
    md = [
        f"# Datasets compared: SVT-AV1 preset {preset}\n",
        "Generated by `scripts/report.py --compare-datasets` from every encoding in `video-results/`; do not edit. "
        f"Each dataset is one batch of the [timestamp datasets](../../datasets/README.md), encoded with preset "
        f"{preset} at every CRF and losslessly. Every chart is one measure against the CRF, one line per dataset "
        "in the same color everywhere, on a scale fitted to the values (not from 0). The lossless encodings are "
        "on the right, on a scale of their own, with their values in bold in the key. Every chart has its own "
        "title, key and axes, so that it can be used on its own. Lower is better everywhere.\n",
        "## The datasets\n",
        "Every image of a batch is one frame, at its capture time. The batches differ in their capture times "
        "and, for some, in which frames of the sequence they hold: SVT-AV1 compresses a frame against the ones "
        "before it, so frames further apart in the sequence differ more and cost more bits. Compare sizes and "
        "quality between datasets with the same frames; between the others, part of the difference is the "
        "content, not the timing.\n",
        "| Dataset | Images | Frames of the sequence | Capture span | Frame rate | Encodings |",
        "|---|---|---|---|---|---|",
    ]
    for (batch, runs) in datasets:
        frames, u = source_frames(batch), runs[0].upload
        skipped = frames[-1] - frames[0] + 1 - len(frames)
        span = (u["last_timestamp_ns"] - u["first_timestamp_ns"]) / 1e9
        md.append(f"| [`{batch}`](../{batch}/presets-crfs/README.md) | {u['images']} | {frames[0]}–{frames[-1]}"
                  f"{f', {skipped} skipped' if skipped else ', none skipped'} | {span:.3f} s | "
                  f"{timing_text(u)} | {len(runs)} |")
    md.append("")
    md += [f"`{batch}` was encoded before the upload report recorded the frame rate (*not recorded*); see "
           "[Constant or variable frame rate](../../README.md#constant-or-variable-frame-rate) for what the server "
           "chooses.\n" for batch, runs in datasets if not runs[0].upload.get("timing")]
    md += [
        "## Every setting at once\n",
        "Size per frame, bitrate, butteraugli max-norm and 3-norm, and the encoder's working time per frame and "
        "for the whole batch, one above the other. Sizes and times are also per frame, because the datasets have "
        "different numbers of images. The bitrate is over the time the frames cover, so `sparse`, whose frames "
        "are far apart, has a low bitrate even with big frames.\n",
        "![Datasets: size, quality and encoding time](compare-datasets-data.svg)\n",
        "## Video size per frame\n",
        "![Video size per frame against CRF](video-size-per-frame.svg)\n",
        "## Bitrate\n",
        "![Bitrate against CRF](bitrate.svg)\n",
        "## Butteraugli max-norm\n",
        "The worst spot of each frame, over its visibility bands. The table under the CRF axis gives each "
        "encoding's size per frame; in the lossless panel, the black mark on each dataset's row is its YUV 4:4:4 "
        "conversion alone, the floor of a lossless 4:4:4 video.\n",
        "![Butteraugli max-norm against CRF](butteraugli-max.svg)\n",
        "## Butteraugli 3-norm\n",
        "The whole frame, with the same table and marks. It has no visibility scale: it compares encodings of "
        "the same images, so only the datasets with the same frames.\n",
        "![Butteraugli 3-norm against CRF](butteraugli-3norm.svg)\n",
        "## Encoder working time per frame\n",
        "![Encoder working time per frame against CRF](encoder-working-time-per-frame.svg)\n",
        "## Encoder working time for the whole batch\n",
        "The total, not per frame: `sparse` has 20 images, the others 60. Its lossless encoding takes about a "
        "third of theirs, in proportion, but its lossy ones take about as long as theirs: at these CRFs most of "
        "the encoder's working time is a cost per encoding, not per frame, which is also why its time per frame "
        "is higher.\n",
        "![Encoder working time for the whole batch against CRF](encoder-working-total-time.svg)\n",
        "## Every encoding\n",
        "Means over the frames; *4:2:0 conversion alone* is what the YUV 4:2:0 conversion loses on that "
        "dataset's images, the floor of its encodings, and *4:4:4 conversion alone* what the YUV 4:4:4 one "
        "would lose, the floor of a lossless 4:4:4 video.\n",
        "| Dataset | CRF | Video size | Per frame | Bitrate | Smaller than PNG | Butteraugli max-norm "
        "| Butteraugli 3-norm | Encoder working | Per frame | Details |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for batch, runs in datasets:
        for r in runs:
            u = r.upload
            name = f"preset_{u['preset']}_" + ("lossless" if r.lossless else f"crf_{u['crf']}")
            bitrate = r.mbit_per_s()
            md.append(
                f"| `{batch}` | {'lossless' if r.lossless else u['crf']} | {r.video_mib:.2f} MiB | "
                f"{per_frame_mib(r) * 1024:.0f} KiB | {f'{bitrate:.1f} Mbit/s' if bitrate else '–'} | "
                f"{r.ratio:.{1 if r.ratio < 10 else 0}f}x | {number(r.mean_loss(BUTTERAUGLI_MAX))} | "
                f"{number(r.mean_loss(BUTTERAUGLI_3NORM))} | "
                f"{seconds(u['encode_seconds'] - u['encode_wait_seconds'])} | {working_ms_per_frame(r):.0f} ms | "
                f"[summary](../{batch}/{name}/summary.md) |")
        floors = [[f for f in (r.mean_loss(loss, loss.conversion) for r in runs) if f is not None]
                  for loss in (BUTTERAUGLI_MAX, BUTTERAUGLI_3NORM)]
        if all(floors):
            md.append(f"| `{batch}` | 4:2:0 conversion alone | | | | | {sum(floors[0]) / len(floors[0]):.2f} | "
                      f"{sum(floors[1]) / len(floors[1]):.2f} | | | |")
        yuv444 = yuv444s.get(batch, {})
        if BUTTERAUGLI_MAX.conversion in yuv444:
            md.append(f"| `{batch}` | 4:4:4 conversion alone | | | | | {yuv444[BUTTERAUGLI_MAX.conversion]:.2f} | "
                      f"{yuv444[BUTTERAUGLI_3NORM.conversion]:.2f} | | | |")
    md.append("")
    return "\n".join(md)


def compare_datasets(results_dir, preset):
    """Writes dataset-comparisons/: the encodings at `preset` of every
    batch in video-results/, one line per batch."""
    datasets = []
    for batch_dir in sorted(results_dir.glob("*/batch_*")):
        runs = [r for r in encodings(batch_dir) if r.upload["preset"] == preset]
        if runs:
            datasets.append((batch_dir.relative_to(results_dir).as_posix(), runs))
    if not datasets:
        raise SystemExit(f"no encodings with preset {preset} in {results_dir}")
    # The ideal camera first, the reference of the others.
    datasets.sort(key=lambda d: (not d[0].startswith("regular/"), d[0]))
    # One color per dataset, the same in every chart.
    groups = [Group(f"{batch} ({runs[0].upload['images']} images)", batch.split("/")[0], runs,
                    PRESETS[n % len(PRESETS)], yuv444_floors(results_dir / batch))
              for n, (batch, runs) in enumerate(datasets)]
    sizes = sorted({(r.upload["width"], r.upload["height"]) for _, runs in datasets for r in runs})
    images = ", ".join(f"{runs[0].upload['images']} for {batch.split('/')[0]}" for batch, runs in datasets)
    context = [f"**Input: PNG images of {' or '.join(f'{w}x{h}' for w, h in sizes)}: {images}.**",
               f"SVT-AV1 preset {preset}: one line per dataset, one point per CRF. Size and time per frame: "
               "the datasets have different numbers of images.",
               "rate-change and sparse hold other frames of the sequence: they differ in content too, not only "
               "in timing."]
    measures = [
        Measure("video-size-per-frame", "Video size per frame", "MiB", per_frame_mib, "smaller is better",
                "The AV1 video's size divided by its number of frames."),
        Measure("bitrate", "Bitrate, over the time the frames cover", "Mbit/s", lambda r: r.mbit_per_s(),
                "smaller is better",
                "Frames far apart in time (sparse) make a low bitrate even with big frames."),
        *quality_measures(("Video size per frame", lambda r: f"{per_frame_mib(r) * 1024:.0f} KiB"),
                          row_floors=True),
        Measure("encoder-working-time-per-frame", "Encoder working time per frame, the AV1 encoding itself", "ms",
                working_ms_per_frame, "lower is faster",
                "Measured on the server, one run per encoding, each after a 2-minute pause."),
        Measure("encoder-working-total-time", "Encoder working time for the whole batch, the AV1 encoding itself",
                "seconds", lambda r: r.upload["encode_seconds"] - r.upload["encode_wait_seconds"], "lower is faster",
                "A total, not per frame: sparse has 20 images, the others 60. Measured on the server, one run each."),
    ]
    folder = results_dir / "dataset-comparisons"
    folder.mkdir(exist_ok=True)
    measure_figures(measures, groups, context, folder,
                    [("compare-datasets-data.svg", f"Datasets: size, quality and encoding time at preset {preset}",
                      measures)])
    (folder / "README.md").write_text(
        datasets_summary(preset, datasets, {batch: yuv444_floors(results_dir / batch) for batch, _ in datasets}))
    print(f"wrote {folder} ({len(datasets)} datasets)")


def main():
    parser = argparse.ArgumentParser(description="Writes the tables and charts of one encoding's results")
    parser.add_argument("--timestamps", type=Path, default=WORKSPACE / "datasets/regular/batch_060/timestamps.csv",
                        help="the images the video was encoded from")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE / "output",
                        help="where the client, the server and the quality tool wrote their files")
    parser.add_argument("--results-dir", type=Path, default=WORKSPACE / "video-results",
                        help="where to write the results")
    parser.add_argument("--preset", type=int, default=DEFAULT_PRESET, help="SVT-AV1 preset of the video")
    quality = parser.add_mutually_exclusive_group()
    quality.add_argument("--crf", type=int, default=DEFAULT_CRF, help="SVT-AV1 CRF of the video")
    quality.add_argument("--lossless", action="store_true", help="the video was encoded losslessly")
    parser.add_argument("--compare-only", action="store_true",
                        help="only rewrite presets-crfs/, from the encodings already in video-results/")
    parser.add_argument("--compare-datasets", action="store_true",
                        help="only rewrite dataset-comparisons/: every batch in video-results/ encoded with --preset")
    parser.add_argument("--redraw", action="store_true",
                        help="redraw every chart and summary in video-results/ from the files already there, then "
                             "presets-crfs/ of every batch and dataset-comparisons/ with --preset")
    args = parser.parse_args()

    if args.redraw:
        redraw(args.results_dir, args.output_dir, args.preset)
        return
    if args.compare_datasets:
        compare_datasets(args.results_dir, args.preset)
        return
    sequence = sequence_name(args.timestamps)
    if args.compare_only:
        compare(sequence, args.output_dir / sequence_dir(sequence), args.results_dir / sequence_dir(sequence))
        return
    quality = "lossless" if args.lossless else f"crf_{args.crf}"
    encoding = sequence_dir(sequence) / f"preset_{args.preset}_{quality}"
    video_dir = args.output_dir / encoding
    upload, quality = video_dir / f"{TOPIC}.upload.json", video_dir / f"{TOPIC}.quality.csv"
    client = video_dir / f"{TOPIC}.client.json"
    if not (upload.exists() and quality.exists()):
        raise SystemExit(
            f"no results for {sequence} in {encoding.name}: run the server, the "
            f"client and the quality tool first ({upload} and {quality} are missing)")
    run = Run.read(upload, quality, client)

    results = args.results_dir / encoding
    results.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(upload, results / "upload.json")
    shutil.copyfile(quality, results / "quality.csv")
    if quality.with_suffix(".json").exists():
        shutil.copyfile(quality.with_suffix(".json"), results / "quality.json")
    if client.exists():
        shutil.copyfile(client, results / "client.json")
    copy_yuv444(video_dir.parent, results.parent)
    write_encoding(sequence, run, results)
    print(f"wrote {results}")

    compare(sequence, video_dir.parent, results.parent)


def redraw(results_dir, output_dir, preset):
    """Every chart and summary of video-results/ again, from the copies there of
    what the client, the server and the quality tool wrote: after a change
    to the charts, also for the encodings whose videos are gone."""
    for batch_dir in sorted(results_dir.glob("*/batch_*")):
        sequence = sequence_name(Path("datasets") / batch_dir.relative_to(results_dir) / "timestamps.csv")
        copy_yuv444(output_dir / batch_dir.relative_to(results_dir), batch_dir)
        for folder in sorted([*batch_dir.glob("preset_*_crf_*"), *batch_dir.glob("preset_*_lossless")]):
            if (folder / "upload.json").exists() and (folder / "quality.csv").exists():
                write_encoding(sequence, Run.read(folder / "upload.json", folder / "quality.csv",
                                                  folder / "client.json"), folder)
        print(f"redrew {batch_dir}")
        compare(sequence, output_dir / batch_dir.relative_to(results_dir), batch_dir)
    compare_datasets(results_dir, preset)


def copy_yuv444(output_batch, batch_dir):
    """The batch's 4:4:4 conversion values, from output/ to video-results/, if
    they were measured (quality --yuv444-floor)."""
    measured = output_batch / "yuv444.quality.json"
    if measured.exists():
        shutil.copyfile(measured, batch_dir / "yuv444.quality.json")


def write_encoding(sequence, run, results):
    """The charts and summary.md of one encoding, in `results`, with the
    batch's 4:4:4 conversion values if they are in video-results/."""
    run.add_yuv444(results.parent / "yuv444.quality.json")
    # The charts of each subject in a folder of its own; none left at the
    # top from before they were.
    for chart in results.glob("*.svg"):
        chart.unlink()
    # Nor in the folders of earlier layouts.
    for old in ("quality", "client-server"):
        shutil.rmtree(results / old, ignore_errors=True)
    folders = {name: results / name for name in
               ("video-quality", "client-time", "server-time", "client-server-time", "filesize-comparison")}
    for folder in folders.values():
        folder.mkdir(exist_ok=True)
    overview_chart(run, folders["video-quality"] / "loss-overview.svg")
    # No folder left for a measure no longer charted.
    for folder in folders["video-quality"].iterdir():
        if folder.is_dir() and folder.name not in {loss.name for loss in LOSSES}:
            shutil.rmtree(folder)
    for loss in LOSSES:
        folder = folders["video-quality"] / loss.name
        if run.losses(loss):
            folder.mkdir(exist_ok=True)
            loss_chart(run, loss, folder / f"{loss.name}.svg")
        else:
            # Not measured: no empty chart, and none left from an earlier run.
            shutil.rmtree(folder, ignore_errors=True)
    filesize_chart(run, folders["filesize-comparison"] / "filesize-comparison.svg")
    client_chart = folders["client-time"] / "client-time.svg"
    if run.client_times:
        stage_chart(run, "Client", "Measured where the client runs.\nThe stages run one after the other and add up to the total.",
                    run.client_times, run.client["seconds"], client_chart)
    else:
        client_chart.unlink(missing_ok=True)
    stage_chart(run, "Server", "Measured where the server runs.\n"
                "**The stages overlap: it decodes and encodes while the images arrive.**\n"
                f"Encoder working + idle = {seconds(run.upload['encode_seconds'])}.",
                run.server_times, run.upload["seconds"], folders["server-time"] / "server-time.svg")
    timeline = folders["server-time"] / "server-timeline.svg"
    if run.upload.get("frames"):
        timeline_chart(run, timeline)
    else:
        timeline.unlink(missing_ok=True)
    together = folders["client-server-time"] / "client-server-time.svg"
    if run.client_times:
        client_server_chart(run, together)
    else:
        together.unlink(missing_ok=True)
    (results / "summary.md").write_text(summary(sequence, run))


def compare(sequence, output_batch, batch_dir):
    """Writes presets-crfs/, every encoding of the batch in video-results/ to
    compare the settings, after copying the 4:4:4 conversion's values from
    output/ if they were measured."""
    copy_yuv444(output_batch, batch_dir)
    yuv444 = yuv444_floors(batch_dir)
    runs = encodings(batch_dir)
    folder = batch_dir / "presets-crfs"
    folder.mkdir(exist_ok=True)
    presets = sorted({r.upload["preset"] for r in runs})
    groups = [Group(f"preset {p}", f"P{p}", [r for r in runs if r.upload["preset"] == p],
                    PRESET_COLORS[n % len(PRESET_COLORS)]) for n, p in enumerate(presets)]
    u = runs[0].upload
    context = [f"**Input: {sequence}, {u['images']} PNG images of {u['width']}x{u['height']}, "
               f"{runs[0].png_mib:.1f} MiB in all.**",
               f"SVT-AV1 preset{'s' if len(presets) > 1 else ''} {', '.join(map(str, presets))}: one line per "
               "preset, one point per CRF."]

    def floors(loss):
        """The conversions alone, in the lossless panel: what every lossless
        encoding loses, and the floor of a lossless 4:4:4 video."""
        found = [f for f in (r.mean_loss(loss, loss.conversion) for r in runs) if f is not None]
        lines = [(CONVERSION_LABEL, sum(found) / len(found), CONVERSION, False)] if found else []
        if loss.conversion in yuv444:
            lines.append((CONVERSION_444_LABEL, yuv444[loss.conversion], CONVERSION_444, True))
        return lines

    working = Measure("encoder-working-time", "Encoder working time, the AV1 encoding itself", "seconds",
                      lambda r: r.upload["encode_seconds"] - r.upload["encode_wait_seconds"], "lower is faster", "Measured on the server: the encoder's time minus its waits for decoded PNGs.")
    wall = Measure("encoder-wall-time", "Encoder wall time, also waiting for decoded PNGs", "seconds",
                   lambda r: r.upload["encode_seconds"], "lower is faster",
                   "Measured on the server, from the first frame to the file closed. At 8K PNG decoding is the "
                   "bottleneck, so it follows the decoder, not the CRF.")
    measures = [
        Measure("video-size", "Video size", "MiB", lambda r: r.video_mib, "smaller is better",
                "The AV1 video's file size."),
        *quality_measures(("Video size", lambda r: f"{r.video_mib:.1f} MiB"),
                          floors),
        working,
    ]
    measure_figures(measures + [wall], groups, context, folder,
                    [("compare-presets-data.svg", "Presets and CRFs: size, quality and encoding time", measures),
                     ("encode-time.svg", "Encoding time against CRF", [wall, working])])
    (folder / "README.md").write_text(presets_crfs_summary(sequence, runs, yuv444))
    print(f"wrote {folder} ({len(runs)} encodings)")

if __name__ == "__main__":
    main()
