import json
from types import SimpleNamespace

from calculinux_update import prefetch


def test_patch_opkg_conf(tmp_path):
    conf = tmp_path / "opkg.conf"
    conf.write_text("option lists_dir /tmp\n")
    offline_root = tmp_path / "offline"
    offline_root.mkdir()

    prefetch._patch_opkg_conf(conf, offline_root)
    data = conf.read_text()
    assert str(offline_root / "var/lib/opkg/lists") in data
    assert str(offline_root / "var/lib/opkg/status") in data


def test_write_state(tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "PREFETCH_STATE_FILE", tmp_path / "state.json")
    plan = SimpleNamespace(reinstall=["foo"], duplicates=[], upgrade=[])
    prefetch._write_state("sha", plan)
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["bundle"] == "sha"
    assert data["reinstall"] == ["foo"]


def test_prefetch_for_bundle(monkeypatch, tmp_path):
    bundle_path = tmp_path / "bundle.raucb"
    bundle_path.write_text("dummy")

    writable = tmp_path / "status"
    writable.write_text("Package: keep\n\n")
    monkeypatch.setattr(prefetch, "WRITABLE_STATUS", writable)
    monkeypatch.setattr(prefetch, "PREFETCH_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(prefetch, "PREFETCH_STATE_FILE", tmp_path / "state.json")

    image_status = tmp_path / "image.status"
    image_status.write_text("Package: keep\n\n")

    opkg_root = tmp_path / "extras"
    config_dir = opkg_root / "etc/opkg"
    config_dir.mkdir(parents=True)
    (config_dir / "opkg.conf").write_text("src/gz test https://example\n")

    extras_obj = SimpleNamespace(
        root=tmp_path,
        opkg_root=opkg_root,
        image_status=image_status,
        cleanup=lambda: None,
    )

    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras_obj)

    # Mock CURRENT_IMAGE_STATUS to provide current slot status
    current_status = tmp_path / "current.status"
    current_status.write_text("Package: keep\nPackage: foo\n\n")
    monkeypatch.setattr(prefetch, "CURRENT_IMAGE_STATUS", current_status)

    class FakeDownloader:
        def __init__(self, *_):
            pass

        def download(self, packages, cache_dir):
            assert packages == ["foo"]
            cache_dir.mkdir(parents=True, exist_ok=True)
            return len(packages)

    monkeypatch.setattr(prefetch, "OpkgDownloader", FakeDownloader)
    monkeypatch.setattr(
        prefetch,
        "compute_reconcile_plan",
        lambda **_: SimpleNamespace(reinstall=["foo"], duplicates=[], upgrade=[]),
    )

    result = prefetch.prefetch_for_bundle(bundle_path, "sha256", console=None)
    assert not result.skipped
    assert result.downloaded == 1


def test_prefetch_skips_without_writable(monkeypatch, tmp_path):
    bundle_path = tmp_path / "bundle.raucb"
    bundle_path.write_text("dummy")
    monkeypatch.setattr(prefetch, "WRITABLE_STATUS", tmp_path / "missing")
    extras_obj = SimpleNamespace(root=tmp_path, cleanup=lambda: None)
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras_obj)
    result = prefetch.prefetch_for_bundle(bundle_path, "sha", console=None)
    assert result.skipped


def test_opkg_downloader_missing_config(tmp_path):
    opkg_root = tmp_path / "extras"
    opkg_root.mkdir()
    downloader = prefetch.OpkgDownloader(opkg_root)
    try:
        downloader.download([], tmp_path / "cache")
    except prefetch.PrefetchError as exc:
        assert "bundle extras missing" in str(exc)
    else:
        raise AssertionError("expected PrefetchError")
