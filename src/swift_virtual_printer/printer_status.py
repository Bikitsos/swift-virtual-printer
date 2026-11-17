"""Check whether the real (forward-to) printer is online and has paper.

- Network printers: ESC/POS real-time status (DLE EOT 1-4) over raw TCP.
- Windows printers: Epson Status API (installed with the Epson Advanced Printer Driver) when
  available, otherwise the Windows print queue status.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass

from .forward import NETWORK, WINDOWS, ForwardTarget


@dataclass(frozen=True)
class PrinterStatus:
    online: bool | None  # None = could not be determined
    problems: tuple[str, ...] = ()
    source: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.online) and not self.problems

    def __str__(self) -> str:
        if self.online is None:
            state = "Unknown"
        elif self.ok:
            return "Online, ready"
        else:
            state = "Online" if self.online else "Offline"
        return f"{state}: {', '.join(self.problems)}" if self.problems else state


def check_status(target: ForwardTarget) -> PrinterStatus:
    if target.kind == NETWORK:
        return _network_status(target.host, target.port)
    if target.kind == WINDOWS:
        return _epson_status(target.printer) or _spooler_status(target.printer)
    return PrinterStatus(None, ("no printer set up",))


# -- network: ESC/POS DLE EOT ------------------------------------------------

def _network_status(host: str, port: int, timeout: float = 2.0) -> PrinterStatus:
    source = "ESC/POS status"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            replies = []
            for n in (1, 2, 3, 4):
                sock.sendall(bytes([0x10, 0x04, n]))
                reply = sock.recv(1)
                if not reply:
                    return PrinterStatus(False, ("connection closed",), source)
                replies.append(reply[0])
    except socket.timeout:
        return PrinterStatus(False, ("no response",), source)
    except OSError as exc:
        return PrinterStatus(False, (f"unreachable ({exc.strerror or exc})",), source)

    printer, offline, error, paper = replies
    problems = []
    if offline & 0x04:
        problems.append("cover open")
    if offline & 0x08:
        problems.append("paper feed button pressed")
    if error & 0x08:
        problems.append("cutter error")
    if error & 0x20:
        problems.append("unrecoverable error")
    if error & 0x40:
        problems.append("print head too hot / recoverable error")
    if paper & 0x60:
        problems.append("paper out")
    elif paper & 0x0C:
        problems.append("paper near end")
    return PrinterStatus(not printer & 0x08, tuple(problems), source)


# -- Windows: Epson Status API ----------------------------------------------

# Epson StatusAPI ASB bits
_ASB = {
    0x00000020: "cover open",
    0x00000040: "paper feed button pressed",
    0x00000400: "mechanical error",
    0x00000800: "cutter error",
    0x00002000: "unrecoverable error",
    0x00004000: "recoverable error",
    0x00080000: "paper out",
}
_ASB_NO_RESPONSE, _ASB_OFF_LINE, _ASB_NEAR_END, _ASB_END = 0x1, 0x8, 0x00020000, 0x00080000


def _epson_status(printer: str) -> PrinterStatus | None:
    """Status via EpsStmApi.dll from the Epson APD; None if the API isn't available for this printer."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        api = ctypes.WinDLL("EpsStmApi.dll")
        api.BiOpenMonPrinter.argtypes = [ctypes.c_int, ctypes.c_char_p]
        api.BiOpenMonPrinter.restype = ctypes.c_int
        api.BiGetStatus.argtypes = [ctypes.c_int, ctypes.POINTER(wintypes.DWORD)]
        api.BiGetStatus.restype = ctypes.c_int
        api.BiCloseMonPrinter.argtypes = [ctypes.c_int]
    except (OSError, AttributeError):
        return None

    handle = api.BiOpenMonPrinter(2, printer.encode("mbcs", errors="replace"))  # 2 = TYPE_PRINTER
    if handle < 0:
        return None
    try:
        status = wintypes.DWORD()
        if api.BiGetStatus(handle, ctypes.byref(status)) != 0:
            return None
    finally:
        api.BiCloseMonPrinter(handle)

    asb, source = status.value, "Epson Status API"
    if asb & _ASB_NO_RESPONSE:
        return PrinterStatus(False, ("no response (turned off or unplugged)",), source)
    problems = [text for bit, text in _ASB.items() if asb & bit]
    if asb & _ASB_NEAR_END and not asb & _ASB_END:
        problems.append("paper near end")
    return PrinterStatus(not asb & _ASB_OFF_LINE, tuple(problems), source)


# -- Windows: print queue ------------------------------------------------------

_PRINTER_STATUS = {
    0x00000001: "queue paused",
    0x00000002: "error",
    0x00000008: "paper jam",
    0x00000010: "paper out",
    0x00000040: "paper problem",
    0x00001000: "not available",
    0x00100000: "needs attention",
    0x00200000: "out of memory",
    0x00400000: "cover open",
}
_STATUS_OFFLINE, _ATTRIBUTE_WORK_OFFLINE = 0x00000080, 0x00000400
_JOB_PROBLEM = 0x2 | 0x20 | 0x40 | 0x200 | 0x400  # error, offline, paper out, blocked, user intervention


def _spooler_status(printer: str) -> PrinterStatus:
    source = "Windows print queue"
    if sys.platform != "win32":
        return PrinterStatus(None, ("Windows printers are only supported on Windows",), source)
    import ctypes
    from ctypes import wintypes

    class PRINTER_INFO_2(ctypes.Structure):
        _fields_ = [(name, wintypes.LPWSTR) for name in (
            "pServerName", "pPrinterName", "pShareName", "pPortName", "pDriverName", "pComment",
            "pLocation")] + [("pDevMode", ctypes.c_void_p)] + [(name, wintypes.LPWSTR) for name in (
            "pSepFile", "pPrintProcessor", "pDatatype", "pParameters")] + [
            ("pSecurityDescriptor", ctypes.c_void_p)] + [(name, wintypes.DWORD) for name in (
            "Attributes", "Priority", "DefaultPriority", "StartTime", "UntilTime", "Status", "cJobs",
            "AveragePPM")]

    class JOB_INFO_1(ctypes.Structure):
        _fields_ = [("JobId", wintypes.DWORD)] + [(name, wintypes.LPWSTR) for name in (
            "pPrinterName", "pMachineName", "pUserName", "pDocument", "pDatatype", "pStatus")] + [
            (name, wintypes.DWORD) for name in ("Status", "Priority", "Position", "TotalPages", "PagesPrinted")] + [
            ("Submitted", wintypes.WORD * 8)]

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer, ctypes.byref(handle), None):
        return PrinterStatus(False, (f"printer not found ({ctypes.WinError(ctypes.get_last_error()).strerror})",), source)
    try:
        needed = wintypes.DWORD()
        winspool.GetPrinterW(handle, 2, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not winspool.GetPrinterW(handle, 2, buffer, needed, ctypes.byref(needed)):
            return PrinterStatus(None, ("cannot read queue status",), source)
        info = ctypes.cast(buffer, ctypes.POINTER(PRINTER_INFO_2)).contents
        status, attributes, jobs = info.Status, info.Attributes, info.cJobs

        stuck = 0
        if jobs:
            returned = wintypes.DWORD()
            winspool.EnumJobsW(handle, 0, 64, 1, None, 0, ctypes.byref(needed), ctypes.byref(returned))
            job_buffer = ctypes.create_string_buffer(max(needed.value, 1))
            if winspool.EnumJobsW(handle, 0, 64, 1, job_buffer, needed, ctypes.byref(needed), ctypes.byref(returned)):
                job_list = ctypes.cast(job_buffer, ctypes.POINTER(JOB_INFO_1))
                stuck = sum(1 for i in range(returned.value) if job_list[i].Status & _JOB_PROBLEM)
    finally:
        winspool.ClosePrinter(handle)

    problems = [text for bit, text in _PRINTER_STATUS.items() if status & bit]
    if attributes & _ATTRIBUTE_WORK_OFFLINE:
        problems.append("set to 'Use printer offline'")
    if stuck:
        problems.append(f"{stuck} job(s) stuck in the queue")
    elif jobs > 2:
        problems.append(f"{jobs} jobs waiting")
    offline = bool(status & _STATUS_OFFLINE or attributes & _ATTRIBUTE_WORK_OFFLINE or stuck)
    return PrinterStatus(not offline, tuple(problems), source)
