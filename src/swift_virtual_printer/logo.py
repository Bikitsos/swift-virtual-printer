"""Convert a PNG/JPG logo to ESC/POS raster graphics and inject it at the top of print jobs."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from .escpos import Bitmap

ESC_INIT = b"\x1b@"
MAX_BAND_ROWS = 255  # many printers limit a single GS v 0 image height
MAX_HEIGHT = 1200


@dataclass(frozen=True)
class LogoSettings:
    path: str = ""
    width_percent: int = 60
    dither: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.path)


def load_logo(settings: LogoSettings, paper_dots: int) -> Bitmap:
    """Load, scale and convert the logo to a 1-bit bitmap (MSB first, 1 = black)."""
    source = QImage(settings.path)
    if source.isNull():
        raise ValueError(f"Cannot read logo image: {settings.path}")

    width = max(8, min(paper_dots, round(paper_dots * settings.width_percent / 100)))
    height = max(1, min(MAX_HEIGHT, round(source.height() * width / source.width())))
    scaled = source.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)

    # Flatten transparency onto white before converting to grayscale.
    canvas = QImage(width, height, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.white)
    painter = QPainter(canvas)
    painter.drawImage(0, 0, scaled)
    painter.end()
    gray = canvas.convertToFormat(QImage.Format.Format_Grayscale8)

    stride = gray.bytesPerLine()
    raw = bytes(gray.constBits())
    pixels = [raw[y * stride + x] for y in range(height) for x in range(width)]
    return Bitmap(width, height, _to_bits(pixels, width, height, settings.dither))


def _to_bits(pixels: list[int], width: int, height: int, dither: bool) -> bytes:
    bpr = (width + 7) // 8
    out = bytearray(bpr * height)
    values = [float(p) for p in pixels] if dither else pixels
    for y in range(height):
        row = y * width
        for x in range(width):
            i = row + x
            old = values[i]
            black = old < 128
            if black:
                out[y * bpr + (x >> 3)] |= 0x80 >> (x & 7)
            if dither:
                # Floyd-Steinberg error diffusion
                err = old - (0 if black else 255)
                if x + 1 < width:
                    values[i + 1] += err * 7 / 16
                if y + 1 < height:
                    if x > 0:
                        values[i + width - 1] += err * 3 / 16
                    values[i + width] += err * 5 / 16
                    if x + 1 < width:
                        values[i + width + 1] += err / 16
    return bytes(out)


def bitmap_to_escpos(bitmap: Bitmap) -> bytes:
    """Centered GS v 0 raster image(s), followed by a small feed and left alignment."""
    bpr = bitmap.bytes_per_row
    parts = [b"\x1ba\x01"]
    for top in range(0, bitmap.height, MAX_BAND_ROWS):
        rows = min(MAX_BAND_ROWS, bitmap.height - top)
        band = bitmap.data[top * bpr:(top + rows) * bpr]
        parts.append(b"\x1dv0\x00" + bytes([bpr & 0xFF, bpr >> 8, rows & 0xFF, rows >> 8]) + band)
    parts.append(b"\x1bJ\x10\x1ba\x00")
    return b"".join(parts)


def inject(data: bytes, logo: bytes) -> bytes:
    """Insert the logo after any leading ESC @ so the job's own reset doesn't follow it."""
    pos = 0
    while data.startswith(ESC_INIT, pos):
        pos += len(ESC_INIT)
    return data[:pos] + logo + data[pos:]
