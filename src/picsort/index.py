from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    md5 TEXT,
    phash TEXT,
    extension TEXT,
    width INTEGER,
    height INTEGER,
    sharpness REAL,
    media_type TEXT NOT NULL DEFAULT 'image',
    duration REAL,
    bitrate INTEGER,
    frame_rate REAL,
    codec TEXT,
    exif_date TEXT,
    date_source TEXT,
    latitude REAL,
    longitude REAL,
    metadata_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    destination_path TEXT,
    error TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_images_md5 ON images(md5);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
"""


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
    migrations = {
        "media_type": "TEXT NOT NULL DEFAULT 'image'",
        "duration": "REAL",
        "bitrate": "INTEGER",
        "frame_rate": "REAL",
        "codec": "TEXT",
        "date_source": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE images ADD COLUMN {column} {definition}")
    return connection


def upsert_image(connection: sqlite3.Connection, values: dict) -> None:
    existing = connection.execute(
        "SELECT size, mtime_ns, status, destination_path FROM images WHERE source_path=?",
        (values["source_path"],),
    ).fetchone()
    if (
        existing
        and existing["size"] == values.get("size")
        and existing["mtime_ns"] == values.get("mtime_ns")
    ):
        values = dict(values)
        values["status"] = existing["status"]
        values["destination_path"] = existing["destination_path"]
        values["error"] = None
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    updates = ", ".join(f"{key}=excluded.{key}" for key in values if key != "source_path")
    connection.execute(
        f"INSERT INTO images ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(source_path) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP",
        values,
    )


def mark_stale(
    connection: sqlite3.Connection, source_root: Path, paths: set[str], media_type: str = "image"
) -> None:
    rows = connection.execute(
        "SELECT source_path FROM images WHERE source_root=? AND media_type=? AND status != 'stale'",
        (str(source_root), media_type),
    ).fetchall()
    stale = [(row["source_path"],) for row in rows if row["source_path"] not in paths]
    connection.executemany(
        "UPDATE images SET status='stale', updated_at=CURRENT_TIMESTAMP WHERE source_path=?", stale
    )


def pending_images(
    connection: sqlite3.Connection,
    media_type: str = "image",
    source_roots: Iterable[Path | str] | None = None,
) -> Iterable[sqlite3.Row]:
    roots = [str(root) for root in source_roots] if source_roots is not None else None
    if roots is not None:
        if not roots:
            return []
        placeholders = ", ".join("?" for _ in roots)
        return connection.execute(
            "SELECT * FROM images WHERE md5 IS NOT NULL AND status IN "
            "('ready', 'pending', 'error', 'organized', 'duplicate') AND media_type=? "
            f"AND source_root IN ({placeholders})",
            (media_type, *roots),
        )
    return connection.execute(
        "SELECT * FROM images WHERE md5 IS NOT NULL AND status IN "
        "('ready', 'pending', 'error', 'organized', 'duplicate') AND media_type=?",
        (media_type,),
    )


def is_unchanged(
    connection,
    source_path: str,
    source_root: str,
    size: int,
    mtime_ns: int,
    media_type: str,
) -> bool:
    row = connection.execute(
        "SELECT size, mtime_ns, md5, status FROM images "
        "WHERE source_path=? AND source_root=? AND media_type=?",
        (source_path, source_root, media_type),
    ).fetchone()
    return bool(
        row
        and row["size"] == size
        and row["mtime_ns"] == mtime_ns
        and row["md5"]
        and row["status"] != "stale"
    )
