import os
from pathlib import Path

import pytest
from PIL import Image, ImageOps

from picsort.cli import main
from picsort.images import inspect_image, normalized_extension
from picsort.index import open_index, upsert_image
from picsort.organize import date_parts, organize
from picsort.report import render


def make_image(path: Path, size=(20, 20), color="red", date=None):
    image = Image.new("RGB", size, color)
    if date:
        image.getexif()[36867] = date
    image.save(path, format="JPEG", exif=image.getexif().tobytes())


def add_index_row(connection, source_path: Path, source_root: Path, status="ready"):
    upsert_image(
        connection,
        {
            "source_path": str(source_path),
            "source_root": str(source_root),
            "size": 1,
            "mtime_ns": 1,
            "media_type": "image",
            "status": status,
        },
    )
    connection.commit()


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
    assert len(list(selected.glob("*.jpeg"))) == 1
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
    assert date_parts("0000-01-01") == ("unsorted", "0000-00-00")
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


def test_heic_image_is_inspected(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    source = tmp_path / "photo.heic"
    pillow_heif.from_pillow(Image.new("RGB", (48, 32), "green")).save(source)

    info = inspect_image(source, tmp_path)

    assert (info["width"], info["height"]) == (48, 32)
    assert info["extension"] == "heic"
    assert info["md5"]
    assert info["phash"]
    assert info["status"] == "ready"


def test_heic_without_optional_dependency_has_clear_error(tmp_path, monkeypatch):
    source = tmp_path / "photo.heic"
    source.write_bytes(b"not decoded")
    original_import = __import__("importlib").import_module

    def import_without_heif(name):
        if name == "pillow_heif":
            raise ModuleNotFoundError(name)
        return original_import(name)

    monkeypatch.setattr("picsort.images._HEIF_REGISTERED", False)
    monkeypatch.setattr("picsort.images.importlib.import_module", import_without_heif)

    with pytest.raises(RuntimeError, match="optional 'heic' dependency"):
        inspect_image(source, tmp_path)


def test_heic_extension_is_not_truncated():
    assert normalized_extension(Path("photo.heic")) == "heic"


def test_jpeg_and_tiff_use_canonical_extensions():
    assert normalized_extension(Path("photo.jpg")) == "jpeg"
    assert normalized_extension(Path("photo.jpeg")) == "jpeg"
    assert normalized_extension(Path("scan.tif")) == "tiff"
    assert normalized_extension(Path("scan.tiff")) == "tiff"


@pytest.mark.parametrize(
    ("source_suffix", "legacy_extension", "canonical_extension"),
    [("jpg", "jpg", "jpeg"), ("jpeg", "jpg", "jpeg"), ("tif", "tif", "tiff")],
)
def test_organize_renames_legacy_image_extensions(
    tmp_path, source_suffix, legacy_extension, canonical_extension
):
    source = tmp_path / f"photo.{source_suffix}"
    source.write_bytes(b"image source")
    destination = tmp_path / "library"
    old_output = destination / "2024" / f"photo.{legacy_extension}"
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"organized image")
    index = open_index(tmp_path / "index.sqlite")
    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "e" * 32,
            "extension": legacy_extension,
            "media_type": "image",
            "status": "organized",
            "destination_path": str(old_output),
        },
    )
    index.commit()

    result = organize(index, destination)

    new_output = old_output.with_suffix(f".{canonical_extension}")
    assert result["renamed"] == 1
    assert not old_output.exists()
    assert new_output.read_bytes() == b"organized image"
    row = index.execute("SELECT extension, destination_path FROM images").fetchone()
    assert tuple(row) == (canonical_extension, str(new_output))


def test_organize_renames_existing_heic_destination(tmp_path):
    source = tmp_path / "photo.heic"
    source.write_bytes(b"heic source")
    destination = tmp_path / "library"
    old_output = destination / "2024" / "photo.hei"
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"organized heic")
    index = open_index(tmp_path / "index.sqlite")
    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "a" * 32,
            "phash": "0" * 16,
            "extension": "hei",
            "media_type": "image",
            "width": 20,
            "height": 20,
            "status": "organized",
            "destination_path": str(old_output),
        },
    )
    index.commit()

    result = organize(index, destination)

    new_output = old_output.with_suffix(".heic")
    assert result["renamed"] == 1
    assert not old_output.exists()
    assert new_output.read_bytes() == b"organized heic"
    row = index.execute("SELECT extension, destination_path FROM images").fetchone()
    assert tuple(row) == ("heic", str(new_output))
    assert organize(index, destination)["renamed"] == 0


def test_organize_uses_heic_for_legacy_pending_row(tmp_path):
    source = tmp_path / "photo.heic"
    source.write_bytes(b"heic source")
    destination = tmp_path / "library"
    index = open_index(tmp_path / "index.sqlite")
    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "d" * 32,
            "extension": "hei",
            "media_type": "image",
            "exif_date": "2024-01-02",
            "status": "ready",
        },
    )
    index.commit()

    result = organize(index, destination)

    output = destination / "2024" / f"2024-01-02-{'d' * 32}.heic"
    assert result["copied"] == 1
    assert output.read_bytes() == source.read_bytes()
    row = index.execute("SELECT extension, destination_path FROM images").fetchone()
    assert tuple(row) == ("heic", str(output))


def test_heic_rename_dry_run_does_not_change_file_or_index(tmp_path):
    source = tmp_path / "photo.heic"
    source.write_bytes(b"heic source")
    destination = tmp_path / "library"
    old_output = destination / "deprecated" / "2024" / "photo.hei"
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"deprecated heic")
    index = open_index(tmp_path / "index.sqlite")
    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "b" * 32,
            "phash": "1" * 16,
            "extension": "hei",
            "media_type": "image",
            "width": 10,
            "height": 10,
            "status": "duplicate",
            "destination_path": str(old_output),
        },
    )
    index.commit()

    result = organize(index, destination, dry_run=True)

    assert result["renamed"] == 1
    assert old_output.exists()
    assert not old_output.with_suffix(".heic").exists()
    row = index.execute("SELECT extension, destination_path FROM images").fetchone()
    assert tuple(row) == ("hei", str(old_output))


def test_heic_rename_does_not_overwrite_existing_target(tmp_path):
    source = tmp_path / "photo.heic"
    source.write_bytes(b"heic source")
    destination = tmp_path / "library"
    old_output = destination / "2024" / "photo.hei"
    new_output = old_output.with_suffix(".heic")
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"old")
    new_output.write_bytes(b"new")
    index = open_index(tmp_path / "index.sqlite")
    upsert_image(
        index,
        {
            "source_path": str(source),
            "source_root": str(tmp_path),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "md5": "c" * 32,
            "extension": "hei",
            "media_type": "image",
            "status": "organized",
            "destination_path": str(old_output),
        },
    )
    index.commit()

    result = organize(index, destination)

    assert result["renamed"] == 0
    assert result["errors"] == 1
    assert old_output.read_bytes() == b"old"
    assert new_output.read_bytes() == b"new"
    row = index.execute("SELECT extension, destination_path FROM images").fetchone()
    assert tuple(row) == ("hei", str(old_output))


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
    organized_thumbnail = tmp_path / "library" / "2024" / "thumbnail.jpg"
    organized_thumbnail.parent.mkdir(parents=True)
    organized_thumbnail.write_bytes(thumbnail.read_bytes())
    thumbnail_info["destination_path"] = str(organized_thumbnail)
    high_info = inspect_image(high, tmp_path)
    high_info["phash"] = thumbnail_info["phash"]
    high_info["status"] = "duplicate"
    upsert_image(index, thumbnail_info)
    upsert_image(index, high_info)
    index.commit()

    result = organize(index, tmp_path / "library")
    assert result["copied"] == 1
    assert result["deprecated"] == 1
    deprecated_thumbnail = tmp_path / "library" / "deprecated" / "2024" / "thumbnail.jpeg"
    assert not organized_thumbnail.exists()
    assert deprecated_thumbnail.read_bytes() == thumbnail.read_bytes()
    thumbnail_row = index.execute(
        "SELECT status, destination_path FROM images WHERE source_path=?",
        (str(thumbnail),),
    ).fetchone()
    assert thumbnail_row["status"] == "duplicate"
    assert thumbnail_row["destination_path"] == str(deprecated_thumbnail)
    assert any(
        path.name.startswith("2024-03-04-")
        for path in (tmp_path / "library" / "2024").glob("*.jpeg")
    )


def test_equal_resolution_does_not_deprecate_organized_image(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    make_image(first, size=(40, 40), color="red")
    make_image(second, size=(40, 40), color="blue")
    index = open_index(tmp_path / "index.sqlite")
    first_info = inspect_image(first, tmp_path)
    organized = tmp_path / "library" / "unsorted" / "first.jpeg"
    organized.parent.mkdir(parents=True)
    organized.write_bytes(first.read_bytes())
    first_info.update(status="organized", destination_path=str(organized))
    second_info = inspect_image(second, tmp_path)
    second_info.update(status="ready", phash=first_info["phash"])
    upsert_image(index, first_info)
    upsert_image(index, second_info)
    index.commit()

    result = organize(index, tmp_path / "library")

    assert result["deprecated"] == 0
    assert organized.exists()
    assert not (tmp_path / "library" / "deprecated").exists()


def test_deprecation_dry_run_does_not_change_file_or_index(tmp_path):
    thumbnail = tmp_path / "thumbnail.jpg"
    high = tmp_path / "high.jpg"
    make_image(thumbnail, size=(10, 10))
    make_image(high, size=(100, 100))
    index = open_index(tmp_path / "index.sqlite")
    thumbnail_info = inspect_image(thumbnail, tmp_path)
    organized = tmp_path / "library" / "unsorted" / "thumbnail.jpeg"
    organized.parent.mkdir(parents=True)
    organized.write_bytes(thumbnail.read_bytes())
    thumbnail_info.update(status="organized", destination_path=str(organized))
    high_info = inspect_image(high, tmp_path)
    high_info.update(status="ready", phash=thumbnail_info["phash"])
    upsert_image(index, thumbnail_info)
    upsert_image(index, high_info)
    index.commit()

    result = organize(index, tmp_path / "library", dry_run=True)

    assert result["deprecated"] == 1
    assert organized.exists()
    assert not (tmp_path / "library" / "deprecated").exists()
    row = index.execute(
        "SELECT status, destination_path FROM images WHERE source_path=?",
        (str(thumbnail),),
    ).fetchone()
    assert tuple(row) == ("organized", str(organized))


def test_deprecation_does_not_overwrite_existing_file(tmp_path):
    thumbnail = tmp_path / "thumbnail.jpg"
    high = tmp_path / "high.jpg"
    make_image(thumbnail, size=(10, 10))
    make_image(high, size=(100, 100))
    index = open_index(tmp_path / "index.sqlite")
    thumbnail_info = inspect_image(thumbnail, tmp_path)
    organized = tmp_path / "library" / "2023" / "thumbnail.jpeg"
    organized.parent.mkdir(parents=True)
    organized.write_bytes(b"organized")
    target = tmp_path / "library" / "deprecated" / "2023" / "thumbnail.jpeg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    thumbnail_info.update(status="organized", destination_path=str(organized))
    high_info = inspect_image(high, tmp_path)
    high_info.update(status="ready", phash=thumbnail_info["phash"])
    upsert_image(index, thumbnail_info)
    upsert_image(index, high_info)
    index.commit()

    result = organize(index, tmp_path / "library")

    assert result["deprecated"] == 0
    assert result["errors"] == 1
    assert organized.read_bytes() == b"organized"
    assert target.read_bytes() == b"existing"
    row = index.execute(
        "SELECT status, destination_path FROM images WHERE source_path=?", (str(thumbnail),)
    ).fetchone()
    assert tuple(row) == ("organized", str(organized))


def test_shared_organized_destination_is_moved_once(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    high = tmp_path / "high.jpg"
    make_image(first, size=(10, 10))
    second.write_bytes(first.read_bytes())
    make_image(high, size=(100, 100))
    index = open_index(tmp_path / "index.sqlite")
    organized = tmp_path / "library" / "2022" / "thumbnail.jpeg"
    organized.parent.mkdir(parents=True)
    organized.write_bytes(first.read_bytes())
    first_info = inspect_image(first, tmp_path)
    first_info.update(status="organized", destination_path=str(organized))
    second_info = inspect_image(second, tmp_path)
    second_info.update(status="organized", destination_path=str(organized))
    high_info = inspect_image(high, tmp_path)
    high_info.update(status="ready", phash=first_info["phash"])
    for info in (first_info, second_info, high_info):
        upsert_image(index, info)
    index.commit()

    result = organize(index, tmp_path / "library")

    target = tmp_path / "library" / "deprecated" / "2022" / "thumbnail.jpeg"
    assert result["deprecated"] == 1
    assert target.exists()
    rows = index.execute(
        "SELECT status, destination_path FROM images WHERE source_path IN (?, ?) ",
        (str(first), str(second)),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("duplicate", str(target)),
        ("duplicate", str(target)),
    ]


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


def test_invalid_utf8_symlink_source_is_skipped(tmp_path, monkeypatch, capsys):
    valid = tmp_path / "valid"
    valid.mkdir()
    make_image(valid / "photo.jpg")
    invalid = tmp_path / "invalid"
    os.symlink(b"bad-\x98-target", os.fsencode(invalid))
    source_list = tmp_path / "sources.txt"
    source_list.write_text(f"{invalid}\n{valid}\n", encoding="utf-8")
    index = tmp_path / "index.sqlite"

    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "discover", "--source-list", str(source_list), "--index", str(index)],
    )
    main()

    output = capsys.readouterr()
    assert "source_errors=1" in output.out
    assert "source resolves to a non-UTF-8 path" in output.err
    rows = open_index(index).execute("SELECT source_root FROM images").fetchall()
    assert [row[0] for row in rows] == [str(valid.resolve())]


def test_all_invalid_sources_exit_without_modifying_index(tmp_path, monkeypatch, capsys):
    invalid = tmp_path / "invalid"
    os.symlink(b"bad-\x98-target", os.fsencode(invalid))
    index_path = tmp_path / "index.sqlite"
    connection = open_index(index_path)
    add_index_row(connection, tmp_path / "existing.jpg", tmp_path)
    connection.close()

    monkeypatch.setattr(
        "sys.argv", ["picsort", "discover", str(invalid), "--index", str(index_path)]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "No valid source directories" in capsys.readouterr().err
    row = open_index(index_path).execute("SELECT status FROM images").fetchone()
    assert row[0] == "ready"


def test_missing_source_does_not_mark_existing_rows_stale(tmp_path, monkeypatch, capsys):
    valid = tmp_path / "valid"
    valid.mkdir()
    missing = tmp_path / "missing"
    index_path = tmp_path / "index.sqlite"
    connection = open_index(index_path)
    add_index_row(connection, missing / "old.jpg", missing.resolve())
    connection.close()
    source_list = tmp_path / "sources.txt"
    source_list.write_text(f"{missing}\n{valid}\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "discover", "--source-list", str(source_list), "--index", str(index_path)],
    )
    main()

    assert "source_errors=1" in capsys.readouterr().out
    row = (
        open_index(index_path)
        .execute("SELECT status FROM images WHERE source_root=?", (str(missing.resolve()),))
        .fetchone()
    )
    assert row[0] == "ready"


def test_non_utf8_child_prevents_stale_marking(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()

    class InvalidEntry:
        path = f"{source}/bad-\udc98.jpg"

    class InvalidScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([InvalidEntry()])

    monkeypatch.setattr("picsort.cli.os.scandir", lambda _path: InvalidScandir())
    index_path = tmp_path / "index.sqlite"
    connection = open_index(index_path)
    add_index_row(connection, source / "old.jpg", source.resolve())
    connection.close()

    monkeypatch.setattr(
        "sys.argv", ["picsort", "discover", str(source), "--index", str(index_path)]
    )
    main()

    output = capsys.readouterr()
    assert "source_errors=1" in output.out
    assert "non-UTF-8 path skipped" in output.err
    row = open_index(index_path).execute("SELECT status FROM images").fetchone()
    assert row[0] == "ready"


def test_report_renders_capture_dates_as_histogram(tmp_path):
    connection = open_index(tmp_path / "index.sqlite")
    for number, date in enumerate(("2024-01-02", "2024-02-03", "2023-09-04", None)):
        source = tmp_path / f"photo-{number}.jpg"
        upsert_image(
            connection,
            {
                "source_path": str(source),
                "source_root": str(tmp_path),
                "size": number + 1,
                "mtime_ns": number + 1,
                "md5": f"{number:032x}",
                "extension": "jpg",
                "media_type": "image",
                "exif_date": date,
                "status": "organized",
                "destination_path": str(tmp_path / "library" / source.name),
            },
        )
    connection.commit()
    output = tmp_path / "report.html"

    render(connection, output)

    report = output.read_text(encoding="utf-8")
    assert '<ul class="date-histogram"' in report
    assert "Embedded capture dates by year" in report
    assert '<span class="histogram-label">2023</span>' in report
    assert '<span class="histogram-label">2024</span>' in report
    assert '<span class="histogram-label">unsorted</span>' in report
    assert 'style="width:100.00%"' in report
    assert report.count('style="width:50.00%"') == 2
    assert '<span class="histogram-count">2</span>' in report
    assert "2024-01" not in report
    assert "2024-02" not in report
    assert "<th>Month</th>" not in report


def test_report_capture_date_histogram_has_empty_state(tmp_path):
    connection = open_index(tmp_path / "index.sqlite")
    output = tmp_path / "report.html"

    render(connection, output)

    report = output.read_text(encoding="utf-8")
    assert "No organized files with capture dates." in report
    assert '<ul class="date-histogram"' not in report
