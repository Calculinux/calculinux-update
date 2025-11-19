"""Configuration loading utilities for Calculinux update frontend."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

ENV_VAR = "CALCULINUX_UPDATE_CONFIG"
DEFAULT_CONFIG_NAME = "calculinux-update.toml"


@dataclass(slots=True)
class ChannelConfig:
    """Represents an update channel (mirror subdirectory)."""

    name: str
    path: str
    machine: Optional[str] = None
    enable: bool = True

    def normalized_path(self) -> str:
        path = self.path.strip()
        if not path.startswith("/"):
            path = f"/{path}"
        return path.rstrip("/")

    def is_enabled(self) -> bool:
        return bool(self.enable)


@dataclass(slots=True)
class UpdateConfig:
    """Top-level configuration."""

    mirror_base_url: str
    download_dir: Path
    machine: Optional[str]
    channels: List[ChannelConfig]

    def iter_channels(
        self,
        selector: Optional[str] = None,
        *,
        include_disabled: bool = False,
    ) -> Iterable[ChannelConfig]:
        if selector is None:
            channels = [
                channel
                for channel in self.channels
                if include_disabled or channel.is_enabled()
            ]
            if not channels:
                raise ValueError(
                    "No channels configured"
                    if include_disabled
                    else "No enabled channels configured"
                )
            return channels

        selector_lower = selector.lower()
        matched = [
            channel
            for channel in self.channels
            if selector_lower in channel.name.lower()
            or selector_lower in channel.normalized_path().lower()
        ]
        if not matched:
            raise ValueError(f"No channel matches selector '{selector}'")

        if include_disabled:
            return matched

        enabled = [channel for channel in matched if channel.is_enabled()]
        if enabled:
            return enabled
        raise ValueError(
            f"Channel '{selector}' is disabled in the configuration"
        )

    def first_channel(self, selector: Optional[str] = None) -> ChannelConfig:
        channels = list(self.iter_channels(selector))
        if not channels:
            raise ValueError("No channels configured")
        return channels[0]


def candidate_config_paths(explicit: Optional[Path] = None) -> List[Path]:
    base_paths: List[Path] = []

    if explicit:
        base_paths.append(explicit)

    env_path = os.environ.get(ENV_VAR)
    if env_path:
        base_paths.append(Path(env_path))

    base_paths.extend(
        [
            Path("/etc/calculinux-update") / DEFAULT_CONFIG_NAME,
            Path.home() / ".config" / "calculinux-update" / DEFAULT_CONFIG_NAME,
        ]
    )

    repo_default = Path(__file__).resolve().parents[2] / "config" / DEFAULT_CONFIG_NAME
    base_paths.append(repo_default)
    return base_paths


def load_config(explicit: Optional[Path] = None) -> UpdateConfig:
    """Load configuration from the first existing candidate path."""

    candidates = candidate_config_paths(explicit)

    for path in candidates:
        if path.is_file():
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            return parse_config(data, path)

    raise FileNotFoundError(
        "No calculinux-update config found. Checked: "
        + ", ".join(str(p) for p in candidates)
    )


def parse_config(data: dict, source: Path) -> UpdateConfig:
    mirror_base_url = data.get("mirror_base_url")
    if not mirror_base_url:
        raise ValueError(f"mirror_base_url missing in {source}")
    mirror_base_url = mirror_base_url.rstrip("/")

    download_dir_str = data.get("download_dir", "/var/tmp/calculinux-update")
    download_dir = Path(download_dir_str)

    channels_raw = data.get("channels") or []
    if not channels_raw:
        raise ValueError(f"No channels defined in {source}")

    channels = [
        ChannelConfig(
            name=channel.get("name") or channel.get("path"),
            path=channel.get("path"),
            machine=channel.get("machine"),
            enable=bool(channel.get("enable", True)),
        )
        for channel in channels_raw
    ]

    for channel in channels:
        if not channel.path:
            raise ValueError(f"Channel '{channel.name}' is missing a path in {source}")

    download_dir.mkdir(parents=True, exist_ok=True)

    return UpdateConfig(
        mirror_base_url=mirror_base_url,
        download_dir=download_dir,
        machine=data.get("machine"),
        channels=channels,
    )
