# picsort

`picsort` scans large image collections, records durable metadata in SQLite, groups exact and visually similar images, and copies the best candidate into a date-based library. Source files are never modified.

## Setup

```bash
python3 -m venv venv
source ./venv/bin/activate
pip3 install -r requirements.txt
```

Install optional decoders when needed:

```bash
pip3 install -e '.[heic,raw]'
```

HEIC originals, including the still-image component of Live Photos, require the `heic` extra. After installing it, rerun `discover` to retry previously failed HEIC files, then rerun `organize` to consider the recovered high-resolution originals.

## Workflow

Discovery is resumable and may be rerun after an interruption:

```bash
./bin/picsort discover /Photos --index /Library/.picsort.sqlite --workers 8
./bin/picsort organize --index /Library/.picsort.sqlite --destination /Library --workers 8 --dry-run
./bin/picsort organize --index /Library/.picsort.sqlite --destination /Library --workers 8
./bin/picsort report --index /Library/.picsort.sqlite --output /Library/index.html
```

For multiple input directories, put one directory per line in a text file. Blank lines and lines beginning with `#` are ignored:

```text
/Volumes/Camera A/DCIM
/Volumes/Camera B/DCIM
# /Volumes/Archive/old-photos
```

Then use the list with `discover` or `run`:

```bash
./bin/picsort discover --source-list sources.txt --index /Library/.picsort.sqlite
./bin/picsort run --source-list sources.txt --index /Library/.picsort.sqlite --destination /Library
```

To run all three phases in sequence, use `run`. The report defaults to `DESTINATION/index.html`:

```bash
./bin/picsort run /Photos \
  --index /Library/.picsort.sqlite \
  --destination /Library \
  --workers 8 \
  --dry-run
```

Videos are opt-in and exclusive. Install the PyAV dependency from `requirements.txt`, then use `--videos` to process only video clips:

```bash
./bin/picsort run /Videos --videos \
  --index /Library/.picsort.sqlite \
  --destination /Library \
  --workers 8
```

Supported video extensions are `mp4`, `mov`, `m4v`, `avi`, `mkv`, and `webm`. Video duplicates use exact MD5 matching and are copied byte-for-byte. Embedded container dates are used for year folders; missing dates and epoch dates go under `unsorted`.

Add `-v` or `--verbose` to any command to print each processed file or report stage. Without verbose mode, `discover` and `organize` show a live progress bar with completed count, processing rate, and estimated time remaining. For example:

```bash
./bin/picsort discover /Photos --index /Library/.picsort.sqlite --workers 8 --verbose
./bin/picsort organize --index /Library/.picsort.sqlite --destination /Library --workers 8 --dry-run
```

`discover` and `run` show directory and entry counts, matching-file counts, elapsed time, and scan rate by default while the source directory is being enumerated. The exact total and ETA become available once enumeration finishes. Use `-q` or `--quiet` to suppress live status output:

```bash
./bin/picsort discover /Photos --index /Library/.picsort.sqlite --quiet
```

Selected files are byte-for-byte copies named `YYYY-MM-DD-<md5>.<ext>`, with canonical extensions (`jpg` and `jpeg` become `jpeg`, `tif` and `tiff` become `tiff`, and `heic` remains `heic`). EXIF capture dates produce a `YYYY` folder; images without a valid EXIF date, or with years 0000, 1969, or 1970, go under `unsorted` and use the `0000-00-00` filename prefix. Existing destination files are never replaced. During `organize`, files previously generated with shortened `.jpg`, `.tif`, or `.hei` extensions are safely renamed to `.jpeg`, `.tiff`, or `.heic`; existing targets are never overwritten, and `--dry-run` previews these renames.

If a later discovery finds a perceptual match with strictly greater pixel area, `organize` copies the higher-resolution image and moves the previously organized lower-resolution file under `DESTINATION/deprecated`, preserving its relative path and year folder. Deprecated files are never overwritten. Use `--dry-run` to preview both the new copy and the deprecation without changing the library or index.

Discovery ignores common thumbnail names such as `thumb`, `thumbnail`, `preview`, `small`, and `icon`. Add `--ignore-pattern` to provide regular expressions. Use `--phash-threshold` on `organize` to tune visually similar grouping; the default is conservative.

Run `./bin/picsort --help` or `./bin/picsort <command> --help` for complete usage. `--workers N` controls parallel metadata inspection during discovery and parallel byte-for-byte copying during organization. `--dry-run` reports planned organization without changing the destination or index.

The report is a standalone HTML file with totals, formats, EXIF date summaries, duplicate/error counts, and links to generated destination folders.

Discovery fingerprints each file using its size and modification time before reading it. Unchanged files are skipped, so rerunning a scan does not reread the existing library.

Invalid, missing, unreadable, and non-UTF-8 source paths are reported as source errors and skipped. Files found during a partially readable scan are indexed, but existing entries are marked stale only after the entire source root is scanned successfully. If validation leaves no valid source roots, discovery exits without modifying the index.

## Development

Run tests with `pytest`. Run `ruff check .` and `ruff format --check .` before submitting changes. Tests use temporary image libraries and must not depend on personal photo data.
