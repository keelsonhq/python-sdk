# Parity Fixtures

Shared JSON fixtures consumed by Go, Node, and Python parity tests.

Each fixture represents an API response payload (or its JSON equivalent).
The three language SDKs parse the same fixture and assert the same semantic
field values to ensure cross-language parity.

See the [public cross-SDK parity contract](https://github.com/keelsonhq/python-sdk/blob/main/PARITY.md)
for the full behavior contract.

## How to use

1. Load the fixture JSON in your language's test.
2. Feed it through the real SDK parse/client path (not raw `JSON.parse`).
3. Assert the **parity fields** listed below with a comment block:
   `--- Cross-language parity assertions ---`
4. Language-specific shape differences (e.g. `time.Time` vs ISO string) are
   allowed as long as the semantic value is equivalent.

## Fixture specifications

### `identity_current_user.json`

API response from the current-user endpoint.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `user.id` | string | exact match |
| `user.email` | string | exact match |
| `user.name` | string | exact match |
| `tenant.id` | string | exact match |
| `tenant.role` | string | exact match |
| `app.id` | string | exact match |
| `app.permissions` | string[] | exact match (order preserved) |
| `app.roles` | string[] | exact match (order preserved) |
| `attributes.groups` | string[] | exact match (order preserved) |

`attributes.groups` lists the **system** groups the user belongs to plus the
**custom** groups that are bound to *this* app (via a permission or role
binding) that the user is a
member of. Custom groups that are not bound to the app never appear. The fixture
mixes both (`admins`/`everyone` = system, `sales` = app-bound custom); values
are key-sorted.

### `identity_members.json`

Paginated member list response.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `items[].id` | string | exact match |
| `items[].email` | string | exact match |
| `items[].name` | string | exact match |
| `items[].role` | string | exact match (empty string = no role) |
| `limit` | int | exact match |
| `offset` | int | exact match |
| `next_offset` | int \| null | exact match |

### `identity_groups.json`

Group list response.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `items[].id` | string | exact match |
| `items[].key` | string \| null | exact match (one legacy item carries `null` to lock in parser null-tolerance for backward compatibility; live Directory responses always carry a key) |
| `items[].display_name` | string | exact match |
| `items[].kind` | string | exact match |
| `items[].system_kind` | string \| null | exact match |

### `group_key_normalization.json`

Golden vectors for workspace group **key normalization**. Unlike the other
fixtures (which are API response payloads), this pins a pure function: the
canonical pipeline that turns free-form input into a stored or lookup key. All
implementations must agree.

Pipeline (fixed order): trim → NFKC → casefold → NFKC → collapse internal
whitespace runs to `-` → allowlist (Unicode `L*`/`M*`/`N*` plus `-`/`_`; first
char `L*`/`N*`) → reject empty → reject > 64 chars.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `valid[].input` | string | normalizes to `valid[].output` exactly |
| `valid[].output` | string | canonical form |
| `invalid[].input` | string | raises / rejects |
| `invalid[].reason` | string | informational (empty / leading_char / disallowed_char / too_long) |

### `media_stat.json`

Media metadata response (HTTP headers mapped to JSON).

| Field | Type | Parity assertion |
| --- | --- | --- |
| `content_type` | string | see note below |
| `content_length` | int | exact match |

**Note:** All three SDKs strip parameters from `content_type`; for example,
`text/plain; charset=utf-8` becomes `text/plain`. The parity tests load the raw
fixture value and assert the normalized result.

### `email_inbound.json`

Inbound email webhook payload.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `delivery_id` | string | exact match |
| `attempt` | int | exact match |
| `received_at` | string (ISO 8601) | exact match |
| `sent_at` | string \| null | exact match |
| `from.name` | string | exact match |
| `from.address` | string | exact match |
| `to[].address` | string | exact match |
| `cc` | array | length check (empty) |
| `reply_to` | object \| null | exact match |
| `subject` | string | exact match |
| `text` | string \| null | exact match |
| `html` | string \| null | exact match |
| `provider_message_id` | string \| null | exact match |
| `in_reply_to` | string \| null | exact match |
| `references` | string[] | exact match |
| `envelope_to` | string | exact match |
| `authentication.spf` | string | exact match |
| `authentication.dkim` | string | exact match |
| `authentication.dmarc` | string | exact match |
| `spam.score` | float | exact match |
| `spam.verdict` | string | exact match |
| `spam.reasons` | string[] | exact match (empty) |
| `attachments[].id` | string | exact match |
| `attachments[].filename` | string | exact match |
| `attachments[].content_type` | string | exact match |
| `attachments[].size_bytes` | int | exact match |

### `files_key_validation.json`

Shared key-grammar cases for the data-files `files` SDK. Unlike the response
fixtures, this pins the pure `validateKey` decision. Every SDK must accept each
`valid` key and reject each `invalid` key with a `FilesError` (Go: an error).

| Field | Type | Parity assertion |
| --- | --- | --- |
| `valid[]` | string | `validateKey` accepts (round-trips through `write`/`read` in local mode) |
| `invalid[].key` | string | `validateKey` rejects |
| `invalid[].reason` | string | informational (empty / leading slash / trailing slash / empty segment / dot / dotdot / C0/C1 control char / over-255-byte segment) |

Control-char cases cover Unicode category Cc: C0 (U+0000, U+001F), DEL is
per-language, and C1 (U+0085 NEL, U+009F APC). One `invalid` case is a single
256-byte segment (≤ 512 total but over NAME_MAX). The ≤512-byte total-length and
255-byte per-segment boundaries are additionally asserted per-language.

### `files_list_ordering.json`

`list()` contract for the data-files SDK: the injected `KEELSON_FILES_PREFIX` is
stripped from each GCS object name, keys are returned in a single cross-language
order, and paging is absorbed internally. The order is UTF-8 byte order (==
Python code-point order == Go byte order == Node `compareUtf8`). Includes a BMP
private-use char (U+E000) and a non-BMP char (U+10000) so the ordering is locked:
JS's default UTF-16 sort would place U+10000 before U+E000 — wrong.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `files_prefix` | string | the injected `KEELSON_FILES_PREFIX` |
| `object_names[]` | string | GCS object names seeded into the fake store (unordered, includes the prefix-only placeholder) |
| `expected[]` | string | `list("")` output: prefix stripped, placeholder dropped, UTF-8-ordered |

### `files_gcs_object_encoding.json`

GCS object-name percent-encoding for the data-files SDK. The SDK must
percent-encode the full object name (prefix + key), including `/` as `%2F` so
each key is a single flat object (never a nested GCS path), space as `%20`, and
non-ASCII as UTF-8 `%XX`.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `files_prefix` | string | the injected `KEELSON_FILES_PREFIX` |
| `key` | string | the key written (contains a space, a `/`, and non-ASCII) |
| `object_name` | string | the decoded object the fake GCS store must hold after `write(key)` |
| `encoded_object` | string | the percent-encoded object component the request URL must contain (proves `/` → `%2F` on the wire) |

### `files_missing_read.json`

Missing-read contract for the data-files SDK.

| Field | Type | Parity assertion |
| --- | --- | --- |
| `missing_status` | int | `read` maps this status (404) to the absent value; `delete` treats it as success |
| `read_missing_returns_empty` | bool | `read` returns `None`/`null`/`(nil,false)` on 404 |
| `delete_missing_is_success` | bool | `delete` of a missing key does not error |
| `error_statuses[]` | int[] | 401 / 403 / 429 / 5xx must raise/return an error, never be treated as missing |

### `email_event.json`

Email event webhook payload (bounce/complaint/delivery).

| Field | Type | Parity assertion |
| --- | --- | --- |
| `event_id` | string | exact match |
| `event_type` | string | exact match |
| `email_address` | string | exact match |
| `resend_email_id` | string \| null | exact match |
| `bounce_type` | string \| null | exact match |
| `detail` | string \| null | exact match |
| `timestamp` | string (ISO 8601) | exact match |

## Adding a new fixture

1. Create `{domain}_{name}.json` in this directory.
2. Add the fixture specification to this README.
3. Write parity tests in all three languages that load the fixture and
   assert the parity fields.
4. If a field has a known language difference, document it in the public
   cross-SDK parity contract under "Current Accidental Differences" or
   "Intentional Differences".
