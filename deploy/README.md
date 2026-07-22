# CDEH — Docker / docker-compose demo

Quickly stand up a C-DEH gateway + two MinIO instances (one as
"source cloud", one as "target cloud") for an end-to-end demo.

## Run

```bash
cd deploy
docker compose up -d
# wait ~5s for MinIO to be ready
./start.sh
# (registers two adapters, defines a share, runs it)
```

## What's running

- `minio-source`  — simulates AWS S3 / Aliyun OSS / etc. on `:9000`
- `minio-target`  — simulates a different cloud on `:9001`
- `cdeh-server`   — the C-DEH HTTP gateway on `:8080`

Open MinIO console: http://localhost:9001 (minioadmin / minioadmin)
Open C-DEH:     http://localhost:8080/healthz

## Stop

```bash
cd deploy
docker compose down -v
./stop.sh
```
