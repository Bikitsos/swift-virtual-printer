# Swift Virtual Printer

A virtual ESC/POS receipt printer for Windows (and other platforms). It listens on raw TCP port **9100** (JetDirect style), accepts ESC/POS print jobs and saves each one as a **PDF**. It runs in the system tray or as a command-line service.

Built with [PySide6](https://doc.qt.io/qtforpython/) and managed with [uv](https://docs.astral.sh/uv/).

## Features

- Raw TCP print server (default `0.0.0.0:9100`)
- ESC/POS to PDF rendering at 203 dpi for 58 mm or 80 mm paper
- A new PDF page at each paper cut, sized to fit its content
- System tray app with a settings window, job list, log view and new-job notifications
- Replies to real-time status requests (`DLE EOT`) so drivers that poll the printer don't stall
- Saves every job as received (raw `.bin` data and PDF), as finalized (exactly what goes to the real printer, logo included), and as a JSON text version with one field per printed line
- Preview any job in the app and reprint it to the real printer
- Shows the status of a **Cy-Net 100** fiscal device (via its RESTful API or file-based driver)
- Optionally forwards each job's original bytes to a real printer (network raw TCP or an installed Windows printer)
- Optional PNG/JPG logo printed at the top of every receipt, in the PDF and on the real printer
- Sorts jobs: receipts matching a configurable marker (default `-R-R-R-` on the first text line) get the logo and go to `receipts/`; Z reports (default: any line contains `Z REPORT`) go to `zreports/` without the logo; everything else prints without the logo and goes to `misc/`
- Optional automatic paper cut for documents that don't contain one (**Printer** tab → *Add a paper cut when the document has none*). It appends `GS V 66 0` (feed to the cutter, partial cut) to the final copy and the real printer, and records `"cut_added"` in the JSON.
- Job clean-up, on by default (**Printer** tab → *Reset the printer before each job and finish an unprinted last line*). It prepends `ESC @` to every job and adds a line feed if the last line was never printed (for example a Windows *Generic / Text Only* test page that ends with a form feed). Without this, such a job leaves text stuck in the printer's buffer, the cut is ignored, and the next receipt prints wrong. Disable it with `--no-cleanup`.

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
- Receipts are saved in the `receipts` folder next to the app (see [Saved receipts](#saved-receipts)).
- **Double-click** a job to open its **preview**. You can switch between *Final (as printed, with logo)* and *As received from the POS*, zoom in or out, **Reprint** it, or open the PDF.
- **Right-click** a job for: Preview, Reprint, Open final PDF, Open PDF as received, Open JSON, Copy JSON path, Show in folder.
- **Reprint** sends the job's final bytes (with the logo it was printed with) to the printer set on the **Forwarding** tab. Older jobs without a final copy are reprinted as received.

### Fiscal device (Cy-Net 100)

The **Fiscal** tab shows the status of a Cy-Net 100 fiscal device by asking its driver. The app only *reads* the status; it does not sign documents or issue Z reports.

| Connection | How | Settings |
| --- | --- | --- |
| RESTful API | `POST http://<address>:<port>/check_system` | Driver address and port (default `127.0.0.1:9999`); the driver's web service must be enabled |
| File-based driver (FES32) | Writes `S` to `command918.txt` in the command folder and reads `answer918.txt` from the answer folder (number 918 avoids clashing with the POS's own command files) | Command and answer folders (default `C:\FISCAL_REQUESTANSWER\COMMAND` / `ANSWER`) |

It shows **Ready**, a warning (e.g. *30 or fewer daily zerosets left*), an error (e.g. *Printer paper end detected*, *Z closure time limit*, *Paper end on device*) or *disconnected*, together with the device counters: last Z number, daily and total signatures, and remaining block signatures (REST); or daily signatures, total zerosets, device date/time, fiscal number and model (file driver). Error codes are translated using the tables in the Cy-Net 100 programmer's manual.

The status bar shows a **Fiscal** indicator. With *Check every 60 s* turned on (the default), a tray notification appears when the status changes. **Check now** checks immediately. Click **Apply & Restart** to save the fiscal settings.

### Service menu

**Service → Service tools...** (also **Service...** in the tray menu) opens the administrative tools after you enter the service password. Ask your administrator for it. After 3 wrong attempts the menu is locked for 60 seconds.

| Section | Tools |
| --- | --- |
| Print service | Restart, stop or start the print service; shows its current status |
| Storage | File count and size of the `receipts` and `misc` folders; **Empty** either one, or delete jobs **older than N days** from both. Only job files are deleted; the folder structure is kept, and every deletion asks for confirmation. |
| Maintenance | **Clear log**, **Copy diagnostics** (version, paths, settings and folder sizes, for support), **Reset settings** to defaults (jobs and password are kept), **Change password** |

The password is stored only as a salted PBKDF2-SHA256 hash. A changed password is saved in the user's settings. The menu prevents casual changes by staff; it is not protection against someone with full access to the computer.

### Updates

The app checks [GitHub Releases](https://github.com/Bikitsos/swift-virtual-printer/releases) for a newer version 5 seconds after it starts. Turn this off under **Help → Check for updates on startup**. When an update is found, a tray notification appears; click it to see what's new. To check by hand, use **Help → Check for updates...** or the tray menu.

Choosing **Download and install** does the following:

1. downloads `SwiftVirtualPrinter-Setup-X.Y.Z.exe` from the release and checks it against the SHA-256 digest GitHub publishes (only GitHub HTTPS addresses are accepted)
2. starts the installer silently and closes the app (Windows asks for permission if the app was installed for all users)
3. restarts the app once the update has finished; settings and receipts are kept

When running from source (`uv run`), the app only offers to open the release page.

### Command line

```powershell
uv run virtual-printer [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Address to bind |
| `--port` | `9100` | TCP port |
| `--output` | `<app>/receipts` | Receipts folder (gets `bin/`, `pdf/`, `json/` and `final/` subfolders) |
| `--paper` | `80` | Paper width in mm (`58` or `80`) |
| `--idle-timeout` | `10` | Seconds of inactivity that end a job |
| `--forward-host` | | Also send each job to a real network printer (raw TCP) |
| `--forward-port` | `9100` | Port for `--forward-host` |
| `--logo` | | PNG/JPG printed at the top of every job |
| `--logo-width` | `60` | Logo width as % of the paper width |
| `--logo-dither` | off | Dither the logo (better for photos and gradients) |
| `--auto-cut` | off | Add a paper cut to jobs that don't contain one |
| `--no-cleanup` | | Don't reset the printer before each job or finish an unprinted last line |
| `--forward-printer` | | Also send each job to an installed Windows printer (RAW); cannot be combined with `--forward-host` |

Press `Ctrl+C` to stop.

### Sending a test job

With the printer running:

```powershell
uv run python scripts/send_test.py [host] [port]
```

This sends a sample receipt with styled text, an image, a CODE128 barcode, a QR code and a cut.

## Receipts and other documents

The POS marks real receipts with a marker text on a known line. The rule is set on the **Receipts** tab:

| Setting | Default | Meaning |
| --- | --- | --- |
| Marker text | `-R-R-R-` | Text that identifies a receipt. Leave it empty to treat **every** job as a receipt. |
| Look at | text line 1 | Which printed text line to check, or **any line** (anywhere in the job). Blank lines and images are not counted, so text line 1 is the first line with text. |
| The line | is exactly the marker | Or *contains the marker*, when the marker is part of a longer line |
| Remove the marker from the printout | on | The marker is removed from what's printed. The whole line goes when nothing else is printed on it. |

**Test on selected job** checks both rules against a job selected in the jobs list. It shows where the job would go and lists its text lines: lines checked by the receipt rule are marked `>` and lines matched by the Z report rule are marked `Z`.

### Z reports

**Detect Z reports** (on the Receipts tab, on by default) uses its own marker rule, with the same settings as above. The default is *any line contains* `Z REPORT`, and the marker is not removed. Z reports are checked **before** receipts. A job that matches is printed **without the logo** and saved in a `zreports` folder next to the `receipts` folder, with the same subfolders. If a Z report also carries the receipt marker, that marker is removed as well. Untick the group, or leave the marker empty, to turn detection off.

| Job | What happens | Saved in |
| --- | --- | --- |
| Matches the Z report rule | Printed without the logo (marker removed only if enabled) | `zreports/` |
| Matches the receipt rule | The marker is removed (if enabled) and the [logo](#logo) is added. This is what's printed and saved as final. | `receipts/` |
| Anything else (reports, kitchen tickets, test pages...) | Printed exactly as received, **without** the logo | `misc/` |

All three kinds are forwarded to the real printer, shown in the jobs list (misc jobs greyed and marked `(misc)`, Z reports blue and marked `(Z report)`), and can be previewed and reprinted. The `misc` and `zreports` folders sit next to the `receipts` folder and have the same subfolders. The Service menu can empty each of them. The as-received copies in `bin/` and `pdf/` still contain the markers. Greek markers work with the Greek code pages (`ESC t` 14 / 15 / 47).

On the command line: `--receipt-marker TEXT`, `--marker-line N` (`0` = any line), `--marker-contains`, `--keep-marker`, `--zreport-marker TEXT` (empty = off), `--zreport-line N`, `--zreport-exact`.

## Saved receipts

Each job is saved under the same name (`job_YYYYMMDD_HHMMSS_NNNN`) in four subfolders:

```
receipts/
  bin/    raw ESC/POS bytes exactly as received from the POS
  pdf/    PDF of the receipt as received
  final/  finalized receipt as sent to the real printer (logo and all):
          .pdf to view/share, .bin used for reprints
  json/   text version of the final receipt
```

The `receipts` folder sits next to `SwiftVirtualPrinter.exe` (or in the project root when running with `uv run`). The installer makes it writable for normal users and keeps it on uninstall. If the folder can't be written to, the app uses `%LOCALAPPDATA%\Swift Virtual Printer\receipts` instead. You can choose a different folder in the window or with `--output`.

In the JSON file, every printed line is a separate field. Images, barcodes, QR codes and cuts appear as markers:

```json
{
  "job": "job_20261112_095239_0001",
  "received": "2026-11-12T09:52:39",
  "client": "127.0.0.1:51739",
  "size_bytes": 1230,
  "paper_mm": 80.0,
  "app": "Swift Virtual Printer 0.1.1",
  "files": { "bin": "bin/job_20261112_095239_0001.bin", "pdf": "pdf/job_20261112_095239_0001.pdf" },
  "line_count": 19,
  "lines": {
    "line_001": "SWIFT STORE",
    "line_002": "123 Main Street",
    "line_013": "[IMAGE 128x48]",
    "line_014": "[BARCODE code128] 1234567890",
    "line_015": "[QR] https://example.com/receipt/42",
    "line_019": "[CUT]"
  }
}
```

## Logo

In the **Logo** box, tick **Print logo at the top of every receipt**, choose a PNG or JPG, set the width (as a percentage of the paper width) and click **Apply & Restart**. The preview shows exactly what the printer will print in black and white.

- The image is scaled to the chosen width. Transparent areas print white.
- By default the image is converted to pure black and white: anything darker than 50% grey prints black. This works best for logos. Turn on **Dither** for photos or gradients.
- The logo is inserted centred at the top of each job (after the job's own reset command), as standard ESC/POS raster graphics (`GS v 0`) that almost every receipt printer supports.
- It appears in the PDF and JSON (as `[IMAGE WxH]`, with a `"logo"` field), and it is also sent to the [real printer](#forwarding-to-a-real-printer) when forwarding is on.
- The `bin/` and `pdf/` copies hold the job exactly as the POS sent it, without the logo. The `final/` copies include it.

## Forwarding to a real printer

The virtual printer can pass every job on, byte for byte, to a real ESC/POS printer while still saving the PDF. Set this up in the **Forward to real printer** section of the window, or with the command-line options above.

- **Network printer:** sends the job over raw TCP to `host:port` (usually port 9100).
- **Installed Windows printer:** sends the job to the Windows print queue with the `RAW` datatype, so the driver doesn't change it. This works with USB receipt printers installed in Windows.

Forwarding runs in the background. A failure (for example the printer being offline) is written to the log and does not affect the PDF. Settings that would forward to the virtual printer itself are refused. Do not point a Windows printer queue back at the virtual printer, because that would also create a loop.

### Printer status

The **Forwarding** tab shows whether the real printer is ready (**Check now** checks immediately). The status bar shows it too: green means ready, orange means online but with a warning such as *paper near end*, red means offline. With *Check every 30 s* turned on (the default), a tray notification appears when the printer goes offline, runs out of paper, or comes back. Reprinting to a printer that looks offline asks for confirmation first.

| Printer type | How the status is read | Detects |
| --- | --- | --- |
| Network (raw TCP) | ESC/POS real-time status `DLE EOT 1-4` | offline, cover open, paper near end/out, cutter and other errors, no response |
| Installed Windows printer with the **Epson Advanced Printer Driver** (e.g. TM-T88IV USB) | Epson Status API (`EpsStmApi.dll`) | offline/turned off, cover open, paper near end/out, cutter and other errors |
| Other installed Windows printers | Windows print queue | paused, offline / "Use printer offline", paper problems, error, jobs stuck in the queue |

The Forwarding tab shows which method was used (*via ...*). If the Epson Status API isn't installed, the app falls back to the Windows print queue. The queue is less precise: a USB printer that is switched off often shows up only once a job gets stuck.

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

### Publishing a GitHub release

```powershell
.\release.bat patch    # or minor, major, or an exact X.Y.Z
```

`release.bat` checks that you are on `main` with no uncommitted changes, bumps the version, commits, creates the tag `vX.Y.Z` and pushes both. Pushing the tag starts the [Release workflow](.github/workflows/release.yml), which runs on a Windows runner and:

1. checks that the tag matches `__version__`
2. builds the exe and the installer with `build.bat`
3. publishes a GitHub release with auto-generated release notes, with `SwiftVirtualPrinter-Setup-X.Y.Z.exe` and `SwiftVirtualPrinter-X.Y.Z-portable.zip` attached

You can follow progress under the repository's **Actions** tab. A release can also be started by pushing any `vX.Y.Z` tag by hand, as long as it matches `__version__`.

To test the build pipeline without publishing anything, run the workflow by hand: `gh workflow run release.yml` (or **Actions → Release → Run workflow**). A manual run builds the exe and installer but skips the version check and publishing.

`build.bat` installs the build dependencies, closes any running copy of the app, and builds the app folder `dist\SwiftVirtualPrinter\` (`SwiftVirtualPrinter.exe` plus an `_internal` folder, no console window). It also builds a portable zip of that folder, `dist\SwiftVirtualPrinter-X.Y.Z-portable.zip`. Python does not need to be installed on the target machine. The exe accepts `--minimized`.

The app is built as a folder rather than a single exe on purpose. A single-file exe unpacks Python into `%TEMP%\_MEI...` every time it starts, and antivirus scanners or temp cleaners can break that, causing *"Failed to load Python DLL"* errors. The folder build loads everything from the install folder and starts faster.

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
  updater.py  update check, download and install from GitHub Releases
  logo.py     PNG/JPG logo conversion and injection into jobs
  main.py     command-line entry point
  gui.py      system tray app
scripts/
  send_test.py  sends a sample receipt
```
