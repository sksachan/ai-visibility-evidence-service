import os
import tempfile
import unittest
import importlib.util
from pathlib import Path

from app.ai_hygiene import NOT_FULLY_CHECKED_SUMMARY, build_ai_discoverability_hygiene


class AiHygieneHelperTests(unittest.TestCase):
    def test_not_found_is_checked_summary(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "json_ld_present": False,
                    "json_ld_block_count": 0,
                    "schema_types_detected": [],
                }
            ],
            robots_txt={"status": "available", "url": "https://example.com/robots.txt"},
            llms_txt={"status": "not found", "url": "https://example.com/llms.txt"},
        )
        self.assertEqual(hygiene["priority"], "high")
        self.assertNotEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)
        self.assertIn("0/1 owned pages (0%)", hygiene["summary"])
        self.assertEqual(hygiene["llms_txt"]["status"], "not found")

    def test_absent_structured_fields_are_not_checked(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[{"url": "https://example.com/a", "title": "A"}],
            robots_txt={"status": "available"},
            llms_txt={"status": "not found", "checked_urls": [{"url": "https://example.com/llms.txt", "http_status_code": 404}]},
        )
        self.assertEqual(hygiene["priority"], "high")
        self.assertEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)
        self.assertEqual(hygiene["structured_data"]["coverage_pct"], 0)

    def test_failed_default_empty_schema_is_not_checked(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[{"url": "https://example.com/a", "crawl_status": "failed", "schema_types_detected": []}],
            robots_txt={"status": "available"},
            llms_txt={"status": "available"},
        )
        self.assertEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)

    def test_geo_scores_do_not_affect_json_ld_counts(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "json_ld_present": False,
                    "json_ld_block_count": 0,
                    "schema_types_detected": [],
                    "geo_dimensions": {"structured_data": 100},
                },
                {
                    "url": "https://example.com/b",
                    "title": "B",
                    "json_ld_present": True,
                    "json_ld_block_count": 1,
                    "schema_types": ["Product", "FAQPage"],
                    "geo_dimensions": {"structured_data": 0},
                },
            ],
            robots_txt={"status": "available"},
            llms_txt={"status": "available"},
        )
        structured = hygiene["structured_data"]
        self.assertEqual(structured["pages_with_json_ld"], 1)
        self.assertEqual(structured["pages_with_schema"], 1)
        self.assertEqual(structured["coverage_pct"], 50.0)
        self.assertEqual(structured["schema_types_detected"], [("Product", 1), ("FAQPage", 1)])
        self.assertEqual(structured["pages_missing_json_ld"], [{"url": "https://example.com/a", "title": "A"}])


class ReportStoreHygieneTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_store_and_latest_inject_hygiene(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "evidence_nissan_japan_test"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "owned_pages_full.json").write_text(
                '{"pages":[{"url":"https://www.nissan.co.jp/a","title":"A","json_ld_present":false,"json_ld_block_count":0,"schema_types_detected":[]}]}',
                encoding="utf-8",
            )
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "test"}],
            }

            client = TestClient(main.app)
            response = client.post(f"/runs/{run_id}/report-bundle", json=bundle)
            self.assertEqual(response.status_code, 200)
            latest = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"})
            self.assertEqual(latest.status_code, 200)
            payload = latest.json()
            self.assertIn("ai_discoverability_hygiene", payload)
            hygiene = payload["ai_discoverability_hygiene"]
            self.assertEqual(hygiene["robots_txt"]["status"], "not checked")
            self.assertEqual(hygiene["llms_txt"]["status"], "not checked")
            self.assertEqual(hygiene["structured_data"]["pages_with_json_ld"], 0)

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_existing_hygiene_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "evidence_preserve_test"
            existing = {
                "priority": "low",
                "summary": "custom",
                "robots_txt": {"status": "available"},
                "llms_txt": {"status": "available"},
                "structured_data": {
                    "owned_pages_total": 1,
                    "pages_with_schema": 1,
                    "pages_with_json_ld": 1,
                    "coverage_pct": 100,
                },
            }
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "test"}],
                "ai_discoverability_hygiene": existing,
            }

            client = TestClient(main.app)
            response = client.post(f"/runs/{run_id}/report-bundle", json=bundle)
            self.assertEqual(response.status_code, 200)
            latest = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"})
            self.assertEqual(latest.status_code, 200)
            self.assertEqual(latest.json()["ai_discoverability_hygiene"], existing)


if __name__ == "__main__":
    unittest.main()
