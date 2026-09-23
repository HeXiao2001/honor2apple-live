"""One-command local converter; never modifies source files or imports Photos."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from .media import UnsupportedMedia, convert_honor_jpeg
from .dng_gps import copy_gps_from_jpeg


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def convert_one(source: Path, output: Path, movie: Path | None,
                gps_jpeg: Path | None, overwrite: bool) -> list[Path]:
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in (".jpg", ".jpeg", ".dng"):
        raise UnsupportedMedia(f"Unsupported file extension: {source.suffix}")
    output.mkdir(parents=True, exist_ok=True)
    stem = source.stem

    if source.suffix.lower() == ".dng":
        if gps_jpeg is not None and source.stem != gps_jpeg.stem:
            raise UnsupportedMedia("GPS companion JPEG must have the same base filename as DNG")
        target = output / source.name
        if target.resolve() == source.resolve():
            if gps_jpeg is not None:
                raise UnsupportedMedia("GPS output would overwrite the original DNG; choose another output directory")
            return [source]
        if target.exists() and not overwrite:
            raise FileExistsError(f"Output exists: {target}; use --overwrite")
        with tempfile.NamedTemporaryFile(dir=output, suffix=".part", delete=False) as temp:
            staging = Path(temp.name)
        try:
            if gps_jpeg is None:
                shutil.copyfile(source, staging)
                if digest(source) != digest(staging):
                    raise IOError("DNG copy differs from original")
            else:
                staging.write_bytes(copy_gps_from_jpeg(source.read_bytes(), gps_jpeg))
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
        return [target]

    identifier = str(uuid.uuid4()).upper()
    hdr, movie_bytes = convert_honor_jpeg(source.read_bytes(), identifier,
                                          movie.read_bytes() if movie else None)
    still_target = output / (stem + ".jpg")
    movie_target = output / (stem + ".mov") if movie_bytes is not None else None
    targets = [still_target] + ([movie_target] if movie_target else [])
    if still_target.resolve() == source.resolve():
        raise UnsupportedMedia("HDR output would overwrite the original JPG; choose another output directory")
    if movie is not None and movie_target is not None and movie_target.resolve() == movie.resolve():
        raise UnsupportedMedia("MOV output would overwrite the source video; choose another output directory")
    if not overwrite:
        for target in targets:
            if target.exists():
                raise FileExistsError(f"Output exists: {target}; use --overwrite")

    with tempfile.TemporaryDirectory(prefix="honor2apple-") as temporary:
        scratch = Path(temporary)
        still_temp = scratch / still_target.name
        still_temp.write_bytes(hdr)
        if movie_target is not None:
            source_video = scratch / "source.mp4"
            source_video.write_bytes(movie_bytes)
            movie_temp = scratch / movie_target.name
            swift_file = Path(__file__).with_name("make_live_mov.swift")
            command = ["swift", "-module-cache-path", str(scratch / "swift-cache"),
                       str(swift_file), str(source_video), str(movie_temp), identifier]
            completed = subprocess.run(command, text=True, capture_output=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            if not movie_temp.is_file():
                raise RuntimeError("MOV conversion produced no file")
        os.replace(still_temp, still_target)
        if movie_target is not None:
            os.replace(movie_temp, movie_target)
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="honor2apple",
        description="Convert calibrated HONOR motion JPG to Apple HDR Live Photo pair; copy DNG unchanged.")
    parser.add_argument("files", nargs="+", type=Path, help="Honor JPG/JPEG or original DNG")
    parser.add_argument("-o", "--output", required=True, type=Path, help="Output directory for Photos import")
    parser.add_argument("--video", type=Path, help="Separate source MP4 (single JPG input only)")
    gps_options = parser.add_mutually_exclusive_group()
    gps_options.add_argument("--gps-from-jpeg", type=Path,
                             help="Copy actual GPS tags from the same-shot JPG to a DNG copy")
    gps_options.add_argument("--auto-gps", action="store_true",
                             help="For each DNG, require a same-named JPG alongside it for GPS")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output files")
    args = parser.parse_args(argv)
    if args.video and (len(args.files) != 1 or args.files[0].suffix.lower() not in (".jpg", ".jpeg")):
        parser.error("--video requires exactly one JPEG input")
    if args.video and not args.video.is_file():
        parser.error(f"Video does not exist: {args.video}")
    if args.gps_from_jpeg and (len(args.files) != 1 or args.files[0].suffix.lower() != ".dng"):
        parser.error("--gps-from-jpeg requires exactly one DNG input")
    if args.gps_from_jpeg and not args.gps_from_jpeg.is_file():
        parser.error(f"GPS JPEG does not exist: {args.gps_from_jpeg}")
    failures = 0
    for source in args.files:
        try:
            gps_jpeg = (args.gps_from_jpeg if args.gps_from_jpeg else
                        source.with_suffix(".jpg") if args.auto_gps and source.suffix.lower() == ".dng"
                        else None)
            if gps_jpeg and not gps_jpeg.is_file():
                raise FileNotFoundError(f"GPS companion JPEG is missing: {gps_jpeg}")
            results = convert_one(source, args.output, args.video, gps_jpeg, args.overwrite)
            print(f"{source.name} -> " + ", ".join(str(path) for path in results))
        except (OSError, ValueError, RuntimeError) as error:
            print(f"{source}: {error}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
