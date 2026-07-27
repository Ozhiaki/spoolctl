# Install

spoolctl requires Python 3.10 or later on macOS or Linux. Windows is not supported; SQLite WAL mode and process-group semantics behave differently on Windows and the test suite does not cover it.

## Checkout usage

The simplest path is to clone the repository and run spoolctl as a Python module:

```bash
git clone https://github.com/Ozhiaki/spoolctl.git
cd spoolctl
python3 -m spoolctl --version
```

No install step is needed. spoolctl has zero runtime dependencies beyond the Python standard library.

## Single-file build

For sandboxed environments or deployment without a full checkout, spoolctl can be built into a single self-contained Python file:

```bash
python3 scripts/build_single_file.py dist/spoolctl.py
python3 dist/spoolctl.py --version
```

The single-file artifact bundles every module into one file. It is functionally identical to the package and is verified by the same signature matrix.

## Future install paths

The following install methods are planned but not yet blessed for production use:

- `pip install spoolctl` -- the package metadata exists in `pyproject.toml` but the package is not yet published to PyPI.
- `uv tool install spoolctl` -- same constraint; will work once published.

Until these paths are available, use the checkout or single-file build above.

## Docs extra

The `[docs]` optional dependency (`pip install '.[docs]'`) installs mkdocs-material for local documentation builds. It is a development convenience and is never required to read the docs, run spoolctl, or pass the test suite. The `pip install spoolctl` path requires nothing beyond the standard library.
