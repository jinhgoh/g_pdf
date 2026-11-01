"""Behavioral coverage of the worker's PDF editing and persistence contract."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from gpdf.engine import PasswordRequiredError, PdfEngine, worker_main


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.original = self.root / "original.pdf"
        document = pymupdf.open()
        for number in range(3):
            page = document.new_page(width=400, height=600)
            page.insert_text((40, 60), f"Searchable OCR layer on page {number + 1}", fontsize=14,
                             render_mode=3 if number == 1 else 0)
        document.set_metadata({"title": "Engine fixture", "author": "Test"})
        document.set_toc([[1, "Beginning", 1], [1, "OCR page", 2]])
        document.save(self.original)
        document.close()
        image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 24, 24), False)
        image.clear_with(160)
        self.image_path = self.root / "image.png"
        image.save(self.image_path)
        self.engine = PdfEngine()
        self.engine.handle("open", {"path": str(self.original)})

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def command(self, name, **payload):
        return self.engine.handle(name, payload)

    def annotations(self, page=0):
        return self.command("render", page=page, scale=0.5)["annotations"]

    def test_importing_worker_module_does_not_load_mupdf(self):
        result = subprocess.run([sys.executable, "-c",
                                 "import sys; import gpdf.engine; assert 'pymupdf' not in sys.modules"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_drive_merge_snapshots_stay_in_working_directory(self):
        private = self.root / "private"
        private.mkdir()
        path = private / "working.pdf"
        path.write_bytes(self.original.read_bytes())
        self.command("open", path=str(path), private_working_copy=True)
        self.command("merge", path=str(self.original))
        snapshots = list(self.engine._temporary_paths)
        self.assertTrue(snapshots)
        self.assertTrue(all(Path(item).parent == private for item in snapshots))
        self.command("save", path=str(path))
        self.assertEqual(self.engine._scratch_directory, str(private))
        self.command("save", path=str(self.root / "local-copy.pdf"))
        self.assertIsNone(self.engine._scratch_directory)
        self.engine.close()
        self.assertTrue(all(not Path(item).exists() for item in snapshots))

    def test_render_rgb_dimensions_and_pixel_limit(self):
        result = self.command("render", page=0, scale=1.5)
        self.assertEqual((result["width"], result["height"]), (600, 900))
        self.assertEqual(len(result["samples"]), result["stride"] * result["height"])
        self.assertEqual(result["stride"], result["width"] * 3)
        large = self.command("render", page=0, scale=100)
        self.assertLessEqual(large["width"] * large["height"], self.engine.MAX_RENDER_PIXELS)
        self.assertLess(large["scale"], 100)

    def test_all_annotation_tools_persist_and_remain_editable(self):
        tools = ["highlight", "underline", "rectangle", "ellipse", "arrow", "ink", "text", "note"]
        for index, tool in enumerate(tools):
            self.command("annotate", page=0, kind=tool,
                         rect=[35, 40 + index * 45, 280, 75 + index * 45],
                         points=[[40, 160], [70, 180], [160, 170]], color="#3366cc",
                         text=f"Comment {tool}", fontsize=16)
        self.command("annotate", page=0, kind="image", rect=[50, 450, 150, 550],
                     image_path=str(self.image_path))
        self.assertEqual({a["kind"] for a in self.annotations()}, set(tools))
        target = self.root / "edited.pdf"
        self.command("save", path=str(target))
        self.assertEqual(len(self.annotations()), len(tools))
        text = next(a for a in self.annotations() if a["kind"] == "text")
        self.command("update_annotation", page=0, xref=text["xref"], text="Edited after reopen",
                     fontsize=24, rect=[40, 300, 340, 380], color="#118844")
        updated = next(a for a in self.annotations() if a["xref"] == text["xref"])
        self.assertEqual(updated["text"], "Edited after reopen")
        self.assertEqual(updated["fontsize"], 24)
        self.assertEqual(updated["color"], "#118844")
        self.assertEqual(updated["rect"], [40, 300, 340, 380])
        with pymupdf.open(target) as saved:
            self.assertEqual(len(saved[0].get_images()), 1)
            self.assertEqual(saved.metadata["title"], "Engine fixture")
        with pymupdf.open(self.original) as source:
            self.assertEqual(list(source[0].annots() or []), [])

    def test_annotation_update_delete_and_undo_redo(self):
        created = self.command("annotate", page=0, kind="text", rect=[40, 100, 250, 150], text="First")
        self.assertTrue(created["can_undo"])
        self.command("update_annotation", page=0, xref=created["xref"], text="Second", fontsize=22)
        self.assertEqual(self.annotations()[0]["text"], "Second")
        self.command("undo")
        self.assertEqual(self.annotations()[0]["text"], "First")
        self.command("redo")
        self.assertEqual(self.annotations()[0]["text"], "Second")
        self.command("delete_annotation", page=0, xref=created["xref"])
        self.assertEqual(self.annotations(), [])
        self.command("undo")
        self.assertEqual(len(self.annotations()), 1)

    def test_korean_and_mixed_unicode_comment_appearance_survives_save(self):
        text = "English 한글 메모 中文 日本語 αβ"
        self.command("annotate", page=0, kind="text", rect=[30, 100, 370, 200],
                     text=text, fontsize=18, color="#102030")
        page = self.engine.doc[0]
        annotation = page.first_annot
        self.assertIn("한글 메모", annotation.get_text())
        self.assertIn("中文", annotation.get_text())
        self.assertIn("日本語", annotation.get_text())
        pixmap = annotation.get_pixmap(alpha=False)
        self.assertLess(min(pixmap.samples), 100)
        del annotation, page, pixmap
        self.command("save", path=str(self.root / "unicode.pdf"))
        page = self.engine.doc[0]
        annotation = page.first_annot
        self.assertEqual(annotation.info["content"], text)
        self.assertIn("한글 메모", annotation.get_text())
        xref = annotation.xref
        del annotation, page
        self.command("update_annotation", page=0, xref=xref, text="수정한 메모 — updated", fontsize=24)
        page = self.engine.doc[0]
        annotation = page.first_annot
        self.assertIn("수정한 메모", annotation.get_text())

    def test_images_support_undo_and_redo(self):
        self.command("annotate", page=0, kind="image", rect=[40, 100, 180, 240],
                     image_path=str(self.image_path))
        self.assertEqual(len(self.engine.doc[0].get_images()), 1)
        self.command("undo")
        self.assertEqual(len(self.engine.doc[0].get_images()), 0)
        self.command("redo")
        self.assertEqual(len(self.engine.doc[0].get_images()), 1)

    def test_horizontal_arrow_and_straight_ink_use_points(self):
        for kind in ["arrow", "ink"]:
            self.command("annotate", page=0, kind=kind, rect=[40, 100, 200, 100],
                         points=[[40, 100], [200, 100]])
        self.assertEqual({a["kind"] for a in self.annotations()}, {"arrow", "ink"})

    def test_rotated_coordinates_round_trip_and_search(self):
        self.command("rotate", page=0, degrees=90)
        rect = [100, 90, 360, 160]
        created = self.command("annotate", page=0, kind="text", rect=rect, text="Upright", fontsize=20)
        annotation = next(a for a in self.annotations() if a["xref"] == created["xref"])
        for expected, actual in zip(rect, annotation["rect"]):
            self.assertAlmostEqual(expected, actual, places=3)
        new_rect = [120, 100, 430, 170]
        self.command("update_annotation", page=0, xref=created["xref"], rect=new_rect, fontsize=25)
        self.assertEqual(self.annotations()[0]["rect"], new_rect)
        results = self.command("search", page=0, query="Searchable")
        self.assertEqual(len(results["rects"]), 1)
        search_rect = pymupdf.Rect(results["rects"][0]) + (-2, -2, 2, 2)
        copied = self.command("copy_text", page=0, rect=list(search_rect))
        self.assertIn("Searchable", copied["text"])
        rendered = self.command("render", page=0, scale=1)
        self.assertEqual((rendered["page_width"], rendered["page_height"]), (600, 400))

    def test_existing_invisible_ocr_text_is_searchable_and_preserved(self):
        result = self.command("search", page=1, query="OCR layer")
        self.assertTrue(result["rects"])
        self.assertIn("OCR layer", self.command("copy_text", page=1)["text"])
        self.command("annotate", page=1, kind="highlight", rect=[35, 40, 350, 70])
        self.command("save", path=str(self.root / "ocr.pdf"))
        self.assertTrue(self.command("search", page=1, query="OCR layer")["rects"])

    def test_highlight_snaps_and_blank_area_falls_back(self):
        self.command("annotate", page=0, kind="highlight", rect=[30, 30, 360, 85])
        page = self.engine.doc[0]
        annotation = page.first_annot
        # The text is 19 points high; the selected area is 55 points high.
        vertices = annotation.vertices
        self.assertLess(max(y for x, y in vertices) - min(y for x, y in vertices), 25)
        self.command("annotate", page=0, kind="highlight", rect=[40, 200, 220, 240])
        self.assertEqual(len(self.annotations()), 2)

    def test_structural_operations_and_extract(self):
        self.command("rotate", page=0, degrees=90)
        self.assertEqual(self.engine.doc[0].rotation, 90)
        self.command("undo")
        self.assertEqual(self.engine.doc[0].rotation, 0)
        self.command("delete_page", page=1)
        self.assertEqual(len(self.engine.doc), 2)
        self.command("undo")
        self.assertEqual(len(self.engine.doc), 3)
        output = self.root / "extract.pdf"
        self.command("extract", path=str(output), pages=[2, 0])
        with pymupdf.open(output) as extracted:
            self.assertEqual(len(extracted), 2)
            self.assertIn("page 3", extracted[0].get_text())
            self.assertIn("page 1", extracted[1].get_text())
        result = self.command("merge", path=str(output))
        self.assertEqual(result["page_count"], 5)
        self.assertIn("history_reset", result)
        self.assertFalse(result["can_undo"])
        self.command("annotate", page=4, kind="text", rect=[40, 100, 260, 150], text="Merged")
        self.command("undo")
        self.assertEqual(self.annotations(page=4), [])

    def test_atomic_save_to_original_and_failure_preserves_edits(self):
        original_bytes = self.original.read_bytes()
        self.command("annotate", page=0, kind="text", rect=[40, 100, 260, 150], text="Unsaved")
        self.assertEqual(self.original.read_bytes(), original_bytes)
        with patch("gpdf.engine.os.replace", side_effect=PermissionError("File locked")):
            with self.assertRaisesRegex(OSError, "edits are still open"):
                self.command("save", path=str(self.original))
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertEqual(self.annotations()[0]["text"], "Unsaved")
        self.command("save", path=str(self.original))
        self.assertNotEqual(self.original.read_bytes(), original_bytes)
        self.assertEqual(self.annotations()[0]["text"], "Unsaved")

    def test_invalid_inputs_do_not_replace_open_document(self):
        invalid = self.root / "broken.pdf"
        invalid.write_text("not a PDF")
        for target in [invalid, self.root / "absent.pdf"]:
            with self.assertRaises(Exception):
                self.command("open", path=str(target))
            self.assertEqual(len(self.engine.doc), 3)
        for payload in [{"page": 999, "scale": 1}, {"page": 0, "scale": float("nan")}]:
            with self.assertRaises(ValueError):
                self.engine.handle("render", payload)
        with self.assertRaises(ValueError):
            self.command("annotate", page=0, kind="text", rect=[0, 0, 0, 0], text="Invalid")
        self.assertFalse(self.engine._status()["can_undo"])

    def test_encrypted_password_permissions_and_preserving_save(self):
        encrypted = self.root / "encrypted.pdf"
        with pymupdf.open(self.original) as document:
            document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                          owner_pw="owner-password", user_pw="reader-password",
                          permissions=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ANNOTATE)
        for password in [None, "incorrect"]:
            with self.assertRaises(PasswordRequiredError):
                self.command("open", path=str(encrypted), password=password)
            self.assertEqual(self.engine.path, str(self.original))
        opened = self.command("open", path=str(encrypted), password="reader-password")
        self.assertTrue(opened["encrypted"])
        self.command("annotate", page=0, kind="text", rect=[40, 100, 260, 160], text="Permitted")
        with self.assertRaises(PermissionError):
            self.command("copy_text", page=0)
        with self.assertRaises(PermissionError):
            self.command("delete_page", page=0)
        saved = self.root / "encrypted-edited.pdf"
        self.command("save", path=str(saved))
        with pymupdf.open(saved) as document:
            self.assertTrue(document.needs_pass)
            self.assertTrue(document.authenticate("reader-password"))
            self.assertFalse(document.permissions & pymupdf.PDF_PERM_COPY)
            self.assertEqual(document[0].first_annot.info["content"], "Permitted")
        self.command("open", path=str(encrypted), password="owner-password")
        self.assertIn("Searchable", self.command("copy_text", page=0)["text"])

    def test_worker_spawn_protocol_and_error_recovery(self):
        context = multiprocessing.get_context("spawn")
        requests, responses = context.Queue(), context.Queue()
        process = context.Process(target=worker_main, args=(requests, responses))
        process.start()
        try:
            requests.put({"id": 1, "command": "open", "payload": {"path": str(self.original)}})
            result = responses.get(timeout=30)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["page_count"], 3)
            requests.put({"id": 2, "command": "render", "payload": {"page": 99, "scale": 1}})
            result = responses.get(timeout=30)
            self.assertFalse(result["ok"])
            self.assertEqual(result["id"], 2)
            requests.put({"id": 3, "command": "render", "payload": {"page": 0, "scale": 0.5}})
            result = responses.get(timeout=30)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["width"], 200)
        finally:
            requests.put(None)
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            requests.close()
            responses.close()
        self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
