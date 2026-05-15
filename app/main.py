from fastapi import FastAPI
from pathlib import Path
import json
import os

app = FastAPI(title="AI Visibility Evidence Service")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/evidence-runs"))


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "ai-visibility-evidence-service",
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists()
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
    run_dir = DATA_DIR / run_id
    compact_path = run_dir / "compact_bundle.json"

    if not compact_path.exists():
        return {
            "status": "not_found",
            "message": "No compact bundle found for this run_id",
            "run_id": run_id,
            "expected_path": str(compact_path)
        }

    return json.loads(compact_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/files/{file_name}")
def get_run_file(run_id: str, file_name: str):
    allowed_files = {
        "run_manifest.json",
        "audit_context.json",
        "evidence_scope.json",
        "google_ai_mode_compact.json",
        "owned_pages_full.json",
        "external_pages_full.json",
        "visibility_matrix.json",
        "source_classification.json",
        "compact_bundle.json"
    }

    if file_name not in allowed_files:
        return {
            "status": "blocked",
            "message": "Requested file is not in the allowed evidence file list"
        }

    file_path = DATA_DIR / run_id / file_name

    if not file_path.exists():
        return {
            "status": "not_found",
            "run_id": run_id,
            "file_name": file_name,
            "expected_path": str(file_path)
        }

    return json.loads(file_path.read_text(encoding="utf-8"))
