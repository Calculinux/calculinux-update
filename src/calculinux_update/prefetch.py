"""Download, before the reboot, the packages the new image needs reinstalled.

After an update that changes the release or the kernel, the overlay's own
packages must be reinstalled from the new image's feeds (see
opkg.reconcile). Doing that after the reboot would need the network, which is
often not up yet. So ``cup install`` resolves and downloads them now, while
the old system is online, into opkg's own cache format:

- an offline opkg root holds the bundle's feed configuration and the new
  image's status file as image status, with an empty writable status, so
  ``opkg install --download-only`` fetches the packages plus every dependency
  the new image lacks;
- the downloads land in PREFETCH_CACHE_DIR, named the way ``opkg --cache-dir``
  looks them up, and the feed lists are kept in PREFETCH_LISTS_DIR, so the
  post-reboot install needs neither ``opkg update`` nor the network.

PREFETCH_STATE_FILE records which image the cache is for (the SHA-256 of its
status file), what was fetched and what could not be.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from rich.console import Console

from .bundle import BundleExtractionError, BundleExtras, extract_bundle_extras
from .opkg.reconcile import compute_reconcile_plan
from .version_compat import load_version_manifest

PREFETCH_CACHE_DIR = Path("/var/cache/calculinux-update/prefetch")
PREFETCH_LISTS_DIR = Path("/var/lib/calculinux-update/prefetch-lists")
PREFETCH_STATE_FILE = Path("/var/lib/calculinux-update/prefetch.json")
WRITABLE_STATUS = Path("/var/lib/opkg/status")
CURRENT_IMAGE_STATUS = Path("/var/lib/opkg/status.image")
CURRENT_VERSION_MANIFEST = Path("/var/lib/calculinux/version-manifest.env")

# Where opkg.conf in the image expects these (the offline root mirrors them)
LISTS_DIR = "var/lib/opkg/lists"
STATUS_FILE = "var/lib/opkg/status"
IMAGE_STATUS_FILE = "var/lib/opkg/status.image"
INFO_DIR = "var/lib/opkg/info"


@dataclass(slots=True)
class PrefetchResult:
    skipped: bool = False
    reason: Optional[str] = None
    packages: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    release_change: bool = False


class PrefetchError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(path: Optional[Path] = None) -> dict:
    path = path or PREFETCH_STATE_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def prefetch_for_bundle(
    bundle_path: Path, bundle_sha256: str, console: Optional[Console] = None
) -> PrefetchResult:
    console = console or Console(stderr=True)
    try:
        extras = extract_bundle_extras(bundle_path)
    except BundleExtractionError as exc:
        return PrefetchResult(skipped=True, reason=str(exc))

    if not extras:
        return PrefetchResult(skipped=True, reason="bundle extras missing")
    try:
        image_status = getattr(extras, "image_status", None)
        if image_status is None or not Path(image_status).exists():
            return PrefetchResult(skipped=True, reason="bundle status.image missing")
        return _prefetch_with_extras(extras, bundle_sha256, console)
    finally:
        extras.cleanup()


def _prefetch_with_extras(
    extras: BundleExtras, bundle_sha256: str, console: Console
) -> PrefetchResult:
    if not WRITABLE_STATUS.exists():
        return PrefetchResult(skipped=True, reason=f"{WRITABLE_STATUS} missing")

    new_manifest = {}
    if getattr(extras, "version_manifest", None):
        new_manifest = load_version_manifest(Path(extras.version_manifest))
    plan = compute_reconcile_plan(
        image_status=Path(extras.image_status),
        writable_status=WRITABLE_STATUS,
        current_status=CURRENT_IMAGE_STATUS,
        old_manifest=load_version_manifest(CURRENT_VERSION_MANIFEST),
        new_manifest=new_manifest,
        classify_duplicates=False,
    )

    _clear_cache()
    image_sha = file_sha256(Path(extras.image_status))
    if not plan.reinstall:
        _write_state(bundle_sha256, image_sha, [], [])
        return PrefetchResult(
            skipped=True,
            reason="no overlay packages need reinstalling for this image",
            release_change=plan.release_change,
        )

    downloader = OpkgDownloader(Path(extras.opkg_root), Path(extras.image_status))
    try:
        missing = downloader.download(plan.reinstall, PREFETCH_CACHE_DIR, PREFETCH_LISTS_DIR)
    except PrefetchError as exc:
        _write_state(bundle_sha256, image_sha, plan.reinstall, list(plan.reinstall))
        return PrefetchResult(
            skipped=True,
            reason=str(exc),
            packages=list(plan.reinstall),
            missing=list(plan.reinstall),
            release_change=plan.release_change,
        )

    _write_state(bundle_sha256, image_sha, plan.reinstall, missing)
    fetched = len(plan.reinstall) - len(missing)
    console.print(
        f"[green]Prefetched[/] {fetched}/{len(plan.reinstall)} packages to reinstall "
        "after the update",
        highlight=False,
    )
    return PrefetchResult(
        packages=list(plan.reinstall),
        missing=missing,
        release_change=plan.release_change,
    )


class OpkgDownloader:
    """Runs opkg against an offline root that looks like the new image."""

    def __init__(self, opkg_root: Path, image_status: Path) -> None:
        self._opkg_root = opkg_root
        self._image_status = image_status

    def download(
        self, packages: Sequence[str], cache_dir: Path, lists_out: Path
    ) -> List[str]:
        """Download ``packages`` and their dependencies; return the ones that failed."""
        source_config = self._opkg_root / "etc/opkg"
        if not source_config.is_dir():
            raise PrefetchError("bundle extras missing /etc/opkg directory")
        if not (source_config / "opkg.conf").exists():
            raise PrefetchError("bundle extras missing opkg.conf")

        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cup-prefetch-") as tmp:
            root = Path(tmp) / "root"
            shutil.copytree(source_config, root / "etc/opkg")
            for sub in (LISTS_DIR, INFO_DIR):
                (root / sub).mkdir(parents=True, exist_ok=True)
            (root / STATUS_FILE).write_text("")
            shutil.copyfile(self._image_status, root / IMAGE_STATUS_FILE)

            result = self._run(root, cache_dir, ["update"])
            if result.returncode != 0:
                raise PrefetchError(f"opkg update failed: {result.stderr.strip()}")

            missing: List[str] = []
            args = ["install", "--download-only", "--force-reinstall"]
            if self._run(root, cache_dir, [*args, *packages]).returncode != 0:
                # Find out which ones cannot be resolved or fetched.
                missing = [
                    pkg for pkg in packages
                    if self._run(root, cache_dir, [*args, pkg]).returncode != 0
                ]

            if lists_out.exists():
                shutil.rmtree(lists_out)
            shutil.copytree(root / LISTS_DIR, lists_out)
            return missing

    @staticmethod
    def _run(root: Path, cache_dir: Path, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "opkg",
                "--conf", str(root / "etc/opkg/opkg.conf"),
                "--offline-root", str(root),
                "--cache-dir", str(cache_dir),
                "--host-cache-dir",
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def _clear_cache() -> None:
    shutil.rmtree(PREFETCH_CACHE_DIR, ignore_errors=True)
    shutil.rmtree(PREFETCH_LISTS_DIR, ignore_errors=True)


def _write_state(
    bundle_sha256: str, image_sha256: str, packages: Sequence[str], missing: Sequence[str]
) -> None:
    PREFETCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "bundle": bundle_sha256,
        "image_status_sha256": image_sha256,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "packages": list(packages),
        "missing": list(missing),
    }
    PREFETCH_STATE_FILE.write_text(json.dumps(state, indent=2))
