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

Intentional differences:

- callback, decorator, explicit verification helper, or HTTP-handler based
  integration are all acceptable shapes

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
| Send | guaranteed | Yes | Yes | Yes | |
| Inbound handling | guaranteed | Yes | Yes | Yes | Go uses explicit verification in HTTP handlers |
| Event handling | guaranteed | Yes | Yes | Yes | Go uses explicit verification in HTTP handlers |
| Attachment download | guaranteed | Yes | Yes | Yes | Python exposes instance method on attachment |
| Webhook signature verification | guaranteed | Yes | Yes | Yes | Svix HMAC-SHA256. Node/Python auto-verify with `KEELSON_EMAIL_WEBHOOK_SECRET` and, in Keelson mode (`KEELSON_MODE=keelson` / platform env), reject unsigned deliveries fail-closed; Go verifies explicitly in the HTTP handler |
| Webhook server bootstrap helper | optional | Yes | Yes | No | Allowed helper difference |
| Replayed / expired signature rejection | guaranteed | Yes | Yes | Yes | Svix timestamp window (±300s) + HMAC reject expired-timestamp replays and tampered payloads in all three SDKs |
| Within-window duplicate suppression (idempotency) | pluggable durable hook (token-fenced 3-state) + default best-effort | Yes | Yes | Yes | Each SDK exposes a pluggable idempotency store hook: Node/Python `setIdempotencyStore`/`set_idempotency_store`; Go `IdempotencyStore` with `VerifyWebhookOnce`/`VerifyEventWebhookOnce`. The hook runs `reserve → handler → commit(token)`, with `release(token)` on handler failure. `reserve` distinguishes **completed** (duplicate success), **pending** (retryable 503), and **acquired** (process now). Token compare-and-set prevents stale attempts from changing a newer reservation. Reservation or commit errors return retryable 500; release errors are logged and leave the lease to fence immediate retries. Shared application storage makes suppression durable across instances; the default store is process-local and best-effort. Delivery remains at-least-once, so handlers must tolerate retries. |

## Current Accidental Differences

None. All three SDKs strip parameters from the `Content-Type` header returned
by `stat` / `Head` (e.g.
`text/plain; charset=utf-8` → `text/plain`). This behavior is covered by the
shared `media_stat.json` parity fixture.

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

The platform signal is any of `KEELSON_APP_ID`, `KEELSON_TENANT_ID`, or
`KEELSON_DEPLOY_ID`.

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
(`KEELSON_APP_ID` / `KEELSON_TENANT_ID`). It never silently falls back to
ephemeral local storage on Keelson. Resolution is identical across Go/Node/Python:

| Condition | Result |
| --- | --- |
| `KEELSON_MODE=keelson` + bucket + prefix + identity set | remote (GCS over ADC) |
| `KEELSON_MODE=keelson` + bucket / prefix missing | **error** (capability unavailable) |
| `KEELSON_MODE=keelson` + identity (`KEELSON_APP_ID`/`KEELSON_TENANT_ID`) missing | **error** (capability unavailable) |
| Exactly one of bucket / prefix set (any mode) | **error** (incomplete remote config) |
| `KEELSON_MODE=local` | local filesystem (`KEELSON_FILES_DIR`, default `./.keelson/files`) |
| `KEELSON_MODE` unset + core identifier set | **error** (refuse silent fallback on platform) |
| `KEELSON_MODE` unset + no platform env (remote env is **not** consulted) | local (zero-config development) |
| Any other non-empty `KEELSON_MODE` (unknown) | **error** (never resolves to local) |

Unlike Media, the `files` SDK does **not** infer remote from the presence of the
remote env when `KEELSON_MODE` is unset; the mode variable is the only signal.
The platform signal is
any of `KEELSON_APP_ID` / `KEELSON_TENANT_ID` / `KEELSON_DEPLOY_ID`. Remote auth
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

**Path confinement is TOCTOU-safe** in every SDK's default local backend (the
one exception — Node's opt-in fallback — is called out in the table below).
Every operation descends the key's path
component-by-component relative to a held directory descriptor (`openat` +
`O_NOFOLLOW`) and opens / renames / unlinks the final element relative to that
descriptor, so an ancestor directory swapped to a symlink — even concurrently,
mid-operation — cannot redirect the operation outside the files dir. Go uses
`os.Root`; Python uses `os.open(..., dir_fd=...)` / `os.rename(..., src_dir_fd,
dst_dir_fd)` / `os.unlink(..., dir_fd=...)`; Node has no `dir_fd` parameter at
all, so it emulates `openat` by re-opening a held directory descriptor through a
**portal** path, `<portal>/<fd>/<name>`.

Node's portal selection (local dev only — production is always
`KEELSON_MODE=keelson` / GCS on Linux):

| Platform | Local backend |
|---|---|
| Linux | `/proc/self/fd` portal, selected unconditionally and without probing — identical to the pre-cross-platform behaviour |
| Other POSIX (macOS, \*BSD) with a working portal | same TOCTOU-safe portal backend; candidates (`/dev/fd`, `/proc/self/fd`) are **probed at runtime**, and a portal is accepted only after it demonstrates every operation the backend uses, including that `O_NOFOLLOW` through the portal rejects a symlink |
| Other POSIX with no working portal | **fails closed** unless `KEELSON_FILES_ALLOW_BESTEFFORT_LOCAL=1` is set, which selects a path-based backend (per-component `lstat` + `O_NOFOLLOW`, plus a whole-path no-symlink open flag when one is observed to work) and prints a stderr warning. Check-then-act windows remain on `mkdir` / `rename` / `unlink`, so it is never selected silently |
| Windows | **unsupported** for local mode, with no opt-in escape hatch (no `O_NOFOLLOW` ⇒ no defence to offer). Use WSL2 or a Linux devcontainer, or remote (GCS) |

The portal mechanism is a Node-runtime workaround, not a contract difference:
Python and Go express the same confinement natively (`dir_fd` / `os.Root`) and
need no platform gate. Wherever a portal is selected — which is always the case
on Linux, i.e. CI and every supported deployment — the TOCTOU guarantee is
identical across all three SDKs.

**The opted-in best-effort backend is explicitly outside that guarantee.** It
enforces confinement by check-then-act (`lstat` per component, then `mkdir` /
`rename` / `unlink`, which accept no `O_NOFOLLOW`), so an ancestor swapped to a
symlink *between* the check and the act can escape the files dir. It rejects
symlinks it can observe, but it does not provide the TOCTOU property; that is
why it requires `KEELSON_FILES_ALLOW_BESTEFFORT_LOCAL=1` and warns on stderr.
It is a local-development escape hatch for a platform where the SDK would
otherwise refuse to run at all — never a production or CI configuration.

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
