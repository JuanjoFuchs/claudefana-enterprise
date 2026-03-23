---
id: "001"
title: ROI Dashboard Panels
status: pending
blocked_by: []
blocks: ["002", "005"]
---

# ROI Dashboard Panels

## Overview

Add new Grafana panels to the Claude Code dashboard that answer ROI questions directly: cost per commit, cache efficiency, subscription tier comparison, session productivity, and context window utilization. All panels use existing Prometheus metrics — no exporter or collector changes needed.

> **Completion rule:** This spec is not complete until all acceptance criteria
> are verified by loading the dashboard in Grafana and confirming each panel
> renders data for the current time range. Build-only verification is insufficient.
> The agent must iterate until verification passes.

## Goals

- Answer "What's our cost efficiency?" at a glance (cost/commit, cost/session, cache ratio)
- Give leadership a subscription vs API cost comparison per user
- Surface session-level productivity (cost vs output per session)
- Show context window utilization to support session length analysis

## RequirementsTypeless, this is the best.

### Functional Requirements

- **FR1**: A "Cost Per Commit" stat panel showing `cost_total / commit_count` for the selected time range and org filters
- **FR2**: A "Cache Efficiency Ratio" stat panel showing `cache_read_tokens / cache_creation_tokens` (higher = better caching)
- **FR3**: A "Sessions-to-PR Conversion Rate" stat panel showing `pull_request_count / session_count`
- **FR4**: A "Subscription Tier Comparison" table panel showing per-user: actual API cost, and whether Pro ($20), Max 5x ($100), or Max 20x ($200) would be cheaper
- **FR5**: A "Session Productivity" table panel showing per-session: total cost, tool calls, lines added, accepted edits
- **FR6**: A "Context Window Utilization" stat panel showing avg and max `(cache_read_tokens + cache_creation_tokens + input_tokens)` per session as a percentage of the model's context window

### Non-Functional Requirements

- **NFR1**: All panels must respect the existing dashboard variables (`$rollup_name`, `$manager_name`, `$user_email`)
- **NFR2**: All panels must work with the "This week", "Last 7 days", and "Last 30 days" time ranges without breaking (use `max_over_time(...[$__range])` pattern with `user_info` join)

### Technical Constraints

- **TC1**: All data comes from existing Prometheus metrics — no Loki queries, no exporter changes
- **TC2**: Panels are added to the existing `claude-code-dashboard.json` in the enterprise repo
- **TC3**: The `user_info` join must use `max_over_time(user_info{...}[$__range])` to avoid the future-timestamp bug (see deploy repo `ai-docs/monitoring-runbook.md`)
- **TC4**: Token metrics have a `session_id` label, enabling per-session aggregation via `sum by (session_id)`

## Implementation Tasks

- [ ] Add "Cost Per Commit" stat panel to the Cost Analysis row
- [ ] Add "Cache Efficiency Ratio" stat panel to the Cost Analysis row
- [ ] Add "Sessions-to-PR Conversion Rate" stat panel to the Overview row
- [ ] Add "Subscription Tier Comparison" table panel as a new "Pricing Analysis" row
- [ ] Add "Session Productivity" table panel as a new "Session Analysis" row
- [ ] Add "Context Window Utilization" stat panel to the "Session Analysis" row
- [ ] Deploy updated ConfigMap and restart Grafana

## Acceptance Criteria

### Core Panels
- [ ] AC1: "Cost Per Commit" panel shows a dollar value when at least one user has both cost and commit data in the selected range
- [ ] AC2: "Cache Efficiency Ratio" panel shows a ratio (e.g., "39:1") derived from `cache_read_tokens_total / cache_creation_tokens_total`
- [ ] AC3: "Sessions-to-PR Conversion Rate" panel shows a percentage
- [ ] AC4: "Subscription Tier Comparison" table lists users with columns: user, actual cost, recommended tier, savings/overspend
- [ ] AC5: "Session Productivity" table lists sessions with columns: session ID (truncated), user, cost, tool calls, lines added, accepted edits
- [ ] AC6: "Context Window Utilization" stat shows avg % and max % across sessions

### Filters and Time Ranges
- [ ] AC7: All panels filter correctly when `$rollup_name`, `$manager_name`, or `$user_email` variables are set
- [ ] AC8: All panels render data for "Last 7 days" and "This week" time ranges without returning empty

## Testing Approach

### Validation Steps

1. Deploy the updated dashboard ConfigMap and restart Grafana
2. Open the dashboard with "Last 7 days" time range — verify all 6 new panels show data
3. Switch to "This week" — verify panels still show data (no future-timestamp bug)
4. Set `$user_email` to a specific active user — verify all panels filter to that user
5. Set `$rollup_name` to a specific VP — verify panels filter to that org subtree

### Test Cases

| Input | Expected Output |
|-------|-----------------|
| Last 7 days, no filters | All panels show org-wide aggregates |
| This week, no filters | All panels show data (not empty) |
| Specific user filter | Panels show only that user's data |
| User with zero commits | Cost Per Commit shows "No data" or infinity indicator, not an error |
| User with zero PRs | Sessions-to-PR shows "No data", not an error |

## Out of Scope

- Loki-based panels (prompt length, ticket linking) — covered in spec 002
- Jira exporter changes (resolution time histogram) — covered in spec 003
- OTEL collector config changes — covered in spec 004
- Batch analysis scripts (efficiency curve, cross-datasource joins) — covered in spec 005
- Process improvement recommendations (ticket reference rate, session length guidance)

## References

- ROI guide mapping: `claudefana-enterprise-deploy/ai-docs/roi-guide-mapping.md`
- Existing dashboard: `claudefana-enterprise/dashboards/claude-code-dashboard.json`
- Future-timestamp fix pattern: search for `max_over_time(user_info{...}[$__range])` in the dashboard JSON
