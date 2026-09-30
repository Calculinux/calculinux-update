"""Entry points for RAUC hooks and post-reboot package reconciliation."""

from __future__ import annotations

import argparse
import configparser
import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .opkg.conffiles import create_dpkg_new_files, detect_modified_conffiles
from .opkg.overlayfs import (
    OverlayIoctlUnsupported,
    get_package_files,
    restore_files_for_packages,
)
from .opkg.reconcile import (
    compute_reconcile_plan,
    prune_writable_status,
)
from .opkg.status import load_package_names, load_status_entries, write_status_entries
from .prefetch import (
    PREFETCH_CACHE_DIR,
    PREFETCH_LISTS_DIR,
    file_sha256,
)
from .prefetch import load_state as load_prefetch_state
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

# RAUC / boot information
RAUC_SYSTEM_CONF = Path("/etc/rauc/system.conf")
PROC_CMDLINE = Path("/proc/cmdline")

# State directory for calculinux-update
STATE_DIR = Path("/var/lib/calculinux-update")
LOCK_FILE = STATE_DIR / ".lock"

# Update state files (new locations in /var/lib/calculinux-update/)
PENDING_DUPLICATES_FILE = STATE_DIR / "update-state.pending-duplicates"
PENDING_REINSTALL_FILE = STATE_DIR / "update-state.pending-reinstalls"
# Written by calculinux-update < 0.8 (every overlay package, any update)
LEGACY_PENDING_UPGRADE_FILE = STATE_DIR / "update-state.pending-upgrades"
LEFTOVERS_FILE = STATE_DIR / "update-state.leftovers"
OPKG_LISTS_DIR = Path("/var/lib/opkg/lists")
MODIFIED_CONFFILES_FILE = STATE_DIR / "update-state.modified-conffiles"
PRE_UPDATE_WRITABLE_STATUS = STATE_DIR / "update-state.pre-update-writable"
PRE_UPDATE_SLOT_NAME = STATE_DIR / "update-state.pre-update-slot"
UPDATED_SLOT_NAME = STATE_DIR / "update-state.updated-slot"
UPDATE_BOOT_ID = STATE_DIR / "update-state.boot-id"
STATUS_PRUNED_MARKER = STATE_DIR / "status-pruned"



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


def _booted_bootname() -> Optional[str]:
    """Return the booted slot's bootname without talking to the RAUC service.

    RAUC exports RAUC_CURRENT_BOOTNAME to handlers and hooks. Outside of RAUC,
    fall back to the rauc.slot= argument the bootloader puts on the cmdline.
    """
    bootname = os.environ.get("RAUC_CURRENT_BOOTNAME")
    if bootname:
        return bootname
    try:
        cmdline = PROC_CMDLINE.read_text()
    except OSError:
        return None
    for arg in cmdline.split():
        if arg.startswith("rauc.slot="):
            return arg.split("=", 1)[1] or None
    return None


def _slot_name_for_bootname(bootname: str) -> Optional[str]:
    """Map a bootname (e.g. 'A') to its slot name (e.g. 'rootfs.0') via system.conf."""
    conf_path = Path(os.environ.get("RAUC_SYSTEM_CONFIG") or RAUC_SYSTEM_CONF)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if not parser.read(conf_path):
            return None
    except configparser.Error as e:
        LOG.warning("failed to parse %s: %s", conf_path, e)
        return None
    for section in parser.sections():
        if not section.startswith("slot."):
            continue
        slot_name = section[len("slot."):]
        if parser.get(section, "bootname", fallback=None) == bootname or slot_name == bootname:
            return slot_name
    return None


def _booted_slot_from_rauc_status() -> Optional[str]:
    """Ask the RAUC service for the booted slot.

    This fails while an install is in progress (the service rejects status
    queries while busy), so it is only a fallback for use outside of hooks.
    """
    result = subprocess.run(
        ["rauc", "status", "--output-format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    for entry in json.loads(result.stdout).get("slots", []):
        for slot_name, info in entry.items():
            if info.get("state") == "booted":
                return slot_name
    return None


def _get_booted_slot_name() -> Optional[str]:
    """Get the name of the currently booted slot (e.g. 'rootfs.1')."""
    bootname = _booted_bootname()
    if bootname:
        slot_name = _slot_name_for_bootname(bootname)
        if slot_name:
            return slot_name
    try:
        return _booted_slot_from_rauc_status()
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, AttributeError) as e:
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
        LEGACY_PENDING_UPGRADE_FILE,
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
    # Leftovers belong to the previous update
    LEFTOVERS_FILE.unlink(missing_ok=True)

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

    old_manifest = load_version_manifest(CURRENT_VERSION_MANIFEST)
    new_manifest = load_version_manifest(_bundle_manifest_path())
    # Informational only: RAUC has already marked the slot active by the time
    # this post-install handler runs, so failing here would not stop the
    # update. The bundle's install-check hook is what refuses an install.
    if old_manifest and new_manifest:
        report = check_compatibility(old_manifest, new_manifest)
        for issue in report.issues:
            LOG.info("[%s] %s: %s", issue.level.name, issue.category, issue.message)

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
        old_manifest=old_manifest,
        new_manifest=new_manifest,
    )

    # Entries with no files in the upper layer only need their status entry
    # dropped, which is safe before the reboot: duplicates the new image
    # provides, and image packages opkg leaked into the writable status.
    prune = plan.status_only_duplicates + plan.leaked
    if prune:
        LOG.info(
            "pruning %d status-only duplicate(s) and %d leaked image entr(ies)",
            len(plan.status_only_duplicates), len(plan.leaked),
        )
        prune_writable_status(WRITABLE_STATUS, prune)

    if plan.release_change:
        LOG.info("release change: %d overlay package(s) will be reinstalled", len(plan.reinstall))
    elif plan.reinstall:
        LOG.info(
            "kernel %s: %d overlay kernel module package(s) will be reinstalled",
            plan.kernel_abi, len(plan.reinstall),
        )
    else:
        LOG.info("same release and kernel: %d overlay package(s) stay as installed",
                 len(plan.overlay))

    # Overlay files of duplicates are removed and reinstalls happen after reboot
    _write_pending(PENDING_DUPLICATES_FILE, plan.duplicates, "duplicate removal")
    _write_pending(PENDING_REINSTALL_FILE, plan.reinstall, "reinstall")
    LEGACY_PENDING_UPGRADE_FILE.unlink(missing_ok=True)

    # Marker is informational; systemd starts post-reboot from pending-* files.
    try:
        _atomic_write(STATUS_PRUNED_MARKER, "pruned\n")
    except (OSError, IOError) as e:
        LOG.warning("failed to mark status as pruned: %s", e)


def _bundle_manifest_path() -> Path:
    explicit = os.environ.get("RAUC_BUNDLE_VERSION_MANIFEST")
    if explicit:
        return Path(explicit)
    # Older post-install handlers point RAUC_BUNDLE_MOUNT_POINT at the
    # unpacked extras instead.
    return Path(os.environ.get("RAUC_BUNDLE_MOUNT_POINT", "/nonexistent")) / (
        "extras/version-manifest.env"
    )


SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")


def _cleanup_retired_units() -> None:
    """Drop enablement left behind by images that shipped cup-reconcile.timer.

    /etc survives slot swaps, so an image that enabled the retired timer
    would otherwise leave dangling symlinks (systemd warns at every boot).
    """
    for unit in ("cup-reconcile.timer", "cup-reconcile.service"):
        for path in (
            SYSTEMD_SYSTEM_DIR / "multi-user.target.wants" / unit,
            SYSTEMD_SYSTEM_DIR / unit,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def postreboot_entrypoint() -> None:
    """Post-reboot reconciliation entry point - requires root."""
    if os.geteuid() != 0:
        LOG.error("post-reboot service must run as root")
        raise SystemExit(1)

    with _state_lock():
        _cleanup_retired_units()
        rollback_info = _detect_rollback()
        if rollback_info["is_rollback"]:
            LOG.info("rollback detected: %s", rollback_info["reason"])
            if _handle_rollback():
                LOG.info("rollback handling complete")
                return
            LOG.error("rollback handling failed")
            raise SystemExit(1)

        if LEGACY_PENDING_UPGRADE_FILE.exists():
            # Older calculinux-update queued every overlay package for an
            # upgrade from the feed after any update. Same-release overlay
            # packages keep working, so there is nothing to do for them.
            LOG.info("dropping upgrade queue from an older calculinux-update")
            LEGACY_PENDING_UPGRADE_FILE.unlink(missing_ok=True)

        # Local work only: prefetched reinstalls, duplicates, conffiles.
        # Never touch the feed here - leftovers wait for 'cup reconcile'.
        result = reconcile_pending(allow_network=False)

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

    if result.waiting:
        LOG.info(
            "%d package(s) still need to be reinstalled once the network is up; "
            "the login notice asks users to run 'cup reconcile'",
            len(result.waiting),
        )


@dataclass(slots=True)
class ReconcileResult:
    removed: List[str] = field(default_factory=list)
    reinstalled: List[str] = field(default_factory=list)
    waiting: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def pending_reinstalls() -> List[str]:
    return _read_pending(PENDING_REINSTALL_FILE)


def read_leftovers() -> List[Tuple[str, str]]:
    try:
        lines = LEFTOVERS_FILE.read_text().splitlines()
    except OSError:
        return []
    return [tuple(line.split("\t", 1)) for line in lines if "\t" in line]  # type: ignore[misc]


def reconcile_pending(allow_network: bool = True) -> ReconcileResult:
    """Carry out queued duplicate removals and reinstalls.

    Local work comes first and never needs the network. Reinstalls use the
    prefetch cache when it was made for the running image; otherwise the feed
    when ``opkg update`` works. Whatever cannot be installed stays queued for
    ``cup reconcile``, which users are pointed to at login.
    """
    result = ReconcileResult()

    duplicates = _read_pending(PENDING_DUPLICATES_FILE)
    if duplicates:
        result.removed, failed = _remove_duplicate_pkgs(duplicates)
        _write_pending(PENDING_DUPLICATES_FILE, failed, "duplicate removal")
        result.failed.extend(failed)

    queue = _read_pending(PENDING_REINSTALL_FILE)
    if not queue:
        return result

    cache_dir = None
    prefetched = _install_prefetched_lists()
    if prefetched:
        cache_dir = PREFETCH_CACHE_DIR
        LOG.info("reinstalling %d package(s) from the prefetch cache", len(queue))
    online = allow_network and _run_opkg(["update"])
    if not prefetched and not online:
        LOG.info("%d reinstall(s) wait for the feed", len(queue))
        result.waiting = queue
        return result

    done, failed = _reinstall_pkgs(queue, cache_dir)
    result.reinstalled = done
    for pkg in failed:
        if online:
            _record_leftover(pkg, "reinstall")
            result.failed.append(pkg)
        else:
            result.waiting.append(pkg)
    _write_pending(PENDING_REINSTALL_FILE, result.waiting, "reinstall")
    return result


def _install_prefetched_lists() -> bool:
    """Use the prefetched feed lists if they were made for the running image."""
    state = load_prefetch_state()
    if not state or not PREFETCH_LISTS_DIR.is_dir() or not CURRENT_IMAGE_STATUS.exists():
        return False
    if state.get("image_status_sha256") != file_sha256(CURRENT_IMAGE_STATUS):
        LOG.info("prefetch cache is for a different image; ignoring it")
        return False
    OPKG_LISTS_DIR.mkdir(parents=True, exist_ok=True)
    for lst in PREFETCH_LISTS_DIR.iterdir():
        if lst.is_file():
            shutil.copyfile(lst, OPKG_LISTS_DIR / lst.name)
    return True


def _reinstall_pkgs(
    packages: List[str], cache_dir: Optional[Path]
) -> Tuple[List[str], List[str]]:
    base = ["--cache-dir", str(cache_dir)] if cache_dir else []
    if _run_opkg([*base, "install", "--force-reinstall", *packages]):
        return list(packages), []
    done, failed = [], []
    for pkg in packages:
        if _run_opkg([*base, "install", "--force-reinstall", pkg]):
            done.append(pkg)
        else:
            failed.append(pkg)
    return done, failed


def _remove_duplicate_pkgs(packages: List[str]) -> Tuple[List[str], List[str]]:
    """``opkg remove`` the overlay copies, then restore the image's files."""
    # File lists must be read before opkg deletes them
    file_lists = {pkg: get_package_files(pkg) for pkg in packages}
    removed: List[str] = []
    failed: List[str] = []
    if _run_opkg(["remove", "--nodeps", *packages]):
        removed = list(packages)
    else:
        for pkg in packages:
            (removed if _run_opkg(["remove", "--nodeps", pkg]) else failed).append(pkg)
    if removed:
        try:
            restored = restore_files_for_packages(
                removed, file_lists={pkg: file_lists[pkg] for pkg in removed}
            )
            LOG.info(
                "removed %d overlay duplicate(s); restored %d image file(s)",
                len(removed), restored,
            )
        except OverlayIoctlUnsupported:
            raise
        except Exception as e:
            LOG.warning("error during file restoration: %s", e)
    return removed, failed


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


def _read_pending(path: Path) -> List[str]:
    try:
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def _write_pending(path: Path, packages: List[str], label: str) -> None:
    if not packages:
        path.unlink(missing_ok=True)
        LOG.info("no packages require %s", label)
        return

    _ensure_state_dir()
    path.write_text("\n".join(packages) + "\n")
    LOG.info("queued %d packages for %s", len(packages), label)


def _record_leftover(pkg: str, reason: str) -> None:
    try:
        _ensure_state_dir()
        with LEFTOVERS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{pkg}\t{reason}\n")
    except (OSError, IOError) as e:
        LOG.warning("failed to record leftover %s: %s", pkg, e)


def _run_opkg(args: List[str]) -> bool:
    result = subprocess.run(
        ["opkg", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        LOG.warning("opkg %s failed: %s", " ".join(args), result.stderr.strip())
        return False
    return True
