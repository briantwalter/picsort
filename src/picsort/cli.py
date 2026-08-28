from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .images import DEFAULT_IGNORE_PATTERNS, ignored_by_name, inspect_media, is_supported
from .index import is_unchanged, mark_stale, open_index, upsert_image
from .organize import organize
from .progress import Progress, ScanProgress
from .report import render


def _display_path(path: Path) -> str:
    return ascii(str(path))


def _valid_utf8_path(path: Path) -> bool:
    try:
        str(path).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _scan_paths(
    source: Path, media_type: str, patterns, on_progress=None
) -> tuple[set[str], list[str]]:
    paths = set()
    errors = []
    directories = entries = 0
    pending = [source]
    while pending:
        directory = pending.pop()
        directories += 1
        try:
            children = os.scandir(directory)
        except OSError as exc:
            errors.append(f"cannot scan {_display_path(directory)}: {exc}")
            continue
        with children:
            for entry in children:
                entries += 1
                path = Path(entry.path)
                if not _valid_utf8_path(path):
                    errors.append(f"non-UTF-8 path skipped: {_display_path(path)}")
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and is_supported(path, media_type)
                        and not ignored_by_name(path, patterns)
                    ):
                        paths.add(str(path.resolve()))
                except OSError as exc:
                    errors.append(f"cannot inspect {_display_path(path)}: {exc}")
                    continue
                if on_progress and entries % 100 == 0:
                    on_progress(directories, entries, len(paths))
    if on_progress:
        on_progress(directories, entries, len(paths))
    return paths, errors


def _input_sources(args) -> tuple[list[Path], list[str]]:
    if args.source_list:
        lines = (
            Path(args.source_list)
            .expanduser()
            .read_text(encoding="utf-8", errors="surrogateescape")
            .splitlines()
        )
        candidates = [
            Path(line.strip()).expanduser()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        candidates = [Path(args.source).expanduser()]

    sources = []
    errors = []
    for candidate in candidates:
        try:
            source = candidate.resolve()
        except OSError as exc:
            errors.append(f"cannot resolve {_display_path(candidate)}: {exc}")
            continue
        if not _valid_utf8_path(source):
            errors.append(
                f"source resolves to a non-UTF-8 path: {_display_path(candidate)} -> "
                f"{_display_path(source)}"
            )
            continue
        try:
            is_directory = source.is_dir()
        except OSError as exc:
            errors.append(f"cannot access {_display_path(source)}: {exc}")
            continue
        if not is_directory:
            errors.append(f"source is not a directory: {_display_path(source)}")
            continue
        sources.append(source)
    return sources, errors


def _discover(args) -> None:
    sources, source_errors = _input_sources(args)
    for error in source_errors:
        print(f"source error: {error}", file=sys.stderr)
    if not sources:
        print("No valid source directories.", file=sys.stderr)
        raise SystemExit(2)
    index_path = Path(args.index).expanduser().resolve()
    media_type = "video" if args.videos else "image"
    verbose = args.verbose and not args.quiet
    if verbose:
        noun = "directory" if len(sources) == 1 else "directories"
        print(f"Starting {media_type} discovery across {len(sources)} input {noun}")
    patterns = tuple(args.ignore_pattern or DEFAULT_IGNORE_PATTERNS)
    scanner = (
        ScanProgress("Scanning input directories") if args.spinner and not args.quiet else None
    )
    if scanner:
        scanner.start()
    try:
        paths_by_root = {}
        complete_roots = set()
        for source in sources:
            source_paths, scan_errors = _scan_paths(
                source, media_type, patterns, scanner.update if scanner else None
            )
            paths_by_root[source] = source_paths
            if scan_errors:
                source_errors.extend(scan_errors)
                for error in scan_errors:
                    print(f"source error: {error}", file=sys.stderr)
            else:
                complete_roots.add(source)
    finally:
        if scanner:
            scanner.stop()
    paths = {path for source_paths in paths_by_root.values() for path in source_paths}
    roots_by_path = {
        path: source for source, source_paths in paths_by_root.items() for path in source_paths
    }
    if verbose:
        print(f"Found {len(paths)} {media_type} files; inspecting metadata and hashes...")
    connection = open_index(index_path)
    candidates = []
    unchanged = 0
    for path in paths:
        try:
            stat = Path(path).stat()
            source = roots_by_path[path]
            if is_unchanged(
                connection, path, str(source), stat.st_size, stat.st_mtime_ns, media_type
            ):
                unchanged += 1
            else:
                candidates.append(path)
        except OSError:
            candidates.append(path)
    workers = args.workers
    results = failures = 0
    if verbose:
        print(f"Skipping {unchanged} unchanged {media_type} files.")
    progress = (
        None if args.quiet else Progress(len(candidates), f"Discovering {media_type}s", verbose)
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(inspect_media, Path(path), roots_by_path[path], media_type): path
            for path in candidates
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                upsert_image(connection, future.result())
                results += 1
            except (OSError, RuntimeError, ValueError) as exc:
                upsert_image(
                    connection,
                    {
                        "source_path": path,
                        "source_root": str(roots_by_path[path]),
                        "size": 0,
                        "mtime_ns": 0,
                        "media_type": media_type,
                        "status": "error",
                        "error": str(exc),
                    },
                )
                failures += 1
            if progress:
                progress.update(path)
            if results % 100 == 0:
                connection.commit()
    if progress:
        progress.finish()
    for source in complete_roots:
        mark_stale(connection, source, paths_by_root[source], media_type)
    connection.commit()
    connection.close()
    print(
        f"discovered={results} errors={failures} source_errors={len(source_errors)} "
        f"unchanged={unchanged} files={len(paths)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./bin/picsort",
        description="Scan, deduplicate, and organize an image library safely.",
        epilog="Examples:\n"
        "  ./bin/picsort discover /Photos --index /Library/.picsort.sqlite --workers 8\n"
        "  ./bin/picsort organize --index /Library/.picsort.sqlite --destination /Library --dry-run\n"
        "  ./bin/picsort report --index /Library/.picsort.sqlite --output /Library/index.html",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, title="commands")
    discover = subparsers.add_parser("discover", help="Index supported images and EXIF metadata")
    discover_sources = discover.add_mutually_exclusive_group(required=True)
    discover_sources.add_argument("source", nargs="?", help="Source folder to scan recursively")
    discover_sources.add_argument(
        "--source-list", metavar="FILE", help="Text file with one source directory per line"
    )
    discover.add_argument(
        "--videos", action="store_true", help="Process videos only instead of photos"
    )
    discover.add_argument(
        "--spinner",
        action="store_true",
        default=True,
        help="Show activity while scanning (default)",
    )
    discover.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress live progress and verbose output"
    )
    discover.add_argument("-v", "--verbose", action="store_true", help="Print every processed file")
    discover.add_argument("--index", required=True, help="SQLite index path")
    discover.add_argument(
        "--workers",
        type=int,
        metavar="N",
        help="Parallel metadata workers (default: Python default)",
    )
    discover.add_argument(
        "--ignore-pattern", action="append", help="Regex filename filter; may be repeated"
    )
    discover.set_defaults(func=_discover)
    organize_parser = subparsers.add_parser(
        "organize", help="Group duplicates and copy the best images"
    )
    organize_parser.add_argument("--index", required=True, help="SQLite index path")
    organize_parser.add_argument("--destination", required=True, help="Output library directory")
    organize_parser.add_argument(
        "--videos", action="store_true", help="Organize videos only instead of photos"
    )
    organize_parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress live progress and verbose output"
    )
    organize_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Print every processed file"
    )
    organize_parser.add_argument(
        "--phash-threshold",
        type=int,
        default=5,
        metavar="BITS",
        help="Maximum perceptual hash distance (default: 5)",
    )
    organize_parser.add_argument(
        "--workers",
        type=int,
        metavar="N",
        help="Parallel file-copy workers (default: Python default)",
    )
    organize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned copies without writing files or index changes",
    )
    organize_parser.set_defaults(func=_organize)
    report = subparsers.add_parser("report", help="Write a standalone HTML library report")
    report.add_argument(
        "-v", "--verbose", action="store_true", help="Print report processing stages"
    )
    report.add_argument("--videos", action="store_true", help="Report on videos only")
    report.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress verbose report messages"
    )
    report.add_argument("--index", required=True, help="SQLite index path")
    report.add_argument("--output", required=True, help="HTML report output path")
    report.set_defaults(func=_report)
    run = subparsers.add_parser("run", help="Run discover, organize, and report in sequence")
    run_sources = run.add_mutually_exclusive_group(required=True)
    run_sources.add_argument("source", nargs="?", help="Source folder to scan recursively")
    run_sources.add_argument(
        "--source-list", metavar="FILE", help="Text file with one source directory per line"
    )
    run.add_argument("--videos", action="store_true", help="Process videos only instead of photos")
    run.add_argument(
        "--spinner",
        action="store_true",
        default=True,
        help="Show activity while scanning (default)",
    )
    run.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress live progress and verbose output"
    )
    run.add_argument("--index", required=True, help="SQLite index path")
    run.add_argument("--destination", required=True, help="Output library directory")
    run.add_argument("--output", help="HTML report output path (default: DESTINATION/index.html)")
    run.add_argument(
        "-v", "--verbose", action="store_true", help="Print every processed file and report stages"
    )
    run.add_argument("--workers", type=int, metavar="N", help="Parallel discovery and copy workers")
    run.add_argument(
        "--ignore-pattern", action="append", help="Regex filename filter; may be repeated"
    )
    run.add_argument(
        "--phash-threshold",
        type=int,
        default=5,
        metavar="BITS",
        help="Maximum perceptual hash distance (default: 5)",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="Preview organization without copying files"
    )
    run.set_defaults(func=_run)
    return parser


def _organize(args) -> None:
    connection = open_index(Path(args.index).expanduser().resolve())
    progress = None
    verbose = args.verbose and not args.quiet
    if verbose:
        print("Preparing duplicate groups and copy plan...")

    def start(total):
        nonlocal progress
        progress = Progress(total, "Organizing", verbose)

    def update(path, status):
        progress.update(f"{status}: {path}")

    print(
        organize(
            connection,
            Path(args.destination).expanduser().resolve(),
            args.phash_threshold,
            args.dry_run,
            args.workers,
            None if args.quiet else start,
            None if args.quiet else update,
            "video" if args.videos else "image",
        )
    )
    if progress:
        progress.finish()
    connection.close()


def _report(args) -> None:
    connection = open_index(Path(args.index).expanduser().resolve())
    if args.verbose and not args.quiet:
        print("Reading SQLite index...")
    render(
        connection, Path(args.output).expanduser().resolve(), "video" if args.videos else "image"
    )
    connection.close()
    if args.verbose and not args.quiet:
        print("Wrote standalone HTML report.")
    print(f"report={args.output}")


def _run(args) -> None:
    _discover(args)
    _organize(args)
    if args.output is None:
        args.output = str(Path(args.destination).expanduser().resolve() / "index.html")
    _report(args)


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
