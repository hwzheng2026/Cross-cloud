# C-DEH API Reference

## Python API

```python
from cdeh import CDEHClient
client = CDEHClient(config_dir="/etc/cdeh")      # embedded mode
# or
client = CDEHClient("http://cdeh.example:8080")    # HTTP mode
```

### Adapters

```python
client.register_adapter(name="aws-prod", kind="s3",
                        endpoint="https://s3.amazonaws.com",
                        bucket="my-data", region="us-east-1",
                        access_key=AK, secret_key=SK)
client.list_adapters()    # → dict[name, {kind, ...}]
client.remove_adapter("aws-prod")
client.get_adapter_config("aws-prod")
```

### Shares

```python
from cdeh.core.transfer import Share

client.share.create(
    name="daily-export",
    source="aws-prod:/orders/2024/",
    dest="oss-prod:/incoming/orders/2024/",
    transform=["mask:email,phone", "codec:csv:parquet"],
    policy="gdpr-strict",
    incremental="etag",
    parallelism=4,
)
client.share.list()          # → list[Share]
client.share.get("name")
client.share.run("name", user="svc-account")
client.share.delete("name")
```

### Catalog

```python
from cdeh.core.catalog import DataAsset

client.catalog.register(DataAsset(
    name="customer-orders",
    adapter="s3", path="/orders/2024/",
    classification="confidential",
    tags=["gdpr-cleared", "production"],
))
client.catalog.list(tag="gdpr-cleared", classification="confidential")
client.catalog.get("customer-orders")
client.catalog.delete("customer-orders")
```

### RBAC

```python
client.rbac.add_user("alice", role="viewer", api_key="alice-key-1")
client.rbac.add_user("bob",   role="operator", api_key="bob-key-1",
                     allowed_assets=["public-data"])
client.rbac.check("alice", "run_share", asset_name="public-data")  # → False
client.rbac.list_users()
```

### Policy

```python
from cdeh.core.policy import Policy

client.add_policy(Policy(
    id="hipaa-strict",
    require_classification=["confidential"],
    require_tags=["hipaa-cleared"],
    transform_required=["mask"],
    rate_limit_per_minute=30,
))
```

### Audit

```python
client.audit_chain_status()   # → {ok, entries, broken_at}
client.audit_tail(50)          # → list[dict]
```

## CLI

```text
cdeh --config-dir <dir> <subcommand> [...]

Subcommands:
  adapter list|register|remove|get|ping
  share list|create|get|delete|run
  catalog list|register|show|delete
  user list|add|show
  audit tail|verify
  serve [--host H] [--port P]
```

### adapter register

```bash
cdeh adapter register <name> <kind> [--adapter-arg key=value]...
  kind: s3 | azure_blob | sftp | mysql | local
  --adapter-arg repeatable. e.g. --adapter-arg endpoint=https://s3.amazonaws.com --adapter-arg bucket=my-data
```

### share create

```bash
cdeh share create <name> \
  --source <adapter>:<path> \
  --dest <adapter>:<path> \
  [--transform csv-list] \
  [--policy name] \
  [--incremental etag|mtime|none] \
  [--parallelism N] \
  [--description text]

# transform format:
#   "mask:col1,col2"           — mask CSV columns
#   "mask:email,phone"         — mask by name
#   "redact:drop:ssn"          — drop column "ssn"
#   "redact:hash:user_id"      — HMAC-pseudonymize
#   "redact:k-anon:age,zip"    — generalize quasi-identifiers
#   "codec:csv:parquet"        — convert format
#   "codec:jsonl:csv"          — convert
# Multiple transforms joined with ","
```

### share run

```bash
cdeh share run <name> [--user <name>]
```

## HTTP API

The HTTP gateway is at `cdeh serve` (default port 8080). All routes
return JSON. Errors return `{"error": "<message>"}` with a 4xx/5xx
status.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET    | `/healthz`                   | — | `{"ok": true}` |
| GET    | `/adapters`                  | — | `{"<name>": {kind, ...}, ...}` |
| POST   | `/adapters`                  | `{name, kind, ...config}` | `{"ok": true, "name": ...}` |
| GET    | `/adapters/<name>`           | — | adapter config dict |
| DELETE | `/adapters/<name>`           | — | `{"ok": bool, "name": ...}` |
| GET    | `/shares`                    | — | list of share dicts |
| POST   | `/shares`                    | `{name, source, dest, transform?, policy?, incremental?, parallelism?, description?}` | `{"ok": true, "name": ...}` |
| GET    | `/shares/<name>`             | — | share dict |
| DELETE | `/shares/<name>`             | — | `{"ok": bool, "name": ...}` |
| POST   | `/shares/<name>/run`         | `{user?}` | TransferResult dict |
| GET    | `/catalog`                   | — | list of asset dicts |
| GET    | `/audit?n=20`                | — | list of last-N audit entries |
| GET    | `/audit/verify`              | — | `{ok, entries, broken_at?}` |

### Example

```bash
curl -s http://localhost:8080/adapters
curl -s -X POST http://localhost:8080/shares \
  -H 'Content-Type: application/json' \
  -d '{"name":"daily","source":"aws-prod:/x","dest":"oss-prod:/y"}'
curl -s -X POST http://localhost:8080/shares/daily/run \
  -H 'Content-Type: application/json' -d '{}'
```