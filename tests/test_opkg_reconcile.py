from pathlib import Path

from calculinux_update.opkg import reconcile


def write_status(path: Path, packages):
    chunks = [f"Package: {pkg}\nVersion: 1.0\n" for pkg in packages]
    path.write_text("\n".join(chunks) + "\n")


def test_compute_reconcile_plan(tmp_path):
    image_status = tmp_path / "image"
    writable_status = tmp_path / "writable"
    current_status = tmp_path / "current"
    write_status(image_status, ["base", "keep"])
    write_status(writable_status, ["base", "overlay"])
    write_status(current_status, ["missing", "keep"])

    plan = reconcile.compute_reconcile_plan(
        image_status, writable_status, current_status=current_status
    )
    assert plan.duplicates == ["base"]
    assert plan.reinstall == ["missing"]
    assert plan.upgrade == ["base", "overlay"]


def test_prune_writable_status(tmp_path):
    writable = tmp_path / "writable"
    write_status(writable, ["keep", "drop"])
    changed = reconcile.prune_writable_status(writable, ["drop"])
    assert changed
    assert "drop" not in writable.read_text()
