from pathlib import Path

import pytest

from calculinux_update.opkg import reconcile

KABI_OLD = "6.1.99-rockchip-standard"
KABI_NEW = "6.1.118-rockchip-standard"
SAME = {"CALCULINUX_CODENAME": "walnascar", "YOCTO_VERSION": "walnascar"}
NEXT = {"CALCULINUX_CODENAME": "wrynose", "YOCTO_VERSION": "wrynose"}


def write_status(path: Path, packages):
    chunks = [f"Package: {pkg}\nVersion: 1.0\n" for pkg in packages]
    path.write_text("\n".join(chunks) + "\n")


@pytest.fixture
def upper(monkeypatch):
    """Packages listed here have files in the upper layer."""
    with_files = set()
    monkeypatch.setattr(
        reconcile, "has_files_in_upper", lambda pkg, overlay=None: pkg in with_files
    )

    class NoOverlay:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(reconcile, "OverlayInfo", NoOverlay)
    return with_files


@pytest.fixture
def statuses(tmp_path):
    new_image = tmp_path / "new"
    writable = tmp_path / "writable"
    current = tmp_path / "current"
    write_status(new_image, ["base", "moved-in", "busybox", f"kernel-{KABI_OLD}"])
    write_status(current, ["base", "busybox", "dropped", "user-upgraded", f"kernel-{KABI_OLD}"])
    write_status(
        writable,
        [
            "moved-in",          # user installed, now in the image, files in upper
            "busybox",           # image entry leaked into writable status
            "dropped",           # image entry leaked; the new image dropped it
            "user-upgraded",     # image package the user upgraded in the overlay
            "dosbox-x",          # user installed, not in any image
            f"kernel-module-rtw89-core-{KABI_OLD}",
        ],
    )
    info = tmp_path / "info"
    info.mkdir()
    # Real installs have a .list file (possibly empty); leaked entries do not.
    for pkg in ["moved-in", "user-upgraded", "dosbox-x", f"kernel-module-rtw89-core-{KABI_OLD}"]:
        (info / f"{pkg}.list").write_text("")
    return new_image, writable, current, info


def plan_for(statuses, **kwargs):
    new_image, writable, current, info = statuses
    return reconcile.compute_reconcile_plan(
        new_image, writable, current_status=current, info_dir=info, **kwargs
    )


def test_same_release_same_kernel_reinstalls_nothing(statuses, upper):
    upper.update({"moved-in", "user-upgraded", "dosbox-x"})
    plan = plan_for(statuses, old_manifest=SAME, new_manifest=SAME)

    assert plan.duplicates == ["moved-in"]
    assert plan.status_only_duplicates == ["busybox"]
    assert plan.leaked == ["dropped"]
    assert plan.overlay == [
        "dosbox-x", f"kernel-module-rtw89-core-{KABI_OLD}", "user-upgraded"
    ]
    assert plan.reinstall == []
    assert not plan.release_change
    assert plan.kernel_abi == KABI_OLD
    assert plan.any_actions()


def test_release_change_reinstalls_all_overlay_packages(statuses, upper):
    upper.update({"user-upgraded"})
    plan = plan_for(statuses, old_manifest=SAME, new_manifest=NEXT)

    assert plan.release_change
    assert plan.reinstall == plan.overlay
    assert "dropped" not in plan.reinstall


def test_kernel_change_reinstalls_only_kernel_modules(statuses, upper):
    new_image = statuses[0]
    write_status(new_image, ["base", f"kernel-{KABI_NEW}"])
    plan = plan_for(statuses, old_manifest=SAME, new_manifest=SAME)

    assert plan.kernel_abi == KABI_NEW
    assert plan.reinstall == [f"kernel-module-rtw89-core-{KABI_OLD}"]


def test_without_current_status_only_listless_entries_leak(statuses, upper):
    new_image, writable, _current, info = statuses
    (info / "dropped.list").write_text("")
    plan = reconcile.compute_reconcile_plan(new_image, writable, info_dir=info)
    assert plan.leaked == []
    assert "dropped" in plan.overlay


def test_entry_without_list_or_files_is_leaked(statuses, upper):
    """Left over from an earlier image: not in the current one, no metadata."""
    new_image, writable, current, info = statuses
    write_status(current, ["base", f"kernel-{KABI_OLD}"])
    upper.update({"moved-in", "user-upgraded", "dosbox-x"})
    plan = plan_for(statuses)
    assert "dropped" in plan.leaked
    assert f"kernel-module-rtw89-core-{KABI_OLD}" in plan.overlay


def test_skipping_duplicate_classification(statuses, upper):
    plan = plan_for(statuses, classify_duplicates=False)
    assert plan.duplicates == [] and plan.status_only_duplicates == []
    assert plan.leaked == ["dropped", "user-upgraded"]


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (SAME, SAME, False),
        (SAME, NEXT, True),
        ({"CALCULINUX_CODENAME": "walnascar"}, {"CALCULINUX_CODENAME": ""}, False),
        ({}, NEXT, False),
        ({"YOCTO_VERSION": "a"}, {"YOCTO_VERSION": "b"}, True),
    ],
)
def test_is_release_change(old, new, expected):
    assert reconcile.is_release_change(old, new) is expected


def test_image_kernel_abi():
    assert reconcile.image_kernel_abi(["kernel-image-6.1", f"kernel-{KABI_OLD}", "x"]) == KABI_OLD
    assert reconcile.image_kernel_abi(["kernel-module-foo", "busybox"]) is None


def test_prune_writable_status(tmp_path):
    writable = tmp_path / "writable"
    write_status(writable, ["keep", "drop", "gone"])
    assert reconcile.prune_writable_status(writable, ["drop", "gone"])
    text = writable.read_text()
    assert "drop" not in text and "gone" not in text and "keep" in text
    assert not reconcile.prune_writable_status(writable, ["absent"])
