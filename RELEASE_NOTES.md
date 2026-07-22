# v0.1.0 (2026-07-22) — Initial Release

The first public release of **Cross-Cloud Data Exchange Hub (C-DEH)**,
a plugin-based, secure, high-performance data sharing component for
industrial-internet cross-cloud environments.

## What's in this release

### Adapters (5)

- **Local filesystem** — `local` kind, useful for testing / on-prem
- **S3-compatible** — `s3` kind, single class for AWS S3, MinIO, Aliyun OSS,
  Tencent COS, Cloudflare R2, Wasabi, Backblaze B2, Ceph RGW
- **Azure Blob Storage** — `azure_blob` kind, native SDK
- **SFTP** — `sftp` kind, lazy paramiko import
- **MySQL** — `mysql` kind, share a table or query result as CSV

### Transformers (5)

- **`mask:col1,col2`** — CSV column-level masking with length-preserving `*`
- **`redact:drop:|hash:|k-anon:`** — drop / HMAC-pseudonymize / k-anonymize
- **`codec:from:to`** — CSV ↔ JSON-lines ↔ Parquet
- **`compress:gzip[:level]`** — gzip / zstd (graceful fallback if zstd not installed)
- **`encrypt:<hex-key>`** — AES-256-GCM in-flight, supports `env:VAR` keys

### Transfer engine

- Sync, async (`Future`), concurrent batch (`run_batch` with shared ThreadPoolExecutor)
- Incremental sync: 3-layer fingerprint (etag / mtime / per-asset catalog fingerprint)
- Resumable: `FileCheckpointStore` (per-share per-path JSON) and `SQLiteCheckpointStore` (WAL mode)
- Retry policy: exponential backoff with per-attempt audit entries
- Per-share filters: `path_glob`, `path_prefix`, `name_regex`, `size_min/max`,
  `mtime_after/before`, `tag`, `department`
- Concurrent chunked transfer (4 threads by default)

### Access control

- RBAC: admin / operator / viewer roles
- Per-asset ACL (`allowed_assets`)
- **Group + department data isolation** — assets tagged `dept:sales`
  are only visible to users with `department="sales"`; admins bypass

### Policy engine

Built-in policies: `default`, `gdpr`, `gdpr-strict`, plus pluggable custom policies
supporting `require_classification`, `require_tags`, `require_hosts_https`,
`rate_limit_per_minute`, `transform_required`.

### Audit

Append-only JSONL with SHA-256 hash chain (each entry references the previous
entry's hash). `cdeh audit verify` detects tampering. 100k-line / 50MB rotation.

### API surfaces

- **Python SDK** — `from cdeh import CDEHClient`
- **CLI** — `cdeh {adapter, share, catalog, user, audit, serve}`
- **HTTP gateway** — stdlib-only (no Flask dependency), 10 routes

## Tests

**44 tests, 100% passing** in 0.27s:

- `test_smoke.py` — 28 unit / integration tests covering adapters, transformers,
  policy, RBAC, cache, audit, end-to-end
- `test_sync_async_batch.py` — 6 tests for the async / batch API
- `test_new_features.py` — 17 tests for the production-grade additions
  (checkpoint, compress, encrypt, filters, retry, RBAC dept isolation)

## Documentation

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture + decision log
- [DEPLOY.md](docs/DEPLOY.md) — local / Docker / k8s / Airflow deployment recipes
- [API.md](docs/API.md) — Python / CLI / HTTP reference
- [SECURITY.md](docs/SECURITY.md) — trust boundary, production hardening, PII

## Install

```bash
pip install cdeh            # core (CLI + local adapter + stdlib HTTP gateway)
pip install cdeh[s3]        # + AWS S3 / MinIO / Aliyun OSS / Tencent COS
pip install cdeh[parquet]   # + pyarrow for the parquet codec
pip install cdeh[all]       # everything (boto3, azure, paramiko, pymysql, pyarrow)
```

## Quick start

```bash
# Register an S3 adapter
cdeh adapter register aws-prod s3 \
  --adapter-arg endpoint=https://s3.amazonaws.com \
  --adapter-arg bucket=my-data \
  --adapter-arg access_key=$AWS_KEY \
  --adapter-arg secret_key=$AWS_SECRET

# Define a cross-cloud share with PII masking
cdeh share create daily-orders \
  --source aws-prod:/orders/2024 \
  --dest aliyun-prod:/incoming/orders/2024 \
  --transform "mask:email,phone" \
  --policy gdpr-strict \
  --incremental etag

# Run it
cdeh share run daily-orders --user ops-svc
```

## Known limitations (v0.1.0)

- Kafka / RabbitMQ async backend: shared ThreadPoolExecutor works for
  single-process deployments; a distributed worker backend is a planned
  v0.2.0 feature
- Redis checkpoint backend: implement `CheckpointStore` protocol to plug in
  (5 methods, ~80 lines of code)

## License

MIT — see [LICENSE](LICENSE)