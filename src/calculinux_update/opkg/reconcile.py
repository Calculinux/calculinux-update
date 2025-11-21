"""Logic for reconciling opkg package states across RAUC slots."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .status import (
    load_package_names,
    load_status_entries,
    write_status_entries,
)

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ReconcilePlan",
    "compute_reconcile_plan",
    "prune_writable_status",
    "snapshot_current_slot_status",
]


@dataclass(slots=True)
class ReconcilePlan:
    duplicates: List[str]
    reinstall: List[str]
    upgrade: List[str]

    def any_actions(self) -> bool:
        return bool(self.duplicates or self.reinstall or self.upgrade)


def compute_reconcile_plan(
    image_status: Path,
    writable_status: Path,
    *,
    current_status: Optional[Path] = None,
) -> ReconcilePlan:
    """Compute package operations required after installing a new slot."""

    image_packages = load_package_names(image_status)
    writable_packages = load_package_names(writable_status)

    duplicates = sorted(writable_packages & image_packages)

    reinstall: List[str] = []
    if current_status and current_status.exists():
        current_packages = load_package_names(current_status)
        reinstall = sorted(
            pkg
            for pkg in current_packages
            if pkg not in image_packages and pkg not in writable_packages
        )

    upgrade = sorted(writable_packages)
    return ReconcilePlan(duplicates=duplicates, reinstall=reinstall, upgrade=upgrade)


def prune_writable_status(writable_status: Path, image_packages: Iterable[str]) -> bool:
    """Drop entries from writable status that are provided by the base image."""

    image_set = set(image_packages)
    entries = load_status_entries(writable_status)
    kept = [entry for entry in entries if entry.name not in image_set]
    if len(kept) == len(entries):
        return False
    write_status_entries(writable_status, kept)
    return True


def snapshot_current_slot_status() -> Optional[Path]:
    """Copy the booted slot's immutable opkg status file to a temp path."""

    if shutil.which("rauc") is None:
        return None
    try:
        result = subprocess.run(
            ["rauc", "status", "--output-format=shell"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.warning("Unable to query RAUC status: %s", exc)
        return None

    if result.returncode != 0:
        LOGGER.warning("rauc status returned %s", result.returncode)
        return None

    env = _parse_shell_assignments(result.stdout)
    device = _find_booted_device(env)
    if not device:
        LOGGER.warning("Could not determine booted slot device from RAUC status")
        return None

    mount_dir = Path(tempfile.mkdtemp(prefix="opkg-slot-"))
    try:
        subprocess.run(
            ["mount", "-o", "ro", device, str(mount_dir)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.warning("Failed to mount %s: %s", device, exc)
        shutil.rmtree(mount_dir, ignore_errors=True)
        return None

    try:
        lower_status = mount_dir / "var/lib/opkg/status"
        if not lower_status.exists():
            LOGGER.warning("Mounted slot %s missing status file", device)
            return None
        fd, tmp_path = tempfile.mkstemp(prefix="opkg-status-current-", suffix=".txt")
        os.close(fd)
        shutil.copy2(lower_status, tmp_path)
        return Path(tmp_path)
    except OSError as exc:
        LOGGER.warning("Failed to snapshot status: %s", exc)
        return None
    finally:
        subprocess.run(["umount", str(mount_dir)], check=False)
        shutil.rmtree(mount_dir, ignore_errors=True)


def _parse_shell_assignments(text: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def _find_booted_device(env: Dict[str, str]) -> Optional[str]:
    for idx in range(1, 5):
        state = env.get(f"RAUC_SLOT_STATE_{idx}")
        if state == "booted":
            device = env.get(f"RAUC_SLOT_DEVICE_{idx}")
            if device:
                return device
    # Fallback: search by suffix
    for key, value in env.items():
        if key.endswith("STATE") and value == "booted":
            guess = key[:-5] + "DEVICE"
            if guess in env:
                return env[guess]
    return None
