"""Logic for reconciling opkg package states across RAUC slots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

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
