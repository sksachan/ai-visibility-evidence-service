from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

OWNED_MARKDOWN_MAX_CHARS = int(os.environ.get("BODHI_OWNED_MARKDOWN_MAX_CHARS", "60000"))
OWNED_CONTENT_EXTRACT_MAX_CHARS = int(os.environ.get("BODHI_OWNED_CONTENT_EXTRACT_MAX_CHARS", "12000"))
EXTERNAL_CONTENT_EXTRACT_MAX_CHARS = int(os.environ.get("BODHI_EXTERNAL_CONTENT_EXTRACT_MAX_CHARS", "8000"))
OWNED_LINK_LIMIT = int(os.environ.get("BODHI_OWNED_LINK_LIMIT", "75"))
EXTERNAL_LINK_LIMIT = int(os.environ.get("BODHI_EXTERNAL_LINK_LIMIT", "25"))
PDF_LINK_LIMIT = int(os.environ.get("BODHI_PDF_LINK_LIMIT", "25"))


def now_epoch() -> int:
    return int(time.time())


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read JSON file {path.name}: {e}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def truncate(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n...[truncated for Bodhi compact payload]"


def slim_links(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def pick(d: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if k in d}


def slim_owned_page(page: dict[str, Any]) -> dict[str, Any]:
    keep = pick(page, [
        "url",
        "final_url",
        "rank",
        "selection_reason",
        "mapping_quality",
        "mapping_score",
        "mapping_reason",
        "brand_topic_category",
        "related_queries_seed",
        "scrape_method",
        "resource_type",
        "crawl_status",
        "extraction_status",
        "content_score_policy",
        "geo_analysis_ready",
        "extraction_quality_score",
        "markdown_chars",
        "raw_markdown_chars",
        "title",
        "description",
        "status_code",
        "canonical_url",
        "robots_meta",
        "language",
        "schema_types_detected",
        "metadata",
        "visible_dates",
        "content_regions",
        "geo_signals",
        "extraction_metrics",
        "content_metrics",
        "static_fetch",
        "rendered_fetch",
        "pdf_parser",
    ])

    keep["markdown"] = truncate(page.get("markdown", ""), OWNED_MARKDOWN_MAX_CHARS)
    keep["content_extract"] = truncate(page.get("content_extract") or page.get("main_text") or page.get("text") or "", OWNED_CONTENT_EXTRACT_MAX_CHARS)
    keep["main_text"] = keep["content_extract"]
    keep["text"] = keep["content_extract"]

    keep["links"] = slim_links(page.get("links"), OWNED_LINK_LIMIT)
    keep["pdf_links"] = slim_links(page.get("pdf_links") or page.get("linked_pdfs"), PDF_LINK_LIMIT)
    keep["linked_pdfs"] = keep["pdf_links"]

    # Keep traceability, but remove heavy local file paths that Bodhi cannot read directly.
    keep["railway_trace"] = {
        "markdown_file": page.get("markdown_file"),
        "manifest_file": page.get("manifest_file") or page.get("extraction_manifest_file"),
    }

    return keep


def slim_external_page(page: dict[str, Any]) -> dict[str, Any]:
    keep = pick(page, [
        "title",
        "url",
        "source_url",
        "source_name",
        "source_domain",
        "source_type",
        "source_quality",
        "source_quality_notes",
        "source_category",
        "source_role",
        "snippet",
        "citation_count",
        "first_cited_position",
        "citation_position",
        "answer_support_weight",
        "supported_block_types",
        "is_owned_domain",
        "is_owned_ecosystem",
        "is_off_market",
        "is_social_or_forum",
        "is_low_authority",
        "selection_reason",
        "brand_topic_category",
        "related_queries_seed",
        "scrape_method",
        "resource_type",
        "crawl_status",
        "extraction_status",
        "geo_analysis_ready",
        "content_score_policy",
        "extraction_quality_score",
        "markdown_chars",
        "raw_markdown_chars",
        "description",
        "status_code",
        "canonical_url",
        "robots_meta",
        "language",
        "schema_types_detected",
        "metadata",
        "visible_dates",
        "geo_signals",
        "extraction_metrics",
        "content_metrics",
    ])

    # External pages are benchmark evidence; keep concise extract, not full markdown.
    keep["content_extract"] = truncate(page.get("content_extract") or page.get("main_text") or page.get("text") or page.get("markdown") or "", EXTERNAL_CONTENT_EXTRACT_MAX_CHARS)
    keep["main_text"] = keep["content_extract"]
    keep["text"] = keep["content_extract"]

    keep["links"] = slim_links(page.get("links"), EXTERNAL_LINK_LIMIT)
    keep["pdf_links"] = slim_links(page.get("pdf_links") or page.get("linked_pdfs"), PDF_LINK_LIMIT)
    keep["linked_pdfs"] = keep["pdf_links"]

    keep["railway_trace"] = {
        "markdown_file": page.get("markdown_file"),
        "manifest_file": page.get("manifest_file") or page.get("extraction_manifest_file"),
    }

    return keep


def slim_failed(records: Any, limit: int = 100) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    out = []
    for r in records[:limit]:
        if not isinstance(r, dict):
            continue
        out.append({
            "url": r.get("url") or r.get("source_url"),
            "crawl_status": r.get("crawl_status"),
            "extraction_status": r.get("extraction_status"),
            "error": truncate(r.get("error", ""), 500),
        })
    return out


def build_bodhi_bundle(run_id: str) -> dict[str, Any]:
    run_dir = DATA_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    audit_context = read_json(run_dir / "audit_context.json", {}) or {}
    evidence_scope = read_json(run_dir / "evidence_scope.json", {}) or {}
    google_ai_mode = read_json(run_dir / "google_ai_mode_compact.json", {}) or {}
    owned_full = read_json(run_dir / "owned_pages_full.json", {}) or {}
    external_full = read_json(run_dir / "external_pages_full.json", {}) or {}
    visibility_matrix = read_json(run_dir / "visibility_matrix.json", {}) or {}
    source_classification = read_json(run_dir / "source_classification.json", {}) or {}

    owned_pages = owned_full.get("pages", [])
    if not isinstance(owned_pages, list):
        owned_pages = []

    external_pages = external_full.get("external_pages") or external_full.get("pages") or []
    if not isinstance(external_pages, list):
        external_pages = []

    slim_owned = [slim_owned_page(p) for p in owned_pages if isinstance(p, dict)]
    slim_external = [slim_external_page(p) for p in external_pages if isinstance(p, dict)]

    owned_payload = {
        **{k: v for k, v in owned_full.items() if k != "pages"},
        "bodhi_compact": True,
        "pages": slim_owned,
    }

    external_payload = {
        **{k: v for k, v in external_full.items() if k not in {"external_pages", "pages", "failed_sources"}},
        "bodhi_compact": True,
        "external_pages": slim_external,
        "pages": slim_external,
        "failed_sources": slim_failed(external_full.get("failed_sources")),
    }

    bundle = {
        "bundle_type": "bodhi_compact",
        "run_id": run_id,
        "generated_at_epoch": now_epoch(),
        "limits": {
            "owned_markdown_max_chars": OWNED_MARKDOWN_MAX_CHARS,
            "owned_content_extract_max_chars": OWNED_CONTENT_EXTRACT_MAX_CHARS,
            "external_content_extract_max_chars": EXTERNAL_CONTENT_EXTRACT_MAX_CHARS,
            "owned_link_limit": OWNED_LINK_LIMIT,
            "external_link_limit": EXTERNAL_LINK_LIMIT,
            "pdf_link_limit": PDF_LINK_LIMIT,
        },
        "audit_context": audit_context,
        "evidence_scope": evidence_scope,
        "google_ai_mode_compact": google_ai_mode,
        "owned_pages_full": owned_payload,
        "external_pages_full": external_payload,
        "visibility_matrix": visibility_matrix,
        "source_classification": source_classification,
        "counts": {
            "owned_pages": len(slim_owned),
            "external_pages": len(slim_external),
            "owned_pages_scoreable": sum(
                1 for p in slim_owned
                if p.get("crawl_status") == "success"
                and p.get("extraction_status") == "success"
                and p.get("geo_analysis_ready") is True
                and p.get("content_score_policy") == "score"
                and (p.get("markdown") or p.get("content_extract"))
            ),
            "external_failed_sources": len(external_payload["failed_sources"]),
        },
    }

    out_path = run_dir / "bodhi_bundle.json"
    write_json(out_path, bundle)

    # Update manifest with the Bodhi compact file.
    manifest_path = run_dir / "run_manifest.json"
    manifest = read_json(manifest_path, {}) or read_json(run_dir / "manifest.json", {}) or {}
    manifest["bodhi_bundle"] = str(out_path)
    manifest["bodhi_compact_generated_at_epoch"] = bundle["generated_at_epoch"]
    write_json(run_dir / "run_manifest.json", manifest)
    write_json(run_dir / "manifest.json", manifest)

    return bundle


@router.get("/runs/{run_id}/bodhi-compact")
def get_bodhi_compact(run_id: str):
    return build_bodhi_bundle(run_id)


@router.post("/admin/runs/{run_id}/build-bodhi-compact")
def post_build_bodhi_compact(run_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    bundle = build_bodhi_bundle(run_id)
    size_mb = len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) / 1024 / 1024
    return {
        "status": "success",
        "run_id": run_id,
        "file": str(DATA_DIR / run_id / "bodhi_bundle.json"),
        "size_mb": round(size_mb, 2),
        "counts": bundle.get("counts", {}),
    }


class CleanupRunsRequest(BaseModel):
    keep_run_ids: list[str] = Field(default_factory=list)
    delete_run_ids: list[str] = Field(default_factory=list)
    dry_run: bool = True
    delete_jobs: bool = False


@router.post("/admin/cleanup-runs")
def cleanup_runs(req: CleanupRunsRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    keep = set(req.keep_run_ids or [])
    delete = set(req.delete_run_ids or [])

    if not DATA_DIR.exists():
        return {"status": "ok", "message": "DATA_DIR does not exist", "data_dir": str(DATA_DIR)}

    deleted = []
    skipped = []
    candidates = []

    for p in DATA_DIR.iterdir():
        if not p.is_dir():
            continue
        if p.name == "_jobs":
            continue
        if delete and p.name not in delete:
            continue
        if p.name in keep:
            skipped.append({"run_id": p.name, "reason": "kept"})
            continue
        candidates.append(p)

    for p in candidates:
        if req.dry_run:
            deleted.append({"run_id": p.name, "path": str(p), "dry_run": True})
        else:
            shutil.rmtree(p, ignore_errors=True)
            deleted.append({"run_id": p.name, "path": str(p), "deleted": True})

    jobs_deleted = None
    jobs_path = DATA_DIR / "_jobs"
    if req.delete_jobs and jobs_path.exists():
        if req.dry_run:
            jobs_deleted = {"path": str(jobs_path), "dry_run": True}
        else:
            shutil.rmtree(jobs_path, ignore_errors=True)
            jobs_deleted = {"path": str(jobs_path), "deleted": True}

    return {
        "status": "dry_run" if req.dry_run else "success",
        "data_dir": str(DATA_DIR),
        "kept": sorted(list(keep)),
        "deleted_or_would_delete": deleted,
        "skipped": skipped,
        "jobs": jobs_deleted,
    }
