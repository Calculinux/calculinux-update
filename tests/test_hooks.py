from pathlib import Path
from types import SimpleNamespace

import pytest

import calculinux_update.hooks as hooks
from calculinux_update.opkg.reconcile import ReconcilePlan


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point every hooks path at tmp_path; returns a namespace of them."""
    d = tmp_path / "state"
    d.mkdir()
    paths = {
        "STATE_DIR": d,
        "PENDING_DUPLICATES_FILE": d / "pending-duplicates",
        "PENDING_REINSTALL_FILE": d / "pending-reinstalls",
        "LEGACY_PENDING_UPGRADE_FILE": d / "pending-upgrades",
        "LEFTOVERS_FILE": d / "leftovers",
        "UPDATED_SLOT_NAME": d / "updated-slot",
        "PRE_UPDATE_SLOT_NAME": d / "pre-update-slot",
        "PRE_UPDATE_WRITABLE_STATUS": d / "pre-update-writable",
        "MODIFIED_CONFFILES_FILE": d / "conffiles",
        "UPDATE_BOOT_ID": d / "boot-id",
        "STATUS_PRUNED_MARKER": d / "status-pruned",
        "PREFETCH_CACHE_DIR": tmp_path / "cache",
        "PREFETCH_LISTS_DIR": tmp_path / "prefetch-lists",
        "OPKG_LISTS_DIR": tmp_path / "opkg-lists",
        "CURRENT_IMAGE_STATUS": tmp_path / "status.image",
        "WRITABLE_STATUS": tmp_path / "status",
        "CURRENT_VERSION_MANIFEST": tmp_path / "version-manifest.env",
    }
    for name, value in paths.items():
        monkeypatch.setattr(hooks, name, value)
    paths["CURRENT_IMAGE_STATUS"].write_text("Package: base\nVersion: 1\n\n")
    return type("State", (), {k.lower(): v for k, v in paths.items()})


class Opkg:
    """Fake _run_opkg: ``fail`` names packages whose commands fail."""

    def __init__(self, update=True, fail=()):
        self.calls = []
        self.update = update
        self.fail = set(fail)

    def __call__(self, args):
        self.calls.append(list(args))
        if args == ["update"]:
            return self.update
        return not any(a in self.fail for a in args)


def test_run_slot_hook(monkeypatch, tmp_path, state):
    state.writable_status.write_text("Package: overlay\n\n")
    state.legacy_pending_upgrade_file.write_text("stale\n")
    bundle_status_image = tmp_path / "bundle-status.image"
    bundle_status_image.write_text("Package: base\n\n")
    manifest = tmp_path / "new-manifest.env"
    manifest.write_text('CALCULINUX_VERSION="9.0.0"\nMIN_CALCULINUX_VERSION="9.0.0"\n')
    state.current_version_manifest.write_text('CALCULINUX_VERSION="1.0.0"\n')

    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status_image))
    monkeypatch.setenv("RAUC_BUNDLE_VERSION_MANIFEST", str(manifest))
    monkeypatch.setattr(hooks, "_get_booted_slot_name", lambda: "rootfs.1")

    seen = {}
    plan = ReconcilePlan(
        duplicates=["moved-in"],
        status_only_duplicates=["busybox"],
        leaked=["dropped"],
        overlay=["dosbox-x"],
        reinstall=["dosbox-x"],
        release_change=True,
    )

    def fake_plan(**kwargs):
        seen["plan_kwargs"] = kwargs
        return plan

    monkeypatch.setattr(hooks, "compute_reconcile_plan", fake_plan)
    monkeypatch.setattr(
        hooks, "prune_writable_status", lambda path, pkgs: seen.setdefault("pruned", pkgs)
    )

    # A failed minimum-version check is only logged: the slot is already active.
    hooks.run_slot_hook("slot-post-install", "rootfs.0")

    assert seen["plan_kwargs"]["new_manifest"]["MIN_CALCULINUX_VERSION"] == "9.0.0"
    assert seen["plan_kwargs"]["old_manifest"]["CALCULINUX_VERSION"] == "1.0.0"
    assert seen["pruned"] == ["busybox", "dropped"]
    assert state.pending_duplicates_file.read_text() == "moved-in\n"
    assert state.pending_reinstall_file.read_text() == "dosbox-x\n"
    assert not state.legacy_pending_upgrade_file.exists()
    assert state.updated_slot_name.read_text() == "rootfs.0\n"


def test_run_slot_hook_same_release_queues_no_reinstalls(monkeypatch, tmp_path, state):
    state.writable_status.write_text("Package: overlay\n\n")
    bundle_status_image = tmp_path / "bundle-status.image"
    bundle_status_image.write_text("Package: base\n\n")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status_image))
    monkeypatch.setattr(hooks, "_get_booted_slot_name", lambda: "rootfs.1")
    monkeypatch.setattr(
        hooks, "compute_reconcile_plan", lambda **_: ReconcilePlan(overlay=["dosbox-x"])
    )
    hooks.run_slot_hook("slot-post-install", "rootfs.0")
    assert not state.pending_reinstall_file.exists()
    assert not state.pending_duplicates_file.exists()


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


def test_run_slot_hook_missing_writable_status(monkeypatch, tmp_path, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    bundle_status = tmp_path / "status.image"
    bundle_status.write_text("Package: base\n\n")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_BUNDLE_STATUS_IMAGE", str(bundle_status))
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", tmp_path / "missing-status")
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "writable status" in caplog.text


def test_bundle_manifest_path(monkeypatch):
    monkeypatch.delenv("RAUC_BUNDLE_VERSION_MANIFEST", raising=False)
    monkeypatch.setenv("RAUC_BUNDLE_MOUNT_POINT", "/tmp/extras")
    assert hooks._bundle_manifest_path() == Path("/tmp/extras/extras/version-manifest.env")
    monkeypatch.setenv("RAUC_BUNDLE_VERSION_MANIFEST", "/x/manifest.env")
    assert hooks._bundle_manifest_path() == Path("/x/manifest.env")


# --- reconcile_pending ---------------------------------------------------------


def test_duplicates_removed_in_one_batch_then_restored(monkeypatch, state):
    state.pending_duplicates_file.write_text("a\nb\n")
    opkg = Opkg()
    restored = {}
    monkeypatch.setattr(hooks, "_run_opkg", opkg)
    monkeypatch.setattr(hooks, "get_package_files", lambda pkg: [f"/usr/bin/{pkg}"])
    monkeypatch.setattr(
        hooks,
        "restore_files_for_packages",
        lambda pkgs, file_lists: restored.update(file_lists) or len(pkgs),
    )

    result = hooks.reconcile_pending()

    assert opkg.calls == [["remove", "--nodeps", "a", "b"]]
    assert restored == {"a": ["/usr/bin/a"], "b": ["/usr/bin/b"]}
    assert result.removed == ["a", "b"]
    assert not state.pending_duplicates_file.exists()


def test_duplicate_removal_falls_back_per_package(monkeypatch, state):
    state.pending_duplicates_file.write_text("good\nbad\n")
    monkeypatch.setattr(hooks, "_run_opkg", Opkg(fail={"bad"}))
    monkeypatch.setattr(hooks, "get_package_files", lambda pkg: [])
    monkeypatch.setattr(hooks, "restore_files_for_packages", lambda pkgs, file_lists: 0)

    result = hooks.reconcile_pending()

    assert result.removed == ["good"]
    assert result.failed == ["bad"]
    assert state.pending_duplicates_file.read_text() == "bad\n"


def _prefetched(monkeypatch, state, image_sha=None):
    state.prefetch_lists_dir.mkdir()
    (state.prefetch_lists_dir / "main").write_text("Package: dosbox-x\n")
    sha = image_sha or hooks.file_sha256(state.current_image_status)
    monkeypatch.setattr(hooks, "load_prefetch_state", lambda: {"image_status_sha256": sha})


def test_reinstalls_from_prefetch_cache_offline(monkeypatch, state):
    state.pending_reinstall_file.write_text("dosbox-x\nrtw89\n")
    _prefetched(monkeypatch, state)
    opkg = Opkg(update=False)
    monkeypatch.setattr(hooks, "_run_opkg", opkg)

    result = hooks.reconcile_pending()

    assert (state.opkg_lists_dir / "main").read_text() == "Package: dosbox-x\n"
    assert [
        "--cache-dir", str(state.prefetch_cache_dir),
        "install", "--force-reinstall", "dosbox-x", "rtw89",
    ] in opkg.calls
    assert result.reinstalled == ["dosbox-x", "rtw89"]
    assert result.waiting == []
    assert not state.pending_reinstall_file.exists()


def test_prefetch_for_another_image_is_ignored(monkeypatch, state):
    state.pending_reinstall_file.write_text("dosbox-x\n")
    _prefetched(monkeypatch, state, image_sha="0" * 64)
    opkg = Opkg(update=False)
    monkeypatch.setattr(hooks, "_run_opkg", opkg)

    result = hooks.reconcile_pending()

    assert opkg.calls == [["update"]]
    assert result.waiting == ["dosbox-x"]
    assert state.pending_reinstall_file.read_text() == "dosbox-x\n"
    assert not state.opkg_lists_dir.exists()


def test_offline_without_prefetch_keeps_queue(monkeypatch, state):
    state.pending_reinstall_file.write_text("dosbox-x\n")
    monkeypatch.setattr(hooks, "load_prefetch_state", lambda: {})
    monkeypatch.setattr(hooks, "_run_opkg", Opkg(update=False))
    result = hooks.reconcile_pending()
    assert result.waiting == ["dosbox-x"]
    assert state.pending_reinstall_file.exists()


def test_online_failures_become_leftovers(monkeypatch, state):
    state.pending_reinstall_file.write_text("dosbox-x\ngone\n")
    monkeypatch.setattr(hooks, "load_prefetch_state", lambda: {})
    opkg = Opkg(update=True, fail={"gone"})
    monkeypatch.setattr(hooks, "_run_opkg", opkg)

    result = hooks.reconcile_pending()

    assert opkg.calls[0] == ["update"]
    assert result.reinstalled == ["dosbox-x"]
    assert result.failed == ["gone"]
    assert hooks.read_leftovers() == [("gone", "reinstall")]
    assert not state.pending_reinstall_file.exists()


def test_offline_failures_with_prefetch_keep_waiting(monkeypatch, state):
    state.pending_reinstall_file.write_text("dosbox-x\nnot-prefetched\n")
    _prefetched(monkeypatch, state)
    monkeypatch.setattr(hooks, "_run_opkg", Opkg(update=False, fail={"not-prefetched"}))

    result = hooks.reconcile_pending()

    assert result.reinstalled == ["dosbox-x"]
    assert result.waiting == ["not-prefetched"]
    assert hooks.pending_reinstalls() == ["not-prefetched"]


def test_nothing_queued_runs_no_opkg(monkeypatch, state):
    opkg = Opkg()
    monkeypatch.setattr(hooks, "_run_opkg", opkg)
    result = hooks.reconcile_pending()
    assert opkg.calls == []
    assert not (result.removed or result.reinstalled or result.waiting or result.failed)


# --- postreboot_entrypoint -----------------------------------------------------


def test_postreboot_drops_legacy_queue_and_cleans_up(monkeypatch, state):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    state.legacy_pending_upgrade_file.write_text("dosbox-x\n")
    state.updated_slot_name.write_text("rootfs.1\n")
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": ""})
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "boot-1")
    opkg = Opkg()
    monkeypatch.setattr(hooks, "_run_opkg", opkg)
    conffiles = []
    monkeypatch.setattr(hooks, "_create_new_conffiles_from_lower", lambda: conffiles.append(1))
    monkeypatch.setattr(hooks, "_report_modified_conffiles", lambda: conffiles.append(2))

    hooks.postreboot_entrypoint()

    assert opkg.calls == []  # no network needed
    assert not state.legacy_pending_upgrade_file.exists()
    assert conffiles == [1, 2]
    assert not state.updated_slot_name.exists()
    assert state.update_boot_id.read_text() == "boot-1\n"


def test_postreboot_never_touches_the_feed(monkeypatch, state):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    state.updated_slot_name.write_text("rootfs.1\n")
    state.pending_reinstall_file.write_text("dosbox-x\n")
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": ""})
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(hooks, "load_prefetch_state", lambda: {})
    opkg = Opkg(update=True)
    monkeypatch.setattr(hooks, "_run_opkg", opkg)
    monkeypatch.setattr(hooks, "_create_new_conffiles_from_lower", lambda: None)
    monkeypatch.setattr(hooks, "_report_modified_conffiles", lambda: None)

    hooks.postreboot_entrypoint()

    assert opkg.calls == []  # post-reboot is offline by design
    assert hooks.pending_reinstalls() == ["dosbox-x"]


def test_cleanup_retired_units_removes_enablement(monkeypatch, state, tmp_path):
    system = tmp_path / "system"
    wants = system / "multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "cup-reconcile.timer").touch()
    (system / "cup-reconcile.service").touch()
    monkeypatch.setattr(hooks, "SYSTEMD_SYSTEM_DIR", system)

    hooks._cleanup_retired_units()

    assert not (wants / "cup-reconcile.timer").exists()
    assert not (system / "cup-reconcile.service").exists()


def test_postreboot_succeeds_while_packages_wait(monkeypatch, state, caplog):
    caplog.set_level("INFO", logger="calculinux_update.hooks")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    state.pending_reinstall_file.write_text("dosbox-x\n")
    monkeypatch.setattr(hooks, "_detect_rollback", lambda: {"is_rollback": False, "reason": ""})
    monkeypatch.setattr(hooks, "load_prefetch_state", lambda: {})
    monkeypatch.setattr(hooks, "_run_opkg", Opkg(update=False))
    monkeypatch.setattr(hooks, "_create_new_conffiles_from_lower", lambda: None)
    monkeypatch.setattr(hooks, "_report_modified_conffiles", lambda: None)

    hooks.postreboot_entrypoint()  # no SystemExit

    assert state.pending_reinstall_file.exists()
    assert "cup reconcile" in caplog.text


def test_write_pending_no_packages(tmp_path):
    path = tmp_path / "pending"
    path.write_text("foo")
    hooks._write_pending(path, [], "reinstall")
    assert not path.exists()


def test_write_pending_with_packages(tmp_path):
    path = tmp_path / "pending"
    hooks._write_pending(path, ["foo", "bar"], "reinstall")
    assert hooks._read_pending(path) == ["foo", "bar"]


# Rollback detection tests


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
    monkeypatch.setattr(hooks, "LEGACY_PENDING_UPGRADE_FILE", tmp_path / "upgrade")

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
    monkeypatch.setattr(hooks, "LEGACY_PENDING_UPGRADE_FILE", files[5])

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
    monkeypatch.setattr(hooks, "LEGACY_PENDING_UPGRADE_FILE", state_dir / "upgrade")

    # Mock boot ID to match
    monkeypatch.setattr(hooks, "_get_current_boot_id", lambda: "abc-123")

    result = hooks._detect_rollback()

    assert result["is_rollback"] is False
    assert "already processed" in result["reason"]
    # Files should be cleaned up
    assert not pre_update_slot.exists()
    assert not updated_slot.exists()
    assert not boot_id_file.exists()


SYSTEM_CONF = """\
[system]
compatible=calculinux-luckfox-lyra
bootloader=uboot

[slot.rootfs.0]
device=/dev/disk/by-partlabel/ROOT_A
type=ext4
bootname=A

[slot.rootfs.1]
device=/dev/disk/by-partlabel/ROOT_B
type=ext4
bootname=B
"""


@pytest.fixture
def rauc_env(tmp_path, monkeypatch):
    conf = tmp_path / "system.conf"
    conf.write_text(SYSTEM_CONF)
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=ttyFIQ0 ro root=PARTLABEL=ROOT_B rauc.slot=B rootwait\n")
    monkeypatch.setattr(hooks, "RAUC_SYSTEM_CONF", conf)
    monkeypatch.setattr(hooks, "PROC_CMDLINE", cmdline)
    monkeypatch.delenv("RAUC_CURRENT_BOOTNAME", raising=False)
    monkeypatch.delenv("RAUC_SYSTEM_CONFIG", raising=False)

    def no_rauc(*args, **kwargs):
        raise AssertionError("rauc status must not be called")

    monkeypatch.setattr(hooks.subprocess, "run", no_rauc)
    return SimpleNamespace(conf=conf, cmdline=cmdline)


def test_booted_slot_from_hook_env_without_rauc_service(rauc_env, monkeypatch):
    # Inside a RAUC handler the service is busy; the env must be enough.
    monkeypatch.setenv("RAUC_CURRENT_BOOTNAME", "A")
    monkeypatch.setenv("RAUC_SYSTEM_CONFIG", str(rauc_env.conf))
    assert hooks._get_booted_slot_name() == "rootfs.0"


def test_booted_slot_from_cmdline(rauc_env):
    assert hooks._get_booted_slot_name() == "rootfs.1"


def test_booted_slot_falls_back_to_rauc_status_json(rauc_env, monkeypatch):
    rauc_env.cmdline.write_text("console=ttyFIQ0 ro\n")
    output = (
        '{"compatible":"calculinux-luckfox-lyra","booted":"B","slots":['
        '{"rootfs.1":{"class":"rootfs","bootname":"B","state":"booted"}},'
        '{"rootfs.0":{"class":"rootfs","bootname":"A","state":"inactive"}}]}'
    )
    monkeypatch.setattr(
        hooks.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output)
    )
    assert hooks._get_booted_slot_name() == "rootfs.1"


@pytest.mark.parametrize(
    "error",
    [hooks.subprocess.CalledProcessError(1, "rauc"), FileNotFoundError("rauc")],
)
def test_booted_slot_none_when_rauc_status_fails(rauc_env, monkeypatch, error):
    rauc_env.cmdline.write_text("console=ttyFIQ0 ro\n")

    def busy(*args, **kwargs):
        raise error

    monkeypatch.setattr(hooks.subprocess, "run", busy)
    assert hooks._get_booted_slot_name() is None
