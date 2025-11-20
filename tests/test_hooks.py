from pathlib import Path
from types import SimpleNamespace

import pytest

import calculinux_update.hooks as hooks


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

    mount = tmp_path / "slot"
    (mount / "var/lib/opkg").mkdir(parents=True)
    (mount / "var/lib/opkg/status.image").write_text("Package: base\n\n")

    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_SLOT_MOUNT_POINT", str(mount))

    pruned = {}
    monkeypatch.setattr(hooks, "prune_writable_status", lambda *_: pruned.setdefault("called", True))

    plan = hooks.ReconcilePlan(duplicates=["base"], reinstall=["foo"], upgrade=["bar"])
    monkeypatch.setattr(hooks, "compute_reconcile_plan", lambda **_: plan)

    recorded = {"duplicates": None, "reinstall": None, "upgrade": None}

    def fake_remove(pkgs):
        recorded["duplicates"] = pkgs

    def fake_write(path, packages, label):
        recorded[label] = list(packages)

    monkeypatch.setattr(hooks, "_remove_duplicates", fake_remove)
    monkeypatch.setattr(hooks, "_write_pending", fake_write)

    hooks.run_slot_hook("slot-post-install", "rootfs.0")

    assert recorded["duplicates"] == ["base"]
    assert recorded["reinstall"] == ["foo"]
    assert recorded["upgrade"] == ["bar"]


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
    monkeypatch.setenv("RAUC_SLOT_MOUNT_POINT", str(tmp_path / "noslot"))
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "missing" in caplog.text


def test_run_slot_hook_missing_writable_status(monkeypatch, tmp_path, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    mount = tmp_path / "slot"
    (mount / "var/lib/opkg").mkdir(parents=True)
    (mount / "var/lib/opkg/status.image").write_text("Package: base\n\n")
    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_SLOT_MOUNT_POINT", str(mount))
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", tmp_path / "missing-status")
    hooks.run_slot_hook("slot-post-install", "slot")
    assert "writable status" in caplog.text


def test_run_slot_hook_cleans_temp_snapshot(monkeypatch, tmp_path):
    writable = tmp_path / "status"
    writable.write_text("Package: overlay\n\n")
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable)

    mount = tmp_path / "slot"
    (mount / "var/lib/opkg").mkdir(parents=True)
    (mount / "var/lib/opkg/status.image").write_text("Package: base\n\n")

    snapshot = tmp_path / "snapshot"
    snapshot.write_text("snapshot")
    monkeypatch.setattr(hooks, "CURRENT_IMAGE_STATUS", tmp_path / "current-default")
    monkeypatch.setattr(hooks, "snapshot_current_slot_status", lambda: snapshot)

    monkeypatch.setenv("RAUC_SLOT_CLASS", "rootfs")
    monkeypatch.setenv("RAUC_SLOT_MOUNT_POINT", str(mount))

    plan = hooks.ReconcilePlan(duplicates=[], reinstall=["foo"], upgrade=[])
    monkeypatch.setattr(hooks, "compute_reconcile_plan", lambda **_: plan)
    monkeypatch.setattr(hooks, "_remove_duplicates", lambda *_: None)
    monkeypatch.setattr(hooks, "_write_pending", lambda *args, **kwargs: None)

    hooks.run_slot_hook("slot-post-install", "slot")
    assert not snapshot.exists()


def test_postreboot_entrypoint(monkeypatch, tmp_path):
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    upg = tmp_path / "upgrade"
    upg.write_text("bar\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", upg)

    calls = []

    monkeypatch.setattr(hooks, "_run_opkg", lambda args: True)
    monkeypatch.setattr(hooks, "_install_reinstall_pkg", lambda pkg: calls.append(("install", pkg)) or True)
    monkeypatch.setattr(hooks, "_upgrade_pkg", lambda pkg: calls.append(("upgrade", pkg)) or True)

    hooks.postreboot_entrypoint()
    assert calls == [("install", "foo"), ("upgrade", "bar")]
    assert not rein.exists()
    assert not upg.exists()


def test_postreboot_entrypoint_update_failure(monkeypatch, tmp_path):
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", tmp_path / "none")
    monkeypatch.setattr(hooks, "_run_opkg", lambda args: False)

    try:
        hooks.postreboot_entrypoint()
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit")


def test_postreboot_entrypoint_partial_failure(monkeypatch, tmp_path):
    rein = tmp_path / "reinstall"
    rein.write_text("foo\n")
    monkeypatch.setattr(hooks, "PENDING_REINSTALL_FILE", rein)
    monkeypatch.setattr(hooks, "PENDING_UPGRADE_FILE", tmp_path / "none")
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
