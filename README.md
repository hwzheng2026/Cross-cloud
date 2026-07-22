# Cross-Cloud Data Exchange Hub (C-DEH)

> 工业互联网跨云数据共享流通通用组件。解决跨异构云平台（AWS、Azure、GCP、
> 阿里云、腾讯云、MinIO、SFTP、MySQL）之间的数据安全共享、权限控制、
> 协议兼容、审计追溯、性能优化等核心问题。

![tests](https://img.shields.io/badge/tests-44%20passing-brightgreen)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

## 核心能力

| 类别 | 功能 |
|------|------|
| **异构云兼容** | 5 个适配器：local / S3 兼容（AWS S3 / Aliyun OSS / Tencent COS / MinIO）/ Azure Blob / SFTP / MySQL |
| **传输模式** | 同步 / 异步 Future / 并发批处理 (`run_batch`) |
| **增量同步** | etag / mtime / per-asset catalog fingerprint 三层 |
| **断点续传** | 持久化 checkpoint（File + SQLite 后端，可注入 Redis） |
| **压缩** | gzip / zstd（缺包自动降级） |
| **加密** | AES-256-GCM，支持 KMS key 包装 |
| **访问控制** | RBAC（admin/operator/viewer）+ 资产 ACL + group + **部门数据隔离** |
| **策略引擎** | default / gdpr / gdpr-strict / 自定义（含速率限制） |
| **审计** | SHA-256 hash chain 不可变日志，篡改可检测 |
| **数据变换** | mask / redact / codec / compress / encrypt（可链式） |
| **过滤器** | path glob / prefix / regex / size / mtime / tag / dept |
| **重试** | 指数退避，retry 次数可配 |

## 5 分钟快速开始

### 安装

```bash
git clone git@github.com:hwzheng2026/Cross-cloud.git
cd Cross-cloud
pip install -e ".[s3,parquet]"
```

### 跑测试（44 个测试，< 1 秒）

```bash
python3 -m pytest tests/ -v
```

### 端到端 demo（local 适配器模拟 AWS → OSS）

```bash
PYTHONPATH=. python3 examples/cross_cloud_share.py        # 跨云增量同步 + PII mask
PYTHONPATH=. python3 examples/policy_demo.py              # RBAC + policy 联防
PYTHONPATH=. python3 examples/batch_demo.py               # 并发跑 3 个 share
PYTHONPATH=. python3 examples/production_features_demo.py # 全部生产级能力
```

### 注册一个真实云适配器并跑

```bash
# 1. 注册 AWS S3
cdeh adapter register aws-prod s3 \
  --adapter-arg endpoint=https://s3.amazonaws.com \
  --adapter-arg bucket=my-bucket \
  --adapter-arg access_key=$AWS_KEY \
  --adapter-arg secret_key=$AWS_SECRET

# 2. 注册 Aliyun OSS（S3 兼容）
cdeh adapter register aliyun-prod s3 \
  --adapter-arg endpoint=https://oss-cn-hangzhou.aliyuncs.com \
  --adapter-arg bucket=my-bucket \
  --adapter-arg access_key=$ALI_KEY \
  --adapter-arg secret_key=$ALI_SECRET

# 3. 定义一个跨云 share（带 PII mask + 增量 + GDPR-strict 策略）
cdeh share create daily-orders \
  --source aws-prod:/orders/2024 \
  --dest aliyun-prod:/incoming/orders/2024 \
  --transform "mask:email,phone" \
  --policy gdpr-strict \
  --incremental etag

# 4. 同步跑
cdeh share run daily-orders --user ops-svc

# 5. 加到 cron
# 0 3 * * *  cdeh share run daily-orders --user nightly-export
```

### HTTP gateway（跨机器 / 浏览器集成）

```bash
cdeh serve --host 0.0.0.0 --port 8080

# 健康检查
curl -sf http://localhost:8080/healthz

# 触发 share
curl -X POST http://localhost:8080/shares/daily-orders/run \
  -H 'Content-Type: application/json' -d '{}'
```

## Docker demo（最简）

```bash
cd deploy
docker compose up -d          # 启 2 MinIO + cdeh gateway
./start.sh                    # 注册 adapter + 跑 demo share
# 访问:
#   cdeh gateway: http://localhost:8080/healthz
#   MinIO source: http://localhost:9090  (minioadmin/minioadmin)
#   MinIO target: http://localhost:9091
```

## Python SDK 速查

```python
from cdeh import CDEHClient

# In-process mode
client = CDEHClient(config_dir="/etc/cdeh")

# HTTP client mode
client = CDEHClient("http://cdeh-gateway:8080")

# Adapter
client.register_adapter("aws-prod", "s3",
                        endpoint="https://s3.amazonaws.com", bucket="...",
                        access_key="...", secret_key="...")

# Share with all bells and whistles
client.share.create(
    name="secure-export",
    source="aws-prod:/data",
    dest="aliyun-prod:/encrypted",
    transform=["compress:gzip:9", "encrypt:${DEK_HEX}", "mask:email,phone"],
    policy="gdpr-strict",
    filters=[
        {"path_glob": "*.csv"},
        {"size_min": 1024, "size_max": 10*1024*1024},
        {"mtime_after": "2024-01-01"},
    ],
    resumable=True,
    retry={"max_attempts": 3, "initial_backoff_seconds": 1.0,
           "backoff_factor": 2.0},
)

# Sync / Async / Batch
result = client.run_share("secure-export", user="airflow-prod")
fut = client.run_share_async("secure-export", user="airflow-prod")
batch = client.run_share_batch(["secure-export", "weekly-export"],
                                 max_workers=2)

# Audit
print(client.audit_chain_status())
client.audit_tail(50)
```

## 文档

- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — 系统架构 + 设计决策日志
- **[DEPLOY.md](docs/DEPLOY.md)** — 各种部署模式（local / Docker / k8s / Airflow）
- **[API.md](docs/API.md)** — Python / CLI / HTTP 完整参考
- **[SECURITY.md](docs/SECURITY.md)** — 信任边界、生产 hardening、PII 处理

## 架构

```
   Source Cloud                  C-DEH Gateway                    Target Cloud
  ┌─────────────┐              ┌─────────────────┐               ┌─────────────┐
  │  AWS S3     │── Adapter ──▶│  Catalog + Auth  │── Adapter ──▶│ Aliyun OSS  │
  │  Aliyun OSS │              │  Transfer Engine │               │  Azure Blob │
  │  MinIO      │              │  Policy Engine   │               │  MySQL      │
  │  MySQL      │              │  Audit Log       │               │  SFTP       │
  │  SFTP       │              │  Cache Layer     │               │             │
  └─────────────┘              │  Checkpointing   │               └─────────────┘
                                └─────────────────┘
```

详细流程见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## License

MIT