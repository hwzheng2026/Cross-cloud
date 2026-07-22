# C-DEH Security

## Trust boundary

- **In-process mode** (`CDEHClient(config_dir=...)`): the client
  process is fully trusted. It reads adapter configs (which contain
  cloud credentials) from the local `adapters.json`.
- **HTTP mode** (`CDEHClient("http://...")`): the gateway is a server
  the client connects to. **In the current implementation, the HTTP
  server does not authenticate clients** — assume the network between
  you and the gateway is trusted, or front it with a reverse proxy
  that enforces mTLS or bearer-token auth.

## Production hardening checklist

- [ ] **Put the gateway behind TLS** (nginx / Envoy / Caddy) — never
      expose port 8080 directly.
- [ ] **Move adapter credentials to a secrets manager**. The current
      `~/.cdeh/adapters.json` is plain-text JSON. Use Vault Agent,
      AWS SM CSI driver, or k8s External Secrets to mount a
      read-only credentials file at the same path.
- [ ] **Set up RBAC properly** before exposing the gateway to anyone
      but operators: `cdeh user add ... --role viewer` for read-only
      consumers, `--role operator` for people who can trigger shares,
      `--role admin` for people who can register adapters.
- [ ] **Per-asset ACL** for sensitive data: `cdeh user add <name>
      --assets <list>` limits what that user can see.
- [ ] **Audit log rotation and off-host storage** — the audit log
      lives at `~/.cdeh/audit.log`. Ship to a SIEM (Splunk, ELK,
      Loki) for tamper-evident retention beyond the host's lifetime.
- [ ] **Audit log integrity monitoring** — `cdeh audit verify` should
      be in your daily ops dashboard. A `False` return means someone
      tampered with the log.
- [ ] **Network segmentation** — the cdeh gateway should be on a
      management VPC with Egress to your S3 / OSS / etc. but no
      Ingress from the open internet.
- [ ] **Pre-signed-URL pattern for cross-org sharing** — when sharing
      with an external partner, generate a short-lived pre-signed URL
      on your bucket and hand that to them. The cdeh adapter
      abstraction is compatible: write a thin adapter that
      delegates `get/put` to URL fetches.

## Threats C-DEH does NOT cover (and what to do about them)

| threat | not in scope because | mitigation |
|---|---|---|
| Malicious user with admin role | the engine is a tool, not a sandbox | Use your org's SSO + audit-log review to detect abuse |
| Compromised gateway host | the engine runs in trust with the OS | Use HSM-backed creds (AWS roles / IRSA) so a stolen secret is useless |
| Adversarial adapter module | third-party adapter plugins are loaded with full process privileges | Pin the adapter set, sign adapter wheel, restrict who can register adapters |
| DoS via large object transfer | the engine has no rate limit on object size | Pre-validate `share.policy.max_object_size_bytes` per-share |
| Schema poisoning | the engine has no schema awareness | Pair with a data-quality tool (Great Expectations, dbt) before the engine runs |

## PII handling

C-DEH's `mask:` and `redact:` transformers cover the most common GDPR /
CCPA / HIPAA needs:

- **Column-level masking** (reversible / irreversible):
  - `mask:col1,col2`  — replace value with `*` (length-preserving)
  - `redact:drop:col`  — drop the column entirely
  - `redact:hash:col`  — replace with HMAC(sha256, secret)[-16]  (pseudonym)
  - `redact:k-anon:col` — bucket numeric; truncate strings to 3 chars
- **Pattern-level masking** (text, no schema required):
  - `mask:email,phone,ssn`  — pattern match: email / phone / SSN

**Not covered** (and intentional, to keep the contract small): format-
preserving encryption, differential privacy, or per-user row-level
filters. Use a downstream tool for those.

For regulation-specific policies, use the built-in `gdpr` or
`gdpr-strict` policies as a starting point and add a custom policy:

```bash
cdeh policy show gdpr-strict  # see the YAML
# (edit ~/.cdeh/policies/gdpr-strict.yaml to extend; not yet
# exposed in the CLI but easy to add)
```

## Audit log format

Each entry is one JSONL line at `~/.cdeh/audit.log`:

```json
{
  "ts": "2026-07-22T04:57:19.227715+00:00",
  "user": "demo-operator",
  "action": "share.transfer",
  "asset": "daily-orders",
  "src_adapter": "local",
  "src_path": "/orders/2024-Q1.csv",
  "dst_adapter": "local",
  "dst_path": "/incoming/2024-Q1.csv",
  "bytes": 87,
  "policy": "default",
  "transforms": "mask:email,phone",
  "success": true,
  "error": "",
  "extra": {"src_etag": "abc123..."},
  "prev_hash": "0011223344556677",
  "entry_hash": "9988776655443322"
}
```

The hash chain: `entry_hash = sha256(stable_json(entry_minus_entry_hash))[:16]`,
and `prev_hash` is the previous entry's `entry_hash`. Rotation at
100k lines or 50MB: file moves to `audit.log.1` and a new chain
starts (the new chain's first entry has `prev_hash = "0" * 16`).

`audit verify` walks the file and recomputes the chain. A `False`
result means an entry was modified after the fact.