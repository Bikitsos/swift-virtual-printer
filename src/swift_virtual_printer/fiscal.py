"""Read the status of a Cy-Net 100 fiscal device through its driver (RESTful API or file-based FES32 interface)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .escpos import FONT_CELLS, EscPosParser

OFF, REST, FILES = "off", "rest", "files"
DEFAULT_COMMAND_DIR = r"C:\FISCAL_REQUESTANSWER\COMMAND"
DEFAULT_ANSWER_DIR = r"C:\FISCAL_REQUESTANSWER\ANSWER"
# Our own request number so we don't collide with the POS's command.txt / answer.txt.
FILE_REQUEST_ID = "918"

# RESTful API error codes (manual section 2.3)
REST_ERRORS = {
    1: "Wrong number of fields", 2: "Field too long", 3: "Field too small", 4: "Field fixed size mismatch",
    5: "Field range or type check failed", 6: "Bad request code", 9: "Printing type bad",
    10: "Cannot execute with day open", 11: "RTC programming requires jumper", 12: "RTC date or time invalid",
    13: "No records in fiscal period", 14: "Device is busy in another task", 15: "No more header records allowed",
    16: "Cannot execute with block open", 17: "Block not open", 18: "Bad data stream", 19: "Bad signature field",
    20: "Z closure time limit (24 h passed, issue a Z report)", 21: "Z closure not found", 22: "Z closure record bad",
    23: "User browsing in progress on the device", 24: "Signature daily limit reached (issue a Z report)",
    25: "Printer paper end detected", 26: "Printer is offline", 27: "Fiscal unit is offline",
    28: "Fatal hardware error", 29: "Fiscal unit is full", 30: "No data passed for signature",
    31: "Signature does not exist", 32: "Battery fault detected", 33: "Recovery in progress",
    34: "Recovery only after CMOS reset", 35: "Real-time clock needs programming", 36: "Z closure date warning",
    37: "Bad character in stream", 129: "Communication error (device disconnected or inaccessible)",
}

# File-based status flags (manual section 1.3)
FLAG1 = {8: "Data sign process in progress", 4: "Device is in re-feed mode", 2: "Daily zeroset process in progress"}
FLAG2 = {
    128: "Fiscal memory full", 64: "30 or fewer daily zerosets left in fiscal memory", 2: "Paper end on device",
    1: "More than 24 h since the first sign: issue the daily Z report",
}
FLAG3 = {
    64: "Unknown command or data in command file", 32: "Unacceptable characters in invoice file",
    16: "Invoice file was not found", 8: "Wrong invoice file name", 4: "Device is not in re-feed mode",
    2: "The chosen daily sign does not exist", 1: "An unexpected error occurred",
}


@dataclass(frozen=True)
class FiscalSettings:
    mode: str = OFF
    host: str = "127.0.0.1"
    port: int = 9999
    command_dir: str = DEFAULT_COMMAND_DIR
    answer_dir: str = DEFAULT_ANSWER_DIR

    @property
    def enabled(self) -> bool:
        return self.mode in (REST, FILES)

    def __str__(self) -> str:
        if self.mode == REST:
            return f"RESTful API http://{self.host}:{self.port}"
        if self.mode == FILES:
            return f"file driver {self.command_dir}"
        return "off"


@dataclass(frozen=True)
class FiscalStatus:
    state: str  # "ok", "warning", "error", "disconnected" or "unknown"
    summary: str
    problems: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)
    source: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def check_status(settings: FiscalSettings) -> FiscalStatus:
    if settings.mode == REST:
        return _rest_status(settings.host, settings.port)
    if settings.mode == FILES:
        return _file_status(Path(settings.command_dir), Path(settings.answer_dir))
    return FiscalStatus("unknown", "Fiscal device not set up")


# -- RESTful API ---------------------------------------------------------------

def _rest_status(host: str, port: int, timeout: float = 8.0) -> FiscalStatus:
    source = "RESTful API"
    request = urllib.request.Request(f"http://{host}:{port}/check_system", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return FiscalStatus("error", f"Driver returned HTTP {exc.code}", source=source)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return FiscalStatus("unknown", f"Fiscal driver not reachable ({reason})", source=source)
    except ValueError:
        return FiscalStatus("error", "Driver sent an invalid response", source=source)

    result = int(body.get("result", -1))
    info = body.get("details") or {}
    labels = {
        "status1": "Status 1", "status2": "Status 2", "last_z_no": "Last Z number",
        "daily_signatures": "Daily signatures", "total_signatures": "Total signatures",
        "block_count": "Block count", "remaining_block_signatures": "Remaining block signatures",
    }
    details = {label: str(info[key]) for key, label in labels.items() if key in info}
    if result == 0:
        return FiscalStatus("ok", "Ready", details=details, source=source)
    message = body.get("exresult") or REST_ERRORS.get(result, f"Error {result}")
    state = "disconnected" if result == 129 else "error"
    return FiscalStatus(state, message, (f"{message} (code {result})",), details, source)


# -- Signing (RESTful API sign_doc) --------------------------------------------------

IMAGE_PLACEHOLDER = "[ΕΙΚΟΝΑ]"  # required by the driver wherever a graphic is printed
DOC_ENCODING = "cp1253"  # Greek Windows code page, one of those the driver accepts
NO_SIGNATURE_TEXT = ("THERE IS NO FISCAL SIGNATURE", "DUE TO FISCAL DEVICE FAILURE")


@dataclass(frozen=True)
class SignResult:
    ok: bool
    signature: str = ""
    device_serial: str = ""
    z_no: str = ""
    daily_signatures: str = ""
    total_signatures: str = ""
    date: str = ""  # ddmmyy
    time: str = ""  # hhmmss
    error: str = ""

    @property
    def printed_datetime(self) -> str:
        d, t = self.date, self.time
        if len(d) == 6 and len(t) >= 4:
            return f"{d[0:2]}/{d[2:4]}/{d[4:6]} {t[0:2]}:{t[2:4]}"
        return f"{d} {t}".strip()

    def as_record(self, doc: str) -> dict:
        record = {"signed": self.ok, "doc": doc}
        if self.ok:
            record.update(
                signature=self.signature, device_serial=self.device_serial, z_no=self.z_no,
                daily_signatures=self.daily_signatures, total_signatures=self.total_signatures,
                date=self.date, time=self.time,
            )
        else:
            record["error"] = self.error
        return record


def build_doc(lines: list[str]) -> str:
    """Printable text of a receipt for sign_doc: graphics become [ΕΙΚΟΝΑ], cuts and outer blank lines go."""
    out = []
    for line in lines:
        if line.startswith(("[IMAGE", "[QR]", "[BARCODE")):
            out.append(IMAGE_PLACEHOLDER)
        elif line != "[CUT]":
            out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    # Only characters the driver accepts; anything else would be rejected as a bad character.
    text = "\r\n".join(out)
    return text.encode(DOC_ENCODING, errors="replace").decode(DOC_ENCODING)


def sign_document(host: str, port: int, doc: str, timeout: float = 10.0) -> SignResult:
    request = urllib.request.Request(
        f"http://{host}:{port}/sign_doc",
        data=json.dumps({"doc": doc}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return SignResult(False, error=f"Driver returned HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return SignResult(False, error=f"Fiscal driver not reachable ({getattr(exc, 'reason', exc)})")
    except ValueError:
        return SignResult(False, error="Driver sent an invalid response")

    result = int(body.get("result", -1))
    info = body.get("details") or {}
    if result != 0:
        message = body.get("exresult") or REST_ERRORS.get(result, f"Error {result}")
        return SignResult(False, error=f"{message} (code {result})")
    signature = str(info.get("signature") or body.get("exresult") or "")
    if not signature:
        return SignResult(False, error="Driver returned no signature")
    return SignResult(
        True, signature, str(info.get("device_serial_no", "")), str(info.get("signature_z_no", "")),
        str(info.get("daily_signatures", "")), str(info.get("total_signatures", "")),
        str(info.get("date", "")), str(info.get("time", "")),
    )


def signature_lines(result: SignResult, width: int) -> list[str]:
    """Text lines of the block printed under a receipt (centred by the printer)."""
    rule = "-" * width
    if not result.ok:
        return [rule, *NO_SIGNATURE_TEXT, rule]
    sig = result.signature
    lines = [rule, "FISCAL SIGNATURE", sig[:20], sig[20:40]]
    # Each entry is printed on one line if it fits the paper, otherwise one part per line.
    for parts in (
        (f"Device: {result.device_serial}", f"Z: {result.z_no}"),
        (f"Sign: {result.daily_signatures} / {result.total_signatures}", result.printed_datetime),
    ):
        joined = "   ".join(p for p in parts if p)
        lines.extend([joined] if len(joined) <= width else [p for p in parts if p])
    return [line for line in lines if line] + [rule]


def signature_escpos(result: SignResult, width: int) -> bytes:
    """ESC/POS bytes for the signature block: normal font, centred, then left alignment again."""
    text = "".join(f"{line[:width]}\n" for line in signature_lines(result, width))
    return b"\x1b!\x00\x1d!\x00\x1dB\x00\x1ba\x01" + text.encode("ascii", errors="replace") + b"\x1ba\x00"


# Big bold centred "COPY" printed at the top of reprints of signed receipts (GS ! 0x22 = triple size).
COPY_HEADER = b"\x1ba\x01\x1bE\x01\x1d!\x22COPY\n\x1d!\x00\x1bE\x00\x1ba\x00"


def chars_per_line(paper_dots: int) -> int:
    return paper_dots // FONT_CELLS[0][0]


def insert_signature(printed: bytes, result: SignResult, paper_dots: int) -> bytes:
    """Insert the signature block after the last printed text line, before the feed and cut."""
    parser = EscPosParser(paper_dots)
    parser.parse(printed)
    position = parser.text_end_before_cut if parser.text_end_before_cut is not None else parser.text_end
    if position is None:
        position = len(printed)
    return printed[:position] + signature_escpos(result, chars_per_line(paper_dots)) + printed[position:]


def replace_signature(final: bytes, result: SignResult, paper_dots: int) -> bytes:
    """Swap the 'no fiscal signature' notice of an unsigned receipt for a new signature block."""
    width = chars_per_line(paper_dots)
    failed = signature_escpos(SignResult(False), width)
    if failed in final:
        return final.replace(failed, signature_escpos(result, width), 1)
    return insert_signature(final, result, paper_dots)


# -- File-based driver -------------------------------------------------------------

def _decode_flags(value: int, table: dict[int, str]) -> list[str]:
    return [text for bit, text in table.items() if value & bit]


def _file_status(command_dir: Path, answer_dir: Path, timeout: float = 15.0) -> FiscalStatus:
    source = "file driver"
    if not command_dir.is_dir() or not answer_dir.is_dir():
        return FiscalStatus("unknown", "Command or answer folder not found", source=source)
    command = command_dir / f"command{FILE_REQUEST_ID}.txt"
    answer = answer_dir / f"answer{FILE_REQUEST_ID}.txt"
    answer.unlink(missing_ok=True)

    # Write under a temporary name and rename, so the driver never sees a half-written command.
    temp = command_dir / f"svp{FILE_REQUEST_ID}.tmp"
    try:
        temp.write_bytes(b"S")
        os.replace(temp, command)
    except OSError as exc:
        return FiscalStatus("error", f"Cannot write command file ({exc.strerror or exc})", source=source)

    deadline = time.monotonic() + timeout
    text = None
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                raw = answer.read_bytes()
                if raw.strip():
                    text = raw.decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("cp1253", errors="replace")
                    answer.unlink(missing_ok=True)
                    break
            except OSError:
                pass  # the driver may still hold the file open
        time.sleep(0.25)
    if text is None:
        command.unlink(missing_ok=True)
        return FiscalStatus("unknown", "No answer from the fiscal driver (is it running?)", source=source)
    return parse_status_answer(text, source)


def parse_status_answer(text: str, source: str = "file driver") -> FiscalStatus:
    lines = [line.strip().lstrip("\ufeff") for line in text.splitlines()]
    result = lines[0] if lines else ""
    if result == "2":
        return FiscalStatus("disconnected", "Fiscal device disconnected", ("Fiscal device disconnected",), source=source)

    problems: list[str] = []
    if len(lines) > 1:
        flags = [int(f) for f in lines[1].split() if f.isdigit()]
        for value, table in zip(flags, (FLAG1, FLAG2, FLAG3)):
            problems += _decode_flags(value, table)

    labels = (
        "Daily signatures", "Total zerosets (Z)", "Device date/time", "Fiscal letters and number",
        "Firmware", "Model", "Total signatures",
    )
    details = {label: value for label, value in zip(labels, lines[2:9]) if value}
    stamp = details.get("Device date/time", "")
    if len(stamp) == 10 and stamp.isdigit():
        details["Device date/time"] = f"{stamp[4:6]}/{stamp[2:4]}/20{stamp[0:2]} {stamp[6:8]}:{stamp[8:10]}"

    if result == "0":
        state = "warning" if problems else "ok"
        summary = "Ready" if not problems else problems[0]
        return FiscalStatus(state, summary, tuple(problems), details, source)
    if result == "1":
        return FiscalStatus("error", problems[0] if problems else "Device reported a failure", tuple(problems), details, source)
    return FiscalStatus("error", f"Unexpected answer: {result!r}", source=source)
