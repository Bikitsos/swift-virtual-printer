"""Raw TCP (JetDirect / port 9100) print server."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QNetworkInterface, QTcpServer, QTcpSocket

from . import APP_NAME, __version__
from .classify import ReceiptRule
from .escpos import Cut, EscPosParser, elements_to_lines
from .fiscal import FiscalSettings, SignResult, build_doc, insert_signature, sign_document
from .forward import NETWORK, ForwardTarget, forward_async
from .logo import ESC_INIT, LogoSettings, bitmap_to_escpos, inject, load_logo
from .render import PdfRenderer

log = logging.getLogger(__name__)

MAX_JOB_BYTES = 64 * 1024 * 1024
DLE_EOT = b"\x10\x04"
STATUS_ONLINE = b"\x12"
# GS V 66 0: feed to the cutter position, then partial cut.
AUTO_CUT = b"\x1dV\x42\x00"
SUBFOLDERS = ("bin", "pdf", "json", "final")


def misc_dir_for(receipts_dir: Path) -> Path:
    """Unmarked documents go to a `misc` folder next to the receipts folder."""
    return receipts_dir.parent / "misc"


def zreports_dir_for(receipts_dir: Path) -> Path:
    """Z reports go to a `zreports` folder next to the receipts folder."""
    return receipts_dir.parent / "zreports"


class PrintServer(QObject):
    job_saved = Signal(Path)
    fiscal_failed = Signal(str, str)  # job name, error
    _signed = Signal(object, str, object)

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
        auto_cut: bool = False,
        clean_jobs: bool = True,
        receipt_rule: ReceiptRule | None = None,
        zreport_rule: ReceiptRule | None = None,
        fiscal_sign: FiscalSettings | None = None,
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
        self.auto_cut = auto_cut
        self.clean_jobs = clean_jobs
        self.receipt_rule = receipt_rule or ReceiptRule()
        # None (or an empty pattern) disables Z report detection.
        self.zreport_rule = zreport_rule if zreport_rule and zreport_rule.pattern else None
        # Fiscal device (RESTful API) used to sign receipts before printing; None = no signing.
        self.fiscal_sign = fiscal_sign
        self._signed.connect(self._finish)
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
        log.info("Receipt rule: %s; other jobs are saved to %s without logo", self.receipt_rule, self.misc_dir.resolve())
        if self.zreport_rule:
            log.info("Z report rule: %s; saved to %s without logo", self.zreport_rule, self.zreports_dir.resolve())
        if self.forward_target.enabled:
            log.info("Forwarding jobs to %s", self.forward_target)
        if self.auto_cut:
            log.info("Adding a paper cut to jobs that have none")
        if self.fiscal_sign:
            log.info("Signing receipts with the fiscal device at %s", self.fiscal_sign)

    @property
    def misc_dir(self) -> Path:
        return misc_dir_for(self.output_dir)

    @property
    def zreports_dir(self) -> Path:
        return zreports_dir_for(self.output_dir)

    def _make_dirs(self) -> None:
        for root in (self.output_dir, self.misc_dir, self.zreports_dir):
            for name in SUBFOLDERS:
                (root / name).mkdir(parents=True, exist_ok=True)

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

        received_parser = EscPosParser(self.paper_dots)
        received_elements = received_parser.parse(data)
        received_lines = elements_to_lines(received_elements)
        receipt = False
        if self.zreport_rule and self.zreport_rule.matches(received_lines):
            # Z reports are checked first and never get the logo.
            root, category = self.zreports_dir, "zreport"
            printed = self.zreport_rule.strip(data)
            if self.receipt_rule.pattern and self.receipt_rule.matches(received_lines):
                printed = self.receipt_rule.strip(printed)
        elif self.receipt_rule.matches(received_lines):
            # Marked receipts: drop the marker line (the logo is added below).
            receipt = True
            root, category = self.output_dir, "receipt"
            printed = self.receipt_rule.strip(data)
        else:
            root, category = self.misc_dir, "misc"
            printed = data

        # Jobs from plain-text drivers (e.g. Windows "Generic / Text Only") don't reset the printer and can
        # leave the last line unprinted in its buffer, which then spoils the next job and blocks the cut.
        reset_added = self.clean_jobs and not printed.startswith(ESC_INIT)
        if reset_added:
            printed = ESC_INIT + printed
        if receipt and self._logo_bytes:
            printed = inject(printed, self._logo_bytes)
        line_end_added = self.clean_jobs and received_parser.unterminated
        if line_end_added:
            printed += b"\n"
        cut_added = self.auto_cut and not any(isinstance(e, Cut) for e in received_elements)
        if cut_added:
            printed += AUTO_CUT

        bin_path = root / "bin" / f"{stem}.bin"
        # bin/ and pdf/ keep the job as received; final/ holds exactly what goes to the real printer.
        bin_path.write_bytes(data)
        job = {
            "stem": stem, "peer": peer, "received": received, "data": data, "printed": printed,
            "received_elements": received_elements, "root": root, "category": category, "receipt": receipt,
            "reset_added": reset_added, "line_end_added": line_end_added, "cut_added": cut_added,
        }
        if receipt and self.fiscal_sign:
            doc = build_doc(elements_to_lines(EscPosParser(self.paper_dots).parse(printed)))
            settings = self.fiscal_sign
            log.info("Signing %s with the fiscal device...", stem)

            def sign() -> None:
                try:
                    result = sign_document(settings.host, settings.port, doc)
                except Exception as exc:  # never lose the receipt because signing crashed
                    result = SignResult(False, error=f"Signing failed: {exc}")
                try:
                    self._signed.emit(job, doc, result)
                except RuntimeError:  # server was stopped and deleted meanwhile (settings applied)
                    log.error("Print service restarted while signing %s; the job was not printed", stem)

            threading.Thread(target=sign, name=f"sign-{stem}", daemon=True).start()
        else:
            self._finish(job, "", None)

    def _finish(self, job: dict, doc: str, result: SignResult | None) -> None:
        stem, peer, data, root, category = job["stem"], job["peer"], job["data"], job["root"], job["category"]
        printed = job["printed"]
        if result is not None:
            if result.ok:
                log.info("Signed %s: %s (Z %s)", stem, result.signature, result.z_no)
            else:
                log.warning("Fiscal signing of %s failed: %s", stem, result.error)
                self.fiscal_failed.emit(stem, result.error)
            printed = insert_signature(printed, result, self.paper_dots)

        pdf_path = root / "pdf" / f"{stem}.pdf"
        json_path = root / "json" / f"{stem}.json"
        final_bin = root / "final" / f"{stem}.bin"
        final_pdf = root / "final" / f"{stem}.pdf"
        final_bin.write_bytes(printed)
        if self.forward_target.enabled:
            forward_async(self.forward_target, printed, stem)

        try:
            elements = EscPosParser(self.paper_dots).parse(printed)
            lines = elements_to_lines(elements)
            record = {
                "job": stem,
                "category": category,
                "received": job["received"].isoformat(timespec="seconds"),
                "client": peer,
                "size_bytes": len(data),
                "paper_mm": self.paper_mm,
                "app": f"{APP_NAME} {__version__}",
                "files": {
                    "bin": f"bin/{stem}.bin",
                    "pdf": f"pdf/{pdf_path.name}",
                    "final_bin": f"final/{final_bin.name}",
                    "final_pdf": f"final/{final_pdf.name}",
                },
                "logo": Path(self.logo.path).name if job["receipt"] and self._logo_bytes else None,
                "fiscal": result.as_record(doc) if result is not None else None,
                "cut_added": job["cut_added"],
                "printer_reset_added": job["reset_added"],
                "line_end_added": job["line_end_added"],
                "line_count": len(lines),
                "lines": {f"line_{i:03d}": text for i, text in enumerate(lines, 1)},
            }
            json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

            self.renderer.render(job["received_elements"], pdf_path)
            if self.renderer.render(elements, final_pdf):
                label = {"receipt": "Receipt", "zreport": "Z report"}.get(category, "Document")
                log.info("%s from %s (%d bytes) -> %s", label, peer, len(data), stem)
                self.job_saved.emit(final_pdf)
            else:
                log.info("Job from %s (%d bytes) had nothing printable", peer, len(data))
        except Exception:
            log.exception("Failed to process job from %s", peer)
