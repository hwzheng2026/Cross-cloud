# docs/ — C-DEH deep-dive

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — system diagram, components,
  decision log (why this design, what was tried, what failed, what
  the upgrade path is).
- **[DEPLOY.md](DEPLOY.md)** — local / docker / k8s / Airflow
  deployment recipes with copy-pasteable `cdeh adapter register`
  commands for every major cloud.
- **[API.md](API.md)** — Python + CLI + HTTP reference. All routes
  with example bodies, all subcommands with example invocations.
- **[SECURITY.md](SECURITY.md)** — trust boundary, production hardening
  checklist, PII handling, audit log format and tamper detection.

The README has the 5-minute quick start. These docs are the
deep-dive for production deployments.