"""A lightweight, single-page PDF canvas.

Only the current page's RGB raster lives here. PDF parsing and rendering happen
outside the widget, so interaction and painting never open or inspect a PDF.
All public geometry is in the displayed page's PDF coordinates (points).
"""

from __future__ import annotations

import math
from bisect import bisect_left
import time
from typing import Any

from PyQt6.QtCore import QTimer, QEvent, QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QPlainTextEdit


class InlineTextEditor(QPlainTextEdit):
    """A page-local text field; Enter adds lines, Ctrl+Enter accepts."""

    accepted = pyqtSignal()
    cancelled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.accepted.emit()
            return
        super().keyPressEvent(event)


class PageCanvas(QAbstractScrollArea):
    inlineTextCommitted = pyqtSignal(dict)
    annotationRequested = pyqtSignal(dict)
    annotationSelected = pyqtSignal(object)
    annotationGeometryChanged = pyqtSignal(dict)
    annotationEditRequested = pyqtSignal(int)
    textSelectionRequested = pyqtSignal(list)
    zoomRequested = pyqtSignal(float)
    pageStepRequested = pyqtSignal(int)
    fileDropped = pyqtSignal(str)

    MARGIN = 8
    HANDLE_SIZE = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pdfCanvas")
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.horizontalScrollBar().setSingleStep(36)
        self.verticalScrollBar().setSingleStep(52)
        self.inline_editor = None
        self.inline_data = None
        self._image = QImage()
        self._page = None
        self._page_width = 0.0
        self._page_height = 0.0
        self._scale = 1.0
        self._annotations: list[dict[str, Any]] = []
        self._selected = None
        self._tool = "select"
        self._color = "#f6ca45"
        self._stroke_width = 2.0
        self._search_rects: list[QRectF] = []
        self._text_selection: QRectF | None = None
        self._drag_mode: str | None = None
        self._start = QPointF()
        self._current = QPointF()
        self._hand_start = QPointF()
        self._hand_scroll = QPoint()
        self._original_rect: QRectF | None = None
        self._preview_rect: QRectF | None = None
        self._ink_points: list[QPointF] = []
        self._edge_scroll = 0.0
        self._edge_direction = 0
        self._edge_source = None
        self._last_edge_event = 0.0
        self._navigation_pending = False
        self._update_cursor()

    def begin_inline_text(self, data: dict) -> None:
        """Edit a draft at its PDF coordinates without calling the worker."""
        self.cancel_inline_text()
        self._cancel_drag()
        self.inline_data = dict(data)
        editor = InlineTextEditor(self.viewport())
        self.inline_editor = editor
        editor.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        editor.document().setDocumentMargin(1)
        editor.setPlainText(data.get("text", ""))
        editor.setToolTip("Type on the page. Ctrl+Enter or click outside to finish; Esc to cancel.")
        editor.accepted.connect(self.finish_inline_text)
        editor.cancelled.connect(self.cancel_inline_text)
        self._update_inline_geometry()
        editor.show()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        editor.selectAll()

    def _update_inline_geometry(self) -> None:
        if self.inline_editor is None:
            return
        rect = self._screen_rect(self._as_rect(self.inline_data["rect"]))
        self.inline_editor.setGeometry(rect.toAlignedRect())
        font = QFont("Arial")
        font.setPixelSize(max(1, round(self.inline_data.get("fontsize", 14) * self._ratios()[1])))
        self.inline_editor.setFont(font)
        color = self.inline_data.get("color", "#245fbd")
        self.inline_editor.setStyleSheet(f"QPlainTextEdit {{ background: white; color: {color}; font-family: Arial; font-size: {font.pixelSize()}px; border: 1px solid #376fe3; border-radius: 0; padding: 0; }}")
        self.inline_editor.raise_()

    def update_inline_style(self, **values) -> None:
        """Preview text size and color immediately on the page."""
        if self.inline_data is not None:
            self.inline_data.update(values)
            self._update_inline_geometry()

    def cancel_inline_text(self) -> None:
        """Discard a draft without modifying the PDF."""
        if self.inline_editor is not None:
            editor = self.inline_editor
            self.inline_editor = self.inline_data = None
            editor.hide()
            editor.deleteLater()
            self.setFocus()

    def finish_inline_text(self) -> None:
        """Send one completed edit, preserving a single undo operation."""
        if self.inline_editor is None:
            return
        data = {**self.inline_data, "text": self.inline_editor.toPlainText()}
        self.cancel_inline_text()
        self.inlineTextCommitted.emit(data)

    @staticmethod
    def _as_rect(value) -> QRectF:
        if isinstance(value, QRectF):
            return QRectF(value)
        if isinstance(value, dict):
            value = value.get("rect", [])
        try:
            x0, y0, x1, y1 = value
            return QRectF(QPointF(float(x0), float(y0)),
                          QPointF(float(x1), float(y1))).normalized()
        except (TypeError, ValueError):
            return QRectF()

    @staticmethod
    def _rect_list(rect: QRectF) -> list[float]:
        return [rect.left(), rect.top(), rect.right(), rect.bottom()]

    def set_page(self, result: dict) -> None:
        """Install an RGB rendering, preserving position for same-page zooms."""
        old_page = self._page
        old_center = self._to_pdf(QPointF(self.viewport().rect().center()), clamp=False)
        had_image = not self._image.isNull()
        width = int(result["width"])
        height = int(result["height"])
        stride = int(result.get("stride", width * 3))
        # Detach from the worker's bytes before its result can be released.
        self._image = QImage(result["samples"], width, height, stride,
                             QImage.Format.Format_RGB888).copy()
        self._page = result.get("page", 0)
        self._scale = max(0.001, float(result.get("scale", 1.0)))
        self._page_width = float(result.get("page_width", width / self._scale))
        self._page_height = float(result.get("page_height", height / self._scale))
        self._annotations = list(result.get("annotations", []))
        changed_page = old_page != self._page
        if changed_page:
            self._selected = None
            self._search_rects = []
            self._text_selection = None
        elif not any(a.get("xref") == self._selected for a in self._annotations):
            self._selected = None
        self._cancel_drag()
        self._update_scrollbars()
        if had_image and not changed_page:
            sx, sy = self._ratios()
            self.horizontalScrollBar().setValue(round(
                self.MARGIN + old_center.x() * sx - self.viewport().width() / 2))
            self.verticalScrollBar().setValue(round(
                self.MARGIN + old_center.y() * sy - self.viewport().height() / 2))
        else:
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)
        self.viewport().update()

    def clear(self) -> None:
        self.cancel_inline_text()
        self._image = QImage()
        self._page = None
        self._page_width = self._page_height = 0
        self._annotations = []
        self._selected = None
        self._search_rects = []
        self._text_selection = None
        if not self._navigation_pending:
            self._reset_edge_scroll()
        self._cancel_drag()
        self._update_scrollbars()
        self.viewport().update()

    def set_navigation_pending(self, pending: bool) -> None:
        """Allow wheel navigation while the requested page has no raster yet.

        Clear pending after installing a page to retain partial wheel movement.
        Clearing it with no raster resets the gesture for document changes.
        """
        self._navigation_pending = bool(pending)
        if not pending and self._image.isNull():
            self._reset_edge_scroll()

    def _reset_edge_scroll(self) -> None:
        self._edge_scroll = 0.0
        self._edge_direction = 0
        self._edge_source = None
        self._last_edge_event = 0.0

    def set_tool(self, tool: str) -> None:
        self._cancel_drag()
        self._tool = tool
        self._update_cursor()
        self.viewport().update()

    def set_color(self, color: str) -> None:
        if QColor(color).isValid():
            self._color = color

    def set_stroke_width(self, width: float) -> None:
        self._stroke_width = max(0.25, min(32.0, float(width)))

    def set_selected_annotation(self, xref) -> None:
        self._selected = xref
        self.viewport().update()

    def set_search_rects(self, rectangles: list) -> None:
        self._search_rects = [self._as_rect(rect) for rect in rectangles]
        self.viewport().update()

    def fit_scale(self, mode: str = "width") -> float:
        if self._page_width <= 0 or self._page_height <= 0:
            return 1.0
        available_width = max(50, self.viewport().width() - self.MARGIN * 2)
        scale = available_width / self._page_width
        if mode == "page":
            scale = min(scale, max(50, self.viewport().height() - self.MARGIN * 2)
                        / self._page_height)
        return max(0.05, min(8.0, scale))

    def sizeHint(self) -> QSize:
        return QSize(900, 720)

    def _ratios(self) -> tuple[float, float]:
        return (self._image.width() / self._page_width if self._page_width else 1.0,
                self._image.height() / self._page_height if self._page_height else 1.0)

    def _page_rect(self) -> QRectF:
        vw, vh = self.viewport().width(), self.viewport().height()
        iw, ih = self._image.width(), self._image.height()
        return QRectF(max(self.MARGIN, (vw - iw) / 2) - self.horizontalScrollBar().value(),
                      max(self.MARGIN, (vh - ih) / 2) - self.verticalScrollBar().value(),
                      iw, ih)

    def _to_pdf(self, point: QPointF, clamp: bool = True) -> QPointF:
        page = self._page_rect()
        sx, sy = self._ratios()
        x, y = (point.x() - page.x()) / sx, (point.y() - page.y()) / sy
        if clamp:
            x = max(0.0, min(self._page_width, x))
            y = max(0.0, min(self._page_height, y))
        return QPointF(x, y)

    def _to_screen(self, point: QPointF) -> QPointF:
        page = self._page_rect()
        sx, sy = self._ratios()
        return QPointF(page.x() + point.x() * sx, page.y() + point.y() * sy)

    def _screen_rect(self, rect: QRectF) -> QRectF:
        return QRectF(self._to_screen(rect.topLeft()), self._to_screen(rect.bottomRight()))

    def _update_scrollbars(self) -> None:
        if self._image.isNull():
            self.horizontalScrollBar().setRange(0, 0)
            self.verticalScrollBar().setRange(0, 0)
            return
        self.horizontalScrollBar().setPageStep(self.viewport().width())
        self.verticalScrollBar().setPageStep(self.viewport().height())
        self.horizontalScrollBar().setRange(
            0, max(0, self._image.width() + self.MARGIN * 2 - self.viewport().width()))
        self.verticalScrollBar().setRange(
            0, max(0, self._image.height() + self.MARGIN * 2 - self.viewport().height()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scrollbars()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            # A file operation can disable the canvas while a mouse button is
            # held. Never complete that old gesture after it becomes enabled.
            self._cancel_drag()
            self.viewport().update()
        super().changeEvent(event)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        # Repaint the viewport only. Qt clips raster drawing to the exposed area.
        self.viewport().update()

    def _selected_item(self):
        return next((a for a in self._annotations if a.get("xref") == self._selected), None)

    @staticmethod
    def _can_change_rect(annotation) -> bool:
        return annotation is not None and annotation.get("kind") in {
            "text", "note", "rectangle", "ellipse"}

    def _hit_annotation(self, point: QPointF):
        sx, sy = self._ratios()
        # Thin lines and tiny sticky-note icons remain easy to select at low zoom.
        for annotation in reversed(self._annotations):
            rect = self._as_rect(annotation.get("rect", []))
            if rect.adjusted(-4 / sx, -4 / sy, 4 / sx, 4 / sy).contains(point):
                return annotation
        return None

    def _resize_handle(self, rect: QRectF) -> QRectF:
        corner = self._screen_rect(rect).bottomRight()
        half = self.HANDLE_SIZE / 2
        return QRectF(corner.x() - half, corner.y() - half,
                      self.HANDLE_SIZE, self.HANDLE_SIZE)

    def _update_cursor(self) -> None:
        if self._drag_mode == "hand":
            cursor = Qt.CursorShape.ClosedHandCursor
        elif self._tool == "hand":
            cursor = Qt.CursorShape.OpenHandCursor
        elif self._tool == "select":
            cursor = Qt.CursorShape.ArrowCursor
        elif self._tool in ("text", "note"):
            cursor = Qt.CursorShape.IBeamCursor
        else:
            cursor = Qt.CursorShape.CrossCursor
        self.viewport().setCursor(cursor)

    def _cancel_drag(self) -> None:
        self._drag_mode = None
        self._original_rect = None
        self._preview_rect = None
        self._ink_points = []
        self._update_cursor()

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor("#e7eaf0"))
        if self._image.isNull():
            self._paint_empty(painter)
            return
        page = self._page_rect()
        painter.fillRect(page.adjusted(-2, 1, 3, 5), QColor(30, 40, 60, 20))
        painter.fillRect(page.adjusted(-1, 0, 1, 2), QColor(30, 40, 60, 18))
        painter.drawImage(page.topLeft(), self._image)
        painter.save()
        painter.setClipRect(page)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 178, 31, 90))
        for rect in self._search_rects:
            painter.drawRect(self._screen_rect(rect))
        if self._text_selection is not None:
            painter.setBrush(QColor(57, 117, 231, 55))
            painter.drawRect(self._screen_rect(self._text_selection))
        self._paint_preview(painter)
        painter.restore()
        annotation = self._selected_item()
        if annotation is not None:
            rect = self._preview_rect if self._drag_mode in ("move", "resize") else None
            rect = rect or self._as_rect(annotation.get("rect", []))
            pen = QPen(QColor("#376fe3"), 1.5, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._screen_rect(rect))
            if self._can_change_rect(annotation):
                painter.setPen(QPen(QColor("#376fe3"), 1.0))
                painter.setBrush(QColor("#ffffff"))
                painter.drawRect(self._resize_handle(rect))

    def _paint_empty(self, painter: QPainter) -> None:
        rect = self.viewport().rect()
        cx, cy = rect.center().x(), rect.center().y()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon = QRectF(cx - 27, cy - 126, 54, 68)
        painter.setBrush(QColor("#f8faff"))
        painter.setPen(QPen(QColor("#9baac4"), 1.7))
        painter.drawRoundedRect(icon, 5, 5)
        painter.setPen(QPen(QColor("#aebbd0"), 2.5))
        for offset in (22, 33, 44):
            painter.drawLine(QPointF(icon.x() + 12, icon.y() + offset),
                             QPointF(icon.right() - 12, icon.y() + offset))
        painter.setPen(QColor("#29364d"))
        painter.setFont(QFont("Segoe UI", 21, QFont.Weight.DemiBold))
        painter.drawText(QRectF(20, cy - 35, rect.width() - 40, 38),
                         Qt.AlignmentFlag.AlignCenter,
                         "Loading page…" if self._navigation_pending else "Your PDFs, at your pace.")
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor("#65738a"))
        painter.drawText(QRectF(20, cy + 11, rect.width() - 40, 28),
                         Qt.AlignmentFlag.AlignCenter,
                         "Keep scrolling to move through pages" if self._navigation_pending
                         else "Drop a PDF here, or open a document to begin.")
        painter.setFont(QFont("Segoe UI", 10))
        painter.setPen(QColor("#7b879b"))
        painter.drawText(QRectF(20, cy + 52, rect.width() - 40, 28),
                         Qt.AlignmentFlag.AlignCenter, "Ctrl + O  Open     ·     Ctrl + wheel  Zoom")

    def _paint_preview(self, painter: QPainter) -> None:
        if self._drag_mode != "create":
            return
        color = QColor(self._color)
        sx, sy = self._ratios()
        pen = QPen(color, max(1.0, self._stroke_width * (sx + sy) / 2))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start, end = self._to_screen(self._start), self._to_screen(self._current)
        rect = QRectF(start, end).normalized()
        if self._tool == "ink":
            if not self._ink_points:
                return
            path = QPainterPath(self._to_screen(self._ink_points[0]))
            for point in self._ink_points[1:]:
                path.lineTo(self._to_screen(point))
            painter.drawPath(path)
        elif self._tool == "arrow":
            painter.drawLine(start, end)
            angle = math.atan2(end.y() - start.y(), end.x() - start.x())
            length = max(10.0, pen.widthF() * 4)
            for offset in (-0.5, 0.5):
                painter.drawLine(end, QPointF(end.x() - length * math.cos(angle + offset),
                                             end.y() - length * math.sin(angle + offset)))
        elif self._tool == "ellipse":
            painter.drawEllipse(rect)
        elif self._tool == "highlight":
            color.setAlpha(95)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRect(rect)
        elif self._tool == "underline":
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        else:
            if self._tool in ("text", "note", "image"):
                painter.setPen(QPen(QColor("#376fe3"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(rect)

    def mousePressEvent(self, event) -> None:
        if self._image.isNull():
            super().mousePressEvent(event)
            return
        if self.inline_editor is not None:
            self.finish_inline_text()
            event.accept()
            return
        position = event.position()
        if event.button() == Qt.MouseButton.MiddleButton or (
                event.button() == Qt.MouseButton.LeftButton and self._tool == "hand"):
            self._drag_mode = "hand"
            self._hand_start = position
            self._hand_scroll = QPoint(self.horizontalScrollBar().value(), self.verticalScrollBar().value())
            self._update_cursor()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton or not self._page_rect().contains(position):
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._start = self._current = self._to_pdf(position)
        self._text_selection = None
        if self._tool == "select":
            selected = self._selected_item()
            selected_rect = self._as_rect(selected.get("rect", [])) if selected else None
            if (self._can_change_rect(selected) and selected_rect is not None
                    and self._resize_handle(selected_rect).adjusted(-4, -4, 4, 4).contains(position)):
                self._drag_mode = "resize"
                self._original_rect = selected_rect
                self._preview_rect = QRectF(selected_rect)
            else:
                annotation = self._hit_annotation(self._start)
                self._selected = annotation.get("xref") if annotation else None
                self.annotationSelected.emit(self._selected)
                if self._can_change_rect(annotation):
                    self._drag_mode = "move"
                    self._original_rect = self._as_rect(annotation.get("rect", []))
                    self._preview_rect = QRectF(self._original_rect)
                elif annotation:
                    self._drag_mode = "annotation"
                else:
                    self._drag_mode = "selection"
        else:
            self._selected = None
            self.annotationSelected.emit(None)
            self._drag_mode = "create"
            self._ink_points = [QPointF(self._start)]
        self.viewport().update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        if self._drag_mode == "hand":
            delta = position - self._hand_start
            self.horizontalScrollBar().setValue(round(self._hand_scroll.x() - delta.x()))
            self.verticalScrollBar().setValue(round(self._hand_scroll.y() - delta.y()))
        elif self._drag_mode:
            self._current = self._to_pdf(position)
            if self._drag_mode == "move" and self._original_rect is not None:
                delta = self._current - self._start
                moved = self._original_rect.translated(delta)
                x = max(0.0, min(max(0.0, self._page_width - moved.width()), moved.x()))
                y = max(0.0, min(max(0.0, self._page_height - moved.height()), moved.y()))
                moved.moveTopLeft(QPointF(x, y))
                self._preview_rect = moved
            elif self._drag_mode == "resize" and self._original_rect is not None:
                self._preview_rect = QRectF(self._original_rect.topLeft(), QPointF(
                    min(self._page_width, max(self._original_rect.left() + 6, self._current.x())),
                    min(self._page_height, max(self._original_rect.top() + 6, self._current.y()))))
            elif self._drag_mode == "selection":
                self._text_selection = QRectF(self._start, self._current).normalized()
            elif self._drag_mode == "create" and self._tool == "ink":
                if not self._ink_points or (self._current - self._ink_points[-1]).manhattanLength() > 0.5:
                    self._ink_points.append(QPointF(self._current))
            self.viewport().update()
        elif self._tool == "select" and not self._image.isNull():
            selected = self._selected_item()
            hovered = self._hit_annotation(self._to_pdf(position, clamp=False))
            if (self._can_change_rect(selected)
                    and self._resize_handle(self._as_rect(selected.get("rect", []))).adjusted(-4, -4, 4, 4).contains(position)):
                self.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif self._can_change_rect(hovered):
                self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            elif hovered:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            elif self._page_rect().contains(position):
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            else:
                self._update_cursor()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if not self._drag_mode:
            super().mouseReleaseEvent(event)
            return
        mode = self._drag_mode
        if mode == "hand":
            self._cancel_drag()
            event.accept()
            return
        self._current = self._to_pdf(event.position())
        rect = QRectF(self._start, self._current).normalized()
        request = None
        geometry = None
        selection = None
        if mode in ("move", "resize") and self._preview_rect is not None:
            if self._preview_rect != self._original_rect:
                geometry = {"xref": self._selected, "rect": self._rect_list(self._preview_rect)}
        elif mode == "selection":
            if rect.width() > 1 and rect.height() > 1:
                self._text_selection = rect
                selection = self._rect_list(rect)
        elif mode == "create":
            request = self._creation_request(rect)
        # Clear transient state before emitting: receivers may replace the page or
        # open a modal editor synchronously.
        self._cancel_drag()
        self.viewport().update()
        if geometry is not None:
            self.annotationGeometryChanged.emit(geometry)
        if selection is not None:
            self.textSelectionRequested.emit(selection)
        if request is not None:
            self.annotationRequested.emit(request)
        event.accept()

    def _creation_request(self, rect: QRectF) -> dict | None:
        tool = self._tool
        if tool in ("text", "note", "image"):
            if rect.width() < 8 or rect.height() < 8:
                width, height = {"text": (210.0, 64.0), "note": (24.0, 24.0),
                                 "image": (180.0, 130.0)}[tool]
                width, height = min(width, self._page_width), min(height, self._page_height)
                rect = QRectF(min(self._start.x(), self._page_width - width),
                              min(self._start.y(), self._page_height - height), width, height)
        elif tool == "ink":
            if len(self._ink_points) < 2:
                return None
            xs, ys = [p.x() for p in self._ink_points], [p.y() for p in self._ink_points]
            rect = QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))
        elif tool == "arrow":
            if (self._current - self._start).manhattanLength() < 3:
                return None
        elif rect.width() < 2 or rect.height() < 2:
            return None
        points = [[p.x(), p.y()] for p in self._ink_points] if tool == "ink" else [
            [self._start.x(), self._start.y()], [self._current.x(), self._current.y()]]
        return {"kind": tool, "rect": self._rect_list(rect), "points": points,
                "color": self._color, "width": self._stroke_width}

    def mouseDoubleClickEvent(self, event) -> None:
        if self._tool in ("select", "text") and event.button() == Qt.MouseButton.LeftButton:
            item = self._hit_annotation(self._to_pdf(event.position(), clamp=False))
            if item is not None and item.get("xref") is not None:
                self._cancel_drag()
                self._selected = item["xref"]
                self.annotationSelected.emit(self._selected)
                self.annotationEditRequested.emit(int(self._selected))
                self.viewport().update()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._reset_edge_scroll()
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self.zoomRequested.emit(1.15 ** max(-3, min(3, delta / 120)))
            event.accept()
            return
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        movement = angle if not angle.isNull() else pixel
        source = "angle" if not angle.isNull() else "pixel"
        delta = movement.y()
        if (not delta or abs(movement.x()) > abs(delta)
                or event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._reset_edge_scroll()
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        direction = 1 if delta < 0 else -1
        at_edge = ((direction > 0 and bar.value() == bar.maximum()) or
                   (direction < 0 and bar.value() == bar.minimum()))
        now = time.monotonic()
        if (at_edge and (not self._image.isNull() or self._navigation_pending)):
            if (direction != self._edge_direction or source != self._edge_source
                    or now - self._last_edge_event > 0.6):
                self._edge_scroll = 0
            self._edge_direction = direction
            self._edge_source = source
            self._last_edge_event = now
            self._edge_scroll += abs(delta)
            # A normal wheel notch is 120 angle units. Pixel-only touchpads use
            # 120 pixels per page; retain smaller movements across render waits.
            steps = int(self._edge_scroll // 120)
            self._edge_scroll -= steps * 120
            if steps:
                self.pageStepRequested.emit(direction * steps)
            event.accept()
            return
        self._reset_edge_scroll()
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel_drag()
            self._text_selection = None
            self._selected = None
            self.annotationSelected.emit(None)
            self.viewport().update()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _dropped_pdf(event) -> str | None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                return url.toLocalFile()
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_pdf(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._dropped_pdf(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        path = self._dropped_pdf(event)
        if path:
            event.acceptProposedAction()
            self.fileDropped.emit(path)
        else:
            event.ignore()


class ReadingCanvas(PageCanvas):
    """Virtual page slots with a bounded raster working set."""

    PAGE_GAP = 4

    visiblePagesChanged = pyqtSignal()
    activePageChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        self.layout_mode = "single"
        self.page_count = 0
        self.layout_scale = 1.0
        self.rasters = {}
        self._page_heights = {}
        self._default_height = 792.0
        self._height_rows = []
        self._height_deltas = [0.0]
        super().__init__(parent)
        self.visibility_timer = QTimer(self)
        self.visibility_timer.setSingleShot(True)
        self.visibility_timer.timeout.connect(self.visiblePagesChanged)

    @property
    def multipage(self):
        return self.layout_mode != "single"

    @property
    def columns(self):
        return 2 if self.layout_mode.startswith("facing") else 1

    def configure_layout(self, mode: str, count: int, scale: float) -> None:
        """Set virtual document geometry without walking PDF pages."""
        changed = mode != self.layout_mode or count != self.page_count
        self.layout_mode, self.page_count, self.layout_scale = mode, count, scale
        if changed:
            self._rebuild_heights()
        self._update_scrollbars()
        self.viewport().update()

    def _rebuild_heights(self):
        self._height_rows = sorted({page // self.columns for page in self._page_heights if page < self.page_count})
        self._height_deltas = [0.0]
        for row in self._height_rows:
            self._height_deltas.append(self._height_deltas[-1] + self._row_height(row) - self._default_height)

    def _row_height(self, row):
        return max((self._page_heights.get(page, self._default_height)
                    for page in range(row * self.columns, min((row + 1) * self.columns, self.page_count))),
                   default=self._default_height)

    def _row_top(self, row):
        delta = self._height_deltas[bisect_left(self._height_rows, row)]
        return (row * self._default_height + delta) * self.layout_scale + row * self.PAGE_GAP

    def _row_at(self, y):
        low, high = 0, math.ceil(self.page_count / self.columns)
        while low < high:
            middle = (low + high) // 2
            if self._row_top(middle) <= y:
                low = middle + 1
            else:
                high = middle
        return max(0, low - 1)

    def _remember_geometry(self, result):
        height = 612 * result.get("page_height", result["height"]) / result.get("page_width", result["width"])
        page = result["page"]
        if self._page_heights.get(page) == height:
            return
        anchor = self._row_at(self.verticalScrollBar().value())
        offset = self.verticalScrollBar().value() - self._row_top(anchor)
        if not self._page_heights:
            self._default_height = height
        self._page_heights[page] = height
        self._rebuild_heights()
        self._update_scrollbars()
        if "continuous" in self.layout_mode:
            self.verticalScrollBar().setValue(round(self._row_top(anchor) + offset))
        if self.multipage:
            self.visibility_timer.start(20)

    def slot_rect(self, page):
        width = 612 * self.layout_scale
        height = self._page_heights.get(page, self._default_height) * self.layout_scale
        row = page // self.columns if "continuous" in self.layout_mode else 0
        total = self.columns * width + (self.columns - 1) * self.PAGE_GAP + 2 * self.MARGIN
        left = max(self.MARGIN, (self.viewport().width() - total) / 2 + self.MARGIN)
        return QRectF(left + page % self.columns * (width + self.PAGE_GAP) - self.horizontalScrollBar().value(),
                      self.MARGIN + self._row_top(row) - self.verticalScrollBar().value(), width, height)

    def visible_pages(self) -> list[int]:
        """Return only the rows intersecting the viewport."""
        if not self.page_count:
            return []
        if not self.multipage:
            return [self._page or 0]
        if "continuous" not in self.layout_mode:
            first = (self._page or 0) // self.columns * self.columns
            return list(range(first, min(first + self.columns, self.page_count)))
        first = self._row_at(self.verticalScrollBar().value() - self.MARGIN)
        last = self._row_at(self.verticalScrollBar().value() + self.viewport().height() - self.MARGIN)
        return list(range(first * self.columns, min((last + 1) * self.columns, self.page_count)))

    def jump_to(self, page: int) -> None:
        """Scroll directly to a page or facing spread."""
        self._page = page
        if "continuous" in self.layout_mode:
            self.verticalScrollBar().setValue(round(self._row_top(page // self.columns)))
        self.viewport().update()

    def install_raster(self, result: dict) -> None:
        """Install a worker raster within the 64 MiB canvas budget."""
        self._remember_geometry(result)
        existing = self.rasters.get(result["page"])
        if existing is not None and existing.width() == result["width"] and existing.height() == result["height"]:
            return
        image = QImage(result["samples"], result["width"], result["height"], result.get("stride", result["width"] * 3), QImage.Format.Format_RGB888).copy()
        self.rasters[result["page"]] = image
        size = sum(image.sizeInBytes() for image in self.rasters.values())
        for page in sorted(self.rasters, key=lambda p: abs(p - (self._page or 0)), reverse=True):
            if size <= 64 * 1024 * 1024:
                break
            if page != self._page:
                size -= self.rasters.pop(page).sizeInBytes()
        self.viewport().update()

    def set_page(self, result):
        self._remember_geometry(result)
        position = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        super().set_page(result)
        if self.multipage:
            self.install_raster(result)
            self.horizontalScrollBar().setValue(position[0])
            self.verticalScrollBar().setValue(position[1])
        self._update_inline_geometry()

    def clear(self):
        self.rasters.clear()
        self._page_heights.clear()
        self._default_height = 792.0
        self._height_rows = []
        self._height_deltas = [0.0]
        super().clear()

    def _page_rect(self):
        if not self.multipage:
            return super()._page_rect()
        return self.image_rect(self._page or 0, self._image)

    def image_rect(self, page, image):
        slot = self.slot_rect(page)
        if image.isNull():
            return slot
        factor = min(slot.width() / image.width(), slot.height() / image.height())
        return QRectF(slot.x(), slot.y(), image.width() * factor, image.height() * factor)

    def _ratios(self):
        if not self.multipage:
            return super()._ratios()
        rect = self._page_rect()
        return rect.width() / (self._page_width or 1), rect.height() / (self._page_height or 1)

    def fit_scale(self, mode="width"):
        if not self.multipage:
            return super().fit_scale(mode)
        scale = max(50, self.viewport().width() - 2 * self.MARGIN - (self.columns - 1) * self.PAGE_GAP) / (612 * self.columns)
        if mode == "page":
            scale = min(scale, max(50, self.viewport().height() - 2 * self.MARGIN) / self._row_height((self._page or 0) // self.columns))
        return max(.05, min(8, scale))

    def _update_scrollbars(self):
        if not self.multipage or not self.page_count:
            return super()._update_scrollbars()
        rows = math.ceil(self.page_count / self.columns) if "continuous" in self.layout_mode else 1
        width = self.columns * 612 * self.layout_scale + (self.columns - 1) * self.PAGE_GAP + 2 * self.MARGIN
        height = (self._row_top(rows) - self.PAGE_GAP if "continuous" in self.layout_mode
                  else self._row_height((self._page or 0) // self.columns) * self.layout_scale) + 2 * self.MARGIN
        for bar, extent, viewport in ((self.horizontalScrollBar(), width, self.viewport().width()), (self.verticalScrollBar(), height, self.viewport().height())):
            bar.setPageStep(viewport)
            bar.setRange(0, max(0, round(extent - viewport)))

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._update_inline_geometry()
        if self.multipage and hasattr(self, "visibility_timer"):
            self.visibility_timer.start(20)

    def paintEvent(self, event):
        if not self.multipage:
            return super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor("#e7eaf0"))
        for page in self.visible_pages():
            image = self.rasters.get(page, QImage())
            rect = self.image_rect(page, image)
            painter.fillRect(rect, QColor("white"))
            if image.isNull():
                painter.setPen(QColor("#65738a"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"Page {page + 1}")
            else:
                painter.drawImage(rect, image)
        if not self._image.isNull() and not self._navigation_pending:
            painter.setClipRect(self._page_rect())
            painter.setBrush(QColor(255, 178, 31, 90))
            painter.setPen(Qt.PenStyle.NoPen)
            for rect in self._search_rects:
                painter.drawRect(self._screen_rect(rect))
            if self._text_selection is not None:
                painter.setBrush(QColor(57, 117, 231, 55))
                painter.drawRect(self._screen_rect(self._text_selection))
            self._paint_preview(painter)
            selected = self._selected_item()
            if selected:
                rect = self._preview_rect or self._as_rect(selected["rect"])
                painter.setPen(QPen(QColor("#376fe3"), 1.5, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self._screen_rect(rect))
                if self._can_change_rect(selected):
                    painter.drawRect(self._resize_handle(rect))

    def mousePressEvent(self, event):
        if self.inline_editor is not None:
            self.finish_inline_text()
            event.accept()
            return
        if self._navigation_pending:
            return
        if self.multipage:
            for page in self.visible_pages():
                if self.slot_rect(page).contains(event.position()) and page != self._page:
                    self.activePageChanged.emit(page)
                    if self._navigation_pending:
                        return
                    break
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        if "continuous" in self.layout_mode and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            QAbstractScrollArea.wheelEvent(self, event)
        else:
            super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._navigation_pending:
            return
        if self.multipage and not self._page_rect().contains(event.position()):
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_inline_geometry()
        if self.multipage and hasattr(self, "visibility_timer"):
            self.visibility_timer.start(20)
