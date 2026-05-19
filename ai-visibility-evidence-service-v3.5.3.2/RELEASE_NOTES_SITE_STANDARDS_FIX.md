# Evidence Service v3.5.3.2

Patch focus: explicit AI discoverability and page technical-signal pass-through.

Changes:
- Preserves domain-level `site_standards` including robots.txt and llms.txt status/URL fields.
- Preserves page-level technical signals: `json_ld_present`, `json_ld_block_count`, `schema_types_detected`, `canonical_present`, and `meta_description_present`.
- Keeps JSON-LD/schema detection separate from GEO scoring, so the UI does not infer structured data from readiness score.
- Maintains backwards-compatible paths for existing report bundles and Bodhi compact payloads.

Validation performed in sandbox:
- Python source compilation passed for Evidence Service and Auditor.
- TypeScript compilation passed for Frontend using `npx tsc -b`.
- Full frontend Vite build could not be completed in the sandbox because the uploaded `node_modules` was missing the Linux Rolldown optional native binding. Running `npm ci && npm run build` on Railway/Linux should resolve this.
