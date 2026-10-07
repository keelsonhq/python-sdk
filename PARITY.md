# Keelson SDK Parity Contract

This document is the source of truth for cross-language SDK parity across:

- the Node SDK ([github.com/keelsonhq/node-sdk](https://github.com/keelsonhq/node-sdk))
- this Python SDK
- the Go SDK ([github.com/keelsonhq/go-sdk](https://github.com/keelsonhq/go-sdk))

The goal is not identical syntax. The goal is a shared mental model that keeps
human users and AI agents from inferring the wrong capability or behavior from
another language's SDK.

## Status Labels

Use these labels when documenting or reviewing SDK changes:

- `guaranteed`: cross-language capability that all three SDKs should provide.
- `optional`: language-specific helper that may exist in some SDKs, but must
  not be presented as cross-language common ground.
- `intentional difference`: the capability is shared, but the shape is allowed
  to differ for language-idiomatic reasons.
- `decision pending`: a capability with cross-language relevance whose final
  parity status has not been fixed yet. Missing support does not currently
  block parity, but the capability must stay out of the accidental-difference
  bucket until the contract decision is made.
- `not yet implemented`: the capability belongs in the cross-language contract,
  but one or more SDKs do not provide it yet.

## Working Rules

These rules apply to future SDK changes and README updates.

1. Additive parity fixes are preferred over breaking changes.
2. The first examples in each README should use cross-language guaranteed APIs.
3. Language-specific convenience helpers belong in a separate
   `Language-specific helpers` section.
4. Package or module layout may differ by language when the capability model is
   still aligned.
5. If a capability is missing in one SDK, that is `not yet implemented` unless
   this document explicitly marks the difference as intentional.
6. `decision pending` capabilities are not treated as parity regressions until
   they are promoted to `guaranteed` or demoted to `optional`.

## README Structure Contract

Each language README should converge on this structure:

1. Overview / installation
2. Cross-language guaranteed API
3. `{Language}`-specific helpers (e.g. **Go-specific helpers**)
4. Modes and environment variables
5. Development / testing

Each language README follows this structure. Every domain section labels its
API table as **Cross-language guaranteed API** or
**`{Language}`-specific helpers**.

## Cross-Language Minimum Contract

### Identity / Directory

Guaranteed capability:

- current user lookup from trusted `X-Keelson-User-*` headers
- current full identity lookup via app-as-actor `getCurrentIdentity` /
  `get_current_identity` / `GetCurrentIdentity`
- member listing
- single user lookup
- group listing
- Directory request context forwarding for `cookie`, `authorization`, and
  `host`
- app-as-actor Directory access via an app token, with a
  `KEELSON_DIRECTORY_TOKEN` env fallback and the canonical, platform-injected
  `KEELSON_DIRECTORY_BASE_URL` base URL (the deprecated `KEELSON_IDENTITY_BASE_URL`
  is a compatibility fallback only and is not injected by the platform)
- local mode support
- request user lookup (`getRequestUser` / `get_request_user` /
  `GetRequestUser`): the current user plus the app permissions parsed from
  `X-Keelson-User-App-Perms` (see "Identity Local-Mode Contract")
- local-mode refusal when a Keelson deployment is detected
- local users file (`KEELSON_LOCAL_USERS_FILE`) for the local-mode fixed user,
  member list, single user, groups, and full identity

Intentional differences:

- Go may keep `identity` and `directory` as separate packages
- function signatures may differ as long as the same request context can be
  forwarded
- app-token injection shape differs: Python/Node use an `app_token` argument,
  Go uses a `WithAppToken` request option
- Directory conflict handling differs: Python/Node reject `cookie` +
  `app_token` (and `authorization` + `app_token`) by raising; Go's functional
  options cannot return an error, so `WithAppToken` drops any Cookie and the
  last of `WithAppToken` / `WithAuthorization` wins. Current full identity is
  app-token only in all three SDKs and rejects explicit cookie credentials.

Shared identifier guidance (`guaranteed`, all three SDKs must present it the
same way):

- A group's `key` is the **code-facing identifier**: it is always present (the
  server guarantees a non-null, normalized key since the key-NOT-NULL
  redesign), stable, and immutable. **Prefer `key` for code references**
  (`GroupItem.key`, `group_key` / `GroupKey` member filters). Keys may be
  non-ASCII (e.g. a Japanese `経理`).
- A group's `id` is a stable UUID for **machine integration / internal
  wiring** — use it when a system needs an opaque, name-independent handle.
- The `key` field stays typed nullable (`*string` / `string | None` /
  `string | null`) and the parsers stay null-tolerant **for backward
  compatibility only**; a keyless group no longer occurs in Directory
  responses. Doc comments across the three SDKs must not advise "prefer id"
  or describe `key` as an optional alias.

### Media

Guaranteed capability:

- upload via a high-level API that generates and returns a file ID
- download / open / read equivalent
- delete
- exists
- metadata lookup
- public URL generation

Optional capability:

- low-level upload API that accepts a caller-supplied file ID

Intentional differences:

- bytes vs stream return types are allowed if the same use cases are covered

### Files (data)

The `files` interface is a key-addressed, overwrite, whole-value data-file
store. It is a **separate** contract
from Media: Media is create-only, ULID-addressed, immutable and served over
HTTP; `files` is key-addressed, overwrite-in-place, non-public, and read only by
the app itself.

Guaranteed capability:

- `write(key, data)` — overwrite; `str`/`string` is stored UTF-8; write-through
- `read(key)` — returns the bytes, or a language-idiomatic **absent** value when
  the key does not exist (Python/Node `None`/`null`; Go `(data, ok, err)` with
  `ok=false`). Absence is the normal case — unlike `media.get`, it is **not** an
  error. Only a 404 is missing; 401 / 403 / 429 / 5xx raise/return an error.
- `delete(key)` — idempotent (no error when the key is absent)
- `list(prefix="")` — full, lexicographically-sorted list of keys; paging is
  absorbed inside the SDK and never surfaced
- shared key grammar: `/`-separated relative path, well-formed UTF-8 with total
  byte length ≤ 512 and each segment ≤ 255 bytes (NAME_MAX), no leading/trailing
  `/`, no empty / `.` / `..` segments, no Unicode control characters (category
  Cc: C0, DEL, and C1 U+0080–U+009F; ill-formed UTF-8 / lone surrogates are also
  rejected)
- one-object soft size limit of 10 MiB, enforced SDK-side on `write`
- local (filesystem, temp + atomic replace) and Keelson (GCS over ADC) backends,
  selected by the `KEELSON_MODE` fail-closed contract (below)

Intentional differences:

- the `read` absent signal differs by language: Python/Node return `None`/`null`,
  Go returns `(data, ok, err)` with `ok=false`
- `delete` is exported as `del` (aliased to `delete`) in Node because `delete`
  is a reserved word; Python/Go use `delete`/`Delete`
- bytes vs stream and error-type shapes follow each language's idiom
  (Python/Node raise `FilesError`; Go returns errors, config errors wrap
  `files.ErrConfig`)

There is intentionally **no** `exists()` (the `read` absent value covers it and
avoids a check-then-act TOCTOU), and `write` returns void to leave room for a
future generation/conditional-write return.

### Email

Guaranteed capability:

- send
- inbound email handling
- event webhook handling
- attachment download
- webhook signature verification

Optional capability:

- webhook server bootstrap helper

Platform availability: the Keelson platform does not offer new email sending
or inbound email at initial launch. Both capabilities remain implemented in all
three SDKs and stay in this contract so they can be resumed. Delivery events
for previously sent email continue through event webhook handling and webhook
signature verification.

Intentional differences:

- callback, decorator, explicit verification helper, or HTTP-handler based
  integration are all acceptable shapes
- all languages prefer `KEELSON_EMAIL_BASE_URL` and `/__keelson/email/*` when
  the non-blank variable is available, while retaining the legacy API URL and
  `/v1/email/*` paths otherwise. Python attachment instances and Go's
  ID-based method select the gateway path directly. Node selects it only when
  `downloadAttachment` receives attachment metadata; its existing URL-string
  form always retains the legacy route. An explicit Go `New(baseURL, token)`
  base URL also always retains legacy paths.

### Tasks

Background tasks: the app enqueues a run of a command it declared under
`tasks:` in `keelson.yaml`, and the platform runs it once on a separate
instance, retrying failed attempts. The HTTP contract (paths, request and
response fields, error codes, auth) is owned by
`docs/specs/app-platform-api-spec.md` § 9; the execution contract (limits,
at-least-once delivery, retries, retention) by `docs/specs/product-spec.md`
§ 2 and `docs/specs/jobs-and-cron-spec.md` § 12A. This section fixes only what
the three SDKs must agree on.

Guaranteed capability:

- `enqueue(name, payload, idempotency_key)` → `task_id` (string). `payload` is
  any JSON value and defaults to `null`; `idempotency_key` is optional
  (1–128 printable ASCII characters). The SDK sends
  `{"payload": ..., "idempotency_key": ...}` to
  `POST {KEELSON_TASKS_BASE_URL}/internal/apps/{app_id}/tasks/{name}/enqueue`.
  A new task (202) and a replay with the same idempotency key (200, the
  existing `task_id`) both return the `task_id`; callers do not see the
  difference. The 64 KiB limit is the platform's: the whole HTTP request body
  must be at most 65,536 bytes, measured on the bytes the server receives.
- `get(task_id)` → the task status object, with exactly the field names and
  types of the API response (`app-platform-api-spec.md` § 9.3):

  | Field | Type | Null when |
  | --- | --- | --- |
  | `task_id` | string | never |
  | `name` | string (normalized task name) | never |
  | `status` | `queued` / `running` / `succeeded` / `failed` / `cancelled` | never |
  | `claimed_attempts` | integer, number of started attempts (≥ 0) | never |
  | `last_failure_code` | string: `exit_nonzero` / `timed_out` / `no_completion` / `delivery_failed` / `manifest_unresolvable` / `quota_exhausted` | no attempt has failed yet, or `succeeded` |
  | `created_at` | RFC 3339 UTC timestamp | never |
  | `finished_at` | RFC 3339 UTC timestamp | status is not terminal |

  `get` never returns the payload or stderr. An unknown `task_id`, or one that
  belongs to another app, is a "not found" error (`TASK_NOT_FOUND`, HTTP 404).
  `get` is guaranteed only while the app declares at least one task and is not
  deleted: removing the last `tasks:` entry or deleting the app revokes the web
  service account's permission to call the API, and later calls fail with an
  auth error (Cloud Run 403 / runtime API 401), not `TASK_NOT_FOUND`.
- remote auth: an OIDC id token from the metadata server whose audience is the
  `KEELSON_TASKS_BASE_URL` value, sent as `Authorization: Bearer`. No token
  env is wired.
- API errors surface their stable code (`TASK_INVALID_REQUEST`,
  `TASK_NOT_DECLARED`, `TASK_NOT_FOUND`, `TASKS_UNAVAILABLE`,
  `TASK_PAYLOAD_TOO_LARGE`, `TASK_BACKLOG_LIMIT_EXCEEDED`,
  `TASK_MONTHLY_QUOTA_EXCEEDED`) so callers can branch on it. The SDK does not
  retry 429 responses automatically.
- failures the API does not describe (auth, Cloud Run, transport, metadata
  server, configuration, local CLI) surface as the SDK's own `TASKS_*` codes,
  and only a transient failure is retried (see "Tasks SDK Error Codes" below)
- local and Keelson backends, selected by the `KEELSON_MODE` fail-closed
  contract (see "Tasks Runtime-Mode Contract" below)

Intentional differences:

- argument naming: `idempotency_key` (Python), `idempotencyKey` (Node), a
  functional option (Go)
- the status object's language shape (dataclass / plain object / struct) may
  differ; field names on the wire and their meaning may not
- error types follow each language's idiom (Python/Node raise `TasksError`;
  Go returns errors, config errors wrap `tasks.ErrConfig`)

There is intentionally **no** cancel, list, delayed ("run in n seconds"), or
manual re-run API. A failed task is retried by enqueueing it again.

## Capability Matrix

This matrix is the review baseline for subsequent changes. `Yes` means the
capability exists today. `No` means it is absent. `Target` shows whether the
capability is part of the parity contract.

### Identity / Directory

| Capability | Target | Node | Python | Go | Notes |
| --- | --- | --- | --- | --- | --- |
| Current user from trusted headers | guaranteed | Yes | Yes | Yes | Returns `UserIdentity`; Go uses `identity` package |
| Current full identity as app actor | guaranteed | Yes | Yes | Yes | Returns `CurrentIdentity`; app-token only |
| Members list | guaranteed | Yes | Yes | Yes | Go uses `directory` package |
| Single user | guaranteed | Yes | Yes | Yes | Go uses `directory` package |
| Groups list | guaranteed | Yes | Yes | Yes | Go uses `directory` package |
| Trusted header input (`x-keelson-user-id` / `email` / `name`) | guaranteed | Yes | Yes | Yes | Shape differs by language |
| Directory request context forwarding (`cookie` / `authorization` / `host`) | guaranteed | Yes | Yes | Yes | Shape differs by language |
| App-as-actor token (`app_token` / `WithAppToken` + `KEELSON_DIRECTORY_TOKEN` fallback) | guaranteed | Yes | Yes | Yes | `app_token` arg (Python/Node), `WithAppToken` option (Go) |
| Local mode | guaranteed | Yes | Yes | Yes | |
| Request user with app permissions (`getRequestUser` / `get_request_user` / `GetRequestUser`) | guaranteed | Yes | Yes | Yes | `identity_request_user.json` fixture |
| Local mode refused when a Keelson deployment is detected | guaranteed | Yes | Yes | Yes | `identity_local_mode_guard.json` fixture. Go refuses in `identity.New` / `directory.New` |
| Local users file (`KEELSON_LOCAL_USERS_FILE`) | guaranteed | Yes | Yes | Yes | `identity_local_roster.json` fixture |
| `identity` + `directory` split package layout | intentional difference | No | No | Yes | Allowed layout difference |

### Media

| Capability | Target | Node | Python | Go | Notes |
| --- | --- | --- | --- | --- | --- |
| High-level upload returns generated file ID | guaranteed | Yes | Yes | Yes | |
| Caller-supplied file ID upload | optional | No | No | Yes | Allowed low-level helper |
| Download / open / read equivalent | guaranteed | Yes | Yes | Yes | Return shape differs |
| Delete | guaranteed | Yes | Yes | Yes | |
| Exists | guaranteed | Yes | Yes | Yes | |
| Metadata lookup | guaranteed | Yes | Yes | Yes | |
| Public URL generation | guaranteed | Yes | Yes | Yes | |
| Bytes vs stream API shape | intentional difference | Yes | Yes | Yes | Same use case, different form |

### Files (data)

| Capability | Target | Node | Python | Go | Notes |
| --- | --- | --- | --- | --- | --- |
| Write (overwrite, whole-value) | guaranteed | Yes | Yes | Yes | `str`/`string` stored UTF-8; write-through |
| Read (absent → language-idiomatic empty) | guaranteed | Yes | Yes | Yes | Python/Node `None`/`null`; Go `(data, ok, err)` |
| Delete (idempotent) | guaranteed | Yes | Yes | Yes | Node exports `del` (aliased `delete`) |
| List (sorted, paging absorbed) | guaranteed | Yes | Yes | Yes | Full lexicographic key list |
| Shared key grammar (≤512B, no traversal/control) | guaranteed | Yes | Yes | Yes | `files_key_validation.json` fixture |
| One-object soft size limit (10 MiB) | guaranteed | Yes | Yes | Yes | Enforced SDK-side on `write` |
| Local + Keelson (GCS/ADC) backends | guaranteed | Yes | Yes | Yes | `KEELSON_MODE` fail-closed contract |
| `read` absent shape (`None`/`null` vs `(data, ok, err)`) | intentional difference | Yes | Yes | Yes | Same use case, different form |
| `exists()` | intentionally absent | No | No | No | `read` absent value replaces it (avoids TOCTOU) |

### Email

| Capability | Target | Node | Python | Go | Notes |
| --- | --- | --- | --- | --- | --- |
| Send | guaranteed | Yes | Yes | Yes | Prefers app-scoped `KEELSON_EMAIL_BASE_URL`; blank/unset falls back to the legacy API URL |
| Inbound handling | guaranteed | Yes | Yes | Yes | Go uses explicit verification in HTTP handlers |
| Event handling | guaranteed | Yes | Yes | Yes | Go uses explicit verification in HTTP handlers |
| Attachment download | guaranteed | Yes | Yes | Yes | Gateway paths are built from attachment IDs. Python exposes an instance method; Node retains its URL-string overload for compatibility |
| Webhook signature verification | guaranteed | Yes | Yes | Yes | Svix HMAC-SHA256. Node/Python auto-verify with `KEELSON_EMAIL_WEBHOOK_SECRET` and, in Keelson mode (`KEELSON_MODE=keelson` / platform env), reject unsigned deliveries fail-closed; Go verifies explicitly in the HTTP handler |
| Webhook server bootstrap helper | optional | Yes | Yes | No | Allowed helper difference |
| Replayed / expired signature rejection | guaranteed | Yes | Yes | Yes | Svix timestamp window (±300s) + HMAC reject expired-timestamp replays and tampered payloads in all three SDKs |
| Within-window duplicate suppression (idempotency) | pluggable durable hook (token-fenced 3-state) + default best-effort | Yes | Yes | Yes | Each SDK exposes a pluggable idempotency store hook: Node/Python `setIdempotencyStore`/`set_idempotency_store`; Go `IdempotencyStore` with `VerifyWebhookOnce`/`VerifyEventWebhookOnce`. The hook runs `reserve → handler → commit(token)`, with `release(token)` on handler failure. `reserve` distinguishes **completed** (duplicate success), **pending** (retryable 503), and **acquired** (process now). Token compare-and-set prevents stale attempts from changing a newer reservation. Reservation or commit errors return retryable 500; release errors are logged and leave the lease to fence immediate retries. Shared application storage makes suppression durable across instances; the default store is process-local and best-effort. Delivery remains at-least-once, so handlers must tolerate retries. |

### Tasks

| Capability | Target | Node | Python | Go | Notes |
| --- | --- | --- | --- | --- | --- |
| `enqueue(name, payload, idempotency_key)` → `task_id` | guaranteed | Yes | Yes | Yes | Payload limit checked SDK-side on the exact request-body bytes |
| `get(task_id)` → status object (`claimed_attempts`, `last_failure_code`, …) | guaranteed | Yes | Yes | Yes | Field names follow `app-platform-api-spec.md` § 9.3; `tasks_get_response.json` fixture |
| Stable API error codes surfaced to the caller | guaranteed | Yes | Yes | Yes | One error type with `code`; `tasks_error_mapping.json` fixture |
| Transient-failure retry (3 attempts; enqueue only with an idempotency key) | guaranteed | Yes | Yes | Yes | See "Tasks SDK Error Codes" |
| Local + Keelson backends | guaranteed | Yes | Yes | Yes | `KEELSON_MODE` fail-closed contract; `tasks_mode_resolution.json` / `tasks_local_cli_result.json` fixtures |
| Idempotency key argument shape | intentional difference | Yes | Yes | Yes | `idempotency_key` / `idempotencyKey` / Go option |
| Cancel / list / delayed enqueue | intentionally absent | No | No | No | Cancellation is console-only; no delay or re-run in v1 |

## Current Accidental Differences

None. All three SDKs strip parameters from the `Content-Type` header returned
by `stat` / `Head` (e.g.
`text/plain; charset=utf-8` → `text/plain`). This behavior is covered by the
shared `media_stat.json` parity fixture.

## Identity Local-Mode Contract

The canonical spec is `docs/specs/local-dev-spec.md` in the Keelson monorepo;
this section lists what the three SDKs must agree on. Local mode
(`KEELSON_LOCAL_MODE` = `1` / `true` / `yes`) keeps its resolution order:
when it is on, every identity / directory function returns fixed data and
never reads headers or calls the Directory API. When it is off, nothing below
changes the production path.

### Request user

`getRequestUser(options)` (Node, async, `options.headers`),
`get_request_user(*, headers=None)` (Python), and
`(*identity.Client).GetRequestUser(opts ...RequestOption)` (Go, `WithHeaders`)
return the `getCurrentUser` fields plus `perms`:

- Node `RequestUser = UserIdentity & { perms: string[] }`; Python frozen
  dataclass `RequestUser(id, email, name, perms)`; Go
  `RequestUser{ID, Email *string, Name *string, Perms []string}`.
- `perms` splits `X-Keelson-User-App-Perms` on `,`, trims each item, drops
  empty items, and keeps order and values. A missing or empty header gives an
  empty list, not an error.
- A missing or blank `X-Keelson-User-Id` raises the same error as
  `getCurrentUser`.
- The gateway sends non-ASCII values (e.g. a Japanese name) as raw UTF-8
  bytes, which some frameworks hand over as a latin-1 string. For each header
  value, if every character is U+00FF or below, at least one is U+0080 or
  above, and its latin-1 bytes are valid UTF-8, `getRequestUser` returns the
  UTF-8 decoding; otherwise the value is kept. `getCurrentUser` is unchanged.
- In local mode it returns the fixed user. Without a users file that is the
  existing fixed user (`KEELSON_LOCAL_USER_*`) with `perms` `["view", "manage"]`.

### Refusal in a Keelson deployment

A production mark is `KEELSON_MODE=keelson` (trimmed, case-insensitive) or a
non-blank `KEELSON_APP_ID`, `KEELSON_WORKSPACE_ID`, `KEELSON_TENANT_ID`,
`KEELSON_DEPLOY_ID`, or `KEELSON_APP_URL`. When local mode is on and any mark
is present, the SDK neither returns fixed data nor silently falls back to the
production path:

| SDK | Behavior |
| --- | --- |
| Node | every local-mode identity / directory call rejects with `IdentityError` |
| Python | every local-mode identity / directory call raises `IdentityError` |
| Go | `identity.New` / `directory.New` return an error |

The message contains `KEELSON_LOCAL_MODE`, the names of the marks found (in the
order above, `KEELSON_MODE` written as `KEELSON_MODE=keelson`), and the advice
to unset `KEELSON_LOCAL_MODE`. It never contains the values.

### Local users file

In local mode the SDK reads the users file named by `KEELSON_LOCAL_USERS_FILE`,
or `./.keelson/dev-users.json` when that exists. Without either, the existing
fixed data is unchanged. An explicitly named file that is missing, unreadable,
or malformed is an error (the same error type as the refusal), never a silent
fallback.

```json
{ "users": [ { "id": "...", "email": "...", "name": "...", "perms": ["view", "manage"], "image_url": null } ] }
```

- `users` is non-empty; `id` is a non-empty unique string; `email` and `name`
  are strings (may be empty); `perms` contains `view`, only `view` / `manage`,
  no duplicates; `image_url` is an optional string or `null`. Unknown keys are
  ignored.
- The fixed user is the first user whose `perms` include `manage`, else the
  first user. Blank `email` / `name` become `null` in `getCurrentUser` /
  `getRequestUser`.
- With a users file, `KEELSON_LOCAL_USER_ID` / `_EMAIL` / `_NAME` and the
  workspace-role variables are not used. The workspace id keeps its existing
  order (`KEELSON_LOCAL_WORKSPACE_ID` → legacy `KEELSON_LOCAL_TENANT_ID` →
  `local-tenant-001`), and `KEELSON_LOCAL_APP_ID` still applies.
- Members keep the file order. `role` is `ADMIN` with `manage`, else
  `APP_USER`. Groups are exactly `admins` (`local-group-admins`) and
  `everyone` (`local-group-everyone`); `admins` holds the `manage` users.
- Full identity: `app.permissions` is the sorted `perms`, `app.roles` is `[]`,
  `attributes.groups` is `["admins", "everyone"]` or `["everyone"]`.
  `authz.version` (`1`) is required in the mock's HTTP response because the Go
  SDK rejects identities without it; SDK types are not extended, and SDK tests
  compare only each SDK's existing public fields.

The same values are served by the `keelson dev serve` Directory mock, so a
user resolves to the same id, member, and identity with or without the CLI.
Fixtures: `identity_local_roster.json`, `identity_request_user.json`,
`identity_local_mode_guard.json`.

## Media Runtime-Mode Contract

The Media SDK is fail-closed on Keelson: it never silently falls back to
local/ephemeral filesystem storage when the platform Media configuration is
missing or incomplete. Resolution is identical across Go/Node/Python:

| Condition | Result |
| --- | --- |
| `KEELSON_MODE=keelson` + both Media env set | remote (the Keelson media service) |
| `KEELSON_MODE=keelson` + Media env missing | **error** (capability unavailable; covers `files_enabled=false`) |
| Exactly one of base URL / token set (any mode) | **error** (incomplete remote config) |
| `KEELSON_MODE=local` | local filesystem (`MEDIA_DIR`, default `./media`) |
| `KEELSON_MODE` unset + both Media env set | remote (backward compatibility) |
| `KEELSON_MODE` unset + Media env missing + core identifier set | **error** (refuse silent fallback on platform) |
| `KEELSON_MODE` unset + Media env missing + no platform env | local (zero-config development) |
| Any other non-empty `KEELSON_MODE` (unknown) | **error** (never resolves to local) |

The platform signal is any of `KEELSON_APP_ID`, `KEELSON_WORKSPACE_ID`, or
`KEELSON_DEPLOY_ID`. The deprecated `KEELSON_TENANT_ID` alias remains accepted.

Error type per language: Go returns an error wrapping `media.ErrConfig`
from `New`; Node throws `MediaError`; Python raises `MediaError`. The
messages are fixed per language and asserted verbatim in the SDK tests.
`url()` also fails closed on a capability-unavailable deployment in all
three languages (Go via the rejecting constructor; Node/Python resolve the
mode before returning a path).

## Files (data) Runtime-Mode Contract

The `files` SDK uses the shared fail-closed `KEELSON_MODE` machinery:
**`KEELSON_MODE` is the single mode signal** — the backend is never inferred
from the presence of the remote env. Remote mode uses the platform-injected
`KEELSON_FILES_BUCKET` / `KEELSON_FILES_PREFIX` plus the platform identity
(`KEELSON_APP_ID` / `KEELSON_WORKSPACE_ID`). The deprecated
`KEELSON_TENANT_ID` alias remains accepted. It never silently falls back to
ephemeral local storage on Keelson. Resolution is identical across Go/Node/Python:

| Condition | Result |
| --- | --- |
| `KEELSON_MODE=keelson` + bucket + prefix + identity set | remote (GCS over ADC) |
| `KEELSON_MODE=keelson` + bucket / prefix missing | **error** (capability unavailable) |
| `KEELSON_MODE=keelson` + identity (`KEELSON_APP_ID`/`KEELSON_WORKSPACE_ID`) missing | **error** (capability unavailable) |
| Exactly one of bucket / prefix set (any mode) | **error** (incomplete remote config) |
| `KEELSON_MODE=local` | local filesystem (`KEELSON_FILES_DIR`, default `./.keelson/files`) |
| `KEELSON_MODE` unset + core identifier set | **error** (refuse silent fallback on platform) |
| `KEELSON_MODE` unset + no platform env (remote env is **not** consulted) | local (zero-config development) |
| Any other non-empty `KEELSON_MODE` (unknown) | **error** (never resolves to local) |

Unlike Media, the `files` SDK does **not** infer remote from the presence of the
remote env when `KEELSON_MODE` is unset; the mode variable is the only signal.
The platform signal is
any of `KEELSON_APP_ID` / `KEELSON_WORKSPACE_ID` / `KEELSON_DEPLOY_ID`; the
deprecated `KEELSON_TENANT_ID` alias remains accepted. Remote auth
is ADC over the GCE metadata server (no auth env is wired); an unavailable ADC
token surfaces as an error at operation time. Error type per language: Go
returns an error wrapping `files.ErrConfig` from `New`; Node throws `FilesError`;
Python raises `FilesError`.

### Local-mode note (filesystem-backed dev only)

The local backend stores each key as a **literal file** `KEELSON_FILES_DIR/<key>`:
the key is the real file path, so an app's state is directly
inspectable (`cat .keelson/files/seen_urls.json`). Nested keys create parent
directories.

A filesystem cannot represent a key and a nested key that shadows it at once
(both `cache` and `cache/item`), which the flat object store allows. This is an
**inherent limitation of the literal layout**. Changing the on-disk format would
make local data less directly inspectable. The collision is
surfaced as an explicit `FilesError` on `write` (in both write orders). `read` /
`delete` of a key shadowed by a directory are treated as missing / an idempotent
no-op (matching the GCS 404). This is the one intentional local-vs-remote
difference in the otherwise-guaranteed API; it is confined to filesystem-backed
local dev mode (production is GCS, which has full parity).

Writes are temp-file + atomic-rename within the **target's own directory**, so
the rename is always same-filesystem (no `EXDEV`, even if `KEELSON_FILES_DIR` is
a mount point); temp files use a control-char prefix so they can never be a valid
key and `list` never surfaces them.

**Path confinement is TOCTOU-safe** where the runtime exposes the required
descriptor-relative filesystem operations. Node's non-Linux local-development
fallback is called out in the table below.
Every operation descends the key's path
component-by-component relative to a held directory descriptor (`openat` +
`O_NOFOLLOW`) and opens / renames / unlinks the final element relative to that
descriptor, so an ancestor directory swapped to a symlink — even concurrently,
mid-operation — cannot redirect the operation outside the files dir. Go uses
`os.Root`; Python uses `dir_fd` on POSIX and a path-based local backend on
Windows; Node has no `dir_fd` parameter at all, so Linux emulates `openat` by
re-opening a held directory descriptor through a **portal** path,
`<portal>/<fd>/<name>`.

Node's portal selection (local dev only — production is always
`KEELSON_MODE=keelson` / GCS on Linux):

| Platform | Local backend |
|---|---|
| Linux | `/proc/self/fd` portal, selected unconditionally and without probing — identical to the pre-cross-platform behaviour |
| Other POSIX (macOS, \*BSD) with a working portal | same TOCTOU-safe portal backend; candidates (`/dev/fd`, `/proc/self/fd`) are **probed at runtime**, and a portal is accepted only after it demonstrates every operation the backend uses, including that `O_NOFOLLOW` through the portal rejects a symlink |
| Other POSIX with no working portal | Path-based local-development backend (per-component `lstat`, plus a whole-path no-symlink open flag when available). On macOS, `O_NOFOLLOW_ANY` is feature-detected and used without the mutually incompatible `O_NOFOLLOW` flag. Check-then-act windows remain whenever a checked path is resolved again because Node exposes no `dir_fd`. |
| Windows | Path-based local-development backend. It rejects Windows path syntax and reserved filenames in keys, rejects observed symlinks and junctions, and confines resolved paths to `KEELSON_FILES_DIR`. Python also rejects other observed name-surrogate reparse points. Windows exposes no `O_NOFOLLOW`, so the same check-then-act limitation applies to read, write, delete, and list. |

The portal mechanism is a Node-runtime workaround, not a contract difference:
Python and Go express the same confinement natively on POSIX (`dir_fd` /
`os.Root`). Python uses the same observed-symlink checks as Node on Windows.
Wherever descriptor-relative operations are available, the TOCTOU guarantee is
identical across all three SDKs.

**The path-based Node and Python backends are scoped to local development.**
They enforce confinement by checking each component with `lstat`. Any later
path-based open, directory traversal, rename, or unlink resolves the path
again, so an ancestor swapped to a redirect between those steps can escape the
files dir. They reject symlinks and Windows name-surrogate reparse points they
can observe. Production uses the remote GCS backend and never reaches this code
path.

Under the default (portal) backend, a symlinked ancestor or key file is rejected as a confinement
error, never followed out of the dir, and `list` never follows or lists
symlinks. A failed write always unlinks its temp file (create / write / close /
rename cleanup) so a partial temp never lingers. Keys must be well-formed UTF-8,
≤ 512 bytes with each segment ≤ 255 bytes (NAME_MAX), and free of Unicode control
characters — each rejected as a typed `FilesError` (Go: an error) in all three
SDKs. Remote list responses are schema-validated identically across the three
SDKs. A 200 with any of the following is a `FilesError`, never a raw `TypeError`
/ silent empty page: a top-level value that is not a JSON object (`null`, an
array, a scalar); a present `items` that is not an array (including an explicit
`null`); a list item that is not a non-null object; an item whose `name` is
missing, `null`, or not a string; or a present `nextPageToken` that is not a
string (including an explicit `null`). Only genuine **absence** of `items` /
`nextPageToken` is allowed (an empty page / the last page).

## Tasks Runtime-Mode Contract

The Tasks SDK uses the same fail-closed `KEELSON_MODE` machinery as `files`:
**`KEELSON_MODE` is the single mode signal**, and the SDK never silently
falls back to local execution on Keelson. Resolution is identical across
Go/Node/Python:

| Condition | Result |
| --- | --- |
| `KEELSON_MODE=keelson` + `KEELSON_TASKS_BASE_URL` set | remote (the Tasks API on the runtime API) |
| `KEELSON_MODE=keelson` + `KEELSON_TASKS_BASE_URL` missing | **error** (capability unavailable; covers the intake flag being off) |
| `KEELSON_MODE=local` | local (CLI subprocess, below) |
| `KEELSON_MODE` unset + core identifier set | **error** (refuse silent fallback on platform) |
| `KEELSON_MODE` unset + no platform env (remote env is **not** consulted) | local (zero-config development) |
| Any other non-empty `KEELSON_MODE` (unknown) | **error** (never resolves to local) |

The platform signal is the same set as `files`: any of `KEELSON_APP_ID` /
`KEELSON_WORKSPACE_ID` / `KEELSON_DEPLOY_ID`; the deprecated
`KEELSON_TENANT_ID` alias remains accepted. In remote mode the `{app_id}`
path segment is `KEELSON_APP_ID`.

`KEELSON_APP_ID` is **not** a mode signal: remote mode is selected by
`KEELSON_MODE=keelson` + `KEELSON_TASKS_BASE_URL` alone. Once remote mode is
selected, a missing `KEELSON_APP_ID` is also a configuration error
(`TASKS_NOT_CONFIGURED`), because the `/internal/apps/{app_id}/...` path
cannot be built.

Python and Node resolve the mode on every `enqueue` / `get` call; Go resolves
it once in `tasks.New()` (like `files`). The id-token audience is the
`KEELSON_TASKS_BASE_URL` value as injected (only surrounding whitespace
removed); the request URL is that value with trailing `/` removed, followed by
`/internal/...`. The `tasks_mode_resolution.json` fixture pins every row.

### Local-mode result (CLI-backed dev only)

In local mode `enqueue` runs the declared command synchronously and returns
when it has finished:

- The SDK starts the CLI found on `PATH` as a child process,
  `keelson dev task run <name> --payload - --json`, in the app process's
  working directory, writes the payload JSON to its stdin, and waits for it to
  exit (Node waits asynchronously and never blocks the event loop). The CLI
  reads the declaration from the local `keelson.yaml` and passes the command
  the same stdin document as production (`attempt_no` is `1`). The CLI's
  stderr (including the command's stdout and stderr) goes to the app's stderr;
  its stdout carries exactly one JSON object, `{"task": {...}}` or
  `{"error": {...}}`, and the SDK decides from that object alone, not from the
  exit code (`tasks_local_cli_result.json`).
- One attempt only: no retry, no concurrency limit, no backlog limit, no
  monthly quota, no ledger. The declared `timeout` still applies.
- A command that exits non-zero or times out is **not** an `enqueue` error:
  `enqueue` returns a `task_id`, and `get` reports `status = failed` with
  `last_failure_code` `exit_nonzero` or `timed_out`. A command that exits 0
  gives `status = succeeded`.
- `enqueue` raises/returns an error only when:
  - the name is empty, the idempotency key is malformed, or the payload cannot
    be serialized to JSON (`TASK_INVALID_REQUEST`), or the request body would
    exceed 65,536 bytes (`TASK_PAYLOAD_TOO_LARGE`). These are the same
    pre-send checks as remote mode, run before the CLI is started; the CLI
    reports the same codes;
  - the name is not declared in `keelson.yaml` (`TASK_NOT_DECLARED`);
  - the CLI is not on `PATH` (`TASKS_LOCAL_CLI_NOT_FOUND`; the message
    includes the install instructions `https://keelson.dev/install.sh`);
  - the CLI cannot be started, is interrupted, prints anything other than one
    valid result, or reports any other error, including an older CLI without
    `dev task run` (`TASKS_LOCAL_CLI_FAILED`; the message suggests
    `keelson upgrade`).
- Go only: when `ctx` is cancelled, the SDK sends `SIGTERM` to the CLI (which
  forwards it to the command's process group), force-kills it after 130 s,
  and `Enqueue` returns `ctx.Err()` itself rather than a `*tasks.Error`.
- `get` returns the same fields as production, with `claimed_attempts = 1`
  and `finished_at` set. It knows only tasks enqueued **in the same process**;
  any other `task_id` (another process, after a restart, unknown) is the same
  "not found" error as the production 404.
- Within the same process, an `enqueue` with the same name and idempotency key
  returns the existing `task_id` without running the command again, matching
  production. The key is recorded only after the CLI finishes, so two
  concurrent calls with the same key both run the command; the first recorded
  `task_id` keeps the key. A running task is never visible to `get`.

A local run passing does not prove the same terminal state on Keelson: the
platform retries, applies plan limits, and may run an attempt more than once.

## Tasks SDK Error Codes

All three SDKs raise/return **one** error type (Python/Node `TasksError`, Go
`*tasks.Error`) carrying `code` (string), `status` (the HTTP status; Python
`None` / Node `null` / Go `0` when there was no HTTP response), and `message`.
There are no per-code subclasses, so every language branches the same way
(`err.code == "TASK_NOT_DECLARED"`). Go's `TASKS_NOT_CONFIGURED` error also
unwraps to `tasks.ErrConfig`. The payload never appears in a message.

A non-success HTTP response is classified in this fixed order, stopping at the
first hit (`tasks_error_mapping.json`):

1. 401 → `TASKS_UNAUTHORIZED` (whatever the body)
2. 403 → `TASKS_FORBIDDEN` (Cloud Run's HTML; whatever the body)
3. a body `{"error": {"code": <non-empty string>}}` → that code verbatim,
   including codes added later and 5xx responses (503 + `TASKS_UNAVAILABLE`
   is passed through and **not** retried)
4. 502 / 503 / 504 → `TASKS_UNAVAILABLE_TRANSIENT`
5. any other 5xx → `TASKS_SERVER_ERROR`
6. anything else (including a redirect, which is never followed) →
   `TASKS_HTTP_ERROR`

| Code | Meaning | Retried |
| --- | --- | --- |
| `TASKS_UNAVAILABLE` | **Server code.** Intake is closed (the platform flag is off). Retrying does not help | no |
| `TASKS_UNAVAILABLE_TRANSIENT` | **SDK code.** 502 / 503 / 504 without an error envelope, connection failure, or the 15 s per-call timeout | yes (below) |
| `TASKS_UNAUTHORIZED` | 401 from the runtime API (id token rejected, e.g. wrong audience) | no |
| `TASKS_FORBIDDEN` | 403 from Cloud Run. Right after the first deploy that declares `tasks:`, the permission can take a few minutes to propagate; the message says so | no |
| `TASKS_SERVER_ERROR` | Any other 5xx without an error envelope | no |
| `TASKS_HTTP_ERROR` | Any other non-success status without an error envelope | no |
| `TASKS_UNEXPECTED_RESPONSE` | A 200 / 202 body that is not the documented shape (`tasks_get_response.json`). An unknown `status` value is **not** rejected | no |
| `TASKS_IDENTITY_TOKEN_ERROR` | No id token from the metadata server | no |
| `TASKS_NOT_CONFIGURED` | The mode resolution above failed, including a missing `KEELSON_APP_ID` in remote mode (Go: wraps `tasks.ErrConfig`) | — |
| `TASKS_LOCAL_CLI_NOT_FOUND` | Local mode: no `keelson` on `PATH` (message has the install instructions) | — |
| `TASKS_LOCAL_CLI_FAILED` | Local mode: the CLI failed or printed no valid result (message suggests `keelson upgrade`) | — |

The SDK's own codes start with `TASKS_` and never collide with the server's
(`TASK_*` and `TASKS_UNAVAILABLE`). Do not confuse `TASKS_UNAVAILABLE` (intake
closed; permanent until the platform turns it on) with
`TASKS_UNAVAILABLE_TRANSIENT` (a passing outage).

Retry: only `TASKS_UNAVAILABLE_TRANSIENT` is retried, up to 3 attempts in
total, waiting 0.5 s and then 1 s. `get` always retries; `enqueue` retries
**only with an idempotency key**, because without one a request the server
already accepted would be enqueued twice (its message suggests adding a key).
403, 429, and every other code are returned immediately. A fresh id token is
fetched for every HTTP call.

Pre-send checks, identical in remote and local mode: an empty name or a
malformed idempotency key (not 1–128 characters in U+0020–U+007E), or a
payload that cannot be serialized to JSON, is `TASK_INVALID_REQUEST`; a
request body (`{"payload": ..., "idempotency_key": ...}` as the SDK serializes
it) over 65,536 bytes is `TASK_PAYLOAD_TOO_LARGE`. Names are not normalized
SDK-side (the server trims and lowercases them) and are URL-encoded in the
path, as is the `task_id` for `get`.

## Contract Decisions

There are no pending contract decisions.

## Review Checklist

Before merging an SDK parity change, confirm:

1. The capability is classified in this document.
2. README examples do not present optional helpers as cross-language common API.
3. New gaps are either closed or recorded here as `not yet implemented` or
   `decision pending`, depending on whether the contract is already fixed.
4. Tests protect the guaranteed semantics, not just the
   local language surface.
