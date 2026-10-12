#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.9"]
# ///
"""Writes poc1/results/RESULTS.md and its upload time chart (poc1/results/upload-time.png)
from the CSV produced by flatten.py.

Usage: uv run results_md.py [results dir] [output file]
       (defaults: poc1/results and poc1/results/RESULTS.md)
"""
import csv
import json
import os
import platform
import random
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

POC_DIR = Path(__file__).resolve().parent.parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else POC_DIR / "results"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else RESULTS / "RESULTS.md"
UPLOAD_CHART = RESULTS / "upload-time.png"
MIB = 1024 * 1024
GIB = 1024 * MIB


def machine() -> str:
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        cpu = platform.processor() or platform.machine()
    return f"{cpu}, {platform.system()} {platform.release()}"


def link(path: Path) -> str:
    """Path of a results file relative to the Markdown file, for links."""
    return os.path.relpath(path, OUT.parent)


def extra_pct(arrow: float, video: float) -> float:
    return (arrow / video - 1) * 100


# One fixed color per codec, so a codec keeps its color in every chart.
CODEC_COLORS = {"AV1": "#2a78d6", "H.264": "#eb6834", "H.265": "#1baf7a", "VP9": "#eda100"}
INK, MUTED, GRID, AXIS, BAND = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#f4f3ef"


def plot_upload_time(rows, path: Path) -> None:
    """Upload time per video: one column per resolution, split by codec, with
    the dots jittered sideways so they don't sit on top of each other."""
    plt.rcParams["font.family"] = "sans-serif"
    resolutions = sorted({(r["resolution_rank"], r["resolution"]) for r in rows})
    fig, ax = plt.subplots(figsize=(12, 5.6), dpi=150, facecolor="white")
    rng = random.Random(0)
    dot_x = {}

    for i, (_, resolution) in enumerate(resolutions):
        if i % 2 == 0:
            ax.axvspan(i - 0.5, i + 0.5, color=BAND, zorder=0, linewidth=0)
        in_res = [r for r in rows if r["resolution"] == resolution]
        codecs = [c for c in CODEC_COLORS if any(r["codec"] == c for r in in_res)]
        width = 0.84 / len(codecs)
        for j, codec in enumerate(codecs):
            vs = [r for r in in_res if r["codec"] == codec]
            center = i - 0.42 + width * (j + 0.5)
            spread = width * 0.32 if len(vs) > 1 else 0
            xs = [center + rng.uniform(-spread, spread) for _ in vs]
            dot_x.update((r["file"], x) for r, x in zip(vs, xs))
            if len(vs) == 1:
                # A lone video is its own median: draw it bigger instead of under a line.
                ax.scatter(xs, [vs[0]["upload_ms"]], s=48, color=CODEC_COLORS[codec],
                           edgecolors="white", linewidths=0.8, zorder=4)
                continue
            ax.scatter(xs, [r["upload_ms"] for r in vs], s=16, alpha=0.7, color=CODEC_COLORS[codec],
                       edgecolors="white", linewidths=0.4, zorder=3)
            median = statistics.median(r["upload_ms"] for r in vs)
            ax.plot([center - width * 0.4, center + width * 0.4], [median, median], color=INK,
                    linewidth=1.6, solid_capstyle="round", zorder=4)
        size = statistics.median(r["file_bytes"] for r in in_res) / MIB
        ax.text(i, -0.07, f"~{size:,.0f} MiB" if size >= 10 else f"~{size:,.1f} MiB",
                transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8, color=MUTED)

    # Name the three slowest uploads, with labels stacked to the left of the
    # rightmost column so they don't overlap each other or the dots.
    label_y = None
    for r in sorted(rows, key=lambda r: r["upload_ms"], reverse=True)[:3]:
        label_y = r["upload_ms"] if label_y is None else min(r["upload_ms"], label_y / 1.22)
        ax.annotate(f"{r['file']}  {r['upload_ms']:.0f} ms", (dot_x[r["file"]], r["upload_ms"]),
                    xytext=(len(resolutions) - 1.6, label_y), textcoords="data", ha="right", va="center",
                    fontsize=8, color=INK,
                    arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.7, "shrinkB": 3})

    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
    ax.yaxis.set_minor_locator(LogLocator(base=10, subs=()))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_ylabel("Upload (ms, log scale)", fontsize=9, color=MUTED)
    ax.set_xticks(range(len(resolutions)), [name for _, name in resolutions])
    ax.set_xlim(-0.5, len(resolutions) - 0.5)

    ax.set_title("Upload time per video", loc="left", fontsize=13, color=INK, pad=24, fontweight="bold")
    ax.text(0, 1.02, "From the start of DoPut to the server's acknowledgement. One dot per video.", transform=ax.transAxes, fontsize=9, color=MUTED,
            va="bottom")
    handles = [plt.Line2D([], [], marker="o", linestyle="", markersize=7, color=CODEC_COLORS[c], label=c)
               for c in CODEC_COLORS if any(r["codec"] == c for r in rows)]
    handles.append(plt.Line2D([], [], color=INK, linewidth=1.6, label="Median per codec"))
    ax.legend(handles=handles, frameon=False, fontsize=9, labelcolor=MUTED, loc="lower right",
              bbox_to_anchor=(1, 1.0), borderaxespad=0.2, ncols=len(handles), handletextpad=0.3,
              columnspacing=1.2)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0, which="both")
    ax.tick_params(axis="x", labelsize=10, colors=INK)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


rows = list(csv.DictReader((RESULTS / "videos.csv").open()))
for r in rows:
    for key in ("file_bytes", "arrow_bytes", "chunks_sent", "chunks_received", "resolution_rank"):
        r[key] = int(r[key])
    for key in ("upload_ms", "server_receive_ms"):
        r[key] = float(r[key])
    r["complete"] = r["complete"] == "True"
    r["extra_pct"] = extra_pct(r["arrow_bytes"], r["file_bytes"])

config = json.loads(next((RESULTS / "reports").glob("*.json")).read_text())["config"]
video_bytes = sum(r["file_bytes"] for r in rows)
arrow_bytes = sum(r["arrow_bytes"] for r in rows)
complete = sum(r["complete"] for r in rows)
slowest = sorted(rows, key=lambda r: r["upload_ms"], reverse=True)

groups = defaultdict(list)
for r in rows:
    groups[(r["source"], r["resolution_rank"], r["resolution"], r["codec"])].append(r)

out = []
w = out.append
w("# poc1 results\n")
w(f"Upload of every video under `datasets/` with the poc1 Arrow Flight client and server, "
  f"one `DoPut` per video. Generated by `poc1/scripts/results_md.py` on {date.today().isoformat()}.\n")
w("## Setup\n")
w(f"- Client and server on the same machine ({machine()}), over loopback.")
w(f"- Chunk size {config['chunk_size']:,} bytes, {config['chunks_per_batch']} chunk(s) per record batch.")
w("- Datasets: Big Buck Bunny 360p and 1080p (`datasets/360p`, `datasets/1080p`) and the SEPE 8K "
  "set in `datasets/LargeFiles` (40 sequences, 480p to 8K, AV1, H.264 and H.265). "
  "`LargeFiles/4K-Raw/raw.yuv` is not included: the client only uploads container files.")
w("- Reproduce with `poc1/scripts/run-all.sh`, `poc1/scripts/flatten.py` and "
  "`uv run poc1/scripts/results_md.py`.\n")

w("## Summary\n")
w("| Measure | Value |\n|---|---:|")
w(f"| Videos received completely (every chunk and byte, matching CRC32) | {complete} of {len(rows)} |")
w(f"| Video data | {video_bytes / GIB:.2f} GiB |")
w(f"| Stored `.arrow` files | {arrow_bytes / GIB:.2f} GiB |")
w(f"| `.arrow` bigger than video by | +{extra_pct(arrow_bytes, video_bytes):.3f}% "
  f"({(arrow_bytes - video_bytes) / MIB:.1f} MiB) |")
w(f"| Total upload time | {sum(r['upload_ms'] for r in rows) / 1000:.2f} s |")
w(f"| Slowest upload | {slowest[0]['upload_ms']:.1f} ms (`{slowest[0]['file']}`) |\n")

chunks = sum(r["chunks_received"] for r in rows)
w("Findings:\n")
w(f"- **The server received all the data** for {complete} of {len(rows)} videos.")
w(f"- **The `.arrow` file is a little bigger than the video**, by about "
  f"{(arrow_bytes - video_bytes) / chunks:.0f} bytes per chunk (Arrow IPC framing and "
  "the `chunk_index` and `offset` columns). The share therefore falls as files grow: "
  f"from +{max(r['extra_pct'] for r in rows):.3f}% for the smallest files to "
  f"+{min(r['extra_pct'] for r in rows):.3f}% for the largest.")
ms_per_mib = [statistics.median(v["upload_ms"] / (v["file_bytes"] / MIB) for v in vs) for vs in groups.values()]
w(f"- **Upload time grows with file size**: the median of each group below is "
  f"{min(ms_per_mib):.2f}–{max(ms_per_mib):.2f} ms per MiB on loopback.")
server_share = sum(r["server_receive_ms"] for r in slowest[:10]) / sum(r["upload_ms"] for r in slowest[:10])
w(f"- In the ten slowest uploads, {server_share:.0%} of the upload time is spent between the server's "
  "first message and the `.arrow` file being written, so the time goes to the server side "
  "(receiving and writing), not to the client.\n")

w("## Upload time\n")
plot_upload_time(rows, UPLOAD_CHART)
w(f"![Upload time per video]({link(UPLOAD_CHART)})\n")
w("Videos are grouped by resolution; the figure under each resolution is its median video size. "
  "The named dots are the three slowest uploads. Big Buck Bunny (360p, 1080p) has one video per "
  "codec, SEPE 8K has 40. `poc1/scripts/per_video_charts.py` charts the size of each `.arrow` file "
  f"against its video, one chart per video, in [`per-video/`]({link(RESULTS / 'per-video' / 'README.md')}).\n")

w("## By resolution and codec\n")
w("Medians across the videos in each group. Upload is measured by the client, from the start "
  "of `DoPut` to the server's acknowledgement; server time is from the first message the "
  "server receives until the `.arrow` file is written.\n")
w("| Dataset | Resolution | Codec | Videos | Complete | Video MiB | `.arrow` MiB | `.arrow` extra "
  "| Upload ms | Slowest upload ms | Server ms |")
w("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
for (source, _, resolution, codec), vs in sorted(groups.items(), key=lambda kv: kv[0][1:] + (kv[0][0],)):
    med = lambda key: statistics.median(v[key] for v in vs)
    w(f"| {source} | {resolution} | {codec} | {len(vs)} | {sum(v['complete'] for v in vs)}/{len(vs)} "
      f"| {med('file_bytes') / MIB:.2f} | {med('arrow_bytes') / MIB:.2f} | +{med('extra_pct'):.3f}% "
      f"| {med('upload_ms'):.2f} | {max(v['upload_ms'] for v in vs):.2f} | {med('server_receive_ms'):.2f} |")
w("")

w("## Slowest uploads\n")
w("| File | Resolution | Codec | Video MiB | Upload ms | Server ms |\n|---|---|---|---:|---:|---:|")
for r in slowest[:10]:
    w(f"| `{r['file']}` | {r['resolution']} | {r['codec']} | {r['file_bytes'] / MIB:.2f} "
      f"| {r['upload_ms']:.2f} | {r['server_receive_ms']:.2f} |")
w("")

incomplete = [r for r in rows if not r["complete"]]
if incomplete:
    w("## Incomplete uploads\n")
    w("| File | Chunks received | Bytes received | Video bytes |\n|---|---:|---:|---:|")
    for r in incomplete:
        w(f"| `{r['file']}` | {r['chunks_received']}/{r['chunks_sent']} "
          f"| {int(r['bytes_received']):,} | {r['file_bytes']:,} |")
    w("")

w(f"The per-video data is in [`videos.csv`]({link(RESULTS / 'videos.csv')}), one row per video, and "
  f"the client reports for each dataset folder are in [`reports/`]({link(RESULTS / 'reports')}/).")

OUT.write_text("\n".join(out) + "\n")
print(f"wrote {OUT}")
