"""Helpers for restoring lower layer files after package removal in OverlayFS.

This module addresses a specific edge case in Calculinux's dual-layer package
management system:

SCENARIO:
1. User installs a package (e.g., SDL) into the OverlayFS upper layer because it is not present
   in the base image
2. New RAUC update includes a version of that package in the base image (lower layer)
3. During reconciliation, the old overlay package is removed with `opkg remove`
4. OverlayFS creates whiteout files (char device 0:0) for each removed file that
   has a corresponding file in the lower layer
5. These whiteouts persist, blocking access to the newer base image version

SOLUTION:
After removing duplicate packages, this module:
1. Restores opkg metadata files first (enables querying package info from base image)
2. Uses the OVL_IOC_IS_RESTORABLE ioctl to check which files have whiteouts
3. Uses the OVL_IOC_RESTORE_LOWER ioctl to remove whiteouts and restore lower layer files

NOTE: The ioctl automatically invalidates dentries, so no remount is needed.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import struct
import subprocess
from enum import Enum
from pathlib import Path
from typing import List

LOGGER = logging.getLogger(__name__)

class FileRestorability(Enum):
    """Represents the state of a file in an overlay filesystem."""
    WHITEOUT = "whiteout"  # File has a whiteout in upper layer (restorable)
    IN_UPPER = "in_upper"  # Real file exists in upper layer (not restorable)
    IN_LOWER_ONLY = "in_lower_only"  # File only in lower or doesn't exist (not restorable)

__all__ = [
    "restore_package_files",
    "restore_files_for_packages",
    "restore_opkg_metadata",
    "get_package_files",
    "find_restorable_files",
    "has_files_in_upper",
]

def find_overlay_mount_point(path: str) -> str:
    """
    Find the overlay mount point for a given file path by parsing /proc/self/mountinfo.
    Returns the mount point as a string, or '/' if not found.
    """
    best_match = None
    best_len = -1
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) < 10:
                    continue
                mount_point = fields[4]
                fs_type = fields[-3]
                if fs_type != "overlay":
                    continue
                # Find the longest matching mount point prefix
                if path.startswith(mount_point) and len(mount_point) > best_len:
                    best_match = mount_point
                    best_len = len(mount_point)
    except Exception as e:
        LOGGER.warning(f"Failed to parse mountinfo: {e}")
    return best_match if best_match else "/"

OVL_IOC_RESTORE_LOWER = 0x400C4F01  # _IOW('O', 1, ...)
OVL_IOC_IS_RESTORABLE = 0x800C4F02  # _IOR('O', 2, ...)

def check_file_restorability(mount_point: str, path: str) -> FileRestorability:
    """
    Check the restorability state of a file in an overlay filesystem.

    Uses the OVL_IOC_IS_RESTORABLE ioctl which returns:
    - 0: File has a whiteout in upper layer (restorable)
    - -EINVAL: File exists in upper but is NOT a whiteout (real file)
    - -ENOENT: File not in upper layer (only in lower or doesn't exist)

    Args:
        mount_point: Path to the overlay mount point
        path: Absolute path to the file to check

    Returns:
        FileRestorability enum indicating the file's state
    """
    try:
        with open(mount_point, 'r') as f:
            path_bytes = path.encode('utf-8')
            args = struct.pack('QII', id(path_bytes), len(path_bytes), 0)
            fcntl.ioctl(f.fileno(), OVL_IOC_IS_RESTORABLE, args)
        return FileRestorability.WHITEOUT
    except OSError as e:
        if e.errno == errno.EINVAL:
            return FileRestorability.IN_UPPER
        else:  # ENOENT or other errors
            return FileRestorability.IN_LOWER_ONLY

def is_file_restorable(mount_point: str, path: str) -> bool:
    """
    Check if a file has a whiteout that can be restored using OverlayFS ioctl.
    Returns True if restorable, False otherwise.

    This is a convenience wrapper around check_file_restorability() for
    cases where you only need a boolean result.
    """
    return check_file_restorability(mount_point, path) == FileRestorability.WHITEOUT

def restore_lower_via_ioctl(mount_point: str, path: str) -> bool:
    """
    Restore lower layer file using OverlayFS ioctl.
    Returns True on success, False on failure.
    """
    try:
        with open(mount_point, 'r') as f:
            path_bytes = path.encode('utf-8')
            # Use id(path_bytes) for pointer, but this is only valid for the duration of the call
            args = struct.pack('QII', id(path_bytes), len(path_bytes), 0)
            fcntl.ioctl(f.fileno(), OVL_IOC_RESTORE_LOWER, args)
        return True
    except OSError as e:
        LOGGER.warning(f"Failed to restore lower for {path}: {e}")
        return False

def is_package_in_writable_status(package_name: str) -> bool:
    """
    Check if a package is in the writable status file.

    Uses opkg's --writable-only flag to properly query only the writable
    status file, ignoring packages in the base image.

    Args:
        package_name: Name of the package to check

    Returns:
        True if package is in writable status, False otherwise
    """
    try:
        result = subprocess.run(
            ["opkg", "status", "--writable-only", package_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Check if package found and installed
        return result.returncode == 0 and "Status: install ok installed" in result.stdout
    except (subprocess.SubprocessError, OSError) as e:
        LOGGER.warning("Failed to check writable status for %s: %s", package_name, e)
        return False


def get_package_files(package_name: str) -> List[str]:
    """
    Get list of files that belong to a package using opkg.

    Args:
        package_name: Name of the package to query

    Returns:
        List of absolute file paths that belong to the package
    """
    try:
        result = subprocess.run(
            ["opkg", "files", package_name],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

        # opkg files output is one file per line
        files = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("Package ") and line != "Not installed":
                # Ensure absolute path
                if not line.startswith('/'):
                    line = '/' + line
                files.append(line)

        return files

    except subprocess.CalledProcessError as e:
        LOGGER.warning("Failed to get file list for package %s: %s", package_name, e)
        return []
    except (subprocess.TimeoutExpired, OSError) as e:
        LOGGER.warning("Error querying opkg for package %s: %s", package_name, e)
        return []


def find_restorable_files(file_paths: List[str]) -> List[Path]:
    """
    Find files that have whiteouts and can be restored.

    Uses the OVL_IOC_IS_RESTORABLE ioctl to check each file path
    to see if it has a whiteout in the overlay upper layer that can be removed.

    Args:
        file_paths: List of file paths to check for restorability

    Returns:
        List of Path objects for files that can be restored
    """
    restorable = []

    for file_path in file_paths:
        path = Path(file_path)

        # Find the overlay mount point for this file
        mount_point = find_overlay_mount_point(str(path))

        # Check if this file is restorable
        if is_file_restorable(mount_point, str(path)):
            LOGGER.debug(f"Found restorable file: {path} (mount: {mount_point})")
            restorable.append(path)

    return restorable


def restore_package_files(
    package_name: str,
    dry_run: bool = False,
    file_list: List[str] | None = None,
) -> int:
    """
    Restore lower layer files for a removed package by removing overlay whiteouts.

    This function should be called after removing a package from the overlay
    that was shadowing files in the base image. It finds and restores files
    that have been hidden by whiteouts.

    IMPORTANT: After `opkg remove`, the package is no longer in opkg's database,
    so `opkg files` will return nothing. Either call this BEFORE removal, or
    provide the file_list explicitly.

    Args:
        package_name: Name of the package that was removed
        dry_run: If True, only report what would be restored without restoring
        file_list: Optional pre-fetched list of files for the package. If None,
            will attempt to get from opkg (which only works if package still exists
            in opkg's database, or after metadata has been restored).

    Returns:
        Number of files restored (or that would be restored in dry_run)
    """
    # Check if the package is still in the writable status file
    if is_package_in_writable_status(package_name):
        LOGGER.debug(
            "Package %s is still in writable status, skipping restoration",
            package_name,
        )
        return 0

    # Get the file list for the package
    if file_list is not None:
        file_paths = file_list
    else:
        # Try to get from opkg - this works if package metadata has been restored
        file_paths = get_package_files(package_name)

    if not file_paths:
        LOGGER.debug("No file list found for package %s", package_name)
        return 0

    LOGGER.debug("Checking %d files for restoration from package %s",
                 len(file_paths), package_name)

    # Find restorable files (files with whiteouts)
    restorable_files = find_restorable_files(file_paths)

    if not restorable_files:
        LOGGER.debug("No restorable files found for package %s", package_name)
        return 0

    # Restore the files using direct ioctl
    restored_count = 0
    for file_path in restorable_files:
        if dry_run:
            LOGGER.info("Would restore lower for: %s", file_path)
            restored_count += 1
        else:
            mount_point = find_overlay_mount_point(str(file_path))
            if restore_lower_via_ioctl(mount_point=mount_point, path=str(file_path)):
                LOGGER.info(f"Restored lower layer for: {file_path} (mount: {mount_point})")
                restored_count += 1
    if restored_count > 0:
        action = "Would restore" if dry_run else "Restored"
        LOGGER.info(
            "%s lower layer for %d file(s) for package %s",
            action, restored_count, package_name
        )
    return restored_count


def restore_opkg_metadata(
    package_name: str, info_dir: str = "/var/lib/opkg/info", dry_run: bool = False
) -> int:
    """
    Restore opkg metadata files for a package by removing overlay whiteouts.

    When opkg removes a package from the upper layer, it deletes the metadata files
    in /var/lib/opkg/info/ (e.g., package.list, package.control). OverlayFS then
    creates whiteouts that hide the base image's metadata files, preventing
    queries like 'opkg files package' from working even though the package exists
    in status.image.

    This function uses the IS_RESTORABLE ioctl to find and restore those files.

    Args:
        package_name: Name of the package whose metadata should be restored
        info_dir: Directory containing opkg metadata (default: /var/lib/opkg/info)
        dry_run: If True, only report what would be restored without restoring

    Returns:
        Number of metadata files restored (or that would be restored in dry_run)
    """
    info_path = Path(info_dir)
    if not info_path.exists():
        LOGGER.debug("Info directory %s does not exist", info_dir)
        return 0

    restored_count = 0

    # Build list of expected metadata files for this package
    metadata_files = [
        info_path / f"{package_name}.list",
        info_path / f"{package_name}.control",
        info_path / f"{package_name}.conffiles",
        info_path / f"{package_name}.preinst",
        info_path / f"{package_name}.postinst",
        info_path / f"{package_name}.prerm",
        info_path / f"{package_name}.postrm",
    ]

    # Find the overlay mount point for the info directory
    mount_point = find_overlay_mount_point(str(info_path))

    # Check each metadata file to see if it's restorable
    for metadata_file in metadata_files:
        if dry_run:
            # In dry run, just check if restorable
            if is_file_restorable(mount_point, str(metadata_file)):
                LOGGER.info(f"Would restore metadata: {metadata_file}")
                restored_count += 1
        else:
            # Try to restore the file
            if is_file_restorable(mount_point, str(metadata_file)):
                if restore_lower_via_ioctl(mount_point=mount_point, path=str(metadata_file)):
                    LOGGER.info(f"Restored metadata: {metadata_file} (mount: {mount_point})")
                    restored_count += 1

    if restored_count > 0:
        action = "Would restore" if dry_run else "Restored"
        LOGGER.info(
            "%s %d metadata file(s) for package %s",
            action,
            restored_count,
            package_name,
        )

    return restored_count


def has_files_in_upper(package_name: str) -> bool:
    """
    Check if a package has any actual files present in the upper layer.

    This is used to distinguish between:
    1. Packages that exist in status file but have no files in upper layer
       (safe to remove from status file only)
    2. Packages that have actual files in upper layer
       (need physical removal with opkg remove + restoration)

    Uses the OVL_IOC_IS_RESTORABLE ioctl to determine file state:
    - WHITEOUT: File has whiteout in upper (not a real file)
    - IN_UPPER: Real file exists in upper layer (what we're looking for)
    - IN_LOWER_ONLY: File only in lower or doesn't exist (not in upper)

    Args:
        package_name: Name of the package to check

    Returns:
        True if the package has any regular files or directories in upper layer,
        False if only has whiteouts or no files at all
    """
    file_paths = get_package_files(package_name)

    if not file_paths:
        LOGGER.debug("No file list found for package %s", package_name)
        return False

    # Check if any of the package's files exist as real files in upper layer
    for file_path in file_paths:
        path = Path(file_path)
        mount_point = find_overlay_mount_point(str(path))

        try:
            restorability = check_file_restorability(mount_point, str(path))

            if restorability == FileRestorability.IN_UPPER:
                # Real file exists in upper layer
                LOGGER.debug("Package %s has real file in upper: %s", package_name, path)
                return True
            # WHITEOUT and IN_LOWER_ONLY are both "not in upper", continue checking
        except (OSError, FileNotFoundError):
            # File can't be accessed - continue checking others
            continue

    LOGGER.debug("Package %s has no real files in upper layer", package_name)
    return False


def restore_files_for_packages(
    package_names: List[str],
    dry_run: bool = False,
    file_lists: dict[str, List[str]] | None = None,
) -> int:
    """
    Restore lower layer files for multiple removed packages by removing overlay whiteouts.

    Args:
        package_names: List of package names that were removed
        dry_run: If True, only report what would be restored without restoring
        file_lists: Optional dict mapping package names to their file lists.
            Should be fetched BEFORE calling opkg remove since removal deletes
            the package from opkg's database.

    Returns:
        Total number of files restored across all packages
    """
    total_restored = 0

    for package_name in package_names:
        try:
            # IMPORTANT: Restore metadata FIRST so we can query package files from base image
            # Restore opkg metadata to expose base image's .list, .control, etc.
            metadata_restored = restore_opkg_metadata(package_name, dry_run=dry_run)
            total_restored += metadata_restored

            # Now that metadata is restored, we can get the file list from the base image
            # Restore package files
            file_list = file_lists.get(package_name) if file_lists else None
            restored = restore_package_files(
                package_name, dry_run=dry_run, file_list=file_list
            )
            total_restored += restored

        except Exception as e:
            LOGGER.error(
                "Error restoring files for package %s: %s",
                package_name,
                e,
            )

    # No need to remount overlayfs; ioctl handles dentry invalidation
    return total_restored
