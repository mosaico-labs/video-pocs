#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.9"]
# ///
"""Turns the client reports written by run-all.sh into the committed results,
all under <results dir>:

- videos.csv: the reports flattened into one row per video;
- per-video/: the per-video charts, in three folders:
  - file-size-comparison/: the video size against an Arrow IPC file (.arrow)
    of the same batches and the Parquet files the server stored;
  - server-time/: the upload time, the server receive time and, inside it,
    the server's Parquet encode and write times (all-videos.png is also the
    upload time chart of RESULTS.md);
  - client-time/: the client steps before the upload: reading the file,
    splitting it into record batches and encoding them as Flight messages.
  Each folder holds one chart per video, <dataset>/<resolution>/<codec>/<video>.png,
  and a README.md index; the two time folders also hold all-videos.png, the
  same times for every video. per-video/README.md links the three folders.
- RESULTS.md: the summary of the run.

Everything listed above is generated output: it is written again on every
run, and per-video/ is deleted first. Charts are saved as PNGs without
metadata, the per-video ones with 256 colors, so the same data always gives
the same, small files.

Usage: uv run analyze.py [results dir]   (default: poc1/results)
"""
import csv
import io
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator  # noqa: E402
from PIL import Image  # noqa: E402

RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "results"
OUT = RESULTS / "per-video"
SIZE_DIR = OUT / "file-size-comparison"
SERVER_DIR = OUT / "server-time"
CLIENT_DIR = OUT / "client-time"
ALL_VIDEOS = "all-videos.png"
RESULTS_MD = RESULTS / "RESULTS.md"
MIB = 1024 * 1024

CODECS = {"av1": "AV1", "h264": "H.264", "h265": "H.265", "hevc": "H.265", "vp9": "VP9"}
# Order of the resolutions from smallest to largest.
RESOLUTIONS = ["360p", "480p", "720p", "1080p", "1K", "2K", "4K", "8K"]

VIDEO_COLOR, ARROW_COLOR, STORED_COLOR = "#898781", "#eb6834", "#2a78d6"
INK, MUTED, GRID, AXIS, BAND = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#f4f3ef"
# Server time steps: upload, server receive, Parquet encode, Parquet write.
SERVER_COLORS = ["#898781", "#c3c2b7", "#7b4fd6", "#1baf7a"]
# Client steps: read, chunk, Flight encode.
CLIENT_COLORS = ["#2a78d6", "#eda100", "#eb6834"]
# One fixed color per codec.
CODEC_COLORS = {"AV1": "#2a78d6", "H.264": "#eb6834", "H.265": "#1baf7a", "VP9": "#eda100"}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def size(n: float) -> str:
    """Byte count in the largest binary unit that keeps it at least 1."""
    if abs(n) < 1024:
        return f"{n:.0f} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit == "KiB" else f"{n:.2f} {unit}"


def size_diff(actual: float, base: float) -> str:
    """Signed size difference, e.g. +38.1 KiB."""
    return ("+" if actual >= base else "-") + size(abs(actual - base))


def extra_pct(stored: float, video: float) -> float:
    return (stored / video - 1) * 100


def ms(value: float) -> str:
    """Milliseconds with 3 significant digits, so 0.0121 ms stays readable."""
    return f"{value:.3g} ms"


def duration(ms_total: float) -> str:
    return f"{ms_total / 1000:.2f} s" if ms_total >= 1000 else f"{ms_total:.3g} ms"


def share(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if part / whole >= 0.01 else f"{part / whole:.2%}"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")


# ---------------------------------------------------------------------------
# videos.csv
# ---------------------------------------------------------------------------

def describe(dataset: str, file: str) -> tuple[str, str, str]:
    """Returns (source, resolution, codec) for a video."""
    parts = Path(file).stem.lower().split("_")
    codec = next(CODECS[p] for p in parts if p in CODECS)
    if "LargeFiles" in dataset:
        # NNN_<codec>_<resolution>.mp4
        resolution = parts[-1].upper().replace("P", "p")
        return "SEPE 8K", resolution, codec
    resolution = re.search(r"(\d+p)", dataset).group(1)
    return "Big Buck Bunny", resolution, codec


def load_reports() -> tuple[dict, list[dict]]:
    """Returns the run settings and one row per video, in report order."""
    reports = [json.loads(p.read_text()) for p in sorted((RESULTS / "reports").glob("*.json"))]
    rows = []
    for data in reports:
        dataset = data["config"]["dataset"]
        for v in data["videos"]:
            source, resolution, codec = describe(dataset, v["file"])
            rows.append({
                "file": v["file"],
                "source": source,
                "resolution": resolution,
                "resolution_rank": RESOLUTIONS.index(resolution),
                "codec": codec,
                "complete": v["complete"],
                "chunks_sent": v["chunks_sent"],
                "chunks_received": v["chunks_received"],
                "file_bytes": v["file_bytes"],
                "bytes_received": v["bytes_received"],
                "arrow_bytes": v["arrow_bytes"],
                "format": v["format"],
                "stored_bytes": v["stored_bytes"],
                "stored_files": v["stored_files"],
                "upload_ms": round(v["upload_ms"], 3),
                "server_receive_ms": round(v["server_receive_ms"], 3),
                "server_encode_ms": round(v["server_encode_ms"], 3),
                "server_store_ms": round(v["server_store_ms"], 3),
                "read_ms": round(v["read_ms"], 3),
                "chunk_ms": round(v["chunk_ms"], 4),
                "encode_ms": round(v["encode_ms"], 3),
            })
    return reports[0]["config"], rows


def write_csv(rows: list[dict]) -> None:
    out = RESULTS / "videos.csv"
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} videos -> {out}")


# ---------------------------------------------------------------------------
# per-video/ charts
# ---------------------------------------------------------------------------

def style(ax) -> None:
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)


def bar_labels(ax, bars, labels) -> None:
    for bar, label in zip(bars, labels):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), label,
                ha="center", va="bottom", fontsize=8.5, color=INK, linespacing=1.3)


def header(fig, row: dict) -> None:
    fig.suptitle(row["file"], x=0.1, y=0.97, ha="left", fontsize=12, color=INK, fontweight="bold")
    fig.text(0.1, 0.895, f"{row['source']} · {row['resolution']} · {row['codec']} · {row['chunks_received']} chunks "
             f"· {row['format']} format", fontsize=8.5, color=MUTED, va="bottom")


def write_png(fig, path: Path, palette: bool = True) -> None:
    """Saves `fig` as a PNG without metadata, so the file only changes when the
    data does. With `palette`, it is a 256-color PNG: the per-video charts have
    a few hundred colors, mostly anti-aliasing shades, so this is under half
    the size of matplotlib's PNG with no visible change. The overviews keep
    every color: their overlapping translucent dots need more than 256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not palette:
        fig.savefig(path, metadata={"Software": None})
        plt.close(fig)
        return
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    image = Image.open(buf).convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                    dither=Image.Dither.NONE)
    image.save(path, optimize=True)


def save(fig, path: Path) -> None:
    fig.subplots_adjust(left=0.1, right=0.97, top=0.78, bottom=0.2)
    write_png(fig, path)


def size_chart(row: dict, path: Path) -> None:
    """Video vs .arrow vs Parquet size of one video."""
    video, arrow, stored = row["file_bytes"], row["arrow_bytes"], row["stored_bytes"]
    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=120, facecolor="white")
    sizes = (video, arrow, stored)
    bars = ax.bar([0, 1, 2], [n / MIB for n in sizes], width=0.55,
                  color=[VIDEO_COLOR, ARROW_COLOR, STORED_COLOR], zorder=3)
    bar_labels(ax, bars, [size(video), f"{size(arrow)}\n{size_diff(arrow, video)}",
                          f"{size(stored)}\n{size_diff(stored, video)}"])
    ax.set_xticks([0, 1, 2], ["Video", ".arrow", f"Parquet ({row['stored_files']} files)"])
    ax.set_ylim(0, max(sizes) / MIB * 1.25)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_ylabel("MiB", fontsize=9, color=MUTED)
    ax.set_title("File size: video vs .arrow vs Parquet", loc="left", fontsize=10, color=INK, fontweight="bold")
    ax.text(0, -0.14, f".arrow {(arrow / video - 1) * 100:+.3f}% · Parquet {(stored / video - 1) * 100:+.3f}% "
            f"· Parquet vs .arrow {size_diff(stored, arrow)}",
            transform=ax.transAxes, fontsize=9, color=INK, ha="left", va="top")
    style(ax)
    header(fig, row)
    save(fig, path)


def time_bars(ax, times, colors, names, mib: float, per_mib: set[int]) -> None:
    """Bars of `times` labelled in ms; the bars at the indexes in `per_mib`
    also show ms per MiB of the `mib` MiB video."""
    bars = ax.bar(range(len(times)), times, width=0.55, color=colors, zorder=3)
    bar_labels(ax, bars, [ms(t) + (f"\n{t / mib:.3g} ms/MiB" if i in per_mib else "")
                          for i, t in enumerate(times)])
    ax.set_xticks(range(len(times)), names)
    ax.set_ylim(0, max(times) * 1.25)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_ylabel("ms", fontsize=9, color=MUTED)


def server_time_chart(row: dict, path: Path) -> None:
    """Upload time of one video, with the server's Parquet encode and write
    times. Encode and write happen inside the server receive, which happens
    inside the upload."""
    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=120, facecolor="white")
    times = (row["upload_ms"], row["server_receive_ms"], row["server_encode_ms"], row["server_store_ms"])
    time_bars(ax, times, SERVER_COLORS, ["Upload", "Server receive", "Parquet encode", "Parquet write"],
              row["file_bytes"] / MIB, per_mib={2, 3})
    ax.set_title(f"Server time for {size(row['file_bytes'])} of video", loc="left", fontsize=10,
                 color=INK, fontweight="bold")
    # `rest` is the server receiving, decoding and checking messages.
    rest = times[1] - times[2] - times[3]
    ax.text(0, -0.14, f"Server receive: encode {times[2] / times[1]:.0%} · write {times[3] / times[1]:.0%} "
            f"· other {ms(rest)}",
            transform=ax.transAxes, fontsize=9, color=INK, ha="left", va="top")
    style(ax)
    header(fig, row)
    save(fig, path)


def client_time_chart(row: dict, path: Path) -> None:
    """Client steps before the upload of one video."""
    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=120, facecolor="white")
    times = (row["read_ms"], row["chunk_ms"], row["encode_ms"])
    time_bars(ax, times, CLIENT_COLORS, ["Read file", "Split into batches", "Flight encode"],
              row["file_bytes"] / MIB, per_mib={0, 1, 2})
    ax.set_title(f"Client time before the upload of {size(row['file_bytes'])} of video", loc="left",
                 fontsize=10, color=INK, fontweight="bold")
    ax.text(0, -0.14, f"Before the upload: {ms(sum(times))} · upload itself: {ms(row['upload_ms'])}",
            transform=ax.transAxes, fontsize=9, color=INK, ha="left", va="top")
    style(ax)
    header(fig, row)
    save(fig, path)


def overview(rows, panels, title: str, subtitle: str, path: Path) -> None:
    """One panel per (CSV column, axis label) in `panels`, with a dot per
    video: one column per resolution, split by codec, with the median per
    codec."""
    resolutions = sorted({(r["resolution_rank"], r["resolution"]) for r in rows})
    fig, axes = plt.subplots(len(panels), 1, figsize=(12, 1 + 4 * len(panels)), dpi=150, facecolor="white",
                             sharex=True)
    rng = random.Random(0)
    for ax, (key, label) in zip(axes, panels):
        for i, (_, resolution) in enumerate(resolutions):
            if i % 2 == 0:
                ax.axvspan(i - 0.5, i + 0.5, color=BAND, zorder=0, linewidth=0)
            in_res = [r for r in rows if r["resolution"] == resolution]
            codecs = [c for c in CODEC_COLORS if any(r["codec"] == c for r in in_res)]
            width = 0.84 / len(codecs)
            for j, codec in enumerate(codecs):
                vs = [r for r in in_res if r["codec"] == codec]
                center = i - 0.42 + width * (j + 0.5)
                if len(vs) == 1:
                    # A lone video is its own median: draw it bigger instead of under a line.
                    ax.scatter([center], [vs[0][key]], s=48, color=CODEC_COLORS[codec],
                               edgecolors="white", linewidths=0.8, zorder=4)
                    continue
                xs = [center + rng.uniform(-width * 0.32, width * 0.32) for _ in vs]
                ax.scatter(xs, [r[key] for r in vs], s=16, alpha=0.7, color=CODEC_COLORS[codec],
                           edgecolors="white", linewidths=0.4, zorder=3)
                median = statistics.median(r[key] for r in vs)
                ax.plot([center - width * 0.4, center + width * 0.4], [median, median], color=INK,
                        linewidth=1.6, solid_capstyle="round", zorder=4)
            # Median cost per MiB of video in this resolution.
            per_mib = statistics.median(r[key] / (r["file_bytes"] / MIB) for r in in_res)
            ax.text(i, 0.97, f"{per_mib:.3g} ms/MiB", transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=8, color=MUTED)
        ax.set_yscale("log")
        # Headroom above the highest dot for the ms/MiB labels.
        values = [r[key] for r in rows]
        ax.set_ylim(min(values) / 1.5, max(values) * 3)
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.yaxis.set_minor_locator(LogLocator(base=10, subs=()))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_ylabel(label, fontsize=9, color=MUTED)
        ax.set_xlim(-0.5, len(resolutions) - 0.5)
        style(ax)
        ax.tick_params(which="both", length=0)

    bottom = axes[-1]
    bottom.set_xticks(range(len(resolutions)), [name for _, name in resolutions])
    bottom.tick_params(axis="x", labelsize=10, colors=INK)
    for i, (_, resolution) in enumerate(resolutions):
        median_mib = statistics.median(r["file_bytes"] for r in rows if r["resolution"] == resolution) / MIB
        bottom.text(i, -0.09, f"~{median_mib:,.0f} MiB" if median_mib >= 10 else f"~{median_mib:,.1f} MiB",
                    transform=bottom.get_xaxis_transform(), ha="center", va="top", fontsize=8, color=MUTED)

    top = axes[0]
    top.set_title(title, loc="left", fontsize=13, color=INK, pad=48, fontweight="bold")
    top.text(0, 1.075, f"One dot per video. {subtitle} Top: median ms per MiB of video.",
             transform=top.transAxes, fontsize=9, color=MUTED, va="bottom")
    handles = [plt.Line2D([], [], marker="o", linestyle="", markersize=7, color=CODEC_COLORS[c], label=c)
               for c in CODEC_COLORS if any(r["codec"] == c for r in rows)]
    handles.append(plt.Line2D([], [], color=INK, linewidth=1.6, label="Median per codec"))
    top.legend(handles=handles, frameon=False, fontsize=9, labelcolor=MUTED, loc="lower left",
               bbox_to_anchor=(0, 1.0), borderaxespad=0.2, ncols=len(handles), handletextpad=0.3,
               columnspacing=1.2)
    fig.tight_layout()
    write_png(fig, path, palette=False)


# What to improve first: the step with the largest total time, with an idea
# for each step. Keys are the step names used in the charts.
CLIENT_HINTS = {
    "Read file": "The client loads the whole video before splitting it. Reading and uploading in a pipeline "
                 "(sending each batch as soon as it is read) would hide most of this time behind the upload.",
    "Split into batches": "Batches are zero-copy slices of the file buffer, so there is little left to gain.",
    "Flight encode": "Each batch is serialized into Arrow IPC Flight messages before the upload starts; "
                     "encoding while uploading would overlap the two.",
}
SERVER_HINTS = {
    "Parquet encode": "Each batch is encoded before the next message is read, as in mosaicod's `do_put`. "
                      "Encoding a batch while the next one arrives would overlap the two.",
    "Parquet write": "Each Parquet file is written before the next message is read. Writing in the background "
                     "while the next batch is encoded would overlap the two.",
    "Receiving and checking messages": "Server receive time outside encode and write: waiting for the Flight "
                                       "messages (over loopback here), decoding them, the chunk order and CRC32 "
                                       "checks and creating the topic folder. Bigger record batches mean fewer "
                                       "messages; worth measuring.",
}


def improve_first(steps: dict[str, float], whole: str, hints: dict[str, str]) -> list[str]:
    """README lines naming the step with the largest total time (and the
    runner-up when it is within 10%), with the share of `whole` each takes."""
    whole_ms = sum(steps.values())
    ranked = sorted(steps.items(), key=lambda kv: kv[1], reverse=True)
    lines = ["## What to improve first\n"]
    leaders = [ranked[0]] + [kv for kv in ranked[1:2] if kv[1] >= 0.9 * ranked[0][1]]
    for name, ms_total in leaders:
        lines.append(f"- **{name}: {duration(ms_total)}, {share(ms_total, whole_ms)} of {whole}.** {hints[name]}")
    rest = ", ".join(f"{name} {duration(ms_total)} ({share(ms_total, whole_ms)})"
                     for name, ms_total in ranked[len(leaders):])
    lines.append(f"- Then: {rest}.\n")
    return lines


def write_charts(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r["source"], r["resolution_rank"], r["codec"], r["file"]))
    shutil.rmtree(OUT, ignore_errors=True)
    overview(rows, [("upload_ms", "Upload (ms, log scale)"),
                    ("server_encode_ms", "Parquet encode (ms, log scale)"),
                    ("server_store_ms", "Parquet write (ms, log scale)")],
             "Upload and server time per video",
             "Upload: DoPut start to server ack; encode: batches to Parquet in memory; write: Parquet files "
             "to disk.", SERVER_DIR / ALL_VIDEOS)
    overview(rows, [("read_ms", "Read file (ms, log scale)"),
                    ("chunk_ms", "Split into batches (ms, log scale)"),
                    ("encode_ms", "Flight encode (ms, log scale)")],
             "Client time before the upload per video",
             "Read: load the file; split: zero-copy record batches; encode: batches to Flight messages.",
             CLIENT_DIR / ALL_VIDEOS)

    def total(key: str) -> float:
        return sum(r[key] for r in rows)

    video_total, arrow_total, stored_total = total("file_bytes"), total("arrow_bytes"), total("stored_bytes")
    group_headers = {
        SIZE_DIR: ["| Video | Size | `.arrow` vs video | Parquet vs video | Parquet vs `.arrow` | Parquet files "
                   "| Chart |",
                   "|---|---:|---:|---:|---:|---:|---|"],
        SERVER_DIR: ["| Video | Size | Upload ms | Server receive ms | Encode ms | Write ms "
                     "| Encode ms/MiB | Write ms/MiB | Chart |",
                     "|---|---:|---:|---:|---:|---:|---:|---:|---|"],
        CLIENT_DIR: ["| Video | Size | Read ms | Split ms | Flight encode ms | Total before upload ms "
                     "| Upload ms | Chart |",
                     "|---|---:|---:|---:|---:|---:|---:|---|"],
    }
    indexes = {
        SIZE_DIR: ["# File size comparison per video\n",
                   "## What to improve first\n",
                   f"- **Parquet vs video: {size_diff(stored_total, video_total)} "
                   f"({(stored_total / video_total - 1) * 100:+.3f}%), about "
                   f"{size((stored_total - video_total) / total('stored_files'))} per Parquet file over "
                   f"{total('stored_files'):,} files.** The overhead is per file (footer, schema, bloom filter and "
                   "the `timestamp_ns`, `chunk_index` and `offset` columns), so fewer, bigger files shrink it: raise "
                   "`--chunks-per-batch`, up to the 50 MiB message limit. It would also mean fewer files and "
                   "statistics rows for mosaicod.",
                   f"- For reference, `.arrow`, one file per video, is {size_diff(arrow_total, video_total)} "
                   f"({(arrow_total / video_total - 1) * 100:+.3f}%). Both are well under 1% of the video, so size "
                   "matters less than time: see [`../server-time/`](../server-time/README.md) and "
                   "[`../client-time/`](../client-time/README.md).\n",
                   f"Generated by `scripts/analyze.py`: one chart per video ({len(rows)} in total) comparing "
                   "the video with an Arrow IPC file (`.arrow`) holding the same batches, which is how poc1 stored "
                   "videos before following mosaicod, and with the Parquet files the server stores now.\n",
                   f"In total: video {size(video_total)}, `.arrow` {size_diff(arrow_total, video_total)} "
                   f"({(arrow_total / video_total - 1) * 100:+.3f}%), Parquet {size_diff(stored_total, video_total)} "
                   f"({(stored_total / video_total - 1) * 100:+.3f}%) in {total('stored_files'):,} files.\n"],
        SERVER_DIR: ["# Server time per video\n",
                     *improve_first({"Parquet encode": total("server_encode_ms"),
                                     "Parquet write": total("server_store_ms"),
                                     "Receiving and checking messages": total("server_receive_ms")
                                     - total("server_encode_ms") - total("server_store_ms")},
                                    "the server receive time", SERVER_HINTS),
                     f"The server receive time is {total('server_receive_ms') / total('upload_ms'):.0%} of the "
                     "upload time, so speeding up these steps speeds up the upload.\n",
                     f"Generated by `scripts/analyze.py`: one chart per video ({len(rows)} in total) with its "
                     "upload time, the server receive time and, inside it, the time the server spends encoding the "
                     "Parquet files and writing them to disk.\n",
                     "## All videos\n",
                     f"![Upload and server time per video]({ALL_VIDEOS})\n",
                     f"In total the server spent {total('server_encode_ms') / 1000:.2f} s encoding Parquet files and "
                     f"{total('server_store_ms') / 1000:.2f} s writing them, for {size(video_total)} of video.\n"],
        CLIENT_DIR: ["# Client time per video\n",
                     *improve_first({"Read file": total("read_ms"), "Split into batches": total("chunk_ms"),
                                     "Flight encode": total("encode_ms")},
                                    "the client time before the upload", CLIENT_HINTS),
                     f"The client spends {(total('read_ms') + total('chunk_ms') + total('encode_ms')) / 1000:.2f} s "
                     f"before the uploads, against {total('upload_ms') / 1000:.2f} s of upload.\n",
                     f"Generated by `scripts/analyze.py`: one chart per video ({len(rows)} in total) with the "
                     "client steps before the upload: loading the file, splitting it into record batches (zero-copy "
                     "slices) and serializing them into Flight messages.\n",
                     "## All videos\n",
                     f"![Client time before the upload per video]({ALL_VIDEOS})\n",
                     f"In total the client spent {total('read_ms') / 1000:.2f} s reading files, "
                     f"{total('chunk_ms'):.2f} ms splitting them and {total('encode_ms') / 1000:.2f} s encoding "
                     f"Flight messages, for {size(video_total)} of video.\n"],
    }
    section = None
    for i, row in enumerate(rows, 1):
        rel = Path(slug(row["source"])) / row["resolution"] / slug(row["codec"]) / f"{Path(row['file']).stem}.png"
        size_chart(row, SIZE_DIR / rel)
        server_time_chart(row, SERVER_DIR / rel)
        client_time_chart(row, CLIENT_DIR / rel)
        group = f"{row['source']} · {row['resolution']} · {row['codec']}"
        if group != section:
            section = group
            for folder, lines in indexes.items():
                lines += [f"\n## {group}\n", *group_headers[folder]]
        video, arrow, stored = row["file_bytes"], row["arrow_bytes"], row["stored_bytes"]
        mib = video / MIB
        chart = f"[chart]({rel.as_posix()})"
        indexes[SIZE_DIR].append(
            f"| `{row['file']}` | {size(video)} "
            f"| {size_diff(arrow, video)} ({(arrow / video - 1) * 100:+.3f}%) "
            f"| {size_diff(stored, video)} ({(stored / video - 1) * 100:+.3f}%) "
            f"| {size_diff(stored, arrow)} | {row['stored_files']} | {chart} |")
        indexes[SERVER_DIR].append(
            f"| `{row['file']}` | {size(video)} | {row['upload_ms']:.1f} | {row['server_receive_ms']:.1f} "
            f"| {row['server_encode_ms']:.1f} | {row['server_store_ms']:.1f} "
            f"| {row['server_encode_ms'] / mib:.2f} | {row['server_store_ms'] / mib:.2f} | {chart} |")
        indexes[CLIENT_DIR].append(
            f"| `{row['file']}` | {size(video)} | {row['read_ms']:.2f} | {row['chunk_ms']:.4f} "
            f"| {row['encode_ms']:.2f} | {row['read_ms'] + row['chunk_ms'] + row['encode_ms']:.2f} "
            f"| {row['upload_ms']:.1f} | {chart} |")
        if i % 100 == 0:
            print(f"{i}/{len(rows)}")

    for folder, lines in indexes.items():
        (folder / "README.md").write_text("\n".join(lines) + "\n")
    (OUT / "README.md").write_text("\n".join([
        "# Per-video charts\n",
        f"Generated by `scripts/analyze.py`, three charts for each of the {len(rows)} videos:\n",
        f"- [`{SIZE_DIR.name}/`]({SIZE_DIR.name}/README.md): file size of the video, of an Arrow IPC file "
        "(`.arrow`) of the same batches and of the Parquet files the server stored.",
        f"- [`{SERVER_DIR.name}/`]({SERVER_DIR.name}/README.md): upload time, server receive time and the "
        f"server's Parquet encode and write time, with [`{ALL_VIDEOS}`]({SERVER_DIR.name}/{ALL_VIDEOS}) for all "
        "videos.",
        f"- [`{CLIENT_DIR.name}/`]({CLIENT_DIR.name}/README.md): client steps before the upload (read the file, "
        f"split it into record batches, encode Flight messages), with [`{ALL_VIDEOS}`]({CLIENT_DIR.name}/{ALL_VIDEOS}) "
        "for all videos.",
    ]) + "\n")
    print(f"{len(rows)} videos -> {SIZE_DIR.name}/, {SERVER_DIR.name}/ and {CLIENT_DIR.name}/ in {OUT}")


# ---------------------------------------------------------------------------
# RESULTS.md
# ---------------------------------------------------------------------------

def machine() -> str:
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        cpu = platform.processor() or platform.machine()
    return f"{cpu}, {platform.system()} {platform.release()}"


def link(path: Path) -> str:
    """Path of a results file relative to RESULTS.md, for links."""
    return os.path.relpath(path, RESULTS_MD.parent)


def write_results_md(config: dict, rows: list[dict]) -> None:
    rows = [{**r, "extra_pct": extra_pct(r["stored_bytes"], r["file_bytes"]),
             "arrow_extra_pct": extra_pct(r["arrow_bytes"], r["file_bytes"])} for r in rows]
    video_bytes = sum(r["file_bytes"] for r in rows)
    arrow_bytes = sum(r["arrow_bytes"] for r in rows)
    stored_bytes = sum(r["stored_bytes"] for r in rows)
    stored_files = sum(r["stored_files"] for r in rows)
    formats = sorted({r["format"] for r in rows})
    complete = sum(r["complete"] for r in rows)
    slowest = sorted(rows, key=lambda r: r["upload_ms"], reverse=True)

    groups = defaultdict(list)
    for r in rows:
        groups[(r["source"], r["resolution_rank"], r["resolution"], r["codec"])].append(r)

    out = []
    w = out.append
    w("# poc1 results\n")
    w(f"Upload of every video under `datasets/` with the poc1 Arrow Flight client and server, "
      f"one `DoPut` per video, stored as mosaicod stores a topic: one Parquet file per record batch. "
      f"Generated by `poc1/scripts/analyze.py` on {date.today().isoformat()}.\n")
    w("## Setup\n")
    w(f"- Client and server on the same machine ({machine()}), over loopback.")
    w(f"- Chunk size {size(config['chunk_size'])}, {config['chunks_per_batch']} chunk(s) per record batch, "
      f"so {size(config['chunk_size'] * config['chunks_per_batch'])} of video per Parquet file.")
    w(f"- Parquet format: {', '.join(f'`{f}`' for f in formats)} (see `poc1/src/format.rs`).")
    w("- Datasets: Big Buck Bunny 360p and 1080p (`datasets/360p`, `datasets/1080p`) and the SEPE 8K "
      "set in `datasets/LargeFiles` (40 sequences, 480p to 8K, AV1, H.264 and H.265). "
      "`LargeFiles/4K-Raw/raw.yuv` is not included: the client only uploads container files.")
    w("- Reproduce with `poc1/scripts/run-all.sh` and `uv run poc1/scripts/analyze.py`.\n")

    w("## Summary\n")
    w("| Measure | Value |\n|---|---:|")
    w(f"| Videos received completely (every chunk and byte, matching CRC32) | {complete} of {len(rows)} |")
    w(f"| Video data | {size(video_bytes)} |")
    w(f"| Stored Parquet files | {stored_files:,} files, {size(stored_bytes)} |")
    w(f"| Parquet bigger than video by | {size_diff(stored_bytes, video_bytes)} "
      f"({extra_pct(stored_bytes, video_bytes):+.3f}%) |")
    w(f"| Same batches as Arrow IPC files (`.arrow`) | {size(arrow_bytes)}, "
      f"{size_diff(arrow_bytes, video_bytes)} ({extra_pct(arrow_bytes, video_bytes):+.3f}%) vs video |")
    w(f"| Parquet vs `.arrow` | {size_diff(stored_bytes, arrow_bytes)} "
      f"({extra_pct(stored_bytes, arrow_bytes):+.3f}%) |")
    w(f"| Total upload time | {sum(r['upload_ms'] for r in rows) / 1000:.2f} s |")
    w(f"| Slowest upload | {slowest[0]['upload_ms']:.1f} ms (`{slowest[0]['file']}`) |\n")

    chunks = sum(r["chunks_received"] for r in rows)
    w("Findings:\n")
    w(f"- **The server received all the data** for {complete} of {len(rows)} videos.")
    w(f"- **The Parquet files are a little bigger than the video**, by about "
      f"{size((stored_bytes - video_bytes) / stored_files)} per file (Parquet footer, embedded "
      "Arrow schema, `timestamp_ns` bloom filter and the `timestamp_ns`, `chunk_index` and `offset` "
      "columns). Every file holds the same amount of video, so the share is about the same for every "
      f"video ({min(r['extra_pct'] for r in rows):+.3f}% to {max(r['extra_pct'] for r in rows):+.3f}%); "
      "bigger record batches would lower it.")
    more_or_less = "more" if stored_bytes >= arrow_bytes else "less"
    w(f"- **Parquet takes {size(abs(stored_bytes - arrow_bytes))} {more_or_less} than `.arrow`** "
      f"({extra_pct(stored_bytes, arrow_bytes):+.3f}%). An Arrow IPC file of the same batches adds "
      f"about {size((arrow_bytes - video_bytes) / chunks)} per chunk (message framing and the "
      "`timestamp_ns`, `chunk_index` and `offset` columns) and has one footer per video, while every "
      "Parquet file carries its own footer, schema and bloom filter. `.arrow` is computed by the client "
      "from the batches it sends; it is how poc1 stored videos before following mosaicod.")
    ms_per_mib = [statistics.median(v["upload_ms"] / (v["file_bytes"] / MIB) for v in vs) for vs in groups.values()]
    w(f"- **Upload time grows with file size**: the median of each group below is "
      f"{min(ms_per_mib):.2f}–{max(ms_per_mib):.2f} ms per MiB on loopback.")
    server_share = sum(r["server_receive_ms"] for r in slowest[:10]) / sum(r["upload_ms"] for r in slowest[:10])
    w(f"- In the ten slowest uploads, {server_share:.0%} of the upload time is spent between the server's "
      "first message and the last Parquet file being written, so the time goes to the server side "
      "(receiving, encoding and writing), not to the client.")
    w(f"- Encoding the Parquet files took {sum(r['server_encode_ms'] for r in rows) / 1000:.2f} s and writing "
      f"them {sum(r['server_store_ms'] for r in rows) / 1000:.2f} s in total. "
      f"[`per-video/server-time/all-videos.png`]({link(SERVER_DIR / ALL_VIDEOS)}) "
      f"plots both for every video, and [`per-video/server-time/`]({link(SERVER_DIR / 'README.md')}) has one "
      "chart per video splitting its upload time into them.\n")

    w("## `.arrow` vs Parquet\n")
    w("Total size per resolution of the videos, of an Arrow IPC file (`.arrow`) holding the same batches "
      "(how poc1 stored videos before following mosaicod, computed by the client) and of the Parquet "
      "files the server stored.\n")
    w("| Dataset | Resolution | Videos | Video | `.arrow` | `.arrow` vs video | Parquet | Parquet vs video "
      "| Parquet vs `.arrow` | Parquet files |")
    w("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    by_resolution = defaultdict(list)
    for r in rows:
        by_resolution[(r["resolution_rank"], r["source"], r["resolution"])].append(r)
    for (_, source, resolution), vs in sorted(by_resolution.items()) + [((None, "**Total**", ""), rows)]:
        video, arrow, stored = (sum(v[k] for v in vs) for k in ("file_bytes", "arrow_bytes", "stored_bytes"))
        w(f"| {source} | {resolution} | {len(vs)} | {size(video)} | {size(arrow)} "
          f"| {size_diff(arrow, video)} ({extra_pct(arrow, video):+.3f}%) | {size(stored)} "
          f"| {size_diff(stored, video)} ({extra_pct(stored, video):+.3f}%) "
          f"| {size_diff(stored, arrow)} ({extra_pct(stored, arrow):+.3f}%) "
          f"| {sum(v['stored_files'] for v in vs):,} |")
    w("")

    w("## Upload and server time\n")
    w(f"![Upload, Parquet encode and write time per video]({link(SERVER_DIR / ALL_VIDEOS)})\n")
    w("Videos are grouped by resolution; the figure under each resolution is its median video size. "
      "Big Buck Bunny (360p, 1080p) has one video per codec, SEPE 8K has 40. There are also three charts "
      "per video: "
      f"[`per-video/file-size-comparison/`]({link(SIZE_DIR / 'README.md')}) "
      "compares the video, `.arrow` and Parquet sizes, "
      f"[`per-video/server-time/`]({link(SERVER_DIR / 'README.md')}) shows the upload "
      "time with the server's Parquet encode and write times, and "
      f"[`per-video/client-time/`]({link(CLIENT_DIR / 'README.md')}) shows the client "
      "steps before the upload (read, split into batches, Flight encode).\n")

    w("## By resolution and codec\n")
    w("Medians across the videos in each group. Upload is measured by the client, from the start "
      "of `DoPut` to the server's acknowledgement; server time is from the first message the "
      "server receives until the last Parquet file is written.\n")
    w("| Dataset | Resolution | Codec | Videos | Complete | Video | `.arrow` extra | Parquet extra | Parquet files "
      "| Upload ms | Slowest upload ms | Server ms |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for (source, _, resolution, codec), vs in sorted(groups.items(), key=lambda kv: kv[0][1:] + (kv[0][0],)):
        def med(key: str) -> float:
            return statistics.median(v[key] for v in vs)
        w(f"| {source} | {resolution} | {codec} | {len(vs)} | {sum(v['complete'] for v in vs)}/{len(vs)} "
          f"| {size(med('file_bytes'))} "
          f"| {size_diff(med('arrow_bytes'), med('file_bytes'))} ({med('arrow_extra_pct'):+.3f}%) "
          f"| {size_diff(med('stored_bytes'), med('file_bytes'))} ({med('extra_pct'):+.3f}%) "
          f"| {med('stored_files'):g} "
          f"| {med('upload_ms'):.2f} | {max(v['upload_ms'] for v in vs):.2f} | {med('server_receive_ms'):.2f} |")
    w("")

    w("## Slowest uploads\n")
    w("| File | Resolution | Codec | Video | Upload ms | Server ms |\n|---|---|---|---:|---:|---:|")
    for r in slowest[:10]:
        w(f"| `{r['file']}` | {r['resolution']} | {r['codec']} | {size(r['file_bytes'])} "
          f"| {r['upload_ms']:.2f} | {r['server_receive_ms']:.2f} |")
    w("")

    incomplete = [r for r in rows if not r["complete"]]
    if incomplete:
        w("## Incomplete uploads\n")
        w("| File | Chunks received | Data received | Video |\n|---|---:|---:|---:|")
        for r in incomplete:
            w(f"| `{r['file']}` | {r['chunks_received']}/{r['chunks_sent']} "
              f"| {size(r['bytes_received'])} | {size(r['file_bytes'])} |")
        w("")

    w(f"The per-video data is in [`videos.csv`]({link(RESULTS / 'videos.csv')}), one row per video, and "
      f"the raw JSON client reports for each dataset folder (run settings, wire bytes, Flight messages, "
      f"problems) are in [`reports/`]({link(RESULTS / 'reports')}/).")

    RESULTS_MD.write_text("\n".join(out) + "\n")
    print(f"wrote {RESULTS_MD}")


config, rows = load_reports()
write_csv(rows)
write_charts(rows)
write_results_md(config, rows)
