# Swift Virtual Printer

A virtual ESC/POS receipt printer for Windows (and other platforms). It listens on raw TCP port **9100** (JetDirect style), accepts ESC/POS print jobs and saves each one as a **PDF**. It runs in the system tray or as a command-line service.

Built with [PySide6](https://doc.qt.io/qtforpython/) and managed with [uv](https://docs.astral.sh/uv/).

## Features

- Raw TCP print server (default `0.0.0.0:9100`)
- ESC/POS to PDF rendering at 203 dpi for 58 mm or 80 mm paper
- A new PDF page at each paper cut, sized to fit its content
- System tray app with a settings window, job list, log view and new-job notifications
- Replies to real-time status requests (`DLE EOT`) so drivers that poll the printer don't stall
- Can also save the raw ESC/POS data (`.bin`) for debugging
- Optionally forwards each job's original bytes to a real printer (network raw TCP or an installed Windows printer)

### Supported ESC/POS commands

| Area | Commands |
| --- | --- |
| Text style | `ESC !`, `ESC E`, `ESC G`, `ESC -`, `ESC M`, `GS !`, `GS B` |
| Layout | `LF`, `HT`, `ESC a`, `ESC 2`, `ESC 3`, `ESC $`, `ESC \`, `ESC d`, `ESC J` |
| Code pages | `ESC t` (CP437, 850, 852, 858, 860, 863, 865, 866, 1252) |
| Images | `ESC *`, `GS v 0`, `GS ( L` / `GS 8 L` (store and print raster graphics) |
| Barcodes | `GS k` (UPC-A, EAN-13, EAN-8, CODE39, ITF, CODABAR, CODE128), `GS h`, `GS w`, `GS H` |
| QR codes | `GS ( k` (size, error correction, store, print) |
| Paper | `GS V`, `ESC i`, `ESC m` (cut, which starts a new page) |
| Misc | `ESC @` reset; drawer kick, beeper and other unsupported commands are skipped safely |

Not supported: page mode, NV/downloaded logos, and UPC-E / CODE93 barcodes (these print as a labelled placeholder box).

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.10+ (uv can install it for you)

## Installation

```powershell
git clone <repo-url> swift-virtual-printer
cd swift-virtual-printer
uv sync
```

## Usage

### System tray app

```powershell
uv run virtual-printer-gui              # open the window and the tray icon
uv run virtual-printer-gui --minimized  # start hidden in the tray
```

- **Tray icon:** green means listening, grey means stopped. Click it to open the window.
- **Right-click menu:** Show window, Open last job, Open jobs folder, Start/Stop printer, Quit.
- Closing the window hides it to the tray. Use **Quit** to exit.
- Settings are remembered between runs. Click **Apply & Restart** after changing them.
- PDFs are saved to `Documents\Virtual Printer` by default.

### Command line

```powershell
uv run virtual-printer [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Address to bind |
| `--port` | `9100` | TCP port |
| `--output` | `./jobs` | Folder for PDFs |
| `--paper` | `80` | Paper width in mm (`58` or `80`) |
| `--idle-timeout` | `10` | Seconds of inactivity that end a job |
| `--save-raw` | off | Also save the raw ESC/POS data as `.bin` |
| `--forward-host` | | Also send each job to a real network printer (raw TCP) |
| `--forward-port` | `9100` | Port for `--forward-host` |
| `--forward-printer` | | Also send each job to an installed Windows printer (RAW); cannot be combined with `--forward-host` |

Press `Ctrl+C` to stop.

### Sending a test job

With the printer running:

```powershell
uv run python scripts/send_test.py [host] [port]
```

This sends a sample receipt with styled text, an image, a CODE128 barcode, a QR code and a cut.

## Forwarding to a real printer

The virtual printer can pass every job on, byte for byte, to a real ESC/POS printer while still saving the PDF. Set this up in the **Forward to real printer** section of the window, or with the command-line options above.

- **Network printer:** sends the job over raw TCP to `host:port` (usually port 9100).
- **Installed Windows printer:** sends the job to the Windows print queue with the `RAW` datatype, so the driver doesn't change it. This works with USB receipt printers installed in Windows.

Forwarding runs in the background. A failure (for example the printer being offline) is written to the log and does not affect the PDF. Settings that would forward to the virtual printer itself are refused. Do not point a Windows printer queue back at the virtual printer, because that would also create a loop.

## Building a Windows exe

```powershell
.\build.bat            # build with the current version
.\build.bat patch      # 0.1.0 -> 0.1.1, then build
.\build.bat minor      # 0.1.0 -> 0.2.0, then build
.\build.bat major      # 0.1.0 -> 1.0.0, then build
.\build.bat 2.3.4      # set an exact version, then build
```

### Versioning

The version is defined in one place: `__version__` in [src/swift_virtual_printer/__init__.py](src/swift_virtual_printer/__init__.py). The package metadata, the exe's file properties (right-click → Properties → Details), the installer file name, the window title, the tray tooltip, the **About** screen and `--version` all read it from there. Show or change it without building:

```powershell
uv run python scripts/bump_version.py          # show the current version
uv run python scripts/bump_version.py patch    # bump it
```

Each exe build also records its build date and git commit, and the **About** screen (Help → About, or About in the tray menu) shows them.

`build.bat` installs the build dependencies, closes any running copy of the app, and builds `dist\SwiftVirtualPrinter.exe`: a single standalone file (about 40 MB) with no console window. Python does not need to be installed on the target machine. The exe accepts `--minimized`.

If [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed (`winget install JRSoftware.InnoSetup`), it also builds an installer, `dist\SwiftVirtualPrinter-Setup-<version>.exe`, from [installer.iss](installer.iss). The version number comes from `pyproject.toml`.

The installer:

- installs for all users (admin) or only the current user (you choose in the setup dialog)
- adds Start Menu shortcuts, plus an optional desktop shortcut
- can start the app with Windows, minimized to the tray (on by default)
- can add a Windows Firewall rule for port 9100 (off by default, admin installs only)
- registers an uninstaller under **Settings → Apps** and in the Start Menu

Uninstalling closes the app and removes its files, shortcuts, startup entry, firewall rule and saved settings. Printed PDFs are kept.

To build only the exe by hand:

```powershell
uv run --group build pyinstaller --noconfirm --clean virtual-printer.spec
```

## Printing from Windows

1. Open **Settings → Bluetooth & devices → Printers & scanners → Add device → Add manually**.
2. Choose **Add a printer using an IP address or hostname**.
3. Enter the machine's address (for example `127.0.0.1`) and select **TCP/IP Device**.
4. When asked for the port type, use **Standard / Generic Network Card** with **Raw** protocol on port **9100**.
5. Pick an ESC/POS driver (for example a vendor's "TM-T88" or "POS-80" driver). **Generic / Text Only** also works for plain text.

Point-of-sale software that prints straight to a network printer can be given the IP address and port 9100 directly.

## How a job ends

A job is finished when the client closes the connection. If the client keeps the connection open, the job ends after the idle timeout. Jobs larger than 64 MB are rejected.

## Security

By default the printer listens on all network interfaces, like a real network printer, so anyone on your network can send it jobs. Use `--host 127.0.0.1` (or set the listen address in the settings window) to accept only local jobs. Allow port 9100 through the Windows Firewall only if other machines need to print to it.

## Project layout

```
src/swift_virtual_printer/
  escpos.py   ESC/POS parser that builds a page layout
  render.py   PDF renderer (QPdfWriter / QPainter)
  server.py   TCP print server (QTcpServer)
  forward.py  forwarding to a real printer (raw TCP / Windows spooler)
  main.py     command-line entry point
  gui.py      system tray app
scripts/
  send_test.py  sends a sample receipt
```
