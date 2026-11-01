"""Repeatable large-file benchmark with distinct noisy scans and hidden text.

Unlike the high-page-count benchmark, every page embeds its own JPEG. This is
a synthetic stress workload, not a claim about arbitrary real-world PDFs.
The generated PDF lives in a temporary directory and is removed afterward.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
import pymupdf

from gpdf.engine import PdfEngine


def generate(path: Path, pages: int, width: int, height: int):
    document = pymupdf.open()
    total_jpeg_bytes = 0
    try:
        for number in range(pages):
            # Distinct sensor-like grain deliberately resists compression. This
            # is heavier than most clean scans, with visible document text on top.
            scan = Image.effect_noise((width, height), 45).convert("L")
            drawing = ImageDraw.Draw(scan)
            drawing.rectangle((55, 60, width - 55, 190), fill=245)
            drawing.text((80, 100), f"G PDF distinct scan stress fixture / PAGE {number + 1}", fill=15)
            for line in range(24):
                top = 260 + line * (height - 350) // 24
                drawing.rectangle((70, top, width - 70, top + 26), fill=220)
                drawing.text((80, top + 4), f"Record {number + 1}:{line + 1} - searchable OCR text layer test", fill=15)
            stream = io.BytesIO()
            scan.save(stream, format="JPEG", quality=90)
            jpeg = stream.getvalue()
            total_jpeg_bytes += len(jpeg)
            if total_jpeg_bytes > 245 * 1024 * 1024:
                raise ValueError("Fixture exceeds the 245 MiB safety limit; reduce --pages.")
            page = document.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=jpeg)
            page.insert_text((32, 60), f"Distinct scan page {number + 1}: searchable OCR layer", fontsize=12,
                             render_mode=3)
        document.save(path)
    finally:
        document.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=80)
    parser.add_argument("--width", type=int, default=1500)
    parser.add_argument("--height", type=int, default=2000)
    parser.add_argument("--scale", type=float, default=1.5)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.pages <= 200 or not 100 <= args.width <= 3000 or not 100 <= args.height <= 4000:
        parser.error("Use 1–200 pages, width 100–3000, height 100–4000; fixture is capped at 245 MiB.")
    report = {
        "workload": "Distinct synthetic grayscale noisy JPEG scans with invisible OCR text per page",
        "pages": args.pages, "scan_dimensions": [args.width, args.height], "jpeg_quality": 90,
        "render_scale": args.scale, "timing_scope": "Direct engine operations; excludes Qt and process IPC",
        "pymupdf": pymupdf.VersionBind,
    }
    with tempfile.TemporaryDirectory(prefix="gpdf-stress-") as directory:
        path = Path(directory) / "distinct-scans.pdf"
        started = time.perf_counter()
        generate(path, args.pages, args.width, args.height)
        report["generation_seconds"] = round(time.perf_counter() - started, 3)
        report["file_mib"] = round(path.stat().st_size / 1024 ** 2, 2)
        engine = PdfEngine()
        try:
            started = time.perf_counter()
            engine.handle("open", {"path": str(path)})
            report["open_ms"] = round((time.perf_counter() - started) * 1000, 2)
            measurements = []
            for index in dict.fromkeys([0, args.pages // 2, args.pages - 1]):
                started = time.perf_counter()
                rendered = engine.handle("render", {"page": index, "scale": args.scale})
                render_ms = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                matches = engine.handle("search", {"page": index, "query": "searchable OCR"})
                search_ms = (time.perf_counter() - started) * 1000
                measurements.append({"page": index + 1, "render_ms": round(render_ms, 2),
                                     "search_ms": round(search_ms, 2), "matches": len(matches["rects"]),
                                     "render_dimensions": [rendered["width"], rendered["height"]]})
            report["sampled_pages"] = measurements
        finally:
            engine.close()
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
