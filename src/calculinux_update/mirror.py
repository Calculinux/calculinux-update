"""Mirror interaction helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable, List, Optional

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


class _BundleLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
        if tag.lower() != "a":
            return
        attr_map = dict(attrs)
        href = attr_map.get("href")
        if href and href.endswith(".raucb"):
            self.links.append(href)


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
        bundles.sort(key=lambda b: b.last_modified or datetime.min, reverse=True)
        return bundles

    def _fetch_channel(self, channel: ChannelConfig) -> Iterable[BundleInfo]:
        index_bundles = self._fetch_from_index(channel)
        if index_bundles is not None:
            return index_bundles
        return self._fetch_from_directory_listing(channel)

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

    def _fetch_from_directory_listing(self, channel: ChannelConfig) -> List[BundleInfo]:
        url = f"{self.config.mirror_base_url}{channel.normalized_path()}/"
        LOGGER.debug("Fetching channel directory %s", url)
        response = self._client.get(url)
        response.raise_for_status()

        parser = _BundleLinkParser()
        parser.feed(response.text)

        bundles: List[BundleInfo] = []
        for link in parser.links:
            bundle_url = url + link
            head = self._safe_head(bundle_url)
            size = None
            last_modified = None
            if head is not None:
                size_header = head.headers.get("Content-Length")
                if size_header and size_header.isdigit():
                    size = int(size_header)
                last_modified_header = head.headers.get("Last-Modified")
                if last_modified_header:
                    try:
                        last_modified = parsedate_to_datetime(last_modified_header)
                    except (TypeError, ValueError):  # pragma: no cover - fallback
                        LOGGER.debug("Failed to parse Last-Modified header for %s", bundle_url)
            bundles.append(
                BundleInfo(
                    name=link,
                    url=bundle_url,
                    channel=channel,
                    size_bytes=size,
                    last_modified=last_modified,
                )
            )
        return bundles

    def _safe_head(self, url: str) -> Optional[httpx.Response]:
        try:
            head = self._client.head(url)
            head.raise_for_status()
            return head
        except httpx.HTTPError as exc:  # pragma: no cover - network
            LOGGER.warning("HEAD request failed for %s: %s", url, exc)
            return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):  # pragma: no cover - fallback
            LOGGER.debug("Unable to parse datetime value %s", value)
            return None


def _safe_int(value: Optional[object]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
