# AI Visibility Evidence Service v3

Railway service for AI Brand Visibility evidence storage, refresh orchestration and report-bundle retrieval.

## Responsibility boundary

The evidence service owns evidence acquisition and storage:

- SerpAPI / Google AI Mode collection
- Sitemap / URL inventory storage
- Owned URL crawling
- External citation URL crawling
- Evidence run status
- Latest successful report bundle retrieval
- Synthetic/manual query portfolio storage

The Bodhi Auditor workflow consumes evidence from this service, builds the canonical `frontend_report_bundle`, runs synthesis, and stores the completed report bundle back here.

## Key endpoints

### Health

`GET /health`

### Latest successful report bundle

`GET /runs/latest/report-bundle?brand=Nissan&market=Japan`

Alias:

`GET /reports/latest-successful?brand=Nissan&market=Japan`

These return only a completed successful report. They intentionally ignore in-progress or failed refreshes.

### Store/read report bundles

`POST /runs/{run_id}/report-bundle`

Stores the Bodhi-produced `frontend_report_bundle.json` and marks the run successful.

`GET /runs/{run_id}/report-bundle`

Reads a specific report bundle.

### Run status

`POST /runs/{run_id}/status`

`GET /runs/{run_id}/status`

`GET /runs/status?brand=Nissan&market=Japan`

The dashboard can poll this while continuing to display the latest successful report.

### Refresh evidence

`POST /refresh/evidence`

Creates a new evidence refresh run and starts the existing crawler flow where possible. The dashboard should not switch report data until a new successful Bodhi report bundle is stored.

### Query portfolio storage

`POST /portfolios`

`GET /portfolios/{portfolio_id}`

`GET /portfolios/latest?brand=Nissan&market=Japan`

The synthetic DeepResearch workflow should write its generated topic/query portfolio here. The Auditor workflow reads it by `query_portfolio_id`; the two Bodhi workflows do not need direct coupling.

### Existing compatibility endpoints

The v2 endpoints remain available, including:

- `GET /runs/{run_id}/bodhi-compact`
- `GET /runs/{run_id}/compact`
- `POST /admin/seed-run`
- `POST /jobs/full-refresh`
- `POST /jobs/collect-serpapi`

## Environment variables

Required/recommended:

```text
DATA_DIR=/data/evidence-runs
ADMIN_TOKEN=<server-side admin token>
SERPAPI_KEY=<optional; only needed when SerpAPI collection is enabled>
```

Keep `SERPAPI_KEY` in Railway only. Do not pass it to Bodhi.
