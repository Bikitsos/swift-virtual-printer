"""Raw TCP (JetDirect / port 9100) print server."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QNetworkInterface, QTcpServer, QTcpSocket

from .escpos import EscPosParser
from .forward import NETWORK, ForwardTarget, forward_async
from .render import PdfRenderer

log = logging.getLogger(__name__)

MAX_JOB_BYTES = 64 * 1024 * 1024
DLE_EOT = b"\x10\x04"
STATUS_ONLINE = b"\x12"


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
        save_raw: bool,
        forward_target: ForwardTarget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self.output_dir = output_dir
        self.paper_dots = paper_dots
        self.idle_ms = int(idle_timeout * 1000)
        self.save_raw = save_raw
        self.forward_target = forward_target or ForwardTarget()
        self.renderer = PdfRenderer(paper_mm, paper_dots)
        self._counter = 0
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)

    def start(self) -> None:
        if self._forwards_to_self():
            raise RuntimeError(f"Forward target {self.forward_target} is this virtual printer (would loop)")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not self._server.listen(QHostAddress(self.host), self.port):
            raise RuntimeError(f"Cannot listen on {self.host}:{self.port}: {self._server.errorString()}")
        log.info("Listening on %s:%d, writing PDFs to %s", self.host, self.port, self.output_dir.resolve())
        if self.forward_target.enabled:
            log.info("Forwarding jobs to %s", self.forward_target)

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
        stem = f"job_{datetime.now():%Y%m%d_%H%M%S}_{self._counter:04d}"
        if self.save_raw:
            (self.output_dir / f"{stem}.bin").write_bytes(data)
        if self.forward_target.enabled:
            forward_async(self.forward_target, data, stem)

        pdf_path = self.output_dir / f"{stem}.pdf"
        try:
            elements = EscPosParser(self.paper_dots).parse(data)
            if self.renderer.render(elements, pdf_path):
                log.info("Job from %s (%d bytes) -> %s", peer, len(data), pdf_path)
                self.job_saved.emit(pdf_path)
            else:
                log.info("Job from %s (%d bytes) had nothing printable", peer, len(data))
        except Exception:
            log.exception("Failed to render job from %s", peer)
