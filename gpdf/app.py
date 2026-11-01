"""Native desktop shell. All PDF work is delegated to PdfBridge."""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path
import sys
import time

from PyQt6.QtCore import QEvent, Qt, QTimer, QSize
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QToolButton, QMenu, QToolBar, QSpinBox, QDoubleSpinBox, QComboBox, QSplitter,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QLineEdit, QPlainTextEdit, QFileDialog, QMessageBox, QColorDialog,
    QDialog, QDialogButtonBox, QInputDialog, QSizePolicy,
)

from .bridge import PdfBridge, PageCache
from .canvas import ReadingCanvas
from .icons import icon
from .drive_ui import DriveWindowMixin


STYLE = """
QMainWindow, QDialog { background: #f8fafc; }
QWidget { font-family: 'Segoe UI'; font-size: 12px; color: #26354b; }
QMenuBar { background: #f8fafc; padding: 4px 10px; border-bottom: 1px solid #e3e8ef; }
QMenuBar::item { padding: 5px 10px; }
QMenuBar::item:selected, QMenu::item:selected { background: #e1eaf9; }
QMenu { background: white; padding: 5px; border: 1px solid #d8e0eb; }
QMenu::item { padding: 7px 28px; }
QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #e2e7ef; spacing: 2px; padding: 2px 4px; }
QToolButton { border: 1px solid transparent; border-radius: 5px; padding: 3px; background: transparent; }
QToolButton:hover { background: #eef3fb; }
QToolButton:checked { background: #e2ebfc; border-color: #a9c2ec; color: #205bba; }
QPushButton { background: #ffffff; border: 1px solid #d5ddea; border-radius: 5px; padding: 7px 12px; }
QPushButton:hover { background: #edf3fd; border-color: #adc4e8; }
QPushButton:disabled, QToolButton:disabled { color: #a1aaba; }
QPushButton#primary { background: #2864cb; color: white; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #2158b7; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit { background: white; border: 1px solid #d5ddea; border-radius: 4px; padding: 3px; selection-background-color: #b7cff8; }
QSpinBox, QDoubleSpinBox { padding-right: 12px; }
QTabWidget::pane { border: none; border-top: 1px solid #e2e7ef; }
QTabBar::tab { background: transparent; padding: 6px 10px; border-bottom: 2px solid transparent; color: #637189; }
QTabBar::tab:selected { color: #245fbd; border-bottom: 2px solid #2864cb; }
QTreeWidget, QListWidget { background: #f8fafc; border: none; outline: none; padding: 6px; }
QTreeWidget::item, QListWidget::item { padding: 8px 4px; border-radius: 4px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #e2ebfb; color: #245fbd; }
QScrollBar:vertical { background: #edf0f5; width: 11px; margin: 0; }
QScrollBar:horizontal { background: #edf0f5; height: 11px; margin: 0; }
QScrollBar::handle { background: #b7c1d0; border-radius: 4px; min-height: 26px; min-width: 26px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QStatusBar { background: #ffffff; border-top: 1px solid #dce3ec; }
QStatusBar QLabel { color: #6b778b; font-size: 11px; padding: 3px 8px; }
QSplitter::handle { background: #e2e7ef; width: 1px; }
QLabel#muted { color: #77849a; }
"""


class TextDialog(QDialog):
    def __init__(self, parent, text="", fontsize=14, title="Text comment"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(430, 290)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(text)
        self.editor.setPlaceholderText("Write your comment…")
        layout.addWidget(self.editor)
        row = QHBoxLayout()
        row.addWidget(QLabel("Font size"))
        self.size = QSpinBox()
        self.size.setRange(6, 144)
        self.size.setSuffix(" pt")
        self.size.setValue(round(fontsize or 14))
        row.addWidget(self.size)
        row.addStretch()
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.editor.setFocus()


class MainWindow(DriveWindowMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gpdf")
        self.resize(1380, 900)
        self.setMinimumSize(960, 620)
        self.bridge = PdfBridge(self)
        self.bridge.failed.connect(self._worker_failed)
        self.cache = PageCache()
        self.path = None
        self.page_count = 0
        self.page = 0
        self.scale = 1.25
        self.fit_mode = "page"
        self.revision = 0
        self.session = 0
        self.render_ticket = 0
        self.display_identity = None
        self.navigation_pending = False
        self.enter_page_bottom = False
        self.search_ticket = 0
        self.search_hits = {}
        self.annotations = []
        self.selected = None
        self.selection_text = ""
        self.dirty = False
        self.busy = False
        self.worker_dead = False
        self.color = "#efb900"
        self.tool = "hand"
        self.tool_colors = {"highlight": "#efb900", "underline": "#e05252", "text": "#245fbd", "note": "#efb900", "arrow": "#e05252", "rectangle": "#e05252", "ellipse": "#e05252", "ink": "#245fbd"}
        self.can_undo = self.can_redo = False
        self._closing = False
        self._inline_continuation = None
        self._after_inline_render = None
        self._init_drive()
        self._build_ui()
        self._build_menus()
        self.layout_combo.setCurrentIndex(self.layout_combo.findData("facing_continuous"))
        self.set_tool("hand")
        self.canvas.set_color(self.color)
        self._update_actions()
        self.setStyleSheet(STYLE)

    def _action(self, text, callback, shortcut=None, icon_name=None):
        action = QAction(icon(icon_name) if icon_name else icon("rectangle"), text, self)
        action.triggered.connect(callback)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        self.addAction(action)
        return action

    def _build_ui(self):
        self.open_action = self._action("Open PDF", self.open_dialog, "Ctrl+O", "open")
        self.drive_open_action = self._action("Open from Google Drive…", self.open_drive)
        self.drive_disconnect_action = self._action("Disconnect Google Drive", self.disconnect_drive)
        self.drive_cancel_action = self._action("Cancel Google Drive operation", self.cancel_drive)
        self.save_action = self._action("Save", self.save, "Ctrl+S", "save")
        self.save_as_action = self._action("Save as…", lambda: self.save(save_as=True), "Ctrl+Shift+S", "save")
        self.undo_action = self._action("Undo", lambda: self.undo_redo("undo"), "Ctrl+Z", "undo")
        self.redo_action = self._action("Redo", lambda: self.undo_redo("redo"), "Ctrl+Y", "redo")
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.delete_action = self._action("Delete selected annotation", self.delete_selected, "Delete", "trash")
        self.edit_action = self._action("Edit selected comment…", self.edit_selected, None, "text")
        self.copy_action = self._action("Copy selected text", self.copy_text, "Ctrl+C")
        self.find_action = self._action("Find in document", self.focus_search, "Ctrl+F", "search")
        self.rotate_action = self._action("Rotate page clockwise", lambda: self.mutate("rotate", {"page": self.page, "degrees": 90}), "Ctrl+R", "rotate")
        self.delete_page_action = self._action("Delete current page…", self.delete_page, None, "trash")
        self.merge_action = self._action("Append PDF…", self.merge_pdf, None, "plus")
        self.extract_action = self._action("Extract pages…", self.extract_pages, None, "open")
        self.prev_action = self._action("Previous page", lambda: self.goto(self.page - self.canvas.columns), "PgUp", "left")
        self.next_action = self._action("Next page", lambda: self.goto(self.page + self.canvas.columns), "PgDown", "right")
        self.zoom_in = self._action("Zoom in", lambda: self.zoom(1.25), "Ctrl+=", "plus")
        self.zoom_in.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.zoom_out = self._action("Zoom out", lambda: self.zoom(0.8), "Ctrl+-", "minus")
        self.fit_page_action = self._action("Fit page", lambda: self.fit("page"), "Ctrl+0")
        self.fit_width_action = self._action("Fit width", lambda: self.fit("width"), "Ctrl+1")

        filebar = QToolBar("Controls", self)
        self.controls_bar = filebar
        filebar.setMovable(False)
        filebar.setIconSize(QSize(18, 18))
        filebar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(filebar)
        self.menu_button = QToolButton()
        self.menu_button.setText("Gpdf")
        self.menu_button.setToolTip("File, edit, view and help menus")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.main_menu = QMenu(self)
        self.menu_button.setMenu(self.main_menu)
        filebar.addWidget(self.menu_button)
        filebar.addAction(self.open_action)
        filebar.addAction(self.save_action)
        filebar.addSeparator()
        self.doc_label = QLabel("Your local PDF workspace", self)
        self.doc_label.hide()
        editbar = filebar
        self.tool_actions = {}
        self.tool_group = QActionGroup(self)
        tools = [("hand", "Pan"), ("select", "Select / copy text"), ("highlight", "Highlight"), ("underline", "Underline"), ("text", "Text comment"), ("note", "Sticky note"), ("arrow", "Arrow"), ("rectangle", "Rectangle"), ("ellipse", "Ellipse"), ("ink", "Freehand mark"), ("image", "Place image")]
        for name, title in tools:
            action = QAction(icon(name), title, self)
            action.setCheckable(True)
            action.setToolTip(title)
            action.triggered.connect(lambda checked, n=name: self.set_tool(n))
            self.tool_group.addAction(action)
            self.tool_actions[name] = action
            editbar.addAction(action)
            if name in ("select", "note"):
                editbar.addSeparator()
        self.tool_actions["hand"].setChecked(True)
        editbar.addSeparator()
        self.color_button = QPushButton()
        self.color_button.setFixedSize(24, 24)
        self.color_button.setToolTip("Annotation color")
        self.color_button.clicked.connect(self.choose_color)
        self._color_swatch()
        editbar.addWidget(self.color_button)
        self.stroke_label = QLabel("Line")
        editbar.addWidget(self.stroke_label)
        self.stroke = QDoubleSpinBox()
        self.stroke.setRange(0.5, 20)
        self.stroke.setSingleStep(0.5)
        self.stroke.setValue(2)
        self.stroke.setSuffix(" pt")
        self.stroke.setFixedWidth(64)
        self.stroke.valueChanged.connect(self.change_width)
        editbar.addWidget(self.stroke)
        editbar.addWidget(QLabel("Text"))
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 144)
        self.font_size.setValue(14)
        self.font_size.setSuffix(" pt")
        self.font_size.setFixedWidth(60)
        self.font_size.editingFinished.connect(self.change_font_size)
        self.font_size.valueChanged.connect(lambda size: self.canvas.update_inline_style(fontsize=size))
        editbar.addWidget(self.font_size)
        editbar.addSeparator()
        editbar.addAction(self.undo_action)
        editbar.addAction(self.redo_action)
        editbar.addAction(self.delete_action)

        annotation_controls = list(filebar.actions())[4:]
        for action in annotation_controls:
            filebar.removeAction(action)

        self.drive_button = QToolButton()
        self.drive_button.setText("Google Drive")
        self.drive_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.drive_button.setToolTip("Connect with Google in your default browser and choose a PDF")
        self.drive_button.clicked.connect(self.drive_open_action.trigger)
        filebar.insertWidget(self.open_action, self.drive_button)

        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.drive_status = QLabel()
        self.drive_status.setTextFormat(Qt.TextFormat.PlainText)
        self.drive_status.setContentsMargins(12, 6, 12, 6)
        self.drive_status.hide()
        layout.addWidget(self.drive_status)
        splitter = QSplitter()
        self.sidebar = QTabWidget()
        self.sidebar.setMinimumWidth(240)
        self.sidebar.setMaximumWidth(480)
        self.outline = QTreeWidget()
        self.outline.setHeaderHidden(True)
        self.outline.itemClicked.connect(lambda item: self.goto(item.data(0, Qt.ItemDataRole.UserRole)))
        self.sidebar.addTab(self.outline, "Outline")
        find_panel = QWidget()
        find_layout = QVBoxLayout(find_panel)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search text or existing OCR…")
        self.search_input.returnPressed.connect(self.start_search)
        find_layout.addWidget(self.search_input)
        find_row = QHBoxLayout()
        self.search_button = QPushButton("Find all")
        self.search_button.clicked.connect(self.start_search)
        self.stop_search_button = QPushButton("Stop")
        self.stop_search_button.clicked.connect(self.stop_search)
        find_row.addWidget(self.search_button)
        find_row.addWidget(self.stop_search_button)
        find_layout.addLayout(find_row)
        self.search_status = QLabel("Search uses the PDF's existing text layer.")
        self.search_status.setWordWrap(True)
        self.search_status.setObjectName("muted")
        find_layout.addWidget(self.search_status)
        self.search_results = QListWidget()
        self.search_results.itemClicked.connect(lambda item: self.goto(item.data(Qt.ItemDataRole.UserRole)))
        find_layout.addWidget(self.search_results)
        self.sidebar.addTab(find_panel, "Find")
        notes_panel = QWidget()
        notes_layout = QVBoxLayout(notes_panel)
        heading = QLabel("ANNOTATIONS ON THIS PAGE")
        heading.setStyleSheet("font-size: 10px; letter-spacing: 1px; color: #78869a; padding: 5px;")
        notes_layout.addWidget(heading)
        self.annotation_list = QListWidget()
        self.annotation_list.itemClicked.connect(lambda item: self.select_annotation(item.data(Qt.ItemDataRole.UserRole)))
        self.annotation_list.itemDoubleClicked.connect(lambda item: self.edit_selected())
        notes_layout.addWidget(self.annotation_list)
        self.edit_note = QPushButton("Edit selected comment")
        self.edit_note.clicked.connect(self.edit_selected)
        notes_layout.addWidget(self.edit_note)
        self.sidebar.addTab(notes_panel, "Comments")
        splitter.addWidget(self.sidebar)
        self.canvas = ReadingCanvas()
        self.canvas.visiblePagesChanged.connect(self.refresh_visible_pages)
        self.canvas.activePageChanged.connect(self.activate_page)
        self.canvas.inlineTextCommitted.connect(self.commit_inline_text)
        self.canvas.annotationRequested.connect(self.add_annotation)
        self.canvas.annotationSelected.connect(self.select_annotation)
        self.canvas.annotationEditRequested.connect(lambda xref: self.edit_selected(xref))
        self.canvas.annotationGeometryChanged.connect(lambda data: self.mutate("update_annotation", {"page": self.page, **data}))
        self.canvas.textSelectionRequested.connect(self.select_text)
        self.canvas.zoomRequested.connect(self.zoom)
        self.canvas.pageStepRequested.connect(self.scroll_pages)
        self.canvas.fileDropped.connect(self.open_path)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 1080])
        layout.addWidget(splitter, 1)

        nav = QWidget()
        self.navigation_bar = nav
        nav.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        nav.setStyleSheet("background: #ffffff; QPushButton { padding: 3px; }")
        navrow = QHBoxLayout(nav)
        navrow.setContentsMargins(0, 0, 0, 0)
        navrow.setSpacing(3)
        for action in (self.prev_action, self.next_action):
            button = QPushButton(action.icon(), "")
            button.setFixedWidth(25)
            button.setToolTip(action.text())
            button.clicked.connect(action.trigger)
            navrow.addWidget(button)
        navrow.addWidget(QLabel("  Page"))
        self.page_input = QSpinBox()
        self.page_input.setRange(1, 1)
        self.page_input.setFixedWidth(60)
        self.page_input.editingFinished.connect(lambda: self.goto(self.page_input.value() - 1))
        navrow.addWidget(self.page_input)
        self.total_label = QLabel("/ —")
        navrow.addWidget(self.total_label)
        self.mode_hint = QLabel("Open a PDF to begin")
        self.mode_hint.setObjectName("muted")
        self.mode_hint.hide()
        self.layout_combo = QComboBox()
        for label, mode in [("Single page", "single"), ("Continuous", "continuous"), ("Facing", "facing"), ("Facing continuous", "facing_continuous")]:
            self.layout_combo.addItem(label, mode)
        self.layout_combo.setAccessibleName("Page layout")
        self.layout_combo.currentIndexChanged.connect(self.change_layout)
        navrow.addWidget(self.layout_combo)
        minus = QPushButton("−")
        minus.setFixedWidth(30)
        minus.clicked.connect(lambda: self.zoom(0.8))
        navrow.addWidget(minus)
        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.addItems(["Fit page", "Fit width", "50%", "75%", "100%", "125%", "150%", "200%", "300%"])
        self.zoom_combo.setFixedWidth(106)
        self.zoom_combo.activated.connect(self.zoom_changed)
        self.zoom_combo.lineEdit().returnPressed.connect(self.zoom_changed)
        navrow.addWidget(self.zoom_combo)
        plus = QPushButton("+")
        plus.setFixedWidth(30)
        plus.clicked.connect(lambda: self.zoom(1.25))
        navrow.addWidget(plus)
        filebar.addWidget(nav)
        filebar.addSeparator()
        for action in annotation_controls:
            filebar.addAction(action)
        self.setCentralWidget(center)
        self.performance = QLabel("On-demand rendering · 128 MB page cache")
        self.statusBar().addPermanentWidget(self.performance)
        self.statusBar().hide()
        self.sidebar.hide()
        self.statusBar().showMessage("Ready — open a PDF or drop it here")
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(lambda: self.fit(self.fit_mode) if self.path and self.fit_mode else None)
        self.zoom_timer = QTimer(self)
        self.zoom_timer.setSingleShot(True)
        self.zoom_timer.timeout.connect(self.request_render)
        self.prefetch_timer = QTimer(self)
        self.prefetch_timer.setSingleShot(True)
        self.prefetch_timer.timeout.connect(self.refresh_visible_pages)
        self.nearby_ticket = 0
        self.page_render_timer = QTimer(self)
        self.page_render_timer.setSingleShot(True)
        self.page_render_timer.timeout.connect(self.request_render)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("File")
        file_menu.addActions([self.open_action, self.save_action, self.save_as_action])
        file_menu.addSeparator()
        file_menu.addActions([self.drive_open_action, self.drive_disconnect_action, self.drive_cancel_action])
        file_menu.addSeparator()
        file_menu.addActions([self.merge_action, self.extract_action])
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)
        edit = self.menuBar().addMenu("Edit")
        edit.addActions([self.undo_action, self.redo_action, self.copy_action, self.find_action])
        edit.addSeparator()
        edit.addActions([self.edit_action, self.delete_action])
        view = self.menuBar().addMenu("View")
        view.addActions([self.fit_page_action, self.fit_width_action, self.zoom_in, self.zoom_out, self.prev_action, self.next_action])
        view.addAction("Toggle sidebar", QKeySequence("F9"), lambda: self.sidebar.setVisible(not self.sidebar.isVisible()))
        self.fullscreen_action = self._action("Fullscreen", self.toggle_fullscreen, "F11")
        view.addAction(self.fullscreen_action)
        layouts = view.addMenu("Page layout")
        for index in range(self.layout_combo.count()):
            layouts.addAction(self.layout_combo.itemText(index), lambda checked=False, i=index: self.layout_combo.setCurrentIndex(i))
        self.status_action = QAction("Show status bar", self)
        self.status_action.setCheckable(True)
        self.status_action.toggled.connect(lambda checked: self.statusBar().setVisible(checked and not self.isFullScreen()))
        view.addAction(self.status_action)
        pages = self.menuBar().addMenu("Pages")
        pages.addActions([self.rotate_action, self.delete_page_action, self.merge_action, self.extract_action])
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction("How to use Gpdf", self.show_help)
        help_menu.addAction("About", lambda: QMessageBox.about(self, "Gpdf", "Gpdf 0.1\nA native PDF workspace with optional Google Drive access.\n\nPowered by Qt and MuPDF.\nDrive transfers occur only when you choose to open or save a Drive PDF."))

        for action in self.menuBar().actions():
            self.main_menu.addMenu(action.menu())
        self.menuBar().hide()

    def _update_actions(self):
        ready = bool(self.path) and not self.busy and not self.worker_dead
        editable = ready and self.display_identity == (self.session, self.revision, self.page)
        for action in [self.save_action, self.save_as_action, self.rotate_action, self.delete_page_action, self.merge_action, self.extract_action, self.find_action, self.fit_page_action, self.fit_width_action, *self.tool_actions.values()]:
            action.setEnabled(ready)
        cloud_idle = self._drive_task is None
        self.open_action.setEnabled(not self.busy and not self.worker_dead and cloud_idle)
        self.drive_open_action.setEnabled(not self.busy and not self.worker_dead and cloud_idle)
        self.drive_button.setEnabled(self.drive_open_action.isEnabled())
        self.drive_disconnect_action.setEnabled(bool(self._drive_clients) and cloud_idle and not self.busy)
        self.drive_cancel_action.setEnabled(not cloud_idle)
        self.save_action.setEnabled(ready and cloud_idle)
        self.save_as_action.setEnabled(ready and cloud_idle)
        self.save_action.setText("Save edited copy to Google Drive" if self.drive_client else "Save")
        self.undo_action.setEnabled(ready and self.can_undo)
        self.redo_action.setEnabled(ready and self.can_redo)
        self.delete_action.setEnabled(editable and self.selected is not None)
        self.edit_action.setEnabled(editable and self.selected is not None)
        self.prev_action.setEnabled(ready and self.page > 0)
        self.next_action.setEnabled(ready and self.page < self.page_count - 1)
        # A blank navigation surface can keep receiving wheel input, while
        # annotation controls remain gated on the displayed page identity.
        self.canvas.setEnabled((editable or (ready and self.navigation_pending) or not self.path)
                               and not self.busy and not self.worker_dead)
        self.annotation_list.setEnabled(editable)
        self.edit_note.setEnabled(editable and self.selected is not None)
        for action in self.tool_actions.values():
            action.setEnabled(editable)
        self.page_input.setEnabled(ready)
        self.search_button.setEnabled(ready)
        self.font_size.setEnabled(editable)
        self.stroke.setEnabled(editable)
        self.color_button.setEnabled(editable)
        title = Path(self.path).name if self.path else "Your local PDF workspace"
        if self.drive_document:
            title = self.drive_document.name + (" · Google Drive" if self.drive_client else " · Disconnected")
        self.doc_label.setTextFormat(Qt.TextFormat.PlainText)
        self.doc_label.setText(title + ("  • Unsaved" if self.dirty else ""))
        self.setWindowTitle(f"{'* ' if self.dirty else ''}{title} — Gpdf" if self.path else "Gpdf")

    def _error(self, response):
        if response.get("result") and self.path:
            self._state(response["result"])
        QMessageBox.warning(self, "Gpdf", response.get("error", "The operation could not be completed."))
        self.statusBar().showMessage("Operation could not be completed", 8000)

    def _worker_failed(self, message):
        self.worker_dead = True
        self.busy = False
        self._update_actions()
        QMessageBox.critical(self, "PDF process stopped", message)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF documents (*.pdf)")
        if path:
            self.open_path(path)

    def _leave_document(self, continuation, on_cancel=None):
        if self.finish_inline_then(lambda: self._leave_document(continuation, on_cancel)):
            return
        if not self.dirty or self.worker_dead:
            continuation()
            return
        answer = QMessageBox.question(self, "Save your edits?", "This PDF has unsaved changes.", QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Save:
            self.save(after=continuation, on_failure=on_cancel)
        elif answer == QMessageBox.StandardButton.Discard:
            continuation()
        elif on_cancel:
            on_cancel()

    def open_path(self, path):
        if self.busy or self._drive_task:
            return
        self._leave_document(lambda: self._open(path))

    def _open(self, path, password="", drive=None):
        self.stop_search()
        self.page_render_timer.stop()
        self.zoom_timer.stop()
        self.render_ticket += 1
        self.bridge.cancel_pending("render")
        self.busy = True
        self._update_actions()
        self.statusBar().showMessage("Opening PDF…")
        def opened(response):
            self.busy = False
            if not response["ok"]:
                self._update_actions()
                if response.get("password_required"):
                    secret, ok = QInputDialog.getText(self, "Password required", "Document password:", QLineEdit.EchoMode.Password)
                    if ok:
                        self._open(path, secret, drive=drive)
                    elif drive:
                        self._dispose_drive_client(drive[0])
                else:
                    self._error(response)
                    if drive:
                        self._dispose_drive_client(drive[0])
                if self.path and not self.busy:
                    self.request_render()
                return
            result = response["result"]
            self._release_drive_document()
            if drive:
                self.drive_client, self.drive_document = drive
                self._drive_owner = drive[0]
            self.path = result.get("path", path)
            self.session += 1
            self.page_count = result["page_count"]
            self.page = 0
            self.revision = 0
            self.dirty = False
            self.fit_mode = "page"
            self.cache.clear()
            self.canvas.rasters.clear()
            self.navigation_pending = False
            self.enter_page_bottom = False
            self.canvas.set_navigation_pending(False)
            self.canvas.clear()
            self.display_identity = None
            self.annotations = []
            self.annotation_list.clear()
            self.selected = None
            self.selection_text = ""
            self.search_hits.clear()
            self.search_results.clear()
            self._state(result)
            self._outline(result.get("toc", []))
            self.statusBar().showMessage(f"Opened {self.page_count:,} pages — ready")
            self.request_render()
        self.bridge.submit("open", {"path": str(path), "password": password,
                                    "private_working_copy": drive is not None}, opened)

    def _outline(self, toc):
        self.outline.clear()
        parents = {}
        for entry in toc:
            level, title, page = entry[:3]
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, max(0, page - 1))
            parent = parents.get(level - 1)
            (parent.addChild if parent else self.outline.addTopLevelItem)(item)
            parents[level] = item
        if not toc:
            item = QTreeWidgetItem(["No bookmarks in this document"])
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.outline.addTopLevelItem(item)
        self.outline.expandToDepth(0)

    def _state(self, result):
        self.page_count = result.get("page_count", self.page_count)
        self.page = min(self.page, max(0, self.page_count - 1))
        self.can_undo = result.get("can_undo", self.can_undo)
        self.can_redo = result.get("can_redo", self.can_redo)
        self.page_input.setRange(1, max(1, self.page_count))
        self.page_input.setValue(self.page + 1)
        self.total_label.setText(f"/ {self.page_count:,}")
        if "toc" in result:
            self._outline(result["toc"])
        self._update_actions()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.statusBar().setVisible(not self.isFullScreen() and hasattr(self, "status_action") and self.status_action.isChecked())

    def change_layout(self):
        self.canvas.configure_layout(self.layout_combo.currentData(), self.page_count, self.scale)
        self.canvas.jump_to(self.page)
        if self.fit_mode and self.path:
            self.scale = self.canvas.fit_scale(self.fit_mode)
        self.request_render()

    def activate_page(self, page):
        if self.finish_inline_then(lambda: self.activate_page(page)):
            return
        self.page = page
        self.selected = None
        self.selection_text = ""
        self.display_identity = None
        self.navigation_pending = True
        self.canvas.set_navigation_pending(True)
        self._update_actions()
        self.request_render()

    def refresh_visible_pages(self):
        if not self.path or self.busy or self.worker_dead or self._closing:
            return
        visible = self.canvas.visible_pages()
        if self.canvas.multipage and visible and self.page not in visible and not self.canvas._drag_mode and self.canvas.inline_editor is None:
            self.activate_page(visible[0])
            return
        self.nearby_ticket += 1
        nearby_ticket = self.nearby_ticket
        self.bridge.cancel_pending("nearby")
        pages = [p for p in visible if p != self.page]
        pages += [p for p in (max(visible or [self.page]) + 1, min(visible or [self.page]) - 1) if 0 <= p < self.page_count and p not in pages]
        identity = (self.session, self.revision, round(self.scale, 3))
        def next_page():
            if nearby_ticket != self.nearby_ticket or identity != (self.session, self.revision, round(self.scale, 3)) or self.busy or not pages:
                return
            page = pages.pop(0)
            key = (self.session, self.revision, page, round(self.scale, 3))
            cached = self.cache.get(key)
            if cached:
                if self.canvas.multipage:
                    self.canvas.install_raster(cached)
                next_page()
                return
            def received(response):
                if nearby_ticket != self.nearby_ticket or identity != (self.session, self.revision, round(self.scale, 3)):
                    return
                if response["ok"]:
                    self.cache.put(key, response["result"])
                    if self.canvas.multipage:
                        self.canvas.install_raster(response["result"])
                next_page()
            self.bridge.submit("render", {"page": page, "scale": self.scale}, received, key="nearby", background=True)
        next_page()

    def request_render(self):
        self.page_render_timer.stop()
        if not self.path or self.busy or self.worker_dead or self._closing:
            return
        self.nearby_ticket += 1
        self.bridge.cancel_pending("nearby")
        self.canvas.configure_layout(self.layout_combo.currentData(), self.page_count, self.scale)
        self.render_ticket += 1
        ticket = self.render_ticket
        session = self.session
        identity = (session, self.revision, self.page)
        key = (session, self.revision, self.page, round(self.scale, 3))
        cached = self.cache.get(key)
        if cached:
            self._display(cached, "cached")
            return
        started = time.perf_counter()
        self.statusBar().showMessage(f"Rendering page {self.page + 1:,}…")
        def rendered(response):
            if identity != (self.session, self.revision, self.page) or ticket != self.render_ticket:
                return
            if not response["ok"]:
                self._error(response)
                return
            result = response["result"]
            self.cache.put(key, result)
            self._display(result, f"{(time.perf_counter() - started) * 1000:.0f} ms")
        self.bridge.submit("render", {"page": self.page, "scale": self.scale}, rendered, key="render")

    def _display(self, result, timing):
        self.display_identity = (self.session, self.revision, self.page)
        self.canvas.set_page(result)
        self.navigation_pending = False
        self.canvas.set_navigation_pending(False)
        if self.enter_page_bottom:
            bar = self.canvas.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.annotations = result.get("annotations", [])
        self.annotation_list.clear()
        for annot in self.annotations:
            label = annot.get("text", "").strip().replace("\n", " ")
            hint = "Select to move or resize" if annot["kind"] in ("text", "note", "rectangle", "ellipse") else "Select to edit or delete"
            item = QListWidgetItem(f"{annot['kind'].replace('_', ' ').title()}\n{label[:70] or hint}")
            item.setData(Qt.ItemDataRole.UserRole, annot["xref"])
            self.annotation_list.addItem(item)
        if self.selected not in [a["xref"] for a in self.annotations]:
            self.selected = None
        self.canvas.set_selected_annotation(self.selected)
        self.canvas.set_search_rects(self.search_hits.get(self.page, []))
        actual_scale = result.get("scale", self.scale)
        self.zoom_combo.setCurrentText(f"{actual_scale * 100:.0f}%" if not self.fit_mode else f"Fit {self.fit_mode}")
        self.performance.setText(f"Page {timing}  ·  Cache {self.cache.bytes_used / 1048576:.0f} / 128 MB")
        self.statusBar().showMessage(f"Page {self.page + 1:,} of {self.page_count:,}  ·  {len(self.annotations)} annotations", 4000)
        self._state(result)
        if self.fit_mode:
            target = min(5.0, max(0.15, self.canvas.fit_scale(self.fit_mode)))
            # A mixed-size page may need one second render to fit its own dimensions.
            if abs(target - self.scale) > 0.015:
                self.scale = target
                self.zoom_timer.start(30)
                return
        self.enter_page_bottom = False
        self.prefetch_timer.start(0 if self.canvas.multipage else 100)
        if self._after_inline_render is not None:
            continuation = self._after_inline_render
            self._after_inline_render = None
            QTimer.singleShot(0, continuation)

    def scroll_pages(self, step):
        """Advance the target immediately, even if its raster is still loading."""
        self.goto(self.page + step * self.canvas.columns, from_wheel=True)

    def _resume_navigation(self):
        """Resume a wheel render postponed by a file operation."""
        if self.navigation_pending and not self.busy:
            self.request_render()

    def goto(self, page, *, from_wheel=False):
        if self.finish_inline_then(lambda: self.goto(page, from_wheel=from_wheel)):
            return
        if not self.path or self.busy or self.worker_dead or page is None:
            return
        page = min(max(0, int(page)), self.page_count - 1)
        if page == self.page:
            self.canvas.set_search_rects(self.search_hits.get(page, []))
            return
        self.enter_page_bottom = from_wheel and page < self.page and not self.canvas.multipage
        self.navigation_pending = from_wheel
        self.canvas.set_navigation_pending(from_wheel)
        self.page_render_timer.stop()
        self.zoom_timer.stop()
        # Invalidate before debouncing: an active worker response can arrive
        # between this wheel event and the new rendering request.
        self.render_ticket += 1
        self.bridge.cancel_pending("render")
        self.page = page
        self.selected = None
        self.selection_text = ""
        self.display_identity = None
        self.annotations = []
        self.annotation_list.clear()
        if self.canvas.multipage:
            self.canvas.jump_to(page)
        else:
            self.canvas.clear()
        self.page_input.setValue(page + 1)
        self._update_actions()
        cached = self.cache.get((self.session, self.revision, page, round(self.scale, 3)))
        if cached:
            self._display(cached, "cached")
        elif from_wheel:
            self.statusBar().showMessage(f"Page {page + 1:,} of {self.page_count:,}")
            self.page_render_timer.start(45)
        else:
            self.request_render()

    def zoom(self, factor):
        if not self.path:
            return
        self.fit_mode = None
        self.scale = min(5.0, max(0.15, self.scale * factor))
        self.zoom_combo.setCurrentText(f"{self.scale * 100:.0f}%")
        self.zoom_timer.start(110)

    def zoom_changed(self):
        value = self.zoom_combo.currentText().strip().lower()
        if "fit" in value:
            self.fit("width" if "width" in value else "page")
            return
        try:
            scale = float(value.rstrip("%")) / 100
        except ValueError:
            return
        self.fit_mode = None
        self.scale = min(5, max(0.15, scale))
        self.request_render()

    def fit(self, mode):
        if not self.path or not mode:
            return
        self.fit_mode = mode
        self.scale = min(5, max(0.15, self.canvas.fit_scale(mode)))
        self.request_render()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "resize_timer"):
            self.resize_timer.start(180)

    def set_tool(self, tool):
        if self.finish_inline_then(lambda: self.set_tool(tool)):
            return
        self.tool_colors[self.tool] = self.color
        self.tool = tool
        self.color = self.tool_colors.get(tool, self.color)
        self._color_swatch()
        self.canvas.set_color(self.color)
        self.canvas.set_tool(tool)
        self.tool_actions[tool].setChecked(True)
        hints = {"hand": "Drag to pan · Ctrl + wheel to zoom", "select": "Select an annotation, or drag across text to copy", "text": "Drag a text box · Double-click a comment to edit", "note": "Click to place a sticky note", "image": "Drag a rectangle to place an image", "highlight": "Drag over text to highlight · Scans support area highlights", "underline": "Drag under text to mark it"}
        self.mode_hint.setText(hints.get(tool, "Drag on the page to draw"))

    def _color_swatch(self):
        self.color_button.setStyleSheet(f"background: {self.color}; border: 3px solid white; border-radius: 6px;")

    def choose_color(self):
        from PyQt6.QtGui import QColor
        color = QColorDialog.getColor(QColor(self.color), self, "Annotation color")
        if color.isValid():
            self.color = color.name()
            self._color_swatch()
            self.canvas.set_color(self.color)
            if self.canvas.inline_editor is not None:
                self.canvas.update_inline_style(color=self.color)
            elif self.selected is not None:
                self.mutate("update_annotation", {"page": self.page, "xref": self.selected, "color": self.color})

    def change_width(self, width):
        if hasattr(self, "canvas"):
            self.canvas.set_stroke_width(width)

    def change_font_size(self):
        if self.canvas.inline_editor is not None:
            self.canvas.update_inline_style(fontsize=self.font_size.value())
            return
        selected = next((a for a in self.annotations if a["xref"] == self.selected), None)
        if selected and selected["kind"] in ("text", "freetext", "free_text"):
            self.mutate("update_annotation", {"page": self.page, "xref": self.selected, "fontsize": self.font_size.value()})

    def finish_inline_then(self, continuation) -> bool:
        """Commit the draft before navigating, saving, or another PDF change."""
        if self.canvas.inline_editor is None:
            return False
        self._inline_continuation = continuation
        self.canvas.finish_inline_text()
        return True

    def begin_inline_text(self, data: dict) -> None:
        """Anchor the draft to the current document revision and page."""
        data = {**data, "_identity": (self.session, self.revision, self.page)}
        self.canvas.begin_inline_text(data)

    def commit_inline_text(self, data: dict) -> None:
        continuation = self._inline_continuation
        self._inline_continuation = None
        if data.get("_identity") != (self.session, self.revision, self.page):
            return
        existing = next((a for a in self.annotations if a["xref"] == data.get("xref")), None)
        unchanged = existing is not None and all(data.get(key) == existing.get(key) for key in ("text", "fontsize", "color"))
        if unchanged or (existing is None and not data["text"].strip()):
            if continuation:
                QTimer.singleShot(0, continuation)
            return
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        payload["page"] = self.page
        if existing is not None:
            # Text edits must not change the native annotation geometry, including rotated pages.
            payload = {key: payload[key] for key in ("page", "xref", "text", "fontsize", "color")}
        def failed():
            self.canvas.begin_inline_text(data)
        self.mutate("update_annotation" if existing is not None else "annotate", payload,
                    after=continuation, on_error=failed)

    def add_annotation(self, data):
        if not self.path or self.busy or self.display_identity != (self.session, self.revision, self.page):
            return
        data = {**data, "page": self.page, "fontsize": self.font_size.value(), "color": self.color, "width": self.stroke.value()}
        if data["kind"] == "text":
            self.begin_inline_text(data)
            return
        if data["kind"] == "note":
            dialog = TextDialog(self, fontsize=self.font_size.value(), title="Sticky note" if data["kind"] == "note" else "Text comment")
            if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.editor.toPlainText().strip():
                return
            data["text"] = dialog.editor.toPlainText()
            data["fontsize"] = dialog.size.value()
        elif data["kind"] == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Place an image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
            if not path:
                return
            data["image_path"] = path
        self.mutate("annotate", data)

    def select_annotation(self, xref):
        if self.busy or self.display_identity != (self.session, self.revision, self.page):
            return
        self.selected = xref
        self.canvas.set_selected_annotation(xref)
        selected = next((a for a in self.annotations if a["xref"] == xref), None)
        if selected:
            self.color = selected.get("color", self.color)
            self._color_swatch()
            self.canvas.set_color(self.color)
            self.stroke.setValue(selected.get("width", 2) or 2)
            if selected.get("fontsize"):
                self.font_size.setValue(round(selected["fontsize"]))
            self.sidebar.setCurrentIndex(2)
            for index in range(self.annotation_list.count()):
                if self.annotation_list.item(index).data(Qt.ItemDataRole.UserRole) == xref:
                    self.annotation_list.setCurrentRow(index)
                    break
        self._update_actions()

    def edit_selected(self, xref=None):
        if self.finish_inline_then(lambda: self.edit_selected(xref)):
            return
        identity = (self.session, self.revision, self.page)
        if self.busy or self.display_identity != identity:
            return
        if isinstance(xref, int) and not isinstance(xref, bool):
            self.select_annotation(xref)
        selected = next((a for a in self.annotations if a["xref"] == self.selected), None)
        if not selected:
            return
        if selected["kind"] in ("text", "freetext", "free_text"):
            self.begin_inline_text({**selected, "page": self.page})
            return
        dialog = TextDialog(self, selected.get("text", ""), selected.get("fontsize", 14), "Edit comment")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if identity != (self.session, self.revision, self.page) or self.busy:
                return
            data = {"page": self.page, "xref": selected["xref"], "text": dialog.editor.toPlainText()}
            if selected["kind"] in ("text", "freetext", "free_text"):
                data["fontsize"] = dialog.size.value()
            self.mutate("update_annotation", data)

    def delete_selected(self):
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit):
            focus.del_()
            return
        if isinstance(focus, QPlainTextEdit):
            cursor = focus.textCursor()
            cursor.deleteChar()
            return
        if self.selected is not None and not self.busy and self.display_identity == (self.session, self.revision, self.page):
            self.mutate("delete_annotation", {"page": self.page, "xref": self.selected})
            self.selected = None

    def undo_redo(self, command):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit)):
            getattr(focus, command)()
        else:
            self.mutate(command)

    def mutate(self, command, payload=None, *, after=None, on_error=None):
        if self.finish_inline_then(lambda: self.mutate(command, payload, after=after, on_error=on_error)):
            return
        if not self.path or self.busy:
            return
        self.stop_search()
        self.page_render_timer.stop()
        self.busy = True
        self._update_actions()
        def changed(response):
            self.busy = False
            if not response["ok"]:
                self._update_actions()
                self._error(response)
                self._resume_navigation()
                if on_error:
                    on_error()
                return
            self.revision += 1
            self.display_identity = None
            self.render_ticket += 1
            self.dirty = True
            self.cache.clear()
            self.canvas.rasters.clear()
            self.search_hits.clear()
            self._state(response["result"])
            self._after_inline_render = after
            self.request_render()
            if response["result"].get("history_reset"):
                self.statusBar().showMessage(str(response["result"]["history_reset"]), 9000)
        self.bridge.submit(command, payload or {}, changed)

    def save(self, checked=False, *, save_as=False, after=None, on_failure=None):
        if self.finish_inline_then(lambda: self.save(save_as=save_as, after=after, on_failure=on_failure)):
            return
        if not self.path or self.busy or self._drive_task:
            return
        save_as = save_as or bool(self.drive_document and not self.drive_client)
        path = self.path
        if save_as:
            suggested = str(Path(path).with_name(Path(path).stem + " - edited.pdf"))
            if self.drive_document:
                suggested = str(Path.home() / "Documents" / "Drive PDF - edited.pdf")
            path, _ = QFileDialog.getSaveFileName(self, "Save PDF as", suggested, "PDF documents (*.pdf)")
            if not path:
                if on_failure:
                    on_failure()
                return
            if self.drive_document and Path(path).resolve().is_relative_to(Path(self.drive_document.path).resolve().parent):
                QMessageBox.warning(self, "Choose another folder", "Save your local copy outside the temporary Drive folder.")
                if on_failure:
                    on_failure()
                return
        self.busy = True
        self.page_render_timer.stop()
        self.stop_search()
        self._update_actions()
        self.statusBar().showMessage("Saving PDF…")
        def saved(response):
            self.busy = False
            if not response["ok"]:
                self._update_actions()
                self._error(response)
                self._resume_navigation()
                if on_failure:
                    on_failure()
                return
            self.path = response["result"].get("path", path)
            if save_as:
                self._release_drive_document()
            self.dirty = False
            self.revision += 1
            self.display_identity = None
            self.selected = None
            self.cache.clear()
            self.canvas.rasters.clear()
            self._state(response["result"])
            if self.drive_client:
                self._upload_drive(after, on_failure)
                return
            self.statusBar().showMessage("PDF saved. Undo history starts again after saving.", 8000)
            if after:
                after()
            else:
                self.request_render()
        self.bridge.submit("save", {"path": path}, saved)

    def select_text(self, rect):
        if not self.path or self.busy:
            return
        session, page = self.session, self.page
        def selected(response):
            if session != self.session or page != self.page:
                return
            if response["ok"]:
                self.selection_text = response["result"].get("text", "")
                self.statusBar().showMessage("Text selected — press Ctrl+C to copy" if self.selection_text else "No text in this area. Image-only scans need OCR first.", 7000)
            else:
                self._error(response)
        self.bridge.submit("copy_text", {"page": page, "rect": rect}, selected, key="copy_text")

    def copy_text(self):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit)):
            focus.copy()
        elif self.selection_text:
            QApplication.clipboard().setText(self.selection_text)
            self.statusBar().showMessage("Selected text copied", 3000)

    def focus_search(self):
        self.sidebar.show()
        self.sidebar.setCurrentIndex(1)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def stop_search(self):
        self.search_ticket += 1
        self.bridge.cancel_pending("search")
        if hasattr(self, "search_status") and getattr(self, "searching", False):
            self.search_status.setText("Search stopped. Results found so far are shown.")
        self.searching = False

    def start_search(self):
        query = self.search_input.text().strip()
        if not query or not self.path or self.busy:
            return
        self.stop_search()
        self.search_hits.clear()
        self.search_results.clear()
        self.canvas.set_search_rects([])
        self.searching = True
        token = self.search_ticket
        count = 0
        def step(page):
            if token != self.search_ticket:
                return
            self.search_status.setText(f"Searching page {page + 1:,} / {self.page_count:,}…")
            def found(response):
                nonlocal count
                if token != self.search_ticket:
                    return
                if not response["ok"]:
                    self.searching = False
                    self.search_status.setText(response.get("error", "Search failed"))
                    return
                rects = response["result"].get("rects", [])
                if rects:
                    self.search_hits[page] = rects
                    count += len(rects)
                    item = QListWidgetItem(f"Page {page + 1}   ·   {len(rects)} matches")
                    item.setData(Qt.ItemDataRole.UserRole, page)
                    self.search_results.addItem(item)
                    if page == self.page:
                        self.canvas.set_search_rects(rects)
                if page + 1 < self.page_count and count < 2000:
                    QTimer.singleShot(0, lambda: step(page + 1))
                else:
                    self.searching = False
                    self.search_status.setText(f"{count:,} matches" + (" — result limit reached" if count >= 2000 else f" across {self.page_count:,} pages"))
            self.bridge.submit("search", {"page": page, "query": query}, found, key="search", background=True)
        step(0)

    def delete_page(self):
        if self.page_count <= 1:
            QMessageBox.information(self, "Delete page", "A PDF must keep at least one page.")
            return
        if QMessageBox.question(self, "Delete page", f"Delete page {self.page + 1}? You can undo this before saving.") == QMessageBox.StandardButton.Yes:
            self.mutate("delete_page", {"page": self.page})

    def merge_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Append PDF pages", "", "PDF documents (*.pdf)")
        if path:
            self.mutate("merge", {"path": path})

    def extract_pages(self):
        text, ok = QInputDialog.getText(self, "Extract pages", f"Pages to extract (1–{self.page_count}), for example 1-3, 7:", text=str(self.page + 1))
        if not ok:
            return
        try:
            pages = []
            for part in text.split(","):
                ends = [int(x.strip()) for x in part.split("-")]
                if len(ends) == 1:
                    pages.append(ends[0] - 1)
                elif len(ends) == 2 and ends[0] <= ends[1]:
                    if ends[0] < 1 or ends[1] > self.page_count:
                        raise ValueError
                    pages.extend(range(ends[0] - 1, ends[1]))
                else:
                    raise ValueError
            if not pages or any(p < 0 or p >= self.page_count for p in pages):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Invalid page range", "Enter valid page numbers or ranges, for example 1-3, 7.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save extracted pages", str(Path(self.path).with_name(Path(self.path).stem + " - extracted.pdf")), "PDF documents (*.pdf)")
        if path:
            self.busy = True
            self._update_actions()
            def extracted(response):
                self.busy = False
                self._update_actions()
                if not response["ok"]:
                    self._error(response)
                else:
                    self.statusBar().showMessage(f"Extracted {len(pages)} pages to {Path(path).name}", 8000)
                self._resume_navigation()
            self.bridge.submit("extract", {"path": path, "pages": pages}, extracted)

    def show_help(self):
        QMessageBox.information(self, "Using Gpdf", "Open or drop a PDF to begin.\n\n• Pan: drag the page; Ctrl+wheel zooms.\n• Select: click annotations to move or resize using the corner handle. Drag across text, then Ctrl+C to copy.\n• Highlight, shapes, arrows, pen: drag on the page.\n• Text: drag a box and type directly on the page. Ctrl+Enter or click outside to finish; Esc cancels. Double-click a comment to edit it.\n• Image: drag a box and choose an image.\n• Ctrl+F searches existing text, including OCR layers.\n• Ctrl+S saves; Ctrl+Shift+S writes a separate copy.\n• PgUp/PgDn changes pages. F9 toggles the sidebar.\n\nUndo/redo covers annotations and page rotation/deletion. Saving and appending a PDF reset undo history. Images become page content; use Undo to remove a newly added image before saving.\n\nThis version does not rewrite existing body text, generate OCR, apply secure redactions, or sign PDFs.")

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        if self._drive_task:
            event.ignore()
            QMessageBox.information(self, "Google Drive operation in progress",
                                    "Wait for the Drive operation to finish, or choose File → Cancel Google Drive operation. "
                                    "Your open edits will be kept.")
            return
        if self.busy:
            event.ignore()
            self.statusBar().showMessage("Please wait for the current file operation to finish.")
            return
        event.ignore()
        def finish():
            self._closing = True
            self.page_render_timer.stop()
            self.zoom_timer.stop()
            self.bridge.shutdown()
            self._close_drive()
            QTimer.singleShot(0, self.close)
        self._leave_document(finish)


def main(argv=None):
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Gpdf - local PDF reader and annotation editor")
    parser.add_argument("file", nargs="?", help="PDF to open")
    parser.add_argument("--smoke-test", metavar="REPORT", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Gpdf")
    app.setOrganizationName("Gpdf")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    if options.smoke_test:
        from .smoke import start
        start(window, options.smoke_test)
    elif options.file:
        QTimer.singleShot(100, lambda: window.open_path(options.file))
    return app.exec()
