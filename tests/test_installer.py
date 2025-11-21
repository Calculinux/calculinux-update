import hashlib

import httpx
import pytest

from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.installer import ChecksumMismatchError, UpdateInstaller
from calculinux_update.mirror import BundleInfo


@pytest.fixture()
def installer(tmp_path):
    config = UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )
    return UpdateInstaller(config, max_attempts=1)


def dummy_bundle(channel):
    return BundleInfo(name="bundle.raucb", url="https://example.com/bundle.raucb", channel=channel)


def test_format_size(installer):
    assert installer.format_size(512) == "512B"
    assert installer.format_size(2048) == "2.0KB"
    assert installer.format_size(3 * 1024 * 1024) == "3.0MB"


def test_ensure_binary_missing(monkeypatch):
    monkeypatch.setattr("calculinux_update.installer.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError):
        UpdateInstaller.ensure_binary_available("rauc")


def test_run_rauc_install_dry_run(installer, monkeypatch, capsys):
    bundle = dummy_bundle(installer.config.channels[0])
    path = installer.config.download_dir / bundle.name

    def fail_run(*_, **__):
        raise AssertionError("should not run")

    monkeypatch.setattr("calculinux_update.installer.subprocess.run", fail_run)

    installer.run_rauc_install(path, dry_run=True)
    captured = capsys.readouterr()
    # Should indicate it's a dry run somehow
    assert "dry" in captured.out.lower() or "would" in captured.out.lower()


def test_run_rauc_install_invokes_subprocess(installer, monkeypatch):
    bundle = dummy_bundle(installer.config.channels[0])
    path = installer.config.download_dir / bundle.name
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)

    monkeypatch.setattr("calculinux_update.installer.subprocess.run", fake_run)
    monkeypatch.setattr("calculinux_update.installer.os.geteuid", lambda: 1000)

    installer.run_rauc_install(path, sudo=True)
    # When sudo is requested as non-root, should use sudo
    assert calls and "sudo" in calls[0]


class StreamStub:
    def __init__(self, data: bytes, headers: dict | None = None, *, status_code: int = 200):
        self._data = data
        self.headers = headers or {"Content-Length": str(len(data))}
        self.status_code = status_code

    def __enter__(self):  # pragma: no cover - trivial
        return self

    def __exit__(self, exc_type, exc, tb):  # pragma: no cover - trivial
        return False

    def iter_bytes(self):
        yield self._data

    def raise_for_status(self):  # pragma: no cover - trivial
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


def test_download_streams_and_validates_checksum(monkeypatch, installer):
    data = b"abc123"
    expected = hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(
        "calculinux_update.installer.httpx.stream",
        lambda *_args, **_kwargs: StreamStub(data),
    )

    bundle = dummy_bundle(installer.config.channels[0])
    result = installer.download(bundle, expected_sha256=expected)

    assert result.sha256 == expected
    assert result.path.exists()


def test_download_reuses_existing_file(monkeypatch, installer):
    bundle = dummy_bundle(installer.config.channels[0])
    path = installer.config.download_dir / bundle.name
    data = b"cached"
    expected = hashlib.sha256(data).hexdigest()
    path.write_bytes(data)

    def fail_stream(*_args, **_kwargs):
        raise AssertionError("network should not be used")

    monkeypatch.setattr("calculinux_update.installer.httpx.stream", fail_stream)

    result = installer.download(bundle, expected_sha256=expected)
    assert result.path == path


def test_download_raises_on_checksum_mismatch(monkeypatch, installer):
    data = b"abc"
    monkeypatch.setattr(
        "calculinux_update.installer.httpx.stream",
        lambda *_args, **_kwargs: StreamStub(data),
    )

    bundle = dummy_bundle(installer.config.channels[0])
    with pytest.raises(ChecksumMismatchError):
        installer.download(bundle, expected_sha256="deadbeef")


def test_download_resume_uses_range_header(monkeypatch, tmp_path):
    config = UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )
    installer = UpdateInstaller(config, resume_downloads=True, max_attempts=1)
    bundle = dummy_bundle(config.channels[0])
    partial = installer.config.download_dir / (bundle.name + ".part")
    partial.write_bytes(b"abc")

    headers_seen = {}

    class RangeStream(StreamStub):
        def __init__(self, data: bytes):
            super().__init__(data, headers={"Content-Length": str(len(data))}, status_code=206)

    def fake_stream(method, url, *, headers=None, **kwargs):
        headers_seen.update(headers or {})
        return RangeStream(b"def")

    monkeypatch.setattr("calculinux_update.installer.httpx.stream", fake_stream)

    result = installer.download(bundle, expected_sha256=None)
    # Should request range starting from existing file size
    assert "Range" in headers_seen
    assert headers_seen["Range"].startswith("bytes=")
    assert result.path.read_bytes() == b"abcdef"


def test_download_restarts_when_range_ignored(monkeypatch, tmp_path):
    config = UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )
    installer = UpdateInstaller(config, resume_downloads=True, max_attempts=1)
    bundle = dummy_bundle(config.channels[0])
    partial = installer.config.download_dir / (bundle.name + ".part")
    partial.write_bytes(b"abc")

    def fake_stream(method, url, *, headers=None, **kwargs):
        # Should request Range header
        assert headers is not None and "Range" in headers
        # Respond with 200 (full content) instead of 206 (partial)
        return StreamStub(b"XYZ", status_code=200)

    monkeypatch.setattr("calculinux_update.installer.httpx.stream", fake_stream)

    result = installer.download(bundle, expected_sha256=None)
    # Should restart and replace entire file
    assert result.path.read_bytes() == b"XYZ"


def test_check_disk_space_sufficient(tmp_path):
    """Test disk space check when sufficient space available."""
    from calculinux_update.installer import _check_disk_space

    # Should not raise for small file
    _check_disk_space(tmp_path, 1024)


def test_check_disk_space_insufficient(tmp_path, monkeypatch):
    """Test disk space check when insufficient space."""
    import os

    from calculinux_update.installer import _check_disk_space

    # Mock statvfs to return very little space
    class MockStatVFS:
        f_bavail = 10  # Only 10 blocks available
        f_frsize = 1024  # 1KB block size

    monkeypatch.setattr(os, "statvfs", lambda path: MockStatVFS())

    # Should raise when requesting 1GB with insufficient space
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        _check_disk_space(tmp_path, 1024 * 1024 * 1024)
