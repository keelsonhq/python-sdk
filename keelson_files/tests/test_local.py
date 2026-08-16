"""Local filesystem backend: write / read / overwrite / delete / list."""

from __future__ import annotations

import os

import pytest

import keelson_files as files
from keelson_files.client import FilesError


@pytest.fixture(autouse=True)
def _local_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KEELSON_MODE", "local")
    monkeypatch.setenv("KEELSON_FILES_DIR", str(tmp_path / "files"))
    for var in ("KEELSON_FILES_BUCKET", "KEELSON_FILES_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_write_read_roundtrip() -> None:
    files.write("seen_urls.json", '["a"]')
    assert files.read("seen_urls.json") == b'["a"]'


def test_write_bytes() -> None:
    files.write("blob.bin", b"\x00\x01\x02")
    assert files.read("blob.bin") == b"\x00\x01\x02"


def test_str_stored_as_utf8() -> None:
    files.write("u.txt", "日本語")
    assert files.read("u.txt") == "日本語".encode("utf-8")


def test_overwrite_replaces_value() -> None:
    files.write("state", "one")
    files.write("state", "two")
    assert files.read("state") == b"two"


def test_read_missing_returns_none() -> None:
    assert files.read("nope.json") is None


def test_nested_key_creates_parents() -> None:
    files.write("cache/hn/latest.json", "{}")
    assert files.read("cache/hn/latest.json") == b"{}"


def test_delete_is_idempotent() -> None:
    files.write("x", "1")
    files.delete("x")
    assert files.read("x") is None
    # deleting again must not raise
    files.delete("x")


def test_list_recursive_sorted(_local_env) -> None:
    files.write("b.txt", "b")
    files.write("a.txt", "a")
    files.write("cache/z.json", "z")
    files.write("cache/a.json", "a")
    assert files.list() == ["a.txt", "b.txt", "cache/a.json", "cache/z.json"]


def test_list_prefix_filter() -> None:
    files.write("a.txt", "a")
    files.write("cache/z.json", "z")
    files.write("cache/a.json", "a")
    assert files.list("cache/") == ["cache/a.json", "cache/z.json"]


def test_list_empty_dir() -> None:
    assert files.list() == []


def test_literal_file_layout(_local_env) -> None:
    # The key is the literal file path so local data remains easy to inspect.
    files.write("seen_urls.json", "x")
    key_file = _local_env / "files" / "seen_urls.json"
    assert key_file.is_file()
    assert key_file.read_bytes() == b"x"
    # No leftover temp artifact in the files dir.
    assert [p.name for p in (_local_env / "files").iterdir()] == ["seen_urls.json"]


def test_temp_prefix_key_is_a_real_key(_local_env) -> None:
    # A key that looks like the old temp name is a valid key and must NOT be
    # hidden by list() (temp files now use a control-char prefix instead).
    files.write(".keelson-tmp-user-state", "v")
    assert files.read(".keelson-tmp-user-state") == b"v"
    assert files.list() == [".keelson-tmp-user-state"]


def test_read_of_key_shadowed_by_directory_is_missing() -> None:
    # A nested key makes "cache" a directory; read("cache") is missing (None),
    # not an error (matching the GCS 404).
    files.write("cache/item", "child")
    assert files.read("cache") is None


def test_delete_of_key_shadowed_by_directory_is_idempotent() -> None:
    files.write("cache/item", "child")
    files.delete("cache")  # shadowed by a directory → no-op
    assert files.read("cache/item") == b"child"


def test_write_parent_over_child_dir_is_a_clear_error() -> None:
    # A literal filesystem layout cannot hold both a key and a nested key below
    # it at the same time.
    files.write("cache/item", "child")
    with pytest.raises(FilesError, match="collides with a nested key"):
        files.write("cache", "parent")
    # The child is untouched.
    assert files.read("cache/item") == b"child"


def test_write_child_under_parent_file_is_a_clear_error() -> None:
    files.write("cache", "parent")
    with pytest.raises(FilesError, match="collides with a nested key"):
        files.write("cache/item", "child")
    assert files.read("cache") == b"parent"


def test_symlink_escape_is_rejected(_local_env, tmp_path) -> None:
    base = _local_env / "files"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = base / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(FilesError, match="escapes the files directory"):
        files.write("escape/pwned", "x")
    assert not (outside / "pwned").exists()


def test_read_does_not_follow_a_symlinked_key_file(_local_env, tmp_path) -> None:
    # A key file that is a symlink to an external file must NOT leak its content.
    base = _local_env / "files"
    base.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret-outside"
    secret.write_bytes(b"secret-outside")
    try:
        os.symlink(secret, base / "leak")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(FilesError, match="escapes the files directory"):
        files.read("leak")


def test_list_does_not_follow_symlinked_directories(_local_env, tmp_path) -> None:
    base = _local_env / "files"
    base.mkdir(parents=True, exist_ok=True)
    files.write("real", "v")
    # An external tree that looks like a stored key ("planted/deep.json").
    outside = tmp_path / "outside" / "planted"
    outside.mkdir(parents=True)
    (outside / "deep.json").write_bytes(b"x")
    try:
        os.symlink(tmp_path / "outside" / "planted", base / "linked",
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # list() must not descend into the symlinked directory.
    assert files.list() == ["real"]


def test_ancestor_symlink_swap_is_confined(_local_env, tmp_path) -> None:
    # TOCTOU: an ancestor directory swapped to an external symlink after a key
    # was written must not let read/write/delete escape (openat re-resolves each
    # component against a held descriptor).
    base = _local_env / "files"
    base.mkdir(parents=True, exist_ok=True)
    files.write("safe/secret", "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"outside-secret")
    import shutil

    shutil.rmtree(base / "safe")
    try:
        os.symlink(outside, base / "safe", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # read must not return the external content (it is rejected as a confinement
    # error, never followed out of the files dir).
    with pytest.raises(FilesError):
        files.read("safe/secret")
    # write must not create files outside the files dir.
    with pytest.raises(FilesError):
        files.write("safe/planted", "x")
    assert not (outside / "planted").exists()
    # delete must not remove the external file.
    files.delete("safe/secret")
    assert (outside / "secret").exists()


def test_oversize_write_rejected() -> None:
    with pytest.raises(FilesError):
        files.write("big", b"x" * (10 * 1024 * 1024 + 1))


def test_size_limit_boundary_ok() -> None:
    files.write("big", b"x" * (10 * 1024 * 1024))
    assert files.read("big") == b"x" * (10 * 1024 * 1024)
