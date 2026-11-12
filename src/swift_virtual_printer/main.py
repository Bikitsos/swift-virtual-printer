"""Command-line entry point for the virtual ESC/POS printer."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import APP_NAME, __version__, default_receipts_dir

PAPER_SIZES = {"80": (80.0, 576), "58": (58.0, 384)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Virtual ESC/POS printer that saves jobs as PDF.")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9100, help="TCP port (default: 9100)")
    parser.add_argument("--output", type=Path, help="receipts folder; gets bin/, pdf/ and json/ subfolders (default: <app>/receipts)")
    parser.add_argument("--paper", choices=PAPER_SIZES, default="80", help="paper width in mm (default: 80)")
    parser.add_argument("--idle-timeout", type=float, default=10.0, help="seconds of inactivity that end a job")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    forward = parser.add_mutually_exclusive_group()
    forward.add_argument("--forward-host", metavar="HOST", help="also send each job to a real network printer (raw TCP)")
    forward.add_argument("--forward-printer", metavar="NAME", help="also send each job to an installed Windows printer (RAW)")
    parser.add_argument("--forward-port", type=int, default=9100, help="port for --forward-host (default: 9100)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("pyBarcode").setLevel(logging.WARNING)

    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtGui import QGuiApplication

    from .forward import NETWORK, WINDOWS, ForwardTarget
    from .server import PrintServer

    app = QGuiApplication(sys.argv[:1])
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if args.forward_host:
        target = ForwardTarget(NETWORK, host=args.forward_host, port=args.forward_port)
    elif args.forward_printer:
        target = ForwardTarget(WINDOWS, printer=args.forward_printer)
    else:
        target = ForwardTarget()

    paper_mm, paper_dots = PAPER_SIZES[args.paper]
    server = PrintServer(
        args.host, args.port, args.output or default_receipts_dir(), paper_mm, paper_dots, args.idle_timeout, target
    )
    try:
        server.start()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
