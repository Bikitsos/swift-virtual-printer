"""Render a parsed ESC/POS layout to a PDF using Qt."""

from __future__ import annotations

from pathlib import Path

import barcode
import segno
from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
)

from .escpos import (
    FONT_CELLS,
    BarcodeBlock,
    Bitmap,
    Cut,
    Element,
    Feed,
    ImageBlock,
    InlineImage,
    Line,
    QRBlock,
    Style,
    TextCell,
    normalize_barcode_data,
)

DPI = 203
MARGIN = 24
BLOCK_PADDING = 8


def _to_qimage(bitmap: Bitmap) -> QImage:
    bpr = bitmap.bytes_per_row
    size = bpr * bitmap.height
    data = bytes(bitmap.data[:size]).ljust(size, b"\0")
    image = QImage(data, bitmap.width, bitmap.height, bpr, QImage.Format.Format_Mono).copy()
    image.setColorTable([0xFFFFFFFF, 0xFF000000])
    return image


def _qr_matrix(block: QRBlock):
    try:
        return segno.make_qr(block.data, error=block.error).matrix
    except Exception:
        return None


def _barcode_modules(block: BarcodeBlock) -> str | None:
    data = normalize_barcode_data(block.symbology, block.data)
    try:
        cls = barcode.get_barcode_class(block.symbology)
        kwargs = {"add_checksum": False} if block.symbology == "code39" else {}
        return cls(data, **kwargs).build()[0]
    except Exception:
        return None


def _split_pages(elements: list[Element]) -> list[list[Element]]:
    pages: list[list[Element]] = [[]]
    for element in elements:
        if isinstance(element, Cut):
            pages.append([])
        else:
            pages[-1].append(element)
    return [p for p in pages if any(not isinstance(e, Feed) for e in p)]


class PdfRenderer:
    def __init__(self, paper_mm: float = 80.0, paper_dots: int = 576) -> None:
        self.paper_mm = paper_mm
        self.paper_dots = paper_dots
        self.page_width = round(paper_mm / 25.4 * DPI)
        self.left = max(0, (self.page_width - paper_dots) / 2)
        self._fonts: dict[tuple[int, bool], tuple[QFont, float, float]] = {}
        self._family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()

    def render(self, elements: list[Element], path: Path) -> bool:
        """Write elements to a PDF, one page per paper cut. Returns False if nothing to print."""
        pages = _split_pages(elements)
        if not pages:
            return False

        writer = QPdfWriter(str(path))
        writer.setResolution(DPI)
        writer.setCreator("swift-virtual-printer")
        writer.setTitle(path.stem)
        self._writer = writer

        painter = QPainter()
        try:
            for index, page in enumerate(pages):
                height = max(self.page_width, 2 * MARGIN + sum(self._height(e) for e in page))
                page_size = QPageSize(
                    QSizeF(self.paper_mm, height * 25.4 / DPI),
                    QPageSize.Unit.Millimeter,
                    "Receipt",
                    QPageSize.SizeMatchPolicy.ExactMatch,
                )
                writer.setPageLayout(
                    QPageLayout(page_size, QPageLayout.Orientation.Portrait, QMarginsF())
                )
                if index == 0:
                    if not painter.begin(writer):
                        raise RuntimeError(f"Cannot write PDF: {path}")
                else:
                    writer.newPage()

                y = float(MARGIN)
                for element in page:
                    self._draw(painter, element, y)
                    y += self._height(element)
        finally:
            if painter.isActive():
                painter.end()
        return True

    # -- metrics ------------------------------------------------------------

    def _font(self, font: int, bold: bool) -> tuple[QFont, float, float]:
        key = (font, bold)
        if key not in self._fonts:
            cell_w, cell_h = FONT_CELLS[font]
            qfont = QFont(self._family)
            qfont.setStyleHint(QFont.StyleHint.TypeWriter)
            qfont.setPixelSize(round(cell_h * 0.9))
            qfont.setBold(bold)
            metrics = QFontMetricsF(qfont, self._writer)
            scale_x = cell_w / metrics.horizontalAdvance("M")
            self._fonts[key] = (qfont, scale_x, metrics.descent())
        return self._fonts[key]

    def _x(self, width: float, align: int) -> float:
        free = max(0.0, self.paper_dots - width)
        return self.left + (0, free / 2, free)[align]

    def _height(self, element: Element) -> float:
        if isinstance(element, Line):
            return element.height
        if isinstance(element, Feed):
            return element.dots
        if isinstance(element, ImageBlock):
            return element.bitmap.size[1]
        if isinstance(element, QRBlock):
            matrix = _qr_matrix(element)
            return len(matrix) * element.module_size + 2 * BLOCK_PADDING if matrix else 0
        if isinstance(element, BarcodeBlock):
            hri_lines = bin(element.hri).count("1")
            return element.height + hri_lines * FONT_CELLS[0][1] + 2 * BLOCK_PADDING
        return 0

    # -- drawing ------------------------------------------------------------

    def _draw(self, painter: QPainter, element: Element, y: float) -> None:
        if isinstance(element, Line):
            self._draw_line(painter, element, y)
        elif isinstance(element, ImageBlock):
            w, h = element.bitmap.size
            painter.drawImage(QRectF(self._x(w, element.align), y, w, h), _to_qimage(element.bitmap))
        elif isinstance(element, QRBlock):
            self._draw_qr(painter, element, y)
        elif isinstance(element, BarcodeBlock):
            self._draw_barcode(painter, element, y)

    def _draw_line(self, painter: QPainter, line: Line, y: float) -> None:
        baseline = y + line.content_height
        x = self._x(line.width, line.align)
        for item in line.items:
            w, h = item.size
            if isinstance(item, TextCell):
                self._draw_cell(painter, x, baseline, item.char, item.style)
            elif isinstance(item, InlineImage):
                painter.drawImage(QRectF(x, baseline - h, w, h), _to_qimage(item.bitmap))
            x += w

    def _draw_cell(self, painter: QPainter, x: float, baseline: float, char: str, style: Style) -> None:
        w, h = style.cell
        color = Qt.GlobalColor.black
        if style.reverse:
            # Slight overlap avoids hairline seams between adjacent reversed cells.
            painter.fillRect(QRectF(x, baseline - h, w + 0.5, h), Qt.GlobalColor.black)
            color = Qt.GlobalColor.white
        if not char.isspace():
            font, scale_x, descent = self._font(style.font, style.bold)
            painter.save()
            painter.setFont(font)
            painter.setPen(color)
            painter.translate(x, baseline - descent * style.height_mul)
            painter.scale(scale_x * style.width_mul, style.height_mul)
            painter.drawText(QPointF(0, 0), char)
            painter.restore()
        if style.underline:
            thickness = 2 * style.underline
            painter.fillRect(QRectF(x, baseline - thickness, w, thickness), color)

    def _draw_text(self, painter: QPainter, text: str, y: float, align: int) -> None:
        style = Style()
        cell_w, cell_h = style.cell
        x = self._x(len(text) * cell_w, align)
        for char in text:
            self._draw_cell(painter, x, y + cell_h, char, style)
            x += cell_w

    def _draw_qr(self, painter: QPainter, block: QRBlock, y: float) -> None:
        matrix = _qr_matrix(block)
        if not matrix:
            return
        m = block.module_size
        x0 = self._x(len(matrix) * m, block.align)
        y0 = y + BLOCK_PADDING
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        for r, row in enumerate(matrix):
            for c, dark in enumerate(row):
                if dark:
                    path.addRect(QRectF(x0 + c * m, y0 + r * m, m, m))
        painter.fillPath(path, Qt.GlobalColor.black)

    def _draw_barcode(self, painter: QPainter, block: BarcodeBlock, y: float) -> None:
        text = normalize_barcode_data(block.symbology, block.data)
        line_h = FONT_CELLS[0][1]
        y += BLOCK_PADDING
        if block.hri & 1:
            self._draw_text(painter, text, y, block.align)
            y += line_h

        modules = _barcode_modules(block)
        if modules:
            module_w = max(1, min(block.module_width, 6))
            while module_w > 1 and len(modules) * module_w > self.paper_dots:
                module_w -= 1
            x0 = self._x(len(modules) * module_w, block.align)
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            for i, bit in enumerate(modules):
                if bit == "1":
                    path.addRect(QRectF(x0 + i * module_w, y, module_w, block.height))
            painter.fillPath(path, Qt.GlobalColor.black)
        else:
            width = self.paper_dots * 0.8
            rect = QRectF(self._x(width, block.align), y, width, block.height)
            painter.drawRect(rect)
            painter.setFont(self._font(0, False)[0])
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"[{block.symbology}] {text}")
        y += block.height

        if block.hri & 2:
            self._draw_text(painter, text, y, block.align)
