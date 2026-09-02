from pathlib import Path

from calculinux_update.version_compat import (
    CompatLevel,
    UpgradeType,
    check_compatibility,
    get_upgrade_type,
    load_version_manifest,
    version_meets_minimum,
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


def test_parse_version_ignores_prefix_and_suffix():
    assert get_upgrade_type("v1.0.0-alpha4", "1.0.0") == UpgradeType.PATCH
    assert get_upgrade_type("1.0.0-dev+f8b3f03", "1.0.0") == UpgradeType.PATCH
    assert get_upgrade_type("0.9.0-continuous+abc", "1.0.0") == UpgradeType.MAJOR


def test_min_calculinux_version_blocks_older():
    old = {"CALCULINUX_VERSION": "0.9.0-dev+abc"}
    new = {
        "CALCULINUX_VERSION": "2.0.0",
        "MIN_CALCULINUX_VERSION": "1.0.0",
    }
    r = check_compatibility(old, new)
    assert r.overall_level == CompatLevel.INCOMPATIBLE
    assert r.any_blockers()
    issue = next(i for i in r.issues if i.category == "min-version")
    assert "1.0.0" in issue.message
    assert "0.9.0-dev+abc" in issue.message


def test_min_calculinux_version_allows_equal_or_newer():
    new = {"CALCULINUX_VERSION": "2.0.0", "MIN_CALCULINUX_VERSION": "1.0.0"}
    assert not check_compatibility(
        {"CALCULINUX_VERSION": "1.0.0"}, new
    ).any_blockers()
    assert not check_compatibility(
        {"CALCULINUX_VERSION": "1.0.0-dev+deadbeef"}, new
    ).any_blockers()
    assert not check_compatibility(
        {"CALCULINUX_VERSION": "1.1.0"}, new
    ).any_blockers()


def test_version_meets_minimum_tail():
    assert version_meets_minimum("1.0.0-continuous+abc", "1.0.0-continuous+abc")
    assert not version_meets_minimum("1.0.0-dev+zzz", "1.0.0-continuous+abc")
    assert not version_meets_minimum(
        "1.0.0-continuous+def", "1.0.0-continuous+abc"
    )
    assert version_meets_minimum("1.1.0-dev+zzz", "1.0.0-continuous+abc")
    assert version_meets_minimum(
        "1.0.0-continuous+def",
        "1.0.0-continuous+abc",
        current_timestamp="2026-09-02T00:00:00Z",
        minimum_timestamp="2026-09-01T00:00:00Z",
    )
    assert not version_meets_minimum(
        "1.0.0-continuous+def",
        "1.0.0-continuous+abc",
        current_timestamp="2026-08-01T00:00:00Z",
        minimum_timestamp="2026-09-01T00:00:00Z",
    )


def test_min_calculinux_version_respects_tail():
    new = {
        "CALCULINUX_VERSION": "2.0.0",
        "MIN_CALCULINUX_VERSION": "1.0.0-continuous+abc123",
        "MIN_BUILD_TIMESTAMP": "2026-09-01T00:00:00Z",
    }
    assert check_compatibility(
        {"CALCULINUX_VERSION": "1.0.0-dev+old"}, new
    ).any_blockers()
    assert not check_compatibility(
        {"CALCULINUX_VERSION": "1.0.0-continuous+abc123"}, new
    ).any_blockers()
    assert not check_compatibility(
        {
            "CALCULINUX_VERSION": "1.0.0-continuous+ffff",
            "BUILD_TIMESTAMP": "2026-09-02T00:00:00Z",
        },
        new,
    ).any_blockers()


def test_min_calculinux_version_absent_or_empty_is_ok():
    old = {"CALCULINUX_VERSION": "0.1.0"}
    assert not check_compatibility(old, {"CALCULINUX_VERSION": "2.0.0"}).any_blockers()
    assert not check_compatibility(
        old, {"CALCULINUX_VERSION": "2.0.0", "MIN_CALCULINUX_VERSION": ""}
    ).any_blockers()


def test_check_compatibility_ranks_multiple_issues():
    old = {
        "CALCULINUX_VERSION": "1.0.0",
        "KERNEL_VERSION": "6.1.120",
        "PYTHON_VERSION": "3.13",
        "YOCTO_VERSION": "walnascar",
        "CALCULINUX_CODENAME": "walnascar",
    }
    new = {
        "CALCULINUX_VERSION": "1.0.0",
        "KERNEL_VERSION": "7.1.0",
        "PYTHON_VERSION": "3.14",
        "YOCTO_VERSION": "wrynose",
        "CALCULINUX_CODENAME": "wrynose",
    }
    r = check_compatibility(old, new)
    assert r.overall_level == CompatLevel.MAJOR_ISSUES
    assert {i.category for i in r.issues} >= {"kernel", "python", "abi", "feeds"}


def test_unparseable_and_pre_only_versions():
    assert get_upgrade_type("not-a-version", "1.0.0") == UpgradeType.MAJOR
    assert get_upgrade_type("1.0.0", "rel 9.0") == UpgradeType.MAJOR
    assert version_meets_minimum("1.0.0+gdeadbeef", "1.0.0+gdeadbeef")
    assert version_meets_minimum("1.0.0", "")


def test_load_version_manifest_missing_junk_and_unreadable(tmp_path: Path):
    assert load_version_manifest(tmp_path / "missing.env") == {}
    p = tmp_path / "manifest.env"
    p.write_text("NOEQUALS\nKEY=val\n")
    assert load_version_manifest(p) == {"KEY": "val"}
    d = tmp_path / "adir"
    d.mkdir()
    assert load_version_manifest(d) == {}
