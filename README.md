# Keelson Python SDK

Python SDK for building apps on the Keelson platform. Provides four modules:

> **Note**: This repository is a read-only release mirror. Development happens in the private Keelson monorepo; issues are welcome here, but pull requests are not accepted — changes land through the next release.

| Module | Import | Description |
|--------|--------|-------------|
| `keelson_media` | `from keelson import media` | Media storage (upload, serve by ID) |
| `keelson_files` | `from keelson import files` | Data files (key-addressed, overwrite, private) |
| `keelson_identity` | `from keelson import identity` | User identity and directory |
| `keelson_email` | `from keelson import email` | Inbound/outbound email |

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
| unset | No Media env, platform core env visible (`KEELSON_APP_ID` / `KEELSON_TENANT_ID` / `KEELSON_DEPLOY_ID`) | **`MediaError`** — refuses silent local fallback |
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

## Identity SDK (`keelson_identity`)

User identity and tenant directory lookup. In production, the Keelson auth
gateway injects trusted `X-Keelson-User-*` headers before requests reach the app.
Use `get_current_user` when the basic user profile is enough; use
`get_current_identity` when the app needs tenant role, app permissions, app
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
print(identity.tenant.role)
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
| `list_members` | `(*, base_url=None, cookie=None, authorization=None, app_token=None, **filters) -> PaginatedMembers` | List tenant members |
| `get_user` | `(user_id, *, base_url=None, cookie=None, authorization=None, app_token=None) -> MemberItem` | Get user by ID |
| `list_groups` | `(*, base_url=None, cookie=None, authorization=None, app_token=None) -> list[GroupItem]` | List tenant groups |

`get_current_user` and `get_current_identity` accept common request header
mappings. The required header is `x-keelson-user-id`; `x-keelson-user-email`
and `x-keelson-user-name` are optional.

Directory functions also support app-as-actor access with `app_token` or the
`KEELSON_DIRECTORY_TOKEN` env fallback. Keep app tokens on the server.

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `is_local_mode` | `() -> bool` | Check if running in local mode |

**Data classes**: `CurrentIdentity`, `UserIdentity`, `TenantIdentity`, `AppIdentity`, `AttributesIdentity`, `MemberItem`, `PaginatedMembers`, `GroupItem`.

**Exception**: `IdentityError`.

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
- **`list_groups()`**: all groups in the *tenant* (each with its stable `id` and `key`). Use for building UI pickers and admin views.

### Modes

| Mode | Condition | Behaviour |
|------|-----------|-----------|
| Local | `KEELSON_LOCAL_MODE=1` | Returns deterministic fixture data (no HTTP calls) |
| Keelson | Default | Calls the Keelson auth gateway via the platform-injected `KEELSON_DIRECTORY_BASE_URL`. |

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_LOCAL_MODE` | Set to `1` to enable local mode (returns fixture data, no HTTP calls). |
| `KEELSON_DIRECTORY_BASE_URL` | **Canonical, platform-injected** base URL for `get_current_identity` and Directory calls. Use this. |
| `KEELSON_DIRECTORY_TOKEN` | App token for app-as-actor identity and Directory access. |
| `KEELSON_IDENTITY_BASE_URL` | Deprecated compatibility alias used only when neither `base_url` nor `KEELSON_DIRECTORY_BASE_URL` is set. |

---

## Email SDK (`keelson_email`)

Inbound/outbound email.

```python
from keelson import email

# Send
email.send(
    to="user@example.com",
    subject="Hello",
    text="Plain text body",
    html="<p>HTML body</p>",
)

# Receive inbound emails via decorator
@email.on_receive
def handle(msg: email.InboundMessage):
    print(msg.subject, msg.text)
    email.send(
        to=msg.reply_to or msg.from_,
        subject=f"Re: {msg.subject}",
        text="Got it!",
        in_reply_to=msg.provider_message_id,
    )

# Handle bounce/complaint events
@email.on_event
def handle_event(event: email.EmailEventPayload):
    if event.event_type == "bounce":
        print("Bounced:", event.email_address)
```

### Cross-language guaranteed API

| Function | Signature | Description |
|----------|-----------|-------------|
| `send` | `(to, subject, text=None, html=None, **kwargs)` | Send an email |
| `verify_webhook` | `(body, headers, secret) -> InboundMessage` | Verify Svix signature and parse inbound email |
| `verify_webhook_bytes` | `(body, headers, secret) -> InboundMessage` | Verify inbound email from raw bytes |
| `verify_event_webhook` | `(body, headers, secret) -> EmailEventPayload` | Verify Svix signature and parse event |
| `verify_event_webhook_bytes` | `(body, headers, secret) -> EmailEventPayload` | Verify event from raw bytes |

Attachment download is also guaranteed; in Python it is an instance method on `InboundAttachment`:

```python
for att in msg.attachments:
    data = att.download()  # -> bytes
```

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `on_receive` | `(handler)` | Decorator to handle inbound email (auto-starts webhook server) |
| `on_event` | `(handler)` | Decorator to handle bounce/complaint/delivery events |
| `serve` | `(**kwargs)` | Start webhook server manually |

When `KEELSON_EMAIL_WEBHOOK_SECRET` is set, `serve()` and the decorators automatically verify inbound webhooks.

**Data classes**: `Address`, `Attachment`, `InboundMessage`, `InboundAttachment`, `EmailEventPayload`, `SpamAssessment`, `AuthenticationResult`.

**Exception**: `EmailError`.

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_EMAIL_API_URL` | Email API endpoint (required). |
| `KEELSON_EMAIL_TOKEN` | Bearer token (required). |
| `KEELSON_EMAIL_WEBHOOK_SECRET` | Optional Svix signing secret for auto-verification. |

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
uv run pytest keelson_email/tests/
```

Lint:

```bash
uv run ruff check .
```
