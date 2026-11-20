# SaaS readiness

Status key: ✅ done · 🟡 seam exists, not built · ⬜ not started

| Item | Status | Notes |
|---|---|---|
| Multi-tenancy: `workspace_id` everywhere | ✅ | Test asserts every business table has it |
| Repository layer enforces workspace scope | ✅ | `WorkspaceRepository`; cross-read and cross-write tests |
| Postgres row-level security | ⬜ | Later hardening |
| AuthN/AuthZ seam (`current_principal`) | 🟡 | Static token → seeded workspace; roles in `memberships` |
| Role checks (owner/editor/viewer) | ⬜ | `Principal.role` is carried; not enforced yet |
| Metering (`usage_events`) | 🟡 | Table exists; writers land in M1 (transcribe minutes) and M3 (tokens) |
| Quotas / rate limits (`QuotaService`) | 🟡 | `AllowAllQuota` adapter |
| Billing | ⬜ | Intentionally absent; `usage_events` is the contract |
| Job isolation: per-priority queues | ✅ | ADR-0002 |
| Per-workspace concurrency limit | 🟡 | Config stub `per_workspace_max_concurrent_jobs` |
| GPU workers scale separately | 🟡 | `worker-gpu` profile on `transcribe*` queues |
| Storage keys workspace-prefixed | ✅ | `BlobKey` enforces `ws/<uuid>/` |
| S3 adapter | ⬜ | `BlobStore` port ready |
| Encrypted per-workspace secrets | 🟡 | `publish_targets.secret_ref` column; `SecretBox` port planned for M6 |
| Audit log | 🟡 | Table + `AuditRepo.record`; used from M1 (override, retire) |
| Takedown flow | ⬜ | M1 |
| ToS/DMCA process documented | ⬜ | Before any public use |
| Per-workspace cost attribution | 🟡 | `usage_events.workspace_id`; report in M3 |
| Versioned scoring profiles & prompts | 🟡 | Immutable `(name, version)` rows; PROMPTS.md versions in M3 |
| API versioned `/api/v1` | ✅ | |
| Pagination | 🟡 | Repositories take limit/offset; endpoints in M1+ |
| Idempotency keys on mutating endpoints | 🟡 | `idempotency_keys` table; middleware with the first POST (M1) |
