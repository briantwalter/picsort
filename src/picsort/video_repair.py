"""Auditable, resumable quarantine of indexed external-reference movies."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .images import md5_file
from .index import open_index
from .video_references import VIDEO_INSPECTION_VERSION, has_external_references


def managed_path(root: Path, value: str) -> Path:
    path = Path(value).absolute()
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        if part in {".", ".."}:
            raise ValueError("Unsafe destination path")
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink in destination path")
    return path


def _save_manifest(path: Path, value: dict) -> None:
    import tempfile

    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def repair_videos(index: Path, destination: Path, apply: bool = False) -> dict:
    destination = destination.absolute()
    if destination.is_symlink() or destination.resolve() != destination:
        raise ValueError("Destination must be a canonical directory without symlinks")
    managed_path(destination, str(destination / "index.html"))
    journal_path = destination / ".picsort-reference-repair.json"
    if journal_path.is_symlink():
        raise ValueError("Unsafe repair manifest")
    journal = (
        json.loads(journal_path.read_text())
        if journal_path.exists()
        else {"index": str(index.resolve()), "destination": str(destination), "moves": []}
    )
    if journal["index"] != str(index.resolve()) or journal["destination"] != str(destination):
        raise ValueError("Repair manifest belongs to another index or destination")
    connection = open_index(index, readonly=True)
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM images WHERE media_type='video' AND destination_path IS NOT NULL"
            )
        ]
    finally:
        connection.close()
    by_path = defaultdict(list)
    for row in rows:
        by_path[row["destination_path"]].append(row)
    result = {
        "scanned": 0,
        "references": 0,
        "quarantined": 0,
        "duplicate_rows": 0,
        "multiple_destinations": 0,
        "errors": [],
        "moves": [],
    }
    active = defaultdict(list)
    checked = []
    known = {move["source"]: move for move in journal["moves"]}
    moves = []
    for value, matching in sorted(by_path.items()):
        try:
            source = managed_path(destination, value)
            relative = source.relative_to(destination)
            if not relative.parts or relative.parts[0] == "deprecated":
                continue
            result["scanned"] += 1
            hashes = {row["md5"] for row in matching}
            if len(hashes) != 1 or None in hashes:
                raise ValueError("Missing or conflicting indexed hashes")
            digest = next(iter(hashes))
            target = destination / "deprecated" / "reference-videos" / relative
            target = managed_path(destination, str(target))
            recorded = known.get(value)
            if recorded and (recorded["md5"] != digest or recorded["target"] != str(target)):
                raise ValueError("Repair manifest no longer matches index")
            # A completed file move can precede the SQLite commit after an interruption.
            inspect_path = target if not source.exists() and recorded else source
            if not inspect_path.is_file():
                raise ValueError("Indexed destination is missing")
            actual_size = inspect_path.stat().st_size
            expected_sizes = {row["size"] for row in matching}
            if actual_size not in expected_sizes:
                raise ValueError(
                    f"Destination size differs from index: actual={actual_size} bytes, "
                    f"indexed={sorted(expected_sizes)}; left unchanged"
                )
            if not has_external_references(inspect_path):
                if inspect_path != source:
                    raise ValueError("Quarantine file is no longer a reference movie")
                checked.extend(row["id"] for row in matching)
                if any(row["status"] not in {"stale", "excluded"} for row in matching):
                    active[digest].append((source, matching))
                continue
            if md5_file(inspect_path) != digest:
                raise ValueError("Reference movie hash differs from index")
            if target.exists():
                identity = [target.stat().st_dev, target.stat().st_ino]
                resumable_copy = recorded and recorded.get("copying_identity") == identity
                if not recorded or (md5_file(target) != digest and not resumable_copy):
                    raise ValueError("Quarantine target already exists or differs")
            move = {
                **(recorded or {}),
                "source": value,
                "target": str(target),
                "md5": digest,
                "rows": [row["id"] for row in matching],
            }
            moves.append(move)
            result["references"] += 1
            result["moves"].append(move)
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{value}: {exc}")
    reconciliations = []
    for digest, destinations in active.items():
        if len(destinations) > 1:
            result["multiple_destinations"] += 1
            continue
        path, matching = destinations[0]
        matching = [r for r in matching if r["status"] not in {"stale", "excluded"}]
        winner = min(matching, key=lambda r: (r["status"] != "organized", r["id"]))
        for row in matching:
            status = "organized" if row["id"] == winner["id"] else "duplicate"
            if row["status"] != status:
                reconciliations.append((status, str(path), row["id"]))
                result["duplicate_rows"] += status == "duplicate"
    if not apply:
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = index.with_name(index.name + ".before-video-repair-" + stamp)
    source_db = open_index(index, readonly=True)
    try:
        with sqlite3.connect(backup) as backup_db:
            source_db.backup(backup_db)
    finally:
        source_db.close()
    result["backup"] = str(backup)
    journal["backup"] = str(backup)
    current_moves = {move["source"]: move for move in journal["moves"]}
    current_moves.update({move["source"]: move for move in moves})
    journal["moves"] = list(current_moves.values())
    _save_manifest(journal_path, journal)
    connection = open_index(index)
    try:
        for move in moves:
            try:
                source = managed_path(destination, move["source"])
                target = managed_path(destination, move["target"])
                if source.exists():
                    if md5_file(source) != move["md5"] or not has_external_references(source):
                        raise ValueError("Source changed since repair preview")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists() or md5_file(target) != move["md5"]:
                        if target.exists():
                            identity = [target.stat().st_dev, target.stat().st_ino]
                            if move.get("copying_identity") != identity:
                                raise ValueError("Unrecognized partial quarantine file")
                            writer = target.open("r+b")
                        else:
                            writer = target.open("xb")
                        with writer, source.open("rb") as reader:
                            stat = os.fstat(writer.fileno())
                            move["copying_identity"] = [stat.st_dev, stat.st_ino]
                            _save_manifest(journal_path, journal)
                            writer.seek(0)
                            shutil.copyfileobj(reader, writer)
                            writer.truncate()
                            writer.flush()
                            os.fsync(writer.fileno())
                    if md5_file(target) != move["md5"]:
                        raise ValueError("Quarantine verification failed; source retained")
                    shutil.copystat(source, target)
                    move.pop("copying_identity", None)
                    _save_manifest(journal_path, journal)
                    source.unlink()
                elif not target.is_file() or md5_file(target) != move["md5"]:
                    raise ValueError("Cannot resume missing or changed quarantine file")
                connection.executemany(
                    "UPDATE images SET status='excluded', exclusion_reason='external_reference', "
                    "video_inspection_version=?, destination_path=?, error=NULL WHERE id=?",
                    [(VIDEO_INSPECTION_VERSION, str(target), row_id) for row_id in move["rows"]],
                )
                connection.commit()
                result["quarantined"] += 1
            except (OSError, ValueError) as exc:
                result["errors"].append(f"{move['source']}: {exc}")
        connection.executemany(
            "UPDATE images SET video_inspection_version=? WHERE id=?",
            [(VIDEO_INSPECTION_VERSION, row_id) for row_id in checked],
        )
        connection.executemany(
            "UPDATE images SET status=?, destination_path=?, error=NULL WHERE id=?",
            reconciliations,
        )
        connection.commit()
        from .report import render

        render(connection, destination / "index.html", "video", destination)
    finally:
        connection.close()
    return result
