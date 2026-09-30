import json
import subprocess
from types import SimpleNamespace

import pytest

from calculinux_update import prefetch


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "PREFETCH_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(prefetch, "PREFETCH_LISTS_DIR", tmp_path / "lists")
    monkeypatch.setattr(prefetch, "PREFETCH_STATE_FILE", tmp_path / "state.json")
    writable = tmp_path / "status"
    writable.write_text("Package: dosbox-x\nVersion: 1\n\n")
    monkeypatch.setattr(prefetch, "WRITABLE_STATUS", writable)
    monkeypatch.setattr(prefetch, "CURRENT_IMAGE_STATUS", tmp_path / "current.status")
    monkeypatch.setattr(prefetch, "CURRENT_VERSION_MANIFEST", tmp_path / "missing.env")
    return tmp_path


@pytest.fixture
def extras(tmp_path):
    opkg_root = tmp_path / "extras"
    (opkg_root / "etc/opkg").mkdir(parents=True)
    (opkg_root / "etc/opkg/opkg.conf").write_text("src/gz main https://feed\ndest root /\n")
    image_status = tmp_path / "image.status"
    image_status.write_text("Package: base\nVersion: 1\n\n")
    return SimpleNamespace(
        root=tmp_path,
        opkg_root=opkg_root,
        image_status=image_status,
        version_manifest=None,
        cleanup=lambda: None,
    )


class FakeOpkg:
    """Records opkg invocations; ``fail`` names packages that cannot be fetched."""

    def __init__(self, fail=(), update_rc=0):
        self.calls = []
        self.fail = set(fail)
        self.update_rc = update_rc

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        root = argv[argv.index("--offline-root") + 1]
        args = argv[argv.index("--host-cache-dir") + 1:]
        if args == ["update"]:
            lists = f"{root}/{prefetch.LISTS_DIR}"
            with open(f"{lists}/main", "w") as fh:
                fh.write("Package: dosbox-x\n")
            return subprocess.CompletedProcess(argv, self.update_rc, "", "no network")
        packages = [a for a in args if not a.startswith("--") and a != "install"]
        rc = 1 if self.fail & set(packages) else 0
        return subprocess.CompletedProcess(argv, rc, "", "")


def test_downloader_runs_opkg_against_an_offline_image_root(paths, extras, monkeypatch):
    fake = FakeOpkg()
    seen = {}

    def run(argv, **kwargs):
        root = argv[argv.index("--offline-root") + 1]
        if not seen:
            seen["status"] = open(f"{root}/{prefetch.STATUS_FILE}").read()
            seen["image"] = open(f"{root}/{prefetch.IMAGE_STATUS_FILE}").read()
            seen["conf"] = open(f"{root}/etc/opkg/opkg.conf").read()
        return fake(argv, **kwargs)

    monkeypatch.setattr(prefetch.subprocess, "run", run)
    downloader = prefetch.OpkgDownloader(extras.opkg_root, extras.image_status)
    missing = downloader.download(["dosbox-x", "rtw89"], paths / "cache", paths / "lists")

    assert missing == []
    assert seen["status"] == ""
    assert seen["image"] == extras.image_status.read_text()
    assert seen["conf"] == (extras.opkg_root / "etc/opkg/opkg.conf").read_text()
    update, install = fake.calls
    assert update[:2] == ["opkg", "--conf"]
    assert update[update.index("--cache-dir") + 1] == str(paths / "cache")
    assert update[-2:] == ["--host-cache-dir", "update"]
    assert install[-5:] == [
        "install", "--download-only", "--force-reinstall", "dosbox-x", "rtw89"
    ]
    assert (paths / "lists" / "main").read_text() == "Package: dosbox-x\n"


def test_downloader_reports_packages_it_cannot_fetch(paths, extras, monkeypatch):
    fake = FakeOpkg(fail={"gone"})
    monkeypatch.setattr(prefetch.subprocess, "run", fake)
    downloader = prefetch.OpkgDownloader(extras.opkg_root, extras.image_status)
    missing = downloader.download(["dosbox-x", "gone"], paths / "cache", paths / "lists")
    assert missing == ["gone"]
    # batch, then one attempt per package
    assert len(fake.calls) == 4


def test_downloader_update_failure(paths, extras, monkeypatch):
    monkeypatch.setattr(prefetch.subprocess, "run", FakeOpkg(update_rc=1))
    downloader = prefetch.OpkgDownloader(extras.opkg_root, extras.image_status)
    with pytest.raises(prefetch.PrefetchError, match="opkg update failed"):
        downloader.download(["dosbox-x"], paths / "cache", paths / "lists")


def test_downloader_missing_config(tmp_path):
    downloader = prefetch.OpkgDownloader(tmp_path / "none", tmp_path / "image")
    with pytest.raises(prefetch.PrefetchError, match="bundle extras missing"):
        downloader.download([], tmp_path / "cache", tmp_path / "lists")


def _plan(reinstall, release_change=False):
    return lambda **_: SimpleNamespace(reinstall=reinstall, release_change=release_change)


def test_prefetch_nothing_to_reinstall(paths, extras, monkeypatch):
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras)
    monkeypatch.setattr(prefetch, "compute_reconcile_plan", _plan([]))
    (paths / "cache").mkdir()
    (paths / "cache" / "stale.ipk").write_text("x")

    result = prefetch.prefetch_for_bundle(paths / "b.raucb", "sha", console=None)

    assert result.skipped and "no overlay packages" in result.reason
    assert not (paths / "cache").exists()
    state = json.loads((paths / "state.json").read_text())
    assert state["packages"] == [] and state["bundle"] == "sha"
    assert state["image_status_sha256"] == prefetch.file_sha256(extras.image_status)


def test_prefetch_downloads_reinstalls(paths, extras, monkeypatch):
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras)
    monkeypatch.setattr(prefetch, "compute_reconcile_plan", _plan(["dosbox-x", "gone"], True))

    class Downloader:
        def __init__(self, opkg_root, image_status):
            assert image_status == extras.image_status

        def download(self, packages, cache_dir, lists_out):
            return ["gone"]

    monkeypatch.setattr(prefetch, "OpkgDownloader", Downloader)
    result = prefetch.prefetch_for_bundle(paths / "b.raucb", "sha", console=None)

    assert not result.skipped
    assert result.packages == ["dosbox-x", "gone"]
    assert result.missing == ["gone"]
    assert result.release_change
    state = prefetch.load_state(paths / "state.json")
    assert state["missing"] == ["gone"]


def test_prefetch_offline_marks_everything_missing(paths, extras, monkeypatch):
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras)
    monkeypatch.setattr(prefetch, "compute_reconcile_plan", _plan(["dosbox-x"]))

    class Downloader:
        def __init__(self, *_):
            pass

        def download(self, *_):
            raise prefetch.PrefetchError("opkg update failed: no network")

    monkeypatch.setattr(prefetch, "OpkgDownloader", Downloader)
    result = prefetch.prefetch_for_bundle(paths / "b.raucb", "sha", console=None)
    assert result.skipped and result.missing == ["dosbox-x"]


def test_prefetch_skips_without_writable(paths, extras, monkeypatch):
    monkeypatch.setattr(prefetch, "WRITABLE_STATUS", paths / "missing")
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras)
    result = prefetch.prefetch_for_bundle(paths / "b.raucb", "sha", console=None)
    assert result.skipped


def test_prefetch_without_image_status(paths, extras, monkeypatch):
    extras.image_status = None
    monkeypatch.setattr(prefetch, "extract_bundle_extras", lambda *_: extras)
    result = prefetch.prefetch_for_bundle(paths / "b.raucb", "sha", console=None)
    assert result.skipped and "status.image" in result.reason


def test_load_state_tolerates_garbage(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json")
    assert prefetch.load_state(bad) == {}
    assert prefetch.load_state(tmp_path / "missing.json") == {}
