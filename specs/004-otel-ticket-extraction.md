---
id: "004"
title: OTEL Collector Ticket Key Extraction
status: pending
blocked_by: []
blocks: []
---

# OTEL Collector Ticket Key Extraction

## Overview

Add a transform processor to the OTEL collector pipeline that extracts Jira ticket keys from `git commit` Bash tool events and writes them as a dedicated attribute on the log event. This makes ticket-to-session correlation queryable via a simple Loki label filter instead of expensive regex on `tool_parameters`.

> **Completion rule:** This spec is not complete until all acceptance criteria
> are verified by sending a test OTLP payload containing a git commit event
> with a ticket key and confirming the extracted key appears as a queryable
> attribute in Loki. Build-only verification is insufficient.
> The agent must iterate until verification passes.

## Goals

- Make Jira ticket keys a first-class queryable field in Loki
- Eliminate the need for regex on `tool_parameters` at query time
- Enable efficient Grafana panels that filter by ticket key

## Requirements

### Functional Requirements

- **FR1**: Log events where `tool_name == "Bash"` and `tool_parameters` contains a pattern matching `git commit` AND a Jira ticket key (`[A-Z]{2,10}-[0-9]+`) get a new attribute `jira_ticket_key` set to the extracted key
- **FR2**: If multiple ticket keys appear in the same event, extract the first match
- **FR3**: Events that don't match the pattern are passed through unchanged — no attribute added
- **FR4**: The extraction runs only on `tool_result` events (not `user_prompt`, `api_request`, etc.)

### Non-Functional Requirements

- **NFR1**: The processor must not add measurable latency to the pipeline (the regex runs only on events matching `tool_name == "Bash"`)

### Technical Constraints

- **TC1**: Configuration is in the OTEL collector config (ConfigMap in `claudefana-enterprise/k8s/base/`)
- **TC2**: The collector is `otelcol-contrib 0.147.0`, which includes the `transform` processor
- **TC3**: `tool_parameters` is a string field containing escaped JSON — the regex operates on the raw string, not parsed JSON
- **TC4**: The new attribute must be written as a log record attribute so Loki indexes it as structured metadata

## Implementation Tasks

- [ ] Add a `transform` processor to the OTEL collector config that matches Bash tool_result events containing `git commit` and extracts ticket keys via regex
- [ ] Wire the processor into the logs pipeline
- [ ] Deploy the updated collector config
- [ ] Verify the attribute appears in Loki

## Acceptance Criteria

- [ ] AC1: A Claude Code `tool_result` event containing `git commit -m "PROJ-123: fix bug"` in `tool_parameters` has `jira_ticket_key="PROJ-123"` as a queryable attribute in Loki
- [ ] AC2: A Claude Code `tool_result` event with `git commit -m "refactor auth flow"` (no ticket key) does NOT have a `jira_ticket_key` attribute
- [ ] AC3: `{service_name="claude-code"} | json | jira_ticket_key != ""` returns only events with extracted ticket keys
- [ ] AC4: Non-Bash events (e.g., `tool_name="Edit"`) are not affected by the processor

## Testing Approach

### Validation Steps

1. Deploy the updated OTEL collector config
2. Wait for Claude Code events containing `git commit` with a ticket key (or trigger one manually)
3. Query Loki: `{service_name="claude-code"} | json | jira_ticket_key != ""`
4. Verify the extracted key matches the ticket key in the commit message
5. Query a non-commit event and confirm `jira_ticket_key` is absent

### Test Cases

| tool_parameters content | Expected jira_ticket_key |
|------------------------|--------------------------|
| `git commit -m "PROJ-123: fix bug"` | `PROJ-123` |
| `git commit -m "DATA-16732: update schema"` | `DATA-16732` |
| `git commit -m "refactor auth flow"` | (not set) |
| `git add . && git status` (not a commit) | (not set) |
| `git commit -m "fix AIR-2488 and DATA-123"` | `AIR-2488` (first match) |

## Out of Scope

- Extracting ticket keys from non-commit events (Read, Edit, user prompts)
- Parsing multiple ticket keys per event into an array
- Looking up ticket details from Jira based on extracted keys
- Dashboard panels using the new attribute (can use spec 002's ticket panel, updated to use the label)

## References

- OTEL transform processor docs: https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/transformprocessor
- ROI guide mapping, Q5 "OTEL collector transform" item: `claudefana-enterprise-deploy/ai-docs/roi-guide-mapping.md`
- Validated event structure from Loki (2026-03-20): `tool_parameters` contains escaped JSON with `full_command` field
