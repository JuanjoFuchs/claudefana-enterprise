---
id: "005"
title: Session Efficiency Analysis Script
status: pending
blocked_by: ["001"]
blocks: []
---

# Session Efficiency Analysis Script

## Overview

A batch analysis script that queries Loki and Prometheus to compute per-session efficiency metrics: cost-per-tool-call, cost-per-accepted-edit, cost-per-lines-added, and the context growth curve. Outputs a report answering "Are long sessions worth the cost?" and identifies the context size breakpoint where returns diminish.

> **Completion rule:** This spec is not complete until all acceptance criteria
> are verified by running the script against the live cluster and confirming
> it produces a report with session efficiency data. Build-only verification
> is insufficient. The agent must iterate until verification passes.

## Goals

- Quantify whether long sessions produce more output per dollar than short ones
- Identify a context size threshold where cost-per-output starts increasing
- Produce a data-backed recommendation for optimal session length
- Cross-correlate Loki per-request data with Prometheus per-session aggregates

## Requirements

### Functional Requirements

- **FR1**: Query Loki for all `api_request` events in a configurable time range, extracting per-request: `session_id`, `user_email`, `model`, `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `output_tokens`, `cost_usd`, `event_timestamp`
- **FR2**: Query Prometheus for per-session aggregates: `claude_code_tool_calls_total`, `claude_code_lines_of_code_count_total{type="added"}`, `claude_code_code_edit_tool_decision_total{decision="accept"}`, `claude_code_cost_usage_USD_total`
- **FR3**: For each session, compute: max context size, total cost, total tool calls, total lines added, total accepted edits, number of API requests
- **FR4**: Compute efficiency metrics per session: $/tool-call, $/accepted-edit, $/line-added
- **FR5**: Compute the context growth curve: at each API request within a session, the cumulative context size and the marginal cost
- **FR6**: Output a summary report in markdown format with:
  - Session efficiency table (sorted by cost descending)
  - Context size vs efficiency scatter data
  - Recommended breakpoint (the context size above which $/tool-call exceeds the org-wide median by 2x)
- **FR7**: Also output a cross-datasource prompt length vs cost efficiency table (joining Loki `prompt_length` with Prometheus cost/commit data per user)

### Non-Functional Requirements

- **NFR1**: Script runs as a one-shot CLI tool, not a persistent service
- **NFR2**: Must complete within 5 minutes for 30 days of data

### Technical Constraints

- **TC1**: Script queries Loki HTTP API at `http://loki:3100` and Prometheus HTTP API at `http://prometheus:9090` (or configurable endpoints)
- **TC2**: Designed to run as an ephemeral pod in the cluster: `kubectl run ... --image=python:3.12-slim -- python3 - < scripts/session-analysis.py`
- **TC3**: Dependencies limited to Python stdlib + `urllib` (no pip installs needed for the ephemeral pod pattern)

## Implementation Tasks

- [ ] Create `scripts/session-analysis.py` in the enterprise repo
- [ ] Implement Loki query for `api_request` events with token breakdowns
- [ ] Implement Loki query for `user_prompt` events with `prompt_length`
- [ ] Implement Prometheus queries for per-session aggregates
- [ ] Implement session efficiency computation (join Loki + Prometheus data by `session_id`)
- [ ] Implement prompt length vs cost efficiency computation (join Loki + Prometheus data by `user_email`)
- [ ] Implement breakpoint detection (context size where $/tool-call exceeds 2x median)
- [ ] Output markdown report to stdout

## Acceptance Criteria

- [ ] AC1: Running the script produces a markdown report listing sessions with cost, tool calls, lines added, accepted edits, max context, and efficiency metrics
- [ ] AC2: The report includes a "Recommended Session Length" section with a context size threshold (or "insufficient data" if not enough sessions exist)
- [ ] AC3: The report includes a prompt length vs cost efficiency table per user
- [ ] AC4: The script completes in under 5 minutes for 30 days of data
- [ ] AC5: The script runs successfully as an ephemeral pod with no pip dependencies

## Testing Approach

### Validation Steps

1. Run the script locally with Prometheus and Loki port-forwarded, or as an ephemeral pod
2. Verify the output contains a session table with at least 5 sessions
3. Verify efficiency metrics are computed (not NaN or zero for sessions with tool calls)
4. Verify the prompt length table lists at least 5 users
5. Verify the breakpoint recommendation section is present

### Test Cases

| Input | Expected Output |
|-------|-----------------|
| 30 days of data, org-wide | Report with 20+ sessions, efficiency metrics, breakpoint |
| 1 day of data | Report with fewer sessions, may say "insufficient data" for breakpoint |
| Port-forward to wrong endpoint | Clear error message, not a stack trace |

## Out of Scope

- Running as a recurring cron job or persistent service
- Writing results to Prometheus pushgateway or a database
- Grafana panel integration (the report is a standalone artifact)
- Automated recommendations sent to developers

## References

- ROI guide mapping, Q7 and "What We Could Add Next" items 15-16: `claudefana-enterprise-deploy/ai-docs/roi-guide-mapping.md`
- Loki `api_request` event schema validated 2026-03-20
- Ephemeral pod pattern: `claudefana-enterprise-deploy/AGENTS.md` "In-Cluster Debugging" section
