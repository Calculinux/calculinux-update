"""Tests for config file handling during RAUC updates."""

from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import pytest

from calculinux_update.opkg.conffiles import (
    ConffileInfo,
    get_package_conffiles,
    get_all_conffiles,
    detect_modified_conffiles,
    create_dpkg_new_files,
    _compute_md5,
)


class TestComputeMd5:
    """Tests for MD5 checksum computation."""

    def test_compute_md5_success(self, tmp_path):
        """Test successful MD5 computation."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")
        
        md5 = _compute_md5(test_file)
        
        # MD5 of "Hello, World!" is 65a8e27d8879283831b664bd8b7f0ad4
        assert md5 == "65a8e27d8879283831b664bd8b7f0ad4"

    def test_compute_md5_nonexistent_file(self, tmp_path):
        """Test MD5 computation for non-existent file."""
        test_file = tmp_path / "nonexistent.txt"
        
        md5 = _compute_md5(test_file)
        
        assert md5 is None

    def test_compute_md5_permission_denied(self, tmp_path):
        """Test MD5 computation when file cannot be read."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")
        test_file.chmod(0o000)
        
        try:
            md5 = _compute_md5(test_file)
            assert md5 is None
        finally:
            test_file.chmod(0o644)


class TestGetPackageConffiles:
    """Tests for getting conffiles from opkg metadata."""

    def test_get_conffiles_with_checksums(self, tmp_path):
        """Test reading conffiles with MD5 checksums."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        conffiles_file = info_dir / "test-package.conffiles"
        conffiles_file.write_text(
            "/etc/test.conf d41d8cd98f00b204e9800998ecf8427e\n"
            "/etc/test2.conf 098f6bcd4621d373cade4e832627b4f6\n"
        )
        
        conffiles = get_package_conffiles("test-package", str(info_dir))
        
        assert len(conffiles) == 2
        assert conffiles[0] == ConffileInfo("/etc/test.conf", "test-package", "d41d8cd98f00b204e9800998ecf8427e")
        assert conffiles[1] == ConffileInfo("/etc/test2.conf", "test-package", "098f6bcd4621d373cade4e832627b4f6")

    def test_get_conffiles_without_checksums(self, tmp_path):
        """Test reading conffiles without MD5 checksums."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        conffiles_file = info_dir / "test-package.conffiles"
        conffiles_file.write_text(
            "/etc/test.conf\n"
            "/etc/test2.conf\n"
        )
        
        conffiles = get_package_conffiles("test-package", str(info_dir))
        
        assert len(conffiles) == 2
        assert conffiles[0] == ConffileInfo("/etc/test.conf", "test-package", None)
        assert conffiles[1] == ConffileInfo("/etc/test2.conf", "test-package", None)

    def test_get_conffiles_with_comments(self, tmp_path):
        """Test that comments are ignored."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        conffiles_file = info_dir / "test-package.conffiles"
        conffiles_file.write_text(
            "# This is a comment\n"
            "/etc/test.conf\n"
            "\n"
            "/etc/test2.conf\n"
        )
        
        conffiles = get_package_conffiles("test-package", str(info_dir))
        
        assert len(conffiles) == 2

    def test_get_conffiles_relative_paths(self, tmp_path):
        """Test that relative paths are converted to absolute."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        conffiles_file = info_dir / "test-package.conffiles"
        conffiles_file.write_text("etc/test.conf\n")
        
        conffiles = get_package_conffiles("test-package", str(info_dir))
        
        assert len(conffiles) == 1
        assert conffiles[0].path == "/etc/test.conf"

    def test_get_conffiles_no_metadata(self, tmp_path):
        """Test handling when conffiles metadata doesn't exist."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        
        conffiles = get_package_conffiles("test-package", str(info_dir))
        
        assert conffiles == []

    def test_get_conffiles_read_error(self, tmp_path):
        """Test handling when conffiles file cannot be read."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        conffiles_file = info_dir / "test-package.conffiles"
        conffiles_file.write_text("/etc/test.conf\\n")
        conffiles_file.chmod(0o000)
        
        try:
            conffiles = get_package_conffiles("test-package", str(info_dir))
            assert conffiles == []
        finally:
            conffiles_file.chmod(0o644)


class TestGetAllConffiles:
    """Tests for getting conffiles from multiple packages."""

    def test_get_all_conffiles(self, tmp_path):
        """Test getting conffiles from multiple packages."""
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        
        (info_dir / "pkg1.conffiles").write_text("/etc/pkg1.conf\n")
        (info_dir / "pkg2.conffiles").write_text("/etc/pkg2.conf\n")

        conffiles = get_all_conffiles(["pkg1", "pkg2"], str(info_dir))

        assert len(conffiles) == 2
        paths = [cf.path for cf in conffiles]
        
        conffiles = get_all_conffiles([], str(info_dir))
        
        assert conffiles == []


class TestDetectModifiedConffiles:
    """Tests for detecting modified config files."""

    def test_detect_modified_conffiles(self, tmp_path):
        """Test detecting modified config files."""
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "etc/upper").mkdir(parents=True)
        (overlay_dir / "etc/lower").mkdir(parents=True)
        (overlay_dir / "etc/upper/test.conf").write_text("modified content")
        (overlay_dir / "etc/lower/test.conf").write_text("original content")

        # The merged-view file only needs to \"exist\" for the detector to proceed.
        # We avoid touching the real /etc by mocking Path.exists for that one path.
        with patch("calculinux_update.opkg.conffiles.get_all_conffiles") as mock_get_all:
            mock_get_all.return_value = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
            orig_exists = Path.exists

            def exists_side_effect(self):
                if str(self) == "/etc/test.conf":
                    return True
                return orig_exists(self)

            with patch.object(Path, "exists", exists_side_effect):
                modified = detect_modified_conffiles(["test-pkg"], str(overlay_dir))

        assert len(modified) == 1
        assert modified[0].path == "/etc/test.conf"

    def test_detect_no_modifications(self, tmp_path):
        """Test when no config files are modified."""
        overlay_dir = tmp_path / "overlay"
        
        with patch("calculinux_update.opkg.conffiles.get_all_conffiles") as mock_get_all:
            mock_get_all.return_value = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
            with patch("pathlib.Path.exists", return_value=True):
                with patch("calculinux_update.opkg.conffiles._compute_md5") as mock_md5:
                    # Same checksum = no modification
                    mock_md5.return_value = "same_checksum"
                    
                    modified = detect_modified_conffiles(
                        ["test-pkg"],
                        str(overlay_dir)
                    )
                    
                    assert modified == []

    def test_detect_conffile_not_in_filesystem(self, tmp_path):
        """Test handling when conffile doesn't exist in filesystem."""
        overlay_dir = tmp_path / "overlay"
        
        with patch("calculinux_update.opkg.conffiles.get_all_conffiles") as mock_get_all:
            mock_get_all.return_value = [ConffileInfo("/etc/missing.conf", "test-pkg", None)]
            with patch("pathlib.Path.exists", return_value=False):
                modified = detect_modified_conffiles(
                    ["test-pkg"],
                    str(overlay_dir)
                )
                
                assert modified == []


class TestCreateDpkgNewFiles:
    """Tests for creating .dpkg-new files."""

    def test_create_dpkg_new_files(self, tmp_path):
        """Test creating .dpkg-new files."""
        # Setup overlay structure
        overlay_dir = tmp_path / "overlay"
        overlay_etc = overlay_dir / "etc"
        (overlay_etc / "lower").mkdir(parents=True)
        
        lower_file = overlay_etc / "lower" / "test.conf"
        lower_file.write_text("new content from base image")
        
        # Create actual etc directory for dpkg-new file
        etc_dir = tmp_path / "etc"
        etc_dir.mkdir()
        
        modified = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
        
        with patch("pathlib.Path") as mock_path_cls:
            def path_side_effect(p):
                if str(p).endswith("test.conf.dpkg-new"):
                    return etc_dir / "test.conf.dpkg-new"
                elif "lower" in str(p):
                    return lower_file
                return Path(p)
            mock_path_cls.side_effect = path_side_effect
            
            with patch("shutil.copy2") as mock_copy:
                created = create_dpkg_new_files(
                    modified,
                    str(overlay_dir),
                    dry_run=False
                )
                
                assert len(created) == 1
                assert "/etc/test.conf" in created
                mock_copy.assert_called_once()

    def test_create_dpkg_new_files_dry_run(self, tmp_path):
        """Test dry run doesn't create files."""
        overlay_dir = tmp_path / "overlay"
        modified = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
        
        with patch("pathlib.Path.exists", return_value=True):
            with patch("shutil.copy2") as mock_copy:
                created = create_dpkg_new_files(
                    modified,
                    str(overlay_dir),
                    dry_run=True
                )
                
                assert len(created) == 1
                mock_copy.assert_not_called()

    def test_create_dpkg_new_files_missing_lower(self, tmp_path):
        """Test handling when lower file is missing."""
        overlay_dir = tmp_path / "overlay"
        modified = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
        
        with patch("pathlib.Path.exists", return_value=False):
            created = create_dpkg_new_files(
                modified,
                str(overlay_dir),
                dry_run=False
            )
            
            assert created == {}

    def test_create_dpkg_new_files_copy_error(self, tmp_path):
        """Test handling copy errors."""
        overlay_dir = tmp_path / "overlay"
        modified = [ConffileInfo("/etc/test.conf", "test-pkg", None)]
        
        with patch("pathlib.Path.exists", return_value=True):
            with patch("shutil.copy2", side_effect=OSError("Permission denied")):
                created = create_dpkg_new_files(
                    modified,
                    str(overlay_dir),
                    dry_run=False
                )
                
                assert created == {}
