"""PDF work performed exclusively by the spawned document worker.

The public protocol uses zero-based pages and *rotated display* coordinates in
PDF points (not screen pixels). Importing this module does not import MuPDF;
only constructing PdfEngine does. This keeps its native state out of Qt's
process and gives each document one serial owner.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import re
import tempfile


class PasswordRequiredError(ValueError):
    """The document needs a password, or the supplied password is incorrect."""


class PdfEngine:
    MAX_RENDER_PIXELS = 12_000_000
    CACHE_TARGET = 96 * 1024 * 1024

    def __init__(self):
        import pymupdf

        self.pdf = pymupdf
        self.doc = None
        self.path = None
        self.password = ""
        self.owner_access = False
        self.encrypted = False
        self._temporary_paths = set()
        self._scratch_directory = None

    def handle(self, command: str, payload: dict | None = None) -> dict:
        methods = {
            "open": self.open, "close": self.close, "render": self.render,
            "annotate": self.annotate, "update_annotation": self.update_annotation,
            "delete_annotation": self.delete_annotation, "copy_text": self.copy_text,
            "search": self.search, "undo": self.undo, "redo": self.redo,
            "rotate": self.rotate, "delete_page": self.delete_page,
            "merge": self.merge, "extract": self.extract, "save": self.save,
        }
        if command not in methods:
            raise ValueError(f"Unknown PDF command: {command}")
        return methods[command](payload or {})

    def _require_document(self):
        if self.doc is None:
            raise ValueError("Open a PDF first.")
        return self.doc

    def _page(self, number):
        document = self._require_document()
        number = int(number)
        if not 0 <= number < len(document):
            raise ValueError("That page does not exist.")
        return document.load_page(number)

    def _status(self, details=False):
        document = self._require_document()
        actions = document.journal_can_do() if document.journal_is_enabled() else {}
        result = {
            "page_count": len(document),
            "can_undo": bool(actions.get("undo")),
            "can_redo": bool(actions.get("redo")),
        }
        if details:
            result.update(path=self.path, metadata=document.metadata,
                          toc=document.get_toc(), encrypted=self.encrypted,
                          permissions=document.permissions)
        return result

    def _read_pdf(self, path, password=""):
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"The PDF file does not exist: {path}")
        document = self.pdf.open(str(path))
        try:
            if not document.is_pdf:
                raise ValueError("Please choose a PDF document.")
            was_encrypted = bool(document.needs_pass or
                                 (document.metadata or {}).get("encryption"))
            access = 4
            if document.needs_pass or was_encrypted:
                access = document.authenticate(password or "")
                if not access:
                    raise PasswordRequiredError("A password is required, or the password was incorrect.")
            if len(document) == 0:
                raise ValueError("The PDF contains no pages.")
            return document, bool(access & 4), was_encrypted
        except Exception:
            document.close()
            raise

    def open(self, payload):
        password = payload.get("password") or ""
        document, owner, encrypted = self._read_pdf(payload["path"], password)
        document.journal_enable()
        self.close({})
        self.doc = document
        self.path = str(Path(payload["path"]).expanduser().resolve())
        if payload.get("private_working_copy"):
            self._scratch_directory = str(Path(self.path).parent)
        self.password, self.owner_access, self.encrypted = password, owner, encrypted
        return self._status(details=True)

    def close(self, payload=None):
        if self.doc is not None:
            self.doc.close()
        self.doc, self.path = None, None
        self._scratch_directory = None
        self.password = ""
        self.owner_access = self.encrypted = False
        for path in list(self._temporary_paths):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        self._temporary_paths.clear()
        self.pdf.TOOLS.store_shrink(100)
        return {"closed": True, "page_count": 0, "can_undo": False, "can_redo": False}

    def _permission(self, permission):
        document = self._require_document()
        if self.owner_access:
            return
        bits = {
            "annotate": self.pdf.PDF_PERM_ANNOTATE,
            "modify": self.pdf.PDF_PERM_MODIFY,
            "assemble": self.pdf.PDF_PERM_ASSEMBLE | self.pdf.PDF_PERM_MODIFY,
            "copy": self.pdf.PDF_PERM_COPY,
        }
        if not document.permissions & bits[permission]:
            raise PermissionError(f"This PDF's permissions do not allow this operation ({permission}).")

    @contextmanager
    def _operation(self, name):
        document = self._require_document()
        before = document.journal_position()[0]
        document.journal_start_op(name)
        try:
            yield
        except Exception:
            document.journal_stop_op()
            if document.journal_position()[0] > before:
                document.journal_undo()
            raise
        else:
            document.journal_stop_op()

    @staticmethod
    def _number(value, minimum, maximum, name):
        value = float(value)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
        return value

    def _rect(self, page, values, clip=True):
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            raise ValueError("A rectangle needs four coordinates.")
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("Rectangle coordinates must be finite.")
        rect = self.pdf.Rect(values).normalize()
        if clip:
            rect &= page.rect
        if rect.is_empty or rect.is_infinite:
            raise ValueError("Draw a rectangle inside the page.")
        return rect * page.derotation_matrix

    def _point(self, page, values):
        if len(values) != 2 or not all(math.isfinite(float(v)) for v in values):
            raise ValueError("A point needs two finite coordinates.")
        return self.pdf.Point(values) * page.derotation_matrix

    @staticmethod
    def _color(value):
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Choose a color in #RRGGBB format.")
        return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))

    @staticmethod
    def _hex_color(values):
        if not values:
            return "#333333"
        if len(values) == 1:
            values = values * 3
        elif len(values) == 4:
            c, m, y, k = values
            values = [1 - min(1, c + k), 1 - min(1, m + k), 1 - min(1, y + k)]
        return "#" + "".join(f"{max(0, min(255, round(float(v) * 255))):02x}" for v in values[:3])

    def _annotations(self, page):
        names = {
            self.pdf.PDF_ANNOT_HIGHLIGHT: "highlight", self.pdf.PDF_ANNOT_UNDERLINE: "underline",
            self.pdf.PDF_ANNOT_SQUARE: "rectangle", self.pdf.PDF_ANNOT_CIRCLE: "ellipse",
            self.pdf.PDF_ANNOT_LINE: "arrow", self.pdf.PDF_ANNOT_INK: "ink",
            self.pdf.PDF_ANNOT_FREE_TEXT: "text", self.pdf.PDF_ANNOT_TEXT: "note",
        }
        items = []
        for annotation in page.annots() or []:
            fontsize = 12.0
            colors = annotation.colors.get("stroke") or annotation.colors.get("fill")
            if annotation.type[0] == self.pdf.PDF_ANNOT_FREE_TEXT:
                appearance = self.doc.xref_get_key(annotation.xref, "DA")[1]
                size = re.search(r"([-+\d.]+)\s+Tf", appearance)
                if size:
                    fontsize = float(size.group(1))
                rgb = re.search(r"([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+rg", appearance)
                if rgb:
                    colors = [float(v) for v in rgb.groups()]
            items.append({
                "xref": annotation.xref, "kind": names.get(annotation.type[0], annotation.type[1].lower()),
                "rect": list(annotation.rect * page.rotation_matrix),
                "text": annotation.info.get("content", ""), "fontsize": fontsize,
                "color": self._hex_color(colors), "width": annotation.border.get("width", 1),
            })
        return items

    def render(self, payload):
        page = self._page(payload["page"])
        scale = self._number(payload.get("scale", 1.0), 0.01, 100, "Zoom")
        width, height = page.rect.width, page.rect.height
        if width <= 0 or height <= 0:
            raise ValueError("This page has invalid dimensions.")
        scale = min(scale, math.sqrt(self.MAX_RENDER_PIXELS / (width * height)),
                    32768 / max(width, height))
        while math.ceil(width * scale) * math.ceil(height * scale) > self.MAX_RENDER_PIXELS:
            scale *= 0.999
        pixmap = page.get_pixmap(matrix=self.pdf.Matrix(scale, scale), colorspace=self.pdf.csRGB,
                                alpha=False, annots=True)
        result = {
            "page": page.number, "scale": scale, "width": pixmap.width,
            "height": pixmap.height, "stride": pixmap.stride, "samples": pixmap.samples,
            "page_width": width, "page_height": height, "rotation": page.rotation,
            "annotations": self._annotations(page), **self._status(),
        }
        del pixmap
        # Older bindings expose cache size; current bindings return None and
        # retain MuPDF's own default store limit.
        used = self.pdf.TOOLS.store_size()
        if used is not None and used > self.CACHE_TARGET:
            self.pdf.TOOLS.store_shrink(max(1, int(100 * (1 - self.CACHE_TARGET / used))))
        return result

    def _display_textpage(self, page):
        # Page.get_textpage temporarily writes /Rotate for rotated pages, which
        # conflicts with journalling even for a read. A display list gives us
        # read-only text extraction directly in the public display coordinates.
        raw = page.get_displaylist(annots=False).get_textpage(flags=self.pdf.TEXTFLAGS_SEARCH)
        return raw if isinstance(raw, self.pdf.TextPage) else self.pdf.TextPage(raw)

    def _text_mark_quads(self, page, rect):
        lines = {}
        for word in self._display_textpage(page).extractWORDS():
            box = self.pdf.Rect(word[:4]) * page.derotation_matrix
            intersection = box & rect
            if intersection.is_empty or intersection.get_area() < box.get_area() * 0.35:
                continue
            key = (word[5], word[6])
            if key in lines:
                lines[key] |= box
            else:
                lines[key] = box
        return [box.quad for box in lines.values()] or [rect.quad]

    def annotate(self, payload):
        kind = payload["kind"]
        kinds = {"highlight", "underline", "rectangle", "ellipse", "arrow", "ink", "text", "note", "image"}
        if kind not in kinds:
            raise ValueError(f"Unsupported annotation tool: {kind}")
        self._permission("modify" if kind == "image" else "annotate")
        page = self._page(payload["page"])
        color = self._color(payload.get("color", "#ffd84d" if kind == "highlight" else "#e45050"))
        width = self._number(payload.get("width", 2), 0.1, 50, "Stroke width")
        fontsize = self._number(payload.get("fontsize", 14), 4, 288, "Font size")
        text = str(payload.get("text", ""))
        rect = self._rect(page, payload["rect"]) if "rect" in payload and kind not in {"arrow", "ink"} else None
        points = payload.get("points", [])
        if kind not in {"arrow", "ink", "note"} and rect is None:
            raise ValueError("Draw an area for this tool.")
        if kind in {"arrow", "ink"}:
            # Ink accepts either one stroke [[x,y],...] or multiple strokes.
            strokes = points if points and isinstance(points[0][0], (list, tuple)) else [points]
            strokes = [[self._point(page, pt) for pt in stroke] for stroke in strokes]
            if not strokes or any(len(stroke) < 2 for stroke in strokes):
                raise ValueError("Draw a line with at least two points.")
        if kind == "image":
            image_path = Path(payload.get("image_path", "")).expanduser().resolve()
            if not image_path.is_file():
                raise ValueError("Choose an image file first.")
        if kind == "note":
            note_point = rect.tl if rect is not None else self._point(page, points[0] if points else (24, 24))
        with self._operation(f"Add {kind}"):
            annotation = None
            if kind in {"highlight", "underline"}:
                quads = self._text_mark_quads(page, rect)
                annotation = (page.add_highlight_annot(quads) if kind == "highlight"
                              else page.add_underline_annot(quads))
            elif kind == "rectangle":
                annotation = page.add_rect_annot(rect)
            elif kind == "ellipse":
                annotation = page.add_circle_annot(rect)
            elif kind == "arrow":
                annotation = page.add_line_annot(strokes[0][0], strokes[0][-1])
                annotation.set_line_ends(self.pdf.PDF_ANNOT_LE_NONE, self.pdf.PDF_ANNOT_LE_CLOSED_ARROW)
            elif kind == "ink":
                annotation = page.add_ink_annot([[tuple(point) for point in stroke] for stroke in strokes])
            elif kind == "text":
                annotation = page.add_freetext_annot(rect, text, fontsize=fontsize, text_color=color,
                                                     rotate=page.rotation)
            elif kind == "note":
                annotation = page.add_text_annot(note_point, text)
            elif kind == "image":
                page.insert_image(rect, filename=str(image_path), keep_proportion=True, rotate=page.rotation)
            if annotation is not None:
                if kind != "text":
                    annotation.set_colors(stroke=color)
                    if kind not in {"highlight", "underline", "note"}:
                        annotation.set_border(width=width)
                    annotation.update(opacity=0.38 if kind == "highlight" else 1)
                xref = annotation.xref
            else:
                xref = None
        return {"xref": xref, **self._status()}

    def update_annotation(self, payload):
        self._permission("annotate")
        page = self._page(payload["page"])
        annotation = page.load_annot(int(payload["xref"]))
        if annotation is None:
            raise ValueError("This annotation no longer exists.")
        free_text = annotation.type[0] == self.pdf.PDF_ANNOT_FREE_TEXT
        rect = self._rect(page, payload["rect"]) if "rect" in payload else None
        if rect is not None and annotation.type[0] not in {
            self.pdf.PDF_ANNOT_FREE_TEXT, self.pdf.PDF_ANNOT_TEXT,
            self.pdf.PDF_ANNOT_SQUARE, self.pdf.PDF_ANNOT_CIRCLE,
        }:
            raise ValueError("Only text, notes, rectangles and ellipses can be resized.")
        fontsize = self._number(payload["fontsize"], 4, 288, "Font size") if "fontsize" in payload else None
        color = self._color(payload["color"]) if "color" in payload else None
        width = self._number(payload["width"], 0.1, 50, "Stroke width") if "width" in payload else None
        with self._operation("Edit annotation"):
            if "text" in payload:
                annotation.set_info(content=str(payload["text"]))
            if rect is not None:
                annotation.set_rect(rect)
            if free_text:
                options = {}
                if fontsize is not None:
                    options["fontsize"] = fontsize
                if color is not None:
                    options["text_color"] = color
                annotation.update(**options)
            else:
                if color is not None:
                    annotation.set_colors(stroke=color)
                if width is not None:
                    annotation.set_border(width=width)
                annotation.update()
        return self._status()

    def delete_annotation(self, payload):
        self._permission("annotate")
        page = self._page(payload["page"])
        annotation = page.load_annot(int(payload["xref"]))
        if annotation is None:
            raise ValueError("This annotation no longer exists.")
        with self._operation("Delete annotation"):
            page.delete_annot(annotation)
        return self._status()

    def copy_text(self, payload):
        self._permission("copy")
        page = self._page(payload["page"])
        textpage = self._display_textpage(page)
        if "rect" in payload:
            clip = self._rect(page, payload["rect"]) * page.rotation_matrix
            text = textpage.extractTextbox(clip)
        else:
            text = textpage.extractText()
        return {"page": page.number, "text": text}

    def search(self, payload):
        page = self._page(payload["page"])
        query = str(payload.get("query", "")).strip()
        rects = self._display_textpage(page).search(query, quads=False) if query else []
        return {"page": page.number, "rects": [list(rect) for rect in rects]}

    def undo(self, payload=None):
        document = self._require_document()
        if document.journal_can_do()["undo"]:
            document.journal_undo()
        return self._status(details=True)

    def redo(self, payload=None):
        document = self._require_document()
        if document.journal_can_do()["redo"]:
            document.journal_redo()
        return self._status(details=True)

    def rotate(self, payload):
        self._permission("assemble")
        page = self._page(payload["page"])
        degrees = int(payload.get("degrees", 90))
        if degrees % 90:
            raise ValueError("Page rotation must be a multiple of 90 degrees.")
        with self._operation("Rotate page"):
            page.set_rotation((page.rotation + degrees) % 360)
        return self._status()

    def delete_page(self, payload):
        self._permission("assemble")
        page = self._page(payload["page"])
        if len(self.doc) <= 1:
            raise ValueError("A PDF must keep at least one page.")
        with self._operation("Delete page"):
            self.doc.delete_page(page.number)
        return self._status(details=True)

    def _temporary_file(self, directory=None):
        fd, name = tempfile.mkstemp(prefix="gpdf-", suffix=".pdf", dir=directory or self._scratch_directory)
        os.close(fd)
        self._temporary_paths.add(name)
        return name

    def _write_snapshot(self, document, path):
        # No garbage collection / xref renumbering: preserve native annotations,
        # encryption, OCR layers, and large already-compressed image streams.
        document.save(path, encryption=self.pdf.PDF_ENCRYPT_KEEP, garbage=0, deflate=False)

    def merge(self, payload):
        self._permission("assemble")
        source, owner, encrypted = self._read_pdf(payload["path"], payload.get("password", ""))
        candidate = None
        snapshot = None
        try:
            if not owner and not source.permissions & self.pdf.PDF_PERM_COPY:
                raise PermissionError("The source PDF does not allow copying its pages.")
            # MuPDF insert_pdf cannot run with journalling enabled. Work on a
            # separate file so failures cannot discard existing unsaved edits.
            snapshot = self._temporary_file()
            self._write_snapshot(self._require_document(), snapshot)
            candidate, _, _ = self._read_pdf(snapshot, self.password)
            candidate.insert_pdf(source)
            candidate.journal_enable()
        except Exception:
            if candidate is not None:
                candidate.close()
            raise
        finally:
            source.close()
        self.doc.close()
        self.doc = candidate
        return {**self._status(details=True), "history_reset": "Merging documents resets undo history."}

    def extract(self, payload):
        self._permission("copy")
        document = self._require_document()
        if self.encrypted:
            raise ValueError("Extracting encrypted PDFs is not supported; their original password protection must be preserved.")
        pages = [int(number) for number in payload.get("pages", [])]
        if not pages or any(number < 0 or number >= len(document) for number in pages):
            raise ValueError("Choose valid page numbers to extract.")
        target = Path(payload["path"]).expanduser().resolve()
        if target == Path(self.path):
            raise ValueError("Choose a different file for extracted pages.")
        if not target.parent.is_dir():
            raise ValueError("The destination folder does not exist.")
        temporary = self._temporary_file(target.parent)
        output = self.pdf.open()
        try:
            for number in pages:
                output.insert_pdf(document, from_page=number, to_page=number)
            output.set_metadata(document.metadata)
            output.save(temporary)
            output.close()
            os.replace(temporary, target)
            self._temporary_paths.discard(temporary)
        finally:
            if not output.is_closed:
                output.close()
        return {"path": str(target), "extracted_pages": len(pages), **self._status()}

    def save(self, payload):
        document = self._require_document()
        target = Path(payload.get("path") or self.path).expanduser().resolve()
        if not target.parent.is_dir():
            raise ValueError("The destination folder does not exist.")
        temporary = self._temporary_file(target.parent)
        self._write_snapshot(document, temporary)
        # Validate the completed file before replacing anything on disk.
        validation, _, _ = self._read_pdf(temporary, self.password)
        validation.close()
        document.close()  # Release Windows' source-file handle before replace.
        self.doc = None
        try:
            os.replace(temporary, target)
        except OSError as error:
            # Keep every unsaved edit available in the worker if replacement is
            # denied (e.g. another reader has locked the destination).
            self.doc, _, _ = self._read_pdf(temporary, self.password)
            self.doc.journal_enable()
            raise OSError(f"Could not replace the destination. Your edits are still open; save to another file. {error}") from error
        self._temporary_paths.discard(temporary)
        self.doc, self.owner_access, self.encrypted = self._read_pdf(target, self.password)
        self.doc.journal_enable()
        if str(target) != self.path:
            self._scratch_directory = None
        self.path = str(target)
        return {**self._status(details=True), "history_reset": "Saving resets undo history."}


def worker_main(request_queue, response_queue):
    """Blocking, serial process entry point; all responses are pickle-safe."""
    engine = PdfEngine()
    try:
        while True:
            request = request_queue.get()
            if request is None or request.get("command") == "shutdown":
                break
            command = request.get("command", "")
            response = {"id": request.get("id"), "command": command}
            try:
                response.update(ok=True, result=engine.handle(command, request.get("payload") or {}))
            except Exception as error:
                response.update(ok=False, error=str(error) or type(error).__name__)
                if isinstance(error, PasswordRequiredError):
                    response["password_required"] = True
                # A failed atomic replacement can retain edits in a reopened
                # recovery snapshot, which necessarily resets its journal.
                if engine.doc is not None:
                    response["result"] = engine._status()
            response_queue.put(response)
    finally:
        engine.close()
