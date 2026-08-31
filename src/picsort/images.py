from __future__ import annotations

import hashlib
import importlib
import re
import threading
from datetime import date
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
    ".raw",
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
VIDEO_SUPPORTED = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".dv"}
RAW_EXTENSIONS = {
    ".raw",
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
DEFAULT_IGNORE_PATTERNS = (r"(^|[_ .-])(thumb|thumbnail|preview|small|tiny|icon|avatar)([_ .-]|$)",)
_HEIF_LOCK = threading.Lock()
_HEIF_REGISTERED = False


def is_supported(path: Path, media_type: str = "image") -> bool:
    extensions = VIDEO_SUPPORTED if media_type == "video" else SUPPORTED
    return path.is_file() and not path.name.startswith("._") and path.suffix.lower() in extensions


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
    return {
        "jpg": "jpeg",
        "jpeg": "jpeg",
        "tif": "tiff",
        "tiff": "tiff",
        "webm": "web",
        "heic": "heic",
    }.get(extension, extension[:3])


def _parse_exif_date(value) -> str | None:
    if not value:
        return None
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2})", str(value))
    return "-".join(match.groups()) if match else None


def _capture_date_from_exif(exif) -> tuple[str | None, str | None]:
    try:
        nested = exif.get_ifd(ExifTags.IFD.Exif)
    except (KeyError, TypeError, ValueError):
        nested = {}
    for tag, name in (
        (36867, "DateTimeOriginal"),
        (36868, "DateTimeDigitized"),
    ):
        date = _parse_exif_date(nested.get(tag) or exif.get(tag))
        if date:
            return date, name
    date = _parse_exif_date(exif.get(306))
    return (date, "DateTime") if date else (None, None)


def _register_heif_opener() -> None:
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    with _HEIF_LOCK:
        if _HEIF_REGISTERED:
            return
        try:
            pillow_heif = importlib.import_module("pillow_heif")
        except ImportError as exc:
            raise RuntimeError("HEIC files require the optional 'heic' dependency") from exc
        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED = True


def _open_image(path: Path):
    if path.suffix.lower() in RAW_EXTENSIONS:
        try:
            import rawpy

            return Image.fromarray(
                rawpy.imread(str(path)).postprocess(output_bps=8, half_size=True)
            )
        except ImportError as exc:
            raise RuntimeError("RAW files require the optional 'raw' dependency") from exc
    if path.suffix.lower() == ".heic":
        _register_heif_opener()
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
        "date_source": None,
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
        result["exif_date"], result["date_source"] = _capture_date_from_exif(exif)
    finally:
        close = getattr(image, "close", None)
        if close:
            close()
        if image is not source_image:
            source_close = getattr(source_image, "close", None)
            if source_close:
                source_close()
    return result


def inspect_capture_date(path: Path) -> tuple[str | None, str | None]:
    source_image = _open_image(path)
    try:
        exif = source_image.getexif() if hasattr(source_image, "getexif") else {}
        return _capture_date_from_exif(exif)
    finally:
        close = getattr(source_image, "close", None)
        if close:
            close()


def _video_date(metadata: dict) -> str | None:
    for key in ("creation_time", "creation-date", "date"):
        value = metadata.get(key)
        if not value:
            continue
        match = re.search(r"(\d{4})[-:](\d{2})[-:](\d{2})", str(value))
        if match:
            return "-".join(match.groups())
    return None


def _dvgrab_date(path: Path) -> str | None:
    match = re.fullmatch(
        r"dvgrab-(\d{4})\.(\d{2})\.(\d{2})_\d{2}-\d{2}-\d{2}\.dv",
        path.name,
        re.IGNORECASE,
    )
    if not match:
        return None
    year, month, day = (int(value) for value in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _video_capture_date(path: Path, metadata: dict) -> tuple[str | None, str | None]:
    metadata_date = _video_date(metadata)
    if metadata_date:
        return metadata_date, "VideoMetadata"
    if path.suffix.lower() == ".dv":
        filename_date = _dvgrab_date(path)
        if filename_date:
            return filename_date, "dvgrab filename"
    return None, None


def _video_frame_rate(stream) -> float | None:
    codec_name = (getattr(stream.codec_context, "name", None) or "").lower()
    if codec_name == "dvvideo":
        rates = (stream.guessed_rate, stream.base_rate, stream.average_rate)
    else:
        rates = (stream.average_rate, stream.guessed_rate, stream.base_rate)
    for rate in rates:
        if rate and float(rate) > 0:
            return float(rate)
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
        result["frame_rate"] = _video_frame_rate(stream)
        if stream.duration is not None and stream.time_base is not None:
            result["duration"] = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            result["duration"] = container.duration / 1_000_000
        metadata = dict(container.metadata)
        metadata.update(stream.metadata)
        result["metadata_count"] = len(metadata)
        result["exif_date"], result["date_source"] = _video_capture_date(path, metadata)
    finally:
        container.close()
    return result


def inspect_media(path: Path, source_root: Path, media_type: str) -> dict:
    if media_type == "video":
        return inspect_video(path, source_root)
    return inspect_image(path, source_root)
