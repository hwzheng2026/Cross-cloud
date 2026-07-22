# C-DEH Deployment

## Quick local demo (no cloud credentials)

```bash
git clone <repo-url> cdeh
cd cdeh
pip install -e ".[s3,parquet]"        # or just .[s3] for plain S3 / MinIO

cd deploy
docker compose up -d                  # spins up two MinIO + cdeh-server
./start.sh                            # registers adapters + creates + runs demo share
```

Open the consoles:
- cdeh-server: http://localhost:8080/healthz
- MinIO (source): http://localhost:9090
- MinIO (target): http://localhost:9091

Both MinIO logins: `minioadmin` / `minioadmin`.

After the demo runs, the target MinIO bucket (`target-bucket/incoming/`)
contains the masked CSV.

## Production deployment patterns

### 1. Local install on a single VM (small/medium deployments)

```bash
# install
pip install cdeh[s3,azure,mysql,parquet]

# register your real cloud adapters (DO NOT hardcode credentials — use env)
cdeh adapter register aws-prod s3 \
  --adapter-arg endpoint=https://s3.amazonaws.com \
  --adapter-arg bucket=acme-prod-data \
  --adapter-arg access_key=$AWS_ACCESS_KEY \
  --adapter-arg secret_key=$AWS_SECRET_KEY

cdeh adapter register aliyun-prod s3 \
  --adapter-arg endpoint=https://oss-cn-hangzhou.aliyuncs.com \
  --adapter-arg bucket=acme-asia-data \
  --adapter-arg access_key=$ALIYUN_KEY \
  --adapter-arg secret_key=$ALIYUN_SECRET

# schedule
0 3 * * *   cdeh --config-dir /etc/cdeh share run daily-export
```

### 2. Docker / Compose (the included `docker-compose.yml`)

```bash
cd deploy
docker compose up -d
```

Adapters are persisted in a named volume `cdeh_data`. To inspect:

```bash
docker exec cdeh-gateway cdeh --config-dir /home/cdeh/.cdeh adapter list
docker exec cdeh-gateway cdeh --config-dir /home/cdeh/.cdeh share list
```

### 3. Kubernetes (Helm chart in `deploy/helm/` — TODO)

A minimal k8s deployment would be:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: cdeh }
spec:
  replicas: 1
  selector: { matchLabels: { app: cdeh } }
  template:
    metadata: { labels: { app: cdeh } }
    spec:
      containers:
        - name: cdeh
          image: your-registry/cdeh:0.1.0
          args: ["serve", "--host", "0.0.0.0", "--port", "8080"]
          env:
            - name: HOME
              value: /home/cdeh
          volumeMounts:
            - { name: cdeh-data, mountPath: /home/cdeh/.cdeh }
      volumes:
        - name: cdeh-data
          emptyDir: {}      # for ephemeral demo; use PVC for real deployments
```

A CronJob would call the HTTP gateway's `POST /shares/<name>/run` on
the schedule.

### 4. Sidecar in Airflow / Argo

The Python API is the natural integration point:

```python
from cdeh import CDEHClient

def export_to_target(**context):
    c = CDEHClient(config_dir="/opt/cdeh")
    result = c.run_share("daily-export", user=f"airflow-{context['ds']}")
    if result.errors:
        raise RuntimeError(result.errors)
    return result.to_dict()

export_to_target = python_operator(export_to_target)
```

## Connecting to real clouds

### AWS S3 (or any S3-compatible)

```bash
cdeh adapter register aws-prod s3 \
  --adapter-arg endpoint=https://s3.us-east-1.amazonaws.com \
  --adapter-arg region=us-east-1 \
  --adapter-arg bucket=acme-prod \
  --adapter-arg access_key=$AWS_ACCESS_KEY \
  --adapter-arg secret_key=$AWS_SECRET_KEY
```

### Aliyun OSS

OSS exposes an S3-compatible endpoint. Use the same `s3` kind with
the OSS endpoint URL:

```bash
cdeh adapter register aliyun-prod s3 \
  --adapter-arg endpoint=https://oss-cn-hangzhou-internal.aliyuncs.com \
  --adapter-arg region=cn-hangzhou \
  --adapter-arg bucket=acme-data \
  --adapter-arg access_key=$ALIYUN_KEY \
  --adapter-arg secret_key=$ALIYUN_SECRET
```

### Tencent COS

Same pattern — COS is S3-compatible:

```bash
cdeh adapter register tencent-prod s3 \
  --adapter-arg endpoint=https://cos.ap-guangzhou.myqcloud.com \
  --adapter-arg region=ap-guangzhou \
  --adapter-arg bucket=acme-1250000000 \
  --adapter-arg access_key=$TENCENT_SECRET_ID \
  --adapter-arg secret_key=$TENCENT_SECRET_KEY
```

### Azure Blob

```bash
cdeh adapter register azure-prod azure_blob \
  --adapter-arg account_name=acmeprod \
  --adapter-arg account_key=$AZURE_STORAGE_KEY \
  --adapter-arg container=shared
```

### SFTP

```bash
cdeh adapter register partner-sftp sftp \
  --adapter-arg host=files.partner.com \
  --adapter-arg port=22 \
  --adapter-arg user=cdeh \
  --adapter-arg key_path=/etc/cdeh/partner_key \
  --adapter-arg root=/srv/data
```

### MySQL (table-level)

```bash
cdeh adapter register analytics-mysql mysql \
  --adapter-arg host=warehouse.internal \
  --adapter-arg port=3306 \
  --adapter-arg user=readonly \
  --adapter-arg password=$MYSQL_PWD \
  --adapter-arg database=analytics
```

Then share a single table: `--source analytics-mysql:/analytics/daily_orders`.

### MinIO (your own object store)

```bash
docker run -d --name minio -p 9000:9000 -p 9090:9090 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data --console-address :9090
cdeh adapter register minio-local s3 \
  --adapter-arg endpoint=http://127.0.0.1:9000 \
  --adapter-arg bucket=source \
  --adapter-arg access_key=minioadmin --adapter-arg secret_key=minioadmin
```

## Configuration

Everything lives in `~/.cdeh/` (or `--config-dir`):

```
~/.cdeh/
├── catalog.json         # data assets + per-asset fingerprint history
├── users.json           # users + roles + api keys
├── audit.log            # append-only hash-chained audit (rotates at 100k lines)
├── audit.log.1          # rotated copy
├── adapters.json        # adapter configs (contain secrets — protect!)
└── shares/              # one JSON file per share
    ├── daily-export.json
    └── ...
```

In production, symlink `adapters.json` to a secrets-manager mount
(Vault agent, AWS SM CSI driver, k8s External Secrets, etc.).

## Operational checks

```bash
# verify audit chain integrity (should be 'ok: True')
cdeh audit verify

# tail the last 50 audit entries
cdeh audit tail -n 50

# list all registered shares
cdeh share list

# one-off run as a different user
cdeh share run daily-export --user ops-svc-account
```

## Upgrades

The on-disk format is JSON. Backwards-compatible reads: old
`catalog.json` (without `src_fingerprints` field on assets) is
silently read as `{}`. Run a smoke test (`cdeh share list`) after
upgrade to confirm.

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| `unknown adapter kind` | adapter module not imported | `python -c "import cdeh.adapters; from cdeh.adapters import local, s3, ..."` |
| `no transformer for kind` | transformer module not imported | same — `from cdeh.transformers import mask, redaction, codec` |
| `rbac_denied` | user role too low | `cdeh user add <name> --role operator` |
| `policy_denied` | data classification / HTTPS / transform requirement | see `cdeh policy show` |
| `rbac_denied: missing required tags` | data asset missing required tags | `cdeh catalog register ... --tags <list>` |
| share runs but transfers 0 bytes, all "skipped" | incremental fingerprint cache thinks source is unchanged | `cdeh catalog show <name>` then `cdeh share delete <name>` to reset the catalog entry |