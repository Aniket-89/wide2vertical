# SaaS readiness

Status key: ✅ done · 🟡 seam exists, not built · ⬜ not started

| Item | Status | Notes |
|---|---|---|
| Multi-tenancy: `workspace_id` everywhere | ✅ | Test asserts every business table has it |
| Repository layer enforces workspace scope | ✅ | `WorkspaceRepository`; cross-read and cross-write tests |
| Postgres row-level security | ⬜ | Later hardening |
| Composite `(workspace_id, id)` FKs | ⬜ | DB-level backstop against cross-tenant references; app layer already validates (e.g. `scoring_profile_id`). Schema change: ask first (post-M2 rule) |
| AuthN/AuthZ seam (`current_principal`) | 🟡 | Static token → seeded workspace; roles in `memberships` |
| Role checks (owner/editor/viewer) | ⬜ | `Principal.role` is carried; not enforced yet |
| Metering (`usage_events`) | 🟡 | `storage_bytes`, `transcribe_minutes` written (M1); LLM tokens in M3, render seconds in M4 |
| Quotas / rate limits (`QuotaService`) | 🟡 | `AllowAllQuota`; transcribe stage already calls `check()` |
| Billing | ⬜ | Intentionally absent; `usage_events` is the contract |
| Job isolation: per-priority queues | ✅ | ADR-0002 |
| Per-workspace concurrency limit | 🟡 | Config stub `per_workspace_max_concurrent_jobs` |
| GPU workers scale separately | 🟡 | `worker-gpu` profile on `transcribe*` queues |
| Storage keys workspace-prefixed | ✅ | `BlobKey` enforces `ws/<uuid>/` |
| S3 adapter | ⬜ | `BlobStore` port ready |
| Encrypted per-workspace secrets | 🟡 | `publish_targets.secret_ref` column; `SecretBox` port planned for M6 |
| Audit log | ✅ | Refusals, overrides, source add/update/retire |
| Takedown flow | ✅ | `POST /sources/{id}/retire` / `clipforge sources retire`; override can't bypass it |
| ToS/DMCA process documented | ⬜ | Before any public use |
| Per-workspace cost attribution | 🟡 | `usage_events.workspace_id`; report in M3 |
| Versioned scoring profiles & prompts | 🟡 | Immutable `(name, version)` rows; PROMPTS.md versions in M3 |
| API versioned `/api/v1` | ✅ | |
| Pagination | ✅ | limit/offset on list endpoints |
| Idempotency keys on mutating endpoints | 🟡 | `POST /videos` is naturally idempotent (upsert by platform+id); `Idempotency-Key` middleware still TODO |
