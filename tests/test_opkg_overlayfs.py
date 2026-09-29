"""Tests for the overlay upper-layer helpers (calculinux_update.opkg.overlayfs)."""

import ctypes
import errno
import os
import stat
import struct
from pathlib import Path

import pytest

from calculinux_update.opkg import overlayfs
from calculinux_update.opkg.overlayfs import (
    OVL_IOC_IS_RESTORABLE,
    OVL_IOC_RESTORE_LOWER,
    OVL_IOC_UPPER_STATE,
    OverlayInfo,
    OverlayIoctlUnsupported,
    UpperInfo,
    UpperState,
    find_restorable_files,
    get_package_entries,
    get_package_files,
    has_files_in_upper,
    restore_files_for_packages,
    restore_opkg_metadata,
    restore_package_files,
)


def mountinfo_line(mount_point: str, fstype: str) -> str:
    return (
        f"36 25 0:32 / {mount_point} rw,relatime shared:1 - {fstype} {fstype} "
        "rw,lowerdir=/x,upperdir=/y,workdir=/z\n"
    )


@pytest.fixture
def overlay_root(tmp_path):
    """An OverlayInfo whose only overlay mount is tmp_path/ovl."""
    mount = tmp_path / "ovl"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(
        mountinfo_line("/", "ext4")
        + mountinfo_line(str(mount), "overlay")
        + mountinfo_line(str(tmp_path / "ovlx"), "tmpfs")
    )
    return OverlayInfo(info), mount


class FakeOverlay:
    """Stands in for OverlayInfo: a path -> UpperInfo map and a restore log."""

    def __init__(self, states=None, errors=None):
        self.states = states or {}
        self.errors = errors or {}
        self.restored = []

    def upper_state(self, path):
        if path in self.errors:
            raise self.errors[path]
        return self.states.get(path, UpperInfo(UpperState.NONE))

    def restore_lower(self, path):
        self.restored.append(path)
        return True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


REG = stat.S_IFREG | 0o644
DIR = stat.S_IFDIR | 0o755


class TestAbi:
    """The numbers and layouts must match include/uapi/linux/overlayfs.h."""

    def test_ioctl_numbers(self):
        assert OVL_IOC_RESTORE_LOWER == 0x40104F01
        assert OVL_IOC_IS_RESTORABLE == 0x40104F02
        assert OVL_IOC_UPPER_STATE == 0xC0204F03

    def test_struct_sizes(self):
        assert overlayfs._PATH_ARGS.size == 16
        assert overlayfs._UPPER_STATE_ARGS.size == 32

    def test_upper_state_passes_real_path_pointer(self, overlay_root, mocker):
        info, mount = overlay_root
        path = str(mount / "etc" / "hostname")
        seen = {}

        def fake_ioctl(fd, request, args):
            seen["request"] = request
            seen["mode"] = os.fstat(fd).st_mode
            ptr, length, flags = struct.unpack_from("=QII", args)
            # What the kernel would copy: path_len bytes plus the NUL.
            seen["path"] = ctypes.string_at(ptr, length + 1)
            seen["flags"] = flags
            struct.pack_into("=III", args, 16, UpperState.UPPER, 1, REG)
            return 0

        mocker.patch("fcntl.ioctl", side_effect=fake_ioctl)
        result = info.upper_state(path)
        info.close()

        assert seen["request"] == OVL_IOC_UPPER_STATE
        assert seen["path"] == path.encode() + b"\0"
        assert seen["flags"] == 0
        assert stat.S_ISDIR(seen["mode"])  # issued on the mount point directory
        assert result == UpperInfo(UpperState.UPPER, mode=REG, opaque=True)

    def test_restore_lower_passes_real_path_pointer(self, overlay_root, mocker):
        info, mount = overlay_root
        path = str(mount / "usr" / "bin" / "foo")
        seen = {}

        def fake_ioctl(fd, request, args):
            seen["request"] = request
            ptr, length, _flags = struct.unpack_from("=QII", args)
            seen["path"] = ctypes.string_at(ptr, length + 1)
            return 0

        mocker.patch("fcntl.ioctl", side_effect=fake_ioctl)
        assert info.restore_lower(path) is True
        info.close()
        assert seen == {"request": OVL_IOC_RESTORE_LOWER, "path": path.encode() + b"\0"}


class TestOverlayInfo:
    def test_mount_point_prefers_longest_overlay(self, tmp_path):
        mi = tmp_path / "mountinfo"
        mi.write_text(
            mountinfo_line("/usr", "overlay")
            + mountinfo_line("/usr/local", "overlay")
            + mountinfo_line("/data", "ext4")
        )
        info = OverlayInfo(mi)
        assert info.mount_point("/usr/local/bin/x") == "/usr/local"
        assert info.mount_point("/usr/bin/x") == "/usr"
        assert info.mount_point("/usr") == "/usr"
        assert info.mount_point("/usrx/bin") is None
        assert info.mount_point("/data/x") is None

    def test_missing_mountinfo_means_no_overlays(self, tmp_path):
        info = OverlayInfo(tmp_path / "missing")
        assert info.mount_point("/etc/x") is None
        assert info.upper_state("/etc/x") == UpperInfo(UpperState.NONE)
        assert info.restore_lower("/etc/x") is False

    def test_enotty_is_unsupported(self, overlay_root, mocker):
        info, mount = overlay_root
        mocker.patch("fcntl.ioctl", side_effect=OSError(errno.ENOTTY, "no ioctl"))
        with pytest.raises(OverlayIoctlUnsupported):
            info.upper_state(str(mount / "f"))
        with pytest.raises(OverlayIoctlUnsupported):
            info.restore_lower(str(mount / "f"))
        info.close()

    @pytest.mark.parametrize("err", [errno.ENOENT, errno.ENOTDIR, errno.EXDEV])
    def test_state_errors_that_mean_none(self, overlay_root, mocker, err):
        info, mount = overlay_root
        mocker.patch("fcntl.ioctl", side_effect=OSError(err, "x"))
        assert info.upper_state(str(mount / "a" / "b")) == UpperInfo(UpperState.NONE)
        info.close()

    def test_other_state_errors_propagate(self, overlay_root, mocker):
        info, mount = overlay_root
        mocker.patch("fcntl.ioctl", side_effect=OSError(errno.EACCES, "denied"))
        with pytest.raises(OSError):
            info.upper_state(str(mount / "f"))
        info.close()

    @pytest.mark.parametrize("err", [errno.ENODATA, errno.EEXIST, errno.ENOENT, errno.EPERM])
    def test_restore_failures_return_false(self, overlay_root, mocker, err):
        info, mount = overlay_root
        mocker.patch("fcntl.ioctl", side_effect=OSError(err, "x"))
        assert info.restore_lower(str(mount / "f")) is False
        info.close()

    def test_one_fd_per_mount(self, overlay_root, mocker):
        info, mount = overlay_root
        opened = mocker.spy(os, "open")
        mocker.patch("fcntl.ioctl", return_value=0)
        for name in ("a", "b", "c"):
            info.upper_state(str(mount / name))
        assert opened.call_count == 1
        info.close()


class TestPackageLists:
    def test_entries_with_modes(self, tmp_path):
        (tmp_path / "pkg.list").write_text(
            "/usr/bin/tool\t0100755\t\n"
            "/usr/share/tool\t040755\t\n"
            "/usr/lib/libtool.so\t0120777\tlibtool.so.1\n"
        )
        assert get_package_entries("pkg", tmp_path) == [
            ("/usr/bin/tool", 0o100755),
            ("/usr/share/tool", 0o40755),
            ("/usr/lib/libtool.so", 0o120777),
        ]

    def test_legacy_and_relative_entries(self, tmp_path):
        (tmp_path / "old.list").write_text("usr/bin/a\n/usr/share/b/\n\n/usr/bin/c\tbogus\n")
        assert get_package_entries("old", tmp_path) == [
            ("/usr/bin/a", None),
            ("/usr/share/b", None),
            ("/usr/bin/c", None),
        ]
        assert get_package_files("old", tmp_path) == ["/usr/bin/a", "/usr/share/b", "/usr/bin/c"]

    def test_missing_list(self, tmp_path):
        assert get_package_entries("absent", tmp_path) == []
        assert get_package_files("absent", tmp_path) == []


class TestHasFilesInUpper:
    @pytest.fixture
    def info_dir(self, tmp_path):
        (tmp_path / "pkg.list").write_text(
            "/usr\t040755\t\n/usr/bin\t040755\t\n/usr/bin/tool\t0100755\t\n"
        )
        return tmp_path

    def test_real_upper_file(self, info_dir):
        overlay = FakeOverlay({"/usr/bin/tool": UpperInfo(UpperState.UPPER, mode=REG)})
        assert has_files_in_upper("pkg", overlay, info_dir) is True

    def test_directories_are_ignored(self, info_dir):
        overlay = FakeOverlay({
            "/usr": UpperInfo(UpperState.UPPER, mode=DIR),
            "/usr/bin": UpperInfo(UpperState.UPPER, mode=DIR),
        })
        assert has_files_in_upper("pkg", overlay, info_dir) is False

    def test_whiteouts_do_not_count(self, info_dir):
        overlay = FakeOverlay({"/usr/bin/tool": UpperInfo(UpperState.WHITEOUT)})
        assert has_files_in_upper("pkg", overlay, info_dir) is False

    def test_unknown_state_counts_as_upper(self, info_dir):
        overlay = FakeOverlay(errors={"/usr/bin/tool": OSError(errno.EACCES, "denied")})
        assert has_files_in_upper("pkg", overlay, info_dir) is True

    def test_unsupported_kernel_is_fatal(self, info_dir):
        overlay = FakeOverlay(errors={"/usr/bin/tool": OverlayIoctlUnsupported("no")})
        with pytest.raises(OverlayIoctlUnsupported):
            has_files_in_upper("pkg", overlay, info_dir)

    def test_no_file_list(self, tmp_path):
        assert has_files_in_upper("absent", FakeOverlay(), tmp_path) is False

    def test_creates_and_closes_its_own_overlay(self, info_dir, mocker):
        fake = FakeOverlay()
        mocker.patch.object(overlayfs, "OverlayInfo", return_value=fake)
        close = mocker.spy(fake, "close")
        assert has_files_in_upper("pkg", info_dir=info_dir) is False
        close.assert_called_once()


class TestRestore:
    def test_find_restorable_skips_no_lower_dir(self):
        overlay = FakeOverlay({
            "/a": UpperInfo(UpperState.WHITEOUT),
            "/b": UpperInfo(UpperState.WHITEOUT, no_lower_dir=True),
            "/c": UpperInfo(UpperState.UPPER, mode=REG),
        })
        assert find_restorable_files(["/a", "/b", "/c", "/d"], overlay) == [Path("/a")]

    def test_restore_package_files(self, tmp_path):
        status = tmp_path / "status"
        status.write_text("Package: other\nStatus: install ok installed\n\n")
        overlay = FakeOverlay({"/a": UpperInfo(UpperState.WHITEOUT)})
        restored = restore_package_files(
            "pkg", file_list=["/a", "/b"], overlay=overlay, writable_status=status
        )
        assert restored == 1
        assert overlay.restored == ["/a"]

    def test_restore_package_files_dry_run(self, tmp_path):
        status = tmp_path / "status"
        status.write_text("")
        overlay = FakeOverlay({"/a": UpperInfo(UpperState.WHITEOUT)})
        assert restore_package_files(
            "pkg", dry_run=True, file_list=["/a"], overlay=overlay, writable_status=status
        ) == 1
        assert overlay.restored == []

    def test_restore_skips_package_still_in_overlay(self, tmp_path):
        status = tmp_path / "status"
        status.write_text("Package: pkg\nStatus: install ok installed\n\n")
        overlay = FakeOverlay({"/a": UpperInfo(UpperState.WHITEOUT)})
        assert restore_package_files(
            "pkg", file_list=["/a"], overlay=overlay, writable_status=status
        ) == 0
        assert overlay.restored == []

    def test_restore_reads_list_when_not_given(self, tmp_path):
        (tmp_path / "pkg.list").write_text("/a\t0100644\t\n")
        overlay = FakeOverlay({"/a": UpperInfo(UpperState.WHITEOUT)})
        assert restore_package_files(
            "pkg", overlay=overlay, info_dir=tmp_path, writable_status=tmp_path / "none"
        ) == 1

    def test_restore_without_files(self, tmp_path):
        assert restore_package_files(
            "pkg", file_list=[], overlay=FakeOverlay(), writable_status=tmp_path / "none"
        ) == 0

    def test_restore_opkg_metadata(self, tmp_path):
        control = str(tmp_path / "pkg.control")
        listing = str(tmp_path / "pkg.list")
        overlay = FakeOverlay({
            control: UpperInfo(UpperState.WHITEOUT),
            listing: UpperInfo(UpperState.WHITEOUT),
        })
        assert restore_opkg_metadata("pkg", tmp_path, overlay=overlay) == 2
        assert sorted(overlay.restored) == sorted([control, listing])
        assert restore_opkg_metadata("pkg", tmp_path, dry_run=True, overlay=overlay) == 2

    def test_restore_files_for_packages(self, tmp_path, mocker):
        fake = FakeOverlay({"/a": UpperInfo(UpperState.WHITEOUT)})
        mocker.patch.object(overlayfs, "OverlayInfo", return_value=fake)
        mocker.patch.object(overlayfs, "is_package_in_writable_status", return_value=False)
        total = restore_files_for_packages(["p1", "p2"], file_lists={"p1": ["/a"]})
        assert total == 1
        assert fake.restored == ["/a"]

    def test_restore_files_for_packages_continues_after_error(self, mocker):
        fake = FakeOverlay()
        mocker.patch.object(overlayfs, "OverlayInfo", return_value=fake)
        mocker.patch.object(
            overlayfs, "restore_opkg_metadata", side_effect=[RuntimeError("boom"), 2]
        )
        mocker.patch.object(overlayfs, "restore_package_files", return_value=1)
        assert restore_files_for_packages(["bad", "good"]) == 3

    def test_restore_files_for_packages_unsupported_is_fatal(self, mocker):
        mocker.patch.object(overlayfs, "OverlayInfo", return_value=FakeOverlay())
        mocker.patch.object(
            overlayfs, "restore_opkg_metadata", side_effect=OverlayIoctlUnsupported("no")
        )
        with pytest.raises(OverlayIoctlUnsupported):
            restore_files_for_packages(["pkg"])
