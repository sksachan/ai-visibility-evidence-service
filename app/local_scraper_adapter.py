from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = APP_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from local_hybrid_scraper import scrape_items_sync  # type: ignore


INLINE_MARKDOWN_MAX_CHARS = int(os.environ.get("INLINE_MARKDOWN_MAX_CHARS", "80000"))
CONTENT_EXTRACT_MAX_CHARS = int(os.environ.get("CONTENT_EXTRACT_MAX_CHARS", "6000"))


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


def norm_url(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    return url.split("#")[0].strip()


def compact_text(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def japanese_char_count(text: str) -> int:
    return len(re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def latin_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", text or ""))


def heading_count(markdown: str) -> int:
    return len(re.findall(r"(?m)^#{1,6}\s+\S+", markdown or ""))


def question_count(markdown: str) -> int:
    return len(re.findall(r"[?？]", markdown or ""))


def list_like_count(markdown: str) -> int:
    return len(re.findall(r"(?m)^\s*[-*•]\s+\S+", markdown or ""))


def table_like_count(markdown: str) -> int:
    return len([ln for ln in (markdown or "").splitlines() if ln.count("|") >= 2])


def duplicate_block_ratio(markdown: str) -> float:
    lines = [compact_text(x).lower() for x in (markdown or "").splitlines()]
    lines = [x for x in lines if len(x) > 20]
    if not lines:
        return 0.0
    unique = len(set(lines))
    return round(1 - (unique / max(1, len(lines))), 3)


def url_domain(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def load_markdown_from_row(row: dict[str, Any]) -> str:
    rel = row.get("markdown_file")
    if not rel:
        return ""
    path = APP_ROOT / str(rel)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def load_manifest_from_row(row: dict[str, Any]) -> dict[str, Any]:
    rel = row.get("manifest_file") or row.get("extraction_manifest_file")
    if not rel:
        return {}
    path = APP_ROOT / str(rel)
    return read_json(path, {}) or {}


def enrich_scraped_row(row: dict[str, Any]) -> dict[str, Any]:
    markdown = load_markdown_from_row(row)
    manifest = load_manifest_from_row(row)

    metadata = manifest.get("metadata") or {}
    links = manifest.get("links") or []
    linked_pdfs = manifest.get("linked_pdfs") or manifest.get("pdf_links") or []
    geo_signals = manifest.get("geo_signals") or {}
    extraction_metrics = manifest.get("extraction_metrics") or {}
    content_regions = manifest.get("content_regions") or {}
    visible_dates = manifest.get("visible_dates") or []

    inline_markdown = markdown[:INLINE_MARKDOWN_MAX_CHARS] if INLINE_MARKDOWN_MAX_CHARS > 0 else markdown

    enriched = dict(row)
    enriched.update({
        "markdown": inline_markdown,
        "raw_markdown_chars": len(markdown),
        "markdown_chars": row.get("markdown_chars") or len(markdown),
        "content_extract": compact_text(markdown, CONTENT_EXTRACT_MAX_CHARS),
        "main_text": compact_text(markdown, CONTENT_EXTRACT_MAX_CHARS),
        "text": compact_text(markdown, CONTENT_EXTRACT_MAX_CHARS),
        "metadata": metadata,
        "links": links[:500],
        "linked_pdfs": linked_pdfs[:100],
        "pdf_links": linked_pdfs[:100],
        "visible_dates": visible_dates,
        "content_regions": content_regions,
        "geo_signals": geo_signals,
        "extraction_metrics": extraction_metrics,
        "static_fetch": manifest.get("static_fetch") or {},
        "rendered_fetch": manifest.get("rendered_fetch") or {},
        "pdf_parser": manifest.get("pdf_parser") or {},
        "schema_types_detected": metadata.get("schema_types") or [],
        "schema_types": metadata.get("schema_types") or [],
        "json_ld_present": bool(metadata.get("json_ld_present")),
        "json_ld_block_count": int(metadata.get("json_ld_block_count") or metadata.get("schema_block_count") or 0),
        "canonical_url": metadata.get("canonical") or "",
        "robots_meta": metadata.get("robots") or "",
        "language": metadata.get("language") or "",
        "final_url": (manifest.get("rendered_fetch") or {}).get("final_url") or (manifest.get("static_fetch") or {}).get("final_url") or row.get("url"),
        "fetch_method": "local_full_page_playwright",
        "geo_analysis_ready": manifest.get("geo_analysis_ready", row.get("geo_analysis_ready")),
        "content_score_policy": manifest.get("content_score_policy", row.get("content_score_policy")),
        "extraction_quality_score": manifest.get("extraction_quality_score", row.get("extraction_quality_score")),
    })

    enriched["content_metrics"] = {
        "raw_markdown_chars": len(markdown),
        "inline_markdown_chars": len(inline_markdown),
        "japanese_char_count": japanese_char_count(markdown),
        "latin_word_count": latin_word_count(markdown),
        "wordish_count": extraction_metrics.get("wordish_count"),
        "heading_count": extraction_metrics.get("heading_count", heading_count(markdown)),
        "question_count": extraction_metrics.get("question_count", question_count(markdown)),
        "list_like_block_count": list_like_count(markdown),
        "table_like_block_count": extraction_metrics.get("table_line_count", table_like_count(markdown)),
        "duplicate_block_ratio": duplicate_block_ratio(markdown),
        "link_count": extraction_metrics.get("link_count", len(links)),
        "pdf_link_count": len(linked_pdfs),
        "numeric_fact_count": extraction_metrics.get("numeric_fact_count"),
    }

    return enriched


def selected_owned_pages_from_scope(scope: dict[str, Any]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}

    for r in scope.get("queries", []):
        if not isinstance(r, dict):
            continue

        for p in r.get("owned_pages", []) or []:
            if not isinstance(p, dict):
                continue

            url = p.get("url")
            if not url:
                continue

            if url not in by_url:
                by_url[url] = {
                    **p,
                    "url": url,
                    "brand_topic_category": p.get("brand_topic_category") or r.get("brand_topic_category", ""),
                    "related_queries_seed": [],
                }

            by_url[url].setdefault("related_queries_seed", []).append({
                "query_id": r.get("query_id"),
                "query": r.get("query", ""),
                "brand_topic_category": r.get("brand_topic_category", ""),
                "query_type": r.get("query_type", ""),
                "priority": r.get("priority", ""),
            })

    return list(by_url.values())


def selected_external_pages_from_scope(scope: dict[str, Any]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}

    for r in scope.get("queries", []):
        if not isinstance(r, dict):
            continue

        for p in r.get("external_pages", []) or []:
            if not isinstance(p, dict):
                continue

            url = p.get("url") or p.get("source_url")
            if not url:
                continue

            if url not in by_url:
                by_url[url] = {
                    **p,
                    "url": url,
                    "brand_topic_category": p.get("brand_topic_category") or r.get("brand_topic_category", ""),
                    "related_queries_seed": [],
                }

            by_url[url].setdefault("related_queries_seed", []).append({
                "query_id": r.get("query_id"),
                "query": r.get("query", ""),
                "brand_topic_category": r.get("brand_topic_category", ""),
                "query_type": r.get("query_type", ""),
                "priority": r.get("priority", ""),
            })

    return list(by_url.values())


def fallback_collect_urls(obj: Any, owned_domains: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    owned: dict[str, dict[str, Any]] = {}
    external: dict[str, dict[str, Any]] = {}

    url_keys = {"url", "source_url", "page_url", "owned_url", "target_url", "canonical_url"}

    def is_owned(url: str) -> bool:
        host = url_domain(url)
        return any(host == d or host.endswith("." + d) for d in owned_domains)

    def walk(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in url_keys and isinstance(v, str):
                    u = norm_url(v)
                    if u.startswith(("http://", "https://")):
                        target = owned if is_owned(u) else external
                        target.setdefault(u, {"url": u, "title": x.get("title", ""), "snippet": x.get("snippet", "")})
                else:
                    walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return list(owned.values()), list(external.values())


def run_local_parity_scrape(
    items: list[dict[str, Any]],
    target_run_id: str,
    kind: str,
    max_items: int,
    scrape_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = items[: max(0, max_items)]
    output_root = f"outputs/railway_runs/{target_run_id}/free_hybrid/{kind}_pages"

    cfg = {
        "playwright_timeout_ms": int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "60000")),
        "pdf_parser": {"max_pages": int(os.environ.get("PDF_MAX_PAGES", "80"))},
    }
    if scrape_cfg:
        cfg.update(scrape_cfg)

    scraped = scrape_items_sync(selected, output_root, kind, cfg)

    pages = []
    for row in scraped.get("pages", []) or []:
        if isinstance(row, dict):
            pages.append(enrich_scraped_row(row))

    failed = scraped.get("failed", []) or []

    return {
        "requested": len(selected),
        "pages": pages,
        "failed": failed,
    }
