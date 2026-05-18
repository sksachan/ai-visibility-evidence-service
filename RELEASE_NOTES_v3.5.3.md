# Evidence Service v3.5.3

Separates site-level owned inventory GEO audit from query-to-owned URL mapping.

## Changes
- Adds `max_owned_inventory_urls` request field while preserving `max_owned_urls` compatibility.
- Selects a broad owned URL inventory from sitemap/robots/common sitemap discovery, prioritising query-mapped URLs and filling the remaining audit set with meaningful owned pages.
- Keeps `max_owned_pages_per_query` strictly for query mapping, CMS, gap and opportunity logic.
- Writes `inventory_source`, `site_inventory_audit` and `query_mapped` flags on owned page records.
- Adds status/count telemetry: `owned_inventory_selected`, `owned_query_mapped_unique`, `owned_inventory_pages`.
- Passes the new cap through Auditor HITL payload for observability without requiring a workflow change.
