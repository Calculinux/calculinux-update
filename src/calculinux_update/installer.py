"""Bundle download + installation helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn

from .config import UpdateConfig
from .mirror import BundleInfo

console = Console()


@dataclass(slots=True)
class DownloadResult:
    bundle: BundleInfo
    path: Path
    sha256: str


class UpdateInstaller:
    def __init__(self, config: UpdateConfig, *, timeout: float = 60.0) -> None:
        self.config = config
        self._timeout = timeout

    def download(self, bundle: BundleInfo) -> DownloadResult:
        dest_path = self.config.download_dir / bundle.name
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with httpx.stream("GET", bundle.url, timeout=self._timeout, follow_redirects=True) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0)) or bundle.size_bytes or 0
            hasher = hashlib.sha256()
            with Progress(
                TextColumn("{task.fields[channel]}"),
                BarColumn(),
                DownloadColumn(),
                TimeRemainingColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task("download", total=total, channel=bundle.channel.name)
                with dest_path.open("wb") as fh:
                    for chunk in response.iter_bytes():
                        fh.write(chunk)
                        hasher.update(chunk)
                        progress.advance(task, len(chunk))

        return DownloadResult(bundle=bundle, path=dest_path, sha256=hasher.hexdigest())

    def run_rauc_install(self, bundle_path: Path, *, rauc_binary: str = "rauc", sudo: bool = False, dry_run: bool = False) -> None:
        if dry_run:
            console.print(f"[yellow]Dry run:[/] would execute '{rauc_binary} install {bundle_path}'")
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
