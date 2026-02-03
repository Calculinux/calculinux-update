"""Logic for reconciling opkg package states across RAUC slots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable, List, Optional, Tuple

from .overlayfs import has_files_in_upper
from .status import (
    load_package_names,
    load_status_entries,
    write_status_entries,
)

__all__ = [
    "ReconcilePlan",
    "compute_reconcile_plan",
    "prune_writable_status",
]


@dataclass(slots=True)
class ReconcilePlan:
    duplicates: List[str]
    status_only_duplicates: List[str]
    reinstall: List[str]
    upgrade: List[str]
    broken_abi: List[str]
    missing_deps: List[str]

    def any_actions(self) -> bool:
        return bool(
            self.duplicates
            or self.status_only_duplicates
            or self.reinstall
            or self.upgrade
            or self.broken_abi
        )


def check_abi_compatibility(
    writable_packages: Iterable[str],
    image_status: Path,
) -> Tuple[List[str], List[str]]:
    """Check if writable packages have satisfied dependencies in the new image.

    Best-effort check based on `opkg info <pkg>` Depends: fields and package
    names present in `image_status`.
    """
    image_packages = load_package_names(image_status)
    broken: List[str] = []
    missing_deps: List[str] = []

    for pkg in sorted(set(writable_packages)):
        try:
            result = subprocess.run(
                ["opkg", "info", pkg],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        if result.returncode != 0:
            continue

        depends_line = None
        for line in result.stdout.splitlines():
            if line.startswith("Depends:"):
                depends_line = line.split(":", 1)[1].strip()
                break

        if not depends_line:
            continue

        for dep in depends_line.split(","):
            dep = dep.strip()
            if not dep:
                continue

            dep_name = dep.split()[0]
            dep_name = dep_name.split("|")[0].strip()
            if not dep_name:
                continue

            # satisfied by new base image
            if dep_name in image_packages:
                continue

            # satisfied by writable layer?
            try:
                check = subprocess.run(
                    ["opkg", "status", "--writable-only", dep_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                check = None

            if not check or check.returncode != 0 or "Status: install ok installed" not in check.stdout:
                missing_deps.append(f"{pkg} -> {dep_name}")
                if pkg not in broken:
                    broken.append(pkg)

    return broken, missing_deps


def compute_reconcile_plan(
    image_status: Path,
    writable_status: Path,
    *,
    current_status: Optional[Path] = None,
) -> ReconcilePlan:
    """Compute package operations required after installing a new slot.

    Splits duplicate packages into two categories:
    - status_only_duplicates: Packages in both writable status and new base image,
      but with NO files actually present in the upper layer. These can be safely
      removed from the status file without any physical file operations.
    - duplicates: Packages in both writable status and new base image that DO have
      files in the upper layer. These need physical removal with opkg + restoration.

    Args:
        image_status: Path to the new base image's status file
        writable_status: Path to the writable overlay's status file
        current_status: Optional path to current running system's status file

    Returns:
        ReconcilePlan with categorized package lists
    """

    image_packages = load_package_names(image_status)
    writable_packages = load_package_names(writable_status)

    # Find all packages that exist in both places
    all_duplicates = sorted(writable_packages & image_packages)

    # Split duplicates based on whether they have files in upper layer
    duplicates = []
    status_only_duplicates = []

    for pkg in all_duplicates:
        if has_files_in_upper(pkg):
            duplicates.append(pkg)
        else:
            status_only_duplicates.append(pkg)

    reinstall: List[str] = []
    if current_status and current_status.exists():
        current_packages = load_package_names(current_status)
        reinstall = sorted(
            pkg
            for pkg in current_packages
            if pkg not in image_packages and pkg not in writable_packages
        )

    upgrade = sorted(writable_packages)
    broken_abi, missing_deps = check_abi_compatibility(writable_packages, image_status)
    return ReconcilePlan(
        duplicates=duplicates,
        status_only_duplicates=status_only_duplicates,
        reinstall=reinstall,
        upgrade=upgrade,
        broken_abi=broken_abi,
        missing_deps=missing_deps,
    )


def prune_writable_status(writable_status: Path, image_packages: Iterable[str]) -> bool:
    """Drop entries from writable status that are provided by the base image."""

    image_set = set(image_packages)
    entries = load_status_entries(writable_status)
    kept = [entry for entry in entries if entry.name not in image_set]
    if len(kept) == len(entries):
        return False
    write_status_entries(writable_status, kept)
    return True
