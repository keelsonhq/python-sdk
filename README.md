# Keelson Python SDK

Python SDK for building apps on the [Keelson](https://keelson.dev) platform ([SDK guide](https://keelson.dev/docs/building-apps/sdk/)). Provides four modules:

> **Note**: This repository is a read-only release mirror. Development happens in the private Keelson monorepo; issues are welcome here, but pull requests are not accepted — changes land through the next release.

| Module | Import | Description |
|--------|--------|-------------|
| `keelson_media` | `from keelson import media` | Media storage (upload, serve by ID) |
| `keelson_files` | `from keelson import files` | Data files (key-addressed, overwrite, private) |
| `keelson_identity` | `from keelson import identity` | User identity and directory |
| `keelson_tasks` | `from keelson import tasks` | Background tasks (enqueue, get) |

Cross-language parity across Node, Python, and Go is defined in
[`./PARITY.md`](./PARITY.md). APIs below are labelled as
**guaranteed** (same capability in all 3 languages) or
**Python-specific** (convenience helpers unique to this SDK).

## Installation

```bash
pip install keelson-sdk
```

The PyPI distribution is named `keelson-sdk`; Python imports continue to use
`keelson` and the domain modules shown below.

---

## Media SDK (`keelson_media`)

Upload immutable media (images, PDFs, generated assets) and serve it by ID. On
Keelson it uses the managed media storage (internal media API); for local
development it uses the local filesystem. The runtime-mode contract is
**fail-closed**: it never silently writes to ephemeral local storage when platform
Media configuration is missing or incomplete (see Modes below).

```python
from keelson import media

# Store a file — returns a generated ULID file ID
file_id = media.put(b"Hello, world!", content_type="text/plain")

# Store with filename (MIME type auto-detected)
with open("report.pdf", "rb") as f:
    pdf_id = media.put(f.read(), filename="report.pdf")

# Retrieve
data = media.get(file_id)

# Check existence and metadata
if media.exists(file_id):
    info = media.stat(file_id)
    print(info.content_type, info.content_length)

# Public URL (for embedding in HTML)
src = media.url(file_id)  # "/media/01HXYZ..."

# Delete
media.delete(file_id)
```

### Cross-language guaranteed API

| Function | Signature | Description |
|----------|-----------|-------------|
| `put` | `(data, *, content_type=None, filename=None) -> str` | Store file, returns ULID `file_id` |
| `get` | `(file_id) -> bytes` | Download file content |
| `delete` | `(file_id) -> None` | Delete file |
| `exists` | `(file_id) -> bool` | Check if file exists |
| `stat` | `(file_id) -> MediaStat` | Get metadata (content_type, content_length, status) |
| `url` | `(file_id) -> str` | Generate public URL path |

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `open` | `(file_id) -> BinaryIO` | Get file as binary stream (wraps `get()` in `BytesIO`) |

**Data class**: `MediaStat` (frozen dataclass with `content_type: str`, `content_length: int`, `status: int`).

**Exception**: `MediaError` (subclass of `RuntimeError`).

### Modes (fail-closed runtime-mode contract)

| `KEELSON_MODE` | Condition | Behaviour |
|------|-----------|-----------|
| `keelson` | Both Media env set | Remote (the Keelson media service) |
| `keelson` | Media env missing | **`MediaError`** — capability unavailable (covers `files_enabled=false`); never local |
| any | Exactly one of base URL / token set | **`MediaError`** — incomplete remote config |
| `local` | — | Local filesystem (`MEDIA_DIR`, default `./media`) |
| unset | Both Media env set | Remote (backward compatibility) |
| unset | No Media env, platform core env visible (`KEELSON_APP_ID` / `KEELSON_WORKSPACE_ID` / `KEELSON_DEPLOY_ID`) | **`MediaError`** — refuses silent local fallback |
| unset | No Media env, no platform env | Local filesystem (local development) |

The SDK never silently falls back to ephemeral local storage on Keelson: set
`KEELSON_MODE=local` explicitly for local development.

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_MODE` | `keelson` (remote, fail-closed) / `local` (local FS) / unset (local development). Platform injects `keelson`. |
| `KEELSON_INTERNAL_MEDIA_BASE_URL` | Internal endpoint for the Keelson media service (Keelson mode; required with the token). |
| `KEELSON_APP_MEDIA_TOKEN` | App-scoped bearer token for the Keelson media service; validated by the platform. |
| `KEELSON_MEDIA_URL_PREFIX` | Public URL prefix (default: `/media/`). |
| `MEDIA_DIR` | Local storage directory (default: `./media`). Used whenever the SDK resolves to local mode — either explicit `KEELSON_MODE=local`, or zero-config local development (`KEELSON_MODE` unset with no Media env and no platform core env). |

---

## Files (data) SDK (`keelson_files`)

Durable file storage for your app's own files — state, settings, caches. Reads
and writes are always whole-file, and `write()` is write-through: once it
returns, the data is persisted. There is no background sync and nothing is
stored on ephemeral local disk. Overwriting an existing key is the normal case;
updates to the same key are limited to about once per second. For user-uploaded
or generated media referenced by ID and served over HTTP, use `keelson_media`;
for data read/written on every request, use the database.

```python
from keelson import files

files.write("seen_urls.json", json.dumps(seen))
seen = json.loads(files.read("seen_urls.json") or "[]")  # read() -> bytes | None
keys = files.list()                                       # sorted list[str]
files.delete("seen_urls.json")                            # idempotent
```

### Cross-language guaranteed API

| Function | Description |
|----------|-------------|
| `write(key, data)` | Overwrite `key` with bytes/str (str stored UTF-8); write-through |
| `read(key)` | `bytes`, or `None` when the key is absent (only a 404 is missing) |
| `delete(key)` | Idempotent delete |
| `list(prefix="")` | Full, lexicographically-sorted key list; paging absorbed |

Key grammar: `/`-separated relative path, well-formed UTF-8 ≤ 512 bytes total and
≤ 255 bytes per segment, no leading/trailing `/`, no empty / `.` / `..` segments,
no control characters. One-object soft limit 10 MiB. There is no `exists()` —
`read()` returning `None` covers it.

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_MODE` | `keelson` (remote) or `local`; the single mode signal |
| `KEELSON_FILES_BUCKET` / `KEELSON_FILES_PREFIX` | Platform-injected in `keelson` mode (managed object storage) |
| `KEELSON_FILES_DIR` | Local-mode directory (default `./.keelson/files`) |

Fail-closed: `KEELSON_MODE=keelson` requires bucket + prefix + platform identity;
missing config raises `FilesError`. See [`./PARITY.md`](./PARITY.md) for the
full contract.

---

## Tasks SDK (`keelson_tasks`)

Enqueue a run of a command declared under `tasks:` in `keelson.yaml`, and
read its state. On Keelson the platform runs the command once on a separate
instance, passes the payload as one JSON line on stdin, and retries failed
attempts (at-least-once: make the command safe to run twice). The SDK does not
receive tasks; the command is an ordinary program that reads stdin.

```yaml
# keelson.yaml
tasks:
  - name: generate-pdf
    command: python make_pdf.py
    timeout: 300      # seconds; optional
```

```python
from keelson import tasks

task_id = tasks.enqueue("generate-pdf", {"order_id": 1},
                        idempotency_key="order-1-pdf")
status = tasks.get(task_id)
print(status.status, status.claimed_attempts, status.last_failure_code)

try:
    tasks.enqueue("generate-pdf", {"order_id": 2})
except tasks.TasksError as e:
    if e.code == "TASK_NOT_DECLARED":
        ...
```

### Cross-language guaranteed API

| Function | Description |
|----------|-------------|
| `tasks.enqueue(name, payload=None, idempotency_key=None)` | Enqueue one run; returns the `task_id` (`str`). `payload` is any JSON value; `idempotency_key` is 1–128 printable ASCII characters |
| `tasks.get(task_id)` | `TaskStatus` (frozen dataclass): `task_id`, `name`, `status` (`queued` / `running` / `succeeded` / `failed` / `cancelled`), `claimed_attempts`, `last_failure_code` (`str \| None`), `created_at`, `finished_at` (`str \| None`; RFC 3339 strings) |
| `tasks.TasksError` | The single error type (`code`, `status`, `message`) |

A repeat with the same idempotency key returns the existing `task_id` instead
of enqueueing again. Payloads are serialized with `allow_nan=False`, so `NaN`
is `TASK_INVALID_REQUEST`.

### Errors

Every failure is one `TasksError` with `code`, `status` (the HTTP status, or
`None` when there was no HTTP response), and `message`. Branch on `code`:

| `code` | Meaning |
|--------|---------|
| `TASK_NOT_DECLARED` | The name is not under `tasks:` in the deployed (or local) `keelson.yaml` |
| `TASK_INVALID_REQUEST` | Empty name, malformed idempotency key, or a payload that is not JSON-serializable |
| `TASK_PAYLOAD_TOO_LARGE` | The request body is over 65,536 bytes (checked before sending) |
| `TASK_NOT_FOUND` | `get` of an unknown task ID (local mode: not enqueued in this process) |
| `TASK_BACKLOG_LIMIT_EXCEEDED` / `TASK_MONTHLY_QUOTA_EXCEEDED` | Plan limits; not retried by the SDK |
| `TASKS_UNAVAILABLE` | Intake is closed on the platform. Retrying does not help |
| `TASKS_UNAVAILABLE_TRANSIENT` | A passing outage (502/503/504, connection failure, 15 s timeout), after the SDK's own retries |
| `TASKS_FORBIDDEN` | 403 from Cloud Run. Right after the first deploy that declares `tasks:`, the permission can take a few minutes to propagate |
| `TASKS_UNAUTHORIZED` / `TASKS_IDENTITY_TOKEN_ERROR` | The id token was rejected / could not be fetched from the metadata server |
| `TASKS_SERVER_ERROR` / `TASKS_HTTP_ERROR` / `TASKS_UNEXPECTED_RESPONSE` | Other unexpected responses |
| `TASKS_NOT_CONFIGURED` | Mode resolution failed (below) |
| `TASKS_LOCAL_CLI_NOT_FOUND` / `TASKS_LOCAL_CLI_FAILED` | Local mode: no `keelson` on `PATH` (install: `https://keelson.dev/install.sh`) / the CLI failed (try `keelson upgrade`) |

Retries: only `TASKS_UNAVAILABLE_TRANSIENT` is retried (3 attempts in total,
waiting 0.5 s then 1 s). `get` always retries; enqueue retries **only with an
idempotency key**, because without one a request the server already accepted
would be enqueued twice. The payload never appears in an error message.

### Modes (fail-closed runtime-mode contract)

| Condition | Result |
|-----------|--------|
| `KEELSON_MODE=keelson` + `KEELSON_TASKS_BASE_URL` set | Keelson (runtime API); `KEELSON_APP_ID` is also required |
| `KEELSON_MODE=keelson` + `KEELSON_TASKS_BASE_URL` missing | `TASKS_NOT_CONFIGURED` (declare `tasks:` in `keelson.yaml` and deploy) |
| `KEELSON_MODE=local` | Local (runs the command through the CLI) |
| `KEELSON_MODE` unset + a platform variable (`KEELSON_APP_ID` / `KEELSON_WORKSPACE_ID` / `KEELSON_DEPLOY_ID`) | `TASKS_NOT_CONFIGURED` (never falls back to local on Keelson) |
| `KEELSON_MODE` unset + none of those | Local (zero-config development) |
| Any other `KEELSON_MODE` value | `TASKS_NOT_CONFIGURED` |

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_MODE` | `keelson` (remote) or `local`; the single mode signal |
| `KEELSON_TASKS_BASE_URL` | Platform-injected runtime API URL when the app declares `tasks:`; also the id-token audience |
| `KEELSON_APP_ID` | Platform-injected app ID; the `/internal/apps/{app_id}/...` path segment |

There is no token variable: the id token comes from the Cloud Run metadata
server on every call.

### Local mode

In local mode, enqueue runs `keelson dev task run <name> --payload - --json`
(the `keelson` CLI on `PATH`) from the app's working directory, so start your
dev server in the directory that has `keelson.yaml`. The command receives the
same stdin document as on Keelson, its output goes to your app's stderr, and
enqueue returns **after the command has finished** — a request handler that
enqueues waits for it. A command that exits non-zero or times out is not an
enqueue error: `get` reports `status` `failed` with `last_failure_code`
`exit_nonzero` or `timed_out`.

Differences from Keelson:

- synchronous: the command runs before enqueue returns, in the same machine
- no retry: one attempt only
- no concurrency, backlog, or monthly-quota limits
- the declared `timeout` applies as written (on Keelson it is capped by your
  plan's limit)
- `get` knows only tasks enqueued in the same process; others are
  `TASK_NOT_FOUND`, and a running task is never visible
- the same name + idempotency key returns the existing task ID without running
  again, but two concurrent calls with the same key both run the command

See [`./PARITY.md`](./PARITY.md) for the full contract.

---

## Identity SDK (`keelson_identity`)

User identity and workspace directory lookup. In production, the Keelson auth
gateway injects trusted `X-Keelson-User-*` headers before requests reach the app.
Use `get_current_user` when the basic user profile is enough; use
`get_current_identity` when the app needs workspace role, app permissions, app
roles, or group attributes.

```python
import os

from keelson_identity import (
    get_current_user,
    get_current_identity,
    list_members,
    get_user,
    list_groups,
)

user = get_current_user(headers=request.headers)
print(user.email)

identity = get_current_identity(
    headers=request.headers,
    app_token=os.environ["KEELSON_DIRECTORY_TOKEN"],
)
print(identity.workspace.role)
print(identity.app.permissions)   # ["manage", "view"]
if identity.attributes:
    print(identity.attributes.groups)  # ["developers", "everyone"]

# Directory lookup as the app actor
page = list_members(
    app_token=os.environ["KEELSON_DIRECTORY_TOKEN"],
    limit=25, offset=0, q="alice",
)
for member in page.items:
    print(member.id, member.email, member.name)

member = get_user("user-id-here", app_token=os.environ["KEELSON_DIRECTORY_TOKEN"])
groups = list_groups(app_token=os.environ["KEELSON_DIRECTORY_TOKEN"])
```

### Cross-language guaranteed API

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_current_user` | `(headers=...) -> UserIdentity` | Parse the current user's basic profile from trusted `X-Keelson-User-*` headers; no network call |
| `get_current_identity` | `(headers=..., app_token=...) -> CurrentIdentity` | Fetch the current user's full identity as the app actor |
| `list_members` | `(*, base_url=None, cookie=None, authorization=None, app_token=None, **filters) -> PaginatedMembers` | List workspace members |
| `get_user` | `(user_id, *, base_url=None, cookie=None, authorization=None, app_token=None) -> MemberItem` | Get user by ID |
| `list_groups` | `(*, base_url=None, cookie=None, authorization=None, app_token=None) -> list[GroupItem]` | List workspace groups |

`get_current_user` and `get_current_identity` accept common request header
mappings. The required header is `x-keelson-user-id`; `x-keelson-user-email`
and `x-keelson-user-name` are optional.

Directory functions also support app-as-actor access with `app_token` or the
`KEELSON_DIRECTORY_TOKEN` env fallback. Keep app tokens on the server.

`MemberItem` (from `list_members` items and `get_user`) carries `id`, `email`,
`name`, `role`, and `image_url`. `image_url` is the member's profile image URL
served by Clerk (`img.clerk.com`), or `None` when the member has not uploaded
an image (render initials instead). Append `width` / `height` query parameters
to get a resized image. Store only the member `id` in your app's DB and
re-fetch `image_url` on display rather than relying on the URL to change when
the member replaces their image. Local mode returns `image_url=None` for every member.

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `is_local_mode` | `() -> bool` | Check if running in local mode |

**Data classes**: `CurrentIdentity`, `UserIdentity`, `WorkspaceIdentity`, `AppIdentity`, `AttributesIdentity`, `MemberItem`, `PaginatedMembers`, `GroupItem`.

**Exception**: `IdentityError`.

The former `TenantIdentity` class, `identity.tenant` attribute, `tenant` wire key,
`KEELSON_TENANT_ID`, and `KEELSON_LOCAL_TENANT_ID` /
`KEELSON_LOCAL_TENANT_ROLE` remain deprecated aliases through at least the next
major SDK version.

### Filtering members by group

`list_members` narrows results to a single group via either `group_id` or
`group_key`:

- `group_key` — the group's code-facing identifier. Always present, stable, and
  immutable. **Prefer this for code references.** Keys may be non-ASCII (e.g. a
  Japanese `経理`).
- `group_id` — a stable UUID for machine integration / internal wiring.

Passing both raises `IdentityError`. On `GroupItem`, `key` is `str | None`
kept nullable for backward compatibility, but the server always populates it;
`id` is the UUID.

### `attributes.groups` vs `list_groups()`

- **`attributes.groups`** (from `get_current_identity()`): the group keys the *current user* belongs to, **scoped to the current app** — the system groups the caller holds by role (a subset of `owners`/`admins`/`developers`/`everyone`, not all four: an OWNER gets `owners`/`developers`/`everyone`, an app user gets only `everyone`; exposed regardless of app binding) plus custom groups bound to this app (via a view/manage permission binding or an app-role binding). Custom groups not bound to the app are excluded, and a system group the caller does not hold never appears. Keys are stable and immutable, so they are safe for authorization checks; bind a group to the app if you need to branch on it.
- **`list_groups()`**: all groups in the *workspace* (each with its stable `id` and `key`). Use for building UI pickers and admin views.

### Modes

| Mode | Condition | Behaviour |
|------|-----------|-----------|
| Local | `KEELSON_LOCAL_MODE=1` | Returns deterministic fixture data (no HTTP calls) |
| Keelson | Default | Calls the Keelson auth gateway via the platform-injected `KEELSON_DIRECTORY_BASE_URL`. |

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_LOCAL_MODE` | Set to `1` to enable local mode (returns fixture data, no HTTP calls). |
| `KEELSON_LOCAL_WORKSPACE_ID` | Override the local workspace ID. |
| `KEELSON_LOCAL_WORKSPACE_ROLE` | Override the local workspace role. |
| `KEELSON_DIRECTORY_BASE_URL` | **Canonical, platform-injected** base URL for `get_current_identity` and Directory calls. Use this. |
| `KEELSON_DIRECTORY_TOKEN` | App token for app-as-actor identity and Directory access. |
| `KEELSON_IDENTITY_BASE_URL` | Deprecated compatibility alias used only when neither `base_url` nor `KEELSON_DIRECTORY_BASE_URL` is set. |

---

## Testing

Run all SDK tests:

```bash
uv sync --group dev
uv run pytest
```

Build the source distribution and wheel from the repository root:

```bash
uv build
```

Run tests for individual modules:

```bash
uv run pytest keelson_media/tests/
uv run pytest keelson_identity/tests/
uv run pytest keelson_tasks/tests/
```

Lint:

```bash
uv run ruff check .
```
