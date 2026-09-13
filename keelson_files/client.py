"""Keelson data-files SDK — key-addressed, overwrite, whole-value file storage.

The ``files`` interface is intentionally distinct from ``keelson_media``:
media is create-only, ULID-addressed, immutable and served over HTTP; ``files``
is key-addressed, overwrite-in-place, and read only by the app itself.

API:

- ``write(key, data)``  — overwrite (str is encoded UTF-8). write-through.
- ``read(key)``         — bytes, or ``None`` when the key does not exist.
- ``delete(key)``       — idempotent (no error when missing).
- ``list(prefix="")``   — lexicographically-sorted list of all keys.

Backends:

- ``local``: cwd-relative real files under ``./.keelson/files/<key>``
  (override the base dir with ``KEELSON_FILES_DIR``). Writes are temp-file +
  atomic replace so a partially-written file is never visible.
- Keelson (``remote``): the per-app service account writes through to GCS over
  ADC (no auth env wiring); the bucket + prefix are injected as
  ``KEELSON_FILES_BUCKET`` / ``KEELSON_FILES_PREFIX``.

Mode resolution: ``KEELSON_MODE`` is the single mode signal. In Keelson mode,
missing platform configuration raises an error instead of silently selecting
local storage. See :func:`_resolve_mode`.
"""

from __future__ import annotations

import builtins
import errno
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_SDK_USER_AGENT = "Keelson-Python-SDK/0.1.1"

# HTTP request timeout (seconds) for GCS / metadata-server calls. Matches the
# media SDK's 30s.
_TIMEOUT = 30

# Each object has a 10 MiB soft size limit, enforced by ``write`` before upload.
# Aggregate storage and object-count limits are enforced by the platform.
MAX_OBJECT_SIZE_BYTES = 10 * 1024 * 1024

# GCS JSON API base and GCE metadata-server token endpoint. These have real
# defaults; the env overrides are SDK-internal test seams (they are NOT part of
# the platform-injected env contract) so the GCS backend can be exercised
# against a local stub.
_DEFAULT_STORAGE_BASE = "https://storage.googleapis.com"
_DEFAULT_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


class FilesError(RuntimeError):
    """Raised on configuration errors and non-404 backend failures."""


# ---------------------------------------------------------------------------
# Key and prefix validation
# ---------------------------------------------------------------------------


# One filesystem path component is limited to NAME_MAX (255 bytes on ext4 /
# APFS / most POSIX filesystems). The literal-file local layout maps each key
# segment to a filename, so each segment must fit — otherwise a spec-valid
# ≤512-byte key with a long single segment would fail with ENAMETOOLONG.
_SEGMENT_MAX_BYTES = 255


def _has_control_char(value: str) -> bool:
    # Unicode control characters (category Cc): C0 (U+0000–U+001F), DEL (U+007F)
    # and C1 (U+0080–U+009F, e.g. U+0085 NEL).
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _utf8_len(value: str, what: str) -> int:
    """UTF-8 byte length, rejecting ill-formed UTF-8 (lone surrogates).

    A Python ``str`` can hold lone surrogates (e.g. ``"\\ud800"``); these have
    no well-formed UTF-8 encoding and would collide/error differently across the
    local FS and the GCS backend, so they are rejected up front as a typed
    ``FilesError`` (matching Node's well-formed check and Go's ``utf8.Valid``).
    """
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise FilesError(
            f"{what} must be well-formed UTF-8 (no lone surrogates)."
        ) from exc


def _validate_key(key: str) -> str:
    """Validate a data-file key, returning it unchanged.

    Grammar: ``/``-separated relative path; well-formed UTF-8 with byte length
    ≤ 512; no leading/trailing ``/``; no empty, ``.`` or ``..`` segments; no
    control characters.
    """
    if not isinstance(key, str):
        raise FilesError("key must be a string.")
    if key == "":
        raise FilesError("key is required.")
    if _utf8_len(key, "key") > 512:
        raise FilesError("key must be at most 512 UTF-8 bytes.")
    if key.startswith("/") or key.endswith("/"):
        raise FilesError("key must not start or end with '/'.")
    if _has_control_char(key):
        raise FilesError("key must not contain control characters.")
    for segment in key.split("/"):
        if segment == "":
            raise FilesError("key must not contain empty segments.")
        if segment in (".", ".."):
            raise FilesError("key must not contain '.' or '..' segments.")
        if len(segment.encode("utf-8")) > _SEGMENT_MAX_BYTES:
            raise FilesError(
                f"each key segment must be at most {_SEGMENT_MAX_BYTES} UTF-8 bytes."
            )
    return key


def _validate_prefix(prefix: str) -> str:
    """Validate a ``list`` prefix (looser than a key: empty and a trailing
    ``/`` are allowed, but path traversal, control chars and oversize are not).
    """
    if not isinstance(prefix, str):
        raise FilesError("prefix must be a string.")
    if prefix == "":
        return ""
    if _utf8_len(prefix, "prefix") > 512:
        raise FilesError("prefix must be at most 512 UTF-8 bytes.")
    if prefix.startswith("/"):
        raise FilesError("prefix must not start with '/'.")
    if _has_control_char(prefix):
        raise FilesError("prefix must not contain control characters.")
    for segment in prefix.split("/"):
        if segment in (".", ".."):
            raise FilesError("prefix must not contain '.' or '..' segments.")
    return prefix


# ---------------------------------------------------------------------------
# Environment and mode resolution
# ---------------------------------------------------------------------------


def _mode() -> str:
    return os.environ.get("KEELSON_MODE", "").strip().lower()


def _bucket() -> str:
    return os.environ.get("KEELSON_FILES_BUCKET", "").strip()


def _files_prefix() -> str:
    prefix = os.environ.get("KEELSON_FILES_PREFIX", "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


# Platform-owned identifiers whose presence means the app is running on Keelson.
_CORE_IDENTIFIER_ENVS = (
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
)


def _is_platform_env() -> bool:
    return any(os.environ.get(name, "").strip() for name in _CORE_IDENTIFIER_ENVS)


def _has_identity() -> bool:
    """True when the workspace + app identity is present."""
    return bool(os.environ.get("KEELSON_APP_ID", "").strip() and _workspace_id())


def _workspace_id() -> str:
    """Return the canonical workspace ID with the legacy env as fallback."""
    return (
        os.environ.get("KEELSON_WORKSPACE_ID", "").strip()
        or os.environ.get("KEELSON_TENANT_ID", "").strip()
    )


def _resolve_mode() -> str:
    """Resolve the storage mode: ``"remote"`` (GCS) or ``"local"`` (filesystem).

    ``KEELSON_MODE`` is the single mode signal; the storage backend is never
    inferred from remote storage variables. Resolution fails closed:

    - ``KEELSON_MODE=keelson`` → remote. Requires ``KEELSON_FILES_BUCKET`` /
      ``KEELSON_FILES_PREFIX`` **and** the platform identity
      (``KEELSON_APP_ID`` / ``KEELSON_WORKSPACE_ID``); any missing → ``FilesError``
      (capability unavailable). ADC availability is validated at operation time.
    - Partial config (exactly one of bucket / prefix) → ``FilesError`` in any
      mode.
    - ``KEELSON_MODE=local`` → local filesystem.
    - ``KEELSON_MODE`` unset → local (zero-config development), unless a
      platform environment is visible, in which case the silent local fallback
      is refused (``FilesError``). The remote env is **not** consulted here.
    - Any other non-empty ``KEELSON_MODE`` → ``FilesError``.
    """
    has_bucket = bool(_bucket())
    has_prefix = bool(_files_prefix())
    mode = _mode()

    if has_bucket != has_prefix:
        missing = "KEELSON_FILES_PREFIX" if has_bucket else "KEELSON_FILES_BUCKET"
        raise FilesError(
            "Incomplete remote Files configuration: both KEELSON_FILES_BUCKET "
            f"and KEELSON_FILES_PREFIX are required, but {missing} is missing."
        )

    if mode == "keelson":
        if not (has_bucket and has_prefix):
            raise FilesError(
                "KEELSON_MODE=keelson but Files is not configured "
                "(KEELSON_FILES_BUCKET and KEELSON_FILES_PREFIX are unset); the "
                "Files capability is unavailable for this deployment."
            )
        if not _has_identity():
            raise FilesError(
                "KEELSON_MODE=keelson but the platform identity is missing "
                "(KEELSON_APP_ID and KEELSON_WORKSPACE_ID must be set; "
                "KEELSON_TENANT_ID remains a deprecated alias); the Files "
                "capability is unavailable for this deployment."
            )
        return "remote"

    if mode == "local":
        return "local"

    if mode == "":
        if _is_platform_env():
            raise FilesError(
                "Platform environment detected "
                "(KEELSON_APP_ID / KEELSON_WORKSPACE_ID (or deprecated "
                "KEELSON_TENANT_ID alias) / KEELSON_DEPLOY_ID set) but "
                "KEELSON_MODE is unset; refusing to fall back to local storage. "
                "Set KEELSON_MODE=local for local development or KEELSON_MODE=keelson "
                "for platform storage."
            )
        return "local"

    raise FilesError(
        f'Unrecognized KEELSON_MODE="{mode}"; expected "keelson" or "local" '
        "(or unset for local development)."
    )


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------
#
# Storage layout: each key maps to a literal file
# ``./.keelson/files/<key>`` — the key IS the real file path, for visual
# debuggability. Nested keys create parent directories.
#
# A key and a nested key that shadows it (e.g. both ``cache`` and ``cache/item``)
# cannot coexist on a filesystem; this is an inherent limitation of the
# literal layout. Changing the on-disk format would make local files less
# directly inspectable. The collision is surfaced as an explicit
# ``FilesError`` on ``write``; ``read`` / ``delete`` of a key shadowed by a
# directory are treated as missing / idempotent (matching the GCS 404).
#
# Confinement is TOCTOU-safe: every operation descends the key's path
# component-by-component with ``openat`` (``os.open(..., dir_fd=...)``) using
# ``O_NOFOLLOW | O_DIRECTORY``, holding a stable directory descriptor, and then
# opens / renames / unlinks the final element RELATIVE to that descriptor. An
# ancestor swapped to a symlink after a check — even mid-operation — cannot
# redirect the operation outside the files dir (the fd references the real
# inode, not a re-resolved path). Temp files use a control-char prefix (never a
# valid key segment) in the target's own directory, so the atomic rename is
# always same-filesystem and ``list`` never surfaces them.

_TMP_PREFIX = "\x01tmp"
_WINDOWS_TMP_PREFIX = ".keelson-tmp-"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_WINDOWS_RESERVED_STEM = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])$",
    re.IGNORECASE,
)
_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"\\|?*]')
_WINDOWS_INTERNAL_TEMP_NAME = re.compile(r"^\.keelson-tmp-[0-9a-f]{24}$", re.IGNORECASE)
_WINDOWS_NAME_SURROGATE_BIT = 0x20000000


class _ConfinementError(FilesError):
    """A path component escapes the files directory (e.g. a symlinked ancestor)."""


def _local_dir() -> Path:
    return Path(os.environ.get("KEELSON_FILES_DIR", "./.keelson/files"))


def _split_key(key: str) -> tuple[list[str], str]:
    parts = key.split("/")
    return parts[:-1], parts[-1]


def _supports_dir_fd_backend() -> bool:
    """Whether this Python runtime exposes the POSIX descriptor APIs we use."""
    return os.name == "posix"


def _validate_windows_local_path(value: str, *, os_name: str | None = None) -> None:
    """Reject key segments that Windows interprets as filesystem syntax."""
    if (os_name or os.name) != "nt":
        return
    for segment in value.split("/"):
        if not segment:  # trailing slash is valid for list prefixes
            continue
        if (
            _WINDOWS_INVALID_CHARS.search(segment)
            or segment.endswith((".", " "))
            or _is_windows_reserved_name(segment)
            or _WINDOWS_INTERNAL_TEMP_NAME.fullmatch(segment)
        ):
            raise FilesError(
                "key or prefix contains a name that Windows cannot store "
                f"locally: {segment!r}."
            )


def _is_windows_reserved_name(segment: str) -> bool:
    """Match Win32 device names after its stem and space normalization."""
    stem = segment.split(".", 1)[0].rstrip(" ")
    return _WINDOWS_RESERVED_STEM.fullmatch(stem) is not None


def _is_path_redirect(info: os.stat_result) -> bool:
    """Return whether an lstat result can redirect path resolution on Windows.

    ``Path.is_junction`` was only added in Python 3.12. The Windows stat fields
    used here have existed since Python 3.8, so this also protects supported
    Python 3.10 and 3.11 runtimes. Unknown reparse points are rejected
    conservatively when the runtime does not expose their tag.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not getattr(info, "st_file_attributes", 0) & reparse_flag:
        return False
    tag = getattr(info, "st_reparse_tag", None)
    return tag is None or bool(tag & _WINDOWS_NAME_SURROGATE_BIT)


def _open_local_path_for_read(path: Path):
    """Open a local file without preventing an atomic replace on Windows."""
    if os.name != "nt":
        return path.open("rb")

    # CPython's regular open() does not request FILE_SHARE_DELETE, so another
    # SDK writer cannot atomically replace the file while a reader holds it.
    # CreateFileW supplies Unix-like sharing while OPEN_REPARSE_POINT prevents
    # a final component swapped to a symlink from being followed.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ|WRITE|DELETE
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _open_base_fd(*, create: bool) -> int:
    base = str(_local_dir())
    if create:
        os.makedirs(base, exist_ok=True)
    return os.open(base, os.O_RDONLY | _O_DIRECTORY)


def _descend_to_parent(base_fd: int, dirs: list[str], *, create: bool) -> int:
    """Descend ``dirs`` from ``base_fd`` with ``openat`` + ``O_NOFOLLOW``.

    Returns the parent directory fd (caller closes it). Closes ``base_fd`` and
    any intermediate fd on the way. Raises :class:`_ConfinementError` when a
    component is a symlink (escape), or ``OSError`` (ENOTDIR for a file
    component, ENOENT for a missing component); the caller maps these to the
    contract. Safety comes from ``O_NOFOLLOW`` on every ``openat`` — the
    ``lstat`` symlink check only classifies the error, and a symlink swapped in
    after it still fails the ``O_NOFOLLOW`` open.
    """
    fd = base_fd
    try:
        for comp in dirs:
            if create:
                try:
                    os.mkdir(comp, dir_fd=fd)
                except FileExistsError:
                    pass
            if stat.S_ISLNK(os.lstat(comp, dir_fd=fd).st_mode):
                raise _ConfinementError("resolved path escapes the files directory.")
            nfd = os.open(comp, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _collision_error(key: str) -> FilesError:
    return FilesError(
        f"cannot write key {key!r} in local mode: it collides with a nested key "
        "on the filesystem (the object store allows both a key and keys under "
        "it, but the literal local file layout cannot represent both)."
    )


def _write_local_fd(key: str, data: bytes) -> None:
    dirs, name = _split_key(key)
    try:
        parent_fd = _descend_to_parent(_open_base_fd(create=True), dirs, create=True)
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.EEXIST):
            raise _collision_error(key) from exc  # a parent segment is a file
        if exc.errno == errno.ELOOP:
            raise FilesError("resolved path escapes the files directory.") from exc
        raise FilesError(f"write failed for key {key!r}: {exc}") from exc
    tmp_name = _TMP_PREFIX + secrets.token_hex(12)
    try:
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o644,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(data)
            os.rename(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except (IsADirectoryError, NotADirectoryError) as exc:
            _unlink_at(parent_fd, tmp_name)
            raise _collision_error(key) from exc  # target is a directory
        except OSError as exc:
            _unlink_at(parent_fd, tmp_name)
            if exc.errno == errno.ENOTEMPTY:
                raise _collision_error(key) from exc
            raise FilesError(f"write failed for key {key!r}: {exc}") from exc
    finally:
        os.close(parent_fd)


def _unlink_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _read_local_fd(key: str) -> bytes | None:
    dirs, name = _split_key(key)
    try:
        base_fd = _open_base_fd(create=False)
    except FileNotFoundError:
        return None
    try:
        parent_fd = _descend_to_parent(base_fd, dirs, create=False)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None  # absent, or a parent path component is a file
        if exc.errno == errno.ELOOP:
            raise FilesError("resolved path escapes the files directory.") from exc
        raise FilesError(f"read failed for key {key!r}: {exc}") from exc
    try:
        try:
            file_fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent_fd)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise FilesError("resolved path escapes the files directory.") from exc
            raise FilesError(f"read failed for key {key!r}: {exc}") from exc
        try:
            with os.fdopen(file_fd, "rb") as handle:
                return handle.read()
        except IsADirectoryError:
            return None  # a nested key occupies this path as a directory
    finally:
        os.close(parent_fd)


def _delete_local_fd(key: str) -> None:
    dirs, name = _split_key(key)
    try:
        base_fd = _open_base_fd(create=False)
    except FileNotFoundError:
        return
    try:
        parent_fd = _descend_to_parent(base_fd, dirs, create=False)
    except _ConfinementError:
        # A symlinked ancestor → nothing to delete inside the files dir → no-op.
        return
    except OSError as exc:
        # Absent / shadowed by a file component → nothing to delete → no-op.
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return
        raise FilesError(f"delete failed for key {key!r}: {exc}") from exc
    try:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            pass  # absent or shadowed by a directory → idempotent no-op
        except OSError as exc:
            if exc.errno in (errno.EISDIR, errno.EPERM, errno.ENOTEMPTY):
                return
            raise FilesError(f"delete failed for key {key!r}: {exc}") from exc
    finally:
        os.close(parent_fd)


def _list_local_fd(prefix: str) -> list[str]:
    try:
        base_fd = _open_base_fd(create=False)
    except FileNotFoundError:
        return []
    keys: list[str] = []

    def _walk(dir_fd: int, rel_prefix: str) -> None:
        with os.scandir(dir_fd) as it:
            # ``list`` is shadowed by the public API function in this module.
            entries = builtins.list(it)
        for entry in entries:
            rel = f"{rel_prefix}{entry.name}"
            if entry.is_symlink():
                continue  # never follow or list symlinks
            if entry.is_dir(follow_symlinks=False):
                try:
                    sub_fd = os.open(
                        entry.name,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=dir_fd,
                    )
                except OSError:
                    continue
                try:
                    _walk(sub_fd, f"{rel}/")
                finally:
                    os.close(sub_fd)
            elif entry.is_file(follow_symlinks=False):
                # Temp files use a control-char prefix no valid key can have.
                if entry.name.startswith(_TMP_PREFIX):
                    continue
                if rel.startswith(prefix):
                    keys.append(rel)

    try:
        _walk(base_fd, "")
    finally:
        os.close(base_fd)
    keys.sort()
    return keys


# ---------------------------------------------------------------------------
# Path-based local-development backend (Windows)
# ---------------------------------------------------------------------------
#
# CPython does not support ``dir_fd`` on Windows. This backend preserves the
# same literal layout, validates Windows path syntax, resolves the storage root,
# and rejects path-redirecting reparse points observed with ``lstat``. Like the
# Node non-Linux backend, every path operation (including read and list) has a
# check-then-act window and is used only for local development.


def _local_base_path(*, create: bool) -> Path:
    base = _local_dir().absolute()
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base.resolve(strict=True)


def _descend_path(base: Path, dirs: list[str], *, create: bool) -> Path:
    current = base
    for comp in dirs:
        next_path = current / comp
        if create:
            try:
                next_path.mkdir()
            except FileExistsError:
                pass
        info = next_path.lstat()
        if _is_path_redirect(info):
            raise _ConfinementError("resolved path escapes the files directory.")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(next_path))
        current = next_path
    if os.path.commonpath((str(base), str(current))) != str(base):
        raise _ConfinementError("resolved path escapes the files directory.")
    return current


def _write_local_path(key: str, data: bytes) -> None:
    _validate_windows_local_path(key)
    dirs, name = _split_key(key)
    try:
        parent = _descend_path(_local_base_path(create=True), dirs, create=True)
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.EEXIST):
            raise _collision_error(key) from exc
        raise FilesError(f"write failed for key {key!r}: {exc}") from exc

    tmp = parent / (_WINDOWS_TMP_PREFIX + secrets.token_hex(12))
    target = parent / name
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            attempts = 20 if os.name == "nt" else 1
            for attempt in range(attempts):
                try:
                    os.replace(tmp, target)
                    break
                except OSError as replace_exc:
                    # Windows rename can briefly report access/sharing errors
                    # while another reader closes its handle. Retry regular-file
                    # targets, but let directory collisions reach the classifier
                    # below immediately.
                    if attempt + 1 == attempts or getattr(
                        replace_exc, "winerror", None
                    ) not in (5, 32):
                        raise
                    try:
                        target_info = target.lstat()
                    except OSError:
                        raise
                    if stat.S_ISDIR(target_info.st_mode):
                        raise
                    time.sleep(0.005)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        # Windows maps replacement of an existing directory to EACCES, which
        # is also used for permission and sharing failures. Inspect the target
        # after the failed replace and classify only an actual directory as a
        # key/nested-key collision.
        try:
            target_info = target.lstat()
        except OSError:
            pass
        else:
            if stat.S_ISDIR(target_info.st_mode) and not _is_path_redirect(target_info):
                raise _collision_error(key) from exc
        if isinstance(exc, (IsADirectoryError, NotADirectoryError)):
            raise _collision_error(key) from exc
        raise FilesError(f"write failed for key {key!r}: {exc}") from exc


def _read_local_path(key: str) -> bytes | None:
    _validate_windows_local_path(key)
    dirs, name = _split_key(key)
    try:
        parent = _descend_path(_local_base_path(create=False), dirs, create=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise FilesError(f"read failed for key {key!r}: {exc}") from exc
    target = parent / name
    try:
        info = target.lstat()
        if _is_path_redirect(info):
            raise _ConfinementError("resolved path escapes the files directory.")
        if stat.S_ISDIR(info.st_mode):
            return None
        with _open_local_path_for_read(target) as handle:
            return handle.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except IsADirectoryError:
        return None
    except OSError as exc:
        raise FilesError(f"read failed for key {key!r}: {exc}") from exc


def _delete_local_path(key: str) -> None:
    _validate_windows_local_path(key)
    dirs, name = _split_key(key)
    try:
        parent = _descend_path(_local_base_path(create=False), dirs, create=False)
    except (_ConfinementError, FileNotFoundError, NotADirectoryError):
        return
    target = parent / name
    try:
        info = target.lstat()
        if _is_path_redirect(info) or stat.S_ISDIR(info.st_mode):
            return
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise FilesError(f"delete failed for key {key!r}: {exc}") from exc
    try:
        target.unlink()
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise FilesError(f"delete failed for key {key!r}: {exc}") from exc


def _list_local_path(prefix: str) -> list[str]:
    _validate_windows_local_path(prefix)
    try:
        base = _local_base_path(create=False)
    except FileNotFoundError:
        return []
    keys: list[str] = []

    def _walk(directory: Path, rel_prefix: str) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if _is_path_redirect(info):
                    continue
                rel = f"{rel_prefix}{entry.name}"
                if stat.S_ISDIR(info.st_mode):
                    _walk(Path(entry.path), f"{rel}/")
                elif stat.S_ISREG(info.st_mode):
                    if _WINDOWS_INTERNAL_TEMP_NAME.fullmatch(entry.name):
                        continue
                    if rel.startswith(prefix):
                        keys.append(rel)

    _walk(base, "")
    keys.sort()
    return keys


def _write_local(key: str, data: bytes) -> None:
    if _supports_dir_fd_backend():
        _write_local_fd(key, data)
    else:
        _write_local_path(key, data)


def _read_local(key: str) -> bytes | None:
    if _supports_dir_fd_backend():
        return _read_local_fd(key)
    return _read_local_path(key)


def _delete_local(key: str) -> None:
    if _supports_dir_fd_backend():
        _delete_local_fd(key)
    else:
        _delete_local_path(key)


def _list_local(prefix: str) -> list[str]:
    if _supports_dir_fd_backend():
        return _list_local_fd(prefix)
    return _list_local_path(prefix)


# ---------------------------------------------------------------------------
# GCS (remote) backend — ADC over the GCE metadata server
# ---------------------------------------------------------------------------

_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}


def _clear_token_cache() -> None:
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0


def _storage_base() -> str:
    return os.environ.get("KEELSON_FILES_STORAGE_BASE", _DEFAULT_STORAGE_BASE).rstrip(
        "/"
    )


def _metadata_url() -> str:
    return os.environ.get("KEELSON_FILES_METADATA_URL", _DEFAULT_METADATA_URL)


def _access_token() -> str:
    now = time.time()
    cached = _token_cache["token"]
    if isinstance(cached, str) and now < float(_token_cache["expires_at"]):  # type: ignore[arg-type]
        return cached
    req = Request(
        _metadata_url(),
        headers={"Metadata-Flavor": "Google", "User-Agent": _SDK_USER_AGENT},
    )
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            raw = resp.read()
    except (HTTPError, URLError) as exc:
        raise FilesError(f"failed to obtain ADC access token: {exc}") from exc
    try:
        # ``.decode`` (UnicodeDecodeError is a ValueError) and the schema access
        # (json / dict / int) are all inside this guard → typed FilesError.
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("token response is not a JSON object")
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
    except (ValueError, TypeError, AttributeError) as exc:
        raise FilesError(
            f"malformed access-token response from the metadata server: {exc}"
        ) from exc
    if not isinstance(token, str) or not token:
        raise FilesError("metadata server returned no access_token.")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(0, expires_in - 60)
    return token


def _object_name(key: str) -> str:
    return f"{_files_prefix()}{key}"


def _gcs_request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    allow_not_found: bool = False,
) -> tuple[int, bytes]:
    request_headers = {
        "Authorization": f"Bearer {_access_token()}",
        "User-Agent": _SDK_USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    req = Request(url, data=data, method=method, headers=request_headers)
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.status, resp.read()
    except HTTPError as exc:
        # Only 404 is "missing"; 401 / 403 / 429 / 5xx are real failures.
        if allow_not_found and exc.code == 404:
            return 404, b""
        detail = exc.read().decode("utf-8", errors="replace")
        raise FilesError(f"{method} {url} failed with {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FilesError(f"{method} {url} failed: {exc}") from exc


def _object_url(key: str) -> str:
    obj = quote(_object_name(key), safe="")
    return f"{_storage_base()}/storage/v1/b/{_bucket()}/o/{obj}"


def _write_remote(key: str, data: bytes) -> None:
    obj = quote(_object_name(key), safe="")
    url = (
        f"{_storage_base()}/upload/storage/v1/b/{_bucket()}/o"
        f"?uploadType=media&name={obj}"
    )
    _gcs_request(
        "POST",
        url,
        data=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def _read_remote(key: str) -> bytes | None:
    status, body = _gcs_request(
        "GET", f"{_object_url(key)}?alt=media", allow_not_found=True
    )
    if status == 404:
        return None
    return body


def _delete_remote(key: str) -> None:
    _gcs_request("DELETE", _object_url(key), allow_not_found=True)


def _list_remote(prefix: str) -> list[str]:
    files_prefix = _files_prefix()
    full_prefix = quote(f"{files_prefix}{prefix}", safe="")
    base = f"{_storage_base()}/storage/v1/b/{_bucket()}/o"
    keys: list[str] = []
    page_token: str | None = None
    while True:
        url = f"{base}?prefix={full_prefix}"
        if page_token:
            url += f"&pageToken={quote(page_token, safe='')}"
        _, body = _gcs_request("GET", url)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
            # Validate the payload SCHEMA identically to Node/Go: a present
            # ``items`` must be an array, each item a non-null object with a
            # present string ``name``, and a present ``nextPageToken`` a string.
            # Absence is allowed; any present-but-wrong-type value (including an
            # explicit ``null``) is a FilesError, never a silent empty page.
            if not isinstance(payload, dict):
                raise TypeError("list response is not a JSON object")
            # ``list`` is shadowed by the public API function in this module.
            items = payload["items"] if "items" in payload else []
            if not isinstance(items, builtins.list):
                raise TypeError("list response 'items' is not an array")
            for item in items:
                if not isinstance(item, dict):
                    raise TypeError("list item is not a non-null object")
                name = item.get("name")
                if not isinstance(name, str):
                    raise TypeError("list item 'name' is missing or not a string")
                if not name.startswith(files_prefix):
                    continue
                key = name[len(files_prefix) :]
                if key:
                    keys.append(key)
            if "nextPageToken" in payload:
                page_token = payload["nextPageToken"]
                if not isinstance(page_token, str):
                    raise TypeError("'nextPageToken' is present but not a string")
            else:
                page_token = None
        except (ValueError, TypeError, AttributeError) as exc:
            raise FilesError(f"malformed list response from GCS: {exc}") from exc
        if not page_token:
            break
    keys.sort()
    return keys


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write(key: str, data: bytes | str) -> None:
    """Write ``data`` at ``key``, overwriting any existing value.

    ``str`` is stored as UTF-8. Write-through: once this returns, the data is
    persisted. Raises :class:`FilesError` if the object exceeds
    :data:`MAX_OBJECT_SIZE_BYTES`.
    """
    validated = _validate_key(key)
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise FilesError("data must be bytes or str.")
    if len(data) > MAX_OBJECT_SIZE_BYTES:
        raise FilesError(
            f"object is {len(data)} bytes, exceeding the "
            f"{MAX_OBJECT_SIZE_BYTES}-byte limit."
        )
    if _resolve_mode() == "remote":
        _write_remote(validated, data)
    else:
        _write_local(validated, data)


def read(key: str) -> bytes | None:
    """Read the bytes stored at ``key``, or ``None`` when the key is absent.

    Absence is the normal case (unlike ``media.get``, which raises); only a
    404 maps to ``None`` — 401 / 403 / 429 / 5xx raise :class:`FilesError`.
    """
    validated = _validate_key(key)
    if _resolve_mode() == "remote":
        return _read_remote(validated)
    return _read_local(validated)


def delete(key: str) -> None:
    """Delete ``key``. Idempotent: no error when the key does not exist."""
    validated = _validate_key(key)
    if _resolve_mode() == "remote":
        _delete_remote(validated)
    else:
        _delete_local(validated)


def list(prefix: str = "") -> builtins.list[str]:  # noqa: A001
    """Return every key (optionally under ``prefix``) in lexicographic order.

    Paging is absorbed internally; the returned list is the full set of keys.
    """
    validated = _validate_prefix(prefix)
    if _resolve_mode() == "remote":
        return _list_remote(validated)
    return _list_local(validated)
