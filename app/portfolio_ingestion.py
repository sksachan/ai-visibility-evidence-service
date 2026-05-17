from __future__ import annotations

import html
import json
import re
from typing import Any


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    for _ in range(4):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, str):
                text = parsed.strip()
                continue
            return parsed
        except Exception:
            break
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            return None
    return None


def _walk_candidates(obj: Any):
    yield obj
    if isinstance(obj, dict):
        # Prefer known Bodhi output locations first.
        for path in [
            ("result",), ("data", "result"), ("data", "result", "response"),
            ("data", "response"), ("response",), ("output",),
            ("data", "stdout"), ("stdout",),
            ("data", "layout"), ("layout",),
        ]:
            cur: Any = obj
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok:
                yield cur
        for v in obj.values():
            yield from _walk_candidates(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_candidates(item)


def is_valid_portfolio(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and str(obj.get("schema_version", "")).startswith("brand_topic_query_portfolio")
        and isinstance(obj.get("queries"), list) and len(obj.get("queries") or []) > 0
        and isinstance(obj.get("topics"), list) and len(obj.get("topics") or []) > 0
    )


def normalise_text_entities(value: Any) -> Any:
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, list):
        return [normalise_text_entities(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise_text_entities(v) for k, v in value.items()}
    return value


def extract_query_portfolio(payload: Any) -> dict[str, Any] | None:
    for candidate in _walk_candidates(payload):
        parsed = _maybe_json(candidate)
        if is_valid_portfolio(parsed):
            return normalise_text_entities(parsed)
        # Some persisted responses wrap under portfolio.
        if isinstance(parsed, dict) and is_valid_portfolio(parsed.get("portfolio")):
            return normalise_text_entities(parsed["portfolio"])
    return None
