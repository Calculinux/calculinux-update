from datetime import datetime, timezone

import httpx
import pytest

from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.mirror import MirrorClient


class StubResponse:
    def __init__(self, text: str = "", headers: dict | None = None, status: int = 200):
        self.text = text
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self) -> None:  # pragma: no cover - not triggered in tests
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class StubClient:
    def __init__(self, html: str, head_map: dict[str, dict[str, str]], *, fail_head: bool = False):
        self._html = html
        self._head_map = head_map
        self._fail_head = fail_head
        self.closed = False

    def get(self, url: str) -> StubResponse:  # pragma: no cover - trivial
        return StubResponse(text=self._html)

    def head(self, url: str) -> StubResponse:
        if self._fail_head:
            raise httpx.HTTPError("boom")
        return StubResponse(headers=self._head_map.get(url, {}))

    def close(self) -> None:  # pragma: no cover - trivial
        self.closed = True


@pytest.fixture()
def cfg(tmp_path):
    return UpdateConfig(
        mirror_base_url="https://example.com",
        download_dir=tmp_path,
        machine="luckfox",
        channels=[ChannelConfig(name="Test", path="/update/test")],
    )


def test_list_bundles_parses_metadata(monkeypatch, cfg):
    html = '<a href="bundle.raucb">bundle.raucb</a>'
    bundle_url = "https://example.com/update/test/bundle.raucb"
    head_map = {
        bundle_url: {
            "Content-Length": "1024",
            "Last-Modified": "Wed, 25 Sep 2024 10:00:00 GMT",
        }
    }

    stub_client = StubClient(html=html, head_map=head_map)
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    with MirrorClient(cfg) as client:
        bundles = client.list_bundles()

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.name == "bundle.raucb"
    assert bundle.size_bytes == 1024
    assert bundle.last_modified == datetime(2024, 9, 25, 10, 0, tzinfo=timezone.utc)


def test_safe_head_returns_none_on_error(monkeypatch, cfg):
    stub_client = StubClient(html="", head_map={}, fail_head=True)
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    client = MirrorClient(cfg)
    client._client = stub_client

    assert client._safe_head("https://example.com/fail") is None
    client.close()
