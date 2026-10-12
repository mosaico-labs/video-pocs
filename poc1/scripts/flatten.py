#!/usr/bin/env python3
"""Flattens the client reports written by run-all.sh into one CSV, one row per video.

Usage: flatten.py [results dir]   (default: poc1/results)
Writes <results dir>/videos.csv.
"""
import csv
import json
import re
import sys
from pathlib import Path

RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "results"
CODECS = {"av1": "AV1", "h264": "H.264", "h265": "H.265", "hevc": "H.265", "vp9": "VP9"}
# Order of the resolutions from smallest to largest.
RESOLUTIONS = ["360p", "480p", "720p", "1080p", "1K", "2K", "4K", "8K"]


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


rows = []
for report in sorted((RESULTS / "reports").glob("*.json")):
    data = json.loads(report.read_text())
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
            "upload_ms": round(v["upload_ms"], 3),
            "server_receive_ms": round(v["server_receive_ms"], 3),
            "read_ms": round(v["read_ms"], 3),
            "chunk_ms": round(v["chunk_ms"], 4),
            "encode_ms": round(v["encode_ms"], 3),
        })

out = RESULTS / "videos.csv"
with out.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f"{len(rows)} videos -> {out}")
