#!/usr/bin/env bash
# Stop the demo stack.
set -e
cd "$(dirname "$0")"
docker compose down -v 2>/dev/null || true
rm -rf cdeh_data
echo "[stop.sh] done"