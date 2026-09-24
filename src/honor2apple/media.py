"""Conservative byte-level conversion for the observed HONOR motion-photo profile."""

from __future__ import annotations

import base64
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import struct

import numpy as np
from PIL import Image


class UnsupportedMedia(ValueError):
    """The input falls outside the measured, verified profile."""


def profile(name: str = "legacy") -> dict:
    files = {"legacy": "calibration.json", "full_1p5": "calibration_full_1p5.json"}
    if name not in files:
        raise ValueError(f"Unknown HDR calibration profile: {name}")
    return json.loads(Path(__file__).with_name(files[name]).read_text())


def select_calibration(private_gain: np.ndarray, requested: str) -> str:
    """Select a measured target rendition without extrapolating gain values."""
    if requested != "auto":
        return requested
    # The legacy 3.846x table was validated through level 134. The new
    # two-photo table validates the complete 0..255 range at 1.5x.
    return "legacy" if int(private_gain.max()) <= 134 else "full_1p5"


def header_segments(data: bytes):
    if not data.startswith(b"\xff\xd8"):
        raise UnsupportedMedia("Not a JPEG")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xff:
            raise UnsupportedMedia("Invalid JPEG marker boundary")
        marker = data[pos + 1]
        if marker == 0xda:
            return
        size = int.from_bytes(data[pos + 2:pos + 4], "big")
        if size < 2 or pos + size + 2 > len(data):
            raise UnsupportedMedia("Invalid JPEG segment length")
        yield pos, marker, data[pos + 4:pos + size + 2]
        pos += size + 2
    raise UnsupportedMedia("Missing JPEG image scan")


def image_scan(data: bytes) -> bytes:
    for pos, marker, _ in header_segments(data):
        if marker == 0xda:
            break
    else:
        # Generators return at SOS before yielding it. Find the position again.
        pos = 2
        while data[pos + 1] != 0xda:
            pos += int.from_bytes(data[pos + 2:pos + 4], "big") + 2
    end = data.find(b"\xff\xd9", pos)
    if end < 0:
        raise UnsupportedMedia("JPEG end marker is missing")
    return data[pos:end + 2]


def primary_end(data: bytes) -> int:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xff:
            raise UnsupportedMedia("Invalid JPEG header")
        marker = data[pos + 1]
        size = int.from_bytes(data[pos + 2:pos + 4], "big")
        if size < 2:
            raise UnsupportedMedia("Invalid JPEG segment")
        if marker == 0xda:
            end = data.find(b"\xff\xd9", pos + size + 2)
            if end < 0:
                raise UnsupportedMedia("Primary JPEG has no end marker")
            return end + 2
        pos += size + 2
    raise UnsupportedMedia("Primary JPEG has no image scan")


def _find_embedded_video(data: bytes, start: int) -> bytes | None:
    probe = start
    while True:
        marker = data.find(b"ftyp", probe)
        if marker < 0:
            return None
        first = marker - 4
        length = int.from_bytes(data[first:marker], "big") if first >= 0 else 0
        if 16 <= length <= 4096 and first + length <= len(data):
            end = first
            types: set[bytes] = set()
            while end + 8 <= len(data):
                size = int.from_bytes(data[end:end + 4], "big")
                kind = data[end + 4:end + 8]
                if size == 1 and end + 16 <= len(data):
                    size = int.from_bytes(data[end + 8:end + 16], "big")
                if size < 8 or end + size > len(data):
                    break
                types.add(kind)
                end += size
            if data[first + 4:first + 8] == b"ftyp" and {b"ftyp", b"mdat", b"moov"} <= types:
                return data[first:end]
        probe = marker + 4


def _find_private_gain(data: bytes, start: int, size: tuple[int, int]) -> np.ndarray:
    probe = start
    while True:
        marker = data.find(b"\xff\xd8\xff", probe)
        if marker < 0:
            raise UnsupportedMedia("No full-size Honor private gain map found")
        try:
            with Image.open(BytesIO(data[marker:])) as image:
                if image.size == size and image.mode == "L":
                    return np.asarray(image, dtype=np.uint8).copy()
        except (OSError, ValueError):
            pass
        probe = marker + 3


def _segment(config: dict, name: str) -> bytes:
    return base64.b64decode(config["segments_base64"][name], validate=True)


def _with_payload(marker: int, payload: bytes) -> bytes:
    if len(payload) + 2 > 65535:
        raise UnsupportedMedia("JPEG metadata exceeds the segment limit")
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _standard_hdr(base: bytes, private_gain: np.ndarray, config: dict) -> bytes:
    values = np.unique(private_gain)
    lookup = config["gain_lut"]
    counts = config["gain_training_counts"]
    if any(int(value) >= len(lookup) or counts[int(value)] < 20 for value in values):
        raise UnsupportedMedia("Gain-map levels exceed measured calibration; refusing to estimate HDR")
    mapped = np.asarray(lookup, dtype=np.uint8)[private_gain]
    stream = BytesIO()
    qtables = {int(k): v for k, v in config["gain_qtables"].items()}
    Image.fromarray(mapped).save(stream, format="JPEG", qtables=qtables, optimize=True)
    gain = stream.getvalue()
    gain = (gain[:2] + _segment(config, "gain_xmp") + _segment(config, "gain_iso")
            + _segment(config, "gain_mpf") + gain[2:])

    base_xmp = _segment(config, "base_xmp")
    updated = re.sub(rb'Item:Length="\d+"',
                     b'Item:Length="' + str(len(gain)).encode() + b'"',
                     base_xmp[4:], count=1)
    if updated == base_xmp[4:]:
        raise UnsupportedMedia("Missing gain-map length metadata")
    primary = bytearray(base[:2] + _with_payload(0xe1, updated)
                        + _segment(config, "base_iso")
                        + _segment(config, "base_mpf") + base[2:])
    mpf = primary.find(b"MPF\0")
    if mpf < 0:
        raise UnsupportedMedia("Missing MPF metadata")
    struct.pack_into(">I", primary, mpf + 58, len(primary))
    struct.pack_into(">I", primary, mpf + 74, len(gain))
    struct.pack_into(">I", primary, mpf + 78, len(primary) - mpf - 4)
    result = bytes(primary) + gain
    if hashlib.sha256(image_scan(base)).digest() != hashlib.sha256(image_scan(result)).digest():
        raise UnsupportedMedia("Primary image scan changed unexpectedly")
    return result


def _apple_note(identifier: str) -> bytes:
    encoded = identifier.encode("ascii") + b"\0"
    note = bytearray(b"Apple iOS\0\0\x01MM" + b"\0" * 18)
    struct.pack_into(">HHHII", note, 14, 1, 0x0011, 2, len(encoded), 32)
    return bytes(note) + encoded


def _add_live_identifier(data: bytes, identifier: str, *, hdr: bool = True) -> bytes:
    exif = [(pos, payload) for pos, marker, payload in header_segments(data)
            if marker == 0xe1 and payload.startswith(b"Exif\0\0")]
    if len(exif) > 1:
        raise UnsupportedMedia("Multiple EXIF segments in still image")
    note = _apple_note(identifier)
    if not exif:
        # Some shared motion photos have no EXIF at all. Add only the Apple
        # pairing tag; the compressed image and any XMP remain untouched.
        tiff = bytearray(b"MM\0*\0\0\0\x08")
        tiff += struct.pack(">H", 1)
        tiff += struct.pack(">HHII", 0x8769, 4, 1, 26)
        tiff += b"\0\0\0\0"
        tiff += struct.pack(">H", 1)
        tiff += struct.pack(">HHII", 0x927c, 7, len(note), 44)
        tiff += b"\0\0\0\0" + note
        edited = _with_payload(0xe1, b"Exif\0\0" + tiff) + data[2:]
        output = data[:2] + edited
        if image_scan(data) != image_scan(output):
            raise UnsupportedMedia("Primary compressed image changed")
        return output
    segment_pos, payload = exif[0]
    tiff = 6
    order = payload[tiff:tiff + 2]
    if order not in (b"II", b"MM"):
        raise UnsupportedMedia("Invalid EXIF byte order")
    endian = "<" if order == b"II" else ">"
    u16 = lambda offset: struct.unpack_from(endian + "H", payload, offset)[0]
    u32 = lambda offset: struct.unpack_from(endian + "I", payload, offset)[0]
    if u16(tiff + 2) != 42:
        raise UnsupportedMedia("Invalid EXIF TIFF header")

    def entries(ifd: int, tag: int) -> list[int]:
        offset = tiff + ifd
        count = u16(offset)
        return [offset + 2 + index * 12 for index in range(count)
                if u16(offset + 2 + index * 12) == tag]

    exif_pointer = entries(u32(tiff + 4), 0x8769)
    if len(exif_pointer) > 1:
        raise UnsupportedMedia("Multiple EXIF IFD pointers")
    edited_payload = bytearray(payload + note)
    if exif_pointer:
        exif_ifd = u32(exif_pointer[0] + 8)
        maker_entries = entries(exif_ifd, 0x927c)
        if maker_entries:
            for entry in maker_entries:
                struct.pack_into(endian + "I", edited_payload, entry + 4, len(note))
                struct.pack_into(endian + "I", edited_payload, entry + 8, len(payload) - tiff)
        else:
            old_ifd_at = tiff + exif_ifd
            old_count = u16(old_ifd_at)
            old_entries = payload[old_ifd_at + 2:old_ifd_at + 2 + 12 * old_count]
            next_ifd = payload[old_ifd_at + 2 + 12 * old_count:old_ifd_at + 6 + 12 * old_count]
            new_ifd = len(edited_payload) - tiff
            edited_payload += struct.pack(endian + "H", old_count + 1)
            edited_payload += old_entries
            edited_payload += struct.pack(endian + "HHII", 0x927c, 7, len(note), len(payload) - tiff)
            edited_payload += next_ifd
            struct.pack_into(endian + "I", edited_payload, exif_pointer[0] + 8, new_ifd)
    else:
        # Keep every original IFD0 entry and all referenced metadata in place.
        old_ifd_at = tiff + u32(tiff + 4)
        old_count = u16(old_ifd_at)
        old_entries = payload[old_ifd_at + 2:old_ifd_at + 2 + 12 * old_count]
        next_ifd = payload[old_ifd_at + 2 + 12 * old_count:old_ifd_at + 6 + 12 * old_count]
        new_exif_ifd = len(edited_payload) - tiff
        edited_payload += struct.pack(endian + "H", 1)
        edited_payload += struct.pack(endian + "HHII", 0x927c, 7, len(note), len(payload) - tiff)
        edited_payload += b"\0\0\0\0"
        new_ifd0 = len(edited_payload) - tiff
        edited_payload += struct.pack(endian + "H", old_count + 1)
        edited_payload += old_entries
        edited_payload += struct.pack(endian + "HHII", 0x8769, 4, 1, new_exif_ifd)
        edited_payload += next_ifd
        struct.pack_into(endian + "I", edited_payload, tiff + 4, new_ifd0)
    replacement = _with_payload(0xe1, bytes(edited_payload))
    old_length = int.from_bytes(data[segment_pos + 2:segment_pos + 4], "big")
    edited = bytearray(data[:segment_pos] + replacement + data[segment_pos + 2 + old_length:])
    if hdr:
        mpf = edited.find(b"MPF\0")
        gain_at = edited.rfind(b"\xff\xd8\xff")
        if mpf < 0 or gain_at <= mpf:
            raise UnsupportedMedia("Cannot update MPF after Live Photo identifier")
        struct.pack_into(">I", edited, mpf + 58, gain_at)
        struct.pack_into(">I", edited, mpf + 74, len(edited) - gain_at)
        struct.pack_into(">I", edited, mpf + 78, gain_at - mpf - 4)
    output = bytes(edited)
    if image_scan(data) != image_scan(output):
        raise UnsupportedMedia("Primary compressed image changed")
    if hdr and image_scan(data[gain_at - (len(replacement) - (old_length + 2)):]) != image_scan(output[gain_at:]):
        raise UnsupportedMedia("Gain-map compressed image changed")
    return output


def convert_motion_sdr(source: bytes, identifier: str,
                       external_movie: bytes | None = None) -> tuple[bytes, bytes]:
    """Preserve the primary JPEG scan and movie when HDR calibration is unavailable."""
    base = source[:primary_end(source)]
    movie = external_movie if external_movie is not None else _find_embedded_video(source, len(base))
    if movie is None:
        raise UnsupportedMedia("No valid embedded movie found")
    return _add_live_identifier(base, identifier, hdr=False), movie


def convert_honor_jpeg(source: bytes, identifier: str | None = None,
                       external_movie: bytes | None = None,
                       calibration_profile: str = "legacy") -> tuple[bytes, bytes | None]:
    """Return an ISO gain-map JPEG and optional unmodified MP4 bitstream."""
    base = source[:primary_end(source)]
    with Image.open(BytesIO(base)) as image:
        size = image.size
        exif = image.getexif()
        if exif.get(271) != "HONOR" or exif.get(272) != "BVL-AN16":
            raise UnsupportedMedia("Only the calibrated HONOR BVL-AN16 profile is supported")
    gain = _find_private_gain(source, len(base), size)
    calibration_profile = select_calibration(gain, calibration_profile)
    config = profile(calibration_profile)
    hdr = _standard_hdr(base, gain, config)
    movie = external_movie if external_movie is not None else _find_embedded_video(source, len(base))
    if movie is not None:
        if identifier is None:
            raise ValueError("Live Photo identifier is required when video exists")
        hdr = _add_live_identifier(hdr, identifier)
    return hdr, movie
