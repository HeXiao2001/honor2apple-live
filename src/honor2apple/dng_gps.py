"""Copy observed JPEG GPS tags into a new DNG IFD without touching raw pixels.

This product includes DNG technology under license by Adobe.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import struct

from PIL import Image

from .media import UnsupportedMedia


def _read_ifd0(data: bytes):
    if data[:2] not in (b"II", b"MM"):
        raise UnsupportedMedia("Not a classic TIFF/DNG")
    order = "<" if data[:2] == b"II" else ">"
    if struct.unpack_from(order + "H", data, 2)[0] != 42:
        raise UnsupportedMedia("BigTIFF and unknown TIFF variants are unsupported")
    offset = struct.unpack_from(order + "I", data, 4)[0]
    if offset < 8 or offset >= len(data) - 2:
        raise UnsupportedMedia("Invalid DNG IFD0 pointer")
    count = struct.unpack_from(order + "H", data, offset)[0]
    end = offset + 2 + count * 12 + 4
    if end > len(data):
        raise UnsupportedMedia("Truncated DNG IFD0")
    entries = [data[offset + 2 + i * 12:offset + 14 + i * 12]
               for i in range(count)]
    next_ifd = struct.unpack_from(order + "I", data, end - 4)[0]
    return order, entries, next_ifd


def _ascii_tag(data: bytes, order: str, entries: list[bytes], tag: int) -> str | None:
    for entry in entries:
        identifier, kind, count, value = struct.unpack(order + "HHII", entry)
        if identifier == tag:
            if kind != 2 or count < 1:
                raise UnsupportedMedia(f"Unexpected TIFF ASCII tag {tag}")
            blob = entry[8:8 + count] if count <= 4 else data[value:value + count]
            return blob.split(b"\0", 1)[0].decode("ascii")
    return None


def copy_gps_from_jpeg(dng: bytes, jpeg_path: Path) -> bytes:
    """Return a metadata-enriched DNG. The source DNG byte stream is unchanged after byte 8."""
    order, entries, next_ifd = _read_ifd0(dng)
    if any(struct.unpack_from(order + "H", entry)[0] == 0x8825 for entry in entries):
        raise UnsupportedMedia("DNG already contains GPS; refusing to replace it")
    with Image.open(jpeg_path) as image:
        exif = image.getexif()
        gps = exif.get_ifd(0x8825)
        if not all(tag in gps for tag in (1, 2, 3, 4)):
            raise UnsupportedMedia("Companion JPEG has no complete GPS coordinates")
        if exif.get(271) != _ascii_tag(dng, order, entries, 271) or exif.get(272) != _ascii_tag(dng, order, entries, 272):
            raise UnsupportedMedia("JPEG and DNG camera models do not match")
        if exif.get(306) != _ascii_tag(dng, order, entries, 306):
            raise UnsupportedMedia("JPEG and DNG capture times do not match")

    out = bytearray(dng)

    def append_aligned(blob: bytes) -> int:
        while len(out) % 2:
            out.append(0)
        offset = len(out)
        out.extend(blob)
        if offset > 0xffffffff:
            raise UnsupportedMedia("DNG exceeds classic TIFF offset limit")
        return offset

    def entry(tag: int, kind: int, count: int, value: bytes) -> bytes:
        if len(value) <= 4:
            field = value.ljust(4, b"\0")
        else:
            field = struct.pack(order + "I", append_aligned(value))
        return struct.pack(order + "HHI", tag, kind, count) + field

    def rationals(values) -> bytes:
        return b"".join(struct.pack(order + "II", int(value.numerator), int(value.denominator))
                        for value in values)

    lat = gps[2] if isinstance(gps[2], tuple) else (gps[2],)
    lon = gps[4] if isinstance(gps[4], tuple) else (gps[4],)
    if len(lat) != 3 or len(lon) != 3:
        raise UnsupportedMedia("Companion GPS coordinate has unexpected shape")
    rows = [
        entry(0, 1, 4, bytes((2, 3, 0, 0))),
        entry(1, 2, 2, gps[1].encode("ascii") + b"\0"),
        entry(2, 5, 3, rationals(lat)),
        entry(3, 2, 2, gps[3].encode("ascii") + b"\0"),
        entry(4, 5, 3, rationals(lon)),
    ]
    if 5 in gps and 6 in gps:
        rows += [entry(5, 1, 1, bytes(gps[5])[:1]),
                 entry(6, 5, 1, rationals((gps[6],)))]
    if 7 in gps:
        stamp = gps[7] if isinstance(gps[7], tuple) else (gps[7],)
        if len(stamp) == 3:
            rows.append(entry(7, 5, 3, rationals(stamp)))
    if 27 in gps:
        method = bytes(gps[27])
        rows.append(entry(27, 7, len(method), method))
    if 29 in gps:
        date = gps[29].encode("ascii") + b"\0"
        rows.append(entry(29, 2, len(date), date))
    gps_offset = append_aligned(struct.pack(order + "H", len(rows)) + b"".join(rows)
                                + struct.pack(order + "I", 0))
    entries.append(struct.pack(order + "HHII", 0x8825, 4, 1, gps_offset))
    entries.sort(key=lambda row: struct.unpack_from(order + "H", row)[0])
    ifd0_offset = append_aligned(struct.pack(order + "H", len(entries)) + b"".join(entries)
                                 + struct.pack(order + "I", next_ifd))
    struct.pack_into(order + "I", out, 4, ifd0_offset)
    result = bytes(out)
    if result[:4] != dng[:4] or result[8:len(dng)] != dng[8:]:
        raise RuntimeError("Original DNG payload changed")
    return result
