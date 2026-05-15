from fastapi import FastAPI
from pathlib import Path
import json
import os
import sys

app = FastAPI(title="AI Visibility Evidence Service")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/evidence-runs"))


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "ai-visibility-evidence-service"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "ai-visibility-evidence-service",
        "python": sys.version,
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "port_env": os.getenv("PORT")
    }


@app.get("/runs/latest")
def get_latest_run(brand: str, market: str):
    key = f"{brand.lower()}_{market.lower()}".replace(" ", "_")
    latest_path = DATA_DIR / "latest" / f"{key}.json"

    if not latest_path.exists():
        return {
            "status": "not_found",
            "message": "No latest run found for this brand/market",
            "brand": brand,
            "market": market,
            "expected_path": str(latest_path)
        }

    return json.loads(latest_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/compact")
def get_compact_run(run_id: str):
    compact_path = DATA_DIR / run_id / "compact_bundle.json"

    if not compact_path.exists():
        return {
            "status": "not_found",
            "message": "No compact bundle found for this run_id",
            "run_id": run_id,
            "expected_path": str(compact_path)
        }

    return json.loads(compact_path.read_text(encoding="utf-8"))