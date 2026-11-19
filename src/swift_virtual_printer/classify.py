"""Decide whether a print job is a receipt (marked by the POS) or a miscellaneous document."""

from __future__ import annotations

from dataclasses import dataclass

from .escpos import EscPosParser, elements_to_lines

DEFAULT_MARKER = "-R-R-R-"
DEFAULT_ZREPORT_MARKER = "Z REPORT"
# Code pages tried when looking for the marker text in the raw job bytes.
_ENCODINGS = ("cp437", "cp1252", "cp1253", "cp737", "utf-8")


@dataclass(frozen=True)
class ReceiptRule:
    """A job is a receipt when its Nth printed text line (or any line) matches the pattern."""

    pattern: str = DEFAULT_MARKER
    line: int = 1  # 1 = first printed text line, 0 = any line; blank lines and images don't count
    contains: bool = False  # False: the line must equal the pattern; True: the line contains it
    remove: bool = True  # remove the marker from what gets printed

    def __str__(self) -> str:
        if not self.pattern:
            return "every job"
        how = "contains" if self.contains else "is"
        where = "any text line" if self.line == 0 else f"text line {self.line}"
        return f"{where} {how} {self.pattern!r}"

    def line_matches(self, text: str) -> bool:
        return self.pattern in text if self.contains else text == self.pattern.strip()

    def matches(self, lines: list[str]) -> bool:
        if not self.pattern:
            return True
        text_lines = [t for t in (line.strip() for line in lines) if t and not t.startswith("[IMAGE")]
        if self.line == 0:
            return any(self.line_matches(text) for text in text_lines)
        if len(text_lines) < self.line:
            return False
        return self.line_matches(text_lines[self.line - 1])

    def strip(self, data: bytes) -> bytes:
        """Remove the marker; the whole line (with its line feed) goes if nothing else is printed on it."""
        if not self.remove or not self.pattern:
            return data
        found = [(i, m) for m in {self.pattern.strip().encode(e, "ignore") for e in _ENCODINGS} if m
                 for i in [data.find(m)] if i >= 0]
        if not found:
            return data
        index, marker = min(found)
        start, end = index, index + len(marker)
        line_start = max(data.rfind(b"\n", 0, start), data.rfind(b"\r", 0, start)) + 1
        line_end = min((p for p in (data.find(b"\n", end), data.find(b"\r", end)) if p >= 0), default=len(data))
        if _has_text(data[line_start:start]) or _has_text(data[end:line_end]):
            return data[:start] + data[end:]
        while start > 0 and data[start - 1] == 0x20:
            start -= 1
        while end < len(data) and data[end] == 0x20:
            end += 1
        if data[end:end + 2] == b"\r\n":
            end += 2
        elif data[end:end + 1] in (b"\n", b"\r"):
            end += 1
        return data[:start] + data[end:]


def _has_text(chunk: bytes) -> bool:
    return any(line.strip() for line in elements_to_lines(EscPosParser().parse(chunk)))
