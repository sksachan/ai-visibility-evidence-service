# AI Visibility Evidence Service v3.3

Railway evidence execution layer for the AI Brand Visibility dashboard.

## Core responsibilities

- Store latest successful `frontend_report_bundle` for dashboard loading.
- Track refresh run status separately from latest successful report data.
- Store synthetic/manual query portfolios.
- Execute evidence refresh stages owned by Railway: sitemap inventory, query-owned URL mapping seed, SerpAPI collection, owned URL crawl, external citation crawl.
- Trigger Bodhi Brand Topic Query Builder when `query_portfolio_mode=synthetic` and no `query_portfolio_id` is supplied.
- Optionally trigger Bodhi Auditor after evidence collection.

## Required Railway start command

```bash
python start.py
```

Do not use `$PORT` directly in the Railway Start Command. `start.py` reads and validates `PORT`.

## Environment variables

Required for app runtime:

```text
DATA_DIR=/data/evidence-runs
PUBLIC_EVIDENCE_SERVICE_URL=https://ai-visibility-evidence-service-production.up.railway.app
```

Required for synthetic portfolio orchestration:

```text
BODHI_API_BASE_URL=https://psaisuite.com/save
BODHI_PAT_TOKEN=pat_<token>
BODHI_PORTFOLIO_TASK_ID=<BrandTopicQueryBuilder task id>
BODHI_PORTFOLIO_WORKFLOW_ID=<optional workflow id>
```

Required if the service should trigger the Auditor after evidence refresh:

```text
BODHI_AUDITOR_TASK_ID=<Auditor task id>
BODHI_AUDITOR_WORKFLOW_ID=<optional workflow id>
```

Required only for live AI citation collection:

```text
SERPAPI_KEY=<serpapi key>
SERPAPI_ENGINE=google_ai_mode
```

Optional:

```text
BODHI_PORTFOLIO_TIMEOUT_SECONDS=900
BODHI_POLL_SECONDS=10
BODHI_HTTP_TIMEOUT_SECONDS=120
ADMIN_TOKEN=<optional write protection token>
```

## Important routes

```text
GET  /health
GET  /debug/routes
POST /refresh/evidence
GET  /runs/status?brand=Nissan&market=Japan
POST /runs/{run_id}/report-bundle
GET  /runs/latest/report-bundle?brand=Nissan&market=Japan
POST /portfolios
GET  /portfolios/{portfolio_id}
GET  /portfolios/latest?brand=Nissan&market=Japan
```

## Synthetic refresh flow

`POST /refresh/evidence` with:

```json
{
  "brand": "Nissan",
  "market": "Japan",
  "domain": "https://www.nissan.co.jp",
  "query_portfolio_mode": "synthetic",
  "query_portfolio_id": "",
  "topic_count": 8,
  "queries_per_topic": 6,
  "query_limit": 50,
  "max_owned_pages_per_query": 3,
  "run_serpapi": false,
  "crawl_owned": true,
  "crawl_external": false,
  "trigger_auditor": true
}
```

The service returns immediately with a `target_run_id`. Poll:

```text
GET /runs/{target_run_id}/status
GET /runs/status?brand=Nissan&market=Japan
```

The dashboard should keep using `/runs/latest/report-bundle` until a new successful report bundle is stored.
