"""Mirror interaction helpers."""

from __future__ import annotations

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
        url = f"{self.config.mirror_base_url}{channel.normalized_path()}/"
        LOGGER.debug("Fetching channel index %s", url)
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
