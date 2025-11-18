"""Mirror interaction helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import httpx

from .config import ChannelConfig, UpdateConfig

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BundleInfo:
    name: str
    url: str
    channel: ChannelConfig
    size_bytes: Optional[int] = None
    last_modified: Optional[datetime] = None
    sha256: Optional[str] = None


class MirrorClient:
    def __init__(self, config: UpdateConfig, *, timeout: float = 10.0) -> None:
        self.config = config
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MirrorClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def list_bundles(self, channel_selector: Optional[str] = None) -> List[BundleInfo]:
        bundles: List[BundleInfo] = []
        for channel in self.config.iter_channels(channel_selector):
            bundles.extend(self._fetch_channel(channel))
        bundles.sort(key=_bundle_sort_key, reverse=True)
        return bundles

    def _fetch_channel(self, channel: ChannelConfig) -> List[BundleInfo]:
        index_bundles = self._fetch_from_index(channel)
        if index_bundles is not None:
            return index_bundles
        raise RuntimeError(
            f"Channel '{channel.name}' at {channel.normalized_path()} is missing index.json"
        )

    def _fetch_from_index(self, channel: ChannelConfig) -> Optional[List[BundleInfo]]:
        index_url = f"{self.config.mirror_base_url}{channel.normalized_path()}/index.json"
        LOGGER.debug("Attempting to fetch index %s", index_url)
        try:
            response = self._client.get(index_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            LOGGER.debug("Index fetch failed for %s: %s", index_url, exc)
            return None
        except httpx.HTTPError as exc:  # pragma: no cover - network errors
            LOGGER.debug("Index fetch error for %s: %s", index_url, exc)
            return None

        try:
            data = response.json()
        except json.JSONDecodeError:
            LOGGER.warning("Invalid JSON received from %s", index_url)
            return None

        artifacts = data.get("artifacts", {}).get("rauc", [])
        bundles: List[BundleInfo] = []
        for entry in artifacts:
            name = entry.get("name")
            if not name:
                continue
            url = entry.get("url") or f"{self.config.mirror_base_url}{channel.normalized_path()}/{name}"
            bundles.append(
                BundleInfo(
                    name=name,
                    url=url,
                    channel=channel,
                    size_bytes=_safe_int(entry.get("size")),
                    last_modified=_parse_iso(entry.get("last_modified")),
                    sha256=entry.get("sha256"),
                )
            )
        return bundles


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):  # pragma: no cover - fallback
            LOGGER.debug("Unable to parse datetime value %s", value)
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_int(value: Optional[object]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bundle_sort_key(bundle: BundleInfo) -> float:
    if not bundle.last_modified:
        return 0.0
    aware = bundle.last_modified
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=timezone.utc)
    return aware.timestamp()
