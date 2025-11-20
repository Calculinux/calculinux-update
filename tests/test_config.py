import pytest

import calculinux_update.config as config_module
from calculinux_update.config import _detect_machine, load_config, parse_config


def test_load_config_from_explicit(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
mirror_base_url = "https://example.com"
download_dir = "{tmp_path}"

[[channels]]
name = "Test"
path = "/update/test"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.mirror_base_url == "https://example.com"
    assert cfg.channels[0].normalized_path() == "/update/test"


def test_parse_config_rejects_missing_channel(tmp_path):
    cfg_path = tmp_path / "config.toml"
    data = {
        "mirror_base_url": "https://example.com",
        "download_dir": str(tmp_path),
        "channels": [],
    }
    try:
        parse_config(data, cfg_path)
    except ValueError as exc:
        assert "No channels" in str(exc)
    else:  # pragma: no cover - ensures failure is reported
        raise AssertionError("Expected ValueError")


def test_load_config_raises_error_when_no_config_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module,
        "candidate_config_paths",
        lambda explicit=None: [tmp_path / "missing.toml"],
    )

    with pytest.raises(FileNotFoundError) as exc:
        config_module.load_config()
    assert "No calculinux-update config found" in str(exc.value)


def test_iter_channels_filters_disabled_entries(tmp_path):
    data = {
        "mirror_base_url": "https://example.com",
        "download_dir": str(tmp_path),
        "channels": [
            {"name": "Enabled", "path": "/update/enabled"},
            {"name": "Disabled", "path": "/update/disabled", "enable": False},
        ],
    }
    cfg = parse_config(data, tmp_path / "config.toml")

    enabled = list(cfg.iter_channels())
    assert [channel.name for channel in enabled] == ["Enabled"]

    with pytest.raises(ValueError) as exc:
        list(cfg.iter_channels("Disabled"))
    assert "disabled" in str(exc.value)


def test_detect_machine_strips_calculinux_prefix(monkeypatch):
    # Mock subprocess to return calculinux-prefixed compatible string
    import subprocess
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "RAUC_SYSTEM_COMPATIBLE='calculinux-luckfox-lyra'\n"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: mock_result,
    )

    machine = _detect_machine()
    assert machine == "luckfox-lyra"


def test_detect_machine_handles_non_calculinux_prefix(monkeypatch):
    # Mock subprocess to return non-calculinux compatible string
    import subprocess
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "RAUC_SYSTEM_COMPATIBLE='some-other-device'\n"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: mock_result,
    )

    machine = _detect_machine()
    assert machine == "some-other-device"


def test_parse_config_uses_detected_machine_when_not_specified(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module,
        "_detect_machine",
        lambda: "luckfox-lyra",
    )

    data = {
        "mirror_base_url": "https://example.com",
        "download_dir": str(tmp_path),
        "channels": [{"name": "Test", "path": "/update/test"}],
    }
    cfg = parse_config(data, tmp_path / "config.toml")
    assert cfg.machine == "luckfox-lyra"


def test_parse_config_prefers_explicit_machine_over_detection(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module,
        "_detect_machine",
        lambda: "auto-detected",
    )

    data = {
        "mirror_base_url": "https://example.com",
        "download_dir": str(tmp_path),
        "machine": "explicit-machine",
        "channels": [{"name": "Test", "path": "/update/test"}],
    }
    cfg = parse_config(data, tmp_path / "config.toml")
    assert cfg.machine == "explicit-machine"
