"""ESC/POS byte stream parser producing a simple layout model."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Union

ESC, GS, FS, DLE = 0x1B, 0x1D, 0x1C, 0x10
LF, CR, HT, FF = 0x0A, 0x0D, 0x09, 0x0C

DEFAULT_LINE_SPACING = 30
# Character cell sizes in dots (203 dpi) for font A and font B.
FONT_CELLS = {0: (12, 24), 1: (9, 17)}

CODEPAGES = {
    0: "cp437", 2: "cp850", 3: "cp860", 4: "cp863", 5: "cp865",
    16: "cp1252", 17: "cp866", 18: "cp852", 19: "cp858",
}

BARCODE_TYPES = {
    0: "upca", 65: "upca", 1: "upce", 66: "upce", 2: "ean13", 67: "ean13",
    3: "ean8", 68: "ean8", 4: "code39", 69: "code39", 5: "itf", 70: "itf",
    6: "codabar", 71: "codabar", 72: "code93", 73: "code128",
}

QR_ERROR_LEVELS = {48: "L", 49: "M", 50: "Q", 51: "H"}


@dataclass(frozen=True)
class Style:
    font: int = 0
    bold: bool = False
    underline: int = 0
    reverse: bool = False
    width_mul: int = 1
    height_mul: int = 1

    @property
    def cell(self) -> tuple[int, int]:
        w, h = FONT_CELLS[self.font]
        return w * self.width_mul, h * self.height_mul


@dataclass
class Bitmap:
    width: int
    height: int
    data: bytes  # row-major, MSB first, 1 = black, rows padded to whole bytes
    scale_x: int = 1
    scale_y: int = 1

    @property
    def bytes_per_row(self) -> int:
        return (self.width + 7) // 8

    @property
    def size(self) -> tuple[int, int]:
        return self.width * self.scale_x, self.height * self.scale_y


@dataclass
class TextCell:
    char: str
    style: Style

    @property
    def size(self) -> tuple[int, int]:
        return self.style.cell


@dataclass
class InlineImage:
    bitmap: Bitmap

    @property
    def size(self) -> tuple[int, int]:
        return self.bitmap.size


@dataclass
class Spacer:
    width: int

    @property
    def size(self) -> tuple[int, int]:
        return self.width, 0


LineItem = Union[TextCell, InlineImage, Spacer]


@dataclass
class Line:
    items: list[LineItem]
    align: int
    spacing: int

    @property
    def width(self) -> int:
        return sum(item.size[0] for item in self.items)

    @property
    def content_height(self) -> int:
        return max((item.size[1] for item in self.items), default=0)

    @property
    def height(self) -> int:
        return max(self.spacing, self.content_height)


@dataclass
class ImageBlock:
    bitmap: Bitmap
    align: int


@dataclass
class QRBlock:
    data: bytes
    module_size: int
    error: str
    align: int


@dataclass
class BarcodeBlock:
    symbology: str
    data: str
    height: int
    module_width: int
    hri: int  # bit 0 = above, bit 1 = below
    align: int


@dataclass
class Feed:
    dots: int


@dataclass
class Cut:
    pass


Element = Union[Line, ImageBlock, QRBlock, BarcodeBlock, Feed, Cut]


class _Truncated(Exception):
    pass


class EscPosParser:
    def __init__(self, paper_dots: int = 576) -> None:
        self.paper_dots = paper_dots
        self.elements: list[Element] = []
        self._line: list[LineItem] = []
        self._line_width = 0
        self._data = b""
        self._pos = 0
        self._reset_state()

    def _reset_state(self) -> None:
        self.style = Style()
        self.align = 0
        self.spacing = DEFAULT_LINE_SPACING
        self.codepage = "cp437"
        self.barcode_height = 162
        self.barcode_width = 3
        self.hri = 0
        self.qr_data = b""
        self.qr_module = 3
        self.qr_error = "M"
        self.graphics: Bitmap | None = None

    def parse(self, data: bytes) -> list[Element]:
        self._data = data
        self._pos = 0
        try:
            while self._pos < len(data):
                self._step(self._u8())
        except _Truncated:
            pass
        self._flush()
        return self.elements

    # -- byte readers -------------------------------------------------------

    def _u8(self) -> int:
        if self._pos >= len(self._data):
            raise _Truncated
        b = self._data[self._pos]
        self._pos += 1
        return b

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise _Truncated
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    def _u16(self) -> int:
        lo, hi = self._take(2)
        return lo | hi << 8

    def _until_nul(self) -> bytes:
        end = self._data.find(b"\0", self._pos)
        if end < 0:
            raise _Truncated
        chunk = self._data[self._pos:end]
        self._pos = end + 1
        return chunk

    # -- layout helpers -----------------------------------------------------

    def _add_item(self, item: LineItem) -> None:
        width = item.size[0]
        if self._line and self._line_width + width > self.paper_dots:
            self._newline()
        self._line.append(item)
        self._line_width += width

    def _add_char(self, char: str) -> None:
        self._add_item(TextCell(char, self.style))

    def _print_line(self, spacing: int) -> None:
        if self._line:
            self.elements.append(Line(self._line, self.align, spacing))
        else:
            self.elements.append(Feed(spacing))
        self._line = []
        self._line_width = 0

    def _newline(self) -> None:
        self._print_line(self.spacing)

    def _flush(self) -> None:
        if self._line:
            self._newline()

    # -- command dispatch ---------------------------------------------------

    def _step(self, b: int) -> None:
        if b in (LF, FF):
            self._newline()
        elif b == HT:
            cell_w = self.style.cell[0]
            for _ in range(8 - (self._line_width // cell_w) % 8):
                self._add_char(" ")
        elif b == ESC:
            self._esc(self._u8())
        elif b == GS:
            self._gs(self._u8())
        elif b == FS:
            self._fs(self._u8())
        elif b == DLE:
            self._dle(self._u8())
        elif b >= 0x20:
            self._add_char(bytes([b]).decode(self.codepage, errors="replace"))

    def _esc(self, c: int) -> None:
        if c == 0x40:  # @ initialize
            self._flush()
            self._reset_state()
        elif c in (0x45, 0x47):  # E / G bold, double-strike
            self.style = replace(self.style, bold=bool(self._u8() & 1))
        elif c == 0x2D:  # - underline
            self.style = replace(self.style, underline=self._u8() & 3)
        elif c == 0x61:  # a justification
            self.align = min(self._u8() & 3, 2)
        elif c == 0x21:  # ! print mode
            n = self._u8()
            self.style = replace(
                self.style,
                font=n & 1,
                bold=bool(n & 0x08),
                height_mul=2 if n & 0x10 else 1,
                width_mul=2 if n & 0x20 else 1,
                underline=1 if n & 0x80 else 0,
            )
        elif c == 0x4D:  # M font
            self.style = replace(self.style, font=self._u8() & 1)
        elif c == 0x32:  # 2 default line spacing
            self.spacing = DEFAULT_LINE_SPACING
        elif c == 0x33:  # 3 line spacing
            self.spacing = self._u8()
        elif c == 0x64:  # d print and feed n lines
            n = self._u8()
            if self._line:
                self._newline()
                n -= 1
            if n > 0:
                self.elements.append(Feed(n * self.spacing))
        elif c == 0x4A:  # J print and feed n dots
            self._print_line(self._u8())
        elif c == 0x74:  # t code page
            self.codepage = CODEPAGES.get(self._u8(), "cp437")
        elif c == 0x2A:  # * bit image
            self._bit_image()
        elif c == 0x24:  # $ absolute position
            pos = self._u16()
            if pos > self._line_width:
                self._add_item(Spacer(pos - self._line_width))
        elif c == 0x5C:  # \ relative position
            offset = self._u16()
            if offset < 0x8000:
                self._add_item(Spacer(offset))
        elif c in (0x69, 0x6D):  # i / m cut
            self._flush()
            self.elements.append(Cut())
        elif c == 0x70:  # p drawer kick
            self._take(3)
        elif c in (0x42, 0x63):  # B beep, c panel/sensor settings
            self._take(2)
        elif c == 0x44:  # D tab positions
            self._until_nul()
        elif c == 0x26:  # & user-defined characters
            y, c1, c2 = self._take(3)
            for _ in range(max(0, c2 - c1 + 1)):
                self._take(y * self._u8())
        elif c == 0x57:  # W print area in page mode
            self._take(8)
        elif c in b" %=?RTUVeru{":
            self._u8()

    def _gs(self, c: int) -> None:
        if c == 0x21:  # ! character size
            n = self._u8()
            self.style = replace(
                self.style, width_mul=((n >> 4) & 7) + 1, height_mul=(n & 7) + 1
            )
        elif c == 0x42:  # B reverse
            self.style = replace(self.style, reverse=bool(self._u8() & 1))
        elif c == 0x56:  # V cut
            if self._u8() in (65, 66, 97, 98, 103, 104):
                self._u8()
            self._flush()
            self.elements.append(Cut())
        elif c == 0x76:  # v 0 raster image
            if self._u8() == 0x30:
                m = self._u8()
                x_bytes, y = self._u16(), self._u16()
                bitmap = Bitmap(
                    x_bytes * 8, y, self._take(x_bytes * y),
                    scale_x=2 if m & 1 else 1, scale_y=2 if m & 2 else 1,
                )
                self._flush()
                self.elements.append(ImageBlock(bitmap, self.align))
        elif c == 0x68:  # h barcode height
            self.barcode_height = self._u8()
        elif c == 0x77:  # w barcode module width
            self.barcode_width = self._u8()
        elif c == 0x48:  # H HRI position
            self.hri = self._u8() & 3
        elif c == 0x6B:  # k barcode
            self._barcode()
        elif c == 0x28:  # ( extended commands
            fn = self._u8()
            payload = self._take(self._u16())
            if fn == 0x6B:
                self._qr(payload)
            elif fn == 0x4C:
                self._graphics(payload)
        elif c == 0x38:  # 8 L extended graphics (32-bit length)
            fn = self._u8()
            payload = self._take(int.from_bytes(self._take(4), "little"))
            if fn == 0x4C:
                self._graphics(payload)
        elif c == 0x2A:  # * downloaded bit image
            x, y = self._take(2)
            self._take(x * y * 8)
        elif c in (0x24, 0x4C, 0x50, 0x57, 0x5C):
            self._take(2)
        elif c == 0x5E:
            self._take(3)
        elif c in b"/abfIrT":
            self._u8()

    def _fs(self, c: int) -> None:
        if c == 0x28:
            self._u8()
            self._take(self._u16())
        elif c == 0x70:  # p NV image print
            self._take(2)
        elif c == 0x71:  # q define NV images
            for _ in range(self._u8()):
                x, y = self._u16(), self._u16()
                self._take(x * y * 8)
        elif c == 0x53:
            self._take(2)
        elif c in b"!-CW":
            self._u8()

    def _dle(self, c: int) -> None:
        if c in (0x04, 0x05):  # EOT / ENQ status
            self._u8()
        elif c == 0x14:  # DC4 real-time commands
            self._take(3)

    # -- graphics / codes ---------------------------------------------------

    def _bit_image(self) -> None:
        m = self._u8()
        cols = self._u16()
        col_bytes = 3 if m in (32, 33) else 1
        raw = self._take(cols * col_bytes)
        height = col_bytes * 8
        bpr = (cols + 7) // 8
        rows = bytearray(bpr * height)
        for x in range(cols):
            for yb in range(col_bytes):
                byte = raw[x * col_bytes + yb]
                if not byte:
                    continue
                for bit in range(8):
                    if byte & (0x80 >> bit):
                        rows[(yb * 8 + bit) * bpr + x // 8] |= 0x80 >> (x % 8)
        bitmap = Bitmap(
            cols, height, bytes(rows),
            scale_x=2 if m in (0, 32) else 1, scale_y=3 if m in (0, 1) else 1,
        )
        self._add_item(InlineImage(bitmap))

    def _barcode(self) -> None:
        m = self._u8()
        data = self._until_nul() if m <= 6 else self._take(self._u8())
        self._flush()
        self.elements.append(BarcodeBlock(
            BARCODE_TYPES.get(m, "unknown"), data.decode("latin-1"),
            self.barcode_height, self.barcode_width, self.hri, self.align,
        ))

    def _qr(self, p: bytes) -> None:
        if len(p) < 2 or p[0] != 49:  # cn 49 = QR Code
            return
        fn = p[1]
        if fn == 67 and len(p) > 2:
            self.qr_module = p[2]
        elif fn == 69 and len(p) > 2:
            self.qr_error = QR_ERROR_LEVELS.get(p[2], "M")
        elif fn == 80:
            self.qr_data = bytes(p[3:])
        elif fn == 81 and self.qr_data:
            self._flush()
            self.elements.append(
                QRBlock(self.qr_data, self.qr_module, self.qr_error, self.align)
            )

    def _graphics(self, p: bytes) -> None:
        if len(p) < 2:
            return
        fn = p[1]
        if fn == 112 and len(p) >= 10:  # store raster graphics
            bx, by, color = p[3], p[4], p[5]
            width, height = p[6] | p[7] << 8, p[8] | p[9] << 8
            if color == 49:  # only the first (black) color plane
                self.graphics = Bitmap(width, height, bytes(p[10:]), bx, by)
        elif fn in (2, 50) and self.graphics:  # print stored graphics
            self._flush()
            self.elements.append(ImageBlock(self.graphics, self.align))


_CODE128_ESCAPE = re.compile(r"\{(.)")


def normalize_barcode_data(symbology: str, data: str) -> str:
    """Strip ESC/POS-specific control sequences from barcode payloads."""
    if symbology == "code128":
        return _CODE128_ESCAPE.sub(lambda m: "{" if m.group(1) == "{" else "", data)
    return data
