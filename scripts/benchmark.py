"""Read-only PDF engine benchmark; uses a temporary synthetic fixture by default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
import tempfile
from time import perf_counter

import pymupdf

# Allow `python scripts/benchmark.py` from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpdf.engine import PdfEngine


def make_fixture(path: Path, page_count: int) -> None:
    """Reuse one scan image, plus per-page invisible OCR-like text."""
    body = "\n".join(
        f"Line {line + 1:02d}: A sample page for rendering and OCR text navigation."
        for line in range(36)
    )
    with pymupdf.open() as source:
        page = source.new_page(width=612, height=792)
        page.draw_rect(page.rect, color=None, fill=(0.97, 0.97, 0.94))
        page.insert_text((42, 52), "Synthetic scanned document", fontsize=20)
        page.insert_text(
            (42, 90), body, fontsize=11, lineheight=17 / 11, color=(0.12, 0.12, 0.12)
        )
        scan = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        scan_bytes = scan.tobytes("jpeg", jpg_quality=85)

    with pymupdf.open() as doc:
        image_xref = 0
        for number in range(page_count):
            page = doc.new_page(width=612, height=792)
            if image_xref:
                page.insert_image(page.rect, xref=image_xref)
            else:
                image_xref = page.insert_image(page.rect, stream=scan_bytes)
            page.insert_text(
                (42, 52), "Synthetic scanned document", fontsize=20, render_mode=3
            )
            page.insert_text(
                (42, 90), body, fontsize=11, lineheight=17 / 11, render_mode=3
            )
            page.insert_text(
                (42, 754), f"Page {number + 1} of {page_count}", fontsize=10
            )
        doc.save(path, garbage=3, deflate=True)


def measure(path: Path, scale: float) -> dict:
    engine = PdfEngine()
    try:
        started = perf_counter()
        metadata = engine.handle("open", {"path": str(path)})
        open_seconds = perf_counter() - started
        page_count = metadata["page_count"]
        if not page_count:
            raise ValueError("The PDF contains no pages.")
        renders = []
        for index in sorted({0, page_count // 2, page_count - 1}):
            started = perf_counter()
            result = engine.handle("render", {"page": index, "scale": scale})
            elapsed = perf_counter() - started
            renders.append(
                {
                    "page": index + 1,
                    "seconds": round(elapsed, 6),
                    "width": result.get("width"),
                    "height": result.get("height"),
                }
            )
            del result
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pymupdf": pymupdf.VersionBind,
            "file_bytes": path.stat().st_size,
            "page_count": page_count,
            "requested_scale": scale,
            "open_seconds": round(open_seconds, 6),
            "renders": renders,
        }
    finally:
        engine.handle("close", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="Existing PDF; never modified")
    parser.add_argument("--pages", type=int, default=1000, help="Synthetic page count")
    parser.add_argument("--scale", type=float, default=1.5, help="Pixels per PDF point")
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be positive")
    if not 0 < args.scale <= 8:
        parser.error("--scale must be greater than 0 and at most 8")
    if args.pdf and not args.pdf.is_file():
        parser.error(f"PDF does not exist: {args.pdf}")

    if args.pdf:
        results = measure(args.pdf.resolve(), args.scale)
        results["fixture"] = "user-supplied PDF"
    else:
        with tempfile.TemporaryDirectory(prefix="strix-pdf-benchmark-") as folder:
            fixture_path = Path(folder) / "synthetic-ocr.pdf"
            make_fixture(fixture_path, args.pages)
            results = measure(fixture_path, args.scale)
            results["fixture"] = "synthetic scan with shared image and hidden text"

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"Fixture: {results['fixture']}")
        print(f"Python {results['python']} / PyMuPDF {results['pymupdf']}")
        print(results["platform"])
        print(f"Pages: {results['page_count']:,}; file: {results['file_bytes'] / 1048576:.2f} MiB")
        print(f"Open: {results['open_seconds'] * 1000:.1f} ms")
        for render in results["renders"]:
            print(
                f"Render page {render['page']:,}: {render['seconds'] * 1000:.1f} ms "
                f"({render['width']} x {render['height']} pixels)"
            )
        print("Engine timings only; excludes process startup, IPC, GUI, and viewer cache.")
        if not args.pdf:
            print("Shared-image synthetic fixture; use --pdf to measure a real large scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
