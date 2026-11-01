# Gpdf

A Windows desktop PDF reader and annotation editor, built with Qt and MuPDF. Gpdf keeps rendering work out of the interface process, requests pages on demand, and limits its image cache to help keep large documents responsive. Local PDFs stay on your computer; optional Google Drive access downloads files you select and uploads edits when you save.

## Run on Windows

Double-click `dist\Gpdf.exe` for the standalone app without a console window. Facing continuous is the default reading layout.

If you have the folder-based packaged app, open `dist\Gpdf\Gpdf.exe` (or `Gpdf.exe` inside the distributed folder). Keep the `_internal` folder beside it. The packaged version includes Python and its dependencies.

To run from source instead:

Install Python 3.11 or newer. From PowerShell in this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

After setup, double-click `launch.bat`. You can also open a PDF from the command line:

```powershell
.\launch.bat "C:\Documents\A large document.pdf"
```

The launcher prefers this project's `.venv`, then an installed Python 3.11+ interpreter. No administrator privileges are required. If Python is available through Conda, run the setup commands from an activated Conda environment, or pass its executable to the build script.

## Reading and editing

- Choose Single page, Continuous, Facing, or Facing continuous from the top layout selector or Gpdf > View > Page layout. Continuous layouts scroll across page boundaries; facing layouts display two pages per row.
- Controls share one compact top row. Menus are under Gpdf; the sidebar (F9) and status bar (Gpdf > View > Show status bar) are hidden by default. Press F11 to toggle fullscreen.
- Visible pages render first; nearby pages are prefetched during idle time into a bounded cache.
- Scroll the mouse wheel at a page boundary to turn pages. Fast wheel bursts advance through multiple pages, including while the next page is loading; Ctrl+wheel zooms.
- Search and copy existing PDF text, including the text layer in OCR PDFs.
- Add highlights, underlines, free text, notes, rectangles, ellipses, arrows, and ink.
- Add text comments by dragging a box and typing directly on the PDF page. Double-click a text comment with Select or Text to edit it in place. Ctrl+Enter or a click elsewhere on the page finishes the edit; Esc cancels. Saving and page navigation commit the draft first.
- Change font size and color while typing to preview them immediately. Select and move or resize supported annotations. Sticky notes retain their separate note editor.
- Place images on pages.
- Undo and redo changes, then save PDF annotations for use in other readers.
- Rotate, delete, merge, and extract pages.

This is an annotation editor. It does not rewrite existing body text or replace fonts inside a PDF. OCR text is usable when it already exists; generating new OCR, digital signatures, and secure redaction are not included. Added images become page content when saved, rather than independently editable annotations; undo can remove a newly inserted image before saving. Rectangle annotations are not a secure way to hide sensitive text.

Saving or appending a PDF starts a new undo history. Save a copy when you want to preserve the original document.

### Google Drive

Click **Google Drive** in the toolbar to sign in and browse PDFs in Google's browser picker. Google can use the account already signed into your default browser; it may ask you to choose an account or approve file access. Users do not choose a client JSON. The app developer must register Gpdf with Google once and include its Desktop OAuth client when building the app. Only files you select are authorized; Gpdf does not request access to your entire Drive. Sign-in tokens remain in memory for the current app session.

For a Drive document, **Save edited copy to Drive / Ctrl+S** creates a new PDF in My Drive. Every Drive save creates a separate copy; the source file is never overwritten. **Save as / Ctrl+Shift+S** saves a local copy and detaches it from Drive. A failed upload leaves edits available for retry or a local save. Use **Disconnect Google Drive** to clear this session and request revocation.

See [Google Drive setup, privacy, and validation](docs/google-drive.md) before configuring or distributing the integration. Google account setup and live end-to-end OAuth testing require your credentials. Security controls reduce risk; they do not guarantee that software or a computer cannot be compromised.

### Keyboard shortcuts

| Action | Shortcut |
| --- | --- |
| Open | Ctrl+O |
| Save / Save as | Ctrl+S / Ctrl+Shift+S |
| Search / Copy selected text | Ctrl+F / Ctrl+C |
| Undo / Redo | Ctrl+Z / Ctrl+Y or Ctrl+Shift+Z |
| Previous / Next page (spread in facing layouts) | Page Up / Page Down |
| Fullscreen | F11 |
| Zoom in / out | Ctrl+Plus / Ctrl+Minus |
| Fit page / Fit width | Ctrl+0 / Ctrl+1 |

## Large documents

The viewer renders the requested page rather than rasterizing the entire document at startup. MuPDF runs in a separate worker process, while a bounded cache retains recent page images. Text searches run in the worker as well.

These choices reduce unnecessary rendering and keep the interface separate from expensive PDF operations. Actual speed and memory use depend on your hardware, scan resolution, page complexity, and the PDF itself. A single very large scanned page can still take time to decode; performance has not been established as faster than Adobe or Foxit. Single page, Continuous, Facing, and Facing continuous layouts are available; only visible and nearby pages are rendered.

### Measure on your computer

```powershell
.\.venv\Scripts\python.exe scripts\benchmark.py
.\.venv\Scripts\python.exe scripts\benchmark.py --pdf "C:\Documents\Large OCR scan.pdf"
```

The benchmark opens the document read-only and renders its first, middle, and last pages. Without `--pdf`, it creates and removes a temporary 1,000-page synthetic PDF with a shared scan image and hidden text. That fixture checks page-count behavior; its compressed shared image is not representative of thousands of distinct high-resolution scans. Timings measure the PDF engine directly and exclude worker startup, IPC, interface painting, and the viewer's cache. Compare real PDFs on the same hardware for useful results. Add `--json` for machine-readable output, or `--scale 2` to measure a higher rendering resolution.

For a larger synthetic file containing distinct scans, install the optional benchmark dependency and run:

```powershell
.\.venv\Scripts\python.exe -m pip install Pillow
.\.venv\Scripts\python.exe scripts\stress_benchmark.py --report artifacts\stress-benchmark.json
```

This creates 80 distinct 1500 by 2000 pixel grayscale scans with hidden text, measures sampled rendering and text searches, and removes its temporary PDF. Pillow is only needed for generating this benchmark, not for running Gpdf.

## Build a Windows executable

Build on Windows using the same Python environment you use to run the app:

```powershell
.\build.ps1 -InstallDependencies
```

The default produces `dist\Gpdf\Gpdf.exe`. Keep the entire `dist\Gpdf` folder together when copying it to another Windows computer. The folder build avoids the extraction step that a single-file executable performs at startup.

Optional variants:

```powershell
.\build.ps1 -InstallDependencies -OneFile
.\build.ps1 -Python "C:\Path With Spaces\python.exe" -InstallDependencies
```

The single-file build produces `dist\Gpdf.exe`. Without `-InstallDependencies`, the script uses already installed dependencies and PyInstaller. If your PowerShell policy blocks local scripts, run this command for this invocation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -InstallDependencies
```

Use `-Console` for a diagnostic build that displays Python errors in a console window.

## Validation

Run the engine, canvas, and interface integration checks from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover PDF editing and save/reopen behavior, rendering limits, annotation placement, communication between Qt and the PDF worker, and mocked OAuth/Drive transfer failures. Live Google sign-in and uploads require the account checks in the [Drive guide](docs/google-drive.md). The performance scripts above measure different workloads; passing the functional tests does not establish performance on every PDF.

## Dependency licensing

PyMuPDF is available under AGPL or a commercial license, and PyQt6 is available under GPL or a commercial license. Their license terms apply when using or distributing a packaged app. Choose the appropriate dependency licenses before distributing a proprietary product. This repository does not assign a license to the application code.

- [PyMuPDF licensing](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)
- [PyQt licensing](https://www.riverbankcomputing.com/commercial/license-faq)
