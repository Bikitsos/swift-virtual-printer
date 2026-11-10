"""System tray application for the virtual ESC/POS printer."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, COMPANY, CREDITS, __version__, build_info
from .forward import NETWORK, OFF, WINDOWS, ForwardTarget, list_windows_printers
from .main import PAPER_SIZES
from .server import PrintServer

DEFAULT_OUTPUT = Path.home() / "Documents" / "Virtual Printer"


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


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        self.emitter.message.emit(self.format(record))


class MainWindow(QMainWindow):
    def __init__(self, log_emitter: LogEmitter) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(720, 560)
        self.settings = QSettings("SwiftVirtualPrinter", "VirtualPrinter")
        self.server: PrintServer | None = None
        self.last_job: Path | None = None
        self._tray_hint_shown = False
        self._icon_on, self._icon_off = make_icon(True), make_icon(False)
        self.setWindowIcon(self._icon_on)

        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._load_settings()
        log_emitter.message.connect(self.log_view.appendPlainText)
        logging.info("%s %s (%s)", APP_NAME, __version__, build_info()[0])
        self.start_server()
        self.refresh_jobs()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
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
        self.raw_check = QCheckBox("Also save raw ESC/POS data (.bin)")

        form = QFormLayout()
        form.addRow("Listen address", self.host_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Paper width", self.paper_combo)
        form.addRow("Output folder", output_row)
        form.addRow("Idle timeout", self.idle_spin)
        form.addRow("", self.raw_check)

        self.status_label = QLabel()
        self.toggle_button = QPushButton()
        self.toggle_button.clicked.connect(self.toggle_server)
        apply_button = QPushButton("Apply && Restart")
        apply_button.clicked.connect(self.apply_settings)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(apply_button)
        status_row.addWidget(self.toggle_button)

        settings_box = QGroupBox("Printer")
        settings_layout = QVBoxLayout(settings_box)
        settings_layout.addLayout(form)
        settings_layout.addWidget(self._build_forward_box())
        settings_layout.addLayout(status_row)

        self.jobs_list = QListWidget()
        self.jobs_list.itemDoubleClicked.connect(lambda item: self._open_path(item.data(Qt.ItemDataRole.UserRole)))
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self.open_output_folder)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_jobs)
        jobs_buttons = QHBoxLayout()
        jobs_buttons.addStretch(1)
        jobs_buttons.addWidget(refresh)
        jobs_buttons.addWidget(open_folder)
        jobs_box = QGroupBox("Jobs (double-click to open)")
        jobs_layout = QVBoxLayout(jobs_box)
        jobs_layout.addWidget(self.jobs_list)
        jobs_layout.addLayout(jobs_buttons)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        log_box = QGroupBox("Log")
        QVBoxLayout(log_box).addWidget(self.log_view)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(jobs_box)
        splitter.addWidget(log_box)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(settings_box)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_forward_box(self) -> QGroupBox:
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

        box = QGroupBox("Forward to real printer")
        box.setLayout(self.forward_form)
        return box

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

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Open jobs folder", self.open_output_folder)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.quit)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("About", self.show_about)

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
        menu.addAction("Open last job", self.open_last_job)
        menu.addAction("Open jobs folder", self.open_output_folder)
        menu.addSeparator()
        self.tray_toggle: QAction = menu.addAction("Stop printer", self.toggle_server)
        menu.addSeparator()
        menu.addAction("About", self.show_about)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self.open_last_job)
        self.tray.show()

    # -- settings -----------------------------------------------------------

    def _load_settings(self) -> None:
        s = self.settings
        self.host_edit.setText(str(s.value("host", "0.0.0.0")))
        self.port_spin.setValue(int(s.value("port", 9100)))
        self.paper_combo.setCurrentIndex(max(0, self.paper_combo.findData(str(s.value("paper", "80")))))
        self.output_edit.setText(str(s.value("output", str(DEFAULT_OUTPUT))))
        self.idle_spin.setValue(float(s.value("idle_timeout", 10.0)))
        self.raw_check.setChecked(s.value("save_raw", False, type=bool))
        self.forward_mode.setCurrentIndex(max(0, self.forward_mode.findData(str(s.value("forward_mode", OFF)))))
        self.forward_host.setText(str(s.value("forward_host", "")))
        self.forward_port.setValue(int(s.value("forward_port", 9100)))
        self.forward_printer.setCurrentText(str(s.value("forward_printer", "")))
        self._update_forward_fields()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("host", self.host_edit.text().strip())
        s.setValue("port", self.port_spin.value())
        s.setValue("paper", self.paper_combo.currentData())
        s.setValue("output", self.output_edit.text().strip())
        s.setValue("idle_timeout", self.idle_spin.value())
        s.setValue("save_raw", self.raw_check.isChecked())
        s.setValue("forward_mode", self.forward_mode.currentData())
        s.setValue("forward_host", self.forward_host.text().strip())
        s.setValue("forward_port", self.forward_port.value())
        s.setValue("forward_printer", self.forward_printer.currentText().strip())

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.output_edit.text())
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
            self.raw_check.isChecked(),
            self._forward_target(),
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
        self.toggle_button.setText("Stop" if running else "Start")
        self.tray_toggle.setText("Stop printer" if running else "Start printer")
        self.tray.setIcon(self._icon_on if running else self._icon_off)
        self.tray.setToolTip(f"{APP_NAME} {__version__}\n{text}")

    # -- jobs ---------------------------------------------------------------

    def _add_job_item(self, path: Path, top: bool = False) -> None:
        item = QListWidgetItem(path.name)
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setToolTip(str(path))
        if top:
            self.jobs_list.insertItem(0, item)
        else:
            self.jobs_list.addItem(item)

    def refresh_jobs(self) -> None:
        self.jobs_list.clear()
        if not self.output_dir.is_dir():
            return
        pdfs = sorted(self.output_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in pdfs[:500]:
            self._add_job_item(path)
        if pdfs and self.last_job is None:
            self.last_job = pdfs[0]

    def _on_job_saved(self, path: Path) -> None:
        self.last_job = path
        self._add_job_item(path, top=True)
        self.tray.showMessage("Print job received", f"{path.name}\nClick to open.", QSystemTrayIcon.MessageIcon.Information, 4000)

    def _open_path(self, path: str | Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_last_job(self) -> None:
        if self.last_job and self.last_job.exists():
            self._open_path(self.last_job)

    def open_output_folder(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.output_dir)

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
