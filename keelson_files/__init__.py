"""Keelson data-files SDK (``files``).

Key-addressed, overwrite, whole-value durable file storage for an app's own
files (state, settings, caches). Distinct from ``keelson_media`` — see the
module docstring in :mod:`keelson_files.client`.

    from keelson import files

    files.write("seen_urls.json", json.dumps(seen))
    seen = json.loads(files.read("seen_urls.json") or "[]")
    keys = files.list()
    files.delete("seen_urls.json")
"""

from keelson_files.client import (
    MAX_OBJECT_SIZE_BYTES,
    FilesError,
    delete,
    list,
    read,
    write,
)

__all__ = [
    "MAX_OBJECT_SIZE_BYTES",
    "FilesError",
    "write",
    "read",
    "delete",
    "list",
]
