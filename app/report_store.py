from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict

try:
    from app.evidence_jobs import FullRefreshRequest, run_full_refresh, make_job_id, update_job
except Exception:  # pragma: no cover
    FullRefreshRequest = None  # type: ignore
    run_full_refresh = None  # type: ignore
    make_job_id = None  # type: ignore
    update_job = None  # type: ignore

router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

SUCCESS_STATES = {"success", "successful", "completed", "succeeded", "ready"}
IN_PROGRESS_STATES = {"queued", "accepted", "pending", "running", "in_progress", "processing"}
FAILED_STATES = {"failed", "error", "cancelled", "canceled"}


def now_epoch() -> int:
    return int(time.time())


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def normalise_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("https://", "").replace("http://", "").replace("/", "_").replace(" ", "_").replace(":", "_")


def safe_brand_market_domain(brand: str | None, market: str | None, domain: str | None = None) -> str:
    parts = [normalise_key(brand or "unknown_brand"), normalise_key(market or "unknown_market")]
    if domain:
        parts.append(normalise_key(domain))
    return "__".join(parts)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_dir(run_id: str) -> Path:
    return DATA_DIR / run_id


def latest_index_dir() -> Path:
    return DATA_DIR / "latest_successful"


def status_dir() -> Path:
    return DATA_DIR / "run_status"


def portfolio_dir() -> Path:
    return DATA_DIR / "portfolios"


def extract_bundle_metadata(bundle: dict[str, Any], fallback_run_id: str | None = None) -> dict[str, Any]:
    meta = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
    executive = bundle.get("executive") if isinstance(bundle.get("executive"), dict) else {}
    return {
        "run_id": bundle.get("run_id") or meta.get("run_id") or fallback_run_id,
        "brand": bundle.get("brand") or meta.get("brand") or executive.get("brand"),
        "market": bundle.get("market") or meta.get("market") or executive.get("market"),
        "domain": bundle.get("domain") or meta.get("domain"),
        "schema_version": bundle.get("schema_version"),
        "contract_version": bundle.get("contract_version"),
    }


def update_latest_successful_index(manifest: dict[str, Any]) -> None:
    brand = manifest.get("brand")
    market = manifest.get("market")
    domain = manifest.get("domain")
    if not brand or not market:
        return
    latest_index_dir().mkdir(parents=True, exist_ok=True)
    keys = [
        safe_brand_market_domain(brand, market),
    ]
    if domain:
        keys.append(safe_brand_market_domain(brand, market, domain))
    for key in keys:
        write_json(latest_index_dir() / f"{key}.json", manifest)


def write_run_status(run_id: str, status: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = read_json(status_dir() / f"{run_id}.json", {}) or {}
    current.update(patch or {})
    current["run_id"] = run_id
    current["status"] = status
    current["updated_at_epoch"] = now_epoch()
    if status in SUCCESS_STATES:
        current.setdefault("completed_at_epoch", now_epoch())
    elif status in FAILED_STATES:
        current.setdefault("failed_at_epoch", now_epoch())
    elif status in IN_PROGRESS_STATES:
        current.setdefault("started_at_epoch", now_epoch())
    write_json(status_dir() / f"{run_id}.json", current)
    # Keep a copy inside the run folder when a run_id directory exists.
    if run_dir(run_id).exists():
        write_json(run_dir(run_id) / "run_status.json", current)
    return current


def load_run_status(run_id: str) -> dict[str, Any] | None:
    status = read_json(status_dir() / f"{run_id}.json")
    if status:
        return status
    return read_json(run_dir(run_id) / "run_status.json")


def report_bundle_path(run_id: str) -> Path:
    return run_dir(run_id) / "frontend_report_bundle.json"


def load_report_bundle(run_id: str) -> dict[str, Any] | None:
    return read_json(report_bundle_path(run_id))


def scan_latest_successful(brand: str | None, market: str | None, domain: str | None = None) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for child in DATA_DIR.iterdir() if DATA_DIR.exists() else []:
        if not child.is_dir() or child.name.startswith("_") or child.name in {"latest", "latest_successful", "run_status", "portfolios"}:
            continue
        manifest = read_json(child / "report_manifest.json") or read_json(child / "run_manifest.json") or {}
        status = (manifest.get("status") or "").lower()
        if status not in SUCCESS_STATES:
            continue
        if brand and normalise_key(manifest.get("brand")) != normalise_key(brand):
            continue
        if market and normalise_key(manifest.get("market")) != normalise_key(market):
            continue
        if domain and manifest.get("domain") and normalise_key(manifest.get("domain")) != normalise_key(domain):
            continue
        if not report_bundle_path(child.name).exists():
            continue
        candidates.append(manifest)
    candidates.sort(key=lambda x: x.get("completed_at_epoch") or x.get("created_at_epoch") or 0, reverse=True)
    return candidates[0] if candidates else None


class RunStatusRequest(BaseModel):
    status: str
    brand: str | None = None
    market: str | None = None
    domain: str | None = None
    task_id: str | None = None
    bodhi_run_id: str | None = None
    evidence_run_id: str | None = None
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    portfolio_id: str | None = None
    schema_version: str | None = "brand_topic_query_portfolio.v1"
    deepresearch_status: str | None = None
    brand: str
    market: str
    domain: str | None = None
    portfolio_source: str = "synthetic_deepresearch"
    topics: list[Any] = Field(default_factory=list)
    queries: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RefreshEvidenceRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    domain: str | None = None
    evidence_service_url: str | None = None
    source_run_id: str | None = None
    target_run_id: str | None = None
    mode: str = "refresh_owned_pages"
    run_mode: str | None = None

    # Query portfolio orchestration. Synthetic mode is handled by Railway evidence
    # service through the Bodhi Brand Topic Query Builder task.
    query_portfolio_mode: str = "reuse"
    query_portfolio_id: str | None = None
    manual_queries_json: str | None = None
    topics_json: str | None = None
    seed_topics: str | None = None
    topic_count: int = 8
    queries_per_topic: int = 6
    language: str = "English"
    portfolio_goal: str | None = None

    # Sitemap / mapping controls.
    sitemap_url: str | None = None
    sitemap_max_urls: int = 2000
    query_limit: int = 50
    max_owned_pages_per_query: int = 3
    max_external_citations_per_query: int = 3
    max_owned_urls: int = 60
    max_external_urls: int = 30

    # Evidence execution flags owned by Railway evidence service.
    crawl_owned: bool = True
    crawl_external: bool = False
    enable_owned_crawl: bool | None = None
    enable_external_crawl: bool | None = None
    run_serpapi: bool = False
    enable_serpapi: bool | None = None
    use_existing_google_ai_mode: bool = True
    trigger_auditor: bool = True

    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/runs/{run_id}/report-bundle")
async def store_report_bundle(run_id: str, request: Request, x_admin_token: str | None = Header(default=None)):
    # Report writes can be protected by ADMIN_TOKEN. If ADMIN_TOKEN is unset, local/dev mode remains open.
    require_admin(x_admin_token)
    bundle = await request.json()
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=400, detail="Report bundle must be a JSON object")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rdir = run_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    write_json(report_bundle_path(run_id), bundle)

    meta = extract_bundle_metadata(bundle, fallback_run_id=run_id)
    manifest = {
        "status": "completed",
        "run_id": run_id,
        "brand": meta.get("brand"),
        "market": meta.get("market"),
        "domain": meta.get("domain"),
        "schema_version": meta.get("schema_version"),
        "contract_version": meta.get("contract_version"),
        "report_bundle": str(report_bundle_path(run_id)),
        "created_at_epoch": now_epoch(),
        "completed_at_epoch": now_epoch(),
    }
    write_json(rdir / "report_manifest.json", manifest)
    write_json(rdir / "run_manifest.json", {**(read_json(rdir / "run_manifest.json", {}) or {}), **manifest})
    write_run_status(run_id, "completed", manifest)
    update_latest_successful_index(manifest)
    return {"status": "stored", "manifest": manifest}


@router.get("/runs/{run_id}/report-bundle")
def get_report_bundle(run_id: str):
    bundle = load_report_bundle(run_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"No frontend report bundle found for run_id={run_id}")
    return bundle


@router.get("/runs/latest/report-bundle")
def get_latest_report_bundle(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    key_candidates = []
    if domain:
        key_candidates.append(safe_brand_market_domain(brand, market, domain))
    key_candidates.append(safe_brand_market_domain(brand, market))

    manifest = None
    for key in key_candidates:
        manifest = read_json(latest_index_dir() / f"{key}.json")
        if manifest:
            break
    if not manifest:
        manifest = scan_latest_successful(brand, market, domain)
    if not manifest:
        raise HTTPException(status_code=404, detail="No latest successful report bundle found")

    bundle = load_report_bundle(manifest["run_id"])
    if not bundle:
        raise HTTPException(status_code=404, detail="Latest manifest exists but report bundle file is missing")
    return bundle


@router.get("/reports/latest-successful")
def get_latest_successful_report(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    return get_latest_report_bundle(brand=brand, market=market, domain=domain)


@router.get("/reports/{run_id}")
def get_report_by_run_id(run_id: str):
    return get_report_bundle(run_id)


@router.post("/runs/{run_id}/status")
def post_run_status(run_id: str, req: RunStatusRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    patch = req.model_dump(exclude={"status"})
    return write_run_status(run_id, req.status, patch)


@router.get("/runs/{run_id}/status")
def get_single_run_status(run_id: str):
    status = load_run_status(run_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"No status found for run_id={run_id}")
    return status


@router.get("/runs/status")
def get_run_statuses(brand: str | None = None, market: str | None = None, domain: str | None = None, limit: int = 20):
    rows: list[dict[str, Any]] = []
    if status_dir().exists():
        for path in status_dir().glob("*.json"):
            status = read_json(path, {}) or {}
            if brand and normalise_key(status.get("brand")) != normalise_key(brand):
                continue
            if market and normalise_key(status.get("market")) != normalise_key(market):
                continue
            if domain and status.get("domain") and normalise_key(status.get("domain")) != normalise_key(domain):
                continue
            rows.append(status)
    rows.sort(key=lambda x: x.get("updated_at_epoch") or x.get("created_at_epoch") or 0, reverse=True)
    latest_successful = scan_latest_successful(brand, market, domain)
    latest_active = next((r for r in rows if str(r.get("status", "")).lower() in IN_PROGRESS_STATES), None)
    return {
        "status": "ok",
        "latest_successful_run_id": (latest_successful or {}).get("run_id"),
        "active_run": latest_active,
        "runs": rows[: max(1, min(limit, 100))],
    }


@router.post("/portfolios")
def store_query_portfolio(req: PortfolioRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    portfolio_id = req.portfolio_id or f"portfolio_{normalise_key(req.brand)}_{normalise_key(req.market)}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    payload = req.model_dump(exclude_none=True)
    payload["portfolio_id"] = portfolio_id
    payload.setdefault("schema_version", "brand_topic_query_portfolio.v1")
    payload["created_at_epoch"] = now_epoch()
    write_json(portfolio_dir() / f"{portfolio_id}.json", payload)

    latest_key = safe_brand_market_domain(req.brand, req.market, req.domain)
    write_json(portfolio_dir() / "latest" / f"{latest_key}.json", payload)
    write_json(portfolio_dir() / "latest" / f"{safe_brand_market_domain(req.brand, req.market)}.json", payload)
    return {"status": "stored", "portfolio_id": portfolio_id, "portfolio": payload}


@router.get("/portfolios/latest")
def get_latest_query_portfolio(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    # IMPORTANT: this static route must be registered before /portfolios/{portfolio_id}.
    # Otherwise FastAPI treats "latest" as a portfolio_id and returns
    # "No portfolio found for portfolio_id=latest".
    keys = []
    if domain:
        keys.append(safe_brand_market_domain(brand, market, domain))
    keys.append(safe_brand_market_domain(brand, market))
    for key in keys:
        payload = read_json(portfolio_dir() / "latest" / f"{key}.json")
        if payload:
            return payload
    raise HTTPException(status_code=404, detail="No latest portfolio found")


@router.get("/portfolios/{portfolio_id}")
def get_query_portfolio(portfolio_id: str):
    payload = read_json(portfolio_dir() / f"{portfolio_id}.json")
    if not payload:
        raise HTTPException(status_code=404, detail=f"No portfolio found for portfolio_id={portfolio_id}")
    return payload


@router.post("/refresh/evidence")
def trigger_refresh_evidence(req: RefreshEvidenceRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    # Phase 2 orchestration lives in Railway evidence service. It can trigger the
    # Bodhi portfolio workflow, wait for completion, ingest the portfolio, run
    # SerpAPI/crawling, and optionally trigger the Bodhi Auditor workflow.
    from app.refresh_orchestrator import start_phase2_refresh

    payload = req.model_dump()
    result = start_phase2_refresh(payload)
    result["note"] = "Dashboard should keep showing latest successful report until this refresh run completes and Bodhi stores a new frontend_report_bundle."
    return result
