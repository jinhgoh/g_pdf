"""User-gesture regressions for the canvas, without a PDF worker process."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from gpdf.canvas import PageCanvas


class CanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = PageCanvas()
        self.canvas.resize(900, 700)
        self.canvas.show()
        self.app.processEvents()
        self.requests = []
        self.geometry = []
        self.selections = []
        self.canvas.annotationRequested.connect(self.requests.append)
        self.canvas.annotationGeometryChanged.connect(self.geometry.append)
        self.canvas.textSelectionRequested.connect(self.selections.append)
        self.canvas.set_page(self.page())
        self.app.processEvents()

    def tearDown(self):
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()

    @staticmethod
    def page(number=0, scale=1, annotations=None):
        width, height = int(600 * scale), int(800 * scale)
        return {"page": number, "scale": scale, "width": width,
                "height": height, "stride": width * 3,
                "samples": bytes([255]) * width * height * 3,
                "page_width": 600, "page_height": 800,
                "annotations": annotations or []}

    def screen(self, x, y):
        return self.canvas._to_screen(QPointF(x, y)).toPoint()

    def drag(self, start, end):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.screen(*start))
        QTest.mouseMove(self.canvas.viewport(), self.screen(*end))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.screen(*end))

    def assertRectAlmostEqual(self, actual, expected):
        self.assertEqual(len(actual), 4)
        for found, wanted in zip(actual, expected):
            self.assertAlmostEqual(found, wanted, delta=1)

    def test_coordinates_with_scroll_and_zoom_are_pdf_points(self):
        self.canvas.set_page(self.page(scale=2))
        self.app.processEvents()
        self.canvas.horizontalScrollBar().setValue(120)
        self.canvas.verticalScrollBar().setValue(170)
        self.canvas.set_tool("rectangle")
        self.drag((120, 140), (220, 210))
        self.assertRectAlmostEqual(self.requests[-1]["rect"], [120, 140, 220, 210])

    def test_arrow_preserves_direction_when_dragged_up_and_left(self):
        self.canvas.set_tool("arrow")
        self.drag((220, 210), (120, 140))
        request = self.requests[-1]
        self.assertRectAlmostEqual(request["rect"], [120, 140, 220, 210])
        self.assertEqual(request["points"], [[220, 210], [120, 140]])

    def test_click_text_creates_box_within_page_edges(self):
        self.canvas.set_tool("text")
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.screen(595, 400))
        self.assertEqual(self.requests[-1]["kind"], "text")
        self.assertRectAlmostEqual(self.requests[-1]["rect"], [390, 400, 600, 464])

    def test_empty_select_drag_requests_text_in_pdf_coordinates(self):
        self.canvas.set_tool("select")
        self.drag((40, 50), (180, 100))
        self.assertRectAlmostEqual(self.selections[-1], [40, 50, 180, 100])
        self.assertFalse(self.requests)
        self.assertFalse(self.geometry)

    def test_annotation_move_and_resize_report_native_xref(self):
        self.canvas.set_page(self.page(annotations=[
            {"xref": 12, "kind": "text", "rect": [40, 50, 180, 150]}]))
        self.canvas.set_tool("select")
        self.drag((70, 80), (100, 110))
        self.assertEqual(self.geometry[-1]["xref"], 12)
        self.assertRectAlmostEqual(self.geometry[-1]["rect"], [70, 80, 210, 180])
        # The worker applies geometry and returns a fresh raster/annotation list.
        self.canvas.set_page(self.page(annotations=[
            {"xref": 12, "kind": "text", "rect": [70, 80, 210, 180]}]))
        self.canvas.set_selected_annotation(12)
        self.drag((210, 180), (250, 240))
        self.assertRectAlmostEqual(self.geometry[-1]["rect"], [70, 80, 250, 240])

    def test_replacing_page_cancels_gesture_before_release(self):
        self.canvas.set_tool("rectangle")
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.screen(40, 50))
        QTest.mouseMove(self.canvas.viewport(), self.screen(180, 160))
        self.canvas.set_page(self.page(number=1))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.screen(180, 160))
        self.assertFalse(self.requests)

    def test_quads_and_paths_select_without_unsupported_rect_edits(self):
        selected = []
        self.canvas.annotationSelected.connect(selected.append)
        self.canvas.set_tool("select")
        for kind in ("highlight", "underline", "arrow", "ink", "stamp"):
            with self.subTest(kind=kind):
                self.canvas.set_page(self.page(annotations=[
                    {"xref": 12, "kind": kind, "rect": [40, 50, 180, 150]}]))
                self.drag((70, 80), (100, 110))
                self.assertEqual(selected[-1], 12)
                self.assertFalse(self.geometry)
                # The bottom-right selection outline is not a resize handle.
                self.drag((180, 150), (220, 190))
                self.assertFalse(self.geometry)

    def test_disable_cancels_gesture_instead_of_applying_it_later(self):
        self.canvas.set_tool("rectangle")
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.screen(40, 50))
        QTest.mouseMove(self.canvas.viewport(), self.screen(180, 160))
        self.canvas.setEnabled(False)
        self.canvas.setEnabled(True)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.screen(180, 160))
        self.assertFalse(self.requests)

    def test_clear_removes_page_and_cancels_gesture(self):
        self.canvas.set_tool("rectangle")
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.screen(40, 50))
        self.canvas.clear()
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.canvas.viewport().rect().center())
        self.assertFalse(self.requests)
        self.assertEqual(self.canvas.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.canvas.horizontalScrollBar().maximum(), 0)
        self.assertTrue(self.canvas._image.isNull())


class _FakeBridge(QObject):
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.submitted = []
        self.stopped = False

    def submit(self, command, payload, callback, **options):
        self.submitted.append((command, payload, callback))
        return len(self.submitted)

    def cancel_pending(self, key=None):
        pass

    def shutdown(self):
        self.stopped = True


class WindowRenderSafetyTests(unittest.TestCase):
    """Slow-worker responses must never make an obsolete page editable."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from gpdf.app import MainWindow

        with patch("gpdf.app.PdfBridge", _FakeBridge):
            self.window = MainWindow()
        self.window.layout_combo.setCurrentIndex(0)  # Exercise single-page navigation.
        self.window.show()
        self.app.processEvents()
        self.window.resize_timer.stop()
        self.window.path = "fixture.pdf"
        self.window.session = 1
        self.window.page_count = 2
        self.window.scale = 1
        self.window.fit_mode = None
        self.window._display(self.render(), "test")

    def tearDown(self):
        self.window.resize_timer.stop()
        self.window.zoom_timer.stop()
        self.window.page_render_timer.stop()
        self.window._closing = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    @staticmethod
    def render(page=0):
        result = CanvasTests.page(page, annotations=[
            {"xref": 12 + page, "kind": "text", "rect": [40, 50, 180, 150],
             "text": "Comment", "fontsize": 14}])
        result["page_count"] = 2
        return result

    def respond(self, index=-1, result=None):
        self.window.bridge.submitted[index][2]({
            "ok": True, "result": self.render() if result is None else result})

    def test_page_navigation_disables_old_canvas_and_clears_sidebar(self):
        self.window.select_annotation(12)
        self.window.goto(1)
        self.assertFalse(self.window.canvas.isEnabled())
        self.assertFalse(self.window.annotation_list.isEnabled())
        self.assertEqual(self.window.annotation_list.count(), 0)
        self.window.select_annotation(12)
        self.assertIsNone(self.window.selected)
        self.respond(result=self.render(1))
        self.assertTrue(self.window.canvas.isEnabled())
        self.assertEqual(self.window.canvas._page, 1)

    def test_mutation_stays_noneditable_until_updated_render_arrives(self):
        self.window.mutate("rotate", {"page": 0, "degrees": 90})
        self.assertFalse(self.window.canvas.isEnabled())
        self.respond(result={"page_count": 2})
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.canvas.isEnabled())
        self.assertFalse(self.window.annotation_list.isEnabled())
        self.respond()
        self.assertTrue(self.window.canvas.isEnabled())
        self.assertEqual(self.window.display_identity, (1, 1, 0))

    def test_old_render_response_cannot_enable_the_wrong_page(self):
        self.window.goto(1)
        first = len(self.window.bridge.submitted) - 1
        self.window.goto(0)
        self.respond(first, self.render(1))
        self.assertFalse(self.window.canvas.isEnabled())
        self.assertIsNone(self.window.canvas._page)
        self.respond(result=self.render(0))
        self.assertTrue(self.window.canvas.isEnabled())
        self.assertEqual(self.window.canvas._page, 0)

    def test_fit_at_zoom_limit_does_not_schedule_endless_renders(self):
        self.window.fit_mode = "page"
        self.window.scale = 5
        with patch.object(self.window.canvas, "fit_scale", return_value=8):
            self.window._display(self.render(), "test")
        self.assertFalse(self.window.zoom_timer.isActive())

    def test_worker_failure_still_allows_window_to_close(self):
        with patch("gpdf.app.QMessageBox.critical"):
            self.window._worker_failed("Simulated process failure")
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.canvas.isEnabled())
        self.window.dirty = True
        self.window.close()
        self.app.processEvents()
        self.assertTrue(self.window.bridge.stopped)
        self.assertFalse(self.window.isVisible())

    def wheel(self, delta):
        viewport = self.window.canvas.viewport()
        local = viewport.rect().center()
        event = QWheelEvent(QPointF(local), QPointF(viewport.mapToGlobal(local)),
                            QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(viewport, event)

    def test_fast_wheel_burst_advances_during_loading_and_renders_latest(self):
        window = self.window
        window._state({"page_count": 20})
        window.canvas.verticalScrollBar().setValue(window.canvas.verticalScrollBar().maximum())
        self.wheel(-120)
        self.wheel(-120)
        self.wheel(-360)
        self.assertEqual(window.page, 5)
        self.assertEqual(window.page_input.value(), 6)
        self.assertTrue(window.canvas.isEnabled())
        self.assertTrue(window.canvas._image.isNull())
        self.assertFalse(window.annotation_list.isEnabled())
        self.assertFalse(window.tool_actions["text"].isEnabled())
        self.assertEqual(window.bridge.submitted, [])
        QTest.qWait(70)
        self.assertEqual(len(window.bridge.submitted), 1)
        self.assertEqual(window.bridge.submitted[0][1]["page"], 5)
        result = self.render(5)
        result["page_count"] = 20
        self.respond(result=result)
        self.assertEqual(window.canvas._page, 5)
        self.assertTrue(window.tool_actions["text"].isEnabled())

    def test_old_render_during_wheel_debounce_never_installs_wrong_page(self):
        window = self.window
        window._state({"page_count": 10})
        window.goto(1)
        old_render = len(window.bridge.submitted) - 1
        window.scroll_pages(3)
        self.respond(old_render, self.render(1))
        self.assertEqual(window.page, 4)
        self.assertIsNone(window.display_identity)
        self.assertIsNone(window.canvas._page)
        self.assertTrue(window.canvas.isEnabled())
        self.assertFalse(window.edit_action.isEnabled())

    def test_pending_wheel_navigation_has_no_editable_geometry(self):
        window = self.window
        window.scroll_pages(1)
        window.add_annotation({"kind": "rectangle", "rect": [10, 10, 40, 40]})
        window.select_annotation(12)
        window.delete_selected()
        self.assertIsNone(window.selected)
        self.assertFalse(window.bridge.submitted)
        self.assertIsNone(window.display_identity)

    def test_backwards_wheel_enters_page_at_bottom(self):
        window = self.window
        window.canvas.setFixedHeight(300)  # Keep the fixture taller than the reading area.
        self.app.processEvents()
        window.goto(1)
        self.respond(result=self.render(1))
        window.canvas.verticalScrollBar().setValue(0)
        self.wheel(120)
        window.request_render()
        self.respond(result=self.render(0))
        bar = window.canvas.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())

    def test_regular_navigation_cancels_delayed_wheel_request(self):
        window = self.window
        window.scroll_pages(1)
        self.assertTrue(window.page_render_timer.isActive())
        window.goto(0)
        self.assertFalse(window.page_render_timer.isActive())
        self.assertFalse(window.navigation_pending)
        QTest.qWait(70)
        self.assertEqual(len(window.bridge.submitted), 1)
        self.assertEqual(window.bridge.submitted[0][1]["page"], 0)

    def test_fast_wheel_stays_inside_document_bounds(self):
        window = self.window
        window.canvas.verticalScrollBar().setValue(window.canvas.verticalScrollBar().maximum())
        self.wheel(-12000)
        self.assertEqual(window.page, 1)
        self.wheel(-12000)
        self.assertEqual(window.page, 1)
        self.wheel(12000)
        self.assertEqual(window.page, 0)
        self.wheel(12000)
        self.assertEqual(window.page, 0)

    def test_failed_edit_resumes_wheel_navigation(self):
        window = self.window
        window.scroll_pages(1)
        window.mutate("rotate", {"page": 1, "degrees": 90})
        with patch.object(window, "_error"):
            window.bridge.submitted[-1][2]({"ok": False, "error": "Cannot edit"})
        self.assertEqual(window.bridge.submitted[-1][0], "render")
        self.assertEqual(window.bridge.submitted[-1][1]["page"], 1)
        self.respond(result=self.render(1))
        self.assertEqual(window.canvas._page, 1)

    def test_failed_save_resumes_wheel_navigation(self):
        window = self.window
        window.scroll_pages(1)
        window.save()
        with patch.object(window, "_error"):
            window.bridge.submitted[-1][2]({"ok": False, "error": "Destination locked"})
        self.assertEqual(window.bridge.submitted[-1][0], "render")
        self.assertEqual(window.bridge.submitted[-1][1]["page"], 1)

    def test_extraction_resumes_wheel_render_that_was_delayed(self):
        window = self.window
        window.scroll_pages(1)
        with patch("gpdf.app.QInputDialog.getText", return_value=("1", True)), \
                patch("gpdf.app.QFileDialog.getSaveFileName", return_value=("extract.pdf", "PDF")):
            window.extract_pages()
        window.request_render()  # Debounce expires while extraction is busy.
        self.assertEqual(window.bridge.submitted[-1][0], "extract")
        self.respond(result={})
        self.assertEqual(window.bridge.submitted[-1][0], "render")
        self.assertEqual(window.bridge.submitted[-1][1]["page"], 1)


if __name__ == "__main__":
    unittest.main()


class ReadingGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_landscape_gap_and_navigation(self):
        from gpdf.canvas import ReadingCanvas
        canvas = ReadingCanvas()
        canvas.resize(900, 700)
        canvas.configure_layout("facing_continuous", 10000, 1)
        canvas._remember_geometry({"page": 0, "width": 1600, "height": 900})
        self.assertAlmostEqual(canvas.slot_rect(2).top() - canvas.slot_rect(0).bottom(), 4)
        canvas.jump_to(5000)
        self.assertIn(5000, canvas.visible_pages())
        self.assertAlmostEqual(canvas.slot_rect(5000).top(), canvas.MARGIN)
        canvas.clear()
        self.assertEqual(canvas._default_height, 792)
        canvas.close()

    def test_mixed_sizes_use_tallest_page_in_spread(self):
        from gpdf.canvas import ReadingCanvas
        canvas = ReadingCanvas()
        canvas.configure_layout("facing_continuous", 6, 1)
        canvas._remember_geometry({"page": 0, "width": 1600, "height": 900})
        canvas._remember_geometry({"page": 1, "width": 612, "height": 792})
        self.assertAlmostEqual(canvas.slot_rect(2).top() - canvas.slot_rect(1).bottom(), 4)
        canvas.configure_layout("continuous", 6, 2)
        self.assertAlmostEqual(canvas.slot_rect(1).top() - canvas.slot_rect(0).bottom(), 4)
        self.assertAlmostEqual(canvas.slot_rect(2).top() - canvas.slot_rect(1).bottom(), 4)
        canvas.close()
