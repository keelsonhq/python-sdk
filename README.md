# Keelson Python SDK

SDK guide: https://keelson.dev/docs/building-apps/sdk/

Python SDK for building apps on the Keelson platform. Provides five modules:

> **Note**: This repository is a read-only release mirror. Development happens in the private Keelson monorepo; issues are welcome here, but pull requests are not accepted — changes land through the next release.

| Module | Import | Description |
|--------|--------|-------------|
| `keelson_media` | `from keelson import media` | Media storage (upload, serve by ID) |
| `keelson_files` | `from keelson import files` | Data files (key-addressed, overwrite, private) |
| `keelson_identity` | `from keelson import identity` | User identity and directory |
| `keelson_tasks` | `from keelson import tasks` | Background tasks (enqueue, get) |
| `keelson_email` | `from keelson import email` | Outbound email and delivery events |

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
| `get_request_user` | `(*, headers=None) -> RequestUser` | The current user plus app permissions (`perms`) from trusted headers; no network call |
| `get_current_identity` | `(headers=..., app_token=...) -> CurrentIdentity` | Fetch the current user's full identity as the app actor |
| `list_members` | `(*, base_url=None, cookie=None, authorization=None, app_token=None, **filters) -> PaginatedMembers` | List workspace members |
| `get_user` | `(user_id, *, base_url=None, cookie=None, authorization=None, app_token=None) -> MemberItem` | Get user by ID |
| `list_groups` | `(*, base_url=None, cookie=None, authorization=None, app_token=None) -> list[GroupItem]` | List workspace groups |

`get_current_user` and `get_current_identity` accept common request header
mappings. The required header is `x-keelson-user-id`; `x-keelson-user-email`
and `x-keelson-user-name` are optional.

`get_request_user` returns `RequestUser(id, email, name, perms)`. `perms` is
`x-keelson-user-app-perms` split on `,` (trimmed, empty items dropped, order
kept), e.g. `["view", "manage"]`; a missing or empty header gives `[]` (machine
and webhook requests carry none). The gateway sends non-ASCII values such as a
Japanese name as raw UTF-8 bytes; when a framework hands them over as a latin-1
string, `get_request_user` restores the UTF-8 text (`get_current_user` does not).

```python
from keelson_identity import get_request_user

user = get_request_user(headers=request.headers)
if "manage" not in user.perms:
    ...  # return 403 from admin endpoints
```

Directory functions also support app-as-actor access with `app_token` or the
`KEELSON_DIRECTORY_TOKEN` env fallback. Keep app tokens on the server.

`MemberItem` (from `list_members` items and `get_user`) carries `id`, `email`,
`name`, `role`, and `image_url`. `image_url` is the member's profile image URL
served by Clerk (`img.clerk.com`), or `None` when the member has not uploaded
an image (render initials instead). Append `width` / `height` query parameters
to get a resized image. Store only the member `id` in your app's DB and
re-fetch `image_url` on display rather than relying on the URL to change when
the member replaces their image. Local mode returns `image_url=None` for every
member unless a local users file sets it.

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `is_local_mode` | `() -> bool` | Check if running in local mode |

**Data classes**: `CurrentIdentity`, `UserIdentity`, `RequestUser`, `WorkspaceIdentity`, `AppIdentity`, `AttributesIdentity`, `MemberItem`, `PaginatedMembers`, `GroupItem`.

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

### Local mode

With `KEELSON_LOCAL_MODE` set to `1` / `true` / `yes`, every identity and
Directory function returns fixed data and never reads headers or calls the
Directory API.

**Refused in a Keelson deployment.** Local mode is for local development only.
If `KEELSON_MODE=keelson` or any of `KEELSON_APP_ID`, `KEELSON_WORKSPACE_ID`,
`KEELSON_TENANT_ID`, `KEELSON_DEPLOY_ID`, `KEELSON_APP_URL` is set, every
function raises `IdentityError` naming the variables found, instead of
returning fixed data or falling back to the production path. Unset
`KEELSON_LOCAL_MODE` there.

**Without a users file**, the fixed user is `KEELSON_LOCAL_USER_ID` /
`_EMAIL` / `_NAME` (default `local-user-001` / `dev@localhost` /
`Local Developer`) with the workspace role `KEELSON_LOCAL_WORKSPACE_ROLE`
(default `OWNER`); members are that user plus Alice / Bob / Carol, and groups
are `everyone` / `developers` / `admins` / `owners`. `get_request_user` returns
the fixed user with `perms=["view", "manage"]`.

**With a users file**, the fixed user, members, groups, and full identity come
from the file. The SDK reads the file named by `KEELSON_LOCAL_USERS_FILE`, or
`./.keelson/dev-users.json` (relative to the working directory) when it exists:

```json
{
  "users": [
    { "id": "sample-tanaka", "email": "tanaka@example.com", "name": "田中 太郎", "perms": ["view", "manage"] },
    { "id": "sample-sato", "email": "sato@example.com", "name": "佐藤 花子", "perms": ["view"], "image_url": null }
  ]
}
```

- `users` is non-empty; `id` is a non-empty unique string; `email` and `name`
  are strings (may be empty); `perms` contains `view`, only `view` / `manage`,
  without duplicates; `image_url` is an optional string or `null`. Unknown keys
  are ignored.
- The fixed user (`get_current_user`, `get_request_user`,
  `get_current_identity`) is the first user with `manage`, else the first
  user. A blank `email` / `name` becomes `None` in `get_current_user` /
  `get_request_user`.
- Members keep the file order; `role` is `ADMIN` with `manage`, else
  `APP_USER`. Groups are exactly `admins` (the `manage` users) and `everyone`.
- Full identity: `app.permissions` is the sorted `perms`, `app.roles` is `[]`,
  `attributes.groups` is `["admins", "everyone"]` or `["everyone"]`.
- `KEELSON_LOCAL_USER_*` and `KEELSON_LOCAL_WORKSPACE_ROLE` are ignored;
  `KEELSON_LOCAL_WORKSPACE_ID` and `KEELSON_LOCAL_APP_ID` still apply.
- A file that is missing (when named explicitly), unreadable, or malformed
  raises `IdentityError`; the SDK never falls back to the built-in data.
- The file is read on each call; it is ignored when local mode is off.

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_LOCAL_MODE` | Set to `1` / `true` / `yes` to enable local mode (returns fixture data, no HTTP calls). Refused when a Keelson deployment is detected. |
| `KEELSON_LOCAL_USERS_FILE` | Local users file for local mode (default: `./.keelson/dev-users.json` when it exists). |
| `KEELSON_LOCAL_USER_ID` / `KEELSON_LOCAL_USER_EMAIL` / `KEELSON_LOCAL_USER_NAME` | Override the local fixed user when there is no users file. |
| `KEELSON_LOCAL_WORKSPACE_ID` | Override the local workspace ID. |
| `KEELSON_LOCAL_WORKSPACE_ROLE` | Override the local workspace role when there is no users file. |
| `KEELSON_LOCAL_APP_ID` | Override the local app ID (default: `local-app-001`). |
| `KEELSON_DIRECTORY_BASE_URL` | **Canonical, platform-injected** base URL for `get_current_identity` and Directory calls. Use this. |
| `KEELSON_DIRECTORY_TOKEN` | App token for app-as-actor identity and Directory access. |
| `KEELSON_IDENTITY_BASE_URL` | Deprecated compatibility alias used only when neither `base_url` nor `KEELSON_DIRECTORY_BASE_URL` is set. |

---

## Email SDK (`keelson_email`)

Send email to your app's users and receive delivery events (delivered /
bounce / complaint). Keelson injects the endpoint, token, and webhook signing
secret at deploy time; no `keelson.yaml` setting is needed. Full guide:
https://keelson.dev/docs/building-apps/external-integrations/#send-email

```python
from keelson import email

result = email.send(
    to="user@example.com",
    subject="Request received",
    text="We received request 1234.",
)
# Store result["send_id"] to match it with delivery events later
print(result["send_id"], result["status"])
```

Sending rules:

- The sender is always `<app-slug>@mail.keelson.run`. `from_name` and
  `reply_to` can be set; custom sender domains are not available
- Send only business communication to your app's users
  ([Acceptable Use Policy](https://keelson.dev/aup/) 2.3). Marketing
  campaigns and sending to people who do not use the app are not allowed
- Up to 50 recipients per message (To + CC + BCC). At least one of `text` or
  `html` is required
- Up to 30 sends per 60 seconds, per app and per workspace
- Monthly recipient limits depend on the plan and are counted per app and per
  workspace ([Plans and limits](https://keelson.dev/docs/workspace/plans-and-limits/))
- Inbound email is not available

A rejected send raises `EmailError`; the message includes the error code.

| HTTP | Error code | Meaning |
|------|------------|---------|
| 400 | `RECIPIENT_SUPPRESSED` | A recipient is on the workspace suppression list |
| 403 | `EMAIL_SENDING_SUSPENDED` | Sending is suspended for the workspace (too many permanent bounces or complaints). Contact Keelson to lift it |
| 422 | `RECIPIENT_LIMIT_EXCEEDED` | More than 50 recipients |
| 429 | `RATE_LIMIT_EXCEEDED` | 60-second send limit reached. Retry later |
| 429 | `MONTHLY_QUOTA_EXCEEDED` | Monthly recipient limit reached |
| 429 | `GLOBAL_RATE_LIMIT_EXCEEDED` / `GLOBAL_DAILY_QUOTA_EXCEEDED` | Platform-wide sending volume limit reached. Retry later |
| 502 | `SEND_OUTCOME_UNKNOWN` | Outcome could not be confirmed; the message may have been sent. Do not retry automatically |

### Delivery events

Keelson posts signed delivery events to your app at
`POST /api/webhooks/email-events`. Verify the signature with
`KEELSON_EMAIL_WEBHOOK_SECRET` over the raw request body, then match the
event to your send record with `send_id`.

```python
import os

from flask import Flask, request
from keelson import email

app = Flask(__name__)


@app.post("/api/webhooks/email-events")
def email_events():
    event = email.verify_event_webhook(
        request.get_data(),
        request.headers,
        os.environ["KEELSON_EMAIL_WEBHOOK_SECRET"],
    )
    if event.event_type == "bounce" and event.bounce_type == "hard":
        ...  # Look up the send by event.send_id and mark event.email_address invalid
    return "", 204
```

`EmailEventPayload` fields: `event_id`, `event_type` (`delivered` / `bounce`
/ `complaint`), `email_address`, `send_id`, `bounce_type` (`hard` / `soft`,
bounces only), `detail`, `provider`, `timestamp`. Delivery is at-least-once;
use `event_id` to detect duplicates.

Permanent bounces and complaints add the address to the workspace
suppression list automatically, whether or not the app handles the event.
After that, no app in the workspace can send to it (`RECIPIENT_SUPPRESSED`).
Owners and Admins can review the list under Email in the console's workspace
settings.

### Cross-language guaranteed API

| Function | Signature | Description |
|----------|-----------|-------------|
| `send` | `(*, to, subject, text=None, html=None, **kwargs) -> dict` | Send an email; returns `{"send_id", "status"}` |
| `verify_event_webhook` | `(body, headers, secret) -> EmailEventPayload` | Verify Svix signature and parse event |
| `verify_event_webhook_bytes` | `(body, headers, secret) -> EmailEventPayload` | Verify event from raw bytes |
| `set_idempotency_store` | `(store)` | Plug in a shared store to suppress duplicate deliveries across instances |

### Python-specific helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `on_event` | `(handler)` | Decorator that registers a synchronous event handler and starts a standalone webhook server on `PORT` |
| `serve` | `(*, port=None, blocking=True)` | Start the webhook server manually |

`on_event` / `serve()` start their own HTTP server, so use them only when the
app has no other server listening on `PORT`. They verify signatures
automatically with `KEELSON_EMAIL_WEBHOOK_SECRET`.

**Data classes**: `Attachment`, `EmailEventPayload`.

**Exception**: `EmailError`.

### Environment variables

| Variable | Description |
|----------|-------------|
| `KEELSON_EMAIL_API_URL` | Email API endpoint (injected). |
| `KEELSON_EMAIL_TOKEN` | Bearer token for sending (injected). |
| `KEELSON_EMAIL_WEBHOOK_SECRET` | Signing secret for delivery events (injected). |
| `KEELSON_EMAIL_BASE_URL` | Optional app-scoped endpoint. When set, the SDK prefers it over `KEELSON_EMAIL_API_URL`. |

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
uv run pytest keelson_email/tests/
```

Lint:

```bash
uv run ruff check .
```
