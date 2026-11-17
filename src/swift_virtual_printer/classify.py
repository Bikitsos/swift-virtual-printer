"""Decide whether a print job is a receipt (marked by the POS) or a miscellaneous document."""

from __future__ import annotations

RECEIPT_MARKER = "-R-R-R-"
_MARKER_BYTES = RECEIPT_MARKER.encode("ascii")


def is_receipt(lines: list[str]) -> bool:
    """True if the first printed text line (ignoring blank lines and images) is the receipt marker."""
    for line in lines:
        text = line.strip()
        if text and not text.startswith("[IMAGE"):
            return text == RECEIPT_MARKER
    return False


def strip_marker(data: bytes) -> bytes:
    """Remove the first marker line (marker, surrounding spaces and its line feed) from the raw bytes."""
    index = data.find(_MARKER_BYTES)
    if index < 0:
        return data
    start, end = index, index + len(_MARKER_BYTES)
    while start > 0 and data[start - 1] == 0x20:
        start -= 1
    while end < len(data) and data[end] == 0x20:
        end += 1
    if data[end:end + 2] == b"\r\n":
        end += 2
    elif data[end:end + 1] in (b"\n", b"\r"):
        end += 1
    return data[:start] + data[end:]
