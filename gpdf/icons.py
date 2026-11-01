"""Small vector icons drawn at the display's own resolution."""

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QPixmap, QPainter
from PyQt6.QtSvg import QSvgRenderer


PATHS = {
    "open": '<path d="M3 7h7l2 2h9l-3 11H3V7Z M3 7V4h7l2 3h7v2"/>',
    "save": '<path d="M4 3h14l3 3v15H3V3Z M7 3v6h10V3 M7 21v-8h10v8"/>',
    "select": '<path d="m5 3 14 10-7 1-4 7Z"/>',
    "hand": '<path d="M8 12V5a2 2 0 0 1 4 0v7-8a2 2 0 0 1 4 0v8-5a2 2 0 0 1 4 0v8c0 5-4 7-7 7s-4-2-6-5l-3-4c-1-2 1-4 3-2l1 1Z"/>',
    "highlight": '<path d="m5 14 9-11 7 6-9 11Z M5 14l7 6 M5 17l-3 4h7"/>',
    "underline": '<path d="M6 3v9a6 6 0 0 0 12 0V3 M4 22h16"/>',
    "text": '<path d="M3 5V3h18v2 M12 3v18 M8 21h8"/>',
    "note": '<path d="M3 3h18v13l-6 6H3Z M15 22v-6h6 M7 8h10 M7 12h7"/>',
    "arrow": '<path d="M4 20 20 4 M8 4h12v12"/>',
    "rectangle": '<rect x="3" y="5" width="18" height="14" rx="1"/>',
    "ellipse": '<ellipse cx="12" cy="12" rx="9" ry="7"/>',
    "ink": '<path d="M3 19c7-18 3 8 10-5s2 6 8-5 M3 22h18"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="2"/><path d="m3 17 6-5 4 3 4-5 4 6"/>',
    "undo": '<path d="M3 10h11a7 7 0 0 1 0 14 M3 10l6-6 M3 10l6 6" transform="translate(0 -2)"/>',
    "redo": '<path d="M21 10H10a7 7 0 0 0 0 14 M21 10l-6-6 M21 10l-6 6" transform="translate(0 -2)"/>',
    "left": '<path d="m15 5-7 7 7 7"/>',
    "right": '<path d="m9 5 7 7-7 7"/>',
    "search": '<circle cx="10" cy="10" r="7"/><path d="m15 15 7 7"/>',
    "rotate": '<path d="M21 8V2l-3 3a9 9 0 1 0 3 11 M21 8h-6"/>',
    "trash": '<path d="M3 6h18 M8 6V3h8v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7"/>',
    "plus": '<path d="M12 4v16 M4 12h16"/>',
    "minus": '<path d="M4 12h16"/>',
}


def icon(name, color="#36445a"):
    body = PATHS.get(name, PATHS["rectangle"])
    source = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">{body}</g></svg>'
    renderer = QSvgRenderer(QByteArray(source.encode()))
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
