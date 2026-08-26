from __future__ import annotations

import hashlib
import re
from pathlib import Path

import imagehash
from PIL import ExifTags, Image, ImageFilter, ImageOps

SUPPORTED = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".raf",
    ".orf",
    ".rw2",
    ".pef",
}
VIDEO_SUPPORTED = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef"}
DEFAULT_IGNORE_PATTERNS = (r"(^|[_ .-])(thumb|thumbnail|preview|small|tiny|icon|avatar)([_ .-]|$)",)
DATE_TAGS = {
    tag
    for tag, name in ExifTags.TAGS.items()
    if name in {"DateTimeOriginal", "DateTimeDigitized", "DateTime"}
}


def is_supported(path: Path, media_type: str = "image") -> bool:
    extensions = VIDEO_SUPPORTED if media_type == "video" else SUPPORTED
    return path.is_file() and path.suffix.lower() in extensions


def ignored_by_name(path: Path, patterns: tuple[str, ...]) -> bool:
    name = path.name.lower()
    return any(re.search(pattern, name) for pattern in patterns)


def md5_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_extension(path: Path) -> str:
    extension = path.suffix.lower().lstrip(".")
    return {"jpeg": "jpg", "tiff": "tif", "webm": "web"}.get(extension, extension[:3])


def _open_image(path: Path):
    if path.suffix.lower() in RAW_EXTENSIONS:
        try:
            import rawpy

            return Image.fromarray(
                rawpy.imread(str(path)).postprocess(output_bps=8, half_size=True)
            )
        except ImportError as exc:
            raise RuntimeError("RAW files require the optional 'raw' dependency") from exc
    return Image.open(path)


def _base_result(path: Path, source_root: Path, media_type: str) -> dict:
    stat = path.stat()
    result = {
        "source_path": str(path),
        "source_root": str(source_root),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "md5": None,
        "phash": None,
        "extension": normalized_extension(path),
        "media_type": media_type,
        "width": None,
        "height": None,
        "sharpness": None,
        "exif_date": None,
        "latitude": None,
        "longitude": None,
        "metadata_count": 0,
        "duration": None,
        "bitrate": None,
        "frame_rate": None,
        "codec": None,
        "status": "ready",
        "destination_path": None,
        "error": None,
    }
    return result


def inspect_image(path: Path, source_root: Path) -> dict:
    result = _base_result(path, source_root, "image")
    result["md5"] = md5_file(path)
    source_image = _open_image(path)
    image = ImageOps.exif_transpose(source_image)
    try:
        result["width"], result["height"] = image.size
        result["phash"] = str(imagehash.phash(image))
        grayscale = image.convert("L").resize((256, 256))
        edges = grayscale.filter(ImageFilter.FIND_EDGES)
        result["sharpness"] = float(edges.resize((1, 1)).getpixel((0, 0)))
        exif = image.getexif() if hasattr(image, "getexif") else {}
        result["metadata_count"] = len(exif)
        for tag in DATE_TAGS:
            value = exif.get(tag)
            if value:
                match = re.match(r"^(\d{4}):(\d{2}):(\d{2})", str(value))
                if match:
                    result["exif_date"] = "-".join(match.groups())
                    break
    finally:
        close = getattr(image, "close", None)
        if close:
            close()
        if image is not source_image:
            source_close = getattr(source_image, "close", None)
            if source_close:
                source_close()
    return result


def _video_date(metadata: dict) -> str | None:
    for key in ("creation_time", "creation-date", "date"):
        value = metadata.get(key)
        if not value:
            continue
        match = re.search(r"(\d{4})[-:](\d{2})[-:](\d{2})", str(value))
        if match:
            return "-".join(match.groups())
    return None


def inspect_video(path: Path, source_root: Path) -> dict:
    result = _base_result(path, source_root, "video")
    result["md5"] = md5_file(path)
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("Video files require the 'av' dependency") from exc
    container = av.open(str(path))
    try:
        stream = next((item for item in container.streams if item.type == "video"), None)
        if stream is None:
            raise ValueError("No video stream found")
        result["width"] = stream.codec_context.width
        result["height"] = stream.codec_context.height
        result["codec"] = stream.codec_context.name
        result["bitrate"] = stream.bit_rate or container.bit_rate
        result["frame_rate"] = float(stream.average_rate) if stream.average_rate else None
        if stream.duration is not None and stream.time_base is not None:
            result["duration"] = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            result["duration"] = container.duration / 1_000_000
        metadata = dict(container.metadata)
        metadata.update(stream.metadata)
        result["metadata_count"] = len(metadata)
        result["exif_date"] = _video_date(metadata)
    finally:
        container.close()
    return result


def inspect_media(path: Path, source_root: Path, media_type: str) -> dict:
    if media_type == "video":
        return inspect_video(path, source_root)
    return inspect_image(path, source_root)
