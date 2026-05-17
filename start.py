from __future__ import annotations

import os
import sys

import uvicorn


def _int_port(value: str | None, default: int = 8080) -> int:
    if not value:
        return default
    value = str(value).strip().strip('"').strip("'")
    try:
        return int(value)
    except ValueError:
        print(f"Invalid PORT value {value!r}; falling back to {default}", file=sys.stderr)
        return default


if __name__ == "__main__":
    port = _int_port(os.environ.get("PORT"), 8080)
    print(f"Starting AI Visibility Evidence Service on 0.0.0.0:{port}", flush=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level=os.environ.get("LOG_LEVEL", "info"))
