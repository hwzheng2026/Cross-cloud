#!/usr/bin/env bash
# Register two MinIO adapters, create + run a demo share.
# Requires: docker compose up -d (MinIO containers running) and the
# cdeh CLI installed locally.
set -e

cd "$(dirname "$0")"

# Wait for MinIO to be ready
echo "[start.sh] waiting for MinIO containers..."
for i in {1..30}; do
  if curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null && \
     curl -sf http://127.0.0.1:9100/minio/health/live >/dev/null; then
    break
  fi
  sleep 1
done
echo "[start.sh] MinIO up"

# Make sure both buckets exist (mc is available inside the MinIO image,
# or we use a Python one-liner via boto3 — we'll use a tiny script).
python3 - <<'PY'
import boto3, sys
for endpoint, bucket in [
    ("http://127.0.0.1:9000", "source-bucket"),
    ("http://127.0.0.1:9100", "target-bucket"),
]:
    s3 = boto3.client("s3", endpoint_url=endpoint,
                      aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin")
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        s3.create_bucket(Bucket=bucket)
        print(f"[start.sh] created bucket {bucket!r} on {endpoint}")
    # Drop a demo file
    s3.put_object(Bucket=bucket, Key="orders/2024-Q1.csv",
                 Body=b"id,name,email,phone\n1,Alice,alice@example.com,555-0100\n2,Bob,bob@example.com,555-0101\n")
    print(f"[start.sh] seeded {endpoint}{endpoint[-4:]}/{bucket}/orders/2024-Q1.csv")
PY

# Register adapters
echo "[start.sh] registering adapters"
cdeh --config-dir ./cdeh_data adapter register aws-prod s3 \
    --adapter-arg endpoint=http://127.0.0.1:9000 \
    --adapter-arg bucket=source-bucket \
    --adapter-arg access_key=minioadmin \
    --adapter-arg secret_key=minioadmin

cdeh --config-dir ./cdeh_data adapter register oss-prod s3 \
    --adapter-arg endpoint=http://127.0.0.1:9100 \
    --adapter-arg bucket=target-bucket \
    --adapter-arg access_key=minioadmin \
    --adapter-arg secret_key=minioadmin

# Add an operator + admin user
cdeh --config-dir ./cdeh_data user add demo-operator --role operator --api-key demopass

# Define a share
echo "[start.sh] defining demo share"
cdeh --config-dir ./cdeh_data share create daily-orders \
    --source aws-prod:/orders \
    --dest oss-prod:/incoming \
    --transform "mask:email,phone" \
    --policy gdpr-strict \
    --incremental etag

# Run it
echo "[start.sh] running share"
cdeh --config-dir ./cdeh_data share run daily-orders --user demo-operator

# Show audit
echo ""
echo "[start.sh] audit tail:"
cdeh --config-dir ./cdeh_data audit tail -n 5

echo ""
echo "[start.sh] ✓ done.  Visit http://localhost:8080/healthz"