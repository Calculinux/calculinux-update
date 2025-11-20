"""Entry points for RAUC hooks and post-reboot package reconciliation."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from .opkg.reconcile import (
    ReconcilePlan,
    compute_reconcile_plan,
    prune_writable_status,
    snapshot_current_slot_status,
)
from .opkg.status import load_package_names

LOG = logging.getLogger("calculinux_update.hooks")
LOG.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("cup-hook: %(message)s"))
LOG.addHandler(handler)

WRITABLE_STATUS = Path("/var/lib/opkg/status")
CURRENT_IMAGE_STATUS = Path("/var/lib/opkg/status.image")
PENDING_REINSTALL_FILE = Path("/var/lib/opkg/opkg-status-hook.pending-reinstalls")
PENDING_UPGRADE_FILE = Path("/var/lib/opkg/opkg-status-hook.pending-upgrades")
PREFETCH_CACHE_DIR = Path("/var/cache/calculinux-update/prefetch")


def hook_entrypoint() -> None:
    parser = argparse.ArgumentParser(description="Calculinux RAUC hook")
    parser.add_argument("hook", help="Hook phase name from RAUC")
    parser.add_argument("slot", help="Slot identifier")
    args = parser.parse_args()
    run_slot_hook(args.hook, args.slot)


def run_slot_hook(hook: str, slot: str) -> None:
    if hook != "slot-post-install":
        return
    if os.environ.get("RAUC_SLOT_CLASS") != "rootfs":
        return

    mount_point = os.environ.get("RAUC_SLOT_MOUNT_POINT")
    if not mount_point:
        LOG.warning("RAUC_SLOT_MOUNT_POINT not provided for slot %s", slot)
        return

    image_status = Path(mount_point) / "var/lib/opkg/status.image"
    if not image_status.exists():
        LOG.warning("image status %s missing", image_status)
        return

    if not WRITABLE_STATUS.exists():
        LOG.warning("writable status %s missing", WRITABLE_STATUS)
        return

    current_status = CURRENT_IMAGE_STATUS if CURRENT_IMAGE_STATUS.exists() else snapshot_current_slot_status()
    cleanup_snapshot = isinstance(current_status, Path) and current_status != CURRENT_IMAGE_STATUS

    try:
        _prune_writable_status(image_status)
        plan = compute_reconcile_plan(image_status=image_status, writable_status=WRITABLE_STATUS, current_status=current_status)
    finally:
        if cleanup_snapshot and isinstance(current_status, Path):
            current_status.unlink(missing_ok=True)

    _remove_duplicates(plan.duplicates)
    _write_pending(PENDING_REINSTALL_FILE, plan.reinstall, "reinstall")
    _write_pending(PENDING_UPGRADE_FILE, plan.upgrade, "upgrade")


def postreboot_entrypoint() -> None:
    if not PENDING_REINSTALL_FILE.exists() and not PENDING_UPGRADE_FILE.exists():
        return

    if not _run_opkg(["update"]):
        LOG.error("opkg update failed; will retry next boot")
        raise SystemExit(1)

    reinstall_status = _process_pending(PENDING_REINSTALL_FILE, _install_reinstall_pkg)
    upgrade_status = _process_pending(PENDING_UPGRADE_FILE, _upgrade_pkg)

    if reinstall_status and upgrade_status:
        LOG.info("post-reboot package reconciliation complete")
    else:
        LOG.error("post-reboot reconciliation incomplete; will retry")
        raise SystemExit(1)


def _prune_writable_status(image_status: Path) -> None:
    changed = prune_writable_status(WRITABLE_STATUS, load_package_names(image_status))
    if changed:
        LOG.info("pruned writable status against new image")


def _remove_duplicates(duplicates: Iterable[str]) -> None:
    for pkg in duplicates:
        LOG.info("removing duplicate package %s", pkg)
        result = subprocess.run(["opkg", "remove", "--nodeps", pkg], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            LOG.warning("failed to remove %s: %s", pkg, result.stderr.strip())


def _write_pending(path: Path, packages: List[str], label: str) -> None:
    if not packages:
        path.unlink(missing_ok=True)
        LOG.info("no packages require %s", label)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(packages) + "\n")
    LOG.info("queued %d packages for %s", len(packages), label)


def _process_pending(path: Path, handler) -> bool:
    if not path.exists():
        return True

    ok = True
    packages = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    for pkg in packages:
        if not handler(pkg):
            ok = False
    if ok:
        path.unlink(missing_ok=True)
    return ok


def _install_reinstall_pkg(pkg: str) -> bool:
    cached = _find_cached_package(pkg)
    if cached:
        LOG.info("reinstalling %s from cache", pkg)
        result = _run_opkg(["install", "--force-reinstall", str(cached)])
    else:
        LOG.info("reinstalling %s from feed", pkg)
        result = _run_opkg(["install", "--force-reinstall", pkg])
    if not result:
        LOG.warning("failed to reinstall %s", pkg)
    return result


def _upgrade_pkg(pkg: str) -> bool:
    result = _run_opkg(["upgrade", pkg])
    if not result:
        LOG.warning("failed to upgrade %s", pkg)
    return result


def _run_opkg(args: List[str]) -> bool:
    result = subprocess.run(["opkg", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        LOG.warning("opkg %s failed: %s", " ".join(args), result.stderr.strip())
        return False
    return True


def _find_cached_package(pkg: str) -> Optional[Path]:
    if not PREFETCH_CACHE_DIR.exists():
        return None
    candidates = sorted(PREFETCH_CACHE_DIR.glob(f"{pkg}_*.ipk"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
