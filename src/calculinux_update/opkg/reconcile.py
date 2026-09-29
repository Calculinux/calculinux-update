"""Logic for reconciling opkg package states across RAUC slots.

The writable status file (/var/lib/opkg/status) describes the overlay; the
image status file describes the read-only base image. When a new image is
installed, every package in the writable status falls in one of these groups:

- duplicates: also in the new image. Their overlay copy goes away so the
  image's copy wins: a real ``opkg remove`` when files are in the upper layer
  (``duplicates``), only a status prune otherwise (``status_only_duplicates``).
- leaked: image packages opkg copied into the writable status file, not user
  installs; their entries are pruned. They have no files in the upper layer
  and either are in the current image or have no ``.list`` file at all (an
  entry left over from an earlier image; a real install always has one).
- overlay: everything else, i.e. what the user installed on top of the image.

Overlay packages keep working on the new image unless its ABI changed:

- a different release (codename or Yocto series) reinstalls all of them;
- a different kernel reinstalls the overlay's kernel-module-* packages.

Otherwise nothing needs to be installed after the update.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .overlayfs import INFO_DIR, OverlayInfo, has_files_in_upper
from .status import (
    load_package_names,
    load_status_entries,
    write_status_entries,
)

__all__ = [
    "ReconcilePlan",
    "compute_reconcile_plan",
    "image_kernel_abi",
    "is_release_change",
    "prune_writable_status",
]

_KERNEL_PACKAGE = re.compile(r"^kernel-(\d.*)$")
_KERNEL_MODULE_PREFIX = "kernel-module-"


@dataclass(slots=True)
class ReconcilePlan:
    duplicates: List[str] = field(default_factory=list)
    status_only_duplicates: List[str] = field(default_factory=list)
    leaked: List[str] = field(default_factory=list)
    overlay: List[str] = field(default_factory=list)
    reinstall: List[str] = field(default_factory=list)
    release_change: bool = False
    kernel_abi: Optional[str] = None

    def any_actions(self) -> bool:
        return bool(
            self.duplicates or self.status_only_duplicates or self.leaked or self.reinstall
        )


def is_release_change(old_manifest: Dict[str, str], new_manifest: Dict[str, str]) -> bool:
    """True when the codename or Yocto series differs between the manifests.

    Missing information counts as the same release; the stepping-stone gate
    guarantees manifests on both sides of a release upgrade.
    """
    for key in ("CALCULINUX_CODENAME", "YOCTO_VERSION"):
        old = (old_manifest or {}).get(key, "").strip()
        new = (new_manifest or {}).get(key, "").strip()
        if old and new and old != new:
            return True
    return False


def image_kernel_abi(image_packages: Iterable[str]) -> Optional[str]:
    """The kernel ABI string (e.g. 6.1.99-rockchip-standard) an image ships."""
    for name in sorted(image_packages):
        match = _KERNEL_PACKAGE.match(name)
        if match:
            return match.group(1)
    return None


def _needs_kernel_rebuild(package: str, kernel_abi: Optional[str]) -> bool:
    if not kernel_abi or not package.startswith(_KERNEL_MODULE_PREFIX):
        return False
    return not package.endswith("-" + kernel_abi)


def compute_reconcile_plan(
    image_status: Path,
    writable_status: Path,
    *,
    current_status: Optional[Path] = None,
    old_manifest: Optional[Dict[str, str]] = None,
    new_manifest: Optional[Dict[str, str]] = None,
    classify_duplicates: bool = True,
    info_dir: Path = INFO_DIR,
) -> ReconcilePlan:
    """Work out what installing the image described by ``image_status`` needs.

    Args:
        image_status: the new image's status file
        writable_status: the overlay's status file
        current_status: the running image's status file, to recognise leaked
            image entries
        old_manifest, new_manifest: version manifests of the running and the
            new image, to detect a release change
        classify_duplicates: split duplicates by whether they have files in the
            upper layer. Prefetch only needs the reinstall list and skips it.
    """
    image_packages = load_package_names(image_status)
    writable_packages = load_package_names(writable_status)
    current_packages: Set[str] = set()
    if current_status is not None and current_status.exists():
        current_packages = load_package_names(current_status)

    plan = ReconcilePlan(
        release_change=is_release_change(old_manifest or {}, new_manifest or {}),
        kernel_abi=image_kernel_abi(image_packages),
    )
    all_duplicates = sorted(writable_packages & image_packages)
    candidates = sorted(writable_packages - image_packages)
    leak_candidates = [
        pkg for pkg in candidates
        if pkg in current_packages or not (Path(info_dir) / f"{pkg}.list").exists()
    ]

    with OverlayInfo() as overlay:
        if classify_duplicates:
            for pkg in all_duplicates:
                if has_files_in_upper(pkg, overlay):
                    plan.duplicates.append(pkg)
                else:
                    plan.status_only_duplicates.append(pkg)
        leaked = {pkg for pkg in leak_candidates if not has_files_in_upper(pkg, overlay)}

    plan.leaked = sorted(leaked)
    plan.overlay = [pkg for pkg in candidates if pkg not in leaked]
    if plan.release_change:
        plan.reinstall = list(plan.overlay)
    else:
        plan.reinstall = [
            pkg for pkg in plan.overlay if _needs_kernel_rebuild(pkg, plan.kernel_abi)
        ]
    return plan


def prune_writable_status(writable_status: Path, packages: Iterable[str]) -> bool:
    """Drop the given packages' entries from the writable status file."""

    drop = set(packages)
    entries = load_status_entries(writable_status)
    kept = [entry for entry in entries if entry.name not in drop]
    if len(kept) == len(entries):
        return False
    write_status_entries(writable_status, kept)
    return True
