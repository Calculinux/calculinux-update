from pathlib import Path

from calculinux_update.version_compat import (
    CompatLevel,
    UpgradeType,
    check_compatibility,
    get_upgrade_type,
    load_version_manifest,
)


def test_load_version_manifest(tmp_path: Path):
    p = tmp_path / "manifest.env"
    p.write_text(
        "# comment\n"
        "CALCULINUX_VERSION=\"1.2.3\"\n"
        "CALCULINUX_CODENAME=walnascar\n"
        "\n"
    )
    m = load_version_manifest(p)
    assert m["CALCULINUX_VERSION"] == "1.2.3"
    assert m["CALCULINUX_CODENAME"] == "walnascar"


def test_get_upgrade_type():
    assert get_upgrade_type("1.0.0", "1.0.1") == UpgradeType.PATCH
    assert get_upgrade_type("1.0.0", "1.1.0") == UpgradeType.MINOR
    assert get_upgrade_type("1.9.0", "2.0.0") == UpgradeType.MAJOR
    assert get_upgrade_type("2.0.0", "1.9.0") == UpgradeType.DOWNGRADE


def test_check_compatibility_detects_major_kernel():
    old = {"CALCULINUX_VERSION": "1.0.0", "KERNEL_VERSION": "5.10.1", "PYTHON_VERSION": "3.11", "YOCTO_VERSION": "scarthgap", "CALCULINUX_CODENAME": "walnascar"}
    new = {"CALCULINUX_VERSION": "2.0.0", "KERNEL_VERSION": "6.1.0", "PYTHON_VERSION": "3.11", "YOCTO_VERSION": "scarthgap", "CALCULINUX_CODENAME": "walnascar"}
    r = check_compatibility(old, new)
    assert r.upgrade_type == UpgradeType.MAJOR
    assert any(i.category == "kernel" for i in r.issues)
    assert r.overall_level in (CompatLevel.MAJOR_ISSUES, CompatLevel.INCOMPATIBLE)

