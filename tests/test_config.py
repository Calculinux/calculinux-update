from importlib import resources
from pathlib import Path

import pytest

import calculinux_update.config as config_module
from calculinux_update.config import load_config, parse_config


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


def test_load_config_falls_back_to_packaged_default(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module,
        "candidate_config_paths",
        lambda explicit=None: [tmp_path / "missing.toml"],
    )

    packaged = config_module.parse_config(
        {
            "mirror_base_url": "https://packaged.example",
            "download_dir": str(tmp_path),
            "channels": [{"name": "Packaged", "path": "/update/packaged"}],
        },
        Path("package://test"),
    )

    monkeypatch.setattr(config_module, "_load_packaged_default", lambda: packaged)

    cfg = config_module.load_config()
    assert cfg.mirror_base_url == "https://packaged.example"
    assert cfg.channels[0].normalized_path() == "/update/packaged"


def test_sample_config_matches_packaged_default():
    repo_config = Path(__file__).resolve().parents[1] / "config" / "calculinux-update.toml"
    pkg_config = (
        resources.files("calculinux_update")
        .joinpath("defaults", "calculinux-update.toml")
        .read_text(encoding="utf-8")
    )
    assert repo_config.read_text(encoding="utf-8") == pkg_config


def test_packaged_default_loader_reads_resource():
    cfg = config_module._load_packaged_default()
    assert cfg is not None
    assert any(channel.name for channel in cfg.channels)


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
