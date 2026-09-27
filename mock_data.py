"""
Mock data backend for PSC Stream.

When the environment variable PSC_STREAM_MOCK=1 is set, the server uses this
module instead of real Telegram access. It provides:

  * a fake library of 12 study videos (metadata only),
  * deterministic fake byte-range streams (patterned bytes, so Range requests
    can be verified byte-for-byte),
  * placeholder thumbnails generated with the stdlib only (no Pillow needed).

Nothing here touches the network.
"""

import struct
import zlib

# ---------------------------------------------------------------------------
# Fake library
# ---------------------------------------------------------------------------

# (msg_id, title, duration_seconds, size_bytes, mime_type)
_FAKE_VIDEOS = [
    (101, "Indian Constitution - Part 1: Preamble & Features", 1845, 214_748_364, "video/mp4"),
    (102, "Indian Constitution - Part 2: Fundamental Rights", 2210, 268_435_456, "video/mp4"),
    (103, "Kerala History - Ancient to Medieval Period", 1560, 187_904_819, "video/mp4"),
    (104, "Kerala History - Freedom Movement", 1980, 241_172_480, "video/mp4"),
    (105, "General Science - Physics: Motion & Force", 1320, 161_061_273, "video/mp4"),
    (106, "General Science - Chemistry: Acids & Bases", 1475, 174_483_046, "video/mp4"),
    (107, "Quantitative Aptitude - Percentages & Ratios", 1690, 201_326_592, "video/mp4"),
    (108, "Quantitative Aptitude - Time, Speed & Distance", 1815, 214_958_080, "video/mp4"),
    (109, "Malayalam Grammar - Sandhi & Samasam", 1230, 147_639_500, "video/mp4"),
    (110, "English Grammar - Tenses Masterclass", 1740, 208_037_478, "video/mp4"),
    (111, "Indian Economy - Budget 2026 Highlights", 2010, 234_881_024, "video/mp4"),
    (112, "Current Affairs - September 2026 Revision", 2280, 262_144_000, "video/mp4"),
]


def fake_library():
    """Return the fake video list as dicts matching the real scanner's shape."""
    videos = []
    for msg_id, title, duration, size, mime in _FAKE_VIDEOS:
        videos.append(
            {
                "id": msg_id,
                "title": title,
                "duration": duration,
                "size": size,
                "mime_type": mime,
                "has_thumb": True,
                "date": "2026-09-2%d" % (msg_id % 10),
            }
        )
    # newest first (higher id = newer), like the real scan
    videos.sort(key=lambda v: v["id"], reverse=True)
    return videos


def fake_size(msg_id):
    for vid, _t, _d, size, _m in _FAKE_VIDEOS:
        if vid == msg_id:
            return size
    return None


def fake_meta(msg_id):
    for v in fake_library():
        if v["id"] == msg_id:
            return v
    return None


# ---------------------------------------------------------------------------
# Fake byte streams
# ---------------------------------------------------------------------------
# A 1 MiB deterministic pattern buffer. Byte at absolute offset p is
# PATTERN[p % len(PATTERN)], so any sub-range is reproducible and verifiable.

_PATTERN = bytes((i * 37 + 11) % 256 for i in range(1024 * 1024))


def fake_range(msg_id, start, end):
    """Return patterned bytes for the inclusive range [start, end]."""
    n = end - start + 1
    out = bytearray(n)
    plen = len(_PATTERN)
    off = start % plen
    # copy in slices so we never materialise more than needed
    pos = 0
    while pos < n:
        take = min(plen - off, n - pos)
        out[pos : pos + take] = _PATTERN[off : off + take]
        pos += take
        off = 0
    return bytes(out)


# ---------------------------------------------------------------------------
# Placeholder thumbnails (pure-stdlib PNG encoder)
# ---------------------------------------------------------------------------

def _png_chunk(chunk_type, data):
    chunk = struct.pack(">I", len(data)) + chunk_type + data
    return chunk + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)


def make_placeholder_png(seed=0, width=320, height=180):
    """
    Build a small PNG: dark gradient background with a play-triangle glyph.
    The hue shifts with `seed` so different videos get different thumbs.
    """
    import colorsys

    hue = (seed * 47) % 360 / 360.0
    r0, g0, b0 = [int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.16)]
    r1, g1, b1 = [int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.32)]

    cx, cy = width // 2, height // 2
    # play triangle vertices (pointing right)
    tri = [(cx - 22, cy - 30), (cx - 22, cy + 30), (cx + 30, cy)]

    def in_triangle(x, y):
        (x1, y1), (x2, y2), (x3, y3) = tri
        d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if d == 0:
            return False
        a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
        b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
        c = 1 - a - b
        return a >= 0 and b >= 0 and c >= 0

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter byte: none
        t = y / max(height - 1, 1)
        br, bg, bb = (
            int(r0 + (r1 - r0) * t),
            int(g0 + (g1 - g0) * t),
            int(b0 + (b1 - b0) * t),
        )
        for x in range(width):
            if in_triangle(x, y):
                raw += b"\xe8\xec\xf5"  # light triangle
            else:
                raw += bytes((br, bg, bb))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def fake_thumb(msg_id):
    return make_placeholder_png(seed=msg_id)
