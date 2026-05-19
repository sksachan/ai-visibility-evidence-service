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
