"""Path-based local backend used by Python on Windows."""

from __future__ import annotations

import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import keelson_files as files
from keelson_files import client
from keelson_files.client import FilesError


@pytest.fixture(autouse=True)
def _path_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("KEELSON_MODE", "local")
    monkeypatch.setenv("KEELSON_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(client, "_supports_dir_fd_backend", lambda: False)
    for var in ("KEELSON_FILES_BUCKET", "KEELSON_FILES_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_roundtrip_overwrite_list_and_delete() -> None:
    files.write("nested/a.txt", "one")
    files.write("nested/a.txt", "two")
    files.write("b.txt", b"b")

    assert files.read("nested/a.txt") == b"two"
    assert files.list() == ["b.txt", "nested/a.txt"]
    assert files.list("nested/") == ["nested/a.txt"]

    files.delete("nested/a.txt")
    files.delete("nested/a.txt")
    assert files.read("nested/a.txt") is None


def test_concurrent_readers_never_observe_partial_replacement() -> None:
    first = b"a" * (64 * 1024)
    second = b"b" * (64 * 1024)
    files.write("state", first)

    def write_repeatedly() -> None:
        for index in range(30):
            files.write("state", first if index % 2 else second)

    def read_repeatedly() -> None:
        for _ in range(100):
            assert files.read("state") in (first, second)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write_repeatedly), executor.submit(read_repeatedly)]
        for future in futures:
            future.result()


def test_key_and_nested_key_collisions_are_clear() -> None:
    files.write("cache/item", "child")
    with pytest.raises(FilesError, match="collides with a nested key"):
        files.write("cache", "parent")

    files.write("flat", "parent")
    with pytest.raises(FilesError, match="collides with a nested key"):
        files.write("flat/item", "child")


def test_observed_symlinks_are_not_followed(_path_backend, tmp_path) -> None:
    base = _path_backend / "files"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"outside")
    linked = base / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        os.symlink(outside, linked, target_is_directory=True)

    with pytest.raises(FilesError, match="escapes the files directory"):
        files.read("linked/secret")
    with pytest.raises(FilesError, match="escapes the files directory"):
        files.write("linked/planted", "x")
    files.delete("linked/secret")
    assert (outside / "secret").read_bytes() == b"outside"
    assert not (outside / "planted").exists()
    assert files.list() == []


def test_windows_name_surrogate_reparse_points_are_path_redirects() -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    junction = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=reparse,
        st_reparse_tag=0xA0000003,
    )
    cloud_placeholder = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_file_attributes=reparse,
        st_reparse_tag=0x9000001A,
    )

    assert client._is_path_redirect(junction)
    assert not client._is_path_redirect(cloud_placeholder)


@pytest.mark.parametrize(
    "value",
    [
        r"a\..\x",
        "a:b",
        "NUL",
        "NUL .txt",
        "con.txt",
        "CONIN$",
        "CONOUT$.log",
        "COM¹",
        "COM².txt",
        "LPT³",
        "trailing.",
        ".keelson-tmp-0123456789abcdef01234567",
        ".KEELSON-TMP-0123456789ABCDEF01234567",
    ],
)
def test_windows_special_names_are_rejected(value: str) -> None:
    with pytest.raises(FilesError, match="Windows cannot store locally"):
        client._validate_windows_local_path(value, os_name="nt")


def test_windows_portable_name_is_accepted() -> None:
    client._validate_windows_local_path("reports/2026-09.csv", os_name="nt")
    client._validate_windows_local_path(".keelson-tmp-user-state", os_name="nt")


def test_list_hides_internal_temp_names_case_insensitively(_path_backend) -> None:
    base = _path_backend / "files"
    base.mkdir()
    (base / ".KEELSON-TMP-0123456789ABCDEF01234567").write_bytes(b"internal")
    assert files.list() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows deletion semantics")
def test_windows_delete_reports_read_only_file(_path_backend) -> None:
    files.write("locked.txt", "keep")
    target = _path_backend / "files" / "locked.txt"
    target.chmod(stat.S_IREAD)
    try:
        with pytest.raises(FilesError, match="delete failed"):
            files.delete("locked.txt")
        assert target.read_bytes() == b"keep"
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_windows_reports_delete_and_overwrite_sharing_violations(
    _path_backend,
) -> None:
    files.write("held.txt", "keep")
    target = _path_backend / "files" / "held.txt"
    with target.open("rb"):
        with pytest.raises(FilesError, match="delete failed"):
            files.delete("held.txt")
        with pytest.raises(FilesError, match="write failed"):
            files.write("held.txt", "replacement")
    assert target.read_bytes() == b"keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows case-folding semantics")
def test_windows_case_variants_share_the_local_path() -> None:
    files.write("Report", "first")
    files.write("report", "second")
    assert files.read("REPORT") == b"second"
