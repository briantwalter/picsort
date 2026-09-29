import json
import struct
from pathlib import Path

import pytest

from picsort.images import _base_result, inspect_video, md5_file
from picsort.index import is_unchanged, open_index, upsert_image
from picsort.organize import _copy_video, organize
from picsort.report import render
from picsort.video_references import has_external_references
from picsort.video_repair import repair_videos


def atom(kind, data=b"", extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(data) + 16) + data
    return struct.pack(">I4s", len(data) + 8, kind) + data


def movie(flags=(1,), kind=b"url ", extended=False):
    entries = b"".join(atom(kind, struct.pack(">I", flag) + b"target\0") for flag in flags)
    data = atom(b"dref", struct.pack(">II", 0, len(flags)) + entries)
    for container in (b"dinf", b"minf", b"mdia", b"trak", b"moov"):
        data = atom(container, data, extended=extended)
    return data + atom(b"mdat", b"local media")


def indexed(db, path, *, status="ready", destination=None, version=None):
    row = _base_result(path, path.parent, "video")
    row.update(
        md5=md5_file(path),
        status=status,
        destination_path=str(destination) if destination else None,
        video_inspection_version=version,
    )
    upsert_image(db, row)
    db.commit()
    return row


@pytest.mark.parametrize(
    "flags,kind,expected",
    [
        ((1,), b"url ", False),
        ((0,), b"url ", True),
        ((0,), b"alis", True),
        ((1, 0), b"url ", True),
    ],
)
@pytest.mark.parametrize("extended", [False, True])
def test_reference_structure(tmp_path, flags, kind, expected, extended):
    path = tmp_path / "ordinary-name.mov"
    path.write_bytes(movie(flags, kind, extended))
    assert has_external_references(path) is expected


@pytest.mark.parametrize(
    "data",
    [
        b"broken",
        atom(b"moov", b"bad"),
        atom(b"moov", atom(b"cmov")),
        struct.pack(">I4s", 100, b"moov"),
        atom(b"moov", atom(b"dref", struct.pack(">II", 0, 2))),
    ],
)
def test_invalid_structure_is_error(tmp_path, data):
    path = tmp_path / "bad.mov"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        has_external_references(path)


def test_reference_movie_atom(tmp_path):
    path = tmp_path / "reference.mov"
    path.write_bytes(
        atom(
            b"moov",
            atom(
                b"rmra",
                atom(b"rmda", atom(b"rdrf", struct.pack(">I4sI", 0, b"url ", 4) + b"abc\0")),
            ),
        )
    )
    assert has_external_references(path)


def test_discovery_exclusion_survives_existing_organized_status(tmp_path):
    path = tmp_path / "clip.mov"
    path.write_bytes(movie((0,)))
    db = open_index(tmp_path / "idx.sqlite")
    indexed(db, path, status="organized", destination=path)
    assert not is_unchanged(
        db, str(path), str(tmp_path), path.stat().st_size, path.stat().st_mtime_ns, "video"
    )
    inspected = inspect_video(path, tmp_path)
    upsert_image(db, inspected)
    row = db.execute("SELECT * FROM images").fetchone()
    assert row["status"] == "excluded"
    assert row["exclusion_reason"] == "external_reference"
    assert row["destination_path"] == str(path)
    assert is_unchanged(
        db, str(path), str(tmp_path), path.stat().st_size, path.stat().st_mtime_ns, "video"
    )


def test_exact_group_dates_extensions_and_rerun(tmp_path):
    db = open_index(tmp_path / "idx.sqlite")
    for name, date in [("a.mov", "2004-01-01"), ("b.mp4", "2005-01-01")]:
        path = tmp_path / name
        path.write_bytes(movie())
        row = indexed(db, path)
        row["exif_date"] = date
        upsert_image(db, row)
    db.commit()
    library = tmp_path / "library"
    preview = organize(db, library, media_type="video", dry_run=True)
    assert preview["copied"] == preview["duplicates"] == 1
    assert not library.exists()
    result = organize(db, library, media_type="video", workers=4)
    assert result["copied"] == result["duplicates"] == 1
    rows = db.execute("SELECT status,destination_path FROM images ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["organized", "duplicate"]
    assert len({r["destination_path"] for r in rows}) == 1
    assert Path(rows[0]["destination_path"]).read_bytes() == movie()
    assert organize(db, library, media_type="video")["copied"] == 0


def test_legacy_organized_group_reconciles_using_destination(tmp_path):
    db = open_index(tmp_path / "idx.sqlite")
    library = tmp_path / "library"
    library.mkdir()
    output = library / "existing.mov"
    output.write_bytes(movie())
    for name in ["a.mov", "b.mov"]:
        source = tmp_path / name
        source.write_bytes(movie())
        indexed(db, source, status="organized", destination=output)
        source.unlink()
    result = organize(db, library, media_type="video")
    assert result["duplicates"] == 1
    assert result["copied"] == 0
    assert [r[0] for r in db.execute("SELECT status FROM images ORDER BY id")] == [
        "organized",
        "duplicate",
    ]


def test_organize_excludes_old_reference_and_isolates_missing(tmp_path):
    db = open_index(tmp_path / "idx.sqlite")
    path = tmp_path / "ref.mov"
    path.write_bytes(movie((0,)))
    indexed(db, path)
    absent = tmp_path / "absent.mov"
    absent.write_bytes(movie())
    indexed(db, absent)
    absent.unlink()
    result = organize(db, tmp_path / "library", media_type="video")
    assert result["copied"] == 0
    assert result["errors"] == 1
    assert [r[0] for r in db.execute("SELECT status FROM images ORDER BY id")] == [
        "excluded",
        "error",
    ]


def test_copy_never_replaces_target(tmp_path):
    source = tmp_path / "source.mov"
    target = tmp_path / "target.mov"
    source.write_bytes(movie())
    target.write_bytes(b"keep")
    with pytest.raises(ValueError):
        _copy_video(str(source), target)
    assert target.read_bytes() == b"keep"


def setup_library(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    index = tmp_path / "idx.sqlite"
    db = open_index(index)
    output = library / "preview.mov"
    output.write_bytes(movie((0,), b"alis"))
    for name in ["one.mov", "two.mov"]:
        source = tmp_path / name
        source.write_bytes(output.read_bytes())
        indexed(db, source, status="organized", destination=output)
    original = library / "original.mov"
    original.write_bytes(movie())
    indexed(db, original, status="organized", destination=original)
    db.close()
    return index, library, output


def test_repair_preview_apply_repeat_and_report(tmp_path):
    index, library, output = setup_library(tmp_path)
    before = index.read_bytes()
    preview = repair_videos(index, library)
    assert preview["references"] == 1
    assert preview["errors"] == []
    assert index.read_bytes() == before
    assert sorted(p.name for p in library.iterdir()) == ["original.mov", "preview.mov"]
    result = repair_videos(index, library, apply=True)
    assert result["quarantined"] == 1
    assert result["errors"] == []
    assert Path(result["backup"]).exists()
    assert not output.exists()
    quarantined = library / "deprecated/reference-videos/preview.mov"
    assert quarantined.read_bytes() == movie((0,), b"alis")
    db = open_index(index, readonly=True)
    rows = db.execute("SELECT status,destination_path FROM images ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["excluded", "excluded", "organized"]
    assert rows[0]["destination_path"] == rows[1]["destination_path"] == str(quarantined)
    assert "Organized: 1" in (library / "index.html").read_text()
    assert "Excluded references: 2" in (library / "index.html").read_text()
    assert repair_videos(index, library, apply=True)["quarantined"] == 0


@pytest.mark.parametrize("problem", ["hash", "collision", "symlink", "missing"])
def test_repair_leaves_unsafe_candidates_untouched(tmp_path, problem):
    index, library, output = setup_library(tmp_path)
    target = library / "deprecated/reference-videos/preview.mov"
    if problem == "hash":
        output.write_bytes(movie((0,), b"url "))
    elif problem == "collision":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"keep")
    elif problem == "symlink":
        output.unlink()
        output.symlink_to(tmp_path / "one.mov")
    else:
        output.unlink()
    result = repair_videos(index, library, apply=True)
    assert result["errors"]
    assert result["quarantined"] == 0
    assert (tmp_path / "one.mov").read_bytes() == movie((0,), b"alis")
    if problem == "collision":
        assert target.read_bytes() == b"keep"


def test_repair_resumes_move_before_index_commit(tmp_path):
    index, library, output = setup_library(tmp_path)
    preview = repair_videos(index, library)
    move = preview["moves"][0]
    manifest = {"index": str(index), "destination": str(library), "moves": [move]}
    (library / ".picsort-reference-repair.json").write_text(json.dumps(manifest))
    target = Path(move["target"])
    target.parent.mkdir(parents=True)
    output.rename(target)
    result = repair_videos(index, library, apply=True)
    assert result["quarantined"] == 1
    assert result["errors"] == []


def test_report_counts_shared_destination_once(tmp_path):
    index, library, _output = setup_library(tmp_path)
    db = open_index(index, readonly=True)
    render(db, library / "report.html", "video", library)
    assert "Organized: 2" in (library / "report.html").read_text()


def test_readonly_old_schema_does_not_migrate(tmp_path):
    index = tmp_path / "idx.sqlite"
    db = open_index(index)
    db.execute("ALTER TABLE images DROP COLUMN video_inspection_version")
    db.execute("ALTER TABLE images DROP COLUMN exclusion_reason")
    db.commit()
    db.close()
    before = index.read_bytes()
    library = tmp_path / "library"
    library.mkdir()
    assert repair_videos(index, library)["scanned"] == 0
    assert index.read_bytes() == before
    db = open_index(index)
    assert {"video_inspection_version", "exclusion_reason"} <= {
        row["name"] for row in db.execute("PRAGMA table_info(images)")
    }


def test_repair_resumes_recorded_partial_copy(tmp_path):
    index, library, output = setup_library(tmp_path)
    move = repair_videos(index, library)["moves"][0]
    target = Path(move["target"])
    target.parent.mkdir(parents=True)
    target.write_bytes(output.read_bytes()[:12])
    move["copying_identity"] = [target.stat().st_dev, target.stat().st_ino]
    (library / ".picsort-reference-repair.json").write_text(
        json.dumps({"index": str(index), "destination": str(library), "moves": [move]})
    )
    result = repair_videos(index, library, apply=True)
    assert result["errors"] == []
    assert result["quarantined"] == 1
    assert target.read_bytes() == movie((0,), b"alis")


def test_repair_reports_multiple_destinations_without_moving(tmp_path):
    index, library, _output = setup_library(tmp_path)
    another = library / "another.mov"
    another.write_bytes(movie())
    db = open_index(index)
    indexed(db, another, status="organized", destination=another)
    db.close()
    result = repair_videos(index, library, apply=True)
    assert result["multiple_destinations"] == 1
    assert another.exists()
    assert (library / "original.mov").exists()


def test_organize_collision_dry_run_and_apply(tmp_path):
    db = open_index(tmp_path / "idx.sqlite")
    source = tmp_path / "source.mov"
    source.write_bytes(movie())
    row = indexed(db, source)
    library = tmp_path / "library"
    target = library / "unsorted" / f"0000-00-00-{row['md5']}.mov"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep")
    for dry_run in [True, False]:
        result = organize(db, library, media_type="video", dry_run=dry_run)
        assert result["errors"] == 1
        assert result["copied"] == 0
        assert target.read_bytes() == b"keep"


def test_source_root_scope_keeps_other_rows_unchanged(tmp_path):
    db = open_index(tmp_path / "idx.sqlite")
    for root in [tmp_path / "a", tmp_path / "b"]:
        root.mkdir()
        path = root / "clip.mov"
        path.write_bytes(movie())
        indexed(db, path)
    organize(db, tmp_path / "library", media_type="video", source_roots=[tmp_path / "a"])
    assert [row[0] for row in db.execute("SELECT status FROM images ORDER BY id")] == [
        "organized",
        "ready",
    ]


def test_real_self_contained_video_in_preview_directory(tmp_path):
    av = pytest.importorskip("av")
    from PIL import Image

    previews = tmp_path / "Previews"
    previews.mkdir()
    path = previews / "IMG_0001.mp4"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        for _ in range(2):
            frame = av.VideoFrame.from_image(Image.new("RGB", (32, 32), "red"))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    inspected = inspect_video(path, tmp_path)
    assert inspected["status"] == "ready"
    assert inspected["exclusion_reason"] is None
    assert inspected["width"] == inspected["height"] == 32
    assert inspected["video_inspection_version"] == 1


def test_cli_preview_does_not_change_index(tmp_path, monkeypatch, capsys):
    from picsort.cli import main

    index, library, _output = setup_library(tmp_path)
    before = index.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        ["picsort", "repair-videos", "--index", str(index), "--destination", str(library)],
    )
    main()
    output = capsys.readouterr().out
    assert "Video repair preview: scanned=2 references=1 quarantined=0" in output
    assert "Preview only; use --apply" in output
    assert '"moves"' not in output
    assert index.read_bytes() == before


def test_repair_cli_verbose_and_errors(tmp_path, monkeypatch, capsys):
    from picsort.cli import main

    index, library, _output = setup_library(tmp_path)
    (library / "original.mov").unlink()
    monkeypatch.setattr(
        "sys.argv",
        [
            "picsort",
            "repair-videos",
            "--index",
            str(index),
            "--destination",
            str(library),
            "--verbose",
        ],
    )
    main()
    captured = capsys.readouterr()
    assert "errors=1" in captured.out
    assert "reference: " in captured.out
    assert "deprecated/reference-videos/preview.mov" in captured.out
    assert "file error:" in captured.err
    assert "Indexed destination is missing" in captured.err


def test_renamed_appledouble_has_clear_diagnostic(tmp_path):
    path = tmp_path / "0000-00-00-legacy.mov"
    path.write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00" + bytes(4088))
    with pytest.raises(ValueError, match="AppleDouble metadata sidecar, not a video"):
        has_external_references(path)


def compressed_movie(data, expected=None):
    import zlib

    payload = struct.pack(">I", len(data) if expected is None else expected) + zlib.compress(data)
    return atom(b"moov", atom(b"cmov", atom(b"dcom", b"zlib") + atom(b"cmvd", payload)))


@pytest.mark.parametrize("external", [False, True])
def test_compressed_movie_references(tmp_path, external):
    path = tmp_path / "compressed.mov"
    path.write_bytes(compressed_movie(movie((0 if external else 1,))))
    assert has_external_references(path) is external


@pytest.mark.parametrize("expected", [1, 100, 17 * 1024 * 1024])
def test_compressed_metadata_enforces_size(tmp_path, expected):
    path = tmp_path / "compressed.mov"
    path.write_bytes(compressed_movie(movie(), expected))
    with pytest.raises(ValueError):
        has_external_references(path)


def test_empty_movie_diagnostic(tmp_path):
    path = tmp_path / "empty.mov"
    path.touch()
    with pytest.raises(ValueError, match="Empty file"):
        has_external_references(path)


def test_repair_reports_truncated_destination(tmp_path):
    index, library, _output = setup_library(tmp_path)
    (library / "original.mov").write_bytes(b"")
    result = repair_videos(index, library)
    assert any("actual=0 bytes" in error and "indexed=" in error for error in result["errors"])
    assert (library / "original.mov").read_bytes() == b""
