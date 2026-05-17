from app.crawl_jobs import router as crawl_jobs_router
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from pathlib import Path
from typing import Optional

import json
import os
import shutil
import sys
import zipfile
from app.evidence_jobs import router as evidence_jobs_router
from app.parity_jobs import router as parity_jobs_router
from app.parity_safe_jobs import router as parity_safe_jobs_router
from app.parity_parallel_jobs import router as parity_parallel_jobs_router
from app.bodhi_compact import router as bodhi_compact_router


app = FastAPI(title="AI Visibility Evidence Service")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/evidence-runs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_SEED_TOKEN = os.getenv("ADMIN_SEED_TOKEN", "ad6878sd8d87sd87")

REQUIRED_FILES = {
    "audit_context.json": [
        "outputs/audit_context/audit_context.json",
        "inputs/audit_context.json"
    ],
    "evidence_scope.json": [
        "outputs/evidence_scope/evidence_scope.json"
    ],
    "google_ai_mode_compact.json": [
        "outputs/google_ai_mode/google_ai_mode_compact.json"
    ],
    "owned_pages_full.json": [
        "outputs/content_intelligence/owned_pages_full.json"
    ],
    "external_pages_full.json": [
        "outputs/external_pages/external_pages_full.json"
    ],
    "visibility_matrix.json": [
        "outputs/visibility/visibility_matrix.json"
    ],
    "source_classification.json": [
        "outputs/source_landscape/source_classification.json"
    ]
}


def normalise_key(value: str) -> str:
    return value.lower().replace(" ", "_")


def read_json_file(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def find_first(root: Path, candidates: list[str]) -> Optional[Path]:
    for rel in candidates:
        candidate = root / rel
        if candidate.exists():
            return candidate
    return None


def seed_from_zip(zip_path: Path, brand: str, market: str, run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    run_dir = DATA_DIR / run_id
    latest_dir = DATA_DIR / "latest"
    tmp_dir = DATA_DIR / "_tmp_seed_extract"

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp_dir)

    possible_roots = [tmp_dir] + [p for p in tmp_dir.iterdir() if p.is_dir()]
    root = None

    for candidate in possible_roots:
        if (candidate / "outputs").exists() or (candidate / "inputs").exists():
            root = candidate
            break

    if root is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise FileNotFoundError("Could not find outputs/ or inputs/ inside uploaded zip")

    run_dir.mkdir(parents=True, exist_ok=True)

    copied = {}
    missing = []

    for output_name, candidates in REQUIRED_FILES.items():
        src = find_first(root, candidates)
        if src:
            dest = run_dir / output_name
            shutil.copyfile(src, dest)
            copied[output_name] = str(dest)
        else:
            missing.append(output_name)

    compact_bundle = {
        "status": "ready" if not missing else "partial",
        "run_id": run_id,
        "brand": brand,
        "market": market,
        "files": {
            "audit_context": read_json_file(run_dir / "audit_context.json", {}),
            "evidence_scope": read_json_file(run_dir / "evidence_scope.json", {}),
            "google_ai_mode_compact": read_json_file(run_dir / "google_ai_mode_compact.json", {}),
            "owned_pages_full": read_json_file(run_dir / "owned_pages_full.json", {}),
            "external_pages_full": read_json_file(run_dir / "external_pages_full.json", {}),
            "visibility_matrix": read_json_file(run_dir / "visibility_matrix.json", {}),
            "source_classification": read_json_file(run_dir / "source_classification.json", {})
        },
        "counts": {
            "missing_required_files": len(missing)
        },
        "missing_files": missing
    }

    compact_path = run_dir / "compact_bundle.json"
    compact_path.write_text(
        json.dumps(compact_bundle, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    manifest = {
        "run_id": run_id,
        "brand": brand,
        "market": market,
        "status": compact_bundle["status"],
        "copied_files": copied,
        "missing_files": missing,
        "compact_bundle": str(compact_path)
    }

    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_key = f"{normalise_key(brand)}_{normalise_key(market)}.json"
    (latest_dir / latest_key).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return manifest


@app.post("/admin/seed-run")
async def seed_run(
    file: UploadFile = File(...),
    brand: str = Form("Nissan"),
    market: str = Form("Japan"),
    run_id: str = Form("nissan_japan_demo_v1"),
    x_admin_token: Optional[str] = Header(None)
):
    if ADMIN_SEED_TOKEN and x_admin_token != ADMIN_SEED_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    upload_dir = DATA_DIR / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    zip_path = upload_dir / f"{run_id}.zip"

    with zip_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    try:
        manifest = seed_from_zip(zip_path, brand=brand, market=market, run_id=run_id)
    finally:
        zip_path.unlink(missing_ok=True)

    return {
        "status": "seeded",
        "manifest": manifest
    }

@app.get("/runs/latest")
def get_latest_run(brand: str, market: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    key = f"{brand.lower()}_{market.lower()}".replace(" ", "_")
    latest_path = DATA_DIR / "latest" / f"{key}.json"

    if not latest_path.exists():
        return {
            "status": "not_found",
            "message": "No latest run found for this brand/market",
            "brand": brand,
            "market": market,
            "expected_path": str(latest_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(latest_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/compact")
def get_compact_run(run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    compact_path = DATA_DIR / run_id / "compact_bundle.json"

    if not compact_path.exists():
        return {
            "status": "not_found",
            "message": "No compact bundle found for this run_id",
            "run_id": run_id,
            "expected_path": str(compact_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(compact_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/manifest")
def get_run_manifest(run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    manifest_path = DATA_DIR / run_id / "run_manifest.json"

    if not manifest_path.exists():
        return {
            "status": "not_found",
            "message": "No manifest found for this run_id",
            "run_id": run_id,
            "expected_path": str(manifest_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/debug/routes")
def debug_routes():
    return {
        "routes": [
            {
                "path": route.path,
                "methods": sorted(list(route.methods or []))
            }
            for route in app.routes
        ]
    }

@app.get("/health")
def health():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    return {
        "status": "ok",
        "service": "ai-visibility-evidence-service",
        "python": sys.version,
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "data_dir_is_dir": DATA_DIR.is_dir(),
        "volume_root_exists": Path("/data").exists(),
        "volume_root_is_dir": Path("/data").is_dir(),
        "port_env": os.getenv("PORT")
    }

app.include_router(crawl_jobs_router)

app.include_router(evidence_jobs_router)

app.include_router(parity_jobs_router)

app.include_router(parity_safe_jobs_router)

app.include_router(parity_parallel_jobs_router)

app.include_router(bodhi_compact_router)


# -----------------------------------------------------------------------------
# Query-workbench report-bundle API
# Locked orchestration: query -> top 3 owned URLs -> top 3 external citations ->
# winning patterns -> CMS/PR recommendations -> rerun deltas.
# -----------------------------------------------------------------------------

def _bundle_latest_key(brand: str, market: str) -> str:
    return f"{normalise_key(brand)}_{normalise_key(market)}.json"


def _report_bundle_path(run_id: str) -> Path:
    return DATA_DIR / run_id / "frontend_report_bundle.json"


def _read_report_bundle(run_id: str):
    path = _report_bundle_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_report_bundle(bundle: dict, run_id: str, brand: str, market: str):
    run_dir = DATA_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    bundle.setdefault("run_id", run_id)
    bundle.setdefault("brand", brand)
    bundle.setdefault("market", market)
    bundle.setdefault("schema_version", "query_workbench.v1")
    (_report_bundle_path(run_id)).write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "brand": brand,
        "market": market,
        "status": "ready",
        "report_bundle": str(_report_bundle_path(run_id)),
        "schema_version": bundle.get("schema_version"),
        "query_count": len(bundle.get("query_workbench") or []),
    }
    (run_dir / "report_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_dir = DATA_DIR / "latest_report_bundles"
    latest_dir.mkdir(parents=True, exist_ok=True)
    (latest_dir / _bundle_latest_key(brand, market)).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _find_json_in_extracted(root: Path, names: list[str]) -> Optional[Path]:
    for name in names:
        direct = root / name
        if direct.exists():
            return direct
    for p in root.rglob("*.json"):
        if p.name in {Path(n).name for n in names}:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and ("query_workbench" in data or "frontend_report_bundle" in data):
                    return p
            except Exception:
                pass
    return None


@app.post("/admin/seed-report-bundle")
async def seed_report_bundle(
    file: UploadFile = File(...),
    brand: str = Form("Nissan"),
    market: str = Form("Japan"),
    run_id: str = Form(""),
    x_admin_token: Optional[str] = Header(None)
):
    if ADMIN_SEED_TOKEN and x_admin_token != ADMIN_SEED_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")
    run_id = run_id or f"{normalise_key(brand)}_{normalise_key(market)}_report_bundle"
    upload_dir = DATA_DIR / "_report_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload.json").suffix.lower()
    upload_path = upload_dir / f"{run_id}{suffix or '.json'}"
    with upload_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    tmp_dir = upload_dir / f"{run_id}_extract"
    try:
        if suffix == ".zip":
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(upload_path, "r") as zf:
                zf.extractall(tmp_dir)
            json_path = _find_json_in_extracted(tmp_dir, ["outputs/frontend_report_bundle.json", "frontend_report_bundle.json", "preview_node_bundle.json"])
            if not json_path:
                raise HTTPException(status_code=400, detail="No frontend_report_bundle.json found in zip")
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        else:
            payload = json.loads(upload_path.read_text(encoding="utf-8"))
        bundle = payload.get("frontend_report_bundle") if isinstance(payload, dict) and isinstance(payload.get("frontend_report_bundle"), dict) else payload
        if not isinstance(bundle, dict) or not isinstance(bundle.get("query_workbench"), list):
            raise HTTPException(status_code=400, detail="Report bundle must contain query_workbench[]")
        manifest = _write_report_bundle(bundle, run_id=run_id, brand=brand, market=market)
        return {"status": "seeded", "manifest": manifest}
    finally:
        upload_path.unlink(missing_ok=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/runs/{run_id}/report-bundle")
def get_report_bundle(run_id: str):
    bundle = _read_report_bundle(run_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No frontend report bundle found for this run_id")
    return bundle


@app.get("/runs/latest/report-bundle")
def get_latest_report_bundle(brand: str, market: str):
    manifest_path = DATA_DIR / "latest_report_bundles" / _bundle_latest_key(brand, market)
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="No latest report bundle found for this brand/market")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle = _read_report_bundle(manifest["run_id"])
    if bundle is None:
        raise HTTPException(status_code=404, detail="Latest report manifest exists but bundle is missing")
    return bundle


@app.get("/runs/{run_id}/query-workbench")
def get_query_workbench(run_id: str):
    bundle = _read_report_bundle(run_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No frontend report bundle found for this run_id")
    return {"run_id": run_id, "query_workbench": bundle.get("query_workbench", [])}


@app.get("/runs/{run_id}/history")
def get_run_history(run_id: str):
    bundle = _read_report_bundle(run_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No frontend report bundle found for this run_id")
    return {"run_id": run_id, "history": bundle.get("run_history", [])}


@app.get("/runs/{run_id}/compare")
def compare_runs(run_id: str, baseline_run_id: str):
    current = _read_report_bundle(run_id)
    baseline = _read_report_bundle(baseline_run_id)
    if current is None or baseline is None:
        raise HTTPException(status_code=404, detail="Both current and baseline report bundles are required")
    base_by_q = {q.get("query_id"): q for q in baseline.get("query_workbench", [])}
    deltas = []
    for q in current.get("query_workbench", []):
        b = base_by_q.get(q.get("query_id"))
        if not b:
            continue
        cur_vis = (q.get("current_ai_visibility") or {}).get("score", 0)
        base_vis = (b.get("current_ai_visibility") or {}).get("score", 0)
        cur_citations = [c.get("url") for c in (q.get("current_ai_visibility") or {}).get("top_citations", [])]
        base_citations = [c.get("url") for c in (b.get("current_ai_visibility") or {}).get("top_citations", [])]
        deltas.append({
            "query_id": q.get("query_id"),
            "query": q.get("query"),
            "visibility_score_delta": cur_vis - base_vis,
            "new_top_citations": [u for u in cur_citations[:3] if u and u not in base_citations[:3]],
            "owned_target_citation_changed": (q.get("current_ai_visibility") or {}).get("owned_target_cited") != (b.get("current_ai_visibility") or {}).get("owned_target_cited"),
        })
    return {"baseline_run_id": baseline_run_id, "run_id": run_id, "deltas": deltas}
