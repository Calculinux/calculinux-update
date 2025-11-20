from types import SimpleNamespace

from typer.testing import CliRunner

from calculinux_update.cli import (
    _build_pagination_prompt,
    _calculate_page_size,
    _handle_pagination_input,
    _pick_bundle,
    app,
)
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


def test_cli_install_yes_skips_prompt(monkeypatch, tmp_path):
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

    def fail_confirm(*_args, **_kwargs):
        raise AssertionError("confirm should not run when --yes is provided")

    monkeypatch.setattr("calculinux_update.cli.typer.confirm", fail_confirm)

    result = runner.invoke(
        app,
        [
            "install",
            "--bundle",
            "bundle",
            "--dry-run",
            "--yes",
        ],
    )
    assert result.exit_code == 0


def test_cli_install_dry_run_skips_binary_check(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    bundle = build_bundle(config)
    installer = StubInstaller(config)

    monkeypatch.setattr("calculinux_update.cli._load_config", lambda *_: config)

    class InstallerFactory:
        @staticmethod
        def ensure_binary_available(*_args, **_kwargs):
            raise AssertionError("should not check binary for dry run")

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


def test_calculate_page_size():
    """Test that _calculate_page_size returns a reasonable value."""
    page_size = _calculate_page_size()
    # Should be at least 1 and at most something reasonable
    assert page_size >= 1
    assert page_size <= 20  # Sanity check


def test_pick_bundle_with_bundle_name(tmp_path):
    """Test _pick_bundle with explicit bundle name."""
    config = build_config(tmp_path)
    bundles = [
        BundleInfo(
            name="bundle1.raucb",
            url="https://example.com/bundle1.raucb",
            channel=config.channels[0],
            size_bytes=1024,
            sha256="abc1",
        ),
        BundleInfo(
            name="bundle2.raucb",
            url="https://example.com/bundle2.raucb",
            channel=config.channels[0],
            size_bytes=2048,
            sha256="abc2",
        ),
    ]

    # Should find the matching bundle
    result = _pick_bundle(bundles, "bundle2")
    assert result.name == "bundle2.raucb"


def test_pick_bundle_with_partial_name(tmp_path):
    """Test _pick_bundle with partial bundle name match."""
    config = build_config(tmp_path)
    bundles = [
        BundleInfo(
            name="my-bundle-v1.0.0.raucb",
            url="https://example.com/my-bundle-v1.0.0.raucb",
            channel=config.channels[0],
            size_bytes=1024,
            sha256="abc1",
        ),
    ]

    # Partial match should work
    result = _pick_bundle(bundles, "v1.0")
    assert result.name == "my-bundle-v1.0.0.raucb"


def test_pick_bundle_empty_list():
    """Test _pick_bundle with empty bundle list."""
    import typer

    try:
        _pick_bundle([], None)
        assert False, "Should have raised Exit"
    except typer.Exit:
        # Expected - empty list should exit
        pass


def test_pick_bundle_not_found(tmp_path):
    """Test _pick_bundle with bundle name that doesn't exist."""
    import typer

    config = build_config(tmp_path)
    bundles = [
        BundleInfo(
            name="bundle1.raucb",
            url="https://example.com/bundle1.raucb",
            channel=config.channels[0],
            size_bytes=1024,
            sha256="abc1",
        ),
    ]

    try:
        _pick_bundle(bundles, "nonexistent")
        assert False, "Should have raised Exit"
    except typer.Exit:
        # Expected - unfound bundle should exit
        pass


def test_build_pagination_prompt():
    """Test _build_pagination_prompt function."""
    # First page - should offer forward navigation but not backward
    prompt = _build_pagination_prompt(0, 3, 10)
    assert "n" in prompt  # Next option available
    assert "p" not in prompt or "previous" not in prompt.lower()  # No previous
    assert "q" in prompt  # Quit always available
    assert any(char.isdigit() for char in prompt)  # Shows numeric range

    # Middle page - should offer both directions
    prompt = _build_pagination_prompt(1, 3, 10)
    assert "n" in prompt  # Next option available
    assert "p" in prompt  # Previous option available
    assert "q" in prompt  # Quit always available

    # Last page - should offer backward navigation but not forward
    prompt = _build_pagination_prompt(2, 3, 10)
    assert "n" not in prompt or "next" not in prompt.lower()  # No next
    assert "p" in prompt  # Previous option available
    assert "q" in prompt  # Quit always available


def test_handle_pagination_input_quit():
    """Test _handle_pagination_input with quit command."""
    new_page, bundle_num = _handle_pagination_input("q", 0, 2, 10)
    assert new_page is None
    assert bundle_num is None

    # Case insensitive
    new_page, bundle_num = _handle_pagination_input("Q", 0, 2, 10)
    assert new_page is None
    assert bundle_num is None


def test_handle_pagination_input_navigation():
    """Test _handle_pagination_input with navigation commands."""
    # Next page - should advance
    new_page, bundle_num = _handle_pagination_input("n", 0, 2, 10)
    assert new_page > 0  # Advanced forward
    assert bundle_num is None  # No bundle selected

    # Previous page - should go back
    new_page, bundle_num = _handle_pagination_input("p", 1, 2, 10)
    assert new_page == 0  # Went backward
    assert bundle_num is None  # No bundle selected

    # Can't go next from last page - should stay
    new_page, bundle_num = _handle_pagination_input("n", 1, 2, 10)
    assert new_page == 1  # Stayed on same page
    assert bundle_num is None

    # Can't go previous from first page - should stay
    new_page, bundle_num = _handle_pagination_input("p", 0, 2, 10)
    assert new_page == 0  # Stayed on same page
    assert bundle_num is None


def test_handle_pagination_input_bundle_selection():
    """Test _handle_pagination_input with bundle number selection."""
    # Valid bundle number
    new_page, bundle_num = _handle_pagination_input("5", 0, 2, 10)
    assert new_page is None
    assert bundle_num == 5

    # Another valid number
    new_page, bundle_num = _handle_pagination_input("1", 0, 2, 10)
    assert new_page is None
    assert bundle_num == 1


def test_handle_pagination_input_invalid():
    """Test _handle_pagination_input with invalid input."""
    # Out of range - should stay on current page
    new_page, bundle_num = _handle_pagination_input("99", 0, 2, 10)
    assert new_page == 0  # Stayed on page
    assert bundle_num is None  # No valid selection

    # Invalid text - should stay on current page
    new_page, bundle_num = _handle_pagination_input("invalid", 0, 2, 10)
    assert new_page == 0  # Stayed on page
    assert bundle_num is None  # No valid selection

    # Negative number - should stay on current page
    new_page, bundle_num = _handle_pagination_input("-1", 0, 2, 10)
    assert new_page == 0  # Stayed on page
    assert bundle_num is None  # No valid selection
