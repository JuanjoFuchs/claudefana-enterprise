---
id: "002"
title: Loki-Based Dashboard Panels
status: pending
blocked_by: ["001"]
blocks: []
---

# Loki-Based Dashboard Panels

## Overview

Add Grafana panels backed by Loki queries to the Claude Code dashboard, covering prompt quality analysis, ticket-linked commits, and per-session context growth. These panels use structured log data from Claude Code OTEL events that are already flowing into Loki.

> **Completion rule:** This spec is not complete until all acceptance criteria
> are verified by loading the dashboard in Grafana and confirming each panel
> renders data for the current time range. Build-only verification is insufficient.
> The agent must iterate until verification passes.

## Goals

- Surface prompt quality metrics (length distribution, short prompt ratio) to identify coaching targets
- Show which Claude Code commits reference Jira tickets
- Visualize context window growth within sessions to support session length analysis

## Requirements

### Functional Requirements

- **FR1**: A "Prompt Length Distribution" bar chart showing prompt counts bucketed by length: <20, 20-49, 50-99, 100-199, 200-499, 500-999, 1000+
- **FR2**: A "Short Prompt Ratio by User" table showing per-user: total prompts, avg length, % under 20 characters
- **FR3**: An "Avg Prompt Length Over Time" timeseries showing org-wide average prompt length trend
- **FR4**: A "Ticket-Linked Commits" logs panel showing recent `git commit` events that contain Jira ticket keys (`[A-Z]+-[0-9]+`), with columns: timestamp, user, ticket key, commit message excerpt
- **FR5**: A "Context Growth per Session" timeseries showing `cache_read_tokens + cache_creation_tokens` over time for the top N most expensive sessions, visualizing how context accumulates

### Non-Functional Requirements

- **NFR1**: Loki queries must complete within 10 seconds for a 7-day range
- **NFR2**: Panels that can be filtered by `user_email` must support the `$user_email` dashboard variable via LogQL label filter

### Technical Constraints

- **TC1**: All data comes from Loki `service_name="claude-code"` stream
- **TC2**: Prompt events use `event_name="user_prompt"` with `prompt_length` as a structured metadata field
- **TC3**: Commit events are `event_name="tool_result"` with `tool_name="Bash"` and `tool_parameters` containing `git commit`
- **TC4**: API request events use `event_name="api_request"` with `cache_read_tokens`, `cache_creation_tokens`, `input_tokens`, `output_tokens`, `cost_usd` as structured metadata fields
- **TC5**: Panels are added to the existing `claude-code-dashboard.json`
- **TC6**: The Loki datasource UID in the dashboard is `loki`

## Implementation Tasks

- [ ] Add "Prompt Length Distribution" bar chart panel to a new "Prompt Quality" row
- [ ] Add "Short Prompt Ratio by User" table panel to the "Prompt Quality" row
- [ ] Add "Avg Prompt Length Over Time" timeseries panel to the "Prompt Quality" row
- [ ] Add "Ticket-Linked Commits" logs panel to the "Session Analysis" row (created in spec 001)
- [ ] Add "Context Growth per Session" timeseries panel to the "Session Analysis" row
- [ ] Deploy updated ConfigMap and restart Grafana

## Acceptance Criteria

### Prompt Quality Panels
- [ ] AC1: "Prompt Length Distribution" shows a bar chart with at least 3 non-empty buckets when data exists in the selected range
- [ ] AC2: "Short Prompt Ratio by User" table lists users with their prompt count, avg length, and short% columns
- [ ] AC3: "Avg Prompt Length Over Time" shows a timeseries line trending over the selected range

### Commit and Session Panels
- [ ] AC4: "Ticket-Linked Commits" shows log entries containing Jira ticket keys extracted from git commit commands
- [ ] AC5: "Context Growth per Session" shows ascending lines representing context accumulation for active sessions

### Filtering
- [ ] AC6: Prompt panels filter by `$user_email` when the variable is set
- [ ] AC7: All panels return data for "Last 7 days" time range

## Testing Approach

### Validation Steps

1. Deploy the updated dashboard ConfigMap and restart Grafana
2. Open the dashboard with "Last 7 days" time range
3. Navigate to the "Prompt Quality" row — verify distribution, ratio table, and trend panels render
4. Navigate to the "Session Analysis" row — verify ticket-linked commits and context growth panels render
5. Set `$user_email` to a known active user — verify prompt panels filter
6. Verify Loki queries complete in under 10 seconds (check panel load indicator)

### Test Cases

| Input | Expected Output |
|-------|-----------------|
| Last 7 days, no filters | Prompt distribution shows bucketed counts |
| Specific user filter | Prompt ratio table shows only that user |
| Time range with no commits | Ticket-Linked Commits shows "No data" gracefully |
| Session with 10+ API requests | Context Growth shows a clear ascending line |

## Out of Scope

- Prometheus-based panels (covered in spec 001)
- OTEL collector transforms to promote ticket keys to labels (covered in spec 004)
- Cross-datasource joins (prompt length vs $/commit correlation) — covered in spec 005
- Enabling `OTEL_LOG_USER_PROMPTS=1` or prompt content analysis

## References

- ROI guide mapping, "Prompt Quality Analysis" section: `claudefana-enterprise-deploy/ai-docs/roi-guide-mapping.md`
- ROI guide mapping, "Question 7" section for context window analysis
- Loki event schema validated 2026-03-20: `event_name`, `prompt_length`, `cache_read_tokens`, etc.
