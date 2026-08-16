"""Key and prefix validation driven by the shared parity fixture."""

from __future__ import annotations

import json

import pytest

from keelson_files.client import (
    FilesError,
    _validate_key,
    _validate_prefix,
)
from sdk_test_fixtures import parity_fixtures_dir

_FIXTURE = parity_fixtures_dir(__file__) / "files_key_validation.json"


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", _fixture()["valid"])
def test_valid_keys_accepted(key: str) -> None:
    assert _validate_key(key) == key


@pytest.mark.parametrize(
    "case", _fixture()["invalid"], ids=lambda c: c["reason"]
)
def test_invalid_keys_rejected(case: dict) -> None:
    with pytest.raises(FilesError):
        _validate_key(case["key"])


def test_key_over_512_bytes_rejected() -> None:
    with pytest.raises(FilesError):
        _validate_key("a" * 513)


def test_key_at_512_bytes_accepted() -> None:
    # 512 total bytes, split into segments each ≤ 255 bytes (NAME_MAX).
    key = "/".join(["a" * 170, "b" * 170, "c" * 170])  # 510 + 2 slashes = 512
    assert len(key.encode("utf-8")) == 512
    assert _validate_key(key) == key


def test_multibyte_byte_length_counts_bytes_not_chars() -> None:
    # 171 * 3 bytes = 513 bytes > 512, though only 171 characters.
    with pytest.raises(FilesError):
        _validate_key("あ" * 171)


def test_segment_over_255_bytes_rejected() -> None:
    # A single 256-byte segment is ≤ 512 bytes total but exceeds NAME_MAX.
    with pytest.raises(FilesError, match="segment must be at most 255"):
        _validate_key("a" * 256)


def test_segment_at_255_bytes_accepted() -> None:
    key = "a" * 255
    assert _validate_key(key) == key


def test_c1_control_char_rejected() -> None:
    # Unicode category Cc includes C1 controls (U+0080–U+009F, e.g. U+0085 NEL).
    with pytest.raises(FilesError, match="control characters"):
        _validate_key("ab")
    with pytest.raises(FilesError, match="control characters"):
        _validate_key("ab")


def test_lone_surrogate_key_rejected() -> None:
    # A Python str can hold a lone surrogate; it has no well-formed UTF-8
    # encoding and must be a typed FilesError, not a raw UnicodeEncodeError.
    with pytest.raises(FilesError, match="well-formed UTF-8"):
        _validate_key("\ud800")
    with pytest.raises(FilesError, match="well-formed UTF-8"):
        _validate_key("a\udc00b")


def test_lone_surrogate_prefix_rejected() -> None:
    with pytest.raises(FilesError, match="well-formed UTF-8"):
        _validate_prefix("\ud800")


def test_prefix_allows_empty_and_trailing_slash() -> None:
    assert _validate_prefix("") == ""
    assert _validate_prefix("cache/") == "cache/"


def test_prefix_rejects_traversal_and_leading_slash() -> None:
    with pytest.raises(FilesError):
        _validate_prefix("/abs")
    with pytest.raises(FilesError):
        _validate_prefix("a/../b")
