"""Tests for OverlayFS whiteout cleanup functionality."""

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from calculinux_update.opkg.overlayfs import (
    cleanup_package_whiteouts,
    cleanup_whiteouts_for_packages,
    find_whiteout_files,
    get_package_files,
    is_whiteout_file,
    remount_overlayfs,
)


@pytest.fixture
def mock_opkg_files():
    """Mock opkg files command output."""
    def _mock_files(package_name):
        files_map = {
            "test-package": [
                "/usr/bin/test-app",
                "/usr/share/test-app/data.txt",
                "/etc/test-app.conf",
            ],
            "another-package": [
                "/usr/lib/libtest.so",
                "/usr/include/test.h",
            ],
        }
        return files_map.get(package_name, [])
    return _mock_files


class TestGetPackageFiles:
    """Tests for get_package_files function."""

    def test_get_package_files_success(self):
        """Test successful retrieval of package files."""
        mock_output = """\
Package test-package (1.0) is installed on root and has the following files:
/usr/bin/test-app
/usr/share/test-app/data.txt
/etc/test-app.conf
"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=mock_output,
            )

            files = get_package_files("test-package")

            assert len(files) == 3
            assert "/usr/bin/test-app" in files
            assert "/usr/share/test-app/data.txt" in files
            assert "/etc/test-app.conf" in files

    def test_get_package_files_relative_paths(self):
        """Test that relative paths are converted to absolute."""
        mock_output = """\
usr/bin/test-app
usr/share/data.txt
"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=mock_output,
            )

            files = get_package_files("test-package")

            assert all(f.startswith('/') for f in files)
            assert "/usr/bin/test-app" in files
            assert "/usr/share/data.txt" in files

    def test_get_package_files_not_installed(self):
        """Test handling of package not installed."""
        mock_output = "Package test-package is not installed.\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=mock_output,
            )

            files = get_package_files("test-package")

            assert files == []

    def test_get_package_files_command_failure(self):
        """Test handling of opkg command failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "opkg")

            files = get_package_files("test-package")

            assert files == []

    def test_get_package_files_timeout(self):
        """Test handling of timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("opkg", 10)

            files = get_package_files("test-package")

            assert files == []


class TestIsWhiteoutFile:
    """Tests for is_whiteout_file function."""

    def test_is_whiteout_character_device_0_0(self, tmp_path):
        """Test detection of character device with major:minor 0:0."""
        # We can't actually create character devices in tests without root,
        # so we'll mock the stat result
        test_file = tmp_path / "test_whiteout"
        test_file.touch()

        mock_stat = Mock()
        mock_stat.st_mode = stat.S_IFCHR | 0o666  # Character device
        mock_stat.st_rdev = os.makedev(0, 0)  # major:minor 0:0

        with patch.object(Path, "stat", return_value=mock_stat):
            assert is_whiteout_file(test_file) is True

    def test_is_not_whiteout_regular_file(self, tmp_path):
        """Test that regular files are not detected as whiteouts."""
        test_file = tmp_path / "regular_file"
        test_file.write_text("content")

        assert is_whiteout_file(test_file) is False

    def test_is_not_whiteout_directory(self, tmp_path):
        """Test that directories are not detected as whiteouts."""
        test_dir = tmp_path / "test_dir"
        test_dir.mkdir()

        assert is_whiteout_file(test_dir) is False

    def test_is_not_whiteout_char_device_wrong_numbers(self, tmp_path):
        """Test that character devices with non-0:0 are not whiteouts."""
        test_file = tmp_path / "test_device"
        test_file.touch()

        mock_stat = Mock()
        mock_stat.st_mode = stat.S_IFCHR | 0o666
        mock_stat.st_rdev = os.makedev(1, 3)  # Not 0:0

        with patch.object(Path, "stat", return_value=mock_stat):
            assert is_whiteout_file(test_file) is False

    def test_is_whiteout_file_not_exists(self, tmp_path):
        """Test handling of non-existent file."""
        test_file = tmp_path / "nonexistent"

        assert is_whiteout_file(test_file) is False


class TestFindWhiteoutFiles:
    """Tests for find_whiteout_files function."""

    def test_find_whiteout_files_with_whiteouts(self, tmp_path):
        """Test finding whiteout files among package files."""
        # Create a mock directory structure
        (tmp_path / "usr" / "bin").mkdir(parents=True)
        (tmp_path / "etc").mkdir()

        test_file = tmp_path / "usr" / "bin" / "test-app"
        whiteout_file = tmp_path / "etc" / "test.conf"
        test_file.touch()
        whiteout_file.touch()

        # Mock is_whiteout_file to return True for whiteout_file
        def mock_is_whiteout(path):
            return path == whiteout_file

        file_paths = ["/usr/bin/test-app", "/etc/test.conf"]

        with patch(
            "calculinux_update.opkg.overlayfs.is_whiteout_file", side_effect=mock_is_whiteout
        ):
            whiteouts = find_whiteout_files(file_paths, str(tmp_path))

        assert len(whiteouts) == 1
        assert whiteouts[0] == whiteout_file

    def test_find_whiteout_files_no_whiteouts(self, tmp_path):
        """Test when no whiteout files exist."""
        (tmp_path / "usr" / "bin").mkdir(parents=True)
        test_file = tmp_path / "usr" / "bin" / "test-app"
        test_file.touch()

        file_paths = ["/usr/bin/test-app"]

        with patch("calculinux_update.opkg.overlayfs.is_whiteout_file", return_value=False):
            whiteouts = find_whiteout_files(file_paths, str(tmp_path))

        assert whiteouts == []

    def test_find_whiteout_files_empty_list(self, tmp_path):
        """Test with empty file list."""
        whiteouts = find_whiteout_files([], str(tmp_path))
        assert whiteouts == []


class TestCleanupPackageWhiteouts:
    """Tests for cleanup_package_whiteouts function."""

    def test_cleanup_package_whiteouts_success(self, tmp_path):
        """Test successful cleanup of whiteout files."""
        # Setup
        (tmp_path / "usr" / "bin").mkdir(parents=True)
        whiteout1 = tmp_path / "usr" / "bin" / "app"
        whiteout2 = tmp_path / "usr" / "bin" / "tool"
        whiteout1.touch()
        whiteout2.touch()

        mock_files = ["/usr/bin/app", "/usr/bin/tool", "/usr/bin/other"]

        def mock_is_whiteout(path):
            return path in [whiteout1, whiteout2]

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch(
                "calculinux_update.opkg.overlayfs.is_whiteout_file", side_effect=mock_is_whiteout
            ):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = Mock(returncode=1, stdout="")  # Not installed

                    removed = cleanup_package_whiteouts("test-pkg", str(tmp_path))

        assert removed == 2
        assert not whiteout1.exists()
        assert not whiteout2.exists()

    def test_cleanup_package_whiteouts_dry_run(self, tmp_path):
        """Test dry run mode doesn't actually remove files."""
        (tmp_path / "usr" / "bin").mkdir(parents=True)
        whiteout = tmp_path / "usr" / "bin" / "app"
        whiteout.touch()

        mock_files = ["/usr/bin/app"]

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.is_whiteout_file", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = Mock(returncode=1, stdout="")

                    removed = cleanup_package_whiteouts("test-pkg", str(tmp_path), dry_run=True)

        assert removed == 1
        assert whiteout.exists()  # Should still exist in dry run

    def test_cleanup_package_whiteouts_package_still_installed(self):
        """Test that cleanup is skipped if package is still installed."""
        mock_status_output = "Status: install ok installed\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=mock_status_output)

            removed = cleanup_package_whiteouts("test-pkg")

        assert removed == 0

    def test_cleanup_package_whiteouts_no_files(self):
        """Test handling when package has no files."""
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=[]):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=1, stdout="")

                removed = cleanup_package_whiteouts("test-pkg")

        assert removed == 0

    def test_cleanup_package_whiteouts_removal_error(self, tmp_path):
        """Test handling of file removal errors."""
        (tmp_path / "usr" / "bin").mkdir(parents=True)
        whiteout = tmp_path / "usr" / "bin" / "app"
        whiteout.touch()

        mock_files = ["/usr/bin/app"]

        def mock_unlink_error(missing_ok=False):
            raise OSError("Permission denied")

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.is_whiteout_file", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = Mock(returncode=1, stdout="")

                    # Patch the unlink method on the Path object in overlayfs module
                    with patch("pathlib.Path.unlink", side_effect=mock_unlink_error):
                        removed = cleanup_package_whiteouts("test-pkg", str(tmp_path))

        # Should handle the error gracefully
        assert removed == 0


class TestCleanupWhiteoutsForPackages:
    """Tests for cleanup_whiteouts_for_packages function."""

    def test_cleanup_multiple_packages(self, tmp_path):
        """Test cleaning up whiteouts for multiple packages."""
        with patch("calculinux_update.opkg.overlayfs.cleanup_package_whiteouts") as mock_cleanup:
            with patch("calculinux_update.opkg.overlayfs.remount_overlayfs") as mock_remount:
                mock_cleanup.side_effect = [2, 3, 0]  # Different counts per package
                mock_remount.return_value = True

                packages = ["pkg1", "pkg2", "pkg3"]
                total = cleanup_whiteouts_for_packages(packages, str(tmp_path))

        assert total == 5
        assert mock_cleanup.call_count == 3
        # Should remount since we removed 5 whiteouts
        mock_remount.assert_called_once_with(str(tmp_path))

    def test_cleanup_multiple_packages_no_remount_if_none_removed(self, tmp_path):
        """Test that remount is skipped if no whiteouts were removed."""
        with patch("calculinux_update.opkg.overlayfs.cleanup_package_whiteouts") as mock_cleanup:
            with patch("calculinux_update.opkg.overlayfs.remount_overlayfs") as mock_remount:
                mock_cleanup.return_value = 0  # No whiteouts removed

                packages = ["pkg1", "pkg2"]
                total = cleanup_whiteouts_for_packages(packages, str(tmp_path))

        assert total == 0
        # Should not remount since nothing was removed
        mock_remount.assert_not_called()

    def test_cleanup_multiple_packages_dry_run_no_remount(self, tmp_path):
        """Test that dry run doesn't remount."""
        with patch("calculinux_update.opkg.overlayfs.cleanup_package_whiteouts") as mock_cleanup:
            with patch("calculinux_update.opkg.overlayfs.remount_overlayfs") as mock_remount:
                mock_cleanup.return_value = 3

                packages = ["pkg1"]
                total = cleanup_whiteouts_for_packages(packages, str(tmp_path), dry_run=True)

        assert total == 3
        # Should not remount in dry run mode
        mock_remount.assert_not_called()

    def test_cleanup_multiple_packages_remount_disabled(self, tmp_path):
        """Test that remount can be explicitly disabled."""
        with patch("calculinux_update.opkg.overlayfs.cleanup_package_whiteouts") as mock_cleanup:
            with patch("calculinux_update.opkg.overlayfs.remount_overlayfs") as mock_remount:
                mock_cleanup.return_value = 3

                packages = ["pkg1"]
                total = cleanup_whiteouts_for_packages(packages, str(tmp_path), remount=False)

        assert total == 3
        # Should not remount when explicitly disabled
        mock_remount.assert_not_called()

    def test_cleanup_multiple_packages_with_error(self, tmp_path):
        """Test that errors in one package don't stop processing others."""
        with patch("calculinux_update.opkg.overlayfs.cleanup_package_whiteouts") as mock_cleanup:
            with patch("calculinux_update.opkg.overlayfs.remount_overlayfs") as mock_remount:
                mock_cleanup.side_effect = [2, Exception("Test error"), 1]
                mock_remount.return_value = True

                packages = ["pkg1", "pkg2", "pkg3"]
                total = cleanup_whiteouts_for_packages(packages, str(tmp_path))

        assert total == 3  # Should continue after error
        assert mock_cleanup.call_count == 3
        # Should still remount even though one package had an error
        mock_remount.assert_called_once()

    def test_cleanup_empty_package_list(self):
        """Test with empty package list."""
        total = cleanup_whiteouts_for_packages([])
        assert total == 0


class TestRemountOverlayfs:
    """Tests for remount_overlayfs function."""

    def test_remount_success(self):
        """Test successful remount."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            result = remount_overlayfs("/")

        assert result is True
        mock_run.assert_called_once_with(
            ["mount", "-o", "remount", "/"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_remount_failure(self):
        """Test remount failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="mount: permission denied")

            result = remount_overlayfs("/")

        assert result is False

    def test_remount_exception(self):
        """Test remount with exception."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("mount", 10)

            result = remount_overlayfs("/")

        assert result is False

    def test_remount_custom_mount_point(self):
        """Test remount with custom mount point."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            result = remount_overlayfs("/mnt/overlay")

        assert result is True
        mock_run.assert_called_once_with(
            ["mount", "-o", "remount", "/mnt/overlay"],
            capture_output=True,
            text=True,
            timeout=10,
        )
