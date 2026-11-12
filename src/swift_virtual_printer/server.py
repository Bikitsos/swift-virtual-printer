"""Raw TCP (JetDirect / port 9100) print server."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QNetworkInterface, QTcpServer, QTcpSocket

from . import APP_NAME, __version__
from .escpos import EscPosParser, elements_to_lines
from .forward import NETWORK, ForwardTarget, forward_async
from .logo import LogoSettings, bitmap_to_escpos, inject, load_logo
from .render import PdfRenderer

log = logging.getLogger(__name__)

MAX_JOB_BYTES = 64 * 1024 * 1024
DLE_EOT = b"\x10\x04"
STATUS_ONLINE = b"\x12"
SUBFOLDERS = ("bin", "pdf", "json", "final")


class PrintServer(QObject):
    job_saved = Signal(Path)

    def __init__(
        self,
        host: str,
        port: int,
        output_dir: Path,
        paper_mm: float,
        paper_dots: int,
        idle_timeout: float,
        forward_target: ForwardTarget | None = None,
        logo: LogoSettings | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self.output_dir = output_dir
        self.paper_mm = paper_mm
        self.paper_dots = paper_dots
        self.idle_ms = int(idle_timeout * 1000)
        self.forward_target = forward_target or ForwardTarget()
        self.logo = logo or LogoSettings()
        self._logo_bytes = b""
        self.renderer = PdfRenderer(paper_mm, paper_dots)
        self._counter = 0
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)

    def start(self) -> None:
        if self._forwards_to_self():
            raise RuntimeError(f"Forward target {self.forward_target} is this virtual printer (would loop)")
        if self.logo.enabled:
            try:
                bitmap = load_logo(self.logo, self.paper_dots)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            self._logo_bytes = bitmap_to_escpos(bitmap)
            log.info("Adding logo %s (%dx%d dots) to every job", Path(self.logo.path).name, bitmap.width, bitmap.height)
        self._make_dirs()
        if not self._server.listen(QHostAddress(self.host), self.port):
            raise RuntimeError(f"Cannot listen on {self.host}:{self.port}: {self._server.errorString()}")
        log.info("Listening on %s:%d, saving receipts to %s", self.host, self.port, self.output_dir.resolve())
        if self.forward_target.enabled:
            log.info("Forwarding jobs to %s", self.forward_target)

    def _make_dirs(self) -> None:
        for name in SUBFOLDERS:
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _forwards_to_self(self) -> bool:
        target = self.forward_target
        if target.kind != NETWORK or target.port != self.port:
            return False
        if target.host.lower() == "localhost":
            return True
        address = QHostAddress(target.host)
        return address.isLoopback() or address in QNetworkInterface.allAddresses()

    def stop(self) -> None:
        if self._server.isListening():
            self._server.close()
            log.info("Stopped listening on %s:%d", self.host, self.port)

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            self._accept(self._server.nextPendingConnection())

    def _accept(self, sock: QTcpSocket) -> None:
        peer = f"{sock.peerAddress().toString()}:{sock.peerPort()}"
        log.info("Connection from %s", peer)
        buffer = bytearray()
        finished = False

        # Some clients keep the socket open after sending; treat inactivity as end of job.
        timer = QTimer(sock)
        timer.setSingleShot(True)
        timer.setInterval(self.idle_ms)
        timer.timeout.connect(lambda: sock.disconnectFromHost())

        def on_ready_read() -> None:
            chunk = sock.readAll().data()
            if not chunk:
                return
            if len(buffer) + len(chunk) > MAX_JOB_BYTES:
                log.warning("Job from %s exceeds %d bytes, dropping connection", peer, MAX_JOB_BYTES)
                buffer.clear()
                sock.abort()
                return
            buffer.extend(chunk)
            # Answer real-time status requests so drivers that poll the printer don't stall.
            for _ in range(chunk.count(DLE_EOT)):
                sock.write(STATUS_ONLINE)
            timer.start()

        def on_disconnected() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            on_ready_read()
            timer.stop()
            sock.deleteLater()
            self._process(bytes(buffer), peer)

        sock.readyRead.connect(on_ready_read)
        sock.disconnected.connect(on_disconnected)
        timer.start()

    def _process(self, data: bytes, peer: str) -> None:
        if not data.strip(b"\0"):
            log.info("Empty job from %s ignored", peer)
            return
        self._counter += 1
        received = datetime.now()
        stem = f"job_{received:%Y%m%d_%H%M%S}_{self._counter:04d}"
        self._make_dirs()
        bin_path = self.output_dir / "bin" / f"{stem}.bin"
        pdf_path = self.output_dir / "pdf" / f"{stem}.pdf"
        json_path = self.output_dir / "json" / f"{stem}.json"
        final_bin = self.output_dir / "final" / f"{stem}.bin"
        final_pdf = self.output_dir / "final" / f"{stem}.pdf"
        bin_path.write_bytes(data)
        # bin/ and pdf/ keep the job as received; final/ holds exactly what goes to the real printer.
        printed = inject(data, self._logo_bytes) if self._logo_bytes else data
        final_bin.write_bytes(printed)
        if self.forward_target.enabled:
            forward_async(self.forward_target, printed, stem)

        try:
            received_elements = EscPosParser(self.paper_dots).parse(data)
            elements = EscPosParser(self.paper_dots).parse(printed)
            lines = elements_to_lines(elements)
            record = {
                "job": stem,
                "received": received.isoformat(timespec="seconds"),
                "client": peer,
                "size_bytes": len(data),
                "paper_mm": self.paper_mm,
                "app": f"{APP_NAME} {__version__}",
                "files": {
                    "bin": f"bin/{bin_path.name}",
                    "pdf": f"pdf/{pdf_path.name}",
                    "final_bin": f"final/{final_bin.name}",
                    "final_pdf": f"final/{final_pdf.name}",
                },
                "logo": Path(self.logo.path).name if self._logo_bytes else None,
                "line_count": len(lines),
                "lines": {f"line_{i:03d}": text for i, text in enumerate(lines, 1)},
            }
            json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

            self.renderer.render(received_elements, pdf_path)
            if self.renderer.render(elements, final_pdf):
                log.info("Job from %s (%d bytes) -> %s", peer, len(data), stem)
                self.job_saved.emit(final_pdf)
            else:
                log.info("Job from %s (%d bytes) had nothing printable", peer, len(data))
        except Exception:
            log.exception("Failed to process job from %s", peer)
