"""Forward raw print jobs to a real printer (network raw TCP or Windows print queue)."""

from __future__ import annotations

import logging
import socket
import sys
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

OFF, NETWORK, WINDOWS = "off", "network", "windows"


@dataclass(frozen=True)
class ForwardTarget:
    kind: str = OFF
    host: str = ""
    port: int = 9100
    printer: str = ""

    @property
    def enabled(self) -> bool:
        return (self.kind == NETWORK and bool(self.host)) or (self.kind == WINDOWS and bool(self.printer))

    def __str__(self) -> str:
        if self.kind == NETWORK:
            return f"{self.host}:{self.port}"
        if self.kind == WINDOWS:
            return f'Windows printer "{self.printer}"'
        return "off"


def send_network(host: str, port: int, data: bytes, timeout: float = 15.0) -> None:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(data)


def list_windows_printers() -> list[str]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PRINTER_INFO_4(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]

    flags = 0x2 | 0x4  # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed, count = wintypes.DWORD(), wintypes.DWORD()
    winspool = ctypes.WinDLL("winspool.drv")
    winspool.EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(count))
    if not needed.value:
        return []
    buffer = ctypes.create_string_buffer(needed.value)
    if not winspool.EnumPrintersW(flags, None, 4, buffer, needed, ctypes.byref(needed), ctypes.byref(count)):
        return []
    infos = ctypes.cast(buffer, ctypes.POINTER(PRINTER_INFO_4))
    return sorted(infos[i].pPrinterName for i in range(count.value))


def send_windows(printer: str, data: bytes, doc_name: str) -> None:
    """Send bytes to a Windows print queue using the RAW datatype (bypasses the driver)."""
    if sys.platform != "win32":
        raise OSError("Windows printers are only supported on Windows")
    import ctypes
    from ctypes import wintypes

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [
            ("pDocName", wintypes.LPWSTR),
            ("pOutputFile", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR),
        ]

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    winspool.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID]
    winspool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1)]
    winspool.StartDocPrinterW.restype = wintypes.DWORD
    winspool.WritePrinter.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    for name in ("StartPagePrinter", "EndPagePrinter", "EndDocPrinter", "ClosePrinter"):
        getattr(winspool, name).argtypes = [wintypes.HANDLE]

    def check(ok) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    handle = wintypes.HANDLE()
    check(winspool.OpenPrinterW(printer, ctypes.byref(handle), None))
    try:
        check(winspool.StartDocPrinterW(handle, 1, ctypes.byref(DOC_INFO_1(doc_name, None, "RAW"))))
        try:
            check(winspool.StartPagePrinter(handle))
            buffer = ctypes.create_string_buffer(data, len(data))
            written = wintypes.DWORD()
            check(winspool.WritePrinter(handle, buffer, len(data), ctypes.byref(written)))
            if written.value != len(data):
                raise OSError(f"Only {written.value} of {len(data)} bytes written")
            check(winspool.EndPagePrinter(handle))
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)


def forward(target: ForwardTarget, data: bytes, doc_name: str) -> None:
    if target.kind == NETWORK:
        send_network(target.host, target.port, data)
    elif target.kind == WINDOWS:
        send_windows(target.printer, data, doc_name)


def forward_async(target: ForwardTarget, data: bytes, doc_name: str) -> None:
    def run() -> None:
        try:
            forward(target, data, doc_name)
            log.info("Forwarded %s (%d bytes) to %s", doc_name, len(data), target)
        except Exception as exc:
            log.error("Forwarding %s to %s failed: %s", doc_name, target, exc)

    threading.Thread(target=run, name=f"forward-{doc_name}", daemon=True).start()
