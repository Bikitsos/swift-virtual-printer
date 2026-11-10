@echo off
rem Builds dist\SwiftVirtualPrinter.exe and, if Inno Setup 6 is installed, the installer.
rem Usage: build.bat [patch ^| minor ^| major ^| X.Y.Z]   (optional version bump before building)
setlocal
cd /d "%~dp0"

where uv >nul 2>nul || (
    echo [ERROR] uv not found. Install it from https://docs.astral.sh/uv/
    exit /b 1
)

if not "%~1"=="" (
    echo === Bumping version: %~1 ===
    uv run python scripts\bump_version.py %~1 || goto :fail
)

for /f "delims=" %%v in ('uv run python scripts\bump_version.py') do set "APP_VERSION=%%v"
if not defined APP_VERSION goto :fail
echo === Version %APP_VERSION% ===

echo === Syncing dependencies ===
uv sync --group build || goto :fail

echo === Stopping running instance (if any) ===
taskkill /F /IM SwiftVirtualPrinter.exe >nul 2>nul

echo === Building exe ===
uv run --group build pyinstaller --noconfirm --clean virtual-printer.spec || goto :fail

set "ISCC="
for %%p in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist %%p set "ISCC=%%~p"
if not defined ISCC for /f "delims=" %%i in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%i"

if not defined ISCC (
    echo.
    echo Built dist\SwiftVirtualPrinter.exe ^(version %APP_VERSION%^)
    echo Inno Setup 6 not found, skipping installer. Install it with:
    echo     winget install JRSoftware.InnoSetup
    exit /b 0
)

echo === Building installer (version %APP_VERSION%) ===
"%ISCC%" /Qp /DMyAppVersion=%APP_VERSION% installer.iss || goto :fail

echo.
echo Built dist\SwiftVirtualPrinter.exe (version %APP_VERSION%)
echo Built dist\SwiftVirtualPrinter-Setup-%APP_VERSION%.exe
exit /b 0

:fail
echo.
echo [ERROR] Build failed.
exit /b 1
