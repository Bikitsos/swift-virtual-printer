"""Check GitHub Releases for a newer version, download the installer and run it."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import GITHUB_REPO, __version__

RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
ASSET_NAME = re.compile(r"^SwiftVirtualPrinter-Setup-\d+\.\d+\.\d+\.exe$")
INSTALLER_ARGS = "/SILENT /SP- /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"


def parse_version(text: str) -> tuple[int, int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", text)[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))  # type: ignore[return-value]


def is_newer(version: str, current: str = __version__) -> bool:
    return parse_version(version) > parse_version(current)


def can_self_update() -> bool:
    """Only the installed Windows exe can update itself; source checkouts use git."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _is_trusted_url(url: QUrl) -> bool:
    host = url.host().lower()
    return url.scheme() == "https" and (
        host in ("github.com", "api.github.com") or host.endswith(".githubusercontent.com")
    )


def _request(url: str, accept: bytes) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept)
    request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, f"SwiftVirtualPrinter/{__version__}")
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
    )
    return request


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    notes: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str | None


class UpdateChecker(QObject):
    """Fetches the latest release; emits (Release | None, error message)."""

    finished = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._network = QNetworkAccessManager(self)

    def check(self) -> None:
        request = _request(RELEASES_API, b"application/vnd.github+json")
        request.setTransferTimeout(15000)
        reply = self._network.get(request)
        reply.finished.connect(lambda: self._on_finished(reply))

    def _on_finished(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.finished.emit(None, reply.errorString())
            return
        try:
            data = json.loads(bytes(reply.readAll().data()))
            asset = next(a for a in data.get("assets", []) if ASSET_NAME.match(a.get("name", "")))
        except (ValueError, StopIteration):
            self.finished.emit(None, "The latest release has no installer attached.")
            return
        digest = asset.get("digest") or ""
        self.finished.emit(
            Release(
                version=str(data.get("tag_name", "")).lstrip("v"),
                page_url=data.get("html_url") or RELEASES_PAGE,
                notes=data.get("body") or "",
                asset_name=asset["name"],
                asset_url=asset["browser_download_url"],
                asset_size=int(asset.get("size", 0)),
                sha256=digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else None,
            ),
            "",
        )


class UpdateDownloader(QObject):
    """Downloads a release installer to the temp folder and verifies it; emits (path, error)."""

    progress = Signal(int, int)
    finished = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._network = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None

    def start(self, release: Release) -> None:
        url = QUrl(release.asset_url)
        if not _is_trusted_url(url) or not ASSET_NAME.match(release.asset_name):
            self.finished.emit("", f"Refusing to download from {release.asset_url}")
            return
        self._release = release
        self._target = Path(tempfile.gettempdir()) / release.asset_name
        self._part = self._target.with_suffix(".part")
        self._file = self._part.open("wb")
        self._hash = hashlib.sha256()

        self._reply = self._network.get(_request(release.asset_url, b"application/octet-stream"))
        self._reply.readyRead.connect(self._on_ready_read)
        self._reply.downloadProgress.connect(lambda done, total: self.progress.emit(done, total))
        self._reply.finished.connect(self._on_finished)

    def cancel(self) -> None:
        if self._reply:
            self._reply.abort()

    def _on_ready_read(self) -> None:
        if self._reply:
            self._write(self._reply)

    def _write(self, reply: QNetworkReply) -> None:
        chunk = bytes(reply.readAll().data())
        if chunk:
            self._file.write(chunk)
            self._hash.update(chunk)

    def _on_finished(self) -> None:
        reply, self._reply = self._reply, None
        self._write(reply)
        self._file.close()
        reply.deleteLater()

        error = ""
        if reply.error() != QNetworkReply.NetworkError.NoError:
            error = reply.errorString()
        elif not _is_trusted_url(reply.url()):
            error = f"Download was redirected to an untrusted host: {reply.url().host()}"
        elif self._release.sha256 and self._hash.hexdigest() != self._release.sha256:
            error = "Downloaded file failed the SHA-256 check."
        elif self._release.asset_size and self._part.stat().st_size != self._release.asset_size:
            error = "Downloaded file has the wrong size."

        if error:
            self._part.unlink(missing_ok=True)
            self.finished.emit("", error)
            return
        self._part.replace(self._target)
        self.finished.emit(str(self._target), "")


def run_installer(path: str) -> None:
    """Start the installer detached (Windows handles UAC elevation); the caller should quit right after."""
    os.startfile(path, arguments=INSTALLER_ARGS)  # type: ignore[attr-defined]
