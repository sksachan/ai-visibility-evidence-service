# AI Visibility Evidence Service

FastAPI service for serving AI Search Visibility evidence snapshots to Bodhi.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
## Health check

curl http://localhost:8000/health

## Seed latest run

python scripts/seed_latest_run.py \
  --zip /path/to/local_ai_visibility_success_run.zip \
  --brand Nissan \
  --market Japan \
  --run-id nissan_japan_demo_v1 \
  --data-dir ./data/evidence-runs


## Query-workbench report-bundle API

Locked orchestration strategy: `query -> top 3 owned URLs -> top 3 external citations -> winning patterns -> CMS/PR recommendations -> rerun delta -> refreshed recommendations`.

New endpoints:

- `POST /admin/seed-report-bundle` uploads a canonical `frontend_report_bundle.json` or zip containing it.
- `GET /runs/{run_id}/report-bundle` returns the canonical dashboard bundle.
- `GET /runs/latest/report-bundle?brand=Nissan&market=Japan` returns the latest bundle for a brand/market.
- `GET /runs/{run_id}/query-workbench` returns the query workbench only.
- `GET /runs/{run_id}/compare?baseline_run_id=...` returns query-level deltas.

The frontend should prefer `/runs/latest/report-bundle` over compact Bodhi files once this service is connected.
