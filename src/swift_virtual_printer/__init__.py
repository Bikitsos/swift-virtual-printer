"""Virtual ESC/POS network printer that renders jobs to PDF."""

import os
import sys
from pathlib import Path

# Single source of truth for the version; bump with `python scripts/bump_version.py`.
__version__ = "0.3.0"

APP_NAME = "Swift Virtual Printer"
AUTHOR = "Bikitsos"
COMPANY = "db IMMIX"
CREDITS = f"Software built by {AUTHOR} for {COMPANY}"
GITHUB_REPO = "Bikitsos/swift-virtual-printer"


def build_info() -> tuple[str, str]:
    """Return (build date, git commit) written at exe build time, or development placeholders."""
    try:
        from ._build_info import BUILD_DATE, GIT_COMMIT
    except ImportError:
        return "development build", ""
    return BUILD_DATE, GIT_COMMIT


def app_root() -> Path:
    """Folder of the exe when frozen, otherwise the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_receipts_dir() -> Path:
    """`<app root>/receipts`, or a per-user folder if the app root is not writable."""
    preferred = app_root() / "receipts"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
        return preferred
    except OSError:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / APP_NAME / "receipts"
