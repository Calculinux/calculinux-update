"""Bundle download + installation helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn

from .config import UpdateConfig
from .mirror import BundleInfo

console = Console()


def _check_disk_space(path: Path, required_bytes: int, margin: float = 1.5) -> None:
    """
    Check if sufficient disk space is available.

    Args:
        path: Path to check (directory or file's parent)
        required_bytes: Minimum bytes needed
        margin: Safety margin multiplier (default 1.5 = 50% extra)

    Raises:
        RuntimeError: If insufficient space available
    """
    if not path.exists():
        path = path.parent

    try:
        stat = os.statvfs(path)
        available = stat.f_bavail * stat.f_frsize
        needed = int(required_bytes * margin)

        if available < needed:
            raise RuntimeError(
                f"Insufficient disk space: need {needed / (1024**2):.1f}MB, "
                f"have {available / (1024**2):.1f}MB available at {path}"
            )
    except (OSError, AttributeError) as e:
        # OSError: filesystem issue, AttributeError: Windows doesn't have statvfs
        console.print(f"[yellow]Warning:[/] Could not check disk space: {e}")


@dataclass(slots=True)
class DownloadResult:
    bundle: BundleInfo
    path: Path
    sha256: str


class UpdateInstaller:
    def __init__(
        self,
        config: UpdateConfig,
        *,
        timeout: float = 60.0,
        max_attempts: int = 3,
        resume_downloads: bool = True,
    ) -> None:
        self.config = config
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._resume_downloads = resume_downloads

    def download(
        self,
        bundle: BundleInfo,
        *,
        expected_sha256: Optional[str] = None,
    ) -> DownloadResult:
        dest_path = self.config.download_dir / bundle.name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        expected = (expected_sha256 or bundle.sha256 or "").lower() or None

        # Check disk space before downloading
        if bundle.size_bytes:
            _check_disk_space(dest_path.parent, bundle.size_bytes)

        if dest_path.exists() and expected:
            existing_hash = _compute_sha256(dest_path)
            if existing_hash.lower() == expected:
                return DownloadResult(bundle=bundle, path=dest_path, sha256=existing_hash)
            dest_path.unlink()

        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                sha = self._download_once(bundle, dest_path, tmp_path, expected)
                return DownloadResult(bundle=bundle, path=dest_path, sha256=sha)
            except ChecksumMismatchError as exc:
                last_error = exc
                with suppress(FileNotFoundError):
                    tmp_path.unlink()
                    dest_path.unlink()
            except httpx.HTTPError as exc:
                last_error = exc

            if attempt < self._max_attempts:
                time.sleep(min(2 ** attempt, 5))

        if last_error:
            raise last_error
        raise RuntimeError("Download failed after retries")

    def _download_once(
        self,
        bundle: BundleInfo,
        dest_path: Path,
        tmp_path: Path,
        expected_sha256: Optional[str],
    ) -> str:
        resume_offset = 0
        headers = {}
        if self._resume_downloads and tmp_path.exists():
            resume_offset = tmp_path.stat().st_size
            if resume_offset:
                headers["Range"] = f"bytes={resume_offset}-"

        if not resume_offset:
            with suppress(FileNotFoundError):
                tmp_path.unlink()

        with httpx.stream(
            "GET",
            bundle.url,
            timeout=self._timeout,
            follow_redirects=True,
            headers=headers,
        ) as response:
            response.raise_for_status()

            if resume_offset and response.status_code != 206:
                resume_offset = 0
                with suppress(FileNotFoundError):
                    tmp_path.unlink()

            total = _determine_total_bytes(response, bundle, resume_offset)
            progress_total = total if total else None
            with Progress(
                TextColumn("{task.fields[channel]}"),
                BarColumn(),
                DownloadColumn(),
                TimeRemainingColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task(
                    "download",
                    total=progress_total,
                    completed=resume_offset,
                    channel=bundle.channel.name,
                )
                mode = "ab" if resume_offset else "wb"
                with tmp_path.open(mode) as fh:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        fh.write(chunk)
                        progress.advance(task, len(chunk))

        sha256 = _compute_sha256(tmp_path)
        if expected_sha256 and sha256.lower() != expected_sha256:
            raise ChecksumMismatchError(
                f"Checksum mismatch for {bundle.name}: expected {expected_sha256} got {sha256}"
            )

        tmp_path.replace(dest_path)
        return sha256

    def run_rauc_install(
        self,
        bundle_path: Path,
        *,
        rauc_binary: str = "rauc",
        sudo: bool = False,
        dry_run: bool = False,
    ) -> None:
        if dry_run:
            console.print(
                f"[yellow]Dry run:[/] would execute '{rauc_binary} install {bundle_path}'"
            )
            return

        if sudo and os.geteuid() != 0:
            cmd = ["sudo", rauc_binary, "install", str(bundle_path)]
        else:
            cmd = [rauc_binary, "install", str(bundle_path)]

        console.print(f"[bold]Executing[/] {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(f"Failed to run {rauc_binary}: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"rauc install exited with {exc.returncode}") from exc

    @staticmethod
    def ensure_binary_available(binary: str = "rauc") -> None:
        if shutil.which(binary) is None:
            raise RuntimeError(f"Required binary '{binary}' not found in PATH")

    @staticmethod
    def format_size(size: Optional[int]) -> str:
        if not size:
            return "?"
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class ChecksumMismatchError(RuntimeError):
    """Raised when the downloaded bundle checksum does not match the expected hash."""


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _determine_total_bytes(response: httpx.Response, bundle: BundleInfo, resume_offset: int) -> int:
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        try:
            total = int(content_range.split("/")[-1])
            return total
        except ValueError:  # pragma: no cover - malformed header
            pass
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return resume_offset + int(content_length)
    return bundle.size_bytes or 0
