@echo off
rem Bumps the version, commits, tags vX.Y.Z and pushes. GitHub Actions then builds and publishes the release.
rem Usage: release.bat patch^|minor^|major^|X.Y.Z
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: release.bat patch^|minor^|major^|X.Y.Z
    exit /b 1
)

git diff --quiet && git diff --cached --quiet || (
    echo [ERROR] You have uncommitted changes. Commit or stash them first.
    exit /b 1
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
if not "%BRANCH%"=="main" (
    echo [ERROR] Releases are made from main ^(current branch: %BRANCH%^).
    exit /b 1
)

git pull --ff-only || goto :fail

for /f "delims=" %%v in ('uv run python scripts\bump_version.py %~1') do set "VERSION=%%v"
if not defined VERSION goto :fail

git rev-parse -q --verify "refs/tags/v%VERSION%" >nul && (
    echo [ERROR] Tag v%VERSION% already exists.
    git checkout -- src\swift_virtual_printer\__init__.py
    exit /b 1
)

uv lock >nul 2>nul
git add src\swift_virtual_printer\__init__.py uv.lock || goto :fail
git commit -m "Release v%VERSION%" || goto :fail
git tag -a "v%VERSION%" -m "Release v%VERSION%" || goto :fail
git push --atomic origin main "v%VERSION%" || goto :fail

echo.
echo Pushed v%VERSION%. GitHub Actions is now building the release:
echo     https://github.com/Bikitsos/swift-virtual-printer/actions
exit /b 0

:fail
echo.
echo [ERROR] Release failed. Check the output above.
exit /b 1
