---
id: "003"
title: Jira Resolution Time Histogram
status: pending
blocked_by: []
blocks: []
---

# Jira Resolution Time Histogram

## Overview

Extend the jira-tempo-exporter to emit a `jira_issue_resolution_time_hours` Histogram metric, measuring the time from issue creation to resolution. This enables MTTR analysis and answers "Is Claude Code making us faster at fixing bugs?" by correlating resolution time with Claude Code usage.

> **Completion rule:** This spec is not complete until all acceptance criteria
> are verified by querying Prometheus for the new histogram metric and confirming
> it returns bucketed data. Build-only verification is insufficient.
> The agent must iterate until verification passes.

## Goals

- Measure actual issue resolution time (created → resolved) per user and project
- Enable MTTR dashboards and trend analysis
- Require no additional Jira API calls — reuse existing `collect_resolved_issues()` query

## Requirements

### Functional Requirements

- **FR1**: A new Prometheus Histogram metric `jira_issue_resolution_time_hours` with labels `user_email`, `project`, `issue_type`, `priority`
- **FR2**: Resolution time computed as `resolutiondate - created` from Jira issue fields, converted to hours
- **FR3**: Histogram buckets appropriate for development workflows: 1, 2, 4, 8, 16, 24, 48, 72, 120, 168, 336, 720 hours (1h to 30d)
- **FR4**: Issues missing `created` or `resolutiondate` fields are skipped without error

### Non-Functional Requirements

- **NFR1**: No additional Jira API calls — add `created,resolutiondate` to the existing `fields` parameter in `collect_resolved_issues()`

### Technical Constraints

- **TC1**: Changes are in `jira-tempo-exporter/exporter.py` only
- **TC2**: Jira returns `created` and `resolutiondate` as ISO 8601 strings (e.g., `2026-03-10T14:30:00.000-0500`)
- **TC3**: The existing `collect_resolved_issues()` function clears metrics at the start of each refresh cycle — the histogram must be cleared and re-observed each cycle (use a fresh Histogram or clear appropriately)

## Implementation Tasks

- [ ] Add `jira_issue_resolution_time_hours` Histogram metric definition with the specified buckets and labels
- [ ] Add `created,resolutiondate` to the `fields` string in `collect_resolved_issues()` (currently line 238)
- [ ] Parse `created` and `resolutiondate` ISO timestamps and compute delta in hours
- [ ] Observe each resolved issue's resolution time into the histogram
- [ ] Skip issues where either timestamp is missing or unparseable
- [ ] Build and push updated Docker image to ECR
- [ ] Deploy and verify

## Acceptance Criteria

- [ ] AC1: `jira_issue_resolution_time_hours_bucket` metric exists in Prometheus with the expected labels and buckets
- [ ] AC2: Running `sum(jira_issue_resolution_time_hours_count)` returns a count matching (approximately) the number of resolved issues
- [ ] AC3: `histogram_quantile(0.5, rate(jira_issue_resolution_time_hours_bucket[30d]))` returns a reasonable median resolution time in hours
- [ ] AC4: Issues with missing `resolutiondate` or `created` do not cause errors in the exporter logs

## Testing Approach

### Validation Steps

1. Build the updated exporter image and deploy to the cluster
2. Wait for one refresh cycle (5 minutes)
3. Query Prometheus for `jira_issue_resolution_time_hours_bucket` — verify non-empty results
4. Query for the count and confirm it's in the expected range
5. Check exporter logs for any parsing errors

### Test Cases

| Input | Expected Output |
|-------|-----------------|
| Issue resolved in 2 hours | Observation in the 2h and higher buckets |
| Issue resolved in 7 days | Observation in the 168h and higher buckets |
| Issue with null `resolutiondate` | Skipped, no error in logs |
| Issue with null `created` | Skipped, no error in logs |

## Out of Scope

- Dashboard panels for MTTR visualization (can be added as a follow-up to spec 001)
- Broadening the Jira query scope beyond `assignee IN (...)`
- Correlating resolution time with specific Claude Code sessions

## References

- Exporter source: `claudefana-enterprise/jira-tempo-exporter/exporter.py`
- `collect_resolved_issues()` function starting at line 221
- ROI guide mapping, Q5 section: `claudefana-enterprise-deploy/ai-docs/roi-guide-mapping.md`
