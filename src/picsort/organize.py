from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .index import pending_images


def quality(row) -> tuple:
    if row["media_type"] == "video":
        codec_score = {
            "av1": 5,
            "hevc": 4,
            "h265": 4,
            "h264": 3,
            "vp9": 2,
            "vp8": 1,
        }.get((row["codec"] or "").lower(), 0)
        return (
            (row["width"] or 0) * (row["height"] or 0),
            row["bitrate"] or 0,
            row["frame_rate"] or 0,
            row["duration"] or 0,
            codec_score,
            row["size"] or 0,
        )
    return (
        (row["width"] or 0) * (row["height"] or 0),
        row["sharpness"] or 0,
        row["size"] or 0,
        row["metadata_count"] or 0,
    )


def _groups(rows, threshold: int):
    groups = []
    exact = {}
    for row in rows:
        if row["md5"] in exact:
            groups[exact[row["md5"]]].append(row)
            continue
        group_index = None
        candidate = row["phash"]
        if candidate:
            current = int(candidate, 16)
            for index, group in enumerate(groups):
                other = group[0]["phash"]
                if other and (current ^ int(other, 16)).bit_count() <= threshold:
                    group_index = index
                    break
        if group_index is None:
            group_index = len(groups)
            groups.append([])
        groups[group_index].append(row)
        exact[row["md5"]] = group_index
    return groups


def _copy_file(source: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)


def _pixel_count(row) -> int:
    return (row["width"] or 0) * (row["height"] or 0)


def _deprecated_path(destination: Path, organized_path: str) -> tuple[Path, Path] | None:
    try:
        destination = destination.resolve()
        source = Path(organized_path).resolve()
    except (OSError, RuntimeError):
        return None
    deprecated_root = destination / "deprecated"
    try:
        relative = source.relative_to(destination)
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] == "deprecated":
        return None
    return source, deprecated_root / relative


def _managed_destination(destination: Path, path: str) -> Path | None:
    try:
        destination = destination.resolve()
        managed = Path(path).resolve()
        managed.relative_to(destination)
    except (OSError, RuntimeError, ValueError):
        return None
    return managed


CANONICAL_EXTENSIONS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".heic": "heic",
}
LEGACY_EXTENSIONS = {"jpeg": {"jpg"}, "tiff": {"tif"}, "heic": {"hei"}}


def _canonical_extension(row) -> str:
    return CANONICAL_EXTENSIONS.get(Path(row["source_path"]).suffix.lower(), row["extension"])


def _needs_normalization(row) -> bool:
    canonical = _canonical_extension(row)
    if canonical not in LEGACY_EXTENSIONS:
        return False
    if row["extension"] != canonical:
        return True
    destination_path = row["destination_path"]
    return bool(
        destination_path
        and Path(destination_path).suffix.lower().lstrip(".") in LEGACY_EXTENSIONS[canonical]
    )


def _normalize_destinations(connection, rows, destination: Path, dry_run: bool) -> tuple[int, int]:
    renamed = errors = 0
    candidates = [row for row in rows if _needs_normalization(row)]
    without_destination = [row for row in candidates if not row["destination_path"]]
    if without_destination and not dry_run:
        connection.executemany(
            "UPDATE images SET extension=? WHERE id=?",
            [(_canonical_extension(row), row["id"]) for row in without_destination],
        )

    by_path = {}
    for row in candidates:
        if row["destination_path"]:
            key = (row["destination_path"], _canonical_extension(row))
            by_path.setdefault(key, []).append(row)
    for (destination_path, canonical), matching_rows in by_path.items():
        source = _managed_destination(destination, destination_path)
        if source is None:
            errors += 1
            continue
        if source.suffix.lower() == f".{canonical}":
            if not dry_run:
                connection.executemany(
                    "UPDATE images SET extension=? WHERE id=?",
                    [(canonical, row["id"]) for row in matching_rows],
                )
            continue
        if source.suffix.lower().lstrip(".") not in LEGACY_EXTENSIONS[canonical]:
            errors += 1
            continue
        target = source.with_suffix(f".{canonical}")
        if source.exists() and target.exists():
            errors += 1
            continue
        if not source.exists() and not target.exists():
            errors += 1
            continue
        if not dry_run and source.exists():
            try:
                source.rename(target)
            except OSError:
                errors += 1
                continue
        renamed += 1
        if not dry_run:
            connection.executemany(
                "UPDATE images SET extension=?, destination_path=? WHERE id=?",
                [(canonical, str(target), row["id"]) for row in matching_rows],
            )
    return renamed, errors


def date_parts(exif_date: str | None) -> tuple[str, str]:
    if exif_date and exif_date[:4] not in {"0000", "1969", "1970"}:
        return exif_date[:4], exif_date
    return "unsorted", "0000-00-00"


def organize(
    connection,
    destination: Path,
    threshold: int = 5,
    dry_run: bool = False,
    workers: int | None = None,
    progress_start=None,
    progress_callback=None,
    media_type: str = "image",
    source_roots=None,
) -> dict:
    rows = list(pending_images(connection, media_type, source_roots))
    renamed, errors = _normalize_destinations(
        connection, rows, destination, dry_run or media_type != "image"
    )
    if not dry_run and media_type == "image":
        rows = list(pending_images(connection, media_type, source_roots))
    copied = skipped = duplicates = deprecated = 0
    plans = []
    groups = [[row] for row in rows] if media_type == "video" else _groups(rows, threshold)
    for group in groups:
        winner = max(group, key=quality)
        pending = [row for row in group if row["status"] != "organized"]
        if not pending:
            continue
        folder_name, filename_date = date_parts(winner["exif_date"])
        folder = destination / folder_name
        output = folder / f"{filename_date}-{winner['md5']}.{_canonical_extension(winner)}"
        retire = [
            row
            for row in group
            if row["status"] == "organized"
            and row["id"] != winner["id"]
            and row["destination_path"]
            and _pixel_count(row) < _pixel_count(winner)
        ]
        plans.append((winner, pending, output, retire))
    if progress_start:
        progress_start(len(plans))

    def retire_lower_resolution(rows):
        nonlocal deprecated, errors
        by_path = {}
        for row in rows:
            by_path.setdefault(row["destination_path"], []).append(row)
        for organized_path, matching_rows in by_path.items():
            paths = _deprecated_path(destination, organized_path)
            if paths is None:
                errors += 1
                continue
            source, target = paths
            if source.exists() and target.exists():
                errors += 1
                continue
            if not source.exists() and not target.exists():
                errors += 1
                continue
            if source.exists():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(source, target)
                except OSError:
                    errors += 1
                    continue
            deprecated += 1
            connection.executemany(
                "UPDATE images SET status='duplicate', destination_path=?, error=NULL WHERE id=?",
                [(str(target), row["id"]) for row in matching_rows],
            )

    def record_success(winner, pending, output, retire, copied_now=False):
        nonlocal copied, skipped, duplicates
        if copied_now:
            copied += 1
        elif output.exists():
            skipped += 1
        else:
            copied += 1
        if not dry_run:
            connection.execute(
                "UPDATE images SET status='organized', destination_path=?, error=NULL WHERE id=?",
                (str(output), winner["id"]),
            )
        for row in pending:
            if row["id"] != winner["id"]:
                duplicates += 1
                if not dry_run:
                    connection.execute(
                        "UPDATE images SET status='duplicate' WHERE id=?", (row["id"],)
                    )
        if not dry_run:
            retire_lower_resolution(retire)
        if progress_callback:
            progress_callback(winner["source_path"], "copied" if copied_now else "skipped")

    if dry_run:
        planned_deprecated = set()
        for winner, pending, output, retire in plans:
            if output.exists():
                skipped += 1
            else:
                copied += 1
            duplicates += sum(row["id"] != winner["id"] for row in pending)
            for row in retire:
                paths = _deprecated_path(destination, row["destination_path"])
                if paths is not None:
                    planned_deprecated.add(paths)
            if progress_callback:
                progress_callback(winner["source_path"], "planned")
        deprecated = len(planned_deprecated)
    else:
        existing_plans = []
        copy_plans = []
        for plan in plans:
            (existing_plans if plan[2].exists() else copy_plans).append(plan)
        for winner, pending, output, retire in existing_plans:
            record_success(winner, pending, output, retire)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_copy_file, winner["source_path"], output): (
                    winner,
                    pending,
                    output,
                    retire,
                )
                for winner, pending, output, retire in copy_plans
            }
            for future in as_completed(futures):
                winner, pending, output, retire = futures[future]
                try:
                    future.result()
                    record_success(winner, pending, output, retire, copied_now=True)
                except (OSError, ValueError) as exc:
                    errors += 1
                    connection.execute(
                        "UPDATE images SET status='error', error=? WHERE id=?",
                        (str(exc), winner["id"]),
                    )
                    if progress_callback:
                        progress_callback(winner["source_path"], "error")
    if not dry_run:
        connection.commit()
    return {
        "copied": copied,
        "skipped": skipped,
        "duplicates": duplicates,
        "deprecated": deprecated,
        "renamed": renamed,
        "errors": errors,
    }
