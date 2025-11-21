import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_parse_shell_assignments():
    env = reconcile._parse_shell_assignments("FOO=bar\nBAR='baz'\n")
    assert env == {"FOO": "bar", "BAR": "baz"}


def test_find_booted_device():
    env = {
        "RAUC_SLOT_STATE_1": "inactive",
        "RAUC_SLOT_STATE_2": "booted",
        "RAUC_SLOT_DEVICE_2": "/dev/slotB",
    }
    device = reconcile._find_booted_device(env)
    # Should find the device for the booted slot
    assert device is not None
    assert device.startswith("/dev/")


def test_find_booted_device_fallback():
    env = {"FOO_STATE": "booted", "FOO_DEVICE": "/dev/x"}
    device = reconcile._find_booted_device(env)
    # Should use fallback logic when standard RAUC_SLOT_STATE_N not found
    assert device is not None
    assert device.startswith("/dev/")


def test_snapshot_current_slot_status_success(monkeypatch, tmp_path):
    monkeypatch.setattr(reconcile.shutil, "which", lambda *_: "/usr/bin/rauc")
    mount_dir = tmp_path / "mnt"

    def fake_mkdtemp(prefix):
        mount_dir.mkdir(parents=True, exist_ok=True)
        return str(mount_dir)

    monkeypatch.setattr(reconcile.tempfile, "mkdtemp", fake_mkdtemp)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["rauc", "status"]:
            stdout = "RAUC_SLOT_STATE_1=booted\nRAUC_SLOT_DEVICE_1=/dev/loop0\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if cmd and cmd[0] == "mount":
            (mount_dir / "var/lib/opkg").mkdir(parents=True, exist_ok=True)
            (mount_dir / "var/lib/opkg/status").write_text("Package: foo\n\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd and cmd[0] == "umount":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    snapshot = reconcile.snapshot_current_slot_status()
    assert snapshot is not None
    assert Path(snapshot).read_text().startswith("Package: foo")


def test_snapshot_current_slot_status_missing_rauc(monkeypatch):
    monkeypatch.setattr(reconcile.shutil, "which", lambda *_: None)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("should not call subprocess when rauc missing")

    monkeypatch.setattr(reconcile.subprocess, "run", fail_run)
    assert reconcile.snapshot_current_slot_status() is None


def test_snapshot_current_slot_status_mount_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(reconcile.shutil, "which", lambda *_: "/usr/bin/rauc")
    mount_dir = tmp_path / "mnt"
    monkeypatch.setattr(reconcile.tempfile, "mkdtemp", lambda prefix: str(mount_dir))

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["rauc", "status"]:
            stdout = "RAUC_SLOT_STATE_1=booted\nRAUC_SLOT_DEVICE_1=/dev/loop0\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if cmd and cmd[0] == "mount":
            raise subprocess.CalledProcessError(1, cmd, "boom")
        if cmd and cmd[0] == "umount":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)
    assert reconcile.snapshot_current_slot_status() is None
    assert not mount_dir.exists()
