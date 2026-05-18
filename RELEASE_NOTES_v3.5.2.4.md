# Evidence Service v3.5.2.4 — Storage Guardrails and Cleanup Safety

This release hardens Railway volume management after a full-refresh run exhausted the 500 MB volume and an admin cleanup deleted protected state.

## Changes

- Adds `GET /admin/storage` for volume diagnostics:
  - disk usage
  - run directory sizes
  - largest runs
  - largest files under the data directory
  - protected system directories

- Hardens `POST /admin/cleanup-runs`:
  - supports both `keep_run_ids` and `preserve_run_ids`
  - always protects `run_status`, `latest_successful`, `portfolios`, and `_jobs`
  - refuses to delete `_jobs` unless `force=true`
  - returns disk usage after cleanup
  - includes per-candidate size in dry-run and delete results

- Adds compact-only crawl persistence for the lightweight crawl job:
  - caps downloaded page bytes via `CRAWL_MAX_READ_BYTES` (default `1200000`)
  - caps persisted text via `CRAWL_TEXT_MAX_CHARS` (default `5000`)
  - caps persisted markdown via `CRAWL_MARKDOWN_MAX_CHARS` (default `5000`)
  - stores `content_extract` rather than full raw page text
  - records JSON-LD presence, JSON-LD block count, schema types, canonical URL, and meta description

## Operational note

Run `/admin/storage` before destructive cleanup. Use `dry_run=true` first. The endpoint now protects system folders by default, but destructive cleanup should still be used cautiously.
