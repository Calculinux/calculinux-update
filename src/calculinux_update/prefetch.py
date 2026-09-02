"""Pre-download OPKG packages needed after installing a RAUC bundle."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from rich.console import Console

from .bundle import BundleExtractionError, BundleExtras, extract_bundle_extras
from .opkg.reconcile import ReconcilePlan, compute_reconcile_plan

PREFETCH_CACHE_DIR = Path("/var/cache/calculinux-update/prefetch")
PREFETCH_STATE_FILE = Path("/var/lib/calculinux-update/prefetch.json")
WRITABLE_STATUS = Path("/var/lib/opkg/status")
CURRENT_IMAGE_STATUS = Path("/var/lib/opkg/status.image")


@dataclass(slots=True)
class PrefetchResult:
    skipped: bool = False
    downloaded: int = 0
    planned: int = 0
    reason: Optional[str] = None


class PrefetchError(RuntimeError):
    pass


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
    image_status = getattr(extras, "image_status", None)
    if image_status is None or not Path(image_status).exists():
        extras.cleanup()
        return PrefetchResult(skipped=True, reason="bundle status.image missing")

    try:
        return _prefetch_with_extras(extras, bundle_sha256, console)
    finally:
        extras.cleanup()


def _prefetch_with_extras(
    extras: BundleExtras, bundle_sha256: str, console: Console
) -> PrefetchResult:
    if not WRITABLE_STATUS.exists():
        return PrefetchResult(skipped=True, reason=f"{WRITABLE_STATUS} missing")

    # Require status.image from current slot - all current Calculinux images have this
    if not CURRENT_IMAGE_STATUS.exists():
        return PrefetchResult(
            skipped=True,
            reason=f"{CURRENT_IMAGE_STATUS} missing - image may be too old"
        )

    plan = compute_reconcile_plan(
        image_status=extras.image_status,
        writable_status=WRITABLE_STATUS,
        current_status=CURRENT_IMAGE_STATUS,
    )

    if not plan.reinstall:
        return PrefetchResult(skipped=True, reason="no reinstall packages")

    downloader = OpkgDownloader(extras.opkg_root)
    try:
        downloaded = downloader.download(plan.reinstall, PREFETCH_CACHE_DIR)
    except PrefetchError as exc:
        return PrefetchResult(skipped=True, reason=str(exc))
    _write_state(bundle_sha256, plan)
    console.print(
        (
            f"[green]Prefetched[/] {downloaded}/{len(plan.reinstall)} "
            f"reinstall packages into {PREFETCH_CACHE_DIR}"
        ),
        highlight=False,
    )
    return PrefetchResult(downloaded=downloaded, planned=len(plan.reinstall))


class OpkgDownloader:
    def __init__(self, opkg_root: Path) -> None:
        self._opkg_root = opkg_root

    def download(self, packages: Sequence[str], cache_dir: Path) -> int:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cup-prefetch-") as tmp:
            offline_root = Path(tmp) / "root"
            config_root = offline_root / "etc/opkg"
            source_config = self._opkg_root / "etc/opkg"
            if not source_config.is_dir():
                raise PrefetchError("bundle extras missing /etc/opkg directory")
            shutil.copytree(source_config, config_root)
            conf_path = config_root / "opkg.conf"
            if not conf_path.exists():
                raise PrefetchError("bundle extras missing opkg.conf")
            _patch_opkg_conf(conf_path, offline_root)
            try:
                self._run_opkg(conf_path, offline_root, ["update"])
            except subprocess.CalledProcessError as exc:
                raise PrefetchError(f"opkg update failed: {exc.stderr}") from exc
            downloaded = 0
            for pkg in packages:
                if self._download_single(conf_path, offline_root, pkg, cache_dir):
                    downloaded += 1
            return downloaded

    def _download_single(
        self,
        conf_path: Path,
        offline_root: Path,
        package: str,
        cache_dir: Path,
    ) -> bool:
        result = subprocess.run(
            [
                "opkg",
                "--conf",
                str(conf_path),
                "--offline-root",
                str(offline_root),
                "download",
                package,
            ],
            cwd=cache_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            return False
        return True

    def _run_opkg(self, conf_path: Path, offline_root: Path, args: List[str]) -> None:
        subprocess.run(
            [
                "opkg",
                "--conf",
                str(conf_path),
                "--offline-root",
                str(offline_root),
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def _patch_opkg_conf(conf_path: Path, offline_root: Path) -> None:
    data = conf_path.read_text().splitlines()
    (offline_root / "var/lib/opkg").mkdir(parents=True, exist_ok=True)
    overrides = {
        "option lists_dir": offline_root / "var/lib/opkg/lists",
        "option info_dir": offline_root / "var/lib/opkg/info",
        "option status_file": offline_root / "var/lib/opkg/status",
        "option image_status_file": offline_root / "var/lib/opkg/status.image",
    }
    overrides["option lists_dir"].mkdir(parents=True, exist_ok=True)
    overrides["option info_dir"].mkdir(parents=True, exist_ok=True)

    patched: List[str] = []
    seen = {key: False for key in overrides}
    for line in data:
        for key, target in overrides.items():
            if line.strip().startswith(key):
                line = f"{key} {target}"
                seen[key] = True
                break
        patched.append(line)

    for key, target in overrides.items():
        if not seen[key]:
            patched.append(f"{key} {target}")

    conf_path.write_text("\n".join(patched) + "\n")


def _write_state(bundle_sha256: str, plan: ReconcilePlan) -> None:
    PREFETCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "bundle": bundle_sha256,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reinstall": plan.reinstall,
    }
    PREFETCH_STATE_FILE.write_text(json.dumps(state, indent=2))
