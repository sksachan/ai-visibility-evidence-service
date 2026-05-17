from __future__ import annotations

import json
import os
import time
from typing import Any

import requests


class BodhiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.getenv("BODHI_API_BASE_URL") or "https://psaisuite.com/save").rstrip("/")
        self.token = token or os.getenv("BODHI_PAT_TOKEN") or ""
        self.timeout = int(os.getenv("BODHI_HTTP_TIMEOUT_SECONDS", "120"))

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self.headers(), timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Bodhi {method} {path} failed: {resp.status_code} {resp.text[:500]}")
        if not resp.text:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}

    def trigger_task_run(self, task_id: str, workflow_id: str | None = None, run_name: str | None = None, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"runName": run_name or "AI Visibility run"}
        overrides: dict[str, Any] = {}
        if workflow_id:
            overrides["workflow"] = workflow_id
        if inputs:
            # Bodhi accepts overrides for workflow/input variables. Preserve both shapes to be tolerant.
            overrides.update(inputs)
            payload["inputs"] = inputs
        if overrides:
            payload["overrides"] = overrides
        return self._request("POST", f"/api/v1/tasks/{task_id}/runs", data=json.dumps(payload))

    def list_task_runs(self, task_id: str) -> Any:
        return self._request("GET", f"/api/v1/tasks/{task_id}/runs")

    def get_run_file(self, run_id: str, srcfile: str) -> Any:
        return self._request("GET", f"/api/v1/tasks/runs/{run_id}/files", params={"srcfile": srcfile})

    @staticmethod
    def extract_run_id(payload: Any) -> str | None:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return None
        candidates = [
            payload.get("run_id"), payload.get("runId"), payload.get("id"),
            (payload.get("data") or {}).get("run_id") if isinstance(payload.get("data"), dict) else None,
            (payload.get("data") or {}).get("runId") if isinstance(payload.get("data"), dict) else None,
            (payload.get("run") or {}).get("id") if isinstance(payload.get("run"), dict) else None,
        ]
        for c in candidates:
            if c:
                return str(c)
        return None

    @staticmethod
    def normalise_runs(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ["runs", "data", "items", "results"]:
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                if isinstance(val, dict):
                    for sub in ["runs", "items", "results"]:
                        arr = val.get(sub)
                        if isinstance(arr, list):
                            return [x for x in arr if isinstance(x, dict)]
        return []

    @staticmethod
    def run_status(run: dict[str, Any]) -> str:
        for key in ["status", "state", "runStatus"]:
            if run.get(key):
                return str(run[key]).lower()
        return "unknown"

    def wait_for_run(self, task_id: str, run_id: str, timeout_seconds: int = 900, poll_seconds: int = 10) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last: dict[str, Any] = {"run_id": run_id, "status": "unknown"}
        complete = {"completed", "success", "succeeded", "finished", "done"}
        failed = {"failed", "error", "cancelled", "canceled"}
        while time.time() < deadline:
            payload = self.list_task_runs(task_id)
            runs = self.normalise_runs(payload)
            for run in runs:
                rid = self.extract_run_id(run) or str(run.get("id") or "")
                if rid == run_id:
                    last = run
                    status = self.run_status(run)
                    if status in complete:
                        return run
                    if status in failed:
                        raise RuntimeError(f"Bodhi run failed: {run_id} status={status} payload={str(run)[:500]}")
            time.sleep(poll_seconds)
        raise TimeoutError(f"Timed out waiting for Bodhi run {run_id}. Last={str(last)[:500]}")
