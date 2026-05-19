from __future__ import annotations

import asyncio
import hashlib
import json
import re
import traceback
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.async_api import async_playwright

try:
    from markdownify import markdownify as md_convert
except Exception:  # pragma: no cover - fallback for minimal environments
    md_convert = None

from lib import resolve_path, write_json, safe_slug

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "ja,en-GB;q=0.9,en;q=0.8",
}

BLOCKED_STATUS_CODES = {401, 403, 429}
PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}

# This module intentionally follows a Firecrawl-like local pattern:
# - render/capture the full page, not only the main content
# - keep header/footer/nav/site-standard evidence
# - convert rendered HTML to markdown deterministically
# - parse PDFs only when the target URL itself is a PDF
# - never call paid APIs


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_block(text: str) -> str:
    text = text or ""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalise_for_dedupe(text: str) -> str:
    text = compact_block(text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[＊※*¹²³0-9\s]+", " ", text)
    return text.lower().strip()


def file_slug(url: str) -> str:
    parsed = urlparse(url or "")
    base = re.sub(r"[^a-zA-Z0-9]+", "_", f"{parsed.netloc}_{parsed.path}".strip("_"))[:90]
    h = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]
    return f"{base or safe_slug(url)}_{h}"


def normalise_scrape_item(item: Any) -> Dict[str, Any]:
    """Accept either a dict row or a raw URL string from upstream scope builders."""
    if isinstance(item, dict):
        row = dict(item)
    elif isinstance(item, str):
        row = {"url": item}
    else:
        row = {"url": str(item or "")}

    url = (
        row.get("url")
        or row.get("source_url")
        or row.get("page_url")
        or row.get("target_page")
        or row.get("link")
        or row.get("href")
        or ""
    )
    row["url"] = str(url)
    return row


def is_probable_pdf_url(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return path.endswith(".pdf")


def jp_aware_wordish_count(text: str) -> int:
    latin = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", text or "")
    cjk = re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text or "")
    return len(latin) + max(0, len(cjk) // 2)


def dedupe_markdown(markdown: str) -> str:
    # Dedupe exact/repeated boilerplate while keeping labelled regions.
    seen = set()
    out: List[str] = []
    for raw in (markdown or "").splitlines():
        line = compact_block(raw)
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        key = normalise_for_dedupe(line)
        if line.startswith("#"):
            key = "heading:" + key
        if len(key) < 2:
            continue
        if key not in seen:
            seen.add(key)
            out.append(line)
    return compact_block("\n".join(out))


def _absolute_url(url: str, base_url: str) -> str:
    if not url:
        return ""
    return urljoin(base_url, url)


def _clone_soup_fragment(node: Tag | None) -> str:
    return str(node) if node else ""


def _remove_active_noise(soup: BeautifulSoup) -> None:
    # Keep header/nav/footer. Remove only non-content execution/tracking layers.
    # Defensive guards are required because BeautifulSoup can leave invalidated
    # descendant tags with attrs=None after parent nodes are decomposed.
    for tag in list(soup.find_all(["script", "style", "template"])):
        try:
            if isinstance(tag, Tag) and tag.attrs is not None:
                tag.decompose()
        except Exception:
            continue

    for tag in list(soup.find_all(["iframe", "video", "canvas"])):
        try:
            if not isinstance(tag, Tag) or tag.attrs is None:
                continue
            alt = tag.get("title") or tag.get("aria-label") or ""
            if alt:
                tag.replace_with(soup.new_string(f" Embedded media: {alt} "))
            else:
                tag.decompose()
        except Exception:
            continue

    for tag in list(soup.find_all(attrs={"aria-hidden": "true"})):
        try:
            if not isinstance(tag, Tag) or tag.attrs is None:
                continue
            if len(tag.get_text(" ", strip=True)) < 80:
                tag.decompose()
        except Exception:
            continue

    noisy_re = re.compile(
        r"(cookie|consent|onetrust|didomi|ad-|ads-|advert|newsletter-popup|modal|overlay|tracking|analytics)",
        re.I,
    )

    to_remove = []
    for tag in list(soup.find_all(True)):
        try:
            if not isinstance(tag, Tag) or tag.attrs is None:
                continue
            classes = " ".join(tag.get("class") or [])
            ident = tag.get("id") or ""
            if noisy_re.search(f"{classes} {ident}") and len(tag.get_text(" ", strip=True)) < 500:
                to_remove.append(tag)
        except Exception:
            continue

    for tag in to_remove:
        try:
            if isinstance(tag, Tag) and tag.attrs is not None:
                tag.decompose()
        except Exception:
            continue

def _make_links_absolute(soup: BeautifulSoup, base_url: str) -> None:
    for tag, attr in [("a", "href"), ("img", "src"), ("source", "src"), ("link", "href")]:
        for node in soup.find_all(tag):
            val = node.get(attr)
            if val and not str(val).startswith(("data:", "mailto:", "tel:", "javascript:")):
                node[attr] = _absolute_url(str(val), base_url)
    for img in soup.find_all("img"):
        for attr in ["data-src", "data-original", "data-lazy-src"]:
            if img.get(attr) and not img.get("src"):
                img["src"] = _absolute_url(str(img.get(attr)), base_url)


def _html_to_markdown(html: str, base_url: str = "") -> str:
    if not html:
        return ""
    if md_convert:
        try:
            return compact_block(md_convert(
                html,
                heading_style="ATX",
                bullets="-",
                strip=["script", "style"],
                convert=["a", "abbr", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "strong", "table", "td", "th", "tr", "ul"],
            ))
        except Exception:
            pass
    soup = BeautifulSoup(html, "html.parser")
    return compact_block(soup.get_text("\n", strip=True))


def _extract_region_html(soup: BeautifulSoup, selectors: List[str]) -> str:
    nodes: List[Tag] = []
    for selector in selectors:
        try:
            for node in soup.select(selector):
                if node and isinstance(node, Tag) and node not in nodes:
                    nodes.append(node)
        except Exception:
            continue
    return "\n".join(_clone_soup_fragment(n) for n in nodes)


def _extract_main_html(soup: BeautifulSoup) -> str:
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    if main:
        return str(main)
    # If no semantic main, keep the body. This is closer to Firecrawl only_main_content=False.
    return str(soup.body or soup)


def _extract_all_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for a in soup.find_all("a"):
        href = _absolute_url(str(a.get("href") or ""), base_url)
        label = compact_block(a.get_text(" ", strip=True) or href)
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        key = href.split("#")[0], label[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": href, "text": label, "is_pdf": ".pdf" in href.lower()})
    return out


def _extract_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        src = _absolute_url(str(src), base_url) if src else ""
        alt = compact_block(img.get("alt") or img.get("title") or "")
        if not alt and not src:
            continue
        key = (src, alt)
        if key in seen:
            continue
        seen.add(key)
        out.append({"src": src, "alt": alt})
    return out


def _table_to_markdown(table: Tag) -> str:
    rows: List[List[str]] = []
    for tr in table.find_all("tr"):
        cells = [compact_block(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = rows[0]
    sep = ["---"] * width
    body = rows[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for row in body[:80]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _extract_tables_markdown(soup: BeautifulSoup) -> str:
    chunks = []
    for i, table in enumerate(soup.find_all("table"), 1):
        md = _table_to_markdown(table)
        if md:
            chunks.append(f"### Table {i}\n\n{md}")
    return "\n\n".join(chunks)


def _extract_schema_and_metadata(soup: BeautifulSoup, url: str, status_code: int | None, content_type: str = "") -> Dict[str, Any]:
    title = compact_block(soup.title.get_text(" ", strip=True) if soup.title else "")
    desc = ""
    meta_desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta_desc:
        desc = compact_block(meta_desc.get("content") or "")
    lang = ""
    if soup.html:
        lang = compact_block(soup.html.get("lang") or "")
    canonical = ""
    can = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
    if can:
        canonical = str(can.get("href") or "")
    robots = ""
    robots_meta = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    if robots_meta:
        robots = compact_block(robots_meta.get("content") or "")
    hreflang = []
    for link in soup.find_all("link", attrs={"hreflang": True}):
        hreflang.append({"hreflang": link.get("hreflang"), "href": link.get("href")})

    json_ld_blocks = []
    schema_types: List[str] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            json_ld_blocks.append(parsed)
            stack = parsed if isinstance(parsed, list) else [parsed]
            while stack:
                obj = stack.pop(0)
                if isinstance(obj, dict):
                    typ = obj.get("@type")
                    if isinstance(typ, list):
                        schema_types.extend(str(t) for t in typ)
                    elif typ:
                        schema_types.append(str(typ))
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(obj, list):
                    stack.extend(obj)
        except Exception:
            # Keep raw schema signal even if JSON is malformed.
            if "@type" in raw:
                schema_types.extend(re.findall(r'"@type"\s*:\s*"([^"]+)"', raw))
    schema_types = list(dict.fromkeys(schema_types))[:40]
    return {
        "title": title,
        "description": desc,
        "language": lang,
        "sourceURL": url,
        "statusCode": status_code,
        "contentType": content_type,
        "canonical": canonical,
        "robots": robots,
        "hreflang": hreflang[:40],
        "json_ld_present": bool(json_ld_blocks),
        "schema_types": schema_types,
        "json_ld_block_count": len(json_ld_blocks),
        "schema_block_count": len(json_ld_blocks),
    }


def _extract_visible_dates(text: str) -> List[str]:
    patterns = [
        r"\b20[12][0-9][/-](?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12][0-9]|3[01])\b",
        r"\b20[12][0-9]\s*(?:年|/|-)\s*(?:0?[1-9]|1[0-2])\s*(?:月|/|-)\s*(?:0?[1-9]|[12][0-9]|3[01])\s*日?",
        r"\b(?:last updated|updated|published|reviewed|valid until|effective date)[:\s]+[^\n]{0,80}",
        r"(?:更新日|掲載日|公開日|有効期限|改定日)[:：\s]*[^\n]{0,80}",
        r"令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日",
    ]
    out: List[str] = []
    for pat in patterns:
        out.extend(compact_block(x) for x in re.findall(pat, text or "", flags=re.I))
    return list(dict.fromkeys([x for x in out if x]))[:30]


def _pdf_text_from_bytes(data: bytes, max_pages: int = 80) -> Tuple[str, Dict[str, Any]]:
    meta = {"pdf_parser": "pypdf", "pages_parsed": 0, "ocr_required": False, "error": None}
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(BytesIO(data))
        chunks = []
        for i, page in enumerate(reader.pages[:max_pages], 1):
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            if txt.strip():
                chunks.append(f"## PDF page {i}\n\n{txt.strip()}")
        meta["pages_parsed"] = min(len(reader.pages), max_pages)
        text = compact_block("\n\n".join(chunks))
        if len(text) < 200 and len(reader.pages) > 0:
            meta["ocr_required"] = True
        return text, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        meta["ocr_required"] = True
        return "", meta


def fetch_static_resource(url: str, timeout: int = 30) -> Tuple[bytes, Dict[str, Any]]:
    meta = {"status_code": None, "final_url": None, "error": None, "blocked": False, "content_type": ""}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, verify=False, headers=HEADERS) as client:
            r = client.get(url)
            meta["status_code"] = r.status_code
            meta["final_url"] = str(r.url)
            meta["content_type"] = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            meta["blocked"] = r.status_code in BLOCKED_STATUS_CODES
            if r.status_code >= 400:
                return b"", meta
            return r.content, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return b"", meta


async def fetch_playwright_html(context: Any, url: str, timeout_ms: int = 60000) -> Tuple[str, Dict[str, Any]]:
    meta = {"status_code": None, "final_url": None, "title": None, "error": None, "blocked": False}
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(2500)
        # Full-page scroll to trigger lazy blocks, accordions/images and SPA sections.
        for _ in range(12):
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(300)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        meta["status_code"] = response.status if response else None
        meta["final_url"] = page.url
        meta["title"] = await page.title()
        meta["blocked"] = meta["status_code"] in BLOCKED_STATUS_CODES
        html = await page.content()
        return html, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return "", meta
    finally:
        await page.close()


def _build_full_page_markdown(url: str, raw_html: str, status_code: int | None = None, content_type: str = "") -> Tuple[str, str, Dict[str, Any]]:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    _make_links_absolute(soup, url)
    metadata = _extract_schema_and_metadata(soup, url, status_code, content_type)

    # Build cleaned HTML after metadata extraction so schema blocks can be preserved in manifest.
    clean_soup = BeautifulSoup(str(soup), "html.parser")
    _remove_active_noise(clean_soup)
    _make_links_absolute(clean_soup, url)

    header_html = _extract_region_html(clean_soup, ["header", "nav", "[role=navigation]"])
    footer_html = _extract_region_html(clean_soup, ["footer"])
    main_html = _extract_main_html(clean_soup)
    tables_md = _extract_tables_markdown(clean_soup)
    links = _extract_all_links(clean_soup, url)
    images = _extract_images(clean_soup, url)

    header_md = _html_to_markdown(header_html, url)
    main_md = _html_to_markdown(main_html, url)
    footer_md = _html_to_markdown(footer_html, url)

    image_lines = []
    for img in images[:300]:
        alt = img.get("alt") or "Image"
        src = img.get("src") or ""
        if src:
            image_lines.append(f"![{alt}]({src})")
        else:
            image_lines.append(f"- Image: {alt}")
    links_lines = [f"- [{l.get('text') or l.get('url')}]({l.get('url')})" for l in links[:500]]

    schema_lines = []
    if metadata.get("schema_types"):
        schema_lines.append("Schema types: " + ", ".join(metadata["schema_types"]))
    if metadata.get("json_ld_present"):
        schema_lines.append(f"JSON-LD blocks detected: {metadata.get('schema_block_count')}")
    if metadata.get("canonical"):
        schema_lines.append(f"Canonical URL: {metadata.get('canonical')}")
    if metadata.get("robots"):
        schema_lines.append(f"Robots meta: {metadata.get('robots')}")
    if metadata.get("description"):
        schema_lines.append(f"Meta description: {metadata.get('description')}")

    parts = [
        f"# {metadata.get('title') or url}",
        "## Metadata and technical signals\n" + "\n".join(schema_lines),
        "## Header and navigation signals\n" + header_md,
        "## Main page content\n" + main_md,
        "## Tables and comparison evidence\n" + tables_md,
        "## Image evidence\n" + "\n".join(image_lines),
        "## All extracted links\n" + "\n".join(links_lines),
        "## Footer and site-standard signals\n" + footer_md,
    ]
    markdown = dedupe_markdown("\n\n".join(p for p in parts if compact_block(p)))
    cleaned_html = str(clean_soup)

    body_text = clean_soup.get_text("\n", strip=True)
    visible_dates = _extract_visible_dates(body_text)
    pdf_links = [l for l in links if l.get("is_pdf")]
    region_metrics = {
        "header_chars": len(header_md),
        "main_chars": len(main_md),
        "footer_chars": len(footer_md),
        "tables_chars": len(tables_md),
        "metadata_chars": len("\n".join(schema_lines)),
        "full_markdown_chars": len(markdown),
    }
    extra = {
        "metadata": metadata,
        "links": links,
        "images": images,
        "pdf_links": pdf_links,
        "visible_dates": visible_dates,
        "content_regions": region_metrics,
    }
    return markdown, cleaned_html, extra


def markdown_metrics(markdown: str) -> Dict[str, Any]:
    markdown = markdown or ""
    links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", markdown)
    images = re.findall(r"!\[[^\]]*\]\((https?://[^)]+)\)", markdown)
    headings = re.findall(r"(?m)^#{1,6}\s+\S+", markdown)
    bullets = re.findall(r"(?m)^\s*[-*•]\s+\S+", markdown)
    table_lines = [ln for ln in markdown.splitlines() if ln.count("|") >= 2]
    pdf_links = [x for x in links if ".pdf" in x.lower()]
    numeric = re.findall(
        r"(?<!\w)(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s?[%％]|km|km/L|kWh|kW|万円|円|年|月|日|時間|分|L|kg|mm|人|台|miles|hours|minutes|APR|%)?",
        markdown,
        flags=re.I,
    )
    questions = re.findall(r"[^\n。.!?]{6,140}[?？]", markdown)
    return {
        "markdown_chars": len(markdown),
        "wordish_count": jp_aware_wordish_count(markdown),
        "heading_count": len(headings),
        "bullet_count": len(bullets),
        "link_count": len(links),
        "image_count": len(images),
        "pdf_link_count": len(pdf_links),
        "table_like": len(table_lines) >= 3,
        "table_line_count": len(table_lines),
        "raw_url_count": len(re.findall(r"https?://", markdown)),
        "numeric_fact_count": len(numeric),
        "question_count": len(questions),
        "unique_line_count": len(set(normalise_for_dedupe(x) for x in markdown.splitlines() if normalise_for_dedupe(x))),
        "numeric_facts_sample": list(dict.fromkeys(numeric))[:25],
        "links_sample": list(dict.fromkeys(links))[:25],
        "image_alt_count": len(re.findall(r"!\[([^\]]+)\]", markdown)),
    }


def detect_geo_signals(markdown: str, html: str = "", extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    extra = extra or {}
    text = compact_block(markdown)
    lower = text.lower()
    m = markdown_metrics(text)
    metadata = extra.get("metadata") or {}
    schema_types = list(dict.fromkeys(metadata.get("schema_types") or []))
    json_ld_present = bool(metadata.get("json_ld_present"))
    canonical = bool(metadata.get("canonical"))
    meta_description = bool(metadata.get("description"))
    robots = str(metadata.get("robots") or "").lower()
    robots_indexable = "noindex" not in robots
    hreflang = len(metadata.get("hreflang") or [])
    pdf_links = extra.get("pdf_links") or []
    visible_dates = extra.get("visible_dates") or []
    regions = extra.get("content_regions") or {}

    citation_signal = m["link_count"] >= 3 or bool(re.search(r"(source|citation|reference|出典|参照|引用|公式|によると|review|guide|rating|調査|データ|according to)", text, re.I))
    # Domain-neutral topic depth terms for automotive/ownership/finance/EV pages. No Nissan-specific boost.
    domain_terms = bool(re.search(r"(vehicle|car|ev|electric|battery|range|charging|hybrid|fuel|warranty|service|maintenance|finance|lease|price|cost|safety|adas|interior|seat|boot|cargo|dealer|roadside|車|自動車|電気自動車|充電|航続|バッテリー|燃費|保証|整備|点検|価格|費用|安全|内装|座席|荷室|販売店)", text, re.I))
    comparison_terms = bool(re.search(r"(compare|comparison|versus|vs\.?|difference|pros|cons|review|best|rating|table|比較|違い|メリット|デメリット|グレード|標準装備|オプション|おすすめ)", text, re.I))
    actionable_terms = bool(re.search(r"(choose|check|guide|tips|buy|price|estimate|book|apply|contact|選ぶ|確認|ガイド|ポイント|購入|見積|予約|問い合わせ)", text, re.I))
    freshness_terms = bool(visible_dates) or bool(re.search(r"(updated|last updated|reviewed|valid until|as of|202[4-9]|更新|掲載日|改定|時点|現在|有効期限|copyright|©)", text, re.I))
    faq_like = bool(re.search(r"(faq|q&a|よくある質問|質問|お問い合わせ|ご質問|question|answer)", text, re.I)) or m["question_count"] >= 2
    image_evidence = m["image_count"] >= 3
    important_links = m["pdf_link_count"] >= 1 or bool(re.search(r"(terms|conditions|privacy|sitemap|contact|warranty|brochure|catalogue|spec|specification|pdf|主要装備一覧|諸元表|カタログ|保証|条件)", text, re.I))
    answer_first = bool(text[:1200]) and m["heading_count"] >= 1 and (domain_terms or len(text[:1200]) > 400)
    extractable_passages = (m["heading_count"] >= 2 and m["unique_line_count"] >= 15 and m["markdown_chars"] >= 900)

    nav_noise_terms = len(re.findall(r"(cookie|ad blocker|subscribe popup|consent management|utm_|fbclid|gclid)", lower, re.I))
    content_terms = len(re.findall(r"(vehicle|car|ev|charging|battery|range|hybrid|warranty|service|price|cost|review|safety|faq|車|充電|保証|整備|価格|安全)", lower, re.I))
    noise_ratio = round(nav_noise_terms / max(1, content_terms), 2)

    return {
        "answer_first": answer_first,
        "headings_present": m["heading_count"] >= 2,
        "extractable_passages": extractable_passages,
        "low_noise_ratio": noise_ratio <= 0.4,
        "faq_like": faq_like,
        "comparison_terms": comparison_terms,
        "actionable_terms": actionable_terms,
        "domain_specific_terms": domain_terms,
        "key_facts": m["numeric_fact_count"] >= 5,
        "statistics_or_numbers": m["numeric_fact_count"] >= 5,
        "source_citations": citation_signal,
        "internal_links": m["link_count"] >= 3,
        "external_links": len([u for u in m["links_sample"] if urlparse(u).netloc]) >= 1,
        "pdf_or_spec_links": important_links,
        "linked_pdf_count": len(pdf_links),
        "image_alt_evidence": image_evidence,
        "schema_json_ld": json_ld_present,
        "schema_types": schema_types,
        "product_schema": any(x.lower() == "product" for x in schema_types),
        "faq_schema": any(x.lower() == "faqpage" for x in schema_types),
        "offer_schema": any(x.lower() in {"offer", "offercatalog"} for x in schema_types),
        "organization_schema": any(x.lower() == "organization" for x in schema_types),
        "breadcrumb_schema": any(x.lower() == "breadcrumblist" for x in schema_types),
        "canonical": canonical,
        "meta_description": meta_description,
        "robots_indexable": robots_indexable,
        "hreflang_count": hreflang,
        "freshness_signals": freshness_terms,
        "visible_dates": visible_dates,
        "authority_signals": citation_signal or important_links or bool(regions.get("footer_chars", 0) > 200),
        "cta_links": bool(re.search(r"(book|estimate|contact|dealer|buy|apply|予約|見積|問い合わせ|販売店)", text, re.I)),
        "footer_present": regions.get("footer_chars", 0) > 50,
        "header_present": regions.get("header_chars", 0) > 50,
        "nav_noise_terms": nav_noise_terms,
        "content_terms": content_terms,
        "noise_ratio": noise_ratio,
    }


def extraction_quality(markdown: str, status_code: int | None, blocked: bool) -> Dict[str, Any]:
    m = markdown_metrics(markdown)
    score = 0.0
    score += 0.25 if m["markdown_chars"] >= 2500 else 0.15 if m["markdown_chars"] >= 1000 else 0.05 if m["markdown_chars"] >= 400 else 0
    score += 0.18 if m["heading_count"] >= 5 else 0.10 if m["heading_count"] >= 2 else 0
    score += 0.15 if m["link_count"] >= 10 else 0.08 if m["link_count"] >= 3 else 0
    score += 0.10 if m["image_count"] >= 3 else 0.03 if m["image_count"] >= 1 else 0
    score += 0.10 if m["numeric_fact_count"] >= 5 else 0.04 if m["numeric_fact_count"] >= 1 else 0
    score += 0.10 if m["table_like"] else 0
    score += 0.12 if status_code and 200 <= int(status_code) < 300 else 0
    score = round(min(1.0, score), 2)
    if blocked:
        status = "blocked"
        allowed = False
    elif score >= 0.45 and m["markdown_chars"] >= 900:
        status = "success"
        allowed = True
    elif m["markdown_chars"] >= 300:
        status = "partial"
        allowed = False
    else:
        status = "failed"
        allowed = False
    return {"extraction_quality_score": score, "extraction_status": status, "scoring_allowed": allowed}


def build_manifest(url: str, markdown: str, html: str, raw_html: str, static_meta: Dict[str, Any], rendered_meta: Dict[str, Any], extra: Dict[str, Any], kind: str, resource_type: str, source_meta: Dict[str, Any] | None = None, pdf_meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    status_code = rendered_meta.get("status_code") or static_meta.get("status_code")
    blocked = bool(static_meta.get("blocked") or rendered_meta.get("blocked") or status_code in BLOCKED_STATUS_CODES)
    q = extraction_quality(markdown, status_code, blocked)
    metrics = markdown_metrics(markdown)
    signals = detect_geo_signals(markdown, html or raw_html, extra)
    crawl_status = q["extraction_status"]
    return {
        "url": url,
        "kind": kind,
        "resource_type": resource_type,
        "scrape_method": "local_full_page_playwright",
        "paid_api_used": False,
        "firecrawl_used": False,
        "crawl_status": crawl_status,
        "extraction_status": crawl_status,
        "content_score_policy": "score" if q["scoring_allowed"] else "exclude_from_content_score",
        "geo_analysis_ready": q["scoring_allowed"],
        "extraction_quality_score": q["extraction_quality_score"],
        "extraction_metrics": metrics,
        "geo_signals": signals,
        "content_regions": extra.get("content_regions") or {},
        "metadata": extra.get("metadata") or {},
        "links": extra.get("links", [])[:500],
        "linked_pdfs": extra.get("pdf_links", [])[:80],
        "images": extra.get("images", [])[:300],
        "visible_dates": extra.get("visible_dates", []),
        "static_fetch": static_meta,
        "rendered_fetch": rendered_meta,
        "pdf_parser": pdf_meta or {},
        "timestamp_utc": now_iso(),
        "source_meta": source_meta or {},
    }


async def _scrape_html_item(context: Any, item: Dict[str, Any], timeout_ms: int) -> Tuple[str, str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    item = normalise_scrape_item(item)
    url = item.get("url", "")
    static_bytes, static_meta = fetch_static_resource(url)
    rendered_html, rendered_meta = await fetch_playwright_html(context, url, timeout_ms)
    raw_html = rendered_html or (static_bytes.decode("utf-8", errors="ignore") if static_bytes else "")
    markdown, clean_html, extra = _build_full_page_markdown(url, raw_html, rendered_meta.get("status_code") or static_meta.get("status_code"), static_meta.get("content_type", "")) if raw_html else ("", "", {})
    return markdown, clean_html, raw_html, static_meta, rendered_meta, extra


def _scrape_pdf_item(item: Dict[str, Any], max_pages: int = 80) -> Tuple[str, str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    item = normalise_scrape_item(item)
    url = item.get("url", "")
    data, static_meta = fetch_static_resource(url)
    pdf_text, pdf_meta = _pdf_text_from_bytes(data, max_pages=max_pages) if data else ("", {"error": static_meta.get("error"), "ocr_required": False})
    title = item.get("title") or url
    markdown = dedupe_markdown(f"# {title}\n\n## PDF target content\n\n{pdf_text}") if pdf_text else ""
    extra = {
        "metadata": {"title": title, "description": item.get("snippet", ""), "language": "", "sourceURL": url, "statusCode": static_meta.get("status_code"), "contentType": static_meta.get("content_type"), "canonical": url, "robots": "", "hreflang": [], "json_ld_present": False, "schema_types": [], "schema_block_count": 0},
        "links": [],
        "images": [],
        "pdf_links": [],
        "visible_dates": _extract_visible_dates(pdf_text),
        "content_regions": {"header_chars": 0, "main_chars": len(pdf_text), "footer_chars": 0, "tables_chars": 0, "metadata_chars": 0, "full_markdown_chars": len(markdown), "pdf_chars": len(pdf_text)},
    }
    rendered_meta = {"status_code": None, "final_url": None, "title": title, "error": None, "blocked": False}
    return markdown, "", "", static_meta, rendered_meta, extra, pdf_meta


async def scrape_items(items: List[Dict[str, Any]], output_root: str, kind: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = cfg or {}
    root = resolve_path(output_root)
    md_dir = root / "markdown"
    html_dir = root / "rendered_html"
    raw_dir = root / "raw_html"
    manifest_dir = root / "manifests"
    for d in [md_dir, html_dir, raw_dir, manifest_dir]:
        d.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    timeout_ms = int(cfg.get("playwright_timeout_ms", 60000))
    pdf_max_pages = int((cfg.get("pdf_parser") or {}).get("max_pages", 80)) if isinstance(cfg.get("pdf_parser"), dict) else int(cfg.get("pdf_max_pages", 80))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 1400}, locale="ja-JP", user_agent=HEADERS["User-Agent"])
        for idx, item in enumerate(items, 1):
            item = normalise_scrape_item(item)
            url = item.get("url", "")
            print(f"[{idx}/{len(items)}] local full-page {kind}: {url}", flush=True)
            slug = f"{idx:03d}_{file_slug(url)}"
            source_meta = {k: v for k, v in item.items() if k not in {"markdown"}}
            resource_type = "pdf" if is_probable_pdf_url(url) else "html"
            try:
                pdf_meta = {}
                if resource_type == "pdf":
                    markdown, clean_html, raw_html, static_meta, rendered_meta, extra, pdf_meta = _scrape_pdf_item(item, max_pages=pdf_max_pages)
                else:
                    markdown, clean_html, raw_html, static_meta, rendered_meta, extra = await _scrape_html_item(context, item, timeout_ms)
                manifest = build_manifest(url, markdown, clean_html, raw_html, static_meta, rendered_meta, extra, kind, resource_type, source_meta, pdf_meta)
                md_path = md_dir / f"{slug}.md"
                html_path = html_dir / f"{slug}.html"
                raw_path = raw_dir / f"{slug}.html"
                manifest_path = manifest_dir / f"{slug}.json"
                md_path.write_text(markdown or "", encoding="utf-8")
                html_path.write_text(clean_html or "", encoding="utf-8")
                raw_path.write_text(raw_html or "", encoding="utf-8")
                write_json(manifest_path, manifest)
                row = {
                    **item,
                    "url": url,
                    "scrape_method": manifest["scrape_method"],
                    "paid_api_used": False,
                    "firecrawl_used": False,
                    "resource_type": resource_type,
                    "crawl_status": manifest["crawl_status"],
                    "extraction_status": manifest["extraction_status"],
                    "content_score_policy": manifest["content_score_policy"],
                    "geo_analysis_ready": manifest["geo_analysis_ready"],
                    "extraction_quality_score": manifest["extraction_quality_score"],
                    "markdown_file": str(md_path.relative_to(resolve_path("."))),
                    "rendered_html_file": str(html_path.relative_to(resolve_path("."))),
                    "raw_html_file": str(raw_path.relative_to(resolve_path("."))),
                    "extraction_manifest_file": str(manifest_path.relative_to(resolve_path("."))),
                    "manifest_file": str(manifest_path.relative_to(resolve_path("."))),
                    "markdown_chars": len(markdown or ""),
                    "title": (extra.get("metadata") or {}).get("title") or item.get("title", ""),
                    "description": (extra.get("metadata") or {}).get("description") or item.get("snippet", ""),
                    "status_code": (extra.get("metadata") or {}).get("statusCode"),
                }
                results.append(row)
                if manifest["crawl_status"] in {"blocked", "failed", "partial"}:
                    failed.append({"url": url, "crawl_status": manifest["crawl_status"], "status_code": row.get("status_code"), "error": manifest.get("static_fetch", {}).get("error") or manifest.get("rendered_fetch", {}).get("error")})
            except Exception as e:
                tb = traceback.format_exc()
                print(tb, flush=True)
                failed.append({
                    "url": url,
                    "crawl_status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": tb
                })
        await context.close()
        await browser.close()
    return {"pages": results, "failed": failed}


def scrape_items_sync(items: List[Dict[str, Any]], output_root: str, kind: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return asyncio.run(scrape_items(items, output_root, kind, cfg))
