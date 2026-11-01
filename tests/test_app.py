"""End-to-end Qt/worker checks: no display server or user documents needed."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import threading

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QFontDatabase, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from gpdf.app import MainWindow
from gpdf.bridge import PageCache


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Qt's offscreen Windows plugin does not enumerate system fonts.
        for name in ("segoeui.ttf", "segoeuib.ttf", "segoeuil.ttf"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        import pymupdf
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "reading.pdf")
        with pymupdf.open() as document:
            for number in range(3):
                page = document.new_page(width=612, height=792)
                page.draw_rect(pymupdf.Rect(0, 0, 612, 140), color=None, fill=(0.08, 0.16, 0.29))
                page.insert_text((48, 55), "Gpdf / FIELD NOTES", fontsize=11, color=(0.5, 0.7, 1))
                page.insert_text((48, 104), "A clearer view of your work", fontsize=25, color=(1, 1, 1))
                page.insert_text((48, 194), f"Chapter {number + 1} — Reading without the wait", fontsize=18)
                page.insert_text((48, 238), "Searchable research notes. Review, highlight, and keep moving.\n\nThe document stays local. Annotations stay in your PDF.\n\nUse the toolbar to add a comment, arrow, mark, or image.", fontsize=12, lineheight=1.7)
            document.set_toc([[1, "Getting started", 1], [1, "Research notes", 2], [1, "Next steps", 3]])
            document.save(self.path)
        self.window = MainWindow()
        self.assertEqual(self.window.layout_combo.currentData(), "facing_continuous")
        self.assertEqual(self.window.canvas.layout_mode, "facing_continuous")
        self.window.layout_combo.setCurrentIndex(0)  # Single-page navigation fixtures.
        self.window.show()
        self.errors = []
        self.window._error = lambda response: self.errors.append(response.get("error"))
        self.window._open(self.path)
        self.wait_ready()

    def tearDown(self):
        self.window.bridge.shutdown()
        self.window._closing = True
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_until(self, predicate, timeout=10):
        limit = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            QTest.qWait(10)
            if self.errors:
                self.fail(str(self.errors))
            if time.monotonic() > limit:
                self.fail("Timed out waiting for the PDF worker / interface")
        self.app.processEvents()

    def wait_ready(self):
        self.wait_until(lambda: self.window.path and not self.window.busy and self.window.display_identity == (self.window.session, self.window.revision, self.window.page) and not self.window.zoom_timer.isActive() and self.window.bridge.active is None)

    def test_open_navigation_search_and_text_copy(self):
        w = self.window
        self.assertEqual(w.page_count, 3)
        self.assertEqual(w.canvas._tool, "hand")
        self.assertEqual(w.outline.topLevelItemCount(), 3)
        w.search_input.setText("Searchable")
        w.start_search()
        self.wait_until(lambda: not w.searching)
        self.assertEqual(len(w.search_hits), 3)
        w.goto(2)
        self.wait_ready()
        self.assertEqual(w.canvas._page, 2)
        w.select_text([40, 220, 570, 310])
        self.wait_until(lambda: "Searchable" in w.selection_text)
        w.canvas.setFocus()
        w.copy_text()
        self.assertIn("Searchable", self.app.clipboard().text())
        w.goto(0)
        self.wait_ready()
        self.assertIn("cached", w.performance.text())

    def test_rapid_wheel_navigation_with_real_worker(self):
        w = self.window
        viewport = w.canvas.viewport()
        center = viewport.rect().center()

        def wheel(delta):
            event = QWheelEvent(QPointF(center), QPointF(viewport.mapToGlobal(center)),
                                QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(viewport, event)

        wheel(-120)
        wheel(-120)
        self.assertEqual(w.page, 2)
        self.assertTrue(w.navigation_pending)
        self.assertFalse(w.tool_actions["text"].isEnabled())
        self.wait_ready()
        self.assertEqual(w.canvas._page, 2)
        self.assertTrue(w.tool_actions["text"].isEnabled())
        wheel(240)
        self.assertEqual(w.page, 0)
        self.wait_ready()
        self.assertEqual(w.canvas._page, 0)
        self.assertFalse(w.dirty)

    def test_mouse_annotation_undo_redo_save_and_reopen(self):
        w = self.window
        w.set_tool("rectangle")
        canvas = w.canvas
        start = canvas._to_screen(QPointF(45, 350)).toPoint()
        end = canvas._to_screen(QPointF(400, 430)).toPoint()
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas.viewport(), end, delay=20)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.wait_ready()
        self.assertEqual(len(w.annotations), 1)
        self.assertTrue(w.dirty)
        w.mutate("undo")
        self.wait_ready()
        self.assertEqual(len(w.annotations), 0)
        w.mutate("redo")
        self.wait_ready()
        self.assertEqual(len(w.annotations), 1)
        target = str(Path(self.temp.name) / "saved.pdf")
        with patch("gpdf.app.QFileDialog.getSaveFileName", return_value=(target, "PDF")):
            w.save(save_as=True)
        self.wait_ready()
        self.assertTrue(Path(target).exists())
        self.assertFalse(w.dirty)
        w._open(target)
        self.wait_ready()
        self.assertEqual(len(w.annotations), 1)
        import pymupdf
        with pymupdf.open(self.path) as original:
            self.assertEqual(len(list(original[0].annots() or [])), 0)

    def test_stale_sidebar_and_page_are_locked_until_rendered(self):
        w = self.window
        w.mutate("annotate", {"page": 0, "kind": "text", "rect": [45, 350, 360, 410], "text": "Review this section", "fontsize": 18, "color": "#245fbd"})
        self.wait_ready()
        xref = w.annotations[0]["xref"]
        w.select_annotation(xref)
        self.assertTrue(w.delete_action.isEnabled())
        w.cache.clear()  # Exercise an uncached page even with nearby prefetch.
        w.goto(1)
        self.assertEqual(w.annotations, [])
        self.assertEqual(w.annotation_list.count(), 0)
        self.assertFalse(w.canvas.isEnabled())
        self.assertFalse(w.delete_action.isEnabled())
        w.select_annotation(xref)
        self.assertIsNone(w.selected)
        self.wait_ready()
        self.assertEqual(len(w.annotations), 0)

    def test_facing_right_page_annotation_and_virtual_document(self):
        w = self.window
        w.layout_combo.setCurrentIndex(2)
        self.wait_ready()
        self.wait_until(lambda: 1 in w.canvas.rasters)
        w.set_tool("rectangle")
        rect = w.canvas.image_rect(1, w.canvas.rasters[1])
        start = (rect.topLeft() + QPointF(40, 50)).toPoint()
        end = (rect.topLeft() + QPointF(130, 140)).toPoint()
        QTest.mousePress(w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(w.canvas.viewport(), end)
        QTest.mouseRelease(w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.wait_ready()
        self.assertEqual(w.page, 1)
        self.assertEqual(len(w.annotations), 1)
        w.goto(0)
        self.wait_ready()
        self.assertEqual(w.annotations, [])
        w.canvas.configure_layout("facing_continuous", 100000, 1)
        w.canvas.jump_to(99998)
        self.assertIn(99998, w.canvas.visible_pages())
        self.assertLessEqual(len(w.canvas.visible_pages()), 6)
        w.canvas.configure_layout("facing", 3, w.scale)
        w.canvas.jump_to(0)
        w.dirty = False

    def test_reading_layouts_and_compact_footer(self):
        w = self.window
        for index in (1, 2, 3, 0):
            w.layout_combo.setCurrentIndex(index)
            self.wait_ready()
            self.assertEqual(w.canvas.layout_mode, w.layout_combo.currentData())
            self.assertLess(w.navigation_bar.height(), 60)
            if w.canvas.multipage:
                self.wait_until(lambda: all(p in w.canvas.rasters for p in w.canvas.visible_pages()))
                for page in w.canvas.visible_pages():
                    w.activate_page(page)
                    self.wait_ready()
                    self.assertEqual(w.canvas._page, page)
                    point = QPointF(100, 150)
                    restored = w.canvas._to_pdf(w.canvas._to_screen(point))
                    self.assertAlmostEqual(restored.x(), point.x())
                    self.assertAlmostEqual(restored.y(), point.y())
        w.layout_combo.setCurrentIndex(3)
        self.wait_ready()
        w.goto(2)
        self.wait_ready()
        self.assertIn(2, w.canvas.visible_pages())
        w.toggle_fullscreen()
        self.app.processEvents()
        self.assertFalse(w.statusBar().isVisible())
        self.assertLess(w.navigation_bar.height(), 60)
        folder = Path("artifacts")
        folder.mkdir(exist_ok=True)
        w.grab().save(str(folder / "facing-continuous-fullscreen.png"))
        w.toggle_fullscreen()
        self.app.processEvents()
        self.assertFalse(w.statusBar().isVisible())
        w.status_action.setChecked(True)
        self.assertTrue(w.statusBar().isVisible())
        w.status_action.setChecked(False)
        w.resize(1380, 900)
        w.goto(0)
        self.wait_ready()
        self.wait_until(lambda: all(p in w.canvas.rasters for p in w.canvas.visible_pages()))
        w.grab().save(str(folder / "facing-continuous.png"))
        self.assertFalse(w.menuBar().isVisible())
        self.assertFalse(w.sidebar.isVisible())
        self.assertLessEqual(w.controls_bar.height(), 40)
        w.resize(960, 620)
        self.wait_until(lambda: not w.resize_timer.isActive())
        self.wait_ready()
        self.assertTrue(w.page_input.isVisible())
        self.assertTrue(w.layout_combo.isVisible())
        self.assertTrue(w.zoom_combo.isVisible())
        self.assertLessEqual(w.controls_bar.height(), 40)
        self.wait_until(lambda: not w.bridge.active and not w.bridge.pending and not w.prefetch_timer.isActive())
        w.grab().save(str(folder / "compact-960.png"))

    def test_inline_text_live_edit_cancel_undo_and_save(self):
        w = self.window
        w.layout_combo.setCurrentIndex(3)
        self.wait_ready()
        w.activate_page(1)
        self.wait_ready()
        w.set_tool("text")
        canvas = w.canvas
        start = canvas._to_screen(QPointF(50, 330)).toPoint()
        end = canvas._to_screen(QPointF(400, 430)).toPoint()
        with patch("gpdf.app.TextDialog", side_effect=AssertionError("Text must edit on the page")):
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(canvas.viewport(), end)
            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
            editor = canvas.inline_editor
            self.assertIsNotNone(editor)
            self.assertEqual(editor.parent(), canvas.viewport())
            revision = w.revision
            QTest.keyClicks(editor, "Live on the page")
            self.assertEqual(editor.toPlainText(), "Live on the page")
            self.assertEqual(w.revision, revision)  # No worker edit per keystroke.
            self.assertEqual(w.annotations, [])
            w.font_size.setValue(20)
            self.assertEqual(canvas.inline_data["fontsize"], 20)
            w.zoom(1.15)
            self.wait_until(lambda: not w.zoom_timer.isActive())
            self.wait_ready()
            self.assertEqual(editor.toPlainText(), "Live on the page")
            self.assertEqual(editor.geometry(), canvas._screen_rect(canvas._as_rect(canvas.inline_data["rect"])).toAlignedRect())
            Path("artifacts").mkdir(exist_ok=True)
            w.grab().save("artifacts/inline-text.png")
            QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.wait_ready()
            self.assertEqual(w.revision, revision + 1)
            self.assertEqual(w.annotations[0]["text"], "Live on the page")
            xref = w.annotations[0]["xref"]
            w.set_tool("select")
            center = canvas._screen_rect(canvas._as_rect(w.annotations[0]["rect"])).center().toPoint()
            QTest.mouseDClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=center)
            self.assertIsNotNone(canvas.inline_editor)
            QTest.keyClicks(canvas.inline_editor, "Cancelled change")
            QTest.keyClick(canvas.inline_editor, Qt.Key.Key_Escape)
            self.assertIsNone(canvas.inline_editor)
            self.assertEqual(w.annotations[0]["text"], "Live on the page")
            w.edit_selected(xref)
            QTest.keyClicks(canvas.inline_editor, "Updated inline")
            QTest.keyClick(canvas.inline_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.wait_ready()
            self.assertEqual(w.annotations[0]["text"], "Updated inline")
            w.undo_redo("undo")
            self.wait_ready()
            self.assertEqual(w.annotations[0]["text"], "Live on the page")
            w.edit_selected(w.annotations[0]["xref"])
            QTest.keyClicks(canvas.inline_editor, "Saved from draft")
            target = str(Path(self.temp.name) / "inline-saved.pdf")
            with patch("gpdf.app.QFileDialog.getSaveFileName", return_value=(target, "PDF")):
                w.save(save_as=True)
                self.wait_until(lambda: w.path == target and not w.busy)
                self.wait_ready()
            w._open(target)
            self.wait_ready()
            w.goto(1)
            self.wait_ready()
            self.assertEqual(w.annotations[0]["text"], "Saved from draft")

    def test_inline_draft_navigation_and_failed_commit(self):
        w = self.window
        data = {"kind": "text", "rect": [50, 330, 400, 430]}
        w.add_annotation(data)
        QTest.keyClicks(w.canvas.inline_editor, "Keep this draft")
        def reject(command, payload, callback, **kwargs):
            self.assertEqual(command, "annotate")
            callback({"ok": False, "error": "Simulated failure"})
        with patch.object(w.bridge, "submit", side_effect=reject), patch.object(w, "_error"):
            w.goto(1)
        self.assertEqual(w.page, 0)
        self.assertEqual(w.canvas.inline_editor.toPlainText(), "Keep this draft")
        self.assertFalse(w.dirty)
        w.goto(1)
        self.wait_until(lambda: w.page == 1)
        self.wait_ready()
        self.assertIsNone(w.canvas.inline_editor)
        self.assertEqual(w.annotations, [])
        w.goto(0)
        self.wait_ready()
        self.assertEqual(w.annotations[0]["text"], "Keep this draft")
        w.dirty = False

    def test_font_resize_and_rendered_desktop(self):
        w = self.window
        w.mutate("annotate", {"page": 0, "kind": "text", "rect": [48, 380, 370, 445], "text": "A comment that stays with the PDF.", "fontsize": 14, "color": "#245fbd"})
        self.wait_ready()
        xref = w.annotations[0]["xref"]
        w.set_tool("select")
        w.select_annotation(xref)
        w.font_size.setValue(22)
        w.change_font_size()
        self.wait_ready()
        self.assertEqual(w.annotations[0]["fontsize"], 22)
        w.mutate("update_annotation", {"page": 0, "xref": xref, "rect": [48, 380, 530, 470]})
        self.wait_ready()
        self.assertAlmostEqual(w.annotations[0]["rect"][2], 530, delta=2)
        output = Path("tmp")
        output.mkdir(exist_ok=True)
        self.assertTrue(w.grab().save(str(output / "ui-review.png")))

    def test_cache_budget_and_lru_eviction(self):
        cache = PageCache(10)
        cache.put("a", {"samples": b"1234"})
        cache.put("b", {"samples": b"1234"})
        cache.get("a")
        cache.put("c", {"samples": b"1234"})
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.bytes_used, 8)
        cache.put("huge", {"samples": b"x" * 20})
        self.assertEqual(cache.bytes_used, 8)

    def _link_test_drive(self):
        client = MagicMock()
        document = SimpleNamespace(path=self.path, name="Research.pdf", file_id="selected")
        self.window.drive_document = document
        self.window.drive_client = self.window._drive_owner = client
        self.window._drive_clients.append(client)
        return client, document

    def test_drive_failed_upload_preserves_edits_and_retry(self):
        from gpdf.drive_auth import DriveError
        w = self.window
        client, document = self._link_test_drive()
        client.upload.side_effect = DriveError("Network unavailable")
        w.mutate("rotate", {"page": 0, "degrees": 90})
        self.wait_ready()
        after = MagicMock()
        with patch("gpdf.drive_ui.QMessageBox.warning") as warning:
            w.save(after=after)
            self.wait_until(lambda: not w.busy and w._drive_task is None)
        self.assertTrue(w.dirty)
        self.assertEqual(w.drive_document, document)
        self.assertTrue(Path(w.path).exists())
        self.assertFalse(after.called)
        warning.assert_called_once()
        client.upload.side_effect = None
        client.upload.return_value = {"id": "new-copy", "name": "Research edited.pdf"}
        with patch("gpdf.drive_ui.QMessageBox.information"):
            w.save()
            self.wait_until(lambda: not w.busy and w._drive_task is None)
        self.assertFalse(w.dirty)

    def test_reading_and_new_edits_during_drive_upload(self):
        w = self.window
        client, _ = self._link_test_drive()
        release = threading.Event()
        client.upload.side_effect = lambda *_: release.wait(5)
        try:
            with patch("gpdf.drive_ui.QMessageBox.information"):
                w.save()
                self.wait_until(lambda: w._drive_task is not None)
                self.assertFalse(w.save_action.isEnabled())
                w.goto(1)
                self.wait_ready()
                self.assertEqual(w.page, 1)
                w.mutate("rotate", {"page": 1, "degrees": 90})
                self.wait_ready()
                release.set()
                self.wait_until(lambda: w._drive_task is None)
                self.assertTrue(w.dirty)
        finally:
            release.set()

    def test_drive_save_as_detaches_and_menu_screenshot(self):
        w = self.window
        client, _ = self._link_test_drive()
        w._update_actions()
        menu = w.menuBar().actions()[0].menu()
        menu.popup(w.mapToGlobal(QPoint(10, 35)))
        self.app.processEvents()
        Path("artifacts").mkdir(exist_ok=True)
        self.assertTrue(menu.grab().save("artifacts/drive-file-menu.png"))
        menu.hide()
        # The test document stands in for a private folder; choose a sibling.
        with tempfile.TemporaryDirectory() as output:
            target = str(Path(output) / "local.pdf")
            with patch("gpdf.app.QFileDialog.getSaveFileName", return_value=(target, "")):
                w.save(save_as=True)
                self.wait_ready()
            self.assertIsNone(w.drive_document)
            self.assertIsNone(w.drive_client)
            self.assertEqual(w.path, target)
            client.upload.assert_not_called()
            client.close.assert_called_once()
            w._open(self.path)
            self.wait_ready()


if __name__ == "__main__":
    unittest.main()
