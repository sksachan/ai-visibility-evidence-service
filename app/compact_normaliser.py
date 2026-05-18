from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def now_epoch() -> int:
    return int(time.time())


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


def domain_from_url(url: Any) -> str:
    try:
        netloc = urlparse(str(url or "")).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _first_text(page: dict[str, Any]) -> str:
    for key in ("markdown", "raw_markdown", "content_extract", "main_text", "text", "snippet", "description"):
        value = page.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def count_words_or_tokens(text: str) -> int:
    if not text:
        return 0
    # Count Latin/number tokens plus CJK characters as extractability tokens.
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-']*|[\u3040-\u30ff\u3400-\u9fff]", text)
    return len(tokens)


def enrich_crawled_page(page: dict[str, Any], *, external: bool = False, citation_hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Materialise quantitative crawl fields used by Auditor/frontend.

    The crawler can return large markdown bodies while omitting lightweight summary
    fields. This function makes those fields deterministic so downstream scoring can
    tell a genuine crawl from metadata-only placeholders.
    """
    out = dict(page or {})
    hint = citation_hint or {}
    url = out.get("url") or out.get("source_url") or hint.get("url") or hint.get("source_url")
    if url:
        out.setdefault("url", url)
        out.setdefault("source_url", url)
    final_url = out.get("final_url") or out.get("resolved_url") or out.get("canonical_url") or url
    if final_url:
        out.setdefault("final_url", final_url)
        out.setdefault("resolved_url", final_url)

    domain = out.get("domain") or out.get("source_domain") or hint.get("source_domain") or domain_from_url(url)
    if domain:
        out["domain"] = domain
        if external or out.get("source_url"):
            out["source_domain"] = out.get("source_domain") or domain

    for key in ("title", "source_name", "source_type", "snippet", "citation_count", "citation_position", "first_cited_position", "queries", "related_queries_seed"):
        if out.get(key) in (None, "", []):
            if hint.get(key) not in (None, "", []):
                out[key] = hint.get(key)

    if out.get("http_status_code") is None:
        out["http_status_code"] = out.get("status_code") or out.get("response_status") or out.get("status")
    if out.get("status_code") is None and out.get("http_status_code") is not None:
        out["status_code"] = out.get("http_status_code")

    markdown = out.get("markdown") if isinstance(out.get("markdown"), str) else ""
    raw_markdown = out.get("raw_markdown") if isinstance(out.get("raw_markdown"), str) else ""
    text_blob = _first_text(out)

    out["markdown_chars"] = int(out.get("markdown_chars") or len(markdown or text_blob))
    out["raw_markdown_chars"] = int(out.get("raw_markdown_chars") or len(raw_markdown or markdown or text_blob))
    out["text_chars"] = int(out.get("text_chars") or len(text_blob))
    out["word_count"] = int(out.get("word_count") or count_words_or_tokens(text_blob))

    if not out.get("content_extract") and text_blob:
        out["content_extract"] = text_blob[:12000]
    if not out.get("main_text") and out.get("content_extract"):
        out["main_text"] = out.get("content_extract")
    if not out.get("text") and out.get("content_extract"):
        out["text"] = out.get("content_extract")

    crawl_status = str(out.get("crawl_status") or "").strip().lower()
    if crawl_status == "success" and out["text_chars"] <= 0:
        out["crawl_status"] = "partial_success_empty_text"
        crawl_status = "partial_success_empty_text"
    elif crawl_status == "" and out["text_chars"] > 0:
        # Missing status plus extracted text usually means an older crawler payload.
        # Do not upgrade explicit pending/not_requested metadata-only candidates.
        out["crawl_status"] = "success"
        crawl_status = "success"

    extraction_status = str(out.get("extraction_status") or "").strip().lower()
    if crawl_status == "success" and out["text_chars"] > 0 and extraction_status in {"", "pending", "not_requested", "empty_extract"}:
        out["extraction_status"] = "success"
    elif out["text_chars"] <= 0 and crawl_status == "success":
        out["extraction_status"] = "empty_extract"

    if crawl_status == "success" and out["word_count"] >= 20:
        out["geo_analysis_ready"] = True
        if not external:
            out["content_score_policy"] = "score"
        else:
            out["content_score_policy"] = out.get("content_score_policy") or "benchmark_score"
    else:
        out.setdefault("geo_analysis_ready", False)
        out.setdefault("content_score_policy", "metadata_only_until_crawled" if not external else "citation_metadata_until_crawled")

    return out


def _pages_by_url(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in pages:
        if not isinstance(p, dict):
            continue
        for key in ("url", "source_url", "final_url", "resolved_url"):
            u = p.get(key)
            if isinstance(u, str) and u:
                out.setdefault(u, p)
    return out


def merge_pages(existing: list[Any], crawled_by_url: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        u = item.get("url") or item.get("source_url") or item.get("final_url")
        enriched = crawled_by_url.get(u) if u else None
        if enriched:
            row = {**item, **enriched}
        else:
            row = dict(item)
        merged.append(row)
        if u:
            seen.add(u)
    for u, row in crawled_by_url.items():
        if u not in seen:
            merged.append(row)
            seen.add(u)
    return merged




def _crawl_attempted(pages: list[dict[str, Any]]) -> int:
    return sum(1 for p in pages if str(p.get("crawl_status") or "").lower() not in {"", "not_requested", "pending"})

def _crawl_success(pages: list[dict[str, Any]]) -> int:
    return sum(1 for p in pages if p.get("crawl_status") == "success")

def _crawl_failed(pages: list[dict[str, Any]]) -> int:
    return sum(1 for p in pages if str(p.get("crawl_status")) in {"failed", "blocked", "error", "partial_success_empty_text"})

def _scoreable(pages: list[dict[str, Any]]) -> int:
    return sum(1 for p in pages if p.get("geo_analysis_ready") is True and int(p.get("word_count") or 0) >= 20)

def _citation_hints(evidence_scope: dict[str, Any], google_ai: dict[str, Any], source_classification: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    candidates: list[Any] = []
    if isinstance(evidence_scope, dict):
        candidates.extend(evidence_scope.get("ai_citations") or [])
        candidates.extend(evidence_scope.get("external_sources") or [])
    if isinstance(source_classification, dict):
        candidates.extend(source_classification.get("sources") or [])
    rows = google_ai.get("rows") or google_ai.get("queries") if isinstance(google_ai, dict) else []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                candidates.extend(row.get("top_citations") or row.get("references") or [])
    for c in candidates:
        if not isinstance(c, dict):
            continue
        u = c.get("url") or c.get("source_url")
        if isinstance(u, str) and u:
            hints.setdefault(u, {}).update(c)
    return hints


def canonicalise_bundle_files(run_dir: Path, req: dict[str, Any] | None = None, *, write_files: bool = True) -> dict[str, Any]:
    req = req or {}
    audit_context = read_json(run_dir / "audit_context.json", {}) or {}
    evidence_scope = read_json(run_dir / "evidence_scope.json", {}) or {}
    google_ai = read_json(run_dir / "google_ai_mode_compact.json", {}) or {}
    owned_full = read_json(run_dir / "owned_pages_full.json", {}) or {}
    external_full = read_json(run_dir / "external_pages_full.json", {}) or {}
    visibility = read_json(run_dir / "visibility_matrix.json", {}) or {}
    source_classification = read_json(run_dir / "source_classification.json", {}) or {}

    citation_hints = _citation_hints(evidence_scope, google_ai, source_classification)

    owned_pages_raw = owned_full.get("pages") if isinstance(owned_full, dict) else []
    if not isinstance(owned_pages_raw, list):
        owned_pages_raw = []
    owned_pages = [enrich_crawled_page(p, external=False) for p in owned_pages_raw if isinstance(p, dict)]
    owned_by_url = _pages_by_url(owned_pages)

    external_pages_raw = []
    if isinstance(external_full, dict):
        external_pages_raw = external_full.get("external_pages") or external_full.get("pages") or []
    if not isinstance(external_pages_raw, list):
        external_pages_raw = []
    external_pages = []
    for p in external_pages_raw:
        if isinstance(p, dict):
            u = p.get("url") or p.get("source_url")
            external_pages.append(enrich_crawled_page(p, external=True, citation_hint=citation_hints.get(u or "")))
    external_by_url = _pages_by_url(external_pages)

    if isinstance(audit_context, dict):
        for key in ("pages", "owned_urls"):
            if isinstance(audit_context.get(key), list):
                audit_context[key] = merge_pages(audit_context[key], owned_by_url)
        counts = audit_context.setdefault("counts", {}) if isinstance(audit_context.setdefault("counts", {}), dict) else {}
        counts["owned_pages_crawled"] = sum(1 for p in owned_pages if p.get("crawl_status") == "success")
        counts["owned_pages_scoreable"] = sum(1 for p in owned_pages if p.get("geo_analysis_ready") is True and (p.get("markdown") or p.get("content_extract") or p.get("text")))

    if isinstance(evidence_scope, dict):
        for key in ("owned_pages", "owned_urls"):
            if isinstance(evidence_scope.get(key), list):
                evidence_scope[key] = merge_pages(evidence_scope[key], owned_by_url)
        if isinstance(evidence_scope.get("external_sources"), list):
            evidence_scope["external_sources"] = merge_pages(evidence_scope["external_sources"], external_by_url)
        if isinstance(evidence_scope.get("ai_citations"), list):
            evidence_scope["ai_citations"] = merge_pages(evidence_scope["ai_citations"], external_by_url)
        collection = evidence_scope.setdefault("evidence_collection", {}) if isinstance(evidence_scope.setdefault("evidence_collection", {}), dict) else {}
        collection.update({
            "owned_pages_attempted": _crawl_attempted(owned_pages),
            "owned_pages_crawled": _crawl_success(owned_pages),
            "owned_pages_failed": _crawl_failed(owned_pages),
            "external_pages_attempted": _crawl_attempted(external_pages),
            "external_pages_crawled": _crawl_success(external_pages),
            "external_pages_failed": _crawl_failed(external_pages),
            "external_citation_count": len(evidence_scope.get("ai_citations") or []),
        })

    # Enrich Google rows and visibility matrix with derived source diagnostics.
    cites_by_q: dict[str, list[dict[str, Any]]] = {}
    rows = google_ai.get("rows") or google_ai.get("queries") if isinstance(google_ai, dict) else []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            qid = str(row.get("query_id") or "")
            refs = row.get("top_citations") or row.get("references") or []
            clean_refs: list[dict[str, Any]] = []
            for r in refs if isinstance(refs, list) else []:
                if isinstance(r, dict):
                    u = r.get("url") or r.get("source_url")
                    enriched = {**r, **(external_by_url.get(u or "") or {})}
                    enriched = enrich_crawled_page(enriched, external=True, citation_hint=r)
                    clean_refs.append(enriched)
            row["top_citations"] = clean_refs[:3]
            row["references"] = clean_refs
            row["top_cited_sources"] = clean_refs[:3]
            row["citation_count"] = len(clean_refs)
            if clean_refs:
                row["leading_citation_domain"] = clean_refs[0].get("source_domain") or clean_refs[0].get("domain")
                row["winning_source_types"] = sorted({c.get("source_type") or "external_citation" for c in clean_refs})
            if qid:
                cites_by_q[qid] = clean_refs
        google_ai["rows"] = rows
        google_ai["queries"] = rows

    if isinstance(visibility, dict) and isinstance(visibility.get("queries"), list):
        for q in visibility["queries"]:
            if not isinstance(q, dict):
                continue
            cites = cites_by_q.get(str(q.get("query_id") or "")) or q.get("citations") or []
            if not isinstance(cites, list):
                cites = []
            q["citations"] = cites
            q["top_citations"] = cites[:3]
            q["citation_count"] = len(cites)
            q["citation_domains"] = [c.get("source_domain") or c.get("domain") for c in cites if c.get("source_domain") or c.get("domain")]
            q["leading_citation_domain"] = q["citation_domains"][0] if q["citation_domains"] else None
            q["winning_source_types"] = sorted({c.get("source_type") or "external_citation" for c in cites}) if cites else []
            if cites:
                q["visibility_status"] = "serpapi_collected_with_citations"

    if isinstance(source_classification, dict):
        sources = source_classification.get("sources") or []
        if isinstance(sources, list):
            source_classification["sources"] = merge_pages(sources, external_by_url)
        counts: dict[str, int] = {}
        for src in source_classification.get("sources") or []:
            if isinstance(src, dict):
                st = src.get("source_type") or "external_citation"
                counts[st] = counts.get(st, 0) + 1
        source_classification["source_type_counts"] = counts
        source_classification["external_pages_crawled"] = sum(1 for p in external_pages if p.get("crawl_status") == "success")

    if isinstance(owned_full, dict):
        owned_full["pages"] = owned_pages
        summary = owned_full.setdefault("summary", {}) if isinstance(owned_full.setdefault("summary", {}), dict) else {}
        summary.update({
            "candidates": len(owned_pages),
            "crawl_attempted": _crawl_attempted(owned_pages),
            "successful": _crawl_success(owned_pages),
            "failed": _crawl_failed(owned_pages),
            "scoreable": _scoreable(owned_pages),
        })
    if isinstance(external_full, dict):
        external_full["external_pages"] = external_pages
        external_full["pages"] = external_pages
        summary = external_full.setdefault("summary", {}) if isinstance(external_full.setdefault("summary", {}), dict) else {}
        summary.update({
            "candidates": len(external_pages),
            "crawl_attempted": _crawl_attempted(external_pages),
            "successful": _crawl_success(external_pages),
            "failed": _crawl_failed(external_pages),
            "scoreable": _scoreable(external_pages),
        })

    telemetry = {
        "owned_page_candidates": len(owned_pages),
        "owned_pages_attempted": _crawl_attempted(owned_pages),
        "owned_pages_crawled": _crawl_success(owned_pages),
        "owned_pages_failed": _crawl_failed(owned_pages),
        "owned_pages_scoreable": _scoreable(owned_pages),
        "external_page_candidates": len(external_pages),
        "external_pages_attempted": _crawl_attempted(external_pages),
        "external_pages_crawled": _crawl_success(external_pages),
        "external_pages_failed": _crawl_failed(external_pages),
        "external_pages_scoreable": _scoreable(external_pages),
        "external_citation_count": sum(int(r.get("citation_count") or 0) for r in (google_ai.get("rows") or []) if isinstance(r, dict)) if isinstance(google_ai, dict) else 0,
        "crawl_success_rate": None,
    }
    attempted = telemetry["owned_pages_attempted"] + telemetry["external_pages_attempted"]
    if attempted:
        telemetry["crawl_success_rate"] = round((telemetry["owned_pages_crawled"] + telemetry["external_pages_crawled"]) / attempted, 4)

    if write_files:
        write_json(run_dir / "audit_context.json", audit_context)
        write_json(run_dir / "evidence_scope.json", evidence_scope)
        write_json(run_dir / "google_ai_mode_compact.json", google_ai)
        write_json(run_dir / "owned_pages_full.json", owned_full)
        write_json(run_dir / "external_pages_full.json", external_full)
        write_json(run_dir / "visibility_matrix.json", visibility)
        write_json(run_dir / "source_classification.json", source_classification)
        write_json(run_dir / "crawl_telemetry.json", telemetry)

    return {
        "audit_context": audit_context,
        "evidence_scope": evidence_scope,
        "google_ai_mode_compact": google_ai,
        "owned_pages_full": owned_full,
        "external_pages_full": external_full,
        "visibility_matrix": visibility,
        "source_classification": source_classification,
        "telemetry": telemetry,
    }
