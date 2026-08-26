from __future__ import annotations

import html
from collections import Counter
from pathlib import Path


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


def render(connection, output: Path, media_type: str = "image") -> None:
    rows = connection.execute(
        "SELECT * FROM images WHERE status != 'stale' AND media_type=?", (media_type,)
    ).fetchall()
    organized = [row for row in rows if row["status"] == "organized"]
    formats = Counter(row["extension"] for row in organized)
    dates = Counter((row["exif_date"] or "unsorted")[:7] for row in organized)
    folders = sorted(
        {str(Path(row["destination_path"]).parent) for row in organized if row["destination_path"]}
    )
    format_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{value}</td></tr>"
        for key, value in sorted(formats.items())
    )
    date_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{value}</td></tr>"
        for key, value in sorted(dates.items())
    )
    folder_rows = "".join(
        f'<li><a href="{html.escape(Path(folder).as_uri())}">{html.escape(folder)}</a></li>'
        for folder in folders
    )
    label = "Videos" if media_type == "video" else "Images"
    video_summary = (
        f" · Total duration: {_format_duration(sum(row['duration'] or 0 for row in organized))}"
        if media_type == "video"
        else ""
    )
    body = f"""<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>picsort report</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto}}table{{border-collapse:collapse}}td,th{{padding:.4rem 1rem;border:1px solid #ccc}}</style>
<h1>picsort library report</h1><p>{label} indexed: {len(rows)} · Organized: {len(organized)} · Duplicates: {sum(row["status"] == "duplicate" for row in rows)} · Errors: {sum(row["status"] == "error" for row in rows)}{video_summary}</p>
<h2>Formats</h2><table><tr><th>Format</th><th>{label}</th></tr>{format_rows}</table>
<h2>Embedded capture dates</h2><table><tr><th>Month</th><th>{label}</th></tr>{date_rows}</table>
<h2>Destination folders</h2><ul>{folder_rows}</ul></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
