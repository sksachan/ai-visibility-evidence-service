import argparse
import json
import shutil
import zipfile
from pathlib import Path


REQUIRED_FILES = {
    "audit_context.json": [
        "outputs/audit_context/audit_context.json",
        "inputs/audit_context.json"
    ],
    "evidence_scope.json": [
        "outputs/evidence_scope/evidence_scope.json"
    ],
    "google_ai_mode_compact.json": [
        "outputs/google_ai_mode/google_ai_mode_compact.json"
    ],
    "owned_pages_full.json": [
        "outputs/content_intelligence/owned_pages_full.json"
    ],
    "external_pages_full.json": [
        "outputs/external_pages/external_pages_full.json"
    ],
    "visibility_matrix.json": [
        "outputs/visibility/visibility_matrix.json"
    ],
    "source_classification.json": [
        "outputs/source_landscape/source_classification.json"
    ]
}


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def find_first(root: Path, candidates):
    for rel in candidates:
        path = root / rel
        if path.exists():
            return path
    return None


def normalise_key(value: str) -> str:
    return value.lower().replace(" ", "_")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, help="Path to latest successful run zip")
    parser.add_argument("--brand", default="Nissan")
    parser.add_argument("--market", default="Japan")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--data-dir", default="/data/evidence-runs")
    args = parser.parse_args()

    zip_path = Path(args.zip).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()

    if not zip_path.exists():
        raise FileNotFoundError(f"Zip not found: {zip_path}")

    run_id = args.run_id or f"{normalise_key(args.brand)}_{normalise_key(args.market)}_seed"
    run_dir = data_dir / run_id
    latest_dir = data_dir / "latest"

    tmp_dir = data_dir / "_tmp_seed_extract"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp_dir)

    # Handle zips that contain a nested project directory.
    possible_roots = [tmp_dir] + [p for p in tmp_dir.iterdir() if p.is_dir()]
    root = None
    for candidate in possible_roots:
        if (candidate / "outputs").exists() or (candidate / "inputs").exists():
            root = candidate
            break

    if root is None:
        raise FileNotFoundError("Could not find outputs/ or inputs/ inside the zip")

    run_dir.mkdir(parents=True, exist_ok=True)

    copied = {}
    missing = []

    for output_name, candidates in REQUIRED_FILES.items():
        src = find_first(root, candidates)
        if src:
            dest = run_dir / output_name
            shutil.copyfile(src, dest)
            copied[output_name] = str(dest)
        else:
            missing.append(output_name)

    audit_context = read_json(run_dir / "audit_context.json", {})
    google_ai = read_json(run_dir / "google_ai_mode_compact.json", {})
    owned_pages = read_json(run_dir / "owned_pages_full.json", {})
    external_pages = read_json(run_dir / "external_pages_full.json", {})
    visibility = read_json(run_dir / "visibility_matrix.json", {})
    source_classification = read_json(run_dir / "source_classification.json", {})
    evidence_scope = read_json(run_dir / "evidence_scope.json", {})

    compact_bundle = {
        "status": "ready",
        "run_id": run_id,
        "brand": args.brand,
        "market": args.market,
        "files": {
            "audit_context": audit_context,
            "evidence_scope": evidence_scope,
            "google_ai_mode_compact": google_ai,
            "owned_pages_full": owned_pages,
            "external_pages_full": external_pages,
            "visibility_matrix": visibility,
            "source_classification": source_classification
        },
        "counts": {
            "missing_required_files": len(missing)
        },
        "missing_files": missing
    }

    (run_dir / "compact_bundle.json").write_text(
        json.dumps(compact_bundle, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    manifest = {
        "run_id": run_id,
        "brand": args.brand,
        "market": args.market,
        "status": "ready" if not missing else "partial",
        "copied_files": copied,
        "missing_files": missing,
        "compact_bundle": str(run_dir / "compact_bundle.json")
    }

    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_key = f"{normalise_key(args.brand)}_{normalise_key(args.market)}.json"
    (latest_dir / latest_key).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
