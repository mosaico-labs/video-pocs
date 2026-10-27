#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.11"]
# ///
"""How widely the image formats of image_formats.py are supported, from
public sources (not from this workspace's measurements), in the tracked
image-results/format-support/ folder:

    README.md            the support, the use on the web and how simple each
                         format is to decode, with the sources
    support.svg          which software opens each format as an image
    web-usage.svg        how many websites use each format

The facts are the SUPPORT, WEB_USAGE and SIMPLICITY tables below, each with
its source; update them, then run:

    uv run scripts/format_support.py

The charts share report.py's visual system.
"""

from matplotlib.patches import Patch, Rectangle

from image_formats import FORMATS
from report import INK, INK_SECONDARY, MUTED, SURFACE, WORKSPACE, plt, save, subtitle

FOLDER = WORKSPACE / "image-results" / "format-support"

# The formats in general, not image_formats.py's settings of them (lossless
# AVIF, 16-bit PNG, quality 100): support doesn't depend on those.
NAMES = {"raw": "Raw I420", "zstd": "zstd (of raw I420)", "avif": "AVIF", "ffv1": "FFV1", "png": "PNG", "jpeg": "JPEG"}

# Where each format opens as an image: built in, only with an add-on or code
# of one's own, or not at all. One hue, light to dark: the more support, the
# darker; each cell is also labeled, so color is never the only cue.
LEVELS = {
    "built in": ("#2a78d6", SURFACE),
    "add-on or own code": ("#a9c7ee", INK),
    "no": ("#efede6", INK_SECONDARY),
}

PLACES = ["Web browsers", "Windows Photos", "macOS Preview", "Python (Pillow)", "FFmpeg"]

# (level, note) per format and place. A note says what the cell rests on;
# the numbers [n] point to SOURCES.
SUPPORT = {
    "raw": [
        ("add-on or own code", "no image format: a page needs its own code (JavaScript, WebGL) to read the planes"),
        ("no", "no image format"),
        ("no", "no image format"),
        ("add-on or own code", "a few lines of NumPy, given the size, then a YUV to RGB conversion"),
        ("built in", "`-f rawvideo -pix_fmt yuv420p -s WxH`: the size and layout given on the command line"),
    ],
    "zstd": [
        ("add-on or own code", "zstd is only an HTTP transfer encoding (Chrome 123+, Firefox 126+ [2], Safari 26.3 "
                               "[2] [15], which caniuse's own zstd page still calls partial [14]); the planes "
                               "inside need the page's own code, as raw"),
        ("no", "no image format"),
        ("no", "no image format"),
        ("add-on or own code", "decompressing is in the standard library since Python 3.14 (`compression.zstd`), "
                               "then the planes as raw"),
        ("add-on or own code", "FFmpeg doesn't decompress zstd files: `zstd -d` first, then as raw"),
    ],
    "avif": [
        ("built in", "~94% of users' browsers (caniuse, 2026) [1]"),
        ("add-on or own code", "needs the free AV1 Video Extension from the Microsoft Store [4]"),
        ("built in", "since macOS 13 Ventura [4]"),
        ("built in", "natively since Pillow 11.3 [3]"),
        ("built in", "with libdav1d or libaom"),
    ],
    "ffv1": [
        ("no", "no browser decodes FFV1"),
        ("add-on or own code", "a player with FFmpeg's decoder: VLC [10], or LAV Filters / ffdshow [13]"),
        ("add-on or own code", "a player with FFmpeg's decoder, such as VLC [10]"),
        ("add-on or own code", "through FFmpeg (PyAV); Pillow doesn't read it"),
        ("built in", "FFmpeg's FFV1 decoder, in libavcodec since 2003 [13]"),
    ],
    "png": [("built in", "")] * 5,
    "jpeg": [("built in", "")] * 5,
}

# Share of websites using each format, W3Techs, 12 July 2026 [6]. The other
# formats are not used for images on the web.
WEB_USAGE = {"png": 76.5, "jpeg": 70.7, "avif": 1.5}

# From the simplest to decode to the most complex: (format, what decoding
# takes, the source of its numbers).
SIMPLICITY = [
    ("raw", "Nothing: the file is the planes. The reader must know the size and the pixel layout from elsewhere, "
            "and convert YUV to RGB to show it.", ""),
    ("zstd", "A general-purpose decompressor, > 500 MB/s per core whatever the level [7], then the planes as raw. "
             "The library is everywhere (Linux, Python's standard library), but it knows nothing about images.",
     "[7]"),
    ("jpeg", "The most widely implemented image decoder: Huffman decoding and an 8x8 inverse DCT, SIMD-accelerated "
             "in libjpeg-turbo, 2-6x faster than plain libjpeg [8], and with hardware decoders on phones, cameras "
             "and GPUs.", "[8]"),
    ("png", "Inflate (zlib) and per-row filters: simple, but one deflate stream, so a single core decodes it. "
            "Rust's native `png` crate is about as fast as a C library's wrapper [9].", "[9]"),
    ("ffv1", "An intra-only video codec with a range coder (RFC 9043 [5]). The implementations are few: FFmpeg's, "
             "a Go decoder, MediaConch [5]; FFmpeg's developers say decoding speed was not its main goal [10].",
     "[5] [10]"),
    ("avif", "A full AV1 intra decoder (dav1d, libaom) behind libavif: the most complex here, and on the CPU "
             "slower than JPEG [11]. GPUs decode AV1 video in hardware [11], but there is no evidence of that "
             "for still AVIF images.", "[11]"),
]

SOURCES = [
    ("caniuse data for AVIF, as cited by a Redmine feature request (April 2026)", "https://redmine.org/issues/43943"),
    ("caniuse: Content-Encoding: zstd", "https://caniuse.com/mdn-http_headers_content-encoding_zstd"),
    ("nixpkgs: pillow-avif-plugin removed, Pillow has native AVIF since 11.3",
     "https://code.ornl.gov/nix/nixpkgs/-/commit/45fabc4e86ddcc597a3c5cc12273e14368e81f33"),
    ("Wikipedia: AVIF (support in operating systems and viewers)", "https://en.wikipedia.org/wiki/AVIF"),
    ("RFC 9043: FFV1 Video Coding Format Versions 0, 1, and 3", "https://www.rfc-editor.org/rfc/rfc9043"),
    ("W3Techs: usage statistics of image file formats for websites", "https://w3techs.com/technologies/overview/image_format"),
    ("zstd(1) man page", "https://man.archlinux.org/man/zstd.1.en.txt"),
    ("libjpeg-turbo", "https://ecosystemdashboard.linaro.org/libjpeg-turbo/"),
    ("Google: decoder benchmarks for Rust", "https://github.com/google/decoder-benchmarks-for-rust"),
    ("FFmpeg developers on FFV1 decoding speed (VLC issue)", "https://code.videolan.org/videolan/vlc/-/issues/9606"),
    ("Cloudflare: Introducing support for the AVIF image format", "https://blog.cloudflare.com/generate-avif-images-with-image-resizing/"),
    ("Library of Congress: Sustainability of Digital Formats, FFV1",
     "https://loc.gov/preservation/digital/formats/fdd/fdd000341.shtml"),
    ("Wikipedia: FFV1 (implementations, libavcodec since 2003, Windows codec packs)", "https://en.wikipedia.org/wiki/FFV1"),
    ("caniuse: zstd (Zstandard) content-encoding", "https://caniuse.com/zstd"),
    ("youngju.dev: Content-Encoding zstd and dictionary transport (Safari 26.3, July 2026)",
     "https://www.youngju.dev/blog/2026-07-17-content-encoding-zstd-dictionary-transport.en"),
]


def support_chart(path):
    """The support matrix: one row per format, one column per place."""
    keys = list(SUPPORT)
    figure, axes = plt.subplots(figsize=(10.4, 4.6))
    # Above the column names, which are on top.
    axes.set_title("Which software opens each format as an image", pad=60)
    subtitle(axes, "From public sources (2026), see the README. Built in: no extra software or code. Add-on or own "
                    "code: a free add-on,\na player, or code of one's own (the size, the YUV to RGB conversion).",
             y=1.11)
    for row, key in enumerate(keys):
        for column, (level, _) in enumerate(SUPPORT[key]):
            fill, ink = LEVELS[level]
            axes.add_patch(Rectangle((column + 0.04, row + 0.06), 0.92, 0.88, facecolor=fill, edgecolor="none"))
            axes.text(column + 0.5, row + 0.5, level, ha="center", va="center", color=ink, fontsize=8.5,
                      fontweight="bold" if level == "built in" else "normal")
    axes.set_xlim(0, len(PLACES))
    axes.set_ylim(len(keys), 0)
    axes.set_xticks([c + 0.5 for c in range(len(PLACES))], PLACES)
    axes.set_yticks([r + 0.5 for r in range(len(keys))], [NAMES[k] for k in keys])
    axes.xaxis.tick_top()
    axes.tick_params(length=0, labelcolor=INK, labelsize=9, pad=6)
    axes.grid(False)
    for side in axes.spines.values():
        side.set_visible(False)
    axes.legend(handles=[Patch(facecolor=fill, edgecolor=MUTED, linewidth=0.5, label=level)
                         for level, (fill, _) in LEVELS.items()],
                loc="upper left", bbox_to_anchor=(0, -0.03), ncol=len(LEVELS), borderaxespad=0)
    save(figure, path)


def usage_chart(path):
    """The share of websites using each format, in each format's color."""
    keys = list(SUPPORT)
    figure, axes = plt.subplots(figsize=(8.6, 3.8))
    axes.set_title("How many websites use each format", pad=34)
    subtitle(axes, "Share of websites using the format, W3Techs, 12 July 2026. Raw, zstd and FFV1 are not used for "
                    "images on the web.", y=1.04)
    for row, key in enumerate(keys):
        share = WEB_USAGE.get(key)
        if share is None:
            axes.text(0.5, row, "not used for web images", va="center", color=INK_SECONDARY, fontsize=8.5)
            continue
        axes.barh(row, share, height=0.55, color=FORMATS[key][1], zorder=2)
        axes.annotate(f"{share:.1f}%", (share, row), xytext=(4, 0), textcoords="offset points", va="center",
                      color=INK, fontweight="bold")
    axes.set_yticks(range(len(keys)), [NAMES[k] for k in keys])
    axes.set_ylim(len(keys) - 0.5, -0.5)
    axes.set_xlim(0, 100)
    axes.set_xlabel("% of websites")
    axes.grid(axis="y", visible=False)
    axes.tick_params(axis="y", length=0, labelcolor=INK, labelsize=9)
    save(figure, path)


def readme():
    keys = list(SUPPORT)
    name = NAMES
    lines = [
        "# How widely the image formats are supported",
        "",
        "Generated by `scripts/format_support.py` from public sources, listed at the end; not from this "
        "workspace's measurements (for those, see each encoding's folder, e.g. "
        "[`regular/batch_060/preset_5_crf_18`](../regular/batch_060/preset_5_crf_18/server/README.md)). Do not edit: "
        "update the script's tables.",
        "",
        "The formats are the six the server can turn a video's frames into: raw I420, zstd, lossless AVIF and FFV1, "
        "which keep the frames exactly, and PNG and JPEG, which convert them.",
        "",
        "## In short",
        "",
        "- **The most used**: PNG and JPEG, on about three websites in four each, built into every browser, viewer "
        "and library. AVIF is built into most browsers but on 1.5% of websites; FFV1 is used for archiving (the "
        "Library of Congress accepts it for preservation [12]), raw and zstd are not image formats at all.",
        "- **The simplest to decode**: raw (nothing to decode) and zstd (a fast, ubiquitous decompressor), but only "
        "for a client with its own code, which knows the size and converts YUV to RGB. Of the real image formats, "
        "JPEG is the simplest and the most widely accelerated, then PNG; FFV1 and AVIF need a video decoder.",
        "",
        "For a chart that puts this support next to the formats' measured size, speed and loss, to choose one, see "
        "[choosing a format](../regular/batch_060/preset_5_crf_18/server/README.md#choosing-a-format).",
        "",
        "## Which software opens them",
        "",
        "Whether each opens the file as an image: *built in* needs no extra software or code; *add-on or own code* "
        "needs a free add-on, a player, or code of one's own; *no* can't.",
        "",
        "![Which software opens each format as an image](support.svg)",
        "",
        "| Format | " + " | ".join(PLACES) + " |",
        "|---|" + "---|" * len(PLACES),
    ]
    for key in keys:
        cells = [f"{level}" + (f": {note}" if note else "") for level, note in SUPPORT[key]]
        lines.append(f"| {name[key]} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## How much the web uses them",
        "",
        "![How many websites use each format](web-usage.svg)",
        "",
        "PNG and JPEG lead, on 76.5% and 70.7% of websites, AVIF is on 1.5% (W3Techs, 12 July 2026 [6]); the "
        "shares add up to more than 100% because a website uses several formats.",
        "",
        "## How simple they are to decode",
        "",
        "From the simplest to the most complex, by what a decoder must do:",
        "",
        "| | Format | What decoding takes |",
        "|---|---|---|",
        *(f"| {i} | {name[key]} | {what} |" for i, (key, what, _) in enumerate(SIMPLICITY, 1)),
        "",
        "Every fact with a number [n] is from that source, below. The others are general knowledge, not from these "
        "sources: Python 3.14's standard library module `compression.zstd`; hardware JPEG decoders (e.g. NVIDIA's "
        "nvJPEG, phones' image signal processors); reading raw planes with NumPy or FFmpeg's `rawvideo`, FFV1 "
        "through PyAV, and FFmpeg not reading zstd files; and that no browser decodes FFV1 (no source was found "
        "either way).",
        "",
        "## Sources",
        "",
        *(f"{i}. [{title}]({url})" for i, (title, url) in enumerate(SOURCES, 1)),
    ]
    return "\n".join(lines) + "\n"


def main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    support_chart(FOLDER / "support.svg")
    usage_chart(FOLDER / "web-usage.svg")
    (FOLDER / "README.md").write_text(readme())
    print(f"wrote {FOLDER}")


if __name__ == "__main__":
    main()
