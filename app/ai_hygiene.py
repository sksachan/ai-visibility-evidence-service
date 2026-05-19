from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


NOT_FULLY_CHECKED_SUMMARY = (
    "AI discoverability hygiene was not fully checked. Robots.txt, LLMs.txt, "
    "or JSON-LD crawl signals are missing from this evidence run."
)


def now_epoch() -> int:
    return int(time.time())


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def is_valid_hygiene(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("priority"), str)
        and isinstance(value.get("summary"), str)
        and isinstance(value.get("robots_txt"), dict)
        and isinstance(value.get("llms_txt"), dict)
        and isinstance(value.get("structured_data"), dict)
    )


def _status_from_attempts(value: dict[str, Any]) -> str:
    status = str(value.get("status") or "").strip().lower().replace("_", " ")
    if status in {"available", "present", "success", "fetched"}:
        return "available"
    if status in {"not found", "missing", "absent", "404", "error", "failed"}:
        return "not found"
    if status in {"not checked", "notchecked", "not requested", "pending", ""}:
        attempts = value.get("checked_urls") or value.get("attempts")
        if isinstance(attempts, list) and attempts:
            return "not found"
        if value.get("http_status_code") or value.get("url"):
            return "not found"
        return "not checked"
    return "not checked"


def normalise_file_status(value: Any, *, include_chars: bool = False) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    status = _status_from_attempts(source)
    out: dict[str, Any] = {"status": status}
    url = source.get("url") or source.get("robots_url") or source.get("llms_url")
    if isinstance(url, str) and url:
        out["url"] = url
    if source.get("sitemap_entries_count") is not None:
        try:
            out["sitemap_entries_count"] = int(source.get("sitemap_entries_count") or 0)
        except Exception:
            pass
    if include_chars and source.get("chars") is not None:
        try:
            out["chars"] = int(source.get("chars") or 0)
        except Exception:
            pass
    return out


def _schema_types(page: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("schema_types", "schema_types_detected"):
        item = page.get(key)
        if isinstance(item, list):
            values.extend(item)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _has_any_structured_signal(page: dict[str, Any]) -> bool:
    if "json_ld_present" in page or "json_ld_block_count" in page:
        return True
    if _schema_types(page):
        return True
    crawl_status = str(page.get("crawl_status") or page.get("extraction_status") or "").lower()
    return crawl_status == "success" and ("schema_types" in page or "schema_types_detected" in page)


def _json_ld_block_count(page: dict[str, Any]) -> int:
    try:
        return int(page.get("json_ld_block_count") if page.get("json_ld_block_count") is not None else page.get("schema_block_count") or 0)
    except Exception:
        return 0


def structured_data_summary(owned_pages: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    pages = [p for p in owned_pages if isinstance(p, dict)]
    total = len(pages)
    checked = total > 0 and any(_has_any_structured_signal(p) for p in pages)
    pages_with_json_ld = 0
    pages_with_schema = 0
    schema_counts: dict[str, int] = {}
    missing: list[dict[str, Any]] = []

    for page in pages:
        json_ld_present = bool(page.get("json_ld_present") is True or _json_ld_block_count(page) > 0)
        schema_types = _schema_types(page)
        if json_ld_present:
            pages_with_json_ld += 1
        if json_ld_present or schema_types:
            pages_with_schema += 1
        for schema_type in schema_types:
            schema_counts[schema_type] = schema_counts.get(schema_type, 0) + 1
        if not json_ld_present and len(missing) < 20:
            row: dict[str, Any] = {}
            url = page.get("url") or page.get("final_url") or page.get("resolved_url") or page.get("source_url")
            title = page.get("title")
            if url:
                row["url"] = url
            if title:
                row["title"] = title
            missing.append(row)

    coverage = round((pages_with_json_ld / total) * 100, 1) if total else 0
    return (
        {
            "owned_pages_total": total,
            "pages_with_schema": pages_with_schema,
            "pages_with_json_ld": pages_with_json_ld,
            "coverage_pct": coverage,
            "schema_types_detected": sorted(schema_counts.items(), key=lambda x: x[1], reverse=True),
            "pages_missing_json_ld": missing,
        },
        checked,
    )


def _format_file_summary(name: str, status: str) -> str:
    if status == "available":
        return f"{name} is available"
    if status == "not found":
        return f"{name} was not found"
    return f"{name} was not checked"


def _format_pct(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "0"
    if number.is_integer():
        return str(int(number))
    return str(number)


def build_ai_discoverability_hygiene(
    *,
    owned_pages: list[dict[str, Any]] | None = None,
    robots_txt: dict[str, Any] | None = None,
    llms_txt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    robots = normalise_file_status(robots_txt)
    llms = normalise_file_status(llms_txt, include_chars=True)
    structured, structured_checked = structured_data_summary(owned_pages or [])
    coverage = float(structured.get("coverage_pct") or 0)

    any_not_checked = robots["status"] == "not checked" or llms["status"] == "not checked" or not structured_checked
    if any_not_checked or coverage < 50:
        priority = "high"
    elif coverage < 80:
        priority = "medium"
    else:
        priority = "low"

    if any_not_checked:
        summary = NOT_FULLY_CHECKED_SUMMARY
    else:
        summary = (
            f"JSON-LD/schema coverage: {structured['pages_with_json_ld']}/{structured['owned_pages_total']} "
            f"owned pages ({_format_pct(structured['coverage_pct'])}%). "
            f"{_format_file_summary('Robots.txt', robots['status'])}; "
            f"{_format_file_summary('LLMs.txt', llms['status'])}."
        )

    return {
        "schema_version": "ai_discoverability_hygiene.v1",
        "generated_at_epoch": now_epoch(),
        "priority": priority,
        "summary": summary,
        "robots_txt": robots,
        "llms_txt": llms,
        "structured_data": structured,
    }


def base_candidates(domain: str | None, owned_pages: list[dict[str, Any]], discovery: dict[str, Any] | None = None) -> list[str]:
    values: list[str] = []

    def add(url: Any) -> None:
        if not url:
            return
        try:
            parsed = urlparse(str(url))
            if parsed.scheme and parsed.netloc:
                candidate = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
                if candidate not in values:
                    values.append(candidate)
        except Exception:
            return

    add(domain)
    try:
        parsed_domain = urlparse(str(domain or ""))
        if parsed_domain.scheme and parsed_domain.netloc.startswith("www."):
            suffix = parsed_domain.netloc[4:]
            for prefix in ["www3", "www2"]:
                add(f"{parsed_domain.scheme}://{prefix}.{suffix}")
    except Exception:
        pass
    if discovery:
        for key in ["robots_url", "discovered_sitemaps", "candidates", "sitemaps_fetched"]:
            value = discovery.get(key)
            if isinstance(value, list):
                for item in value:
                    add(item.get("url") if isinstance(item, dict) else item)
            else:
                add(value)
    for page in owned_pages:
        if isinstance(page, dict):
            add(page.get("resolved_url") or page.get("final_url") or page.get("url") or page.get("source_url"))
    return values


def fetch_file_status(domain: str | None, owned_pages: list[dict[str, Any]], discovery: dict[str, Any] | None, path: str) -> dict[str, Any]:
    headers = {"User-Agent": "ai-visibility-evidence-service/3.5.3"}
    bases = base_candidates(domain, owned_pages, discovery)
    if not bases:
        return {"status": "not checked"}
    attempts: list[dict[str, Any]] = []
    for base in bases:
        url = base.rstrip("/") + path
        try:
            response = requests.get(url, timeout=15, headers=headers)
            chars = len(response.text or "")
            attempts.append({"url": url, "http_status_code": response.status_code, "chars": chars})
            if response.status_code < 400 and response.text.strip():
                return {"status": "available", "url": url, "http_status_code": response.status_code, "chars": chars, "checked_urls": attempts}
        except Exception as exc:
            attempts.append({"url": url, "error": str(exc)[:240]})
    return {"status": "not found", "url": attempts[0].get("url") if attempts else "", "checked_urls": attempts}


def check_site_ai_hygiene(domain: str | None, owned_pages: list[dict[str, Any]], discovery: dict[str, Any] | None = None) -> dict[str, Any]:
    robots = fetch_file_status(domain, owned_pages, discovery, "/robots.txt")
    if discovery:
        robots["sitemap_entries_count"] = len(discovery.get("candidates") or [])
    llms = fetch_file_status(domain, owned_pages, discovery, "/llms.txt")
    return build_ai_discoverability_hygiene(owned_pages=owned_pages, robots_txt=robots, llms_txt=llms)


def owned_pages_from_run_dir(run_dir: Path) -> list[dict[str, Any]]:
    owned_full = read_json(run_dir / "owned_pages_full.json", {}) or {}
    if isinstance(owned_full, dict) and isinstance(owned_full.get("pages"), list):
        return [p for p in owned_full.get("pages") if isinstance(p, dict)]
    bodhi = read_json(run_dir / "bodhi_bundle.json", {}) or {}
    owned_payload = bodhi.get("owned_pages_full") if isinstance(bodhi, dict) else {}
    if isinstance(owned_payload, dict) and isinstance(owned_payload.get("pages"), list):
        return [p for p in owned_payload.get("pages") if isinstance(p, dict)]
    return []


def hygiene_from_run_dir(run_dir: Path) -> dict[str, Any]:
    existing = read_json(run_dir / "site_ai_hygiene.json", {}) or {}
    pages = owned_pages_from_run_dir(run_dir)
    if is_valid_hygiene(existing):
        return build_ai_discoverability_hygiene(
            owned_pages=pages,
            robots_txt=existing.get("robots_txt"),
            llms_txt=existing.get("llms_txt"),
        )
    bodhi = read_json(run_dir / "bodhi_bundle.json", {}) or {}
    if isinstance(bodhi, dict):
        nested = bodhi.get("ai_discoverability_hygiene") or bodhi.get("site_ai_hygiene")
        if is_valid_hygiene(nested):
            return build_ai_discoverability_hygiene(
                owned_pages=pages,
                robots_txt=nested.get("robots_txt"),
                llms_txt=nested.get("llms_txt"),
            )
    return build_ai_discoverability_hygiene(owned_pages=pages)
