# Evidence Service v3.5.3.1

- Adds deterministic category-balanced query selection when `query_limit` is lower than the stored portfolio size.
- Replaces legacy first-N slicing for full-refresh evidence capture.
- Preserves configurability through request fields: `query_selection_strategy`, `query_selection_min_non_branded_pct`, `query_selection_min_competitor_pct`, `query_selection_min_local_count`, and `query_selection_min_ownership_count`.
- Writes `selected_query_portfolio.json` for auditability and includes `query_selection` telemetry in run status.
- Keeps `query_selection_strategy=sequential` / `first_n` / `legacy` available for backwards-compatible testing.
