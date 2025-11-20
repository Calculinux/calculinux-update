"""Helpers for inspecting RAUC bundles for Calculinux extras."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = ["BundleExtras", "extract_bundle_extras"]

EXTRAS_DIR = Path("extras/opkg")


@dataclass(slots=True)
class BundleExtras:
    root: Path
    opkg_root: Path
    image_status: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class BundleExtractionError(RuntimeError):
    pass


def extract_bundle_extras(bundle_path: Path) -> Optional[BundleExtras]:
    """Extract Calculinux-specific extras from a RAUC bundle.

    Returns None when extras are missing. The caller is responsible for calling
    ``cleanup`` on the returned BundleExtras once finished with the temporary
    directory.
    """

    if not bundle_path.exists():
        raise FileNotFoundError(bundle_path)

    temp_dir = Path(tempfile.mkdtemp(prefix="cup-bundle-"))
    try:
        subprocess.run(
            [
                "unsquashfs",
                "-f",
                "-d",
                str(temp_dir),
                str(bundle_path),
                str(EXTRAS_DIR),
            ],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise BundleExtractionError("unsquashfs binary not found") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise BundleExtractionError(
            f"Failed to extract bundle extras from {bundle_path}: {exc.stderr.decode().strip()}"
        ) from exc

    opkg_path = temp_dir / EXTRAS_DIR
    image_status = opkg_path / "status.image"
    if not opkg_path.is_dir() or not image_status.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    return BundleExtras(root=temp_dir, opkg_root=opkg_path, image_status=image_status)
