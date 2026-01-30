"""Config file handling for RAUC image updates with OverlayFS.

When a RAUC update replaces the base image, config files in the lower layer
are replaced with new versions. However, if the user has modified these files,
the modifications persist in the upper layer and shadow the new versions.

This module detects modified config files and creates .dpkg-new files containing
the new versions from the base image, similar to how opkg handles modified
conffiles during package upgrades.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ConffileInfo",
    "get_package_conffiles",
    "get_all_conffiles",
    "detect_modified_conffiles",
    "create_dpkg_new_files",
]


class ConffileInfo(NamedTuple):
    """Information about a config file."""
    path: str
    package: str
    md5_from_meta: Optional[str] = None


def _compute_md5(file_path: Path) -> Optional[str]:
    """Compute MD5 checksum of a file.
    
    Args:
        file_path: Path to the file
        
    Returns:
        MD5 checksum as hex string, or None if file doesn't exist or can't be read
    """
    try:
        md5 = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                md5.update(chunk)
        return md5.hexdigest()
    except (OSError, IOError) as e:
        LOGGER.debug("Cannot compute MD5 for %s: %s", file_path, e)
        return None


def get_package_conffiles(package_name: str, info_dir: str = "/var/lib/opkg/info") -> List[ConffileInfo]:
    """Get list of config files for a package.
    
    Reads the <package>.conffiles file from opkg's info directory.
    Format is one file path per line, optionally followed by MD5 checksum.
    
    Args:
        package_name: Name of the package
        info_dir: Directory containing opkg info files
        
    Returns:
        List of ConffileInfo objects for the package's config files
    """
    conffiles_path = Path(info_dir) / f"{package_name}.conffiles"
    
    if not conffiles_path.exists():
        LOGGER.debug("No conffiles metadata for package %s", package_name)
        return []
    
    conffiles = []
    try:
        with open(conffiles_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # Format can be:
                # /path/to/file
                # or
                # /path/to/file md5sum
                parts = line.split(None, 1)
                file_path = parts[0]
                md5sum = parts[1] if len(parts) > 1 else None
                
                # Ensure absolute path
                if not file_path.startswith('/'):
                    file_path = '/' + file_path
                
                conffiles.append(ConffileInfo(
                    path=file_path,
                    package=package_name,
                    md5_from_meta=md5sum
                ))
    except (OSError, IOError) as e:
        LOGGER.warning("Failed to read conffiles for %s: %s", package_name, e)
        return []
    
    return conffiles


def get_all_conffiles(
    packages: List[str],
    info_dir: str = "/var/lib/opkg/info"
) -> List[ConffileInfo]:
    """Get config files for multiple packages.
    
    Args:
        packages: List of package names
        info_dir: Directory containing opkg info files
        
    Returns:
        Combined list of ConffileInfo for all packages
    """
    all_conffiles = []
    for package in packages:
        conffiles = get_package_conffiles(package, info_dir)
        all_conffiles.extend(conffiles)
    return all_conffiles


def detect_modified_conffiles(
    image_packages: List[str],
    overlay_mount: str = "/data/overlay"
) -> List[ConffileInfo]:
    """Detect config files that have been modified in the upper layer.
    
    Compares config files between upper and lower layers:
    - If file exists in both layers with different checksums, it's been modified
    - Only considers files from packages in the new base image
    
    Args:
        image_packages: List of package names in the new base image
        overlay_mount: Base path for overlay upper/lower directories
        
    Returns:
        List of ConffileInfo for files that were modified in upper layer
    """
    modified = []
    
    # Get all config files from new base image packages
    image_conffiles = get_all_conffiles(image_packages)
    
    if not image_conffiles:
        LOGGER.debug("No config files found in image packages")
        return []
    
    for conffile in image_conffiles:
        file_path = Path(conffile.path)
        
        # Skip if file doesn't actually exist in the filesystem
        if not file_path.exists():
            LOGGER.debug("Config file %s doesn't exist, skipping", conffile.path)
            continue
        
        # Get paths for upper and lower copies
        # The actual file at /path/to/file is the merged view
        # Upper: /data/overlay/<dir>/upper/<basename>
        # Lower: /data/overlay/<dir>/lower/<basename>
        rel_dir = str(file_path.parent).lstrip('/')
        
        upper_file = Path(overlay_mount) / rel_dir / "upper" / file_path.name
        lower_file = Path(overlay_mount) / rel_dir / "lower" / file_path.name
        
        # If file only exists in lower, it hasn't been modified
        if not upper_file.exists():
            LOGGER.debug("Config file %s not in upper layer, skipping", conffile.path)
            continue
        
        # If lower doesn't exist, the upper file is the only version
        # This shouldn't happen for packages in the image, but handle it
        if not lower_file.exists():
            LOGGER.debug("Config file %s has no lower layer version, skipping", conffile.path)
            continue
        
        # Compute checksums for both versions
        upper_md5 = _compute_md5(upper_file)
        lower_md5 = _compute_md5(lower_file)
        
        if upper_md5 is None or lower_md5 is None:
            LOGGER.debug("Cannot compare %s: upper_md5=%s, lower_md5=%s",
                        conffile.path, upper_md5, lower_md5)
            continue
        
        if upper_md5 != lower_md5:
            LOGGER.info("Modified conffile detected: %s (package: %s)",
                       conffile.path, conffile.package)
            modified.append(conffile)
    
    return modified


def create_dpkg_new_files(
    modified_conffiles: List[ConffileInfo],
    overlay_mount: str = "/data/overlay",
    dry_run: bool = False
) -> Dict[str, str]:
    """Create .dpkg-new files for modified config files.
    
    For each modified config file, copies the new version from the lower layer
    to a .dpkg-new file in the actual filesystem location.
    
    Args:
        modified_conffiles: List of config files that were modified
        overlay_mount: Base path for overlay upper/lower directories
        dry_run: If True, don't actually create files
        
    Returns:
        Dict mapping original file path to .dpkg-new file path
    """
    created_files = {}
    
    for conffile in modified_conffiles:
        file_path = Path(conffile.path)
        dpkg_new_path = Path(str(file_path) + '.dpkg-new')
        
        # Get lower layer version
        rel_dir = str(file_path.parent).lstrip('/')
        lower_file = Path(overlay_mount) / rel_dir / "lower" / file_path.name
        
        if not lower_file.exists():
            LOGGER.warning("Lower layer file missing for %s, skipping", conffile.path)
            continue
        
        if dry_run:
            LOGGER.info("Would create %s from lower layer", dpkg_new_path)
            created_files[conffile.path] = str(dpkg_new_path)
        else:
            try:
                shutil.copy2(lower_file, dpkg_new_path)
                LOGGER.info("Created %s from lower layer", dpkg_new_path)
                created_files[conffile.path] = str(dpkg_new_path)
            except (OSError, IOError) as e:
                LOGGER.error("Failed to create %s: %s", dpkg_new_path, e)
    
    return created_files
