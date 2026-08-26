from pathlib import Path

from PIL import Image, ImageOps

from picsort.cli import main
from picsort.images import inspect_image
from picsort.index import open_index
from picsort.organize import date_parts, organize


def make_image(path: Path, size=(20, 20), color="red", date=None):
    image = Image.new("RGB", size, color)
    if date:
        image.getexif()[36867] = date
    image.save(path, format="JPEG", exif=image.getexif().tobytes())


def test_discover_organize_and_report(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    destination = tmp_path / "library"
    source.mkdir()
    make_image(source / "best.jpg", size=(40, 40), date="2024:03:04 12:00:00")
    make_image(source / "copy.jpeg", size=(20, 20), date="2024:03:04 12:00:00")
    (source / "thumb.jpg").write_bytes((source / "copy.jpeg").read_bytes())
    index = tmp_path / "index.sqlite"

    monkeypatch.setattr(
        "sys.argv", ["picsort", "discover", str(source), "--index", str(index), "--workers", "2"]
    )
    main()
    main()
    assert "unchanged=2" in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "organize", "--index", str(index), "--destination", str(destination)],
    )
    main()
    output = destination / "index.html"
    monkeypatch.setattr(
        "sys.argv", ["picsort", "report", "--index", str(index), "--output", str(output)]
    )
    main()

    selected = destination / "2024"
    assert len(list(selected.glob("*.jpg"))) == 1
    assert output.exists()
    assert "Organized: 1" in output.read_text()
    indexed_paths = [row[0] for row in open_index(index).execute("SELECT source_path FROM images")]
    assert all("thumb" not in path for path in indexed_paths)


def test_organize_is_idempotent(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "library"
    source.mkdir()
    make_image(source / "photo.jpg", date="2024:01:02 12:00:00")
    index = open_index(tmp_path / "index.sqlite")
    from picsort.index import upsert_image

    upsert_image(index, inspect_image(source / "photo.jpg", source))
    index.commit()
    first = organize(index, destination)
    second = organize(index, destination)
    assert first["copied"] == 1
    assert second["copied"] == 0
    assert second["skipped"] == 0


def test_epoch_dates_are_unsorted():
    assert date_parts("1969-12-31") == ("unsorted", "0000-00-00")
    assert date_parts("1970-01-01") == ("unsorted", "0000-00-00")
    assert date_parts(None) == ("unsorted", "0000-00-00")
    assert date_parts("2024-03-04") == ("2024", "2024-03-04")


def test_perceptual_hash_normalizes_exif_orientation(tmp_path):
    original = Image.new("RGB", (40, 60), "blue")
    exif = original.getexif()
    exif[274] = 6
    high = tmp_path / "high.jpg"
    original.save(high, exif=exif.tobytes())
    thumbnail = tmp_path / "thumbnail.jpg"
    ImageOps.exif_transpose(original).resize((6, 4)).save(thumbnail)

    high_info = inspect_image(high, tmp_path)
    thumbnail_info = inspect_image(thumbnail, tmp_path)
    assert (high_info["width"], high_info["height"]) == (60, 40)
    assert (thumbnail_info["width"], thumbnail_info["height"]) == (6, 4)
    assert high_info["phash"] == thumbnail_info["phash"]


def test_video_discovery_is_exclusive(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    make_image(source / "photo.jpg")
    (source / "clip.mp4").write_bytes(b"not a real video")
    index = tmp_path / "index.sqlite"
    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "discover", str(source), "--videos", "--index", str(index)],
    )
    main()
    connection = open_index(index)
    rows = connection.execute("SELECT media_type, source_path, status FROM images").fetchall()
    assert len(rows) == 1
    assert rows[0]["media_type"] == "video"
    assert rows[0]["source_path"].endswith("clip.mp4")
    assert rows[0]["status"] == "error"


def test_video_organize_uses_md5_and_embedded_date(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video bytes")
    index = open_index(tmp_path / "index.sqlite")
    from picsort.index import upsert_image

    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "a" * 32,
            "media_type": "video",
            "extension": "mp4",
            "width": 1920,
            "height": 1080,
            "bitrate": 1000,
            "frame_rate": 30,
            "duration": 4,
            "codec": "h264",
            "exif_date": "2024-03-04",
            "status": "ready",
        },
    )
    index.commit()
    result = organize(index, tmp_path / "library", media_type="video")
    assert result["copied"] == 1
    assert (tmp_path / "library" / "2024" / ("2024-03-04-" + "a" * 32 + ".mp4")).exists()


def test_pending_high_resolution_wins_over_organized_thumbnail(tmp_path):
    thumbnail = tmp_path / "thumbnail.jpg"
    high = tmp_path / "high.jpg"
    make_image(thumbnail, size=(270, 360), date="2024:03:04 12:00:00")
    make_image(high, size=(2448, 3264), date="2024:03:04 12:00:00")
    index = open_index(tmp_path / "index.sqlite")
    from picsort.index import upsert_image

    thumbnail_info = inspect_image(thumbnail, tmp_path)
    thumbnail_info["status"] = "organized"
    thumbnail_info["destination_path"] = str(tmp_path / "library" / "2024" / "thumbnail.jpg")
    high_info = inspect_image(high, tmp_path)
    high_info["phash"] = thumbnail_info["phash"]
    high_info["status"] = "duplicate"
    upsert_image(index, thumbnail_info)
    upsert_image(index, high_info)
    index.commit()

    result = organize(index, tmp_path / "library")
    assert result["copied"] == 1
    assert any(
        path.name.startswith("2024-03-04-")
        for path in (tmp_path / "library" / "2024").glob("*.jpg")
    )


def test_source_list_indexes_multiple_directories(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    make_image(first / "one.jpg")
    make_image(second / "two.jpg")
    source_list = tmp_path / "sources.txt"
    source_list.write_text(f"{first}\n# ignored\n\n{second}\n", encoding="utf-8")
    index = tmp_path / "index.sqlite"
    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "discover", "--source-list", str(source_list), "--index", str(index)],
    )
    main()
    connection = open_index(index)
    rows = connection.execute("SELECT source_root FROM images ORDER BY source_root").fetchall()
    assert [row[0] for row in rows] == [str(first.resolve()), str(second.resolve())]
