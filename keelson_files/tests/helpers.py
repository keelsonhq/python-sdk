"""Shared test helpers: an in-memory fake GCS + metadata server.

Installs a fake ``urlopen`` on ``keelson_files.client`` that answers the GCE
metadata token endpoint and the GCS JSON API (upload / download / delete /
list) from an in-memory object store, so the remote (GCS) backend can be
exercised without network access.
"""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse

import keelson_files.client as client

REMOTE_ENV = {
    "KEELSON_FILES_BUCKET": "example-bucket",
    "KEELSON_FILES_PREFIX": "tenants/t/apps/a/files/",
    "KEELSON_FILES_STORAGE_BASE": "https://storage.example",
    "KEELSON_FILES_METADATA_URL": "http://metadata.example/token",
    # Keelson mode requires platform identity and fails closed when it is absent.
    "KEELSON_APP_ID": "a",
    "KEELSON_TENANT_ID": "t",
}


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class FakeGcs:
    """In-memory GCS/metadata stub. ``store`` maps object name → bytes."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_fetches = 0
        self.expires_in = 3600
        # Per-status forced error for the next matching request, keyed by method.
        self.force_status: dict[str, int] = {}
        # For paging tests: max items per list page (None = unbounded).
        self.page_size: int | None = None
        # Malformed-payload injection (200 OK but not valid JSON / bad schema).
        self.bad_token_json = False
        self.bad_list_json = False
        # Raw body override for list responses (valid JSON, wrong schema).
        self.list_body: bytes | None = None

    def urlopen(self, req, timeout=None):  # noqa: ANN001
        method = req.get_method()
        url = req.full_url
        parsed = urlparse(url)
        self.calls.append((method, url))

        # Metadata token endpoint.
        if parsed.netloc == "metadata.example":
            self.token_fetches += 1
            if self.bad_token_json:
                return _FakeResponse(200, b"not-json")
            body = json.dumps(
                {
                    "access_token": f"tok-{self.token_fetches}",
                    "expires_in": self.expires_in,
                }
            ).encode()
            return _FakeResponse(200, body)

        forced = self.force_status.get(method)
        if forced:
            raise HTTPError(url, forced, "forced", {}, BytesIO(b"forced"))

        path = parsed.path
        # Upload: POST /upload/storage/v1/b/<bucket>/o?uploadType=media&name=<enc>
        if method == "POST" and path.startswith("/upload/"):
            name = unquote(parse_qs(parsed.query)["name"][0])
            self.store[name] = req.data or b""
            return _FakeResponse(200, b"{}")

        # List: GET /storage/v1/b/<bucket>/o?prefix=<enc>[&pageToken=..]
        if method == "GET" and path.endswith("/o"):
            if self.list_body is not None:
                return _FakeResponse(200, self.list_body)
            qs = parse_qs(parsed.query)
            prefix = unquote(qs.get("prefix", [""])[0])
            page_token = qs.get("pageToken", [None])[0]
            names = sorted(n for n in self.store if n.startswith(prefix))
            start = int(page_token) if page_token else 0
            if self.page_size is not None:
                page = names[start : start + self.page_size]
                next_start = start + self.page_size
                payload = {"items": [{"name": n} for n in page]}
                if next_start < len(names):
                    payload["nextPageToken"] = str(next_start)
            else:
                payload = {"items": [{"name": n} for n in names]}
            if self.bad_list_json:
                return _FakeResponse(200, b"not-json")
            return _FakeResponse(200, json.dumps(payload).encode())

        # Object GET / DELETE: /storage/v1/b/<bucket>/o/<enc>
        marker = "/o/"
        idx = path.find(marker)
        name = unquote(path[idx + len(marker) :]) if idx >= 0 else ""
        if method == "GET":
            if name not in self.store:
                raise HTTPError(url, 404, "not found", {}, None)
            return _FakeResponse(200, self.store[name])
        if method == "DELETE":
            if name not in self.store:
                raise HTTPError(url, 404, "not found", {}, None)
            del self.store[name]
            return _FakeResponse(200, b"")

        raise AssertionError(f"unexpected request: {method} {url}")


def install_fake_gcs(monkeypatch) -> FakeGcs:  # noqa: ANN001
    fake = FakeGcs()
    client._clear_token_cache()
    monkeypatch.setattr(client, "urlopen", fake.urlopen)
    for key, value in REMOTE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KEELSON_MODE", "keelson")
    return fake
