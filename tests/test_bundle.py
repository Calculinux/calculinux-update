from pathlib import Path
from types import SimpleNamespace

import calculinux_update.bundle as bundle


def test_extract_bundle_extras_success(tmp_path, monkeypatch):
    bundle_path = tmp_path / "test.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        # cmd layout: unsquashfs -f -d <dest> <bundle> extras/opkg
        dest = Path(cmd[3])
        extracted = dest / bundle.EXTRAS_DIR
        (extracted / "etc/opkg").mkdir(parents=True)
        (extracted / "status.image").write_text("status")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is not None
    assert extras.image_status.read_text() == "status"
    extras.cleanup()
    assert not extras.root.exists()


def test_extract_bundle_extras_missing(tmp_path, monkeypatch):
    bundle_path = tmp_path / "missing.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        dest = Path(cmd[3])
        # create incomplete tree (no status.image)
        (dest / bundle.EXTRAS_DIR / "etc/opkg").mkdir(parents=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is None
