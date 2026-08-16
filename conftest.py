"""Shared test helpers for all keelson SDK packages.

Usage in any SDK test module:

    from conftest import mock_urlopen_json, mock_urlopen_error, make_http_error

These fixtures and helpers standardize the mock-first test pattern so tests do
not depend on live platform services.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------


def make_http_error(
    code: int,
    body: str = "",
    url: str = "http://test",
) -> HTTPError:
    """Create an ``HTTPError`` with the given status code and body."""
    fp = BytesIO(body.encode())
    return HTTPError(url=url, code=code, msg="", hdrs={}, fp=fp)  # type: ignore[arg-type]


def mock_urlopen_json(target: str, payload: dict | list):
    """Patch *target* (a ``urlopen`` import path) to return a JSON response.

    Parameters
    ----------
    target:
        Fully-qualified import path to ``urlopen``, e.g.
        ``"keelson_media.client.urlopen"``.
    payload:
        JSON-serialisable object returned by the fake response.

    Returns
    -------
    A ``unittest.mock.patch`` context manager / decorator.

    Example::

        with mock_urlopen_json("keelson_media.client.urlopen", {"ok": True}):
            result = some_sdk_call()
    """
    body = json.dumps(payload).encode()

    class _FakeResponse:
        def read(self) -> bytes:
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_: Any):
            pass

    return patch(target, return_value=_FakeResponse())


def mock_urlopen_error(target: str, code: int, body: str = ""):
    """Patch *target* to raise ``HTTPError`` with the given status code.

    Parameters
    ----------
    target:
        Fully-qualified import path to ``urlopen``.
    code:
        HTTP status code (e.g. 401, 403, 404).
    body:
        Optional response body text.
    """

    def _side_effect(*_args: Any, **_kwargs: Any):
        raise make_http_error(code, body)

    return patch(target, side_effect=_side_effect)


class FakeUrlopen:
    """Callable replacement for ``urlopen`` that captures requests.

    Usage::

        fake = FakeUrlopen({"items": []})
        with patch("keelson_media.client.urlopen", side_effect=fake):
            some_sdk_call()
        assert len(fake.captured) == 1
        req = fake.captured[0]
        assert "/v1/media" in req.full_url
    """

    def __init__(self, payload: dict | list) -> None:
        self._body = json.dumps(payload).encode()
        self.captured: list[Any] = []

    def __call__(self, req: Any, **kwargs: Any):
        self.captured.append(req)

        body = self._body

        class _Resp:
            def read(self) -> bytes:
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_: Any):
                pass

        return _Resp()
