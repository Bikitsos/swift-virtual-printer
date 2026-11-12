# PyInstaller spec: build with `uv run --group build pyinstaller --noconfirm --clean virtual-printer.spec`
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)
from PySide6.QtGui import QGuiApplication

from swift_virtual_printer import APP_NAME, COMPANY, CREDITS, __version__
from swift_virtual_printer.gui import make_icon

root = Path(SPECPATH)
build_dir = root / "build"
build_dir.mkdir(exist_ok=True)

# Stamp the build date and git commit into the package so the About screen can show them.
try:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    commit = ""
(root / "src" / "swift_virtual_printer" / "_build_info.py").write_text(
    f'BUILD_DATE = "{datetime.now():%Y-%m-%d %H:%M}"\nGIT_COMMIT = "{commit}"\n', encoding="utf-8"
)

# Render the tray icon to an .ico so the exe uses the same artwork.
_app = QGuiApplication.instance() or QGuiApplication([])
icon_path = str(build_dir / "app.ico")
make_icon(True).pixmap(64, 64).save(icon_path)

numbers = tuple(int(n) for n in re.findall(r"\d+", __version__)[:3])
file_version = (numbers + (0, 0, 0, 0))[:4]
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=file_version, prodvers=file_version),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", COMPANY),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", "SwiftVirtualPrinter"),
                StringStruct("LegalCopyright", CREDITS),
                StringStruct("OriginalFilename", "SwiftVirtualPrinter.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)
version_path = build_dir / "version_info.txt"
version_path.write_text(str(version_info), encoding="utf-8")

a = Analysis(
    [str(root / "scripts" / "gui_entry.py")],
    pathex=[str(root / "src")],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
# One-folder build: one-file exes unpack Python to %TEMP% on every start, which antivirus and
# temp cleaners can break ("Failed to load Python DLL").
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="SwiftVirtualPrinter",
    console=False,
    icon=icon_path,
    version=str(version_path),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SwiftVirtualPrinter", upx=False)
