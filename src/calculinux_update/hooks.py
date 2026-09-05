"""Entry points for RAUC hooks and post-reboot package reconciliation."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .opkg.conffiles import create_dpkg_new_files, detect_modified_conffiles
from .opkg.overlayfs import get_package_files, restore_files_for_packages
from .opkg.reconcile import (
    compute_reconcile_plan,
    prune_writable_status,
)
from .opkg.status import load_package_names, load_status_entries, write_status_entries
from .version_compat import check_compatibility, load_version_manifest

LOG = logging.getLogger("calculinux_update.hooks")
LOG.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("cup-hook: %(message)s"))
LOG.addHandler(handler)

# OPKG file locations
WRITABLE_STATUS = Path("/var/lib/opkg/status")
CURRENT_IMAGE_STATUS = Path("/var/lib/opkg/status.image")
CURRENT_VERSION_MANIFEST = Path("/var/lib/calculinux/version-manifest.env")

# State directory for calculinux-update
STATE_DIR = Path("/var/lib/calculinux-update")
LOCK_FILE = STATE_DIR / ".lock"

# Update state files (new locations in /var/lib/calculinux-update/)
PENDING_DUPLICATES_FILE = STATE_DIR / "update-state.pending-duplicates"
PENDING_REINSTALL_FILE = STATE_DIR / "update-state.pending-reinstalls"
PENDING_UPGRADE_FILE = STATE_DIR / "update-state.pending-upgrades"
MODIFIED_CONFFILES_FILE = STATE_DIR / "update-state.modified-conffiles"
PRE_UPDATE_WRITABLE_STATUS = STATE_DIR / "update-state.pre-update-writable"
PRE_UPDATE_SLOT_NAME = STATE_DIR / "update-state.pre-update-slot"
UPDATED_SLOT_NAME = STATE_DIR / "update-state.updated-slot"
UPDATE_BOOT_ID = STATE_DIR / "update-state.boot-id"
STATUS_PRUNED_MARKER = STATE_DIR / "status-pruned"

# Cache directory
PREFETCH_CACHE_DIR = Path("/var/cache/calculinux-update/prefetch")


@contextmanager
def _state_lock():
    """
    Acquire exclusive lock on state directory to prevent concurrent operations.

    This ensures that only one update/rollback operation can manipulate state
    files at a time, preventing race conditions.
    """
    _ensure_state_dir()
    lock_fd = None
    # Derive from STATE_DIR so test/runtime overrides stay consistent.
    lock_path = STATE_DIR / ".lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        LOG.debug("acquired state lock")
        yield
    except (OSError, IOError) as e:
        LOG.error("failed to acquire state lock: %s", e)
        raise SystemExit(1) from e
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                LOG.debug("released state lock")
            except (OSError, IOError):
                pass


def _atomic_write(path: Path, content: str) -> None:
    """
    Write content to file atomically using tempfile + rename.

    This prevents partial writes from being visible if the process is
    interrupted or the system loses power.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        # In test environments or restricted permissions, continue anyway
        # The subsequent operations will fail if truly not writable
        pass

    # Create temp file in same directory to ensure same filesystem
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        fd = None
        # Atomic rename on POSIX systems
        Path(temp_path).replace(path)
        LOG.debug("atomically wrote %s", path)
    except (OSError, IOError) as e:
        LOG.error("failed to write %s: %s", path, e)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        # Clean up temp file if rename failed
        try:
            Path(temp_path).unlink()
        except FileNotFoundError:
            pass


def _ensure_state_dir() -> None:
    """Ensure the state directory exists (lazy initialization)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        # In test environments or when running without permissions,
        # this will fail. That's OK - the tests will mock the paths anyway.
        LOG.debug("could not create state directory: %s", e)


def _get_booted_slot_name() -> Optional[str]:
    """Get the name of the currently booted slot from RAUC."""
    try:
        result = subprocess.run(
            ["rauc", "status", "--output-format=shell"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse output for RAUC_SLOT_STATE_<slot>=booted
        for line in result.stdout.splitlines():
            if "RAUC_SLOT_STATE_" in line and line.endswith("=booted"):
                # Extract slot name from RAUC_SLOT_STATE_rootfs.0=booted
                slot_var = line.split("=")[0]
                slot_name = slot_var.replace("RAUC_SLOT_STATE_", "")
                return slot_name
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        LOG.warning("failed to get booted slot: %s", e)
    return None


def _get_current_boot_id() -> Optional[str]:
    """Get the current boot ID from /proc/sys/kernel/random/boot_id."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IOError) as e:
        LOG.warning("failed to read boot ID: %s", e)
        return None


def _cleanup_update_state() -> None:
    """Remove all update state files after successful processing."""
    state_files = [
        PRE_UPDATE_WRITABLE_STATUS,
        PRE_UPDATE_SLOT_NAME,
        UPDATED_SLOT_NAME,
        UPDATE_BOOT_ID,
        PENDING_DUPLICATES_FILE,
        PENDING_REINSTALL_FILE,
        PENDING_UPGRADE_FILE,
        STATUS_PRUNED_MARKER,  # Clear pruned marker on new update
    ]

    for path in state_files:
        path.unlink(missing_ok=True)

    LOG.debug("cleaned up update state files")


def _save_pre_update_state(updated_slot: str) -> None:
    """Save current state before update for rollback detection."""
    _ensure_state_dir()

    # Clear status-pruned marker to ensure post-reboot service runs
    # Even if there are no pending operations, we need to prune writable status
    STATUS_PRUNED_MARKER.unlink(missing_ok=True)
    LOG.debug("cleared status-pruned marker for new update")

    # Critical: Record which slot we're updating to
    try:
        _atomic_write(UPDATED_SLOT_NAME, updated_slot + "\n")
        LOG.info("recorded updated slot: %s", updated_slot)
    except (OSError, IOError) as e:
        LOG.error("failed to save updated slot (critical): %s", e)
        raise

    # Critical: Record which slot we're currently booted from
    current_slot = _get_booted_slot_name()
    if current_slot:
        try:
            _atomic_write(PRE_UPDATE_SLOT_NAME, current_slot + "\n")
            LOG.info("recorded pre-update slot: %s", current_slot)
        except (OSError, IOError) as e:
            LOG.error("failed to save pre-update slot (critical): %s", e)
            raise
    else:
        LOG.warning("cannot determine current slot - rollback detection may be impaired")

    # Best-effort: Save current writable status for package-level rollback
    if WRITABLE_STATUS.exists():
        try:
            entries = load_status_entries(WRITABLE_STATUS)
            # Use atomic write via temp file
            temp_status = PRE_UPDATE_WRITABLE_STATUS.with_suffix(".tmp")
            write_status_entries(temp_status, entries)
            temp_status.replace(PRE_UPDATE_WRITABLE_STATUS)
            LOG.info("saved pre-update writable status (%d packages)", len(entries))
        except (OSError, IOError) as e:
            LOG.warning("failed to save pre-update status: %s", e)
            # Non-critical: slot comparison will still work


def _detect_rollback() -> Dict[str, any]:
    """
    Detect if we've rolled back instead of moving forward.

    Returns dict with 'is_rollback' (bool) and 'reason' (str).
    """
    # Check if we have the necessary state files
    if not PRE_UPDATE_SLOT_NAME.exists() or not UPDATED_SLOT_NAME.exists():
        return {"is_rollback": False, "reason": "no update state found"}

    # Check boot ID to avoid re-processing same boot
    current_boot_id = _get_current_boot_id()
    if current_boot_id and UPDATE_BOOT_ID.exists():
        saved_boot_id = UPDATE_BOOT_ID.read_text().strip()
        if current_boot_id == saved_boot_id:
            LOG.debug("already processed this boot, cleaning up state")
            _cleanup_update_state()
            return {"is_rollback": False, "reason": "already processed this boot"}

    # Get slot names
    try:
        pre_update_slot = PRE_UPDATE_SLOT_NAME.read_text().strip()
        updated_slot = UPDATED_SLOT_NAME.read_text().strip()
        booted_slot = _get_booted_slot_name()

        if not booted_slot:
            return {"is_rollback": False, "reason": "cannot determine booted slot"}

        LOG.debug(
            "slot comparison: pre=%s, updated=%s, booted=%s",
            pre_update_slot, updated_slot, booted_slot
        )

        # Primary detection: slot name comparison
        if booted_slot == updated_slot:
            # Forward update: booted into the updated slot
            return {"is_rollback": False, "reason": "forward update (booted into updated slot)"}
        elif booted_slot == pre_update_slot:
            # Rollback: booted back into the pre-update slot
            return {
                "is_rollback": True,
                "reason": (
                    f"rollback detected (booted {booted_slot} == "
                    f"pre-update {pre_update_slot})"
                ),
            }
        else:
            # Ambiguous: booted into a different slot entirely
            # Fall back to package comparison
            if PRE_UPDATE_WRITABLE_STATUS.exists():
                saved_packages = {
                    e.name for e in load_status_entries(PRE_UPDATE_WRITABLE_STATUS)
                }
                current_packages = {
                    e.name for e in load_status_entries(WRITABLE_STATUS)
                }
                missing = saved_packages - current_packages

                if missing:
                    LOG.warning(
                        "ambiguous slot state but %d packages missing, treating as rollback",
                        len(missing)
                    )
                    return {
                        "is_rollback": True,
                        "reason": f"package comparison ({len(missing)} packages missing)"
                    }

            return {
                "is_rollback": False,
                "reason": (
                    f"ambiguous slot (booted {booted_slot}, expected "
                    f"{updated_slot} or {pre_update_slot})"
                ),
            }

    except (OSError, IOError) as e:
        LOG.warning("error during rollback detection: %s", e)
        return {"is_rollback": False, "reason": f"error: {e}"}


def _handle_rollback() -> bool:
    """
    Handle rollback by restoring pre-update package state.

    Returns True if successful, False otherwise.
    """
    if not PRE_UPDATE_WRITABLE_STATUS.exists():
        LOG.warning("cannot restore: pre-update status not found")
        return False

    try:
        # Load saved pre-update state
        pre_update_entries = load_status_entries(PRE_UPDATE_WRITABLE_STATUS)
        LOG.info("restoring pre-update state (%d packages)", len(pre_update_entries))

        # Write to temp file first for atomicity
        temp_status = WRITABLE_STATUS.with_suffix(".rollback.tmp")
        write_status_entries(temp_status, pre_update_entries)

        # Atomic replace
        temp_status.replace(WRITABLE_STATUS)
        LOG.info("restored pre-update package state")

        # Only clean up after successful restore
        _cleanup_update_state()
        LOG.info("rollback handling complete")

        return True

    except (OSError, IOError) as e:
        LOG.error("failed to restore pre-update state: %s", e)
        # Don't clean up state files on failure - leave for debugging/retry
        return False


def hook_entrypoint() -> None:
    """RAUC hook entry point - requires root."""
    if os.geteuid() != 0:
        LOG.error("hook must run as root")
        raise SystemExit(1)

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

    # Get the status.image from bundle extras (provided by post-install-handler.sh)
    bundle_status_image = os.environ.get("RAUC_BUNDLE_STATUS_IMAGE")
    if not bundle_status_image:
        LOG.warning("RAUC_BUNDLE_STATUS_IMAGE not provided for slot %s", slot)
        return

    image_status = Path(bundle_status_image)
    if not image_status.exists():
        LOG.warning("bundle status image %s missing", image_status)
        return

    if not WRITABLE_STATUS.exists():
        LOG.warning("writable status %s missing", WRITABLE_STATUS)
        return

    bundle_mount = os.environ.get("RAUC_BUNDLE_MOUNT_POINT")
    bundle_manifest = (
        Path(bundle_mount) / "extras/version-manifest.env" if bundle_mount else None
    )
    if CURRENT_VERSION_MANIFEST.exists() and bundle_manifest and bundle_manifest.exists():
        try:
            old_manifest = load_version_manifest(CURRENT_VERSION_MANIFEST)
            new_manifest = load_version_manifest(bundle_manifest)
            if old_manifest and new_manifest:
                report = check_compatibility(old_manifest, new_manifest)
                for issue in report.issues:
                    LOG.info("[%s] %s: %s", issue.level.name, issue.category, issue.message)
                    if issue.recommendation:
                        LOG.info("  -> %s", issue.recommendation)
                if report.any_blockers():
                    LOG.error("update blocked by compatibility check")
                    raise SystemExit(1)
        except Exception as e:
            LOG.warning("version compatibility check failed: %s", e)

    # Save pre-update state for rollback detection
    _save_pre_update_state(slot)

    # The current slot must have a status.image file
    # All Calculinux images include this file
    if not CURRENT_IMAGE_STATUS.exists():
        LOG.error("current image status %s missing - image too old?", CURRENT_IMAGE_STATUS)
        raise SystemExit(1)

    # Plan first while writable status still lists overlay copies of
    # packages that are also in the new image. Pruning those entries
    # before planning makes every duplicate look like it vanished.
    plan = compute_reconcile_plan(
        image_status=image_status,
        writable_status=WRITABLE_STATUS,
        current_status=CURRENT_IMAGE_STATUS,
    )

    # Phase 1: Remove packages from status file that have no files in upper layer
    # This is safe to do before reboot since there are no physical files to remove
    if plan.status_only_duplicates:
        LOG.info(
            "pruning %d status-only duplicate(s) (no files in upper layer)",
            len(plan.status_only_duplicates)
        )
        _prune_status_only_duplicates(plan.status_only_duplicates)

    # Phase 2: Physical duplicate removal and overlay upgrades happen after reboot
    _write_pending(PENDING_DUPLICATES_FILE, plan.duplicates, "duplicate removal")
    _write_pending(PENDING_REINSTALL_FILE, plan.reinstall, "reinstall")
    _write_pending(PENDING_UPGRADE_FILE, plan.upgrade, "upgrade")

    # Conffile .dpkg-new files are created post-reboot from the new lower layer.

    # Marker is informational; systemd starts post-reboot from pending-* files.
    try:
        _atomic_write(STATUS_PRUNED_MARKER, "pruned\n")
        LOG.info("marked status as pruned for new image")
    except (OSError, IOError) as e:
        LOG.warning("failed to mark status as pruned: %s", e)


def postreboot_entrypoint() -> None:
    """Post-reboot reconciliation entry point - requires root."""
    if os.geteuid() != 0:
        LOG.error("post-reboot service must run as root")
        raise SystemExit(1)

    # Use locking to prevent concurrent operations
    with _state_lock():
        # Check for rollback first
        rollback_info = _detect_rollback()
        if rollback_info["is_rollback"]:
            LOG.info("rollback detected: %s", rollback_info["reason"])
            if _handle_rollback():
                LOG.info("rollback handling complete")
                return
            else:
                LOG.error("rollback handling failed")
                raise SystemExit(1)

        # Not a rollback - proceed with forward update processing
        has_pending = (
            PENDING_DUPLICATES_FILE.exists() or
            PENDING_REINSTALL_FILE.exists() or
            PENDING_UPGRADE_FILE.exists()
        )

        if has_pending:
            if not _run_opkg(["update"]):
                LOG.error("opkg update failed; will retry next boot")
                raise SystemExit(1)

            # Phase 2: Physical duplicates / overlay upgrades after reboot
            duplicates_status = _process_pending(PENDING_DUPLICATES_FILE, _remove_duplicate_pkg)
            reinstall_status = _process_pending(PENDING_REINSTALL_FILE, _install_reinstall_pkg)
            upgrade_status = _process_pending(PENDING_UPGRADE_FILE, _upgrade_pkg)
            if not (duplicates_status and reinstall_status and upgrade_status):
                LOG.error("post-reboot reconciliation incomplete; will retry")
                raise SystemExit(1)
            LOG.info("post-reboot package reconciliation complete")
        else:
            LOG.info("no pending package operations")

        # Conffiles and cleanup run even when the update queued no packages
        _create_new_conffiles_from_lower()
        _report_modified_conffiles()

        current_boot_id = _get_current_boot_id()
        if current_boot_id:
            try:
                _ensure_state_dir()
                _atomic_write(UPDATE_BOOT_ID, current_boot_id + "\n")
            except (OSError, IOError) as e:
                LOG.warning("failed to save boot ID: %s", e)

        for path in [
            PRE_UPDATE_WRITABLE_STATUS,
            PRE_UPDATE_SLOT_NAME,
            UPDATED_SLOT_NAME,
            MODIFIED_CONFFILES_FILE,
        ]:
            path.unlink(missing_ok=True)


def _prune_writable_status(image_status: Path) -> None:
    changed = prune_writable_status(WRITABLE_STATUS, load_package_names(image_status))
    if changed:
        LOG.info("pruned writable status against new image")


def _prune_status_only_duplicates(packages: List[str]) -> None:
    """Remove packages from writable status that have no files in upper layer.

    This is Phase 1 of duplicate handling - safe to do before reboot since
    there are no physical files to remove, only status metadata cleanup.
    """
    if not packages:
        return

    changed = prune_writable_status(WRITABLE_STATUS, packages)
    if changed:
        LOG.info("pruned %d status-only duplicate(s) from writable status", len(packages))


def _create_new_conffiles_from_lower() -> None:
    """Create .dpkg-new files from the new image lower layer after reboot."""
    if not CURRENT_IMAGE_STATUS.exists():
        return
    try:
        image_packages = load_package_names(CURRENT_IMAGE_STATUS)
        modified_conffiles = detect_modified_conffiles(list(image_packages))
        if not modified_conffiles:
            return
        LOG.info("detected %d modified config file(s)", len(modified_conffiles))
        created_files = create_dpkg_new_files(modified_conffiles)
        if created_files:
            LOG.info("created %d .dpkg-new file(s) for modified configs", len(created_files))
            conffile_data = "\n".join(
                f"{cf.path}\t{cf.package}" for cf in modified_conffiles
            )
            _atomic_write(MODIFIED_CONFFILES_FILE, conffile_data + "\n")
    except Exception as e:
        LOG.warning("failed to create .dpkg-new files: %s", e)


def _report_modified_conffiles() -> None:
    """Report modified config files to the user.

    Reads the list of modified conffiles created during the update and logs them
    along with their corresponding .dpkg-new file locations.
    """
    if not MODIFIED_CONFFILES_FILE.exists():
        return

    try:
        with open(MODIFIED_CONFFILES_FILE, 'r') as f:
            lines = [line.strip() for line in f if line.strip()]

        if not lines:
            return

        LOG.info("=== Modified Configuration Files ===")
        LOG.info("The following config files were modified and have new versions available:")
        LOG.info("")

        for line in lines:
            parts = line.split('\t', 1)
            if len(parts) == 2:
                conf_path, package = parts
                dpkg_new = conf_path + '.dpkg-new'
                LOG.info("  %s (from package: %s)", conf_path, package)
                LOG.info("    New version saved as: %s", dpkg_new)

        LOG.info("")
        LOG.info("To apply the new versions, compare and merge the changes:")
        LOG.info("  diff <original-file> <original-file>.dpkg-new")
        LOG.info("  mv <original-file>.dpkg-new <original-file>  # to accept new version")
        LOG.info("=================================")

    except (OSError, IOError) as e:
        LOG.warning("failed to read modified conffiles list: %s", e)


def _remove_duplicates(duplicates: Iterable[str]) -> None:
    removed_packages = []
    package_files_map = {}

    # Get file lists BEFORE removal since opkg remove will delete the package info
    for pkg in duplicates:
        files = get_package_files(pkg)
        if files:
            package_files_map[pkg] = files

    for pkg in duplicates:
        LOG.info("removing duplicate package %s", pkg)
        result = subprocess.run(
            ["opkg", "remove", "--nodeps", pkg],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            LOG.warning("failed to remove %s: %s", pkg, result.stderr.strip())
        else:
            removed_packages.append(pkg)

    # Restore lower layer files for successfully removed packages
    # This ensures files from the base image become visible again
    if removed_packages:
        LOG.info(
            "restoring lower layer files for %d removed packages", len(removed_packages)
        )
        try:
            files_restored = restore_files_for_packages(
                removed_packages, file_lists=package_files_map
            )
            if files_restored > 0:
                LOG.info(
                    "restored %d file(s) from base image",
                    files_restored,
                )
        except Exception as e:
            LOG.warning("error during file restoration: %s", e)


def _write_pending(path: Path, packages: List[str], label: str) -> None:
    if not packages:
        path.unlink(missing_ok=True)
        LOG.info("no packages require %s", label)
        return

    _ensure_state_dir()
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


def _remove_duplicate_pkg(pkg: str) -> bool:
    """Remove a single duplicate package (Phase 2 - packages with files in upper).

    This physically removes the package and cleans up any OverlayFS whiteouts.
    """
    # Get file list BEFORE removal since opkg remove will delete the package info
    file_list = get_package_files(pkg)

    LOG.info("removing duplicate package %s from upper layer", pkg)
    result = subprocess.run(
        ["opkg", "remove", "--nodeps", pkg],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        LOG.warning("failed to remove %s: %s", pkg, result.stderr.strip())
        return False

    # Restore lower layer files for the removed package
    try:
        file_lists = {pkg: file_list} if file_list else {}
        files_restored = restore_files_for_packages([pkg], file_lists=file_lists)
        if files_restored > 0:
            LOG.info(
                "restored %d file(s) from base image for %s",
                files_restored,
                pkg,
            )
    except Exception as e:
        LOG.warning("error during file restoration for %s: %s", pkg, e)

    return True


def _record_leftover(pkg: str, reason: str) -> None:
    path = STATE_DIR / "update-state.leftovers"
    try:
        _ensure_state_dir()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{pkg}\t{reason}\n")
    except (OSError, IOError) as e:
        LOG.warning("failed to record leftover %s: %s", pkg, e)


def _upgrade_pkg(pkg: str) -> bool:
    result = _run_opkg(["upgrade", pkg])
    if not result:
        LOG.warning("failed to upgrade %s; leaving installed as-is", pkg)
        _record_leftover(pkg, "upgrade")
    return True


def _run_opkg(args: List[str]) -> bool:
    result = subprocess.run(
        ["opkg", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        LOG.warning("opkg %s failed: %s", " ".join(args), result.stderr.strip())
        return False
    return True


def _find_cached_package(pkg: str) -> Optional[Path]:
    if not PREFETCH_CACHE_DIR.exists():
        return None
    candidates = sorted(
        PREFETCH_CACHE_DIR.glob(f"{pkg}_*.ipk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
