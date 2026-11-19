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
    parser.add_argument("--logo", metavar="IMAGE", help="PNG/JPG printed at the top of every job (PDF and real printer)")
    parser.add_argument("--logo-width", type=int, default=60, metavar="PERCENT", help="logo width as %% of paper width (default: 60)")
    parser.add_argument("--logo-dither", action="store_true", help="dither the logo (better for photos)")
    parser.add_argument("--auto-cut", action="store_true", help="add a paper cut to jobs that don't contain one")
    parser.add_argument("--no-cleanup", action="store_true", help="don't reset the printer before each job or finish an unterminated last line")
    parser.add_argument("--receipt-marker", default="-R-R-R-", metavar="TEXT", help="text that marks a job as a receipt; empty = every job (default: -R-R-R-)")
    parser.add_argument("--marker-line", type=int, default=1, metavar="N", help="printed text line to check, 1 = first, 0 = any line (default: 1)")
    parser.add_argument("--marker-contains", action="store_true", help="the line only has to contain the marker instead of being equal to it")
    parser.add_argument("--keep-marker", action="store_true", help="don't remove the marker from the printout")
    parser.add_argument("--zreport-marker", default="Z REPORT", metavar="TEXT", help="text that marks a Z report; empty = don't detect (default: Z REPORT)")
    parser.add_argument("--zreport-line", type=int, default=0, metavar="N", help="text line to check for the Z report marker, 0 = any line (default: 0)")
    parser.add_argument("--zreport-exact", action="store_true", help="the line must equal the Z report marker instead of containing it")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("pyBarcode").setLevel(logging.WARNING)

    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtGui import QGuiApplication

    from .classify import ReceiptRule
    from .forward import NETWORK, WINDOWS, ForwardTarget
    from .logo import LogoSettings
    from .server import PrintServer

    app = QGuiApplication(sys.argv[:1])
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if args.forward_host:
        target = ForwardTarget(NETWORK, host=args.forward_host, port=args.forward_port)
    elif args.forward_printer:
        target = ForwardTarget(WINDOWS, printer=args.forward_printer)
    else:
        target = ForwardTarget()

    logo = LogoSettings(args.logo or "", max(10, min(100, args.logo_width)), args.logo_dither)
    rule = ReceiptRule(args.receipt_marker, max(0, args.marker_line), args.marker_contains, not args.keep_marker)
    zreport_rule = ReceiptRule(args.zreport_marker, max(0, args.zreport_line), not args.zreport_exact, remove=False)
    paper_mm, paper_dots = PAPER_SIZES[args.paper]
    server = PrintServer(
        args.host, args.port, args.output or default_receipts_dir(), paper_mm, paper_dots, args.idle_timeout,
        target, logo, args.auto_cut, not args.no_cleanup, rule, zreport_rule,
    )
    try:
        server.start()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
