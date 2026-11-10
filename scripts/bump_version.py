"""Show or bump the app version.

Usage:
    python scripts/bump_version.py                 # print current version
    python scripts/bump_version.py patch|minor|major
    python scripts/bump_version.py 1.2.3
"""

import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "src" / "swift_virtual_printer" / "__init__.py"
PATTERN = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)


def main() -> int:
    text = INIT.read_text(encoding="utf-8")
    match = PATTERN.search(text)
    if not match:
        print(f"__version__ not found in {INIT}", file=sys.stderr)
        return 1
    current = match.group(1)
    if len(sys.argv) < 2:
        print(current)
        return 0

    arg = sys.argv[1].lower()
    if re.fullmatch(r"\d+\.\d+\.\d+", arg):
        new = arg
    elif arg in ("major", "minor", "patch"):
        major, minor, patch = (int(p) for p in re.findall(r"\d+", current)[:3])
        new = {
            "major": f"{major + 1}.0.0",
            "minor": f"{major}.{minor + 1}.0",
            "patch": f"{major}.{minor}.{patch + 1}",
        }[arg]
    else:
        print("Expected patch, minor, major or X.Y.Z", file=sys.stderr)
        return 1

    INIT.write_text(PATTERN.sub(f'__version__ = "{new}"', text, count=1), encoding="utf-8")
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
