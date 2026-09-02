import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import calculinux_update.bundle as bundle


def test_extract_bundle_extras_success(tmp_path, monkeypatch):
    bundle_path = tmp_path / "test.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        # cmd layout: unsquashfs -f -d <dest> <bundle> bundle-extras.tar.gz
        dest = Path(cmd[3])
        tarball_path = dest / "bundle-extras.tar.gz"

        # Create a tarball with the expected structure
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w:gz') as tar:
            # Create extras/opkg/status.image
            status_data = b"status"
            status_info = tarfile.TarInfo(name="extras/opkg/status.image")
            status_info.size = len(status_data)
            tar.addfile(status_info, io.BytesIO(status_data))

            # Create extras/opkg/etc/opkg directory marker
            etc_info = tarfile.TarInfo(name="extras/opkg/etc/opkg")
            etc_info.type = tarfile.DIRTYPE
            tar.addfile(etc_info)

        tarball_path.write_bytes(tar_buffer.getvalue())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is not None
    assert extras.image_status.read_text() == "status"
    extras.cleanup()
    assert not extras.root.exists()


def test_extract_bundle_extras_manifest_only(tmp_path, monkeypatch):
    bundle_path = tmp_path / "manifest-only.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        dest = Path(cmd[3])
        tarball_path = dest / "bundle-extras.tar.gz"
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
            data = b'MIN_CALCULINUX_VERSION="1.0.0"\n'
            info = tarfile.TarInfo(name="extras/version-manifest.env")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tarball_path.write_bytes(tar_buffer.getvalue())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is not None
    assert extras.image_status is None
    assert extras.version_manifest is not None
    assert "1.0.0" in extras.version_manifest.read_text()
    extras.cleanup()


def test_extract_bundle_extras_missing(tmp_path, monkeypatch):
    bundle_path = tmp_path / "missing.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        dest = Path(cmd[3])
        tarball_path = dest / "bundle-extras.tar.gz"

        # Create a tarball without status.image
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w:gz') as tar:
            # Create only the directory structure
            etc_info = tarfile.TarInfo(name="extras/opkg/etc/opkg")
            etc_info.type = tarfile.DIRTYPE
            tar.addfile(etc_info)

        tarball_path.write_bytes(tar_buffer.getvalue())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is None


def test_extract_bundle_extras_tarball_missing(tmp_path, monkeypatch):
    """Test when unsquashfs succeeds but tarball isn't in the bundle."""
    bundle_path = tmp_path / "no-tarball.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        # unsquashfs succeeds but doesn't create the tarball
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    extras = bundle.extract_bundle_extras(bundle_path)
    assert extras is None


def test_extract_bundle_extras_corrupted_tarball(tmp_path, monkeypatch):
    """Test when tarball exists but is corrupted."""
    bundle_path = tmp_path / "corrupted.raucb"
    bundle_path.write_text("dummy")

    def fake_run(cmd, **_):
        dest = Path(cmd[3])
        tarball_path = dest / "bundle-extras.tar.gz"
        # Create a corrupted tarball (not actually gzipped)
        tarball_path.write_text("not a valid tarball")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)

    try:
        bundle.extract_bundle_extras(bundle_path)
        assert False, "Expected BundleExtractionError"
    except bundle.BundleExtractionError as exc:
        assert "Failed to extract tarball" in str(exc)
