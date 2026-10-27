#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""Charts the client reading a batch's images back from the server
(scripts/fetch.sh: `client --fetch` with the server's DoGet streams
LZ4-compressed and uncompressed), in the tracked
image-results/<scenario>/<batch>/preset_<P>_crf_<C>/client/ folder, next to
the server's results in server/:

    fetch-lz4_frame.json, fetch-none.json   copies of the client's reports
    README.md            how the images are read back, as mosaicod serves a
                         topic, what was measured, the results and what they
                         show
    receive-time.svg     receiving all the images of each format
    first-image.svg      from the DoGet call to the first image
    wire-size.svg        what each format's stream carried
    end-to-end.svg       receiving, saving and decoding: until the client
                         holds every frame

    uv run scripts/fetch_results.py [--timestamps CSV] [--preset P] [--crf C]

The charts share report.py's and image_formats.py's visual system.
"""

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

import report
from image_formats import FORMATS, MIB, bar_chart, bold, draw_groups, grouped_bars, plt, save, subtitle
from matplotlib.patches import Patch
from report import INK, INK_SECONDARY, MUTED, SURFACE, TOPIC, WORKSPACE

# The two runs: the server's DoGet compression, as the client saw it.
RUNS = {"none": "no compression", "lz4_frame": "LZ4, as mosaicod"}
LZ4_HATCH = "///"
def facts(runs):
    """What the texts say from the data: images at a time when decoding,
    the most images a batch carried, and raw's largest image, MiB."""
    topics = [t for run in runs.values() for t in run["topics"]]
    raw = [t for t in topics if t["format"] == "raw"]
    return (topics[0]["workers"], max(max(t["batch_rows"]) for t in topics),
            max((i["bytes"] for t in raw for i in t["images"]), default=0) / MIB)


# Links the wire bytes would take, for scale: loopback has no limit.
LINKS = [("1 Gbit/s", 1e9), ("10 Gbit/s", 10e9)]


def name(key):
    return FORMATS[key][0]


def paired(runs, value):
    """Bars of every format, "uncompressed" then "LZ4" (hatched), with the
    formats' names under their pairs and the groups (lossless, lossy) under
    those, for bar_chart: (bars, pairs, groups)."""
    bars, exact, pairs = [], [], []
    for topic in runs["none"]["topics"]:
        key = topic["format"]
        lz4 = next(t for t in runs["lz4_frame"]["topics"] if t["format"] == key)
        color = FORMATS[key][1]
        pairs.append((len(bars), len(bars) + 1, name(key)))
        bars += [("uncompressed", value(topic), color), ("LZ4", value(lz4), color, LZ4_HATCH)]
        exact += [topic, topic]
    return bars, pairs, grouped_bars(exact)


def end_to_end(path, runs, n, context):
    """Stacked: receiving, saving and decoding (several at a time) every image,
    uncompressed then LZ4, per format."""
    # One color per part, the same for every format (named under its bars):
    # validated with dataviz's validate_palette.js.
    workers = facts(runs)[0]
    parts = [("receiving", "receive_seconds", "#2a78d6"), ("saving", "save_seconds", "#e3a21a"),
             (f"decoding, {workers} at a time", "decode_parallel_seconds", "#7a5cc7")]
    topics = runs["none"]["topics"]
    figure, axes = plt.subplots(figsize=(1.25 * 2 * len(topics) + 2.4, 4.8))
    axes.set_title("From the DoGet call to every frame decoded", pad=44)
    subtitle(axes, f"{context}\nPer format: receiving all {n} images (blue), saving each as a file (amber), then "
             f"decoding them {workers} at a time (purple). The parts add up; lower is faster.\nEach format uncompressed, then "
             "the same fetch with LZ4 (hatched).", y=1.03)
    x, labels, totals = 0, [], []
    for topic in topics:
        key = topic["format"]
        for run, hatch in (("none", None), ("lz4_frame", LZ4_HATCH)):
            t = next(t for t in runs[run]["topics"] if t["format"] == key)
            bottom = 0.0
            for _, part, color in parts:
                # A thin surface-colored edge parts one segment from the next.
                bar = axes.bar(x, t[part], bottom=bottom, width=0.6, color=color, edgecolor=SURFACE, linewidth=1,
                               hatch=hatch, zorder=2)
                bottom += t[part]
            totals.append((x, bottom))
            labels.append("uncompressed" if hatch is None else "LZ4")
            x += 1
    top = max(t for _, t in totals)
    for x, total in totals:
        axes.annotate(f"{total:.1f} s", (x, total), xytext=(0, 4), textcoords="offset points", ha="center",
                      va="bottom", color=INK, fontweight="bold", fontsize=8.5)
    axes.set_xticks(range(len(labels)), labels)
    axes.tick_params(axis="x", colors=INK, labelcolor=INK, labelsize=8.5, length=0)
    draw_groups(axes, grouped_bars([t for t in topics for _ in range(2)]), len(labels),
                [(2 * i, 2 * i + 1, name(t["format"])) for i, t in enumerate(topics)])
    axes.set_ylim(0, top * 1.12)
    axes.set_ylabel("s")
    axes.grid(axis="x", visible=False)
    axes.legend(handles=[Patch(facecolor=color, label=label) for label, _, color in parts]
                + [Patch(facecolor=SURFACE, edgecolor=MUTED, hatch=LZ4_HATCH, label="hatched: the same fetch with LZ4")],
                loc="upper left", bbox_to_anchor=(0, -0.3), ncol=len(parts) + 1, borderaxespad=0)
    save(figure, path)


def readme(sequence, settings, runs):
    none, lz4 = runs["none"], runs["lz4_frame"]
    n = len(none["topics"][0]["images"])
    by = {run: {t["format"]: t for t in runs[run]["topics"]} for run in runs}
    keys = [t["format"] for t in none["topics"]]
    workers, most_rows, raw_largest = facts(runs)

    def mib(b):
        return f"{b / MIB:,.1f} MiB"

    lines = [
        f"# {sequence}, {settings}: the images read back by a client",
        "",
        "Generated by `scripts/fetch_results.py` from the client's reports (`client --fetch`, copied here as "
        "`fetch-none.json` and `fetch-lz4_frame.json`); do not edit. The images are the server's, from its "
        f"[results](../server/README.md): one topic per format, {n} frames of {none['width']}x{none['height']}.",
        "",
        "```mermaid",
        "sequenceDiagram",
        "    participant P as output/.../images/<format>.parquet",
        "    participant S as server",
        "    participant C as client",
        "    participant F as output/.../fetched/<format>/",
        "",
        "    Note over P: one image topic per format, stored before the fetch:<br/>"
        "{ timestamp_ns (as uploaded), format, data (ZSTD 5) }",
        "    C->>S: GetFlightInfo: GetFlightInfoCmd { locator = sequence }",
        "    S-->>C: FlightInfo: one endpoint per format<br/>ticket TicketTopic, app_metadata TopicAppMetadata",
        "    loop each endpoint (format)",
        "        C->>S: DoGet: TicketTopic { locator = sequence/camera/images/<format> }",
        "        loop each batch read (rows = 25 MiB / average image size)",
        "            P->>S: record batch (ZSTD undone)",
        "            Note over S: over 25 MiB? split into fewer rows,<br/>never inside an image",
        "            S-->>C: FlightData per piece: Arrow IPC, LZ4 or none<br/>(the client accepts up to 512 MiB)",
        "            Note over C: check timestamp_ns = timestamps.csv",
        "        end",
        "        C->>F: save each image",
        f"        Note over C: decode with the formats crate, one and {workers} at a time:<br/>"
        "exact formats = raw's frames",
        "    end",
        "    Note over C: camera.fetch-<compression>.json",
        "```",
        "",
        "## How the images are read back",
        "",
        "As mosaicod serves a topic to its clients:",
        "",
        "1. **Before the client asks, each format's images are already stored on the server.** They are the "
        "video's frames turned back into each format ([the server's results](../server/README.md)), and each "
        "format is kept as one topic, `camera/images/<format>`: one Parquet file with a row per image, in capture "
        "order, holding its capture time (`timestamp_ns`, as the client uploaded it), its `format` and its bytes "
        "(`data`, ZSTD-compressed), as mosaicod stores an image topic. This is done once, ahead of the fetch, "
        "and is not in the times below.",
        "2. **`GetFlightInfo`: the client asks what it can read.** It sends a `GetFlightInfoCmd` with the "
        "sequence's locator, and the server answers with one endpoint per topic, so one per format. Each "
        "endpoint carries:",
        "   - a **ticket** (`TicketTopic`): the topic's locator, which the client hands back in `DoGet` to say "
        "which topic it wants;",
        "   - **metadata** (`TopicAppMetadata`): the format, the frames' size, and the topic's rows and bytes, "
        "which the client can read without downloading anything.",
        "3. **`DoGet`: the client reads one topic per ticket, and the server streams its images back.** The "
        "server does this as mosaicod does, in three steps:",
        "   - **It reads the Parquet file in batches sized to fit about 25 MiB** (mosaicod's *target* message "
        "size). Rows per batch = 25 MiB ÷ the topic's average image size, rounded down, with at least 1. "
        "Images averaging 8 MiB are read 3 at a time; images averaging over 25 MiB give 0, so they are read "
        "1 at a time.",
        "   - **Arrow Flight's encoder splits any batch that still comes out over 25 MiB.** Images vary in size, "
        "so a batch can go over the target. The encoder measures the batch (uncompressed, in memory), needs "
        "n = ⌈size ÷ 25 MiB⌉ pieces, and puts ⌊rows ÷ n⌋ rows in each (at least 1). Two things follow. It "
        "cuts only between rows, never inside an image, so a single image is sent whole however big it is "
        f"(a {raw_largest:.1f} MiB raw image goes out as one {raw_largest:.1f} MiB message). "
        "And because it rounds down, a 3-image batch just over 25 MiB needs 2 pieces, gets ⌊3 ÷ 2⌋ = 1 row in "
        "each, and goes out as 3 one-image messages.",
        "   - **Each piece becomes one `FlightData` message** (Arrow IPC), compressed with LZ4 as mosaicod sends "
        "it, or not (both are measured). The split is decided before compression, so LZ4 does not change the "
        "number of messages. The stream opens with one more message: the schema.",
        "",
        "   **25 MiB is what the server aims for, not a limit; 512 MiB is the limit.** Nothing is ever sent at "
        "512 MiB: it is the largest message mosaicod will send (`GRPC_MSG_MAX_SIZE_BYTES`), and the client "
        "sets its receive limit to match. Since an image is never split, a message is as big as its images, "
        "and 512 MiB is how big a single image can get before reading it back would fail. The upload limit "
        "is different: 50 MiB by default, which raw's images exceed.",
        "",
        "   What this gave for each format, uncompressed (LZ4 gives the same messages):",
        "",
        *batching(by["none"], keys),
        "",
        "   The encoder measures a batch by the memory its buffers hold, which can be more than the image bytes. "
        "That is why batches with less than 25 MiB of images were still split, and why some messages carry "
        "fewer images than were read.",
        "",
        "4. **The client** saves each image as a file and decodes it with the server's own readers (the `formats` "
        f"crate): one at a time, then {workers} at a time. Every image of an exact format must decode to the same "
        "frame as raw's, and every image's `timestamp_ns` must be the one uploaded, or the fetch fails.",
        "",
        "Client and server run on the same machine, over loopback: the network is not a bottleneck, and the two "
        "share the CPU. Each time is one run.",
        "",
        "## What was measured",
        "",
        "- **Receiving**: from the `DoGet` call to the end of the stream, for all the images: the server reading "
        "the Parquet file (and undoing its ZSTD), compressing with LZ4 or not, gRPC over loopback, and the client "
        "decoding the Arrow IPC messages (and undoing the LZ4).",
        "- **First image**: how long the client waits before the stream starts: from its `DoGet` request to the "
        "first image in its hands (the image's bytes and its `timestamp_ns`). In between, the server reads the first "
        "batch from the topic's Parquet file (undoing its ZSTD) and encodes it (with LZ4 or not), the message "
        "crosses loopback, and the client decodes it back into a record batch. Decoding the image into a frame is "
        f"not counted; a batch can carry up to {most_rows} images, which arrive together. Raw's first message is "
        f"a whole {raw_largest:.1f} MiB image, so it starts later than the others.",
        "- **On the wire**: what the `FlightData` messages carried (IPC headers and bodies), against the image "
        "bytes themselves.",
        f"- **Saving** each image as a file, and **decoding** the files to frames, one at a time and {workers} at a time "
        "(the OS keeps the files in its cache).",
        "",
        f"`GetFlightInfo` took {ms(none['get_flight_info_seconds'])} uncompressed and "
        f"{ms(lz4['get_flight_info_seconds'])} with LZ4: negligible.",
        "",
        "## Results",
        "",
        "| Format | Images | On the wire, none | On the wire, LZ4 | Receiving, none | Receiving, LZ4 | First image, "
        "none / LZ4 | Messages | Same frames as raw |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key in keys:
        a, b = by["none"][key], by["lz4_frame"][key]
        check = "yes" if a["matches_raw"] else ("the reference" if key == "raw" else "– (converted)")
        lines.append(f"| {name(key)} | {mib(a['image_bytes'])} | {mib(a['wire_bytes'])} | {mib(b['wire_bytes'])} | "
                     f"{a['receive_seconds']:.2f} s | {b['receive_seconds']:.2f} s | "
                     f"{ms(a['first_image_seconds'])} / {ms(b['first_image_seconds'])} | {a['messages']} | {check} |")
    lines += [
        "",
        "![Receiving all the images](receive-time.svg)",
        "",
        "![The first image](first-image.svg)",
        "",
        "![On the wire](wire-size.svg)",
        "",
        "### Until the client holds every frame",
        "",
        f"| Format | Receiving | Saving | Decoding, one at a time | Decoding, {workers} at a time | Receiving + "
        f"saving + decoding {workers} at a time |",
        "|---|---|---|---|---|---|",
    ]
    for key in keys:
        a = by["none"][key]
        total = a["receive_seconds"] + a["save_seconds"] + a["decode_parallel_seconds"]
        lines.append(f"| {name(key)} | {a['receive_seconds']:.2f} s | {a['save_seconds']:.2f} s | "
                     f"{a['decode_seconds']:.2f} s | {a['decode_parallel_seconds']:.2f} s | {total:.2f} s |")
    lines += [
        "",
        "Uncompressed streams; with LZ4 the chart below shows both.",
        "",
        "![From the DoGet call to every frame decoded](end-to-end.svg)",
        "",
        "### On a real network",
        "",
        "Over loopback the wire costs almost nothing. On a link, the bytes alone would take at least (the stream "
        "with fewer bytes, LZ4 or not):",
        "",
        "| Format | Fewer bytes on the wire | " + " | ".join(f"At {label}" for label, _ in LINKS) + " | Per image at "
        f"{LINKS[0][0]} |",
        "|---|---|" + "---|" * len(LINKS) + "---|",
    ]
    for key in keys:
        wire = min(by["none"][key]["wire_bytes"], by["lz4_frame"][key]["wire_bytes"])
        lines.append(f"| {name(key)} | {mib(wire)} | " + " | ".join(f"{wire * 8 / rate:.1f} s" for _, rate in LINKS)
                     + f" | {ms(wire * 8 / LINKS[0][1] / n)} |")
    lines += ["", "## What it shows", "", *findings(by, keys, n), "", "## Limits", "",
              "- Loopback: no network between client and server, so receiving measures the server's reading and "
              "encoding and the client's decoding, not a link. The table above gives the link's share.",
              "- Client and server share the machine: each slows the other a little.",
              "- One run of each, files in the OS cache (see the server's limits)."]
    return "\n".join(lines) + "\n"


def batching(topics, keys):
    """How DoGet batched each format's images: the rows read per batch (the
    target over the average image, as the server sizes it), the messages they
    were split into, and the largest one's image bytes, as table lines."""
    target = 25 * MIB
    lines = ["   | Format | Average image | Largest image | Rows read per batch | Images per message | Largest "
             "message (image bytes) | Messages (with the schema) |",
             "   |---|---|---|---|---|---|---|"]
    for key in keys:
        t = topics[key]
        sizes = [i["bytes"] for i in t["images"]]
        average = sum(sizes) / len(sizes)
        read = min(max(int(target // average), 1), 8192)
        # Images arrive in order, batch after batch: group them by batch_rows.
        largest, offset = 0, 0
        for rows in t["batch_rows"]:
            largest = max(largest, sum(sizes[offset:offset + rows]))
            offset += rows
        counts = Counter(t["batch_rows"])
        per_message = ", ".join(f"{counts[rows]} × {rows}" for rows in sorted(counts, reverse=True))
        lines.append(f"   | {name(key)} | {average / MIB:.1f} MiB | {max(sizes) / MIB:.1f} MiB | {read} | "
                     f"{per_message} | {largest / MIB:.1f} MiB | {t['messages']} |")
    return lines


def ms(seconds):
    return f"{seconds * 1000:,.0f} ms" if seconds >= 0.01 else f"{seconds * 1000:.1f} ms"


def findings(by, keys, n):
    none, lz4 = by["none"], by["lz4_frame"]
    lines = []
    # LZ4: what it saves and what it costs, per format.
    shrinks = [k for k in keys if lz4[k]["wire_bytes"] < 0.9 * none[k]["wire_bytes"]]
    same = [k for k in keys if k not in shrinks]
    for k in shrinks:
        lines.append(f"- **LZ4 shrinks {name(k)}** from {none[k]['wire_bytes'] / MIB:,.0f} to "
                     f"{lz4[k]['wire_bytes'] / MIB:,.0f} MiB on the wire ({none[k]['wire_bytes'] / lz4[k]['wire_bytes']:.1f}x), "
                     f"and receiving takes {lz4[k]['receive_seconds']:.2f} s against {none[k]['receive_seconds']:.2f} s "
                     "uncompressed: "
                     + ("over loopback, compressing and decompressing cost more than the bytes saved; on a slow link, "
                        "where the bytes dominate, it would pay." if lz4[k]["receive_seconds"] > none[k]["receive_seconds"]
                        else "it pays even over loopback, and more on a slower link, where the bytes dominate."))
    if same:
        slower = [k for k in same if lz4[k]["receive_seconds"] > 1.1 * none[k]["receive_seconds"]]
        lines.append("- **LZ4 doesn't shrink " + ", ".join(name(k) for k in same) + "**: their images are already "
                     "compressed, so Arrow's IPC writer keeps their buffers as they are (it does when LZ4 doesn't "
                     "make one smaller), and the streams carry the same bytes."
                     + (" It still costs time, as the writer compresses every buffer before keeping it as it is: "
                        + "; ".join(f"{name(k)} {none[k]['receive_seconds']:.2f} s uncompressed, "
                                    f"{lz4[k]['receive_seconds']:.2f} s with LZ4" for k in slower) + "."
                        if slower else " They take about the same time to receive."))
    # Receiving against decoding.
    fastest = min((k for k in keys if k != "raw"), key=lambda k: none[k]["receive_seconds"])
    lines.append(f"- **Receiving is fast against decoding** for every compressed format: "
                 + "; ".join(f"{name(k)} {none[k]['receive_seconds']:.2f} s to receive, "
                             f"{none[k]['decode_parallel_seconds']:.2f} s to decode {none[k]['workers']} at a time"
                             for k in keys if k != "raw")
                 + f". Over loopback the client waits on its decoder, not on the stream ({name(fastest)} arrives "
                 f"fastest, {none[fastest]['image_bytes'] / MIB / none[fastest]['receive_seconds']:,.0f} MiB/s).")
    lines.append(f"- **Raw is the slowest to receive** ({none['raw']['receive_seconds']:.2f} s uncompressed): "
                 f"{none['raw']['image_bytes'] / MIB:,.0f} MiB, one image per message ("
                 + ("each over mosaicod's 50 MiB upload limit but within its 512 MiB download one"
                    if max(i["bytes"] for i in none["raw"]["images"]) > 50 * MIB else "each within mosaicod's limits")
                 + "), and the server undoes the Parquet ZSTD of every image.")
    if all(t.get("timestamps_as_uploaded") for run in by.values() for t in run.values()):
        lines.append(f"- **Every image comes back with the capture time it was uploaded with**, to the nanosecond: "
                     f"the client checked each `timestamp_ns` of every format against its `timestamps.csv`, in both "
                     "runs.")
    exact = [k for k in keys if all(by[run][k]["matches_raw"] for run in by)]
    lines.append(f"- **The exact formats arrive exact**: {', '.join(name(k) for k in exact)} decoded on the client "
                 f"to the same {n} frames as raw, byte for byte, in both runs.")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timestamps", type=Path, default=WORKSPACE / "datasets/regular/batch_060/timestamps.csv")
    parser.add_argument("--preset", type=int, default=5)
    parser.add_argument("--crf", type=int, default=18)
    args = parser.parse_args()

    sequence = report.sequence_name(args.timestamps)
    encoding = Path(report.sequence_dir(sequence)) / f"preset_{args.preset}_crf_{args.crf}"
    results = WORKSPACE / "image-results" / encoding / "client"
    results.mkdir(parents=True, exist_ok=True)
    runs = {}
    for run in RUNS:
        source = WORKSPACE / "output" / encoding / f"{TOPIC}.fetch-{run}.json"
        shutil.copyfile(source, results / f"fetch-{run}.json")
        runs[run] = json.loads(source.read_text())
    n = len(runs["none"]["topics"][0]["images"])
    settings = f"preset {args.preset} CRF {args.crf}"
    context = (f"{sequence}, {settings}: {n} frames of {runs['none']['width']}x{runs['none']['height']}, read back "
               "over loopback as mosaicod serves a topic.")
    width = 1.25 * 2 * len(runs["none"]["topics"]) + 2.4

    bars, pairs, groups = paired(runs, lambda t: t["receive_seconds"])
    bar_chart("Receiving all the images", f"{context}\nFrom the DoGet call to the end of the stream, all {n} images; "
              "each format uncompressed, then the same fetch with LZ4 (hatched). Lower is faster.", "s", bars, results / "receive-time.svg", 2,
              width=width, groups=groups, pairs=pairs)
    bars, pairs, groups = paired(runs, lambda t: t["first_image_seconds"] * 1000)
    bar_chart("The first image: how long the client waits before the stream starts",
              f"{context}\n" + bold("From the client sending DoGet to the first image in its hands") + " (its bytes "
              "and timestamp_ns): the server reads the first batch from the\ntopic's Parquet file (undoing its ZSTD) and "
              "encodes it (with LZ4 or not), the message crosses loopback, and the client decodes it back into a "
              "record batch.\nNot counted: turning the image into a frame (see end-to-end). A batch can carry up to "
              f"{facts(runs)[1]} images, which arrive together.\nEach format uncompressed, then the same fetch with LZ4 (hatched). "
              "Lower is faster.", "ms", bars, results / "first-image.svg", 0, width=width, groups=groups, pairs=pairs)
    bars, pairs, groups = paired(runs, lambda t: t["wire_bytes"] / MIB)
    bar_chart("On the wire", f"{context}\nWhat the FlightData messages carried for all {n} images; each format "
              "uncompressed, then the same fetch with LZ4 (hatched). Lower is less.", "MiB", bars, results / "wire-size.svg", 0, width=width,
              groups=groups, pairs=pairs)
    end_to_end(results / "end-to-end.svg", runs, n, context)
    (results / "README.md").write_text(readme(sequence, settings, runs))
    print(f"wrote {results}")


if __name__ == "__main__":
    main()
