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
