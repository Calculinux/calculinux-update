from types import SimpleNamespace

from typer.testing import CliRunner

from calculinux_update.cli import app
from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.mirror import BundleInfo

runner = CliRunner()


class StubMirror:
    def __init__(self, config, bundles):
        self.config = config
        self._bundles = bundles

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def list_bundles(self, channel_selector=None):  # pragma: no cover - trivial
        return self._bundles


class StubInstaller:
    def __init__(self, config):
        self.config = config
        self.download_calls = []
        self.install_calls = []

    @staticmethod
    def ensure_binary_available(binary="rauc"):
        return None

    def download(self, bundle, *, expected_sha256=None):
        self.download_calls.append((bundle, expected_sha256))
        return SimpleNamespace(
            bundle=bundle,
            path=self.config.download_dir / bundle.name,
            sha256="abcd",
        )

    def run_rauc_install(self, path, *, rauc_binary="rauc", sudo=True, dry_run=False):
        self.install_calls.append((path, rauc_binary, sudo, dry_run))


def build_config(tmp_path):
    return UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )


def build_bundle(config):
    return BundleInfo(
        name="bundle.raucb",
        url="https://example.com/bundle.raucb",
        channel=config.channels[0],
        size_bytes=1024,
        last_modified=None,
        sha256="abcd",
    )


def test_cli_list_outputs_table(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    bundle = build_bundle(config)

    monkeypatch.setattr("calculinux_update.cli._load_config", lambda *_: config)
    monkeypatch.setattr(
        "calculinux_update.cli.MirrorClient",
        lambda cfg: StubMirror(cfg, [bundle]),
    )

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "bundle.raucb" in result.stdout


def test_cli_download_with_bundle(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    bundle = build_bundle(config)
    installer = StubInstaller(config)

    monkeypatch.setattr("calculinux_update.cli._load_config", lambda *_: config)
    monkeypatch.setattr(
        "calculinux_update.cli.MirrorClient",
        lambda cfg: StubMirror(cfg, [bundle]),
    )
    monkeypatch.setattr("calculinux_update.cli.UpdateInstaller", lambda cfg: installer)

    result = runner.invoke(app, ["download", "--bundle", "bundle"])
    assert result.exit_code == 0
    assert installer.download_calls
    bundle_called, expected_sha = installer.download_calls[0]
    assert bundle_called.name == "bundle.raucb"
    assert expected_sha == bundle.sha256


def test_cli_install_triggers_run(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    bundle = build_bundle(config)
    installer = StubInstaller(config)

    monkeypatch.setattr("calculinux_update.cli._load_config", lambda *_: config)
    class InstallerFactory:
        @staticmethod
        def ensure_binary_available(*_args, **_kwargs):
            return None

        def __call__(self, _cfg):
            return installer

    monkeypatch.setattr("calculinux_update.cli.UpdateInstaller", InstallerFactory())
    monkeypatch.setattr(
        "calculinux_update.cli.MirrorClient",
        lambda cfg: StubMirror(cfg, [bundle]),
    )
    monkeypatch.setattr("calculinux_update.cli.typer.confirm", lambda *_, **__: True)

    result = runner.invoke(
        app,
        [
            "install",
            "--bundle",
            "bundle",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert installer.install_calls and installer.install_calls[0][3] is True
    assert installer.download_calls[0][1] == bundle.sha256
