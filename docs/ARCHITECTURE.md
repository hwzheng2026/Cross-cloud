# C-DEH Architecture

## High-level

```
   Source Cloud                  C-DEH Gateway                    Target Cloud
  ┌─────────────┐              ┌─────────────────┐               ┌─────────────┐
  │  AWS S3     │── Adapter ──▶│  Catalog + Auth  │── Adapter ──▶│ Aliyun OSS  │
  │  Aliyun OSS │              │  Transfer Engine │               │  Azure Blob │
  │  MinIO      │              │  Policy Engine   │               │  MySQL      │
  │  MySQL      │              │  Audit Log       │               │  SFTP       │
  │  SFTP       │              │  Cache Layer     │               │             │
  └─────────────┘              └─────────────────┘               └─────────────┘
                                       │
                                       ▼
                              ┌────────────────────┐
                              │  Transformers        │
                              │  (mask, redact,      │
                              │   codec, ...)        │
                              └────────────────────┘
```

## Core flow

For each `share` (a named cross-cloud movement), `TransferEngine.run(share)`
executes the following pipeline:

```
1. RBAC check             → user has "run_share" permission?
2. Adapter instantiation → source & dest adapters built from their
                             registered config (in ~/.cdeh/adapters.json)
3. List source objects   → for prefix shares.src_path
4. Per object:
   a. policy check        → PolicyEngine.evaluate(share.policy, ctx)
   b. incremental skip    → compare src fingerprint to last-transferred
                             fingerprint stored on the data_asset in
                             DataCatalog. Skip if unchanged.
   c. fetch src bytes     → through Cache (LRU + TTL)
   d. transform chain     → mask:email,phone → codec:csv:parquet
   e. upload to dst       → single-shot for small, multipart for large
   f. update catalog      → record last_src_fingerprint for next skip
   g. audit append        → entry with hash chain
5. Return TransferResult  → bytes / objects / errors / audit hashes
```

## Components

### Adapters (`cdeh/adapters/`)

`BaseAdapter` is the common interface. Every concrete adapter
implements `stat / list / get / put / delete / ping`. Path semantics
are uniform: `bucket/key/...` translated by each adapter to its
native object/blob path.

| kind          | storage                                | path semantics                     |
|---------------|----------------------------------------|------------------------------------|
| `local`       | local filesystem                       | `<root>/<key>`                     |
| `s3`          | AWS S3 / MinIO / Aliyun OSS / Tencent COS / etc | `<bucket>/<key>` (S3 native) |
| `azure_blob`  | Azure Blob Storage                     | `<container>/<blob>`                |
| `sftp`        | SFTP server                            | `<root>/<path>`                     |
| `mysql`       | MySQL/MariaDB table or query result    | `/<db>/<table>` or `?sql=...`       |

To add a new vendor: subclass `BaseAdapter`, set `kind = "<vendor>"`
and `@register` it in `cdeh/adapters/__init__.py`.

### Transformers (`cdeh/transformers/`)

Apply on the byte stream between `get()` and `put()`. Composable in any
order. All transformers are stateless and `from_config(cfg)`-driven.

| kind       | effect                                                        |
|------------|---------------------------------------------------------------|
| `mask`     | column-level PII masking (CSV), or pattern-based (regex)    |
| `redact`   | drop/hash/k-anonymize column-level fields                    |
| `codec`    | format conversion (CSV ↔ JSON-lines ↔ Parquet)               |

To add a new transformer: subclass `BaseTransformer`, set `kind`, and
register manually in `cdeh/transformers/__init__.py`.

### Core (`cdeh/core/`)

- **`gateway.py`** — `CDEHClient` façade (in-process + HTTP modes)
- **`catalog.py`** — `DataCatalog`: thread-safe JSON-persisted
  registry of data assets and per-asset incremental fingerprint history
- **`transfer.py`** — `TransferEngine` (the orchestrator) and
  `Share` (the declarative config)
- **`auth.py`** — `RBAC`: admin / operator / viewer roles, per-asset ACL
- **`policy.py`** — `PolicyEngine` with built-in `default / gdpr /
  gdpr-strict / block-all` policies, extensible via `add_policy`
- **`audit.py`** — `AuditLog`: append-only JSONL with SHA-256 hash
  chain (each entry references the previous entry's hash). Tampering
  is detectable via `verify_chain()`
- **`cache.py`** — `TransferCache`: in-memory LRU+TTL for repeated reads

### CLI / HTTP

- `cli.py` — `cdeh {adapter, share, catalog, user, audit, serve}`
  subcommands. Adapter configs are stored at `~/.cdeh/adapters.json`
  separately from credentials-free catalog/users/audit files.
- `server.py` — stdlib HTTP gateway. Same surface as `CDEHClient`,
  talks to a running `cdeh serve` process. Useful for: cross-host
  deployments, browser-based dashboards, or integrating with
  Airflow / Argo / cron / etc.

## Decision log

### Why SHA-256 for the audit chain, not a real blockchain?

For an in-process demo and small-team deployments, appending to a JSONL
file with a per-entry hash chain is enough to detect tampering: any
modification to a past entry invalidates the chain from that point
forward. To upgrade to a real blockchain (FISCO BCOS, Hyperledger
Fabric, …) you would replace `AuditLog._save()` with a contract
invocation. The interface (`append`, `verify_chain`, `tail`) is
unchanged.

### Why the catalog lives at `~/.cdeh/catalog.json` instead of a real DB?

For the demo, JSON-on-disk is enough and keeps the dep list small.
In production, swap `DataCatalog` with an Apache Atlas / DataHub /
Unity Catalog adapter — the interface (`register / get / list /
mark_synced`) is small enough to wrap any backend.

### Why "incremental fingerprint" recorded in the catalog instead of
server-side etag comparison?

When the share has transforms (mask / redact / codec), the source
object's etag/mtime and the destination object's etag/mtime are
incomparable: the bytes on the destination are different from the
source even when the source hasn't changed. The catalog stores the
**last-transferred source fingerprint** per `(asset, src_path)`, so
"unchanged since last run" is well-defined regardless of what
transforms are in the pipeline.

For shares without transforms, the engine falls back to a direct
src-vs-dst etag comparison (no catalog write needed) — that path is
faster for the common case.

### Why manual registration of adapters/transformers (no @register
decorator)?

A `@register` decorator on each class needs the registry dict to exist
*before* the class definition evaluates. With Python's import order
that meant importing adapters via `cdeh.adapters.<name>` (not
`cdeh.adapters`) — confusing for users. The manual
`register(local.LocalAdapter)` at the end of `cdeh/adapters/__init__.py`
is explicit and avoids the chicken-and-egg.

### Why stdlib HTTP (no Flask)?

The gateway's surface is small (10 routes). The stdlib
`http.server.ThreadingHTTPServer` is enough and removes a dep.
For real production with auth middleware, rate limits, TLS termination
and a real reverse proxy in front (nginx/Envoy), the stdlib server
is still a fine back-end; swap to Flask/FastAPI only if you need
templated HTML or async streaming.

### Why a "concurrent chunked transfer" in the engine when boto3 already
does multipart transparently?

For adapters that aren't S3 (SFTP, local, MySQL), there's no
native multipart. The engine's chunking keeps the cross-adapter
contract uniform: every adapter sees a `put(path, data_bytes, ...)`.
For S3 specifically, the engine bypasses multipart and calls
`put_object` once per part; boto3 is happy with this.

### Why store adapter configs separately from catalog/users?

Catalog and user files are committed to git in the demo (no
secrets). Adapter configs hold `access_key / secret_key / password`
and must live in a secrets manager (Vault, AWS Secrets Manager,
1Password, …) in production. Splitting them out at the file-path
level (`adapters.json` vs `catalog.json / users.json`) makes the
secret boundary obvious and matches the deployment model.