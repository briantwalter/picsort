# Repository Guidelines

## Project Structure & Module Organization

This repository contains the `picsort` Python CLI for deduplicating and organizing large image libraries.

- `src/picsort/`: application package, divided by CLI commands, indexing, image inspection, organization, and reporting.
- `tests/`: automated unit and integration tests, mirroring the `src/picsort/` package structure.
- `README.md`: setup, supported formats, workflow examples, and operational guidance.
- `pyproject.toml`: package metadata, dependencies, and development tooling.
- `venv/`: local virtual environment; never commit it.

The SQLite index and generated HTML reports belong in a user-selected output directory, not in source control.

## Build, Test, and Development Commands

Create and activate the development environment on macOS:

```bash
python3 -m venv venv
source ./venv/bin/activate
pip3 install -r requirements.txt
```

Run the CLI with `python -m picsort` or the installed `picsort` command. Typical workflows are:

```bash
./bin/picsort discover SOURCE --index OUTPUT/.picsort.sqlite
./bin/picsort organize --index OUTPUT/.picsort.sqlite --destination OUTPUT --dry-run
./bin/picsort report --index OUTPUT/.picsort.sqlite --output OUTPUT/index.html
```

Run the full test suite with `pytest`. Use `ruff check .` for linting and `ruff format --check .` for formatting validation.

## Coding Style & Naming Conventions

Use Python 3, four-space indentation, type hints for public interfaces, and small testable functions. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Keep filesystem operations explicit and non-destructive by default. Format with Ruff and avoid adding inline comments unless they clarify a non-obvious constraint.

## Testing Guidelines

Use `pytest`; name files `test_*.py` and tests `test_<behavior>`. Cover resumability, SQLite index updates, exact and perceptual duplicates, EXIF dates, unsupported/corrupt files, dry runs, collisions, and byte-for-byte copies. Tests must use temporary directories and fixtures rather than real photo libraries. Maintain meaningful coverage for changed code.

## Commit & Pull Request Guidelines

There is no existing Git history, so use imperative commit subjects such as `Add resumable discovery index`. Keep commits focused. Pull requests should describe behavior changes, document CLI examples, list tests and checks run, and call out compatibility or dependency changes. Include report screenshots only when changing HTML presentation.

## Security & Configuration Tips

Never delete or replace source or destination files implicitly. Validate user paths, handle symlinks deliberately, avoid following paths outside the requested source when unsupported, and record per-file errors without stopping a long scan. Keep generated indexes, reports, and local configuration out of commits.
