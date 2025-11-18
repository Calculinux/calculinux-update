import json
from datetime import datetime, timezone

import httpx
import pytest

from calculinux_update.config import ChannelConfig, UpdateConfig
from calculinux_update.mirror import MirrorClient


class StubResponse:
    def __init__(self, *, json_data: dict | None = None, status: int = 200):
        self._json_data = json_data
        self.status_code = status

    def raise_for_status(self) -> None:  # pragma: no cover - not triggered in tests
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        if self._json_data is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._json_data


class StubClient:
    def __init__(self, responses: dict[str, StubResponse]):
        self._responses = responses
        self.closed = False

    def get(self, url: str) -> StubResponse:  # pragma: no cover - trivial
        if url not in self._responses:
            raise AssertionError(f"Unexpected URL {url}")
        return self._responses[url]

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


def test_list_bundles_uses_index_json_and_sorts(monkeypatch, cfg):
    index_payload = {
        "artifacts": {
            "rauc": [
                {
                    "name": "new.raucb",
                    "size": 2048,
                    "last_modified": "2024-09-25T10:00:00+00:00",
                    "sha256": "abc123",
                },
                {
                    "name": "old.raucb",
                    "size": 1024,
                },
            ]
        }
    }

    index_url = "https://example.com/update/test/index.json"
    stub_client = StubClient({index_url: StubResponse(json_data=index_payload)})
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    with MirrorClient(cfg) as client:
        bundles = client.list_bundles()

    assert [bundle.name for bundle in bundles] == ["new.raucb", "old.raucb"]
    assert bundles[0].last_modified == datetime(2024, 9, 25, 10, 0, tzinfo=timezone.utc)
    assert bundles[1].last_modified is None


def test_list_bundles_raises_when_index_missing(monkeypatch, cfg):
    index_url = "https://example.com/update/test/index.json"
    stub_client = StubClient({index_url: StubResponse(status=404)})
    monkeypatch.setattr("calculinux_update.mirror.httpx.Client", lambda *_, **__: stub_client)

    with MirrorClient(cfg) as client:
        with pytest.raises(RuntimeError):
            client.list_bundles()
