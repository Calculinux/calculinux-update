import pytest

from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.installer import UpdateInstaller
from calculinux_update.mirror import BundleInfo


@pytest.fixture()
def installer(tmp_path):
    config = UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )
    return UpdateInstaller(config)


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
    assert "Dry run" in captured.out


def test_run_rauc_install_invokes_subprocess(installer, monkeypatch):
    bundle = dummy_bundle(installer.config.channels[0])
    path = installer.config.download_dir / bundle.name
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)

    monkeypatch.setattr("calculinux_update.installer.subprocess.run", fake_run)
    monkeypatch.setattr("calculinux_update.installer.os.geteuid", lambda: 1000)

    installer.run_rauc_install(path, sudo=True)
    assert calls and calls[0][0] == "sudo"
