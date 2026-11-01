# Repository Guidelines

## Top Priority: Fast PDF Reading

Fast PDF reading is the project's number one priority. Opening, displaying, navigating, and scrolling PDFs must remain responsive and smooth, even for large files and documents with many pages or high-resolution scans. Treat lag during reading or scrolling as a critical performance regression.

Prioritize visible-page rendering and user input over background work. Keep expensive PDF operations off the GUI thread, use bounded caches and rendering queues, and avoid processing the entire document before users can start reading. Evaluate changes affecting loading, rendering, caching, or navigation with representative large PDFs and the benchmark scripts; preserve document correctness and file safety while optimizing performance.

## Project Structure & Module Organization

- `gpdf/app.py` contains the PyQt6 window; `canvas.py` handles page display and gestures, and `icons.py` defines vector icons.
- `gpdf/bridge.py` manages worker requests and the page cache; `engine.py` owns PDF rendering, annotations, and persistence. `smoke.py` verifies packaged workflows.
- `main.py` and `launch.bat` launch the app. `build.ps1` packages it with PyInstaller.
- `tests/` contains engine, canvas, and interface tests. `scripts/` contains benchmarks; `artifacts/` holds validation reports and screenshots. `.build/`, `dist/`, and `tmp/` are generated directories.

## Build, Test, and Development Commands

Use Python 3.11+ on Windows. Run commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\build.ps1 -InstallDependencies
```

These commands create an environment, install dependencies, launch the app, run tests, and build `dist/StrixPDF/StrixPDF.exe`. Keep the complete distribution folder together.

Run `python scripts/benchmark.py --pdf "document.pdf"` in the environment to measure a real document. `scripts/stress_benchmark.py` generates distinct synthetic scans and additionally requires Pillow.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_CASE` constants. Preserve Qt override names such as `mousePressEvent`. Add concise docstrings and type hints for new public interfaces. No formatter or linter is configured; optional linting uses `python -m pyflakes gpdf main.py tests scripts` after installing Pyflakes.

## Architecture & File Safety

Keep MuPDF objects inside the spawned worker; never perform PDF work in the GUI process. Bridge payloads use zero-based pages and rotated display coordinates in PDF points. Preserve bounded rendering/caching, stale-response checks, and atomic saves. Failed saves must retain edits; preserve encryption, permissions, and OCR layers.

## Testing Guidelines

Use `unittest` and Qt's `QTest`; name files `test_*.py` and methods `test_*`. Generate temporary fixtures instead of using private documents. Add regression tests for changed behavior, especially coordinates, worker failures, and save/reopen safety. Qt tests configure offscreen mode. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

This checkout has no Git history to establish conventions. Use concise imperative commit subjects, such as `Fix rotated annotation placement`. PRs should explain the problem, resulting behavior, validation commands, and relevant issue links. Include screenshots for interface changes and reproducible workload details for performance claims. Exclude generated builds and private PDFs.
