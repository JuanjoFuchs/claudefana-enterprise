# Mapping Anthropic's ROI Guide to Claudefana Enterprise

How to answer every question from [Anthropic's Claude Code ROI Measurement Guide](https://github.com/anthropics/claude-code-monitoring-guide/blob/main/claude_code_roi_full.md) using our Claudefana Enterprise deployment.

## The Questions That Matter

Measuring ROI on AI coding tools boils down to seven questions. Each one tells leadership something different about whether the investment is paying off. We've consolidated them into 5 dashboard sections.

**Q1. "What's our ROI per developer?"**
Are we getting our money's worth from each person using Claude Code? What does each commit, resolved ticket, and line of code cost in API spend?

**Q2. "Does experience level change how effectively people use it?"**
Do certain roles get more value from Claude Code than others? Which roles accept Claude's suggestions most, spend the most, and write the best prompts?

**Q3. "Should we switch pricing models?"**
How many users fit each subscription tier? Which teams should be on Pro ($20), Max 5x ($100), or Max 20x ($200)?

**Q4. "Where are teams getting stuck?"** *(merged with "Are long sessions worth the cost?")*
Which teams have high rejection rates, expensive sessions, and oversized context windows? Long sessions and high rejection rates are friction signals.

**Q5. "Is Claude Code actually making us faster at fixing bugs?"**
Are tickets getting resolved faster? Which teams resolve issues quickest? How does resolution speed vary by priority?

---

## ROI Dashboard Implementation

**Dashboard:** `claude-code-roi` (UID: `claude-code-roi`)
**Location:** `dashboards/claude-code-roi.json`

### Design Principles

1. **No tables** — everything is charts (stats, bar gauges, bar charts, pie charts)
2. **No individual user data** — all metrics aggregated by team (`rollup_name`) or role (`job_title`)
3. **Sections organized by question** — each section has explanatory text + relevant charts
4. **Consistent patterns** — bar gauges sorted by `topk`/`bottomk`, color-coded thresholds, team-level joins via `user_info`

### Dashboard Sections

#### Q1: What's Our ROI Per Developer? (open by default)

| Panel | Type | Datasource | What it answers |
|-------|------|------------|-----------------|
| Text: explanation | text | — | Context for the section |
| Cost Per Commit | stat | prometheus | Org-wide $/commit — baseline unit economics |
| Cost Per Resolved Issue | stat | prometheus | $/Jira-issue — ties AI spend to business outcomes |
| Cost Per Line of Code | stat | prometheus | $/line — raw output efficiency |
| Cost Per Commit by Team | bargauge | prometheus | Which teams get the most output per dollar (sorted highest-first via `topk`) |

**Key query patterns:**
- All use `max_over_time(metric[$__range])` with `user_info` join for variable filtering
- Cost Per Commit by Team uses `topk(20, (cost_by_team) / (commits_by_team))` for sorting

#### Q2: Does Experience Level Change Effectiveness? (collapsed)

| Panel | Type | Datasource | What it answers |
|-------|------|------------|-----------------|
| Text: explanation | text | — | Context |
| Code Edit Acceptance Rate by Role | bargauge | prometheus | Which roles reject Claude most (sorted lowest-first via `bottomk`) |
| Total Cost by Role | bargauge | prometheus | Where AI spend is concentrated by role (sorted via `topk`) |
| Cost Per Commit by Role | bargauge | prometheus | Which roles produce cheapest output (sorted via `topk`) |
| Avg Prompt Length by Role | bargauge | prometheus | Which roles write detailed vs terse prompts |
| Short Prompt Ratio by Role | bargauge | prometheus | % of prompts <20 chars per role — coaching targets |

**Prompt quality by role** is enabled by the `claude_code_prompt_length_chars` histogram added to the OTEL signaltometrics connector. This converts Loki `prompt_length` data into Prometheus metrics with `user.email`, enabling joins with `user_info` for `job_title` grouping.

**OTEL config change** (in `configmaps.yaml` signaltometrics section):
```yaml
- name: claude_code_prompt_length_chars
  description: "Distribution of prompt lengths in characters"
  histogram:
    value: Double(attributes["prompt_length"])
    buckets: [20, 100, 500]
  attributes:
    - key: user.email
  conditions:
    - 'attributes["event.name"] == "user_prompt"'
```

This produces:
- `claude_code_prompt_length_chars_bucket{le="20|100|500|+Inf", user_email="..."}` — distribution
- `claude_code_prompt_length_chars_sum{user_email="..."}` — total chars (for avg = sum/count)
- `claude_code_prompt_length_chars_count{user_email="..."}` — total prompts

#### Q3: Should We Switch Pricing Models? (collapsed)

| Panel | Type | Datasource | What it answers |
|-------|------|------------|-----------------|
| Text: explanation | text | — | Context |
| Users Per Subscription Tier | bargauge | prometheus | How many users fit Pro/Max5x/Max20x/PayAsYouGo (color-coded bars) |
| Avg Cost Per User by Team | bargauge | prometheus | Which tier each team needs (color = tier threshold) |

**Tier bucketing** uses `count()` with range filters:
- Pro: `count(per_user_cost < 20)`
- Max 5x: `count(per_user_cost >= 20 and per_user_cost < 100)`
- Max 20x: `count(per_user_cost >= 100 and per_user_cost < 200)`
- Pay-as-you-go: `count(per_user_cost >= 200)`

Each bar is color-coded via field overrides (green/yellow/orange/red).

#### Q4: Where Are Teams Getting Stuck? (collapsed)

*Merged Q4 ("Where are people wasting time?") with Q7 ("Are long sessions worth the cost?")*

| Panel | Type | Datasource | What it answers |
|-------|------|------------|-----------------|
| Text: explanation | text | — | Context |
| Rejection Rate by Team | bargauge | prometheus | Which teams reject Claude's suggestions most (sorted via `topk`) |
| Avg Cost Per Session by Team | bargauge | prometheus | Which teams run the most expensive sessions |
| Session Size Distribution | bargauge | prometheus | Org-wide: how many sessions in each context bucket (<100K, 100K-500K, 500K-1M, >1M) |
| Large Session Rate by Team | bargauge | prometheus | % of each team's sessions exceeding 500K context tokens |

**Friction correlation:** Teams appearing high on rejection rate, session cost, AND large session rate are the strongest coaching targets — three independent signals pointing to the same problem.

**Session size bucketing** uses `sum by (session_id)` of `token_usage_tokens_total{type=~"cacheRead|cacheCreation"}` joined with `user_info` for team grouping.

#### Q5: Is Claude Code Making Us Faster at Fixing Bugs? (collapsed)

| Panel | Type | Datasource | What it answers |
|-------|------|------------|-----------------|
| Text: explanation | text | — | Context |
| Median Resolution Time by Team | bargauge | prometheus | Hours to resolve per team (via `histogram_quantile` on `jira_issue_resolution_time_hours_bucket` joined with `user_info`) |
| Median Resolution Time by Priority | bargauge | prometheus | Hours to resolve by Critical/High/Medium/Low |
| Cost Per Resolved Issue by Team | bargauge | prometheus | $/issue by team — direct ROI metric |
| Resolution Time Distribution | bargauge | prometheus | Bucketed: Fast (<1d), Normal (1-3d), Slow (3-7d), Very Slow (1-2wk), Critical (>2wk) |

**Resolution time histogram** (`jira_issue_resolution_time_hours`) was added to the Jira exporter with labels `user_email`, `project`, `issue_type`, `priority` and buckets `[1, 2, 4, 8, 16, 24, 48, 72, 120, 168, 336, 720]`.

---

## What We Have vs What the Guide Assumes

The guide assumes a basic Prometheus + Grafana setup with raw OTEL metrics. Our stack goes further — we have org-tree enrichment via MS Graph, Jira/Tempo integration, and pre-built dashboards. This means we can answer most questions directly from existing panels, and the rest with simple PromQL queries.

### Metrics Inventory

**Claude Code OTEL metrics (from otel-collector:8889):**
| Metric | Guide Name | Available |
|--------|-----------|-----------|
| `claude_code_cost_usage_USD_total` | `claude_code.cost.usage` | Yes |
| `claude_code_token_usage_tokens_total` | `claude_code.token.usage` | Yes |
| `claude_code_input_tokens_total` | — | Yes |
| `claude_code_output_tokens_total` | — | Yes |
| `claude_code_cache_creation_tokens_total` | — | Yes |
| `claude_code_cache_read_tokens_total` | — | Yes |
| `claude_code_active_time_seconds_total` | — | Yes |
| `claude_code_session_count_total` | `claude_code.session.count` | Yes |
| `claude_code_commit_count_total` | `claude_code.commit.count` | Yes |
| `claude_code_pull_request_count_total` | `claude_code.pull_request.count` | Yes |
| `claude_code_lines_of_code_count_total` | `claude_code.lines_of_code.count` | Yes |
| `claude_code_code_edit_tool_decision_total` | `claude_code.code_edit_tool.decision` | Yes |
| `claude_code_tool_calls_total` | — | Yes |
| `claude_code_user_prompts_total` | — | Yes |
| `claude_code_api_requests_total` | — | Yes |
| `claude_code_request_duration_ms` | — | Yes |
| `claude_code_prompt_length_chars` | — | **New** (histogram via signaltometrics) |

**Enrichment metrics (our additions, not in the guide):**
| Metric | Source | Purpose |
|--------|--------|---------|
| `user_info` | graph-enrichment | Org hierarchy: `display_name`, `job_title`, `department`, `manager_name`, `rollup_name`, `employee_id` |
| `org_tree_node` | graph-enrichment | Org chart structure for treemap visualization |
| `jira_issues_created_total` | jira-tempo-exporter | Jira issues created per user |
| `jira_issues_resolved_total` | jira-tempo-exporter | Jira issues resolved per user |
| `jira_story_points_resolved_total` | jira-tempo-exporter | Story points resolved per user |
| `jira_issue_resolution_time_hours` | jira-tempo-exporter | **New** — Histogram of resolution time (created→resolved) |
| `tempo_time_logged_by_user_seconds` | jira-tempo-exporter | Tempo time logged per user |

**Loki log events (structured metadata on each event):**
`event_name`, `tool_name`, `tool_parameters`, `duration_ms`, `success`, `decision_type`, `decision_source`, `user_email`, `session_id`, `prompt_id`, `service_version`, `terminal_type`, `os_type`, `prompt_length`, `jira_ticket_key` (extracted by OTEL transform processor)

Key event types: `user_prompt`, `tool_decision`, `tool_result`, `api_request`.

---

## Infrastructure Changes Implemented

### 1. OTEL Collector — Ticket Key Extraction (configmaps.yaml)

Added `transform/ticket_extraction` processor that extracts Jira ticket keys from `git commit` Bash events and writes them as `jira_ticket_key` attribute in Loki.

```yaml
transform/ticket_extraction:
  error_mode: ignore
  log_statements:
    - context: log
      conditions:
        - 'attributes["event.name"] == "tool_result"'
        - 'IsMatch(attributes["tool_name"], "Bash")'
        - 'IsMatch(attributes["tool_parameters"], "git commit")'
      statements:
        - 'set(attributes["jira_ticket_key"], ExtractPatterns(attributes["tool_parameters"], "^.*?(?P<ticket>[A-Z]{2,10}-[0-9]+).*$")["ticket"])'
```

Wired into logs pipeline: `processors: [memory_limiter, resource, transform/ticket_extraction, batch]`

### 2. OTEL Collector — Prompt Length Histogram (configmaps.yaml)

Added `claude_code_prompt_length_chars` histogram to signaltometrics connector, converting Loki `prompt_length` into a Prometheus metric joinable with `user_info` by `user.email`.

### 3. Jira Exporter — Resolution Time Histogram (exporter.py)

Added `jira_issue_resolution_time_hours` Histogram metric with labels `[user_email, project, issue_type, priority]` and buckets `[1, 2, 4, 8, 16, 24, 48, 72, 120, 168, 336, 720]`. Computes `resolutiondate - created` for each resolved issue.

### 4. Session Analysis Script (scripts/session-analysis.py)

stdlib-only Python script for ephemeral pod execution. Queries Loki for per-request token breakdowns and Prometheus for per-session aggregates. Computes efficiency curves, context breakpoints, and prompt quality analysis. Outputs markdown report.

```bash
AWS_PROFILE=Dev kubectl run roi-analysis --rm -i --restart=Never -n claudefana \
  --image=python:3.12-slim -- python3 - < claudefana-enterprise/scripts/session-analysis.py
```

---

## What We Could Add Next

### Completed (moved from backlog):

- ~~Cost Per Commit panel~~ → Q1 stat panel
- ~~Sessions-to-PR Conversion Rate~~ → Evaluated and removed (misleading: most sessions aren't PR-producing)
- ~~Cache Efficiency Ratio~~ → Evaluated and removed from ROI dashboard (belongs in operational monitoring, not ROI)
- ~~Subscription Tier Recommendation~~ → Q3 tier bucketing with color-coded bar gauge
- ~~Ticket-linked sessions panel~~ → Evaluated and removed from ROI dashboard (raw logs not useful for leadership; ticket data flows through Jira histogram instead)
- ~~Prompt length distribution panel~~ → Q2 via `claude_code_prompt_length_chars` histogram by role
- ~~Short prompt ratio per user~~ → Q2 Short Prompt Ratio by Role
- ~~Session cost vs output~~ → Q4 Avg Cost Per Session by Team
- ~~Context window utilization~~ → Q4 Session Size Distribution + Large Session Rate by Team
- ~~Jira resolution time histogram~~ → Deployed in exporter, visualized in Q5
- ~~Ticket key extraction processor~~ → Deployed in OTEL collector

### Remaining backlog:

1. **Broaden Jira assignee scope** — query by worklog author or reporter in addition to assignee, catching developers who commit against tickets they don't own. Currently ~33% of commits reference Jira tickets; broadening the query would improve correlation.

2. **Prompt length vs cost efficiency correlation** — the `claude_code_prompt_length_chars` histogram enables avg prompt length by role, but correlating it directly with $/commit requires cross-metric joins that are complex in PromQL. A batch script joining both would produce the correlation table from the original analysis.

3. **Session efficiency curve analysis** — `session-analysis.py` computes this, but it's a manual run. Could be automated as a cron job writing results to a Prometheus pushgateway for dashboard consumption.

4. **Historical baseline comparison** — Q5 shows current resolution times but can't answer "faster compared to before Claude Code?" without pre-deployment baseline data. Consider snapshotting current metrics as baseline for future comparison.

5. **Automated weekly reporting** — cron job fetching Prometheus/Loki data and generating executive summaries via Anthropic API.

6. **Prompt content analysis via Haiku** — requires enabling `OTEL_LOG_USER_PROMPTS=1` client-side (privacy decision). Would enable task categorization and quality scoring beyond `prompt_length` proxy.

### Process improvements:

7. **Increase ticket reference rate** — currently 33% of Claude Code commits include a Jira key. Team practice improvement, not a technical change.

8. **Prompting coaching** — use Q2's Short Prompt Ratio by Role and Cost Per Commit by Role to identify coaching targets.

9. **Session length guidance** — use Q4's Large Session Rate by Team to recommend "start a new session after N interactions" for high-rate teams.
