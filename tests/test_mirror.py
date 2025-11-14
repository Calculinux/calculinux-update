import json
from datetime import datetime, timezone

import httpx
import pytest

from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.mirror import MirrorClient


class StubResponse:
    def __init__(
        self,
        text: str = "",
        headers: dict | None = None,
        status: int = 200,
        json_data: dict | None = None,
    ):
        self.text = text
        self.headers = headers or {}
        self.status_code = status
        self._json_data = json_data

    def raise_for_status(self) -> None:  # pragma: no cover - not triggered in tests
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        if self._json_data is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._json_data


class StubClient:
    def __init__(
        self,
        html: str,
        head_map: dict[str, dict[str, str]],
        *,
        fail_head: bool = False,
        index_json: dict | None = None,
    ):
        self._html = html
        self._head_map = head_map
        self._fail_head = fail_head
        self._index_json = index_json
        self.closed = False
        self.head_calls = 0

    def get(self, url: str) -> StubResponse:  # pragma: no cover - trivial
        if url.endswith("index.json") and self._index_json is not None:
            return StubResponse(json_data=self._index_json)
        return StubResponse(text=self._html)

    def head(self, url: str) -> StubResponse:
        self.head_calls += 1
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
    assert bundle.sha256 is None


def test_list_bundles_prefers_index_json(monkeypatch, cfg):
    index_payload = {
        "artifacts": {
            "rauc": [
                {
                    "name": "bundle.raucb",
                    "size": 2048,
                    "last_modified": "2024-09-25T10:00:00+00:00",
                    "sha256": "abc123",
                }
            ]
        }
    }

    stub_client = StubClient(html="", head_map={}, index_json=index_payload)
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    with MirrorClient(cfg) as client:
        bundles = client.list_bundles()

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.size_bytes == 2048
    assert bundle.sha256 == "abc123"
    assert stub_client.head_calls == 0


def test_safe_head_returns_none_on_error(monkeypatch, cfg):
    stub_client = StubClient(html="", head_map={}, fail_head=True)
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    client = MirrorClient(cfg)
    client._client = stub_client

    assert client._safe_head("https://example.com/fail") is None
    client.close()
