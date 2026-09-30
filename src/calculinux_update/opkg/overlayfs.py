"""Overlay upper-layer queries and whiteout restore for package reconciliation.

Calculinux keeps packages the user installs in the overlayfs upper layer on
top of a read-only base image. When an update moves such a package into the
image, its overlay copy is removed with ``opkg remove``. overlayfs then leaves
a whiteout for every removed file that also exists in the lower layer, which
hides the image's new copy. The whiteouts are removed again here.

Both the "does this package have files in the upper layer" query and the
restore use the ioctls of Calculinux's overlayfs module
(https://github.com/Calculinux/overlayfs, include/uapi/linux/overlayfs.h):

- ``OVL_IOC_UPPER_STATE`` reports whether the upper layer holds nothing, a
  whiteout or a real entry for a path;
- ``OVL_IOC_RESTORE_LOWER`` removes a whiteout so the lower entry shows again.

The ioctls are issued on the overlay's mount point, opened as a directory.
A kernel without them (``ENOTTY``) raises :class:`OverlayIoctlUnsupported`;
there is deliberately no userspace fallback.
"""

from __future__ import annotations

import array
import errno
import fcntl
import logging
import os
import stat
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .status import load_package_names

LOGGER = logging.getLogger(__name__)

__all__ = [
    "OVL_IOC_IS_RESTORABLE",
    "OVL_IOC_RESTORE_LOWER",
    "OVL_IOC_UPPER_STATE",
    "OverlayInfo",
    "OverlayIoctlUnsupported",
    "UpperInfo",
    "UpperState",
    "find_restorable_files",
    "get_package_entries",
    "get_package_files",
    "has_files_in_upper",
    "restore_files_for_packages",
    "restore_opkg_metadata",
    "restore_package_files",
]

INFO_DIR = Path("/var/lib/opkg/info")
WRITABLE_STATUS = Path("/var/lib/opkg/status")
MOUNTINFO = Path("/proc/self/mountinfo")

METADATA_SUFFIXES = (
    ".list", ".control", ".conffiles", ".preinst", ".postinst", ".prerm", ".postrm",
)


# --- ioctl ABI (include/uapi/linux/overlayfs.h) -----------------------------

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


# The image's Python has no ctypes, so the argument structs are packed with
# struct and the path's address comes from an array buffer.

# struct ovl_restore_lower_args / ovl_is_restorable_args:
#   __aligned_u64 path_ptr; __u32 path_len; __u32 flags;
_PATH_ARGS = struct.Struct("=QII")
# struct ovl_upper_state_args:
#   __aligned_u64 path_ptr; __u32 path_len, flags, state, state_flags, mode, pad;
_UPPER_STATE_ARGS = struct.Struct("=QIIIIII")

OVL_IOC_RESTORE_LOWER = _ioc(_IOC_WRITE, "O", 1, _PATH_ARGS.size)
OVL_IOC_IS_RESTORABLE = _ioc(_IOC_WRITE, "O", 2, _PATH_ARGS.size)
OVL_IOC_UPPER_STATE = _ioc(_IOC_READ | _IOC_WRITE, "O", 3, _UPPER_STATE_ARGS.size)

_STATE_F_OPAQUE = 1 << 0
_STATE_F_NO_LOWER_DIR = 1 << 1


class UpperState(IntEnum):
    NONE = 0       # no upper entry
    WHITEOUT = 1   # upper entry is a whiteout
    UPPER = 2      # real upper entry


@dataclass(frozen=True)
class UpperInfo:
    state: UpperState
    mode: int = 0
    opaque: bool = False
    no_lower_dir: bool = False


class OverlayIoctlUnsupported(RuntimeError):
    """The running kernel's overlayfs lacks the Calculinux ioctls."""


def _path_buffer(path: str) -> Tuple[array.array, int, int]:
    """A NUL-terminated copy of ``path``, its address and its strlen().

    The array must stay referenced until the ioctl returns.
    """
    raw = path.encode()
    buf = array.array("B", raw + b"\0")
    return buf, buf.buffer_info()[0], len(raw)


class OverlayInfo:
    """Overlay mount points (read once) and one ioctl fd per mount."""

    def __init__(self, mountinfo: Path = MOUNTINFO) -> None:
        self._mounts = sorted(self._read_mounts(mountinfo), key=len, reverse=True)
        self._fds: Dict[str, int] = {}

    @staticmethod
    def _read_mounts(mountinfo: Path) -> List[str]:
        mounts = []
        try:
            for line in mountinfo.read_text().splitlines():
                left, sep, right = line.partition(" - ")
                if not sep:
                    continue
                if right.split(" ", 1)[0] == "overlay":
                    mounts.append(left.split()[4])
        except OSError as exc:
            LOGGER.warning("failed to read %s: %s", mountinfo, exc)
        return mounts

    def mount_point(self, path: str) -> Optional[str]:
        """The overlay mount that contains ``path``, or None."""
        for mount in self._mounts:
            if mount == "/" or path == mount or path.startswith(mount + "/"):
                return mount
        return None

    def _fd(self, mount: str) -> int:
        fd = self._fds.get(mount)
        if fd is None:
            fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
            self._fds[mount] = fd
        return fd

    def upper_state(self, path: str) -> UpperInfo:
        """What the upper layer holds for ``path`` (NONE when not on an overlay)."""
        mount = self.mount_point(path)
        if mount is None:
            return UpperInfo(UpperState.NONE)
        path_buf, address, length = _path_buffer(path)
        args = bytearray(_UPPER_STATE_ARGS.pack(address, length, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(self._fd(mount), OVL_IOC_UPPER_STATE, args)
        except OSError as exc:
            if exc.errno == errno.ENOTTY:
                raise OverlayIoctlUnsupported(
                    f"overlayfs on {mount} does not support OVL_IOC_UPPER_STATE"
                ) from exc
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EXDEV):
                # parent missing, or the path crosses into another filesystem
                return UpperInfo(UpperState.NONE)
            raise
        del path_buf
        _ptr, _len, _flags, state, state_flags, mode, _pad = _UPPER_STATE_ARGS.unpack(args)
        return UpperInfo(
            state=UpperState(state),
            mode=mode,
            opaque=bool(state_flags & _STATE_F_OPAQUE),
            no_lower_dir=bool(state_flags & _STATE_F_NO_LOWER_DIR),
        )

    def restore_lower(self, path: str) -> bool:
        """Remove the whiteout at ``path``; True once a lower entry is visible."""
        mount = self.mount_point(path)
        if mount is None:
            return False
        path_buf, address, length = _path_buffer(path)
        args = bytearray(_PATH_ARGS.pack(address, length, 0))
        try:
            fcntl.ioctl(self._fd(mount), OVL_IOC_RESTORE_LOWER, args)
        except OSError as exc:
            if exc.errno == errno.ENOTTY:
                raise OverlayIoctlUnsupported(
                    f"overlayfs on {mount} does not support OVL_IOC_RESTORE_LOWER"
                ) from exc
            if exc.errno == errno.ENODATA:
                LOGGER.debug("removed whiteout %s but nothing lower shows through", path)
            elif exc.errno not in (errno.ENOENT, errno.EEXIST):
                LOGGER.warning("failed to restore lower for %s: %s", path, exc)
            return False
        finally:
            del path_buf
        return True

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    def __enter__(self) -> "OverlayInfo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- package file lists -------------------------------------------------------


def get_package_entries(
    package_name: str, info_dir: Path = INFO_DIR
) -> List[Tuple[str, Optional[int]]]:
    """(path, mode) for every entry in the package's ``.list`` file.

    opkg records installed files in ``<info_dir>/<package>.list`` as
    ``path<TAB>mode<TAB>link`` (older files have only the path; mode is then
    None). ``opkg files`` reads the same file, so there is nothing to fall back
    to when it is missing.
    """
    list_file = Path(info_dir) / f"{package_name}.list"
    try:
        text = list_file.read_text(errors="replace")
    except OSError:
        LOGGER.debug("no file list for package %s", package_name)
        return []

    entries = []
    for line in text.splitlines():
        fields = line.split("\t")
        path = fields[0].strip()
        if not path:
            continue
        if not path.startswith("/"):
            path = "/" + path
        mode = None
        if len(fields) > 1 and fields[1].strip():
            try:
                mode = int(fields[1].strip(), 8)
            except ValueError:
                mode = None
        entries.append((path.rstrip("/") or "/", mode))
    return entries


def get_package_files(package_name: str, info_dir: Path = INFO_DIR) -> List[str]:
    """Absolute paths of the files (and directories) a package installed."""
    return [path for path, _mode in get_package_entries(package_name, info_dir)]


def has_files_in_upper(
    package_name: str,
    overlay: Optional[OverlayInfo] = None,
    info_dir: Path = INFO_DIR,
) -> bool:
    """True if any non-directory file of the package is a real upper-layer entry.

    Directories are skipped: parents such as /usr exist in the upper layer as
    soon as anything below them is written. When the state of a file cannot be
    determined, the package counts as having upper files, which leads to a real
    ``opkg remove`` rather than only dropping its status entry.
    """
    entries = get_package_entries(package_name, info_dir)
    if not entries:
        return False

    own = overlay is None
    overlay = overlay or OverlayInfo()
    try:
        for path, mode in entries:
            if mode is not None and stat.S_ISDIR(mode):
                continue
            try:
                info = overlay.upper_state(path)
            except OverlayIoctlUnsupported:
                raise
            except OSError as exc:
                LOGGER.warning(
                    "cannot tell whether %s (%s) is in the upper layer: %s",
                    path, package_name, exc,
                )
                return True
            if info.state == UpperState.UPPER and not stat.S_ISDIR(info.mode):
                LOGGER.debug("package %s has a real file in upper: %s", package_name, path)
                return True
        return False
    finally:
        if own:
            overlay.close()


# --- restore --------------------------------------------------------------------


def find_restorable_files(
    file_paths: Iterable[str], overlay: Optional[OverlayInfo] = None
) -> List[Path]:
    """The paths among ``file_paths`` that are whiteouts over a lower directory."""
    own = overlay is None
    overlay = overlay or OverlayInfo()
    try:
        restorable = []
        for file_path in file_paths:
            info = overlay.upper_state(str(file_path))
            if info.state == UpperState.WHITEOUT and not info.no_lower_dir:
                restorable.append(Path(file_path))
        return restorable
    finally:
        if own:
            overlay.close()


def is_package_in_writable_status(
    package_name: str, writable_status: Path = WRITABLE_STATUS
) -> bool:
    """Whether the overlay's own status file still lists the package."""
    try:
        return package_name in load_package_names(writable_status)
    except OSError as exc:
        LOGGER.warning("failed to read %s: %s", writable_status, exc)
        return False


def restore_package_files(
    package_name: str,
    dry_run: bool = False,
    file_list: Optional[List[str]] = None,
    overlay: Optional[OverlayInfo] = None,
    info_dir: Path = INFO_DIR,
    writable_status: Path = WRITABLE_STATUS,
) -> int:
    """Remove the whiteouts a removed package left over lower-layer files.

    Call after ``opkg remove``. Pass the file list captured before the removal,
    or restore the package's metadata first so the image's ``.list`` shows.

    Returns the number of files restored (or that would be, with dry_run).
    """
    if is_package_in_writable_status(package_name, writable_status):
        LOGGER.debug("package %s is still installed in the overlay, skipping", package_name)
        return 0

    file_paths = file_list if file_list is not None else get_package_files(
        package_name, info_dir
    )
    if not file_paths:
        LOGGER.debug("no file list found for package %s", package_name)
        return 0

    own = overlay is None
    overlay = overlay or OverlayInfo()
    try:
        restorable = find_restorable_files(file_paths, overlay)
        if dry_run:
            for path in restorable:
                LOGGER.info("would restore lower for: %s", path)
            return len(restorable)
        restored = sum(1 for path in restorable if overlay.restore_lower(str(path)))
    finally:
        if own:
            overlay.close()

    if restored:
        LOGGER.info("restored %d lower-layer file(s) for package %s", restored, package_name)
    return restored


def restore_opkg_metadata(
    package_name: str,
    info_dir: Path = INFO_DIR,
    dry_run: bool = False,
    overlay: Optional[OverlayInfo] = None,
) -> int:
    """Remove whiteouts over the image's opkg metadata for a removed package.

    ``opkg remove`` deletes ``<package>.list``, ``.control`` and friends, which
    whites out the base image's copies and hides the package from opkg even
    though the image still ships it.
    """
    paths = [str(Path(info_dir) / f"{package_name}{suffix}") for suffix in METADATA_SUFFIXES]
    own = overlay is None
    overlay = overlay or OverlayInfo()
    try:
        restorable = find_restorable_files(paths, overlay)
        if dry_run:
            return len(restorable)
        restored = sum(1 for path in restorable if overlay.restore_lower(str(path)))
    finally:
        if own:
            overlay.close()
    if restored:
        LOGGER.info("restored %d metadata file(s) for package %s", restored, package_name)
    return restored


def restore_files_for_packages(
    package_names: Iterable[str],
    dry_run: bool = False,
    file_lists: Optional[Dict[str, List[str]]] = None,
) -> int:
    """Restore metadata and files for several removed packages.

    Metadata goes first so the image's file list is readable when no list was
    captured before the removal.
    """
    total = 0
    with OverlayInfo() as overlay:
        for package_name in package_names:
            try:
                total += restore_opkg_metadata(package_name, dry_run=dry_run, overlay=overlay)
                file_list = file_lists.get(package_name) if file_lists else None
                total += restore_package_files(
                    package_name, dry_run=dry_run, file_list=file_list, overlay=overlay
                )
            except OverlayIoctlUnsupported:
                raise
            except Exception as exc:
                LOGGER.error("error restoring files for package %s: %s", package_name, exc)
    return total
