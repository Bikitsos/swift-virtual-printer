"""System tray application for the virtual ESC/POS printer."""

from __future__ import annotations

import argparse
import logging
import platform
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal, qVersion
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, COMPANY, CREDITS, __version__, app_root, build_info, default_receipts_dir
from .admin import DEFAULT_PASSWORD_HASH, delete_job_files, folder_usage, format_size, hash_password, verify_password
from .escpos import EscPosParser, elements_to_lines
from .classify import DEFAULT_MARKER, DEFAULT_ZREPORT_MARKER, ReceiptRule
from .forward import NETWORK, OFF, WINDOWS, ForwardTarget, forward_async, list_windows_printers
from .logo import LogoSettings, load_logo
from .render import PdfRenderer, bitmap_to_qimage
from .main import PAPER_SIZES
from .server import PrintServer, misc_dir_for, zreports_dir_for
from .printer_status import PrinterStatus, check_status
from .updater import Release, UpdateChecker, UpdateDownloader, can_self_update, is_newer, run_installer

DEFAULT_OUTPUT = default_receipts_dir()
LOGO_PREVIEW_WIDTH = 400
PRINTER_CHECK_SECONDS = 30


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #777;")
    return label


def make_icon(active: bool) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    outline = QPen(QColor("#333333"), 3)
    p.setPen(outline)
    p.setBrush(QColor("white"))
    p.drawRect(18, 6, 28, 18)
    p.setBrush(QColor("#2e7d32") if active else QColor("#9e9e9e"))
    p.drawRoundedRect(6, 22, 52, 26, 6, 6)
    p.setBrush(QColor("white"))
    p.drawRect(16, 40, 32, 20)
    p.setPen(QPen(QColor("#333333"), 2))
    p.drawLine(21, 47, 43, 47)
    p.drawLine(21, 53, 38, 53)
    p.end()
    return QIcon(pixmap)


class LogEmitter(QObject):
    message = Signal(str)


@dataclass(frozen=True)
class Job:
    """Files of one print job inside the receipts folder."""

    root: Path
    stem: str

    def _file(self, folder: str, ext: str) -> Path:
        return self.root / folder / f"{self.stem}.{ext}"

    @property
    def bin(self) -> Path:
        return self._file("bin", "bin")

    @property
    def pdf(self) -> Path:
        return self._file("pdf", "pdf")

    @property
    def json(self) -> Path:
        return self._file("json", "json")

    @property
    def final_bin(self) -> Path:
        return self._file("final", "bin")

    @property
    def final_pdf(self) -> Path:
        return self._file("final", "pdf")

    @property
    def best_pdf(self) -> Path:
        return self.final_pdf if self.final_pdf.exists() else self.pdf

    @property
    def print_bytes_path(self) -> Path | None:
        """Final bytes (with logo) if saved, otherwise the bytes as received."""
        return next((p for p in (self.final_bin, self.bin) if p.exists()), None)


class PreviewDialog(QDialog):
    ZOOMS = (50, 75, 100, 125, 150)

    def __init__(self, window: MainWindow, job: Job) -> None:
        super().__init__(window)
        self._window = window
        self.job = job
        self.setWindowTitle(f"Preview - {job.stem}")
        self.resize(560, 760)

        self.source = QComboBox()
        if job.final_bin.exists():
            self.source.addItem("Final (as printed, with logo)", job.final_bin)
        if job.bin.exists():
            self.source.addItem("As received from the POS", job.bin)
        self.zoom = QComboBox()
        for zoom in self.ZOOMS:
            self.zoom.addItem(f"{zoom}%", zoom)
        self.zoom.setCurrentIndex(self.ZOOMS.index(75))
        self.source.currentIndexChanged.connect(self._render)
        self.zoom.currentIndexChanged.connect(self._show_image)
        top = QHBoxLayout()
        top.addWidget(QLabel("Show"))
        top.addWidget(self.source, 1)
        top.addWidget(QLabel("Zoom"))
        top.addWidget(self.zoom)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        scroll.setStyleSheet("QScrollArea { background: #9e9e9e; } QLabel { background: #9e9e9e; }")
        scroll.setWidget(self.image_label)

        buttons = QDialogButtonBox()
        reprint = buttons.addButton("Reprint", QDialogButtonBox.ButtonRole.ActionRole)
        reprint.setEnabled(job.print_bytes_path is not None)
        target = window.active_forward_target()
        reprint.setToolTip(f"Send to {target}" if target.enabled else "Set up a real printer on the Forwarding tab first")
        reprint.clicked.connect(lambda: window.reprint_job(job))
        open_pdf = buttons.addButton("Open PDF", QDialogButtonBox.ButtonRole.ActionRole)
        open_pdf.setEnabled(job.best_pdf.exists())
        open_pdf.clicked.connect(lambda: window._open_path(job.best_pdf))
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

        self._image: QImage | None = None
        self._render()

    def _render(self) -> None:
        path = self.source.currentData()
        self._image = None
        if path is None:
            self.image_label.setText("No print data saved for this job.")
            return
        paper_mm, paper_dots = PAPER_SIZES[self._window.paper_combo.currentData()]
        elements = EscPosParser(paper_dots).parse(Path(path).read_bytes())
        self._image = PdfRenderer(paper_mm, paper_dots).render_image(elements)
        if self._image is None:
            self.image_label.setText("Nothing printable in this job.")
            return
        self._show_image()

    def _show_image(self) -> None:
        if self._image is None:
            return
        width = round(self._image.width() * self.zoom.currentData() / 100)
        pixmap = QPixmap.fromImage(self._image).scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(pixmap)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        self.emitter.message.emit(self.format(record))


class ServiceDialog(QDialog):
    """Administrative tools, shown after the service password has been entered."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.setWindowTitle("Service")
        self.setMinimumWidth(520)

        self.status = QLabel()
        self.status.setWordWrap(True)
        restart = QPushButton("Restart service")
        restart.clicked.connect(lambda: (window.restart_service(), self.refresh()))
        stop = QPushButton("Stop")
        stop.clicked.connect(lambda: (window.stop_server(), self.refresh()))
        start = QPushButton("Start")
        start.clicked.connect(lambda: (window.start_server(), self.refresh()))
        service_buttons = QHBoxLayout()
        service_buttons.addWidget(restart)
        service_buttons.addWidget(stop)
        service_buttons.addWidget(start)
        service_box = QGroupBox("Print service")
        service_layout = QVBoxLayout(service_box)
        service_layout.addWidget(self.status)
        service_layout.addLayout(service_buttons)

        storage = QFormLayout()
        self.usage_labels: dict[str, QLabel] = {}
        for name, root in window.job_roots:
            label = QLabel()
            empty = QPushButton("Empty...")
            empty.clicked.connect(lambda _=False, r=root, n=name: self._delete([(r, n)]))
            self.usage_labels[name] = label
            storage.addRow(name.capitalize() if name != "zreports" else "Z reports", self._row(label, empty))
        self.days = QSpinBox()
        self.days.setRange(1, 3650)
        self.days.setValue(30)
        self.days.setSuffix(" days")
        delete_old = QPushButton("Delete...")
        delete_old.clicked.connect(lambda: self._delete([(r, n) for n, r in window.job_roots], self.days.value()))
        older = QHBoxLayout()
        older.addWidget(QLabel("Jobs older than"))
        older.addWidget(self.days)
        older.addStretch(1)
        older.addWidget(delete_old)
        storage.addRow("Clean up", older)
        storage_box = QGroupBox("Storage (bin, pdf, json and final files; folders are kept)")
        storage_box.setLayout(storage)

        clear_log = QPushButton("Clear log")
        clear_log.clicked.connect(window.log_view.clear)
        diagnostics = QPushButton("Copy diagnostics")
        diagnostics.clicked.connect(window.copy_diagnostics)
        reset = QPushButton("Reset settings...")
        reset.clicked.connect(lambda: (window.reset_settings(), self.refresh()))
        password = QPushButton("Change password...")
        password.clicked.connect(window.change_service_password)
        maintenance = QHBoxLayout()
        for button in (clear_log, diagnostics, reset, password):
            maintenance.addWidget(button)
        maintenance_box = QGroupBox("Maintenance")
        maintenance_box.setLayout(maintenance)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(service_box)
        layout.addWidget(storage_box)
        layout.addWidget(maintenance_box)
        layout.addWidget(close)
        self.refresh()

    @staticmethod
    def _row(label: QLabel, button: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(label, 1)
        row.addWidget(button)
        return row

    def refresh(self) -> None:
        self.status.setText(self._window.status_label.text())
        for name, root in self._window.job_roots:
            count, size = folder_usage(root)
            self.usage_labels[name].setText(f"{count} files, {format_size(size)}  -  {root}")

    def _delete(self, roots: list[tuple[Path, str]], older_than_days: int | None = None) -> None:
        usage = [(root, name, *folder_usage(root, older_than_days)) for root, name in roots]
        count = sum(u[2] for u in usage)
        if not count:
            QMessageBox.information(self, "Service", "There are no matching files to delete.")
            return
        scope = f" older than {older_than_days} days" if older_than_days else ""
        details = "\n".join(f"  {name}: {c} files ({format_size(s)})" for _, name, c, s in usage if c)
        answer = QMessageBox.question(
            self, "Service", f"Delete {count} job files{scope}?\n\n{details}\n\nThis cannot be undone."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for root, name, _, _ in usage:
            deleted = delete_job_files(root, older_than_days)
            logging.info("Service: deleted %d files%s from %s", deleted, scope, name)
        self._window.refresh_jobs()
        self.refresh()


class MainWindow(QMainWindow):
    _printer_status_signal = Signal(object, bool)

    def __init__(self, log_emitter: LogEmitter) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1040, 640)
        self.settings = QSettings("SwiftVirtualPrinter", "VirtualPrinter")
        self.server: PrintServer | None = None
        self.last_job: Job | None = None
        self._tray_hint_shown = False
        self._tray_click = self.preview_last_job
        self._update_silent = False
        self._service_failures = 0
        self._service_locked_until = 0.0
        self._icon_on, self._icon_off = make_icon(True), make_icon(False)
        self.setWindowIcon(self._icon_on)
        self.update_checker = UpdateChecker(self)
        self.update_checker.finished.connect(self._on_update_checked)
        self._printer_status: PrinterStatus | None = None
        self._printer_checking = False
        self._printer_status_signal.connect(self._on_printer_status)
        self._printer_timer = QTimer(self)
        self._printer_timer.setInterval(PRINTER_CHECK_SECONDS * 1000)
        self._printer_timer.timeout.connect(self.check_printer)

        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._load_settings()
        log_emitter.message.connect(self.log_view.appendPlainText)
        logging.info("%s %s (%s)", APP_NAME, __version__, build_info()[0])
        self.start_server()
        self.refresh_jobs()
        if self.auto_update_action.isChecked():
            QTimer.singleShot(5000, lambda: self.check_for_updates(silent=True))

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_printer_tab(), "Printer")
        tabs.addTab(self._build_receipt_tab(), "Receipts")
        tabs.addTab(self._build_forward_tab(), "Forwarding")
        tabs.addTab(self._build_logo_tab(), "Logo")
        self.paper_combo.currentIndexChanged.connect(self._update_logo_preview)

        self.toggle_button = QPushButton()
        self.toggle_button.clicked.connect(self.toggle_server)
        apply_button = QPushButton("Apply && Restart")
        apply_button.setDefault(True)
        apply_button.clicked.connect(self.apply_settings)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.toggle_button)
        buttons.addWidget(apply_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(tabs, 1)
        left_layout.addLayout(buttons)

        self.jobs_list = QListWidget()
        self.jobs_list.itemDoubleClicked.connect(lambda item: self.preview_job(self._job_at(item)))
        self.jobs_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.jobs_list.customContextMenuRequested.connect(self._show_job_menu)
        open_folder = QPushButton("Receipts folder")
        open_folder.clicked.connect(self.open_output_folder)
        open_misc = QPushButton("Misc folder")
        open_misc.clicked.connect(self.open_misc_folder)
        open_zreports = QPushButton("Z reports")
        open_zreports.clicked.connect(self.open_zreports_folder)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_jobs)
        jobs_buttons = QHBoxLayout()
        jobs_buttons.addWidget(_hint("Double-click to preview, right-click to reprint or open files."), 1)
        jobs_buttons.addWidget(refresh)
        jobs_buttons.addWidget(open_folder)
        jobs_buttons.addWidget(open_misc)
        jobs_buttons.addWidget(open_zreports)
        jobs_box = QGroupBox("Jobs")
        jobs_layout = QVBoxLayout(jobs_box)
        jobs_layout.addWidget(self.jobs_list)
        jobs_layout.addLayout(jobs_buttons)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        log_box = QGroupBox("Log")
        QVBoxLayout(log_box).addWidget(self.log_view)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(jobs_box)
        right.addWidget(log_box)
        right.setSizes([360, 220])

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(left)
        main.addWidget(right)
        main.setStretchFactor(0, 0)
        main.setStretchFactor(1, 1)
        main.setSizes([480, 560])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(main)
        self.setCentralWidget(central)

        self.status_dot = QLabel("\u25cf")
        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_dot)
        self.statusBar().addWidget(self.status_label, 1)
        self.printer_dot = QLabel("\u25cf")
        self.printer_label = QLabel()
        self.statusBar().addPermanentWidget(self.printer_dot)
        self.statusBar().addPermanentWidget(self.printer_label)

    def _build_printer_tab(self) -> QWidget:
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.paper_combo = QComboBox()
        for key in PAPER_SIZES:
            self.paper_combo.addItem(f"{key} mm", key)
        self.output_edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(browse)
        self.idle_spin = QDoubleSpinBox()
        self.idle_spin.setRange(1, 600)
        self.idle_spin.setSuffix(" s")
        self.auto_cut_check = QCheckBox("Add a paper cut when the document has none")
        self.auto_cut_check.setToolTip("Appends an ESC/POS cut (GS V 66 0: feed to cutter, partial cut) to jobs without a cut.")
        self.clean_check = QCheckBox("Reset the printer before each job and finish an unprinted last line")
        self.clean_check.setToolTip(
            "Prepends ESC @ and adds a final line feed when needed. Fixes jobs from plain-text drivers "
            "(e.g. Windows 'Generic / Text Only' test pages) that don't cut and disturb the next receipt."
        )

        form = QFormLayout()
        form.addRow("Listen address", self.host_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Paper width", self.paper_combo)
        form.addRow("Receipts folder", output_row)
        form.addRow("Idle timeout", self.idle_spin)
        form.addRow("Paper cut", self.auto_cut_check)
        form.addRow("Clean up jobs", self.clean_check)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "POS software prints to this address and port. Receipts (see the Receipts tab) get the logo and are "
            "saved in the receipts folder; all other documents are printed without logo and saved in the misc "
            "folder next to it."
        ))
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    @staticmethod
    def _rule_widgets(placeholder: str) -> tuple[QLineEdit, QSpinBox, QComboBox, QCheckBox, QFormLayout]:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        line = QSpinBox()
        line.setRange(0, 50)
        line.setPrefix("text line ")
        line.setSpecialValueText("any line")
        mode = QComboBox()
        mode.addItem("is exactly the marker", False)
        mode.addItem("contains the marker", True)
        remove = QCheckBox("Remove the marker from the printout")
        remove.setToolTip("The whole line is removed when nothing else is printed on it.")
        form = QFormLayout()
        form.addRow("Marker text", edit)
        form.addRow("Look at", line)
        form.addRow("The line", mode)
        form.addRow("", remove)
        return edit, line, mode, remove, form

    def _build_receipt_tab(self) -> QWidget:
        self.marker_edit, self.marker_line, self.marker_mode, self.marker_remove, receipt_form = self._rule_widgets(
            "empty = every job is a receipt"
        )
        receipt_box = QGroupBox("Receipts: logo added, saved in the receipts folder")
        receipt_box.setLayout(receipt_form)

        self.zreport_box = QGroupBox("Detect Z reports: no logo, saved in the zreports folder")
        self.zreport_box.setCheckable(True)
        self.zreport_edit, self.zreport_line, self.zreport_mode, self.zreport_remove, zreport_form = self._rule_widgets(
            "e.g. Z REPORT"
        )
        self.zreport_box.setLayout(zreport_form)

        test = QPushButton("Test on selected job")
        test.clicked.connect(self.test_receipt_rule)
        test_row = QHBoxLayout()
        test_row.addStretch(1)
        test_row.addWidget(test)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "A job matches when the chosen printed text line (or any line) matches the marker. Blank lines and "
            "images are not counted, so text line 1 is the first line with text. Z reports are checked first; "
            "jobs that are neither go to the misc folder without logo."
        ))
        layout.addWidget(receipt_box)
        layout.addWidget(self.zreport_box)
        layout.addLayout(test_row)
        layout.addStretch(1)
        return page

    def _receipt_rule(self) -> ReceiptRule:
        return ReceiptRule(
            self.marker_edit.text(),
            self.marker_line.value(),
            bool(self.marker_mode.currentData()),
            self.marker_remove.isChecked(),
        )

    def _zreport_rule(self) -> ReceiptRule | None:
        if not self.zreport_box.isChecked() or not self.zreport_edit.text().strip():
            return None
        return ReceiptRule(
            self.zreport_edit.text(),
            self.zreport_line.value(),
            bool(self.zreport_mode.currentData()),
            self.zreport_remove.isChecked(),
        )

    def test_receipt_rule(self) -> None:
        item = self.jobs_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Test rules", "Select a job in the jobs list first.")
            return
        job = self._job_at(item)
        if not job.bin.exists():
            QMessageBox.warning(self, "Test rules", f"No received data saved for {job.stem}.")
            return
        receipt_rule, zreport_rule = self._receipt_rule(), self._zreport_rule()
        lines = elements_to_lines(EscPosParser(PAPER_SIZES[self.paper_combo.currentData()][1]).parse(job.bin.read_bytes()))
        text_lines = [t for t in (line.strip() for line in lines) if t and not t.startswith("[IMAGE")]

        def checked(rule: ReceiptRule | None, i: int, text: str) -> bool:
            if rule is None or not rule.pattern:
                return False
            return rule.line_matches(text) if rule.line == 0 else i == rule.line

        def mark(i: int, text: str) -> str:
            return ("Z" if checked(zreport_rule, i, text) else " ") + (">" if checked(receipt_rule, i, text) else " ")

        any_line = receipt_rule.line == 0 or (zreport_rule is not None and zreport_rule.line == 0)
        limit = len(text_lines) if any_line else 8
        shown = "\n".join(f"{mark(i, t)} {i:2}: {t}" for i, t in enumerate(text_lines[:limit], 1))
        if zreport_rule and zreport_rule.matches(lines):
            verdict = "Z REPORT (no logo, zreports folder)"
        elif receipt_rule.matches(lines):
            verdict = "RECEIPT (logo, receipts folder)"
        else:
            verdict = "Other document (no logo, misc folder)"
        rules = f"Receipt rule (>): {receipt_rule}\nZ report rule (Z): {zreport_rule or 'off'}"
        QMessageBox.information(
            self, "Test rules", f"{job.stem}\n{rules}\n\nResult: {verdict}\n\nText lines:\n{shown}"
        )

    def _build_forward_tab(self) -> QWidget:
        self.forward_mode = QComboBox()
        self.forward_mode.addItem("Off", OFF)
        self.forward_mode.addItem("Network printer (raw TCP)", NETWORK)
        if sys.platform == "win32":
            self.forward_mode.addItem("Installed Windows printer", WINDOWS)
        self.forward_host = QLineEdit()
        self.forward_host.setPlaceholderText("e.g. 192.168.1.50")
        self.forward_port = QSpinBox()
        self.forward_port.setRange(1, 65535)
        self.forward_printer = QComboBox()
        self.forward_printer.setEditable(True)
        self.forward_printer.addItems(list_windows_printers())

        self.forward_form = QFormLayout()
        self.forward_form.addRow("Send jobs to", self.forward_mode)
        self.forward_form.addRow("Printer address", self.forward_host)
        self.forward_form.addRow("Printer port", self.forward_port)
        self.forward_form.addRow("Windows printer", self.forward_printer)
        self.forward_mode.currentIndexChanged.connect(self._update_forward_fields)

        self.printer_status_label = QLabel("Not checked yet")
        self.printer_status_label.setWordWrap(True)
        check_now = QPushButton("Check now")
        check_now.clicked.connect(lambda: self.check_printer(manual=True))
        status_row = QHBoxLayout()
        status_row.addWidget(self.printer_status_label, 1)
        status_row.addWidget(check_now)
        self.monitor_check = QCheckBox(f"Check every {PRINTER_CHECK_SECONDS} s and notify when it goes offline or runs out of paper")
        self.forward_form.addRow("Printer status", status_row)
        self.forward_form.addRow("", self.monitor_check)
        self.monitor_check.toggled.connect(self._update_printer_timer)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "Also send every job, byte for byte (with the logo if enabled), to a real receipt printer. "
            "Status uses ESC/POS for network printers and the Epson Status API or Windows print queue for "
            "installed (USB) printers."
        ))
        layout.addLayout(self.forward_form)
        layout.addStretch(1)
        return page

    def _update_forward_fields(self) -> None:
        mode = self.forward_mode.currentData()
        for widget, visible in (
            (self.forward_host, mode == NETWORK),
            (self.forward_port, mode == NETWORK),
            (self.forward_printer, mode == WINDOWS),
        ):
            self.forward_form.setRowVisible(widget, visible)

    def _forward_target(self) -> ForwardTarget:
        return ForwardTarget(
            self.forward_mode.currentData(),
            host=self.forward_host.text().strip(),
            port=self.forward_port.value(),
            printer=self.forward_printer.currentText().strip(),
        )

    def _build_logo_tab(self) -> QWidget:
        self.logo_check = QCheckBox("Print logo at the top of every receipt")
        self.logo_path = QLineEdit()
        self.logo_path.setPlaceholderText("PNG or JPG")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_logo)
        path_row = QHBoxLayout()
        path_row.addWidget(self.logo_path)
        path_row.addWidget(browse)
        self.logo_width = QSpinBox()
        self.logo_width.setRange(10, 100)
        self.logo_width.setSuffix(" % of paper width")
        self.logo_dither = QCheckBox("Dither (for photos / gradients)")
        self.logo_preview = QLabel()
        self.logo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_preview.setMinimumSize(LOGO_PREVIEW_WIDTH + 12, 180)
        self.logo_preview.setStyleSheet("background: #e0e0e0; border: 1px solid #bbb; color: #666;")
        self.logo_info = _hint("")

        form = QFormLayout()
        form.addRow(self.logo_check)
        form.addRow("Image", path_row)
        form.addRow("Width", self.logo_width)
        form.addRow("", self.logo_dither)
        self.logo_check.toggled.connect(self._update_logo_preview)
        self.logo_path.editingFinished.connect(self._update_logo_preview)
        self.logo_width.valueChanged.connect(self._update_logo_preview)
        self.logo_dither.toggled.connect(self._update_logo_preview)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addWidget(QLabel("Preview (as printed, on the paper width):"))
        layout.addWidget(self.logo_preview, 1)
        layout.addWidget(self.logo_info)
        return page

    def _browse_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose logo", self.logo_path.text(), "Images (*.png *.jpg *.jpeg)")
        if path:
            self.logo_path.setText(path)
            self.logo_check.setChecked(True)
            self._update_logo_preview()

    def _logo_settings(self) -> LogoSettings:
        path = self.logo_path.text().strip() if self.logo_check.isChecked() else ""
        return LogoSettings(path, self.logo_width.value(), self.logo_dither.isChecked())

    def _update_logo_preview(self) -> None:
        settings = LogoSettings(self.logo_path.text().strip(), self.logo_width.value(), self.logo_dither.isChecked())
        self.logo_preview.setPixmap(QPixmap())
        self.logo_info.setText("")
        if not settings.enabled:
            self.logo_preview.setText("No logo selected")
            return
        paper = self.paper_combo.currentData()
        paper_dots = PAPER_SIZES[paper][1]
        try:
            bitmap = load_logo(settings, paper_dots)
        except ValueError as exc:
            self.logo_preview.setText(str(exc))
            return

        # Draw the logo on a strip of "paper" so its size relative to the receipt is visible.
        margin = 16
        paper_image = QPixmap(paper_dots, bitmap.height + 2 * margin)
        paper_image.fill(Qt.GlobalColor.white)
        painter = QPainter(paper_image)
        painter.drawImage((paper_dots - bitmap.width) // 2, margin, bitmap_to_qimage(bitmap))
        painter.end()
        self.logo_preview.setPixmap(
            paper_image.scaled(
                LOGO_PREVIEW_WIDTH,
                max(self.logo_preview.height() - 12, 120),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        state = "" if self.logo_check.isChecked() else " - not enabled"
        self.logo_info.setText(f"{bitmap.width} \u00d7 {bitmap.height} dots on {paper} mm paper{state}")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Open receipts folder", self.open_output_folder)
        file_menu.addAction("Open misc folder", self.open_misc_folder)
        file_menu.addAction("Open Z reports folder", self.open_zreports_folder)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.quit)
        service_menu = self.menuBar().addMenu("&Service")
        service_menu.addAction("Service tools...", self.open_service)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Check for updates...", self.check_for_updates)
        self.auto_update_action = help_menu.addAction("Check for updates on startup")
        self.auto_update_action.setCheckable(True)
        self.auto_update_action.setChecked(self.settings.value("auto_update_check", True, type=bool))
        self.auto_update_action.toggled.connect(lambda on: self.settings.setValue("auto_update_check", on))
        help_menu.addSeparator()
        help_menu.addAction("About", self.show_about)

    # -- service ------------------------------------------------------------

    def open_service(self) -> None:
        remaining = self._service_locked_until - time.monotonic()
        if remaining > 0:
            QMessageBox.warning(self, "Service", f"Too many wrong passwords. Try again in {int(remaining) + 1} seconds.")
            return
        self.show_window()
        password, ok = QInputDialog.getText(self, "Service", "Service password:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        stored = str(self.settings.value("service_password_hash", DEFAULT_PASSWORD_HASH))
        if not verify_password(password, stored):
            self._service_failures += 1
            logging.warning("Service: wrong password")
            if self._service_failures >= 3:
                self._service_failures = 0
                self._service_locked_until = time.monotonic() + 60
            QMessageBox.warning(self, "Service", "Wrong password.")
            return
        self._service_failures = 0
        logging.info("Service: menu opened")
        ServiceDialog(self).exec()

    def restart_service(self) -> None:
        logging.info("Service: restarting print service")
        self.start_server()

    def reset_settings(self) -> None:
        answer = QMessageBox.question(
            self, "Reset settings",
            "Reset all settings (port, paper, folders, forwarding, logo, updates) to their defaults?\n\n"
            "Saved jobs and the service password are kept.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        password_hash = self.settings.value("service_password_hash")
        self.settings.clear()
        if password_hash:
            self.settings.setValue("service_password_hash", password_hash)
        self._load_settings()
        self.auto_update_action.setChecked(True)
        logging.info("Service: settings reset to defaults")
        self.start_server()
        self.refresh_jobs()

    def change_service_password(self) -> None:
        new, ok = QInputDialog.getText(self, "Change password", "New service password:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if len(new) < 6:
            QMessageBox.warning(self, "Change password", "The password must be at least 6 characters.")
            return
        again, ok = QInputDialog.getText(self, "Change password", "Repeat the new password:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if new != again:
            QMessageBox.warning(self, "Change password", "The passwords do not match.")
            return
        self.settings.setValue("service_password_hash", hash_password(new))
        logging.info("Service: password changed")
        QMessageBox.information(self, "Change password", "The service password has been changed.")

    def copy_diagnostics(self) -> None:
        import PySide6

        build_date, commit = build_info()
        lines = [
            f"{APP_NAME} {__version__} (build {build_date}{' ' + commit if commit else ''})",
            f"Python {platform.python_version()}, Qt {qVersion()}, PySide6 {PySide6.__version__}, {platform.platform()}",
            f"Installed exe: {getattr(sys, 'frozen', False)}, app folder: {app_root()}",
            f"Status: {self.status_label.text()}",
            f"Paper: {self.paper_combo.currentData()} mm, idle timeout: {self.idle_spin.value()} s, "
            f"auto cut: {self.auto_cut_check.isChecked()}, clean up jobs: {self.clean_check.isChecked()}",
            f"Receipt rule: {self._receipt_rule()} (remove marker: {self.marker_remove.isChecked()})",
            f"Z report rule: {self._zreport_rule() or 'off'}",
            f"Forwarding: {self.active_forward_target()}",
            f"Logo: {self._logo_settings()}",
            f"Settings: {self.settings.fileName()}",
        ]
        for name, root in self.job_roots:
            count, size = folder_usage(root)
            lines.append(f"{name.capitalize()}: {root} ({count} files, {format_size(size)})")
        QApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(self, "Diagnostics", "Diagnostics copied to the clipboard.\n\n" + "\n".join(lines))

    # -- updates ------------------------------------------------------------

    def check_for_updates(self, silent: bool = False) -> None:
        self._update_silent = silent
        logging.info("Checking for updates...")
        self.update_checker.check()

    def _on_update_checked(self, release: Release | None, error: str) -> None:
        silent = self._update_silent
        if error or release is None:
            logging.warning("Update check failed: %s", error)
            if not silent:
                QMessageBox.warning(self, "Check for updates", f"Could not check for updates.\n\n{error}")
            return
        if not is_newer(release.version):
            logging.info("Up to date (latest release is %s)", release.version)
            if not silent:
                QMessageBox.information(self, "Check for updates", f"You have the latest version ({__version__}).")
            return
        logging.info("Update available: %s", release.version)
        if silent:
            self._tray_click = lambda: self._prompt_update(release)
            self.tray.showMessage(
                "Update available",
                f"Version {release.version} is available. Click to update.",
                QSystemTrayIcon.MessageIcon.Information,
                10000,
            )
        else:
            self._prompt_update(release)

    def _prompt_update(self, release: Release) -> None:
        self.show_window()
        notes = release.notes.strip()
        if len(notes) > 1500:
            notes = notes[:1500] + "\n..."
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIconPixmap(self._icon_on.pixmap(64, 64))
        box.setText(f"Version {release.version} is available (you have {__version__}).")
        if notes:
            box.setDetailedText(notes)
        if can_self_update():
            box.setInformativeText("Download and install it now? The app will close and restart after the update.")
            install = box.addButton("Download and install", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText("Automatic updates only work in the installed app. Open the release page instead?")
            install = None
        page = box.addButton("Open release page", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if install is not None and box.clickedButton() is install:
            self._download_update(release)
        elif box.clickedButton() is page:
            QDesktopServices.openUrl(QUrl(release.page_url))

    def _download_update(self, release: Release) -> None:
        dialog = QProgressDialog(f"Downloading {release.asset_name}...", "Cancel", 0, 100, self)
        dialog.setWindowTitle("Updating")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        downloader = UpdateDownloader(self)

        def on_progress(done: int, total: int) -> None:
            if total > 0:
                dialog.setValue(int(done * 100 / total))

        def on_finished(path: str, error: str) -> None:
            dialog.close()
            downloader.deleteLater()
            if error:
                if error != "Operation canceled":
                    logging.error("Update download failed: %s", error)
                    QMessageBox.warning(self, "Update failed", f"Could not download the update.\n\n{error}")
                return
            logging.info("Starting installer %s", path)
            try:
                run_installer(path)
            except OSError as exc:
                QMessageBox.warning(self, "Update failed", f"Could not start the installer.\n\n{exc}")
                return
            self.quit()

        downloader.progress.connect(on_progress)
        downloader.finished.connect(on_finished)
        dialog.canceled.connect(downloader.cancel)
        dialog.show()
        downloader.start(release)

    def show_about(self) -> None:
        build_date, commit = build_info()
        build = f"{build_date} ({commit})" if commit else build_date
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(self._icon_on.pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<h3>{APP_NAME}</h3>"
            f"<p>Version {__version__}<br>Build: {build}</p>"
            "<p>Virtual ESC/POS receipt printer that listens on TCP port 9100 "
            "and saves every print job as a PDF.</p>"
            f"<p><b>{CREDITS}</b></p>"
        )
        box.exec()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._icon_off, self)
        menu = QMenu(self)
        menu.addAction("Show window", self.show_window)
        menu.addAction("Preview last job", self.preview_last_job)
        menu.addAction("Open receipts folder", self.open_output_folder)
        menu.addAction("Open misc folder", self.open_misc_folder)
        menu.addAction("Open Z reports folder", self.open_zreports_folder)
        menu.addSeparator()
        self.tray_toggle: QAction = menu.addAction("Stop printer", self.toggle_server)
        menu.addSeparator()
        menu.addAction("Check for updates...", self.check_for_updates)
        menu.addAction("Service...", self.open_service)
        menu.addAction("About", self.show_about)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(lambda: self._tray_click())
        self.tray.show()

    # -- settings -----------------------------------------------------------

    def _load_settings(self) -> None:
        s = self.settings
        self.host_edit.setText(str(s.value("host", "0.0.0.0")))
        self.port_spin.setValue(int(s.value("port", 9100)))
        self.paper_combo.setCurrentIndex(max(0, self.paper_combo.findData(str(s.value("paper", "80")))))
        self.output_edit.setText(str(s.value("receipts_dir", str(DEFAULT_OUTPUT))))
        self.idle_spin.setValue(float(s.value("idle_timeout", 10.0)))
        self.auto_cut_check.setChecked(s.value("auto_cut", False, type=bool))
        self.clean_check.setChecked(s.value("clean_jobs", True, type=bool))
        self.forward_mode.setCurrentIndex(max(0, self.forward_mode.findData(str(s.value("forward_mode", OFF)))))
        self.forward_host.setText(str(s.value("forward_host", "")))
        self.forward_port.setValue(int(s.value("forward_port", 9100)))
        self.forward_printer.setCurrentText(str(s.value("forward_printer", "")))
        self._update_forward_fields()
        self.logo_check.setChecked(s.value("logo_enabled", False, type=bool))
        self.logo_path.setText(str(s.value("logo_path", "")))
        self.logo_width.setValue(int(s.value("logo_width", 60)))
        self.logo_dither.setChecked(s.value("logo_dither", False, type=bool))
        self.monitor_check.setChecked(s.value("monitor_printer", True, type=bool))
        self.marker_edit.setText(str(s.value("marker_pattern", DEFAULT_MARKER)))
        self.marker_line.setValue(int(s.value("marker_line", 1)))
        self.marker_mode.setCurrentIndex(1 if s.value("marker_contains", False, type=bool) else 0)
        self.marker_remove.setChecked(s.value("marker_remove", True, type=bool))
        self.zreport_box.setChecked(s.value("zreport_enabled", True, type=bool))
        self.zreport_edit.setText(str(s.value("zreport_pattern", DEFAULT_ZREPORT_MARKER)))
        self.zreport_line.setValue(int(s.value("zreport_line", 0)))
        self.zreport_mode.setCurrentIndex(1 if s.value("zreport_contains", True, type=bool) else 0)
        self.zreport_remove.setChecked(s.value("zreport_remove", False, type=bool))
        self._update_logo_preview()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("host", self.host_edit.text().strip())
        s.setValue("port", self.port_spin.value())
        s.setValue("paper", self.paper_combo.currentData())
        s.setValue("receipts_dir", self.output_edit.text().strip())
        s.setValue("idle_timeout", self.idle_spin.value())
        s.setValue("auto_cut", self.auto_cut_check.isChecked())
        s.setValue("clean_jobs", self.clean_check.isChecked())
        s.setValue("forward_mode", self.forward_mode.currentData())
        s.setValue("forward_host", self.forward_host.text().strip())
        s.setValue("forward_port", self.forward_port.value())
        s.setValue("forward_printer", self.forward_printer.currentText().strip())
        s.setValue("logo_enabled", self.logo_check.isChecked())
        s.setValue("logo_path", self.logo_path.text().strip())
        s.setValue("logo_width", self.logo_width.value())
        s.setValue("logo_dither", self.logo_dither.isChecked())
        s.setValue("monitor_printer", self.monitor_check.isChecked())
        s.setValue("marker_pattern", self.marker_edit.text())
        s.setValue("marker_line", self.marker_line.value())
        s.setValue("marker_contains", bool(self.marker_mode.currentData()))
        s.setValue("marker_remove", self.marker_remove.isChecked())
        s.setValue("zreport_enabled", self.zreport_box.isChecked())
        s.setValue("zreport_pattern", self.zreport_edit.text())
        s.setValue("zreport_line", self.zreport_line.value())
        s.setValue("zreport_contains", bool(self.zreport_mode.currentData()))
        s.setValue("zreport_remove", self.zreport_remove.isChecked())

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Receipts folder", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def apply_settings(self) -> None:
        self._save_settings()
        self.start_server()
        self.refresh_jobs()

    @property
    def output_dir(self) -> Path:
        return Path(self.output_edit.text().strip() or DEFAULT_OUTPUT)

    # -- server control -----------------------------------------------------

    def start_server(self) -> None:
        self.stop_server()
        paper_mm, paper_dots = PAPER_SIZES[self.paper_combo.currentData()]
        server = PrintServer(
            self.host_edit.text().strip() or "0.0.0.0",
            self.port_spin.value(),
            self.output_dir,
            paper_mm,
            paper_dots,
            self.idle_spin.value(),
            self._forward_target(),
            self._logo_settings(),
            self.auto_cut_check.isChecked(),
            self.clean_check.isChecked(),
            self._receipt_rule(),
            self._zreport_rule(),
            parent=self,
        )
        try:
            server.start()
        except (RuntimeError, OSError) as exc:
            server.deleteLater()
            logging.error("%s", exc)
            self._set_running(False, str(exc))
            self.tray.showMessage(APP_NAME, str(exc), QSystemTrayIcon.MessageIcon.Warning)
            return
        server.job_saved.connect(self._on_job_saved)
        self.server = server
        self._set_running(True)
        # Target may have changed: forget the old status and check the (new) real printer.
        self._printer_status = None
        self._update_printer_timer()
        QTimer.singleShot(1000, self.check_printer)

    def stop_server(self) -> None:
        if self.server:
            self.server.stop()
            self.server.deleteLater()
            self.server = None
        self._set_running(False)

    def toggle_server(self) -> None:
        if self.server:
            self.stop_server()
        else:
            self.start_server()

    def _set_running(self, running: bool, error: str = "") -> None:
        if running and self.server:
            text = f"Listening on {self.server.host}:{self.server.port}"
            if self.server.forward_target.enabled:
                text += f", forwarding to {self.server.forward_target}"
        else:
            text = f"Stopped: {error}" if error else "Stopped"
        self.status_label.setText(text)
        self.status_dot.setStyleSheet(f"font-size: 14pt; color: {'#2e7d32' if running else '#c62828' if error else '#9e9e9e'};")
        self.toggle_button.setText("Stop" if running else "Start")
        self.tray_toggle.setText("Stop printer" if running else "Start printer")
        self.tray.setIcon(self._icon_on if running else self._icon_off)
        self.tray.setToolTip(f"{APP_NAME} {__version__}\n{text}")

    # -- jobs ---------------------------------------------------------------

    def _job_at(self, item: QListWidgetItem) -> Job:
        return item.data(Qt.ItemDataRole.UserRole)

    def _add_job_item(self, job: Job, top: bool = False) -> None:
        tag = {self.misc_dir: "    (misc)", self.zreports_dir: "    (Z report)"}.get(job.root, "")
        item = QListWidgetItem(job.stem + tag)
        item.setData(Qt.ItemDataRole.UserRole, job)
        item.setToolTip(str(job.best_pdf))
        if job.root == self.misc_dir:
            item.setForeground(QColor("#777777"))
        elif job.root == self.zreports_dir:
            item.setForeground(QColor("#1565c0"))
        if top:
            self.jobs_list.insertItem(0, item)
        else:
            self.jobs_list.addItem(item)

    def refresh_jobs(self) -> None:
        self.jobs_list.clear()
        found: dict[Job, float] = {}
        for _, root in self.job_roots:
            for folder in ("pdf", "final"):
                directory = root / folder
                if directory.is_dir():
                    for path in directory.glob("*.pdf"):
                        job = Job(root, path.stem)
                        found[job] = max(found.get(job, 0.0), path.stat().st_mtime)
        jobs = sorted(found, key=found.get, reverse=True)
        for job in jobs[:500]:
            self._add_job_item(job)
        if jobs and self.last_job is None:
            self.last_job = jobs[0]

    def _on_job_saved(self, path: Path) -> None:
        job = Job(path.parent.parent, path.stem)
        self.last_job = job
        self._tray_click = self.preview_last_job
        self._add_job_item(job, top=True)
        kind = {self.misc_dir: "Document (misc)", self.zreports_dir: "Z report"}.get(job.root, "Receipt")
        self.tray.showMessage(f"{kind} received", f"{path.stem}\nClick to preview.", QSystemTrayIcon.MessageIcon.Information, 4000)

    def _open_path(self, path: str | Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_job_menu(self, pos) -> None:
        item = self.jobs_list.itemAt(pos)
        if not item:
            return
        job = self._job_at(item)
        menu = QMenu(self)
        menu.addAction("Preview", lambda: self.preview_job(job))
        menu.addAction("Reprint", lambda: self.reprint_job(job)).setEnabled(job.print_bytes_path is not None)
        menu.addSeparator()
        menu.addAction("Open final PDF", lambda: self._open_path(job.final_pdf)).setEnabled(job.final_pdf.exists())
        menu.addAction("Open PDF as received", lambda: self._open_path(job.pdf)).setEnabled(job.pdf.exists())
        menu.addAction("Open JSON", lambda: self._open_path(job.json)).setEnabled(job.json.exists())
        menu.addAction("Copy JSON path", lambda: QApplication.clipboard().setText(str(job.json))).setEnabled(job.json.exists())
        menu.addSeparator()
        menu.addAction("Show in folder", lambda: self._open_path(job.best_pdf.parent))
        menu.exec(self.jobs_list.viewport().mapToGlobal(pos))

    def preview_job(self, job: Job) -> None:
        PreviewDialog(self, job).exec()

    def preview_last_job(self) -> None:
        if self.last_job:
            self.show_window()
            self.preview_job(self.last_job)

    def active_forward_target(self) -> ForwardTarget:
        return self.server.forward_target if self.server else self._forward_target()

    def reprint_job(self, job: Job) -> None:
        target = self.active_forward_target()
        if not target.enabled:
            QMessageBox.information(
                self, "Reprint", "No real printer is set up.\n\nChoose one on the Forwarding tab and click Apply && Restart."
            )
            return
        path = job.print_bytes_path
        if path is None:
            QMessageBox.warning(self, "Reprint", f"No print data saved for {job.stem}.")
            return
        status = self._printer_status
        if status is not None and status.online is False:
            answer = QMessageBox.question(self, "Reprint", f"The printer looks offline ({status}).\n\nSend the reprint anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        forward_async(target, path.read_bytes(), f"{job.stem} (reprint)")
        self.statusBar().showMessage(f"Reprint of {job.stem} sent to {target}", 6000)

    # -- real printer status -------------------------------------------------

    def _update_printer_timer(self) -> None:
        if self.monitor_check.isChecked() and self.active_forward_target().enabled:
            self._printer_timer.start()
        else:
            self._printer_timer.stop()
        if not self.active_forward_target().enabled:
            self._show_printer_status(None)

    def check_printer(self, manual: bool = False) -> None:
        target = self.active_forward_target()
        if not target.enabled:
            self._show_printer_status(None)
            if manual:
                QMessageBox.information(self, "Printer status", "No real printer is set up on the Forwarding tab.")
            return
        if self._printer_checking:
            return
        self._printer_checking = True
        if manual:
            self.printer_status_label.setText("Checking...")

        def run() -> None:
            try:
                status = check_status(target)
            except Exception as exc:  # status checks must never take the app down
                status = PrinterStatus(None, (f"check failed: {exc}",))
            self._printer_status_signal.emit(status, manual)

        threading.Thread(target=run, name="printer-status", daemon=True).start()

    def _on_printer_status(self, status: PrinterStatus, manual: bool) -> None:
        self._printer_checking = False
        previous, self._printer_status = self._printer_status, status
        self._show_printer_status(status)
        changed = previous is None or (previous.online, previous.problems) != (status.online, status.problems)
        if changed or manual:
            log = logging.info if status.ok else logging.warning
            log("Real printer: %s (%s)", status, status.source or "no source")
        if changed and previous is not None and not manual:
            icon = QSystemTrayIcon.MessageIcon.Information if status.ok else QSystemTrayIcon.MessageIcon.Warning
            title = "Printer is back online" if status.ok else "Printer problem"
            self.tray.showMessage(title, f"{self.active_forward_target()}\n{status}", icon, 8000)

    def _show_printer_status(self, status: PrinterStatus | None) -> None:
        if status is None:
            text, color, detail = "Real printer: not set up", "#9e9e9e", "No real printer is set up."
        else:
            text = f"Real printer: {status}"
            color = "#2e7d32" if status.ok else "#9e9e9e" if status.online is None else "#ef6c00" if status.online else "#c62828"
            detail = f"{status}  (via {status.source})" if status.source else str(status)
        self.printer_label.setText(text)
        self.printer_dot.setStyleSheet(f"font-size: 14pt; color: {color};")
        self.printer_status_label.setText(detail)

    def open_output_folder(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.output_dir)

    def open_misc_folder(self) -> None:
        self.misc_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.misc_dir)

    def open_zreports_folder(self) -> None:
        self.zreports_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.zreports_dir)

    @property
    def zreports_dir(self) -> Path:
        return zreports_dir_for(self.output_dir)

    @property
    def job_roots(self) -> list[tuple[str, Path]]:
        return [("receipts", self.output_dir), ("zreports", self.zreports_dir), ("misc", self.misc_dir)]

    @property
    def misc_dir(self) -> Path:
        return misc_dir_for(self.output_dir)

    # -- window / tray behaviour --------------------------------------------

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Still running in the system tray.", QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            self.quit()

    def quit(self) -> None:
        self.stop_server()
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} (system tray)")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(COMPANY)
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    emitter = LogEmitter()
    handler = QtLogHandler(emitter)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("pyBarcode").setLevel(logging.WARNING)

    window = MainWindow(emitter)
    if not args.minimized or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
