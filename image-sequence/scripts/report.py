#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""Collects the results of one encoding of a batch into the tracked
results/<scenario>/<batch>/preset_<P>_crf_<C>/ folder, the same layout as in
output/:

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
the loss in a published study (see visibility_bands). SSIMULACRA2 and butteraugli also show what the
conversion to YUV 4:2:0 alone lost (the `*_yuv420` columns), before AV1.

Run it after the server and the quality tool:

    uv run scripts/report.py [--timestamps CSV] [--preset P] [--crf C | --lossless]
    uv run scripts/report.py [--timestamps CSV] --compare-only

--compare-only rewrites only presets-crfs/, the comparison of the batch's
encodings, from results/. It also draws what the conversion to YUV 4:4:4
alone loses, the floor of a lossless 4:4:4 video, which SVT-AV1 cannot
encode: measured with `quality --yuv444-floor` into output/, and kept in
results/<scenario>/<batch>/yuv444.quality.json.

A lossless video goes to preset_<P>_lossless/: its only loss is the
conversion to YUV 4:2:0. It has no CRF, so in the charts that compare the
encodings of a batch it gets a place of its own: a panel past the lossy
sizes, or a "lossless" column before the lowest CRF.

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
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

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
# batch's encodings: on the 4:2:0 conversion's line, so far from its orange.
CONVERSION_444 = "#5a6e8c"
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
        return cls(upload, metrics, [int(r["frame"]) for r in rows], max(errors, default=0), decoding, client)

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
             f"{run.settings}."]
    axes.set_title(loss.title, pad=14 + 11 * len(lines))
    for i, line in enumerate(reversed(lines)):
        bold = line.startswith("**")
        axes.annotate(line.strip("*"), (0, 1.03), xycoords="axes fraction", xytext=(0, 11 * i),
                      textcoords="offset points", color=INK if bold else INK_SECONDARY, fontsize=9,
                      fontweight="bold" if bold else "normal")
    if video:
        values = [v for _, v in video + conversion]
        top = max(values) * 1.15 or 1
        # Always show where "can't be seen" ends, for scale.
        if len(loss.bands) > 1:
            top = max(top, loss.bands[1][0] * 1.25)
        draw_bands(axes, loss, top)
        handles = []
        if conversion:
            handles += axes.plot(*zip(*conversion), color=CONVERSION, label=CONVERSION_LABEL, zorder=2)
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
    (tick), over the bands of what the eye sees, each named inside."""
    lines = [f"Mean of {run.upload['images']} frames, {run.settings}. Each row is one measure, on its own scale.",
             "**0 = identical, higher = more lost.**",
             "Shaded bands: how visible the loss is, from light (invisible) to dark (clearly visible), "
             "named inside each band where it fits (every band is in the measure's own chart).",
             "Dashed lines: where one band ends and the next starts. A row without bands has no published "
             "visibility thresholds.",
             "On the right of each row: the band the mean falls in, the worst frame, and what the conversion "
             "alone lost."]
    head = 0.42 + 0.17 * len(lines) + 0.3  # inches: title, description, legend
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
        axes.set_ylim(-0.5, 0.75)
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
        axes.plot([worst[1]] * 2, [-0.18, 0.34], color=INK, linewidth=1.5, zorder=3)
        band = band_of(loss, mean)
        text = f"mean {mean:.2f}" + (f" → {band}" if band else "") + f"   worst {worst[1]:.2f} (frame {worst[0]})"
        if conversion is not None:
            text += f"   conversion alone {conversion:.2f}"
        axes.text(1, 1.02, text, transform=axes.transAxes, ha="right", va="bottom", fontsize=7.5, color=INK)
    legend_y = 1 - (0.42 + 0.17 * len(lines) + 0.02) / height
    figure.legend(handles=[Patch(color=LINE, label=f"mean, {VIDEO_LABEL}"),
                           Patch(color=CONVERSION, label=f"mean, {CONVERSION_LABEL}"),
                           Line2D([], [], color=INK, linewidth=1.5, label="worst frame")],
                  loc="upper left", bbox_to_anchor=(0.015, legend_y), ncol=3, fontsize=8)
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
    md = [
        f"# {sequence}: AV1 encoding results\n",
        f"Generated by `scripts/report.py` from `output/`; do not edit. {u['images']} {images} of "
        f"{u['width']}x{u['height']}, {run.png_mib:.1f} MiB of PNG, encoded as `{u['pixel_format']}` "
        f"with {run.settings}.\n",
        "## What the video lost\n",
        "Every frame of the video compared with its PNG, as a loss: 0 means identical, higher means more "
        "was lost. *Conversion alone* is what turning the PNG into YUV 4:2:0 (half-resolution color) "
        "loses before any encoding; the video's loss includes it.\n",
        "![How much the video lost against the PNG images](video-quality/loss-overview.svg)\n",
        "| Measure | Unit | Mean | Worst frame | Conversion alone | The mean is | What it measures |",
        "|---|---|---|---|---|---|---|",
    ]
    for loss in LOSSES:
        mean, worst = run.mean_loss(loss), run.worst_loss(loss)
        conversion = run.mean_loss(loss, loss.conversion) if loss.conversion else None
        md.append(
            f"| {loss.title} | {loss.unit} | {number(mean)} | "
            f"{'–' if worst is None else f'{worst[1]:.2f} (frame {worst[0]})'} | {number(conversion)} | "
            f"{(band_of(loss, mean) if mean is not None else None) or '–'} | {loss.about} |"
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
        f"| {run.video_mib:.2f} MiB | {f'{bitrate:.1f} Mbit/s' if bitrate else '–'} | {run.ratio:.0f}x |\n",
        "![Size of the PNG images against the video](filesize-comparison/filesize-comparison.svg)\n",
        "## Raw metric values\n",
        "The metrics' own scores, over the measured frames. Higher is better for every metric except "
        "`butteraugli_*`, where lower is better. `*_yuv420`: the PNG against itself converted to YUV 4:2:0, "
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
    """Every encoding of a batch in results/, ordered by preset then CRF,
    the lossless one first."""
    runs = []
    for folder in [*batch_dir.glob("preset_*_crf_*"), *batch_dir.glob("preset_*_lossless")]:
        upload, quality = folder / "upload.json", folder / "quality.csv"
        if upload.exists() and quality.exists():
            runs.append(Run.read(upload, quality, folder / "client.json"))
    return sorted(runs, key=lambda r: (r.upload["preset"], -1 if r.lossless else r.upload["crf"]))


# The lossless encodings' marker, in their preset's color.
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


def lossless_key():
    return Line2D([], [], marker=LOSSLESS_MARKER, linestyle="none", color=MUTED, markersize=6,
                  markeredgecolor=SURFACE, label="lossless (no CRF)")


class CrfAxis:
    """An x-axis of CRFs, with the lossless encodings in a column of their
    own before the lowest CRF (lossless is where lowering the CRF leads),
    past a dotted line."""

    GAP = 6

    def __init__(self, runs):
        self.crfs = sorted({r.upload["crf"] for r in runs if not r.lossless})
        self.has_lossless = any(r.lossless for r in runs)
        self.lossless_x = (self.crfs[0] if self.crfs else 0) - self.GAP

    def x(self, run):
        return self.lossless_x if run.lossless else run.upload["crf"]

    def draw(self, axes, right=2.5):
        left = self.lossless_x if self.has_lossless else (self.crfs[0] if self.crfs else 0)
        axes.set_xlim(left - (2 if self.has_lossless else 1), (self.crfs[-1] if self.crfs else left) + right)
        if self.has_lossless:
            axes.axvline(self.lossless_x + self.GAP / 2, color=MUTED, linewidth=1, linestyle=(0, (1, 2)), zorder=1)

    def ticks(self, axes):
        ticks = ([self.lossless_x] if self.has_lossless else []) + self.crfs
        axes.set_xticks(ticks, (["lossless"] if self.has_lossless else []) + [str(c) for c in self.crfs])
        axes.set_xlabel("CRF (lower = higher quality, bigger video); lossless on the left")


def presets_crfs_chart(sequence, runs, loss, title, notes, path, yuv444=None):
    """The mean of a butteraugli measure over the frames of each encoding of
    a batch against its size: one line per preset, one point per CRF, over
    the measure's visibility bands if it has any, and over what the YUV
    4:2:0 conversion alone loses, the floor of every encoding. `yuv444`:
    what the YUV 4:4:4 conversion alone loses, dashed, if measured."""
    lossless = [r for r in runs if r.lossless and r.mean_loss(loss) is not None]
    if lossless:
        # Far bigger than the lossy videos: past a break in the size axis,
        # sharing the loss axis.
        figure, (axes, side) = plt.subplots(1, 2, figsize=(8.4, 5.2), sharey=True,
                                            gridspec_kw={"width_ratios": [5, 1], "wspace": 0.08})
    else:
        figure, axes = plt.subplots(figsize=(8.4, 5.2))
        side = None
    lines = [f"{sequence}: one point per encoding, labelled with its CRF; one line per SVT-AV1 preset.",
             *notes,
             "Orange line: what the YUV 4:2:0 conversion alone loses, the floor of every encoding.",
             *(["Dashed gray-blue line: what the YUV 4:4:4 conversion alone loses, the floor of a lossless 4:4:4 "
                "video (SVT-AV1 encodes only 4:2:0)."] if yuv444 is not None else []),
             *(["Right, past the break in the size axis: the lossless encodings, which lose only the conversion."]
               if lossless else [])]
    axes.set_title(title, pad=14 + 11 * len(lines))
    for i, line in enumerate(reversed(lines)):
        bold = line.startswith("**")
        axes.annotate(line.strip("*"), (0, 1.03), xycoords="axes fraction", xytext=(0, 11 * i),
                      textcoords="offset points", color=INK if bold else INK_SECONDARY, fontsize=9,
                      fontweight="bold" if bold else "normal")
    handles, top = [], 0
    presets = sorted({r.upload["preset"] for r in runs})
    for n, preset in enumerate(presets):
        points = sorted((r.video_mib, r.mean_loss(loss), r.upload["crf"]) for r in runs
                        if r.upload["preset"] == preset and not r.lossless and r.mean_loss(loss) is not None)
        if not points:
            continue
        top = max(top, *(p[1] for p in points))
        color = PRESETS[n % len(PRESETS)]
        handles += axes.plot([p[0] for p in points], [p[1] for p in points], "-o", color=color, markersize=6,
                             markeredgecolor=SURFACE, markeredgewidth=1.5, label=f"preset {preset}", zorder=3)
        for x, y, crf in points:
            axes.annotate(f"CRF {crf}", (x, y), xytext=(6, 4), textcoords="offset points", fontsize=8, color=INK)
    floors = [r.mean_loss(loss, loss.conversion) for r in runs]
    floors = [f for f in floors if f is not None]
    if floors:
        floor = sum(floors) / len(floors)
        handles.append(axes.axhline(floor, color=CONVERSION, linewidth=1.5, zorder=1.5, label=CONVERSION_LABEL))
        if side:
            side.axhline(floor, color=CONVERSION, linewidth=1.5, zorder=1.5)
        axes.annotate(f"YUV 4:2:0 conversion alone, {floor:.2f}", (1, floor), xycoords=("axes fraction", "data"),
                      xytext=(-4, 4), textcoords="offset points", ha="right", va="bottom", fontsize=8,
                      color=CONVERSION, fontweight="bold")
    if yuv444 is not None:
        # Close to the 4:2:0 line: dashed over it, labelled at the other end.
        for panel in (axes, side) if side else (axes,):
            line = panel.axhline(yuv444, color=CONVERSION_444, linewidth=1.5, linestyle=(0, (4, 3)), zorder=1.6,
                                 label=CONVERSION_444_LABEL)
        handles.append(line)
        axes.annotate(f"YUV 4:4:4 conversion alone, {yuv444:.2f}", (0, yuv444), xycoords=("axes fraction", "data"),
                      xytext=(4, 4), textcoords="offset points", ha="left", va="bottom", fontsize=8,
                      color=CONVERSION_444, fontweight="bold")
    if side:
        # All on the conversion's line, a few MiB apart: their labels go
        # above and below in turn, so that neighbors don't overlap.
        for i, r in enumerate(sorted(lossless, key=lambda r: r.video_mib)):
            color = PRESETS[presets.index(r.upload["preset"]) % len(PRESETS)]
            side.plot(r.video_mib, r.mean_loss(loss), LOSSLESS_MARKER, color=color, markersize=7,
                      markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
            side.annotate(f"P{r.upload['preset']}", (r.video_mib, r.mean_loss(loss)),
                          xytext=(0, 8 if i % 2 == 0 else -8), textcoords="offset points", ha="center",
                          va="bottom" if i % 2 == 0 else "top", fontsize=8, color=INK)
            top = max(top, r.mean_loss(loss))
        sizes = [r.video_mib for r in lossless]
        pad = (max(sizes) - min(sizes)) * 0.6 or max(sizes) * 0.15
        side.set_xlim(min(sizes) - pad, max(sizes) + pad)
        side.set_title("lossless", fontsize=9, fontweight="normal", color=INK_SECONDARY, loc="center")
        side.tick_params(axis="y", left=False)
        side.grid(axis="x", visible=False)
        # The break between the two size axes.
        for panel, x in ((axes, 1), (side, 0)):
            panel.plot([x], [0], marker=[(-1, -2), (1, 2)], markersize=10, color=MUTED, clip_on=False,
                       transform=panel.transAxes, zorder=5)
        handles.append(lossless_key())
    top = top * 1.15 or 1
    if loss.bands:
        draw_bands(axes, loss, top)
        if side:
            draw_bands(side, loss, top)
        handles += band_key(loss)
    axes.set_ylim(0, top)
    axes.set_xlim(left=0)
    axes.margins(x=0.15)
    axes.set_xlabel("video size (MiB)")
    axes.set_ylabel(f"mean {loss.unit} over the frames")
    axes.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, -0.12), ncol=1, borderaxespad=0)
    save(figure, path)


def spread_labels(axes, labels, gap_pt=11):
    """Writes (x, y, text, color) labels to the right of their points,
    moving the ones of each x apart vertically so that none overlap. Call
    it once the layout is final: it works in points on the page."""
    points_per_pixel = 72 / axes.figure.dpi
    for x in sorted({x for x, *_ in labels}):
        placed = None
        for _, y, text, color in sorted((l for l in labels if l[0] == x), key=lambda l: l[1]):
            at = axes.transData.transform((x, y))[1] * points_per_pixel
            shown = at if placed is None else max(at, placed + gap_pt)
            # On a patch of the background, above the lines, which then pass behind it.
            axes.annotate(text, (x, y), xytext=(7, shown - at), textcoords="offset points", ha="left",
                          va="center", fontsize=8, color=color, fontweight="bold", zorder=5,
                          bbox={"boxstyle": "square,pad=0.15", "facecolor": SURFACE, "edgecolor": "none"})
            placed = shown


def encode_time_chart(sequence, runs, path):
    """How long the encoder took for each encoding of a batch, against its
    CRF, in two panels: its wall time, which also counts waiting for
    decoded PNGs, and its working time, the AV1 encoding itself."""
    panels = [("Encoder wall time, also waiting for decoded PNGs", lambda u: u["encode_seconds"]),
              ("Encoder working, the AV1 encoding itself", lambda u: u["encode_seconds"] - u["encode_wait_seconds"])]
    lines = [f"{sequence}: one point per encoding; one line per SVT-AV1 preset. Lower is faster.",
             "**Top: the encoder's wall time. Bottom: the encoder working, the AV1 encoding itself.**",
             "When PNG decoding is the bottleneck, as at 8K, the wall time follows the decoder, not the CRF.",
             "Back-to-back runs heat the machine and slow the later ones.",
             *(["Left of the dotted line: the lossless encodings (diamonds), which have no CRF."]
               if any(r.lossless for r in runs) else [])]
    head = 0.42 + 0.17 * len(lines) + 0.1
    height = 3.0 * len(panels) + head
    figure, rows = plt.subplots(len(panels), 1, figsize=(8.4, height), sharex=True)
    figure.text(0.02, 1 - 0.1 / height, "Encoding time against CRF", ha="left", va="top", fontsize=13,
                fontweight="bold", color=INK)
    for i, line in enumerate(lines):
        bold = line.startswith("**")
        figure.text(0.02, 1 - (0.42 + 0.17 * i) / height, line.strip("*"), va="top", fontsize=9,
                    color=INK if bold else INK_SECONDARY, fontweight="bold" if bold else "normal")
    presets = sorted({r.upload["preset"] for r in runs})
    crf_axis = CrfAxis(runs)
    handles = []
    for axes, (title, value) in zip(rows, panels):
        axes.set_title(title, fontsize=10, pad=6)
        labels, high = [], 0
        for n, preset in enumerate(presets):
            color = PRESETS[n % len(PRESETS)]
            mine = [r for r in runs if r.upload["preset"] == preset]
            points = [(r.upload["crf"], value(r.upload)) for r in mine if not r.lossless]
            if points:
                line, = axes.plot(*zip(*points), "-o", color=color, markersize=5, markeredgecolor=SURFACE,
                                  markeredgewidth=1.2, label=f"preset {preset}", zorder=3)
                if axes is rows[0]:
                    handles.append(line)
            for r in mine:
                if r.lossless:
                    points.append((crf_axis.lossless_x, value(r.upload)))
                    axes.plot(*points[-1], LOSSLESS_MARKER, color=color, markersize=6, markeredgecolor=SURFACE,
                              markeredgewidth=1.2, zorder=3)
            high = max(high, *(y for _, y in points))
            labels += [(x, y, seconds(y), color) for x, y in points]
        axes.set_ylim(0, high * 1.15 or 1)
        crf_axis.draw(axes)
        axes.set_ylabel("seconds")
        axes.grid(axis="x", visible=False)
        axes.labels = labels
    crf_axis.ticks(rows[-1])
    if crf_axis.has_lossless:
        handles.append(lossless_key())
    figure.tight_layout(rect=(0, 0, 1, 1 - head / height), h_pad=1.6)
    for axes in rows:
        spread_labels(axes, axes.labels)
    figure.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.015, 0.0), ncol=len(handles), fontsize=8,
                  frameon=False)
    save(figure, path)


def presets_crfs_overview(sequence, runs, path):
    """Every setting of a batch in one figure, on a shared CRF axis: video
    size, butteraugli max-norm and 3-norm, and the encoder's working time,
    one line per preset."""
    by_name = {loss.name: loss for _, loss, _, _ in COMPARISONS}
    panels = [("Video size", "MiB", lambda r: r.video_mib, None, "smaller is better"),
              ("Butteraugli max-norm, the worst spot (mean over the frames)", "max-norm",
               lambda r: r.mean_loss(by_name["butteraugli-max"]), by_name["butteraugli-max"], "lower is better"),
              ("Butteraugli 3-norm, the whole frame (mean over the frames)", "3-norm",
               lambda r: r.mean_loss(by_name["butteraugli-3norm"]), None, "lower is better"),
              ("Encoder working time, the AV1 encoding itself", "seconds",
               lambda r: r.upload["encode_seconds"] - r.upload["encode_wait_seconds"], None, "lower is faster")]
    # The same images for every encoding of the batch.
    u = runs[0].upload
    lines = [f"{sequence}: every encoding, one line per SVT-AV1 preset; read down a column for one CRF.",
             f"**Input: {u['images']} PNG images of {u['width']}x{u['height']}, {runs[0].png_mib:.1f} MiB in all.**",
             "**Each panel: lower is better.**",
             "The encoder's time is measured on the server; back-to-back runs heat the machine and slow the later ones.",
             *(["Left of the dotted line: the lossless encodings (diamonds); a triangle at the top is off the scale, "
                "with its value."] if any(r.lossless for r in runs) else [])]
    head = 0.42 + 0.17 * len(lines) + 0.1  # inches: title and description
    height = 2.3 * len(panels) + head
    figure, rows = plt.subplots(len(panels), 1, figsize=(8.4, height), sharex=True)
    figure.text(0.02, 1 - 0.1 / height, "Presets and CRFs: size, quality and encoding time",
                ha="left", va="top", fontsize=13, fontweight="bold", color=INK)
    for i, line in enumerate(lines):
        bold = line.startswith("**")
        figure.text(0.02, 1 - (0.42 + 0.17 * i) / height, line.strip("*"), va="top", fontsize=9,
                    color=INK if bold else INK_SECONDARY, fontweight="bold" if bold else "normal")
    presets = sorted({r.upload["preset"] for r in runs})
    crf_axis = CrfAxis(runs)
    handles = []
    for axes, (title, unit, value, banded, note) in zip(rows, panels):
        axes.set_title(f"{title}  ·  {note}", fontsize=10, pad=6)
        high, extra = 0, []
        for n, preset in enumerate(presets):
            color = PRESETS[n % len(PRESETS)]
            mine = [r for r in runs if r.upload["preset"] == preset and value(r) is not None]
            points = [(r.upload["crf"], value(r)) for r in mine if not r.lossless]
            extra += [(value(r), color, preset) for r in mine if r.lossless]
            if not points:
                continue
            high = max(high, *(y for _, y in points))
            line, = axes.plot(*zip(*points), "-o", color=color, markersize=5,
                              markeredgecolor=SURFACE, markeredgewidth=1.2, label=f"preset {preset}", zorder=3)
            if axes is rows[0]:
                handles.append(line)
        # A lossless value ten times the lossy ones (the video size) would
        # flatten them: it goes at the top, off the scale, and its value is
        # written under it, one line per preset.
        off_scale = [e for e in extra if high and e[0] > 10 * high]
        high = max(high, *(e[0] for e in extra if e not in off_scale), 0)
        top_y = high * 1.15 or 1
        for i, (y, color, preset) in enumerate(extra):
            # Side by side in the lossless column, one per preset: their
            # quality is the same, and would hide each other.
            x = crf_axis.lossless_x + (i - (len(extra) - 1) / 2)
            shown = (y, color, preset) in off_scale
            axes.plot(x, top_y * 0.94 if shown else y, "^" if shown else LOSSLESS_MARKER, color=color,
                      markersize=7, markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3, clip_on=False)
        for i, (y, color, preset) in enumerate(off_scale):
            axes.annotate(f"P{preset} {y:.0f} {unit}", (crf_axis.lossless_x, top_y * 0.94), xytext=(0, -12 * (i + 1)),
                          textcoords="offset points", ha="center", va="center", fontsize=8, color=color,
                          fontweight="bold")
        if banded:
            draw_bands(axes, banded, top_y)
        axes.set_ylim(0, top_y)
        crf_axis.draw(axes, right=1)
        axes.set_ylabel(unit)
        axes.grid(axis="x", visible=False)
    crf_axis.ticks(rows[-1])
    keys = handles + ([lossless_key()] if crf_axis.has_lossless else []) + band_key(by_name["butteraugli-max"])
    # One row: the presets, then the max-norm bands.
    figure.legend(handles=keys, loc="upper left", bbox_to_anchor=(0.015, 0.0), ncol=len(keys), fontsize=8,
                  frameon=False, columnspacing=1.2)
    figure.tight_layout(rect=(0, 0, 1, 1 - head / height), h_pad=1.4)
    save(figure, path)


def presets_crfs_summary(sequence, runs, yuv444=None):
    """The README of presets-crfs/: its charts, and every encoding of the
    batch in one table. `yuv444`: what the YUV 4:4:4 conversion alone
    loses, by column, if measured."""
    by_name = {loss.name: loss for _, loss, _, _ in COMPARISONS}
    md = [
        f"# {sequence}: presets and CRFs compared\n",
        f"Generated by `scripts/report.py` from every encoding in `results/`; do not edit. "
        f"{len(runs)} encodings, one line per SVT-AV1 preset in each chart. Lower is better everywhere; "
        "in the size charts, down and left is smaller and better. The lossless encodings are diamonds: "
        "they have no CRF and lose only the YUV 4:2:0 conversion.\n",
        *(["The dashed gray-blue line is what the conversion to YUV 4:4:4 alone loses: the floor of a lossless 4:4:4 video, "
           "measured without encoding, as SVT-AV1 encodes only 4:2:0. The PNGs carry almost no color detail finer "
           "than 2x2 pixels, so 4:2:0 loses almost nothing more than 4:4:4: both floors are mostly the rounding "
           "to 8-bit limited range.\n"] if yuv444 else []),
        "## Every setting at once\n",
        "Video size, butteraugli max-norm and 3-norm, and the encoder's working time, on a shared CRF axis: "
        "read down a column for one CRF.\n",
        "![Presets and CRFs: size, quality and encoding time](compare-presets-data.svg)\n",
        "## Butteraugli max-norm against size\n",
        "The worst spot of each frame, over its visibility bands: whether the worst differences can be seen.\n",
        "![Butteraugli max-norm against video size](butteraugli-max.svg)\n",
        "## Butteraugli 3-norm against size\n",
        "The whole frame. It has no visibility scale: it ranks the settings of this batch, not encodings of "
        "different batches.\n",
        "![Butteraugli 3-norm against video size](butteraugli-3norm.svg)\n",
        "## Encoding time\n",
        "The encoder's wall time, which also counts waiting for decoded PNGs, and its working time, the AV1 "
        "encoding itself. Back-to-back runs heat the machine and slow the later ones.\n",
        "![Encoding time against CRF](encode-time.svg)\n",
        "## Every encoding\n",
        "Means over the frames; *conversion alone* is the floor of every encoding"
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
            f"{number(r.mean_loss(by_name['butteraugli-max']))} | {number(r.mean_loss(by_name['butteraugli-3norm']))} | "
            f"{seconds(u['encode_seconds'] - u['encode_wait_seconds'])} | [summary](../{name}/summary.md) |")
    floors = {loss.name: [f for f in (r.mean_loss(loss, loss.conversion) for r in runs) if f is not None]
              for loss in by_name.values()}
    if all(floors.values()):
        md.append(f"| conversion alone | | | | {sum(floors['butteraugli-max']) / len(floors['butteraugli-max']):.2f} "
                  f"| {sum(floors['butteraugli-3norm']) / len(floors['butteraugli-3norm']):.2f} | | |")
    if yuv444:
        md.append(f"| 4:4:4 conversion alone | | | | {yuv444[by_name['butteraugli-max'].conversion]:.2f} "
                  f"| {yuv444[by_name['butteraugli-3norm'].conversion]:.2f} | | |")
    md.append("")
    return "\n".join(md)


# The measures that compare the encodings of a batch: (file, loss, title,
# notes). The 3-norm has no visibility scale but ranks the settings of one
# batch; the max-norm says whether the worst spot is visible.
COMPARISONS = [
    ("butteraugli-3norm.svg",
     Loss("butteraugli-3norm", "butteraugli_3norm", "", "butteraugli 3-norm", same,
          conversion="butteraugli_3norm_yuv420"),
     "Butteraugli 3-norm (whole frame) against video size",
     ["**Lower is better: less visible loss over the whole frame. Down and left: smaller and better.**",
      "The 3-norm has no visibility scale: compare the encodings of one batch, not across batches."]),
    ("butteraugli-max.svg",
     Loss("butteraugli-max", "butteraugli_max", "", "butteraugli max-norm", same,
          bands=next(loss.bands for loss in LOSSES if loss.name == "butteraugli-max"),
          conversion="butteraugli_max_yuv420"),
     "Butteraugli max-norm (worst spot of the frame) against video size",
     ["**Lower is better: the worst spot of a frame is less visible. Down and left: smaller and better.**",
      "Shaded bands: how visible the worst spot is (key below); dashed lines: where they meet."]),
]


def main():
    parser = argparse.ArgumentParser(description="Writes the tables and charts of one encoding's results")
    parser.add_argument("--timestamps", type=Path, default=WORKSPACE / "datasets/regular/batch_060/timestamps.csv",
                        help="the images the video was encoded from")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE / "output",
                        help="where the client, the server and the quality tool wrote their files")
    parser.add_argument("--results-dir", type=Path, default=WORKSPACE / "results",
                        help="where to write the results")
    parser.add_argument("--preset", type=int, default=DEFAULT_PRESET, help="SVT-AV1 preset of the video")
    quality = parser.add_mutually_exclusive_group()
    quality.add_argument("--crf", type=int, default=DEFAULT_CRF, help="SVT-AV1 CRF of the video")
    quality.add_argument("--lossless", action="store_true", help="the video was encoded losslessly")
    parser.add_argument("--compare-only", action="store_true",
                        help="only rewrite presets-crfs/, from the encodings already in results/")
    args = parser.parse_args()

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
    print(f"wrote {results}")

    compare(sequence, video_dir.parent, results.parent)


def compare(sequence, output_batch, batch_dir):
    """Writes presets-crfs/, every encoding of the batch in results/ to
    compare the settings, after copying the 4:4:4 conversion's values from
    output/ if they were measured."""
    measured = output_batch / "yuv444.quality.json"
    if measured.exists():
        shutil.copyfile(measured, batch_dir / "yuv444.quality.json")
    yuv444 = yuv444_floors(batch_dir)
    runs = encodings(batch_dir)
    folder = batch_dir / "presets-crfs"
    folder.mkdir(exist_ok=True)
    for name, loss, title, notes in COMPARISONS:
        presets_crfs_chart(sequence, runs, loss, title, notes, folder / name, yuv444.get(loss.conversion))
    encode_time_chart(sequence, runs, folder / "encode-time.svg")
    presets_crfs_overview(sequence, runs, folder / "compare-presets-data.svg")
    (folder / "README.md").write_text(presets_crfs_summary(sequence, runs, yuv444))
    print(f"wrote {folder} ({len(runs)} encodings)")


if __name__ == "__main__":
    main()
