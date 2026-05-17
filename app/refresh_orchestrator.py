from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from app.bodhi_client import BodhiClient
from app.evidence_jobs import FullRefreshRequest, SerpApiJobRequest, run_full_refresh, run_serpapi_collection, make_job_id, update_job
from app.portfolio_ingestion import extract_query_portfolio

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))


def now_epoch() -> int:
    return int(time.time())


def normalise_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("https://", "").replace("http://", "").replace("/", "_").replace(" ", "_").replace(":", "_")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def status_dir() -> Path:
    return DATA_DIR / "run_status"


def run_dir(run_id: str) -> Path:
    return DATA_DIR / run_id


def portfolio_dir() -> Path:
    return DATA_DIR / "portfolios"


def write_run_status(run_id: str, status: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = read_json(status_dir() / f"{run_id}.json", {}) or {}
    current.update(patch or {})
    current["run_id"] = run_id
    current["status"] = status
    current["updated_at_epoch"] = now_epoch()
    write_json(status_dir() / f"{run_id}.json", current)
    if run_dir(run_id).exists():
        write_json(run_dir(run_id) / "run_status.json", current)
    return current


def store_portfolio(portfolio: dict[str, Any], fallback_brand: str, fallback_market: str, fallback_domain: str | None = None) -> dict[str, Any]:
    pid = portfolio.get("portfolio_id") or f"portfolio_{normalise_key(fallback_brand)}_{normalise_key(fallback_market)}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    portfolio["portfolio_id"] = pid
    portfolio.setdefault("brand", fallback_brand)
    portfolio.setdefault("market", fallback_market)
    if fallback_domain and not portfolio.get("domain"):
        portfolio["domain"] = fallback_domain
    portfolio.setdefault("created_at_epoch", now_epoch())
    write_json(portfolio_dir() / f"{pid}.json", portfolio)
    for key in [
        f"{normalise_key(portfolio.get('brand'))}__{normalise_key(portfolio.get('market'))}",
        f"{normalise_key(portfolio.get('brand'))}__{normalise_key(portfolio.get('market'))}__{normalise_key(portfolio.get('domain'))}",
    ]:
        write_json(portfolio_dir() / "latest" / f"{key}.json", portfolio)
    return portfolio


def load_portfolio(portfolio_id: str) -> dict[str, Any] | None:
    return read_json(portfolio_dir() / f"{portfolio_id}.json")


def trigger_and_wait_for_portfolio(req: dict[str, Any], target_run_id: str) -> dict[str, Any]:
    task_id = os.getenv("BODHI_PORTFOLIO_TASK_ID", "")
    workflow_id = os.getenv("BODHI_PORTFOLIO_WORKFLOW_ID", "")
    if not task_id:
        raise RuntimeError("BODHI_PORTFOLIO_TASK_ID is not set. Cannot generate synthetic portfolio.")
    client = BodhiClient()
    if not client.enabled:
        raise RuntimeError("BODHI_PAT_TOKEN is not set. Cannot trigger Bodhi portfolio task.")

    inputs = {
        "brand": req.get("brand"),
        "market": req.get("market"),
        "domain": req.get("domain"),
        "evidence_service_url": os.getenv("PUBLIC_EVIDENCE_SERVICE_URL") or req.get("evidence_service_url") or "",
        "portfolio_id": req.get("query_portfolio_id") or "",
        "seed_topics": req.get("seed_topics") or "",
        "topic_count": req.get("topic_count") or 8,
        "queries_per_topic": req.get("queries_per_topic") or 6,
        "language": req.get("language") or "English",
        "portfolio_goal": req.get("portfolio_goal") or "AI answer visibility audit query portfolio.",
    }
    write_run_status(target_run_id, "running", {"stage": "portfolio_generation_queued", "portfolio_task_id": task_id})
    trigger = client.trigger_task_run(
        task_id=task_id,
        workflow_id=workflow_id or None,
        run_name=f"Portfolio generation - {req.get('brand')} {req.get('market')} - {target_run_id}",
        inputs=inputs,
    )
    bodhi_run_id = client.extract_run_id(trigger)
    if not bodhi_run_id:
        raise RuntimeError(f"Could not determine Bodhi portfolio run id from response: {str(trigger)[:500]}")
    write_run_status(target_run_id, "running", {"stage": "portfolio_generation_running", "portfolio_bodhi_run_id": bodhi_run_id})

    timeout = int(os.getenv("BODHI_PORTFOLIO_TIMEOUT_SECONDS", "900"))
    poll = int(os.getenv("BODHI_POLL_SECONDS", "10"))
    client.wait_for_run(task_id, bodhi_run_id, timeout_seconds=timeout, poll_seconds=poll)

    # Try known file outputs first, then outputs.json.
    candidates = [
        "outputs.json", "output.json", "outputs/query_portfolio.json", "query_portfolio.json",
        "outputs/frontend_report_bundle.json", "result.json",
    ]
    errors: list[str] = []
    for src in candidates:
        try:
            payload = client.get_run_file(bodhi_run_id, src)
            portfolio = extract_query_portfolio(payload)
            if portfolio:
                portfolio.setdefault("metadata", {})["bodhi_portfolio_run_id"] = bodhi_run_id
                stored = store_portfolio(portfolio, req.get("brand", ""), req.get("market", ""), req.get("domain"))
                write_run_status(target_run_id, "running", {
                    "stage": "portfolio_generation_completed",
                    "query_portfolio_id": stored.get("portfolio_id"),
                    "portfolio_query_count": len(stored.get("queries") or []),
                    "portfolio_topic_count": len(stored.get("topics") or []),
                })
                return stored
        except Exception as e:
            errors.append(f"{src}: {str(e)[:180]}")
    raise RuntimeError("Portfolio run completed but no valid portfolio was found. " + "; ".join(errors[:5]))


def fetch_sitemap_urls(domain: str | None, sitemap_url: str | None, max_urls: int = 2000) -> list[str]:
    if not sitemap_url and domain:
        sitemap_url = domain.rstrip("/") + "/sitemap.xml"
    if not sitemap_url:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    to_fetch = [sitemap_url]
    headers = {"User-Agent": "ai-visibility-evidence-service/3.3"}
    while to_fetch and len(urls) < max_urls:
        current = to_fetch.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            resp = requests.get(current, timeout=45, headers=headers)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            locs = [el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
            for loc in locs:
                if loc.lower().endswith(".xml") and len(seen) < 50:
                    to_fetch.append(loc)
                elif loc.startswith(("http://", "https://")):
                    urls.append(loc)
                    if len(urls) >= max_urls:
                        break
        except Exception:
            break
    # dedupe preserving order
    out = []
    seen2 = set()
    for u in urls:
        if u not in seen2:
            seen2.add(u)
            out.append(u)
    return out


def tokenise(value: Any) -> set[str]:
    text = str(value or "").lower()
    return {t for t in re.findall(r"[a-z0-9ぁ-んァ-ン一-龥]+", text) if len(t) >= 3}


def map_queries_to_sitemap(portfolio: dict[str, Any], urls: list[str], max_per_query: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    mappings: list[dict[str, Any]] = []
    selected: list[str] = []
    url_tokens = [(u, tokenise(u.replace("-", " ").replace("_", " ").replace("/", " "))) for u in urls]
    for q in (portfolio.get("queries") or []):
        qtext = " ".join([str(q.get("query") or ""), str(q.get("topic") or ""), str(q.get("recommended_page_type") or "")])
        qt = tokenise(qtext)
        ranked = []
        for u, ut in url_tokens:
            score = len(qt & ut)
            # light boosts for common page intent terms
            lower = u.lower()
            if "ev" in qtext.lower() and ("ev" in lower or "ariya" in lower or "leaf" in lower): score += 3
            if "service" in qtext.lower() and ("service" in lower or "support" in lower): score += 3
            if "dealer" in qtext.lower() and ("dealer" in lower or "shop" in lower): score += 3
            if "safety" in qtext.lower() and ("safety" in lower): score += 3
            if "finance" in qtext.lower() and ("finance" in lower or "price" in lower): score += 3
            ranked.append((score, u))
        ranked.sort(key=lambda x: (-x[0], len(x[1])))
        top = [u for score, u in ranked[:max_per_query] if score > 0] or [u for _, u in ranked[:max_per_query]]
        for idx, u in enumerate(top, start=1):
            mappings.append({
                "query_id": q.get("query_id"),
                "query": q.get("query"),
                "rank": idx,
                "url": u,
                "mapping_score": next((score for score, ru in ranked if ru == u), 0),
                "mapping_reason": "Sitemap lexical match against query, topic and recommended page type.",
            })
            selected.append(u)
    deduped = []
    seen = set()
    for u in selected:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return mappings, deduped


def trigger_auditor_if_configured(req: dict[str, Any], target_run_id: str, portfolio_id: str | None) -> dict[str, Any] | None:
    task_id = os.getenv("BODHI_AUDITOR_TASK_ID", "")
    workflow_id = os.getenv("BODHI_AUDITOR_WORKFLOW_ID", "")
    if not task_id:
        return None
    client = BodhiClient()
    if not client.enabled:
        return None
    inputs = {
        "brand": req.get("brand"),
        "market": req.get("market"),
        "domain": req.get("domain"),
        "evidence_service_url": os.getenv("PUBLIC_EVIDENCE_SERVICE_URL") or req.get("evidence_service_url") or "",
        "evidence_run_id": target_run_id,
        "run_mode": req.get("mode") or req.get("run_mode") or "fresh_evidence",
        "query_portfolio_mode": req.get("query_portfolio_mode") or "reuse",
        "query_portfolio_id": portfolio_id or "",
        "max_owned_pages_per_query": req.get("max_owned_pages_per_query") or req.get("max_owned_urls_per_query") or 3,
        "max_external_citations_per_query": req.get("max_external_citations_per_query") or 3,
        "query_limit": req.get("query_limit") or 50,
    }
    trigger = client.trigger_task_run(task_id, workflow_id or None, f"Auditor - {target_run_id}", inputs)
    rid = client.extract_run_id(trigger)
    return {"bodhi_auditor_run_id": rid, "trigger_response": trigger}


def run_phase2_refresh(job_id: str, req: dict[str, Any]) -> None:
    target_run_id = req["target_run_id"]
    try:
        target = run_dir(target_run_id)
        target.mkdir(parents=True, exist_ok=True)
        write_json(target / "refresh_request.json", req)
        write_run_status(target_run_id, "running", {"stage": "initialising", "job_id": job_id, "request": req})
        update_job(job_id, {"status": "running", "stage": "initialising", "target_run_id": target_run_id})

        portfolio: dict[str, Any] | None = None
        portfolio_id = req.get("query_portfolio_id")
        mode = str(req.get("query_portfolio_mode") or "reuse").lower()
        if portfolio_id:
            portfolio = load_portfolio(portfolio_id)
            if not portfolio:
                raise RuntimeError(f"query_portfolio_id was supplied but not found: {portfolio_id}")
        elif mode == "synthetic":
            portfolio = trigger_and_wait_for_portfolio(req, target_run_id)
            portfolio_id = portfolio.get("portfolio_id")
        elif mode in {"manual", "manual_topics_and_queries"}:
            manual = req.get("manual_queries_json") or req.get("queries_json") or req.get("topics_json")
            if isinstance(manual, str) and manual.strip():
                parsed = json.loads(manual)
                if isinstance(parsed, dict) and parsed.get("queries"):
                    portfolio = store_portfolio(parsed, req.get("brand", ""), req.get("market", ""), req.get("domain"))
                    portfolio_id = portfolio.get("portfolio_id")
        if portfolio:
            write_json(target / "query_portfolio.json", portfolio)

        queries = (portfolio or {}).get("queries") or []
        query_limit = int(req.get("query_limit") or 50)
        queries = queries[:query_limit]

        write_run_status(target_run_id, "running", {"stage": "sitemap_inventory_running", "query_count": len(queries), "query_portfolio_id": portfolio_id})
        sitemap_url = req.get("sitemap_url")
        sitemap_urls = fetch_sitemap_urls(req.get("domain"), sitemap_url, max_urls=int(req.get("sitemap_max_urls") or 2000))
        write_json(target / "sitemap_inventory.json", {"source": sitemap_url or (str(req.get("domain") or "").rstrip("/") + "/sitemap.xml"), "url_count": len(sitemap_urls), "urls": sitemap_urls, "generated_at_epoch": now_epoch()})

        owned_urls: list[str] = []
        if portfolio and sitemap_urls:
            write_run_status(target_run_id, "running", {"stage": "owned_url_mapping_running", "sitemap_url_count": len(sitemap_urls)})
            mappings, mapped_owned = map_queries_to_sitemap(portfolio, sitemap_urls, int(req.get("max_owned_pages_per_query") or req.get("max_owned_urls_per_query") or 3))
            write_json(target / "query_owned_url_mapping.json", {"run_id": target_run_id, "query_portfolio_id": portfolio_id, "mappings": mappings, "mapped_owned_url_count": len(mapped_owned)})
            owned_urls = mapped_owned[: int(req.get("max_owned_urls") or 60)]

        # Optional live SerpAPI collection owned by evidence service.
        if bool(req.get("run_serpapi") or req.get("enable_serpapi")) and queries:
            write_run_status(target_run_id, "running", {"stage": "serpapi_collection_running", "serpapi_query_count": len(queries)})
            serp_job = make_job_id("serpapi_inline")
            update_job(serp_job, {"status": "accepted", "job_id": serp_job, "parent_job_id": job_id, "target_run_id": target_run_id})
            run_serpapi_collection(serp_job, SerpApiJobRequest(brand=req.get("brand", ""), market=req.get("market", ""), target_run_id=target_run_id, queries=queries, max_queries=query_limit))
            write_run_status(target_run_id, "running", {"stage": "serpapi_collection_completed", "serpapi_job_id": serp_job})

        # Crawl mapped owned URLs and top external URLs from existing/source/current evidence.
        write_run_status(target_run_id, "running", {"stage": "crawl_refresh_running", "owned_url_count": len(owned_urls)})
        refresh_req = FullRefreshRequest(
            brand=req.get("brand", ""),
            market=req.get("market", ""),
            source_run_id=req.get("source_run_id"),
            target_run_id=target_run_id,
            mode=req.get("mode") or req.get("run_mode") or "phase2_refresh",
            use_existing_google_ai_mode=bool(req.get("use_existing_google_ai_mode", not bool(req.get("run_serpapi") or req.get("enable_serpapi")))),
            run_serpapi=False,
            queries=queries,
            owned_urls=owned_urls,
            external_urls=req.get("external_urls") or [],
            crawl_owned=bool(req.get("crawl_owned", req.get("enable_owned_crawl", True))),
            crawl_external=bool(req.get("crawl_external", req.get("enable_external_crawl", False))),
            max_queries=query_limit,
            max_owned_urls=int(req.get("max_owned_urls") or max(20, len(owned_urls) or 20)),
            max_external_urls=int(req.get("max_external_urls") or 30),
        )
        run_full_refresh(job_id, refresh_req)

        auditor = None
        if bool(req.get("trigger_auditor", True)):
            write_run_status(target_run_id, "running", {"stage": "auditor_queued"})
            try:
                auditor = trigger_auditor_if_configured(req, target_run_id, portfolio_id)
                if auditor:
                    write_run_status(target_run_id, "running", {"stage": "auditor_running", **auditor})
            except Exception as e:
                write_run_status(target_run_id, "running", {"stage": "auditor_trigger_failed", "auditor_error": str(e)[:500]})

        write_run_status(target_run_id, "completed", {"stage": "evidence_ready", "query_portfolio_id": portfolio_id, "auditor": auditor, "completed_at_epoch": now_epoch()})
        update_job(job_id, {"status": "completed", "stage": "evidence_ready", "target_run_id": target_run_id, "completed_at_epoch": now_epoch()})
    except Exception as e:
        write_run_status(target_run_id, "failed", {"stage": "failed", "error": str(e)[:1500], "failed_at_epoch": now_epoch()})
        update_job(job_id, {"status": "failed", "stage": "failed", "error": str(e)[:1500], "failed_at_epoch": now_epoch()})


def start_phase2_refresh(req: dict[str, Any]) -> dict[str, Any]:
    target_run_id = req.get("target_run_id") or f"evidence_{normalise_key(req.get('brand'))}_{normalise_key(req.get('market'))}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    req = {**req, "target_run_id": target_run_id}
    job_id = make_job_id("phase2")
    write_run_status(target_run_id, "accepted", {"stage": "accepted", "job_id": job_id, "request": req, "brand": req.get("brand"), "market": req.get("market"), "domain": req.get("domain")})
    update_job(job_id, {"status": "accepted", "stage": "accepted", "target_run_id": target_run_id, "request": req, "created_at_epoch": now_epoch()})
    thread = threading.Thread(target=run_phase2_refresh, args=(job_id, req), daemon=True)
    thread.start()
    return {"status": "accepted", "job_id": job_id, "target_run_id": target_run_id, "run_status": read_json(status_dir() / f"{target_run_id}.json", {})}
