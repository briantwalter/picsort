from __future__ import annotations

import html
import os
from collections import Counter
from pathlib import Path
from urllib.parse import quote


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


def _date_histogram(dates: Counter) -> str:
    if not dates:
        return '<p class="empty-histogram">No organized files with capture dates.</p>'
    maximum = max(dates.values())
    rows = "".join(
        '<li class="histogram-row">'
        f'<span class="histogram-label">{html.escape(year)}</span>'
        '<span class="histogram-track">'
        f'<span class="histogram-bar" style="width:{count / maximum * 100:.2f}%"></span>'
        "</span>"
        f'<span class="histogram-count">{count}</span>'
        "</li>"
        for year, count in sorted(dates.items())
    )
    return f'<ul class="date-histogram" aria-label="Embedded capture dates by year">{rows}</ul>'


def _capture_year(exif_date: str | None) -> str:
    if not exif_date or exif_date[:4] in {"0000", "1969", "1970"}:
        return "unsorted"
    return exif_date[:4]


def _existing_destination(path: str | None, destination: Path) -> bool:
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(destination.resolve())
        return resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _folder_link(folder: Path, output: Path, destination: Path) -> str:
    href = quote(os.path.relpath(folder, output.parent), safe="/")
    label = folder.relative_to(destination).as_posix()
    return f'<li><a href="{html.escape(href)}">{html.escape(label)}</a></li>'


def render(
    connection,
    output: Path,
    media_type: str = "image",
    destination: Path | None = None,
) -> None:
    destination = destination or output.parent
    rows = connection.execute(
        "SELECT * FROM images WHERE status != 'stale' AND media_type=?", (media_type,)
    ).fetchall()
    organized = [
        row
        for row in rows
        if row["status"] == "organized"
        and _existing_destination(row["destination_path"], destination)
    ]
    formats = Counter(row["extension"] for row in organized)
    dates = Counter(_capture_year(row["exif_date"]) for row in organized)
    folders = sorted({Path(row["destination_path"]).parent for row in organized})
    format_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{value}</td></tr>"
        for key, value in sorted(formats.items())
    )
    date_histogram = _date_histogram(dates)
    folder_rows = "".join(_folder_link(folder, output, destination) for folder in folders)
    label = "Videos" if media_type == "video" else "Images"
    video_summary = (
        f" · Total duration: {_format_duration(sum(row['duration'] or 0 for row in organized))}"
        if media_type == "video"
        else ""
    )
    body = f"""<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>picsort report</title>
<style>
body{{font:16px system-ui;max-width:900px;margin:2rem auto}}
table{{border-collapse:collapse}}td,th{{padding:.4rem 1rem;border:1px solid #ccc}}
.date-histogram{{list-style:none;padding:0;display:grid;gap:.5rem}}
.histogram-row{{display:grid;grid-template-columns:6rem minmax(8rem,1fr) 4rem;gap:.75rem;align-items:center}}
.histogram-label{{font-variant-numeric:tabular-nums}}
.histogram-track{{height:1.25rem;background:#eee;border-radius:.2rem;overflow:hidden}}
.histogram-bar{{display:block;height:100%;background:#4677b5}}
.histogram-count{{font-variant-numeric:tabular-nums;text-align:right}}
</style>
<h1>picsort library report</h1><p>{label} indexed: {len(rows)} · Organized: {len(organized)} · Duplicates: {sum(row["status"] == "duplicate" for row in rows)} · Errors: {sum(row["status"] == "error" for row in rows)}{video_summary}</p>
<h2>Formats</h2><table><tr><th>Format</th><th>{label}</th></tr>{format_rows}</table>
<h2>Embedded capture dates by year</h2>{date_histogram}
<h2>Destination folders</h2><ul>{folder_rows}</ul></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
