"""Tests for OverlayFS whiteout cleanup functionality."""

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from calculinux_update.opkg.overlayfs import (
    FileRestorability,
    restore_opkg_metadata,
    restore_package_files,
    restore_files_for_packages,
    find_restorable_files,
    get_package_files,
    has_files_in_upper,
    is_package_in_writable_status,
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


class TestRestorePackageFiles:
    """Tests for restore_package_files function."""

    def test_restore_package_files_success(self, tmp_path):
        """Test successful restoration of files."""
        mock_files = ["/usr/bin/app", "/usr/bin/tool", "/usr/bin/other"]

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.find_restorable_files") as mock_find:
                with patch("calculinux_update.opkg.overlayfs.restore_lower_via_ioctl", return_value=True):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = Mock(returncode=1, stdout="")  # Not installed
                        mock_find.return_value = [Path("/usr/bin/app"), Path("/usr/bin/tool")]

                        restored = restore_package_files("test-pkg")

        assert restored == 2

    def test_restore_package_files_dry_run(self, tmp_path):
        """Test dry run mode doesn't actually restore files."""
        mock_files = ["/usr/bin/app"]

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.find_restorable_files") as mock_find:
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = Mock(returncode=1, stdout="")
                    mock_find.return_value = [Path("/usr/bin/app")]

                    restored = restore_package_files("test-pkg", dry_run=True)

        assert restored == 1

    def test_restore_package_files_package_still_installed(self):
        """Test that restoration is skipped if package is still installed."""
        mock_status_output = "Status: install ok installed\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=mock_status_output)

            restored = restore_package_files("test-pkg")

        assert restored == 0

    def test_restore_package_files_no_files(self):
        """Test handling when package has no files."""
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=[]):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=1, stdout="")

                restored = restore_package_files("test-pkg")

        assert restored == 0

    def test_restore_package_files_restoration_error(self, tmp_path):
        """Test handling of file restoration errors."""
        mock_files = ["/usr/bin/app"]

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.find_restorable_files") as mock_find:
                with patch("calculinux_update.opkg.overlayfs.restore_lower_via_ioctl", return_value=False):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = Mock(returncode=1, stdout="")
                        mock_find.return_value = [Path("/usr/bin/app")]

                        restored = restore_package_files("test-pkg")

        # Should handle the error gracefully
        assert restored == 0


class TestRestoreFilesForPackages:
    """Tests for restore_files_for_packages function."""

    def test_restore_multiple_packages(self, tmp_path):
        """Test restoring files for multiple packages."""
        with patch("calculinux_update.opkg.overlayfs.restore_package_files") as mock_restore:
            with patch("calculinux_update.opkg.overlayfs.restore_opkg_metadata") as mock_metadata:
                mock_restore.side_effect = [2, 3, 0]  # Different counts per package
                mock_metadata.return_value = 1

                packages = ["pkg1", "pkg2", "pkg3"]
                total = restore_files_for_packages(packages)

        assert total == 8  # 3 metadata + 5 package files
        assert mock_restore.call_count == 3
        assert mock_metadata.call_count == 3

    def test_restore_multiple_packages_dry_run(self, tmp_path):
        """Test that dry run is passed through correctly."""
        with patch("calculinux_update.opkg.overlayfs.restore_package_files") as mock_restore:
            with patch("calculinux_update.opkg.overlayfs.restore_opkg_metadata") as mock_metadata:
                mock_restore.return_value = 3
                mock_metadata.return_value = 1

                packages = ["pkg1"]
                total = restore_files_for_packages(packages, dry_run=True)

        assert total == 4

    def test_restore_multiple_packages_with_error(self, tmp_path):
        """Test that errors in one package don't stop processing others."""
        with patch("calculinux_update.opkg.overlayfs.restore_package_files") as mock_restore:
            with patch("calculinux_update.opkg.overlayfs.restore_opkg_metadata") as mock_metadata:
                mock_restore.side_effect = [2, 1]
                mock_metadata.side_effect = [1, Exception("Test error"), 1]

                packages = ["pkg1", "pkg2", "pkg3"]
                total = restore_files_for_packages(packages)

        # Should continue after error: pkg1 (1+2) + pkg3 (1+1) = 5
        assert total == 5
        assert mock_restore.call_count == 2  # pkg1 and pkg3
        assert mock_metadata.call_count == 3  # All 3 attempted

    def test_restore_empty_package_list(self):
        """Test with empty package list."""
        total = restore_files_for_packages([])
        assert total == 0


class TestRestoreOpkgMetadata:
    """Test restore_opkg_metadata function."""

    def test_restore_metadata_success(self, tmp_path, mocker):
        """Test successful restoration of metadata files."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        package = "test-pkg"

        # Mock the ioctl functions
        with patch("calculinux_update.opkg.overlayfs.is_file_restorable", return_value=True):
            with patch("calculinux_update.opkg.overlayfs.restore_lower_via_ioctl", return_value=True):
                result = restore_opkg_metadata(package, str(info_dir))

        # Should attempt to restore multiple metadata files
        assert result > 0

    def test_restore_metadata_dry_run(self, tmp_path, mocker):
        """Test dry run mode for metadata restoration."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        package = "test-pkg"

        with patch("calculinux_update.opkg.overlayfs.is_file_restorable", return_value=True):
            result = restore_opkg_metadata(package, str(info_dir), dry_run=True)

        # Should report what would be restored
        assert result > 0

    def test_restore_metadata_no_info_dir(self):
        """Test handling when info directory doesn't exist."""
        result = restore_opkg_metadata("test-pkg", "/nonexistent/path")
        assert result == 0

    def test_restore_metadata_no_restorable_files(self, tmp_path):
        """Test when no metadata files are restorable."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        package = "test-pkg"

        with patch("calculinux_update.opkg.overlayfs.is_file_restorable", return_value=False):
            result = restore_opkg_metadata(package, str(info_dir))

        assert result == 0

    def test_restore_metadata_partial_restoration(self, tmp_path):
        """Test when only some metadata files are restorable."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        package = "test-pkg"

        # Mock to return True for some files, False for others
        call_count = 0
        def mock_restorable(mount, path):
            nonlocal call_count
            call_count += 1
            return call_count <= 2  # First 2 calls return True

        with patch("calculinux_update.opkg.overlayfs.is_file_restorable", side_effect=mock_restorable):
            with patch("calculinux_update.opkg.overlayfs.restore_lower_via_ioctl", return_value=True):
                result = restore_opkg_metadata(package, str(info_dir))

        assert result == 2


class TestIsPackageInWritableStatus:
    """Test is_package_in_writable_status function."""

    def test_uses_opkg_writable_only_flag(self, mocker):
        """Test that function uses opkg --writable-only when available."""
        package = "test-pkg"
        mock_run = mocker.patch(
            "subprocess.run",
            return_value=mocker.MagicMock(
                returncode=0, stdout="Package: test-pkg\nStatus: install ok installed\n"
            ),
        )

        result = is_package_in_writable_status(package)

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "--writable-only" in call_args[0][0]
        assert "status" in call_args[0][0]
        assert package in call_args[0][0]

    def test_package_not_in_writable_status(self, mocker):
        """Test when package is not in writable status."""
        package = "test-pkg"
        # Empty output means not found
        mocker.patch(
            "subprocess.run", return_value=mocker.MagicMock(returncode=0, stdout="")
        )

        result = is_package_in_writable_status(package)

        assert result is False

    def test_command_fails(self, mocker):
        """Test when opkg command fails."""
        package = "test-pkg"
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.SubprocessError("Command failed"),
        )

        result = is_package_in_writable_status(package)

        assert result is False

    def test_command_returns_error(self, mocker):
        """Test when opkg returns non-zero exit code."""
        package = "test-pkg"
        mocker.patch(
            "subprocess.run",
            return_value=mocker.MagicMock(returncode=1, stdout=""),
        )

        result = is_package_in_writable_status(package)

        assert result is False


class TestHasFilesInUpper:
    """Tests for has_files_in_upper function."""

    def test_has_files_in_upper_with_real_files(self, tmp_path):
        """Test package with actual files in upper layer."""
        mock_files = ["/usr/bin/test-app", "/etc/test.conf"]

        # Mock: files are in upper (real files)
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.check_file_restorability", return_value=FileRestorability.IN_UPPER):
                result = has_files_in_upper("test-package")

        assert result is True

    def test_has_files_in_upper_only_whiteouts(self, tmp_path):
        """Test package with only whiteout files (no real files)."""
        mock_files = ["/usr/bin/test-app"]

        # Mock: file is a whiteout, so should return False
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.check_file_restorability", return_value=FileRestorability.WHITEOUT):
                result = has_files_in_upper("test-package")

        assert result is False

    def test_has_files_in_upper_no_files_exist(self, tmp_path):
        """Test package where files don't exist in upper layer at all."""
        mock_files = ["/usr/bin/test-app", "/etc/test.conf"]

        # Mock: files are in lower only (not in upper)
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.check_file_restorability", return_value=FileRestorability.IN_LOWER_ONLY):
                result = has_files_in_upper("test-package")

        assert result is False

    def test_has_files_in_upper_mixed_files_and_whiteouts(self, tmp_path):
        """Test package with both real files and whiteouts."""
        mock_files = ["/usr/bin/test-app", "/etc/test.conf"]

        # Mock: first file is real (in upper), second is whiteout
        def mock_restorability(mount, path):
            if path == "/usr/bin/test-app":
                return FileRestorability.IN_UPPER
            return FileRestorability.WHITEOUT

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.check_file_restorability", side_effect=mock_restorability):
                result = has_files_in_upper("test-package")

        # Should return True because at least one real file exists
        assert result is True

    def test_has_files_in_upper_no_package_files(self, tmp_path):
        """Test package with no files listed by opkg."""
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=[]):
            result = has_files_in_upper("test-package")

        assert result is False

    def test_has_files_in_upper_file_access_error(self, tmp_path):
        """Test handling of file access errors."""
        mock_files = ["/usr/bin/test-app"]

        def mock_exists():
            raise OSError("Permission denied")

        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("pathlib.Path.exists", side_effect=mock_exists):
                result = has_files_in_upper("test-package")

        # Should handle error gracefully and return False
        assert result is False

    def test_has_files_in_upper_directory(self, tmp_path):
        """Test that directories count as real files."""
        mock_files = ["/usr/share/test-app"]

        # Mock: directory is a real file in upper
        with patch("calculinux_update.opkg.overlayfs.get_package_files", return_value=mock_files):
            with patch("calculinux_update.opkg.overlayfs.check_file_restorability", return_value=FileRestorability.IN_UPPER):
                result = has_files_in_upper("test-package")

        assert result is True
