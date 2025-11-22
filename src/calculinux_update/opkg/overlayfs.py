"""Helpers for cleaning up OverlayFS whiteout files after package removal.

This module addresses a specific edge case in Calculinux's dual-layer package
management system:

SCENARIO:
1. User installs a package he needs (e.g., SDL) into the OverlayFS layer because it is not present
   in the base image
2. New RAUC update includes a version of that package in the base image
3. During reconciliation, the old overlay package is removed with `opkg remove`
4. OverlayFS creates whiteout files (char device 0:0) for each removed file that
   has a corresponding file in the lower layer
5. These whiteouts persist, blocking access to the newer base image version

SOLUTION:
After removing duplicate packages, this module:
1. Gets the file list for removed packages using `opkg files <package>`
2. Checks each file path for whiteout files (character device with major:minor 0:0)
3. Removes the whiteouts to expose the base image files

NOTE: OverlayFS may need to be remounted to fully pick up changes. This typically
happens automatically during the reboot following the update.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import List

LOGGER = logging.getLogger(__name__)

__all__ = [
    "cleanup_package_whiteouts",
    "cleanup_whiteouts_for_packages",
    "cleanup_opkg_metadata_whiteouts",
    "get_package_files",
    "find_whiteout_files",
    "has_files_in_upper",
    "remount_overlayfs",
]


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


def is_whiteout_file(path: Path) -> bool:
    """
    Check if a path is an OverlayFS whiteout file.

    OverlayFS whiteout files are character devices with major:minor 0:0.

    Args:
        path: Path to check

    Returns:
        True if the path is a whiteout file, False otherwise
    """
    try:
        st = path.stat()
        # Check if it's a character device with major/minor 0/0
        return (
            stat.S_ISCHR(st.st_mode) and
            os.major(st.st_rdev) == 0 and
            os.minor(st.st_rdev) == 0
        )
    except (OSError, FileNotFoundError):
        return False


def find_whiteout_files(file_paths: List[str], upper_dir: str = "/") -> List[Path]:
    """
    Find whiteout files in the upper layer that correspond to package files.

    OverlayFS creates whiteout files in the upper layer to hide files from the
    lower layer. When a package is removed, these whiteouts may persist and
    block access to files in the base image.

    Args:
        file_paths: List of file paths to check for whiteouts
        upper_dir: Root directory of the overlay upper layer (default: /)

    Returns:
        List of Path objects for whiteout files found
    """
    whiteouts = []

    for file_path in file_paths:
        # Convert to Path and make relative to upper_dir
        path = Path(upper_dir) / file_path.lstrip('/')

        if is_whiteout_file(path):
            LOGGER.debug("Found whiteout file: %s", path)
            whiteouts.append(path)

    return whiteouts


def cleanup_package_whiteouts(
    package_name: str,
    upper_dir: str = "/",
    dry_run: bool = False,
    file_list: List[str] | None = None,
) -> int:
    """
    Clean up OverlayFS whiteout files for a removed package.

    This function should be called after removing a package from the overlay
    that was shadowing files in the base image. It finds and removes whiteout
    files that would block access to the base image files.

    IMPORTANT: After `opkg remove`, the package is no longer in opkg's database,
    so `opkg files` will return nothing. Either call this BEFORE removal, or
    provide the file_list explicitly.

    Args:
        package_name: Name of the package that was removed
        upper_dir: Root directory of the overlay upper layer (default: /)
        dry_run: If True, only report what would be removed without removing
        file_list: Optional pre-fetched list of files for the package. If None,
            will attempt to get from opkg (which only works if package still exists
            in opkg's database).

    Returns:
        Number of whiteout files removed (or that would be removed in dry_run)
    """
    # Check if the package is still in the writable status file
    if is_package_in_writable_status(package_name):
        LOGGER.debug(
            "Package %s is still in writable status, skipping whiteout cleanup",
            package_name,
        )
        return 0

    # Get the file list for the package
    if file_list is not None:
        file_paths = file_list
    else:
        # Try to get from opkg - this only works if package still in database
        file_paths = get_package_files(package_name)

    if not file_paths:
        LOGGER.debug("No file list found for package %s", package_name)
        return 0

    LOGGER.debug("Checking %d files for whiteouts from package %s",
                 len(file_paths), package_name)

    # Find whiteout files
    whiteouts = find_whiteout_files(file_paths, upper_dir)

    if not whiteouts:
        LOGGER.debug("No whiteout files found for package %s", package_name)
        return 0

    # Remove the whiteout files
    removed_count = 0
    for whiteout in whiteouts:
        try:
            if dry_run:
                LOGGER.info("Would remove whiteout: %s", whiteout)
                removed_count += 1
            else:
                whiteout.unlink()
                LOGGER.info("Removed whiteout file: %s", whiteout)
                removed_count += 1
        except OSError as e:
            LOGGER.warning("Failed to remove whiteout %s: %s", whiteout, e)

    if removed_count > 0:
        action = "Would remove" if dry_run else "Removed"
        LOGGER.info(
            "%s %d whiteout file(s) for package %s",
            action, removed_count, package_name
        )

    return removed_count


def cleanup_opkg_metadata_whiteouts(
    package_name: str, info_dir: str = "/var/lib/opkg/info", dry_run: bool = False
) -> int:
    """
    Clean up OverlayFS whiteout files for opkg metadata after package removal.

    When opkg removes a package from the upper layer, it deletes the metadata files
    in /var/lib/opkg/info/ (e.g., package.list, package.control). OverlayFS then
    creates whiteout files that hide the base image's metadata files, preventing
    queries like 'opkg files package' from working even though the package exists
    in status.image.

    This function removes those whiteout files to expose the base image's metadata.

    Args:
        package_name: Name of the package whose metadata whiteouts to remove
        info_dir: Directory containing opkg metadata (default: /var/lib/opkg/info)
        dry_run: If True, only report what would be removed without removing

    Returns:
        Number of whiteout files removed (or that would be removed in dry_run)
    """
    info_path = Path(info_dir)
    if not info_path.exists():
        LOGGER.debug("Info directory %s does not exist", info_dir)
        return 0

    removed_count = 0

    # Check for whiteout files matching the package
    # OverlayFS creates .wh.<filename> for whited out files
    pattern = f".wh.{package_name}.*"

    try:
        for whiteout in info_path.glob(pattern):
            if is_whiteout_file(whiteout):
                try:
                    if dry_run:
                        LOGGER.info("Would remove metadata whiteout: %s", whiteout)
                        removed_count += 1
                    else:
                        whiteout.unlink()
                        LOGGER.info("Removed metadata whiteout: %s", whiteout)
                        removed_count += 1
                except OSError as e:
                    LOGGER.warning("Failed to remove metadata whiteout %s: %s", whiteout, e)

    except (OSError, IOError) as e:
        LOGGER.warning("Error scanning for metadata whiteouts in %s: %s", info_dir, e)

    if removed_count > 0:
        action = "Would remove" if dry_run else "Removed"
        LOGGER.info(
            "%s %d metadata whiteout file(s) for package %s",
            action,
            removed_count,
            package_name,
        )

    return removed_count


def remount_overlayfs(mount_point: str = "/") -> bool:
    """
    Remount OverlayFS to pick up whiteout changes.

    After removing whiteout files, OverlayFS needs to be remounted to fully
    recognize the changes and expose the lower layer files.

    Args:
        mount_point: Mount point to remount (default: /)

    Returns:
        True if remount succeeded, False otherwise
    """
    try:
        result = subprocess.run(
            ["mount", "-o", "remount", mount_point],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            LOGGER.info("Successfully remounted %s to pick up whiteout changes", mount_point)
            return True
        else:
            LOGGER.warning("Failed to remount %s: %s", mount_point, result.stderr.strip())
            return False
    except (subprocess.SubprocessError, OSError) as e:
        LOGGER.warning("Error remounting %s: %s", mount_point, e)
        return False


def has_files_in_upper(package_name: str, upper_dir: str = "/") -> bool:
    """
    Check if a package has any actual files present in the upper layer.

    This is used to distinguish between:
    1. Packages that exist in status file but have no files in upper layer
       (safe to remove from status file only)
    2. Packages that have actual files in upper layer
       (need physical removal with opkg remove + whiteout cleanup)

    Args:
        package_name: Name of the package to check
        upper_dir: Root directory of the overlay upper layer (default: /)

    Returns:
        True if the package has any regular files or directories in upper layer,
        False if only has whiteouts or no files at all
    """
    file_paths = get_package_files(package_name)

    if not file_paths:
        LOGGER.debug("No file list found for package %s", package_name)
        return False

    # Check if any of the package's files exist as real files (not whiteouts) in upper
    for file_path in file_paths:
        path = Path(upper_dir) / file_path.lstrip('/')

        try:
            # Check if path exists and is NOT a whiteout
            if path.exists() and not is_whiteout_file(path):
                LOGGER.debug("Package %s has real file in upper: %s", package_name, path)
                return True
        except (OSError, FileNotFoundError):
            # File doesn't exist or can't be accessed - continue checking others
            continue

    LOGGER.debug("Package %s has no real files in upper layer", package_name)
    return False


def cleanup_whiteouts_for_packages(
    package_names: List[str],
    upper_dir: str = "/",
    dry_run: bool = False,
    remount: bool = True,
    file_lists: dict[str, List[str]] | None = None,
) -> int:
    """
    Clean up OverlayFS whiteout files for multiple removed packages.

    Args:
        package_names: List of package names that were removed
        upper_dir: Root directory of the overlay upper layer (default: /)
        dry_run: If True, only report what would be removed
        remount: If True, remount the overlay after cleanup to pick up changes
        file_lists: Optional dict mapping package names to their file lists.
            Should be fetched BEFORE calling opkg remove since removal deletes
            the package from opkg's database.

    Returns:
        Total number of whiteout files removed across all packages
    """
    total_removed = 0

    for package_name in package_names:
        try:
            # Clean up package file whiteouts
            file_list = file_lists.get(package_name) if file_lists else None
            removed = cleanup_package_whiteouts(
                package_name, upper_dir, dry_run, file_list=file_list
            )
            total_removed += removed

            # Clean up opkg metadata whiteouts to expose base image's .list, .control, etc.
            metadata_removed = cleanup_opkg_metadata_whiteouts(package_name, dry_run=dry_run)
            total_removed += metadata_removed

        except Exception as e:
            LOGGER.error(
                "Error cleaning up whiteouts for package %s: %s",
                package_name,
                e,
            )

    # Remount the overlay if we actually removed any whiteouts
    if total_removed > 0 and remount and not dry_run:
        LOGGER.info("Remounting overlay to pick up %d whiteout removal(s)", total_removed)
        remount_overlayfs(upper_dir)

    return total_removed
