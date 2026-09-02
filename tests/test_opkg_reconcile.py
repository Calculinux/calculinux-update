from pathlib import Path
from unittest.mock import patch

from calculinux_update.opkg import reconcile


def write_status(path: Path, packages):
    chunks = [f"Package: {pkg}\nVersion: 1.0\n" for pkg in packages]
    path.write_text("\n".join(chunks) + "\n")


def test_compute_reconcile_plan(tmp_path):
    """Test basic reconcile plan computation without upper layer detection."""
    image_status = tmp_path / "image"
    writable_status = tmp_path / "writable"
    current_status = tmp_path / "current"
    write_status(image_status, ["base", "keep"])
    write_status(writable_status, ["base", "overlay"])
    write_status(current_status, ["missing", "keep"])

    # Mock has_files_in_upper to return True for all packages
    with patch("calculinux_update.opkg.reconcile.has_files_in_upper", return_value=True):
        plan = reconcile.compute_reconcile_plan(
            image_status, writable_status, current_status=current_status
        )

    assert plan.duplicates == ["base"]
    assert plan.status_only_duplicates == []
    assert plan.reinstall == ["missing"]
    assert plan.upgrade == ["base", "overlay"]
    assert plan.any_actions()


def test_compute_reconcile_plan_with_status_only_duplicates(tmp_path):
    """Test reconcile plan splits duplicates based on upper layer files."""
    image_status = tmp_path / "image"
    writable_status = tmp_path / "writable"
    write_status(image_status, ["pkg-with-files", "pkg-without-files", "pkg-also-with"])
    write_status(
        writable_status, ["pkg-with-files", "pkg-without-files", "pkg-also-with", "local-only"]
    )

    # Mock has_files_in_upper to simulate different scenarios
    def mock_has_files(pkg):
        return pkg in ["pkg-with-files", "pkg-also-with"]

    with patch(
        "calculinux_update.opkg.reconcile.has_files_in_upper", side_effect=mock_has_files
    ):
        plan = reconcile.compute_reconcile_plan(image_status, writable_status)

    # Packages with files in upper go to duplicates (need physical removal)
    assert sorted(plan.duplicates) == ["pkg-also-with", "pkg-with-files"]
    # Packages without files in upper go to status_only_duplicates (safe status pruning)
    assert plan.status_only_duplicates == ["pkg-without-files"]
    assert plan.reinstall == []
    assert sorted(plan.upgrade) == [
        "local-only",
        "pkg-also-with",
        "pkg-with-files",
        "pkg-without-files",
    ]


def test_compute_reconcile_plan_all_status_only(tmp_path):
    """Test when all duplicates have no files in upper layer."""
    image_status = tmp_path / "image"
    writable_status = tmp_path / "writable"
    write_status(image_status, ["pkg1", "pkg2"])
    write_status(writable_status, ["pkg1", "pkg2", "local"])

    # All packages have no files in upper
    with patch("calculinux_update.opkg.reconcile.has_files_in_upper", return_value=False):
        plan = reconcile.compute_reconcile_plan(image_status, writable_status)

    assert plan.duplicates == []
    assert sorted(plan.status_only_duplicates) == ["pkg1", "pkg2"]
    assert sorted(plan.upgrade) == ["local", "pkg1", "pkg2"]


def test_prune_writable_status(tmp_path):
    writable = tmp_path / "writable"
    write_status(writable, ["keep", "drop"])
    changed = reconcile.prune_writable_status(writable, ["drop"])
    assert changed
    assert "drop" not in writable.read_text()
