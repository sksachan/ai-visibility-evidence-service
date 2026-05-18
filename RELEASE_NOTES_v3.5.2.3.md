# Evidence Service v3.5.2.3

## Purpose
Hardens integration with Brand Topic Query Portfolio Builder v1.2 before full-scale audits.

## Changes
- Submits all v1.2 Query Builder HITL UI-node fields explicitly:
  - planner_model
  - writer_model
  - search_api
  - number_of_queries
  - max_search_depth
  - url_mode
  - human_feedback
- Uses backend-managed DeepResearch defaults:
  - planner_model = gpt-5.2
  - writer_model = gpt-5.2
  - search_api = google
  - number_of_queries = 4
  - max_search_depth = 2
  - url_mode = domain
  - human_feedback = false
- Adds optional request fields for these controls so API callers can override them later.
- Upgrades full-refresh owned URL cap from the legacy 60 default to 100 when no explicit higher cap is supplied.

## Notes
No frontend change is required for these defaults. The UI can remain simple while the Evidence Service handles DeepResearch workflow tuning internally.
