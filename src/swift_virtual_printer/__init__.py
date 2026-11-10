"""Virtual ESC/POS network printer that renders jobs to PDF."""

# Single source of truth for the version; bump with `python scripts/bump_version.py`.
__version__ = "0.1.0"

APP_NAME = "Swift Virtual Printer"
AUTHOR = "Bikitsos"
COMPANY = "db IMMIX"
CREDITS = f"Software built by {AUTHOR} for {COMPANY}"


def build_info() -> tuple[str, str]:
    """Return (build date, git commit) written at exe build time, or development placeholders."""
    try:
        from ._build_info import BUILD_DATE, GIT_COMMIT
    except ImportError:
        return "development build", ""
    return BUILD_DATE, GIT_COMMIT
