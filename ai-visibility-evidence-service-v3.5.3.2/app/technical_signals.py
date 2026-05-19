from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

AI_AGENT_TOKENS = ("gptbot", "chatgpt-user", "google-extended", "ccbot", "perplexitybot", "claudebot", "anthropic-ai")


def now_epoch() -> int:
    return int(time.time())


def _compact(value: str, limit: int = 1600) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def candidate_bases(domain_or_url: str) -> list[str]:
    raw = (domain_or_url or "").strip()
    if not raw:
        return []
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path.split("/")[0]
    if not host:
        return []
    bases = [f"{scheme}://{host}/"]
    if host.startswith("www."):
        bases.append(f"{scheme}://{host[4:]}/")
    else:
        bases.append(f"{scheme}://www.{host}/")
    out: list[str] = []
    seen: set[str] = set()
    for b in bases:
        if b not in seen:
            seen.add(b); out.append(b)
    return out


def fetch_standard(url: str, timeout: int = 15) -> dict[str, Any]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "AIVisibilityEvidenceService/3.5 (+https://openai.com)"}, allow_redirects=True)
        text = r.text or ""
        ok = 200 <= r.status_code < 300 and bool(text.strip())
        return {
            "url": url,
            "resolved_url": str(r.url),
            "status": "present" if ok else "missing",
            "available": ok,
            "http_status_code": r.status_code,
            "content_type": r.headers.get("content-type", ""),
            "chars": len(text),
            "sample": _compact(text),
        }
    except Exception as e:
        return {"url": url, "resolved_url": "", "status": "error", "available": False, "http_status_code": None, "content_type": "", "chars": 0, "sample": "", "error": str(e)[:300]}


def first_present(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next((r for r in rows if r.get("available") or r.get("status") == "present"), rows[0] if rows else {})


def collect_site_standards(domain_or_url: str) -> dict[str, Any]:
    bases = candidate_bases(domain_or_url)
    robots_attempts = [fetch_standard(urljoin(base, "robots.txt")) for base in bases]
    llms_attempts = [fetch_standard(urljoin(base, "llms.txt")) for base in bases]
    robots = first_present(robots_attempts)
    llms = first_present(llms_attempts)
    robots_sample = (robots.get("sample") or "").lower()
    return {
        "site_standards_status": "success" if bases else "domain_not_supplied",
        "checked_at_epoch": now_epoch(),
        "domain": domain_or_url,
        "candidate_bases_checked": bases,
        "robots_txt_status": "present" if robots.get("available") else (robots.get("status") or "missing"),
        "robots_txt_url": robots.get("resolved_url") or robots.get("url") or "",
        "llms_txt_status": "present" if llms.get("available") else (llms.get("status") or "missing"),
        "llms_txt_url": llms.get("resolved_url") or llms.get("url") or "",
        "robots_txt": robots,
        "llms_txt": llms,
        "robots_attempts": robots_attempts,
        "llms_txt_attempts": llms_attempts,
        "signals": {
            "robots_available": bool(robots.get("available")),
            "llms_txt_available": bool(llms.get("available")),
            "robots_mentions_sitemap": "sitemap:" in robots_sample,
            "robots_blocks_common_ai_agents": any(token in robots_sample for token in AI_AGENT_TOKENS),
        },
    }


def _first_number(*values: Any) -> int:
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return 0


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [x.strip() for x in re.split(r"[,|]", value) if x.strip()]
    return []


def normalize_page_technical_signals(page: dict[str, Any]) -> dict[str, Any]:
    metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    structured = page.get("structured_data") if isinstance(page.get("structured_data"), dict) else {}
    technical = page.get("technical_signals") if isinstance(page.get("technical_signals"), dict) else {}
    geo = page.get("geo_signals") if isinstance(page.get("geo_signals"), dict) else {}
    schema_types = []
    for value in (page.get("schema_types_detected"), page.get("schema_types"), metadata.get("schema_types"), structured.get("schema_types"), technical.get("schema_types"), geo.get("schema_types")):
        schema_types.extend(_as_list(value))
    schema_types = list(dict.fromkeys(schema_types))
    block_count = _first_number(page.get("json_ld_block_count"), page.get("schema_block_count"), page.get("schema_blocks_count"), structured.get("json_ld_block_count"), technical.get("json_ld_block_count"), metadata.get("schema_block_count"))
    if not block_count and isinstance(page.get("schema_blocks"), list):
        block_count = len(page.get("schema_blocks") or [])
    json_ld_present = any(v is True for v in (page.get("json_ld_present"), page.get("schema_json_ld"), structured.get("json_ld_present"), technical.get("json_ld_present"), metadata.get("json_ld_present"), geo.get("schema_json_ld"))) or block_count > 0 or bool(schema_types)
    canonical = page.get("canonical_url") or page.get("canonical") or metadata.get("canonical") or technical.get("canonical_url") or ""
    meta_description = page.get("meta_description") or page.get("description") or metadata.get("description") or metadata.get("og:description") or ""
    robots_meta = page.get("robots_meta") or metadata.get("robots") or technical.get("robots_meta") or ""
    return {
        "json_ld_present": bool(json_ld_present),
        "json_ld_block_count": int(block_count),
        "schema_types_detected": schema_types,
        "canonical_present": bool(canonical),
        "canonical_url": canonical,
        "meta_description_present": bool(meta_description),
        "meta_description": meta_description,
        "robots_meta": robots_meta,
        "schema_types": schema_types,
    }


def enrich_page_technical_signals(page: dict[str, Any]) -> dict[str, Any]:
    signals = normalize_page_technical_signals(page)
    out = dict(page)
    out.update({
        "json_ld_present": signals["json_ld_present"],
        "json_ld_block_count": signals["json_ld_block_count"],
        "schema_types_detected": signals["schema_types_detected"],
        "canonical_present": signals["canonical_present"],
        "meta_description_present": signals["meta_description_present"],
    })
    out["technical_signals"] = {**(out.get("technical_signals") if isinstance(out.get("technical_signals"), dict) else {}), **signals}
    out["structured_data"] = {**(out.get("structured_data") if isinstance(out.get("structured_data"), dict) else {}), "json_ld_present": signals["json_ld_present"], "json_ld_block_count": signals["json_ld_block_count"], "schema_types": signals["schema_types_detected"]}
    return out


def technical_summary(pages: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(pages)
    with_json = 0
    types: list[str] = []
    for page in pages:
        s = normalize_page_technical_signals(page)
        if s["json_ld_present"]:
            with_json += 1
        types.extend(s["schema_types_detected"])
    return {
        "json_ld_pages": with_json,
        "owned_pages_total": total,
        "json_ld_coverage_pct": round((with_json / total * 100), 1) if total else 0,
        "schema_types_detected": list(dict.fromkeys(types)),
    }
