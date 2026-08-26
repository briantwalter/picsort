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


def date_parts(exif_date: str | None) -> tuple[str, str]:
    if exif_date and exif_date[:4] not in {"1969", "1970"}:
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
) -> dict:
    rows = list(pending_images(connection, media_type))
    copied = skipped = duplicates = errors = 0
    plans = []
    groups = [[row] for row in rows] if media_type == "video" else _groups(rows, threshold)
    for group in groups:
        winner = max(group, key=quality)
        pending = [row for row in group if row["status"] != "organized"]
        if not pending:
            continue
        folder_name, filename_date = date_parts(winner["exif_date"])
        folder = destination / folder_name
        output = folder / f"{filename_date}-{winner['md5']}.{winner['extension']}"
        plans.append((winner, pending, output))
    if progress_start:
        progress_start(len(plans))

    def record_success(winner, pending, output, copied_now=False):
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
        if progress_callback:
            progress_callback(winner["source_path"], "copied" if copied_now else "skipped")

    if dry_run:
        for winner, pending, output in plans:
            if output.exists():
                skipped += 1
            else:
                copied += 1
            duplicates += sum(row["id"] != winner["id"] for row in pending)
            if progress_callback:
                progress_callback(winner["source_path"], "planned")
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_copy_file, winner["source_path"], output): (winner, pending, output)
                for winner, pending, output in plans
                if not output.exists()
            }
            for winner, pending, output in plans:
                if output.exists():
                    record_success(winner, pending, output)
            for future in as_completed(futures):
                winner, pending, output = futures[future]
                try:
                    future.result()
                    record_success(winner, pending, output, copied_now=True)
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
    return {"copied": copied, "skipped": skipped, "duplicates": duplicates, "errors": errors}
