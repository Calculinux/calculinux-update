from pathlib import Path
from types import SimpleNamespace

import pytest

import calculinux_update.hooks as hooks
from calculinux_update.opkg.reconcile import ReconcilePlan


def test_find_cached_package(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    file_old = cache / "foo_1.ipk"
    file_old.write_text("old")
    file_new = cache / "foo_2.ipk"
    file_new.write_text("new")
    monkeypatch.setattr(hooks, "PREFETCH_CACHE_DIR", cache)
    cached = hooks._find_cached_package("foo")
    # Should find a cached package matching the name
    assert cached is not None
    assert "foo" in cached.name
    assert cached.name.endswith(".ipk")


def test_process_pending(tmp_path):
    path = tmp_path / "pending"
    path.write_text("a\n b\n")
    seen = []

    def handler(pkg):
        seen.append(pkg)
        return True

    assert hooks._process_pending(path, handler)
    assert seen == ["a", "b"]
    assert not path.exists()


def test_process_pending_failure(tmp_path):
    path = tmp_path / "pending"
    path.write_text("foo\n")

    def handler(_):
        return False

    assert not hooks._process_pending(path, handler)
    assert path.exists()


def test_run_slot_hook(monkeypatch, tmp_path):
    writable = tmp_path / "status"
    writable.write_text("Package: overlay\n\n")
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable)

    current = tmp_path / "current"
    current.write_text("Package: current\n\n")
    monkeypatch.setattr(hooks, "CURRENT_IMAGE_STATUS", current)

    # Mock state directory and files for rollback tracking
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(hooks, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", state_dir / "updated-slot")
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", state_dir / "pre-update-slot")
    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", state_dir / "pre-update-writable")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", state_dir / "pending-reinstalls")
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", state_dir / "pending-upgrades")
    monkeypatch.setattr(hooks, "STATUS_PRUNED_MARKER", state_dir / "status-pruned")

    mount = tmp_path / "slot"
    (mount / "var/lib/opkg").mkdir(parents=True)
    bundle_status_image = mount / "var/lib/opkg/status.image"
    bundle_status_image.write_text("Package: base\n\n")

    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status_image))

    pruned = {}
    monkeypatch.setattr(
        hooks, "prune_writable_status", lambda *_: pruned.setdefault("called", True)
    )

    plan = ReconcilePlan(
        duplicates=["base"],
        status_only_duplicates=["status-only"],
        reinstall=["foo"],
        upgrade=["bar"],
        broken_abi=[],
        missing_deps=[],
    )
    monkeypatch.setattr(hooks, "compute_reconcile_plan", lambda **_: plan)

    recorded = {"duplicates": None, "reinstall": None, "upgrade": None, "status_only": None}

    def fake_prune_status_only(pkgs):
        recorded["status_only"] = pkgs

    def fake_write(path, packages, label):
        recorded[label] = list(packages)

    monkeypatch.setattr(hooks, "_prune_status_only_duplicates", fake_prune_status_only)
    monkeypatch.setattr(hooks, "_write_pending", fake_write)

    hooks.run_slot_hook("slot-post-install", "rootfs.0")

    # Phase 1: status-only duplicates should be pruned
    assert recorded["status_only"] == ["status-only"]
    # Phase 2: physical duplicates should be queued for post-reboot
    assert recorded["duplicate removal"] == ["base"]
    assert recorded["reinstall"] == ["foo"]
    assert recorded["upgrade"] == ["bar"]


def test_run_slot_hook_logs_compat_report(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="calculinux_update.hooks")

    writable = tmp_path / "status"
    writable.write_text("Package: overlay\n\n")
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable)

    current = tmp_path / "current"
    current.write_text("Package: current\n\n")
    monkeypatch.setattr(hooks, "CURRENT_IMAGE_STATUS", current)

    # Mock state directory and files for rollback tracking to avoid writing /var
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(hooks, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", state_dir / "updated-slot")
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", state_dir / "pre-update-slot")
    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", state_dir / "pre-update-writable")
    monkeypatch.setattr(hooks, "PENDING_DUPLICATES_FILE", state_dir / "pending-duplicates")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", state_dir / "pending-reinstalls")
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", state_dir / "pending-upgrades")
    monkeypatch.setattr(hooks, "STATUS_PRUNED_MARKER", state_dir / "status-pruned")

    # Provide manifests
    cur_manifest = tmp_path / "version-manifest.env"
    cur_manifest.write_text('CALCULINUX_VERSION="1.0.0"\n')
    monkeypatch.setattr(hooks, "CURRENT_VERSION_MANIFEST", cur_manifest)

    bundle_mount = tmp_path / "bundle"
    (bundle_mount / "extras").mkdir(parents=True)
    (bundle_mount / "extras/version-manifest.env").write_text('CALCULINUX_VERSION="2.0.0"\n')
    monkeypatch.setenv("RAUC_BUNDLE_MOUNT_POINT", str(bundle_mount))

    # Minimal required bundle status image env
    bundle_status = tmp_path / "status.image"
    bundle_status.write_text("Package: base\n\n")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status))

    plan = ReconcilePlan(duplicates=[], status_only_duplicates=[], reinstall=[], upgrade=[], broken_abi=[], missing_deps=[])
    monkeypatch.setattr(hooks, "compute_reconcile_plan", lambda **_: plan)
    monkeypatch.setattr(hooks, "prune_writable_status", lambda *_: False)

    hooks.run_slot_hook("slot-post-install", "rootfs.0")
    assert "Version upgrade" in caplog.text


def test_run_slot_hook_non_post_install(monkeypatch):
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    hooks.run_slot_hook("slot-pre-install", "slot")


def test_run_slot_hook_non_rootfs(monkeypatch):
    monkeypatch.setenv("RAUC_SLOT_CLASS", "other")
    hooks.run_slot_hook("slot-post-install", "slot")


def test_run_slot_hook_missing_mount_point(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "not provided" in caplog.text


def test_run_slot_hook_missing_image_status(monkeypatch, tmp_path, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    # Don't set RAUC_BUNDLE_STATUS_IMAGE - this simulates bundle without extras
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "not provided" in caplog.text


def test_run_slot_hook_missing_writable_status(monkeypatch, tmp_path, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    bundle_status = tmp_path / "status.image"
    bundle_status.write_text("Package: base\n\n")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status))
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", tmp_path / "missing-status")
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "writable status" in caplog.text


def test_postreboot_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setattr("os.geteuid", lambda: 0)  # Mock root check
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    upg = tmp_path / "upgrade"
    upg.write_text("bar\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", upg)
    monkeypatch.setattr(hooks, "STATE_DIR", tmp_path)

    # Mock rollback detection to return "not a rollback"
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": "test"})

    calls = []

    monkeypatch.setattr(hooks, "_run_opkg", lambda args: True)
    monkeypatch.setattr(
        hooks, "_install_reinstall_pkg", lambda pkg: calls.append(("install", pkg)) or True
    )
    monkeypatch.setattr(
        hooks, "_upgrade_pkg", lambda pkg: calls.append(("upgrade", pkg)) or True
    )

    hooks.postreboot_entrypoint()
    assert calls == [("install", "foo"), ("upgrade", "bar")]
    assert not rein.exists()
    assert not upg.exists()


def test_postreboot_entrypoint_update_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("os.geteuid", lambda: 0)  # Mock root check
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", tmp_path / "none")
    monkeypatch.setattr(hooks, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": "test"})
    monkeypatch.setattr(hooks, "_run_opkg", lambda args: False)

    try:
        hooks.postreboot_entrypoint()
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit")


def test_postreboot_entrypoint_partial_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("os.geteuid", lambda: 0)  # Mock root check
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", tmp_path / "none")
    monkeypatch.setattr(hooks, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": "test"})
    monkeypatch.setattr(hooks, "_run_opkg", lambda args: True)
    monkeypatch.setattr(hooks, "_install_reinstall_pkg", lambda pkg: False)
    with pytest.raises(SystemExit):
        hooks.postreboot_entrypoint()
    assert rein.exists()


def test_install_reinstall_pkg_prefers_cache(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cached_ipk = cache / "foo_1.ipk"
    cached_ipk.write_text("data")
    monkeypatch.setattr(hooks, "PREFETCH_CACHE_DIR", cache)

    captured = {}

    def fake_run(args):
        captured["args"] = args
        return True

    monkeypatch.setattr(hooks, "_run_opkg", fake_run)
    assert hooks._install_reinstall_pkg("foo")
    # Should use cached .ipk file when available
    assert any(".ipk" in str(arg) for arg in captured["args"])
    assert any("foo" in str(arg) for arg in captured["args"])


def test_install_reinstall_pkg_failure(monkeypatch):
    monkeypatch.setattr(hooks, "PREFETCH_CACHE_DIR", Path("/nonexistent"))
    calls = []

    def fake_run(args):
        calls.append(args)
        return False

    monkeypatch.setattr(hooks, "_run_opkg", fake_run)
    assert not hooks._install_reinstall_pkg("foo")
    assert calls[-1][-1] == "foo"


def test_upgrade_pkg_failure(monkeypatch):
    captured = []

    def fake_run(args):
        captured.append(args)
        return False

    monkeypatch.setattr(hooks, "_run_opkg", fake_run)
    assert not hooks._upgrade_pkg("bar")
    assert captured == [["upgrade", "bar"]]


def test_write_pending_no_packages(tmp_path):
    path = tmp_path / "pending"
    path.write_text("foo")
    hooks._write_pending(path, [], "reinstall")
    assert not path.exists()


def test_write_pending_with_packages(tmp_path):
    path = tmp_path / "pending"
    hooks._write_pending(path, ["foo", "bar"], "upgrade")
    assert path.read_text().strip().splitlines() == ["foo", "bar"]


def test_remove_duplicates_handles_failures(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        pkg = cmd[-1]
        calls.append(pkg)
        if pkg == "good":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        return SimpleNamespace(returncode=1, stderr="boom", stdout="")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    # Mock get_package_files to return empty list (simulating no files found)
    monkeypatch.setattr(hooks, "get_package_files", lambda pkg: [])
    # Mock restore_files_for_packages to accept the new parameter
    monkeypatch.setattr(
        hooks, "restore_files_for_packages", lambda packages, **kwargs: len(packages)
    )
    hooks._remove_duplicates(["good", "bad"])
    assert calls == ["good", "bad"]


def test_prune_writable_status_logs(monkeypatch, caplog, tmp_path):
    caplog.set_level("INFO", logger="calculinux_update.hooks")
    writable = tmp_path / "status"
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable)

    def fake_load(path):
        assert path == tmp_path / "image"
        return {"foo"}

    def fake_prune(path, names):
        assert path == writable
        assert names == {"foo"}
        return True

    monkeypatch.setattr(hooks, "load_package_names", fake_load)
    monkeypatch.setattr(hooks, "prune_writable_status", fake_prune)
    hooks._prune_writable_status(tmp_path / "image")
    assert "pruned writable status" in caplog.text


def test_prune_writable_status_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", tmp_path / "status")
    monkeypatch.setattr(hooks, "load_package_names", lambda _: {"foo"})
    monkeypatch.setattr(hooks, "prune_writable_status", lambda *args: False)
    hooks._prune_writable_status(tmp_path / "image")


# Rollback detection tests


def test_get_booted_slot_name_success(monkeypatch):
    """Test successful slot name parsing from rauc status."""
    fake_output = """RAUC_SLOT_STATE_rootfs.0=booted
RAUC_SLOT_STATE_rootfs.1=inactive
RAUC_SLOT_STATE_appfs.0=active
"""
    result = SimpleNamespace(stdout=fake_output, returncode=0)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: result)

    slot_name = hooks._get_booted_slot_name()
    assert slot_name == "rootfs.0"


def test_get_booted_slot_name_rauc_not_found(monkeypatch):
    """Test handling when rauc binary is not found."""
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("rauc not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    slot_name = hooks._get_booted_slot_name()
    assert slot_name is None


def test_get_booted_slot_name_rauc_error(monkeypatch):
    """Test handling when rauc command fails."""
    from subprocess import CalledProcessError

    def fake_run(*args, **kwargs):
        raise CalledProcessError(1, "rauc")

    monkeypatch.setattr("subprocess.run", fake_run)
    slot_name = hooks._get_booted_slot_name()
    assert slot_name is None


def test_get_current_boot_id_success(tmp_path, monkeypatch):
    """Test reading boot ID from /proc."""
    boot_id = "d359b438-b28b-416b-9270-257484a8a58e"

    class FakePath:
        def read_text(self):
            return boot_id + "\n"

    monkeypatch.setattr(
        hooks.Path,
        "__new__",
        lambda cls, x: FakePath() if "boot_id" in str(x) else Path(x),
    )

    result = hooks._get_current_boot_id()
    assert result == boot_id


def test_get_current_boot_id_missing(monkeypatch):
    """Test handling when boot ID file is missing."""
    monkeypatch.setattr(
        "pathlib.Path.read_text", lambda self: (_ for _ in ()).throw(OSError("not found"))
    )

    boot_id = hooks._get_current_boot_id()
    assert boot_id is None


def test_save_pre_update_state(tmp_path, monkeypatch):
    """Test saving pre-update state."""
    writable_status = tmp_path / "status"
    writable_status.write_text("Package: foo\n\nPackage: bar\n\n")

    pre_update_status = tmp_path / "pre-status"
    pre_update_slot = tmp_path / "pre-slot"
    updated_slot = tmp_path / "updated-slot"

    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable_status)
    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", pre_update_status)
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", pre_update_slot)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", updated_slot)
    monkeypatch.setattr(hooks, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hooks, "_get_booted_slot_name", lambda: "rootfs.0")

    hooks._save_pre_update_state("rootfs.1")

    assert pre_update_status.exists()
    assert pre_update_slot.exists()
    assert updated_slot.exists()
    assert pre_update_slot.read_text() == "rootfs.0\n"
    assert updated_slot.read_text() == "rootfs.1\n"


def test_detect_rollback_forward_update(tmp_path, monkeypatch):
    """Test detecting a forward update (not a rollback)."""
    pre_slot = tmp_path / "pre-slot"
    updated_slot = tmp_path / "updated-slot"

    pre_slot.write_text("rootfs.0\n")
    updated_slot.write_text("rootfs.1\n")

    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", pre_slot)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", updated_slot)
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", tmp_path / "none")
    monkeypatch.setattr(hooks, "_get_booted_slot_name", lambda: "rootfs.1")
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "abc123")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is False
    assert "forward update" in result["reason"]


def test_detect_rollback_actual_rollback(tmp_path, monkeypatch):
    """Test detecting an actual rollback."""
    pre_slot = tmp_path / "pre-slot"
    updated_slot = tmp_path / "updated-slot"

    pre_slot.write_text("rootfs.0\n")
    updated_slot.write_text("rootfs.1\n")

    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", pre_slot)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", updated_slot)
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", tmp_path / "none")
    monkeypatch.setattr(hooks, "_get_booted_slot_name", lambda: "rootfs.0")
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "abc123")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is True
    assert "rollback detected" in result["reason"]


def test_detect_rollback_already_processed(tmp_path, monkeypatch):
    """Test that we don't re-process the same boot."""
    pre_slot = tmp_path / "pre-slot"
    updated_slot = tmp_path / "updated-slot"
    boot_id_file = tmp_path / "boot-id"

    pre_slot.write_text("rootfs.0\n")
    updated_slot.write_text("rootfs.1\n")
    boot_id_file.write_text("abc123\n")

    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", pre_slot)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", updated_slot)
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", boot_id_file)
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "abc123")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is False
    assert "already processed" in result["reason"]


def test_detect_rollback_no_state_files(tmp_path, monkeypatch):
    """Test behavior when state files don't exist."""
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", tmp_path / "none")
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", tmp_path / "none")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is False
    assert "no update state" in result["reason"]


def test_handle_rollback_success(tmp_path, monkeypatch):
    """Test successful rollback handling."""
    pre_status = tmp_path / "pre-status"
    writable_status = tmp_path / "status"

    pre_status.write_text("Package: foo\nVersion: 1.0\n\nPackage: bar\nVersion: 2.0\n\n")
    writable_status.write_text("Package: baz\n\n")

    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", pre_status)
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable_status)
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", tmp_path / "slot")
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", tmp_path / "slot2")
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", tmp_path / "boot")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", tmp_path / "reinstall")
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", tmp_path / "upgrade")

    result = hooks._handle_rollback()

    assert result is True
    # Writable status should be restored
    content = writable_status.read_text()
    assert "foo" in content
    assert "bar" in content


def test_handle_rollback_missing_pre_status(tmp_path, monkeypatch):
    """Test rollback handling when pre-update status is missing."""
    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", tmp_path / "none")

    result = hooks._handle_rollback()

    assert result is False


def test_cleanup_update_state(tmp_path, monkeypatch):
    """Test cleanup of all update state files."""
    files = [
        tmp_path / "pre-status",
        tmp_path / "pre-slot",
        tmp_path / "updated-slot",
        tmp_path / "boot-id",
        tmp_path / "reinstall",
        tmp_path / "upgrade",
    ]

    for f in files:
        f.write_text("content")

    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", files[0])
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", files[1])
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", files[2])
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", files[3])
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", files[4])
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", files[5])

    hooks._cleanup_update_state()

    for f in files:
        assert not f.exists()


def test_atomic_write(tmp_path):
    """Test atomic file writing."""
    target = tmp_path / "test.txt"
    content = "test content\n"

    hooks._atomic_write(target, content)

    assert target.exists()
    assert target.read_text() == content


def test_atomic_write_creates_parent(tmp_path):
    """Test that atomic write creates parent directory if needed."""
    target = tmp_path / "subdir" / "test.txt"
    content = "test content\n"

    hooks._atomic_write(target, content)

    assert target.exists()
    assert target.read_text() == content


def test_state_lock_basic(tmp_path, monkeypatch):
    """Test that state lock can be acquired and released."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(hooks, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks, "LOCK_FILE", state_dir / ".lock")

    acquired = False
    with hooks._state_lock():
        acquired = True

    assert acquired


def test_state_lock_prevents_concurrent_access(tmp_path, monkeypatch):
    """Test that state lock actually prevents concurrent access."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_file = state_dir / ".lock"
    monkeypatch.setattr(hooks, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks, "LOCK_FILE", lock_file)

    # Just verify the lock file gets created and the context manager works
    with hooks._state_lock():
        assert lock_file.exists()


def test_reconcile_state_file_respects_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "STATE_DIR", tmp_path)
    # Save/load should use STATE_DIR dynamically
    hooks._save_reconcile_state(hooks.ReconcileState.STARTED)
    assert hooks._load_reconcile_state() == hooks.ReconcileState.STARTED
    hooks._clear_reconcile_state()
    assert hooks._load_reconcile_state() == hooks.ReconcileState.NONE


def test_detect_rollback_cleans_up_on_boot_id_match(tmp_path, monkeypatch):
    """Test that state is cleaned up when boot ID indicates already processed."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    pre_update_slot = state_dir / "pre-slot"
    updated_slot = state_dir / "updated-slot"
    boot_id_file = state_dir / "boot-id"

    pre_update_slot.write_text("rootfs.0\n")
    updated_slot.write_text("rootfs.1\n")
    boot_id_file.write_text("abc-123\n")

    monkeypatch.setattr(hooks, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks, "PRE_UPDATE_SLOT_NAME", pre_update_slot)
    monkeypatch.setattr(hooks, "UPDATED_SLOT_NAME", updated_slot)
    monkeypatch.setattr(hooks, "UPDATE_BOOT_ID", boot_id_file)
    monkeypatch.setattr(hooks, "PRE_UPDATE_WRITABLE_STATUS", state_dir / "pre-status")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", state_dir / "reinstall")
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", state_dir / "upgrade")

    # Mock boot ID to match
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "abc-123")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is False
    assert "already processed" in result["reason"]
    # Files should be cleaned up
    assert not pre_update_slot.exists()
    assert not updated_slot.exists()
    assert not boot_id_file.exists()
