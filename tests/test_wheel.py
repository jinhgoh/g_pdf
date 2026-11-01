"""Mouse wheel navigation remains responsive across asynchronous page renders."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from gpdf.canvas import PageCanvas


class WheelNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = PageCanvas()
        self.canvas.resize(900, 700)
        self.canvas.show()
        self.app.processEvents()
        self.canvas.set_page(self.page())
        self.steps = []
        self.zooms = []
        self.canvas.pageStepRequested.connect(self.steps.append)
        self.canvas.zoomRequested.connect(self.zooms.append)
        self.clock = patch("gpdf.canvas.time.monotonic", return_value=10.0).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()

    @staticmethod
    def page(number=0, width=300, height=400):
        return {"page": number, "scale": 1, "width": width, "height": height,
                "samples": bytes([255]) * width * height * 3,
                "page_width": width, "page_height": height, "annotations": []}

    def wheel(self, delta=0, *, pixel=0, horizontal=0,
              modifiers=Qt.KeyboardModifier.NoModifier):
        position = QPointF(self.canvas.viewport().rect().center())
        event = QWheelEvent(position, position, QPoint(0, pixel),
                            QPoint(horizontal, delta), Qt.MouseButton.NoButton,
                            modifiers, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(self.canvas.viewport(), event)
        return event

    def test_fast_notches_all_advance_without_a_cooldown(self):
        for _ in range(8):
            self.wheel(-120)
        self.assertEqual(self.steps, [1] * 8)

    def test_combined_wheel_delta_advances_multiple_pages(self):
        self.wheel(-600)
        self.wheel(1200)
        self.assertEqual(self.steps, [5, -10])

    def test_fractional_notches_accumulate_and_retain_remainder(self):
        self.wheel(-90)
        self.assertEqual(self.steps, [])
        self.wheel(-60)
        self.assertEqual(self.steps, [1])
        self.wheel(-90)
        self.assertEqual(self.steps, [1, 1])

    def test_fraction_survives_render_completion(self):
        self.wheel(-150)
        self.canvas.set_navigation_pending(True)
        self.canvas.clear()
        self.canvas.set_page(self.page(number=1))
        self.canvas.set_navigation_pending(False)
        self.wheel(-90)
        self.assertEqual(self.steps, [1, 1])

    def test_direction_reversal_discards_previous_partial_notch(self):
        self.wheel(-90)
        self.wheel(90)
        self.assertEqual(self.steps, [])
        self.wheel(30)
        self.assertEqual(self.steps, [-1])

    def test_idle_gap_discards_previous_partial_notch(self):
        self.wheel(-90)
        self.clock.return_value += 0.61
        self.wheel(-30)
        self.assertEqual(self.steps, [])
        self.wheel(-90)
        self.assertEqual(self.steps, [1])

    def test_ctrl_wheel_keeps_zoom_behavior(self):
        self.wheel(-600, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.steps, [])
        self.assertEqual(self.zooms, [1.15 ** -3])

    def test_shift_and_horizontal_wheels_never_turn_pages(self):
        self.wheel(-600, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.wheel(horizontal=-600)
        self.wheel(-60, horizontal=-600)
        self.assertEqual(self.steps, [])

    def test_pixel_only_touchpad_accumulates_and_advances_multiple_pages(self):
        self.wheel(pixel=-60)
        self.assertEqual(self.steps, [])
        self.wheel(pixel=-300)
        self.assertEqual(self.steps, [3])
        self.wheel(pixel=120)
        self.assertEqual(self.steps, [3, -1])

    def test_angle_and_pixel_sources_do_not_mix_partial_units(self):
        self.wheel(-90)
        self.wheel(pixel=-60)
        self.assertEqual(self.steps, [])
        self.wheel(pixel=-60)
        self.assertEqual(self.steps, [1])

    def test_wheel_scrolls_inside_tall_page_before_turning_page(self):
        self.canvas.set_page(self.page(height=1800))
        self.app.processEvents()
        bar = self.canvas.verticalScrollBar()
        self.wheel(-120)
        self.assertGreater(bar.value(), 0)
        self.assertLess(bar.value(), bar.maximum())
        self.assertEqual(self.steps, [])
        bar.setValue(bar.maximum())
        self.wheel(-240)
        self.assertEqual(self.steps, [2])

    def test_blank_canvas_accepts_wheel_only_while_navigation_is_pending(self):
        self.canvas.clear()
        self.wheel(-600)
        self.assertEqual(self.steps, [])
        self.canvas.set_navigation_pending(True)
        self.wheel(-600)
        self.wheel(-120)
        self.assertEqual(self.steps, [5, 1])
        self.canvas.set_navigation_pending(False)
        self.wheel(-120)
        self.assertEqual(self.steps, [5, 1])

    def test_synchronous_render_clear_preserves_pending_wheel_remainder(self):
        def begin_loading(_):
            self.canvas.set_navigation_pending(True)
            self.canvas.clear()

        self.canvas.pageStepRequested.connect(begin_loading)
        self.wheel(-150)
        self.assertTrue(self.canvas._image.isNull())
        self.wheel(-90)
        self.assertEqual(self.steps, [1, 1])

    def test_new_document_reset_discards_partial_wheel_movement(self):
        self.wheel(-90)
        self.canvas.set_navigation_pending(True)
        self.canvas.clear()
        self.canvas.set_navigation_pending(False)
        self.canvas.set_page(self.page())
        self.wheel(-30)
        self.assertEqual(self.steps, [])

    def test_pending_blank_canvas_cannot_create_or_edit_annotations(self):
        requests = []
        edits = []
        self.canvas.annotationRequested.connect(requests.append)
        self.canvas.annotationGeometryChanged.connect(requests.append)
        self.canvas.annotationEditRequested.connect(edits.append)
        self.canvas.set_navigation_pending(True)
        self.canvas.clear()
        center = self.canvas.viewport().rect().center()
        for tool in ("text", "note", "rectangle", "arrow", "image", "select"):
            self.canvas.set_tool(tool)
            QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                             pos=center)
            QTest.mouseMove(self.canvas.viewport(), center + QPoint(80, 60))
            QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                               pos=center + QPoint(80, 60))
            QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                              pos=center)
        self.assertEqual(requests, [])
        self.assertEqual(edits, [])


if __name__ == "__main__":
    unittest.main()
