# Project Understanding — claudefana-enterprise

> Enterprise extensions for claudefana — multi-user dashboards with org-chart enrichment and Jira/Tempo correlation.

## Overview

claudefana-enterprise layers on top of the [claudefana](https://github.com/JuanjoFuchs/claudefana) core stack, adding two custom Prometheus exporters and two expanded Grafana dashboards. It transforms a personal observability tool into an organization-level adoption and ROI tracker by joining Claude Code OTEL telemetry with Microsoft Graph org data and Jira/Tempo worklog metrics.

The core stack (OTEL Collector, Prometheus, Loki, Grafana) is pulled in via Docker Compose `include:` — this repo only adds the enterprise-specific services and dashboards.

## Architecture

```
                                    claudefana (core)
                                    ┌──────────────────────────────┐
Claude Code ──OTLP──▶ OTEL Collector ──▶ Prometheus (:9090) ──▶ Grafana (:3000)
                            │                  ▲    ▲
                            ▼                  │    │
                       Loki (:3100) ──────▶ Grafana │
                                    └──────────────────────────────┘
                                                   │    │
                          ┌────────────────────────┘    │
                          │                             │
              graph-enrichment (:9101)     jira-tempo-exporter (:9102)
              (Microsoft Graph API)         (Jira REST + Tempo API)
```

**6 total services:** 4 from core + 2 enterprise exporters.

| Service | Port | Image | Role |
|---------|------|-------|------|
| otel-collector | 4317, 4318, 8889 | `otel/opentelemetry-collector-contrib` | Core — receives OTLP, converts logs→metrics |
| prometheus | 9090 | `prom/prometheus` | Core — time-series storage (uses enterprise scrape config) |
| loki | 3100 | `grafana/loki` | Core — log aggregation |
| grafana | 3000 | `grafana/grafana` | Core — dashboard UI (enterprise dashboards overlay core) |
| graph-enrichment | 9101 | Custom Python 3.12 | Queries Microsoft Graph API for user org data + org hierarchy tree |
| jira-tempo-exporter | 9102 | Custom Python 3.12 | Queries Jira REST API + Tempo API for issues, story points, worklogs |

## File Structure

```
claudefana-enterprise/
├── docker-compose.enterprise.yaml       # Compose overlay — includes core, adds 2 exporters
├── prometheus-enterprise.yml            # Scrape config (core targets + 2 exporter targets)
├── dashboards/
│   ├── claude-code-dashboard.json       # Enterprise adoption dashboard (40+ panels, 5 rows)
│   └── claude-code-user-explorer.json   # Per-user deep-dive dashboard (20+ panels, 6 rows)
├── graph-enrichment-exporter/
│   ├── exporter.py                      # Microsoft Graph exporter (msal + requests)
│   ├── Dockerfile                       # Python 3.12-slim
│   ├── requirements.txt                 # prometheus_client, msal, requests, pyyaml
│   └── config.yaml.example             # Azure AD config template
├── jira-tempo-exporter/
│   ├── exporter.py                      # Jira + Tempo exporter (httpx)
│   ├── Dockerfile                       # Python 3.12-slim
│   └── requirements.txt                 # prometheus_client, httpx, pyyaml, python-dotenv
├── k8s/
│   └── base/
│       ├── kustomization.yaml           # Kustomize entrypoint + dashboard ConfigMap generator
│       ├── namespace.yaml               # claudefana namespace
│       ├── configmaps.yaml              # OTEL Collector, Prometheus, alerts, Grafana provisioning
│       ├── deployments.yaml             # 6 deployments (all services)
│       ├── services.yaml                # 6 ClusterIP services
│       ├── pvcs.yaml                    # Prometheus 50Gi + Grafana 5Gi (storageClass commented)
│       └── ingresses.yaml.example       # Example ALB ingress (copy to overlay)
├── ai-docs/
│   ├── org-tree-panel-implementation.md  # Tree panel PromQL patterns, CxO exclusion, plugin gotchas
│   └── prometheus-v3-scrape-bug.md       # Prometheus 3.x drops high-cardinality metrics; v2.55 fix
├── .env.example                         # Environment template (Jira + Azure credentials)
├── .gitignore
└── README.md
```

## OTEL Collector Pipeline

The OTEL Collector receives OTLP logs from Claude Code and transforms them into Prometheus metrics using two connectors:

### Count Connector
Creates counter metrics from log events:
- `claude_code_api_requests_total` — from `api_request` events (labels: model, session.id, user.email)
- `claude_code_tool_calls_total` — from `tool_result` events (labels: tool_name, session.id)
- `claude_code_user_prompts_total` — from `user_prompt` events (labels: session.id)

### Signal-to-Metrics Connector
Extracts numeric values from log attributes using `Double()` parsing (Claude Code exports values as strings):
- `claude_code_cost_usd_total` — sum of `cost_usd` (labels: model, user.email)
- `claude_code_input_tokens_total` — sum of `input_tokens` (labels: model)
- `claude_code_output_tokens_total` — sum of `output_tokens` (labels: model)
- `claude_code_cache_read_tokens_total` — sum of `cache_read_tokens` (labels: model)
- `claude_code_cache_creation_tokens_total` — sum of `cache_creation_tokens` (labels: model)
- `claude_code_request_duration_ms` — gauge of `duration_ms` (labels: model)

### Processors
- `deltatocumulative` — converts delta metrics to cumulative (max_stale: 10m, max_streams: 10000)
- `memory_limiter` — 512 MiB limit with 128 MiB spike buffer
- `resource` — upserts `service.name: "claude-code"`

Logs are also forwarded to Loki for raw log querying.

## Prometheus Alerts

10 alert rules defined in the K8s configmaps (and available for Docker Compose via `alerts/*.yml`):

| Alert | Condition | Severity |
|-------|-----------|----------|
| `ClaudeCodeUsage5hWarning` | 5h rolling utilization > 70% | warning |
| `ClaudeCodeUsage5hCritical` | 5h rolling utilization > 85% | critical |
| `ClaudeCodeUsage5hExhausted` | 5h rolling utilization > 95% | critical |
| `ClaudeCodeUsage7dWarning` | 7d all-models utilization > 70% | warning |
| `ClaudeCodeUsage7dCritical` | 7d all-models utilization > 85% | critical |
| `ClaudeCodeUsage7dSonnetWarning` | 7d Sonnet utilization > 70% | warning |
| `ClaudeCodeHighBurnRate` | Consumption rate > 20%/hour | warning |
| `ClaudeCodeLowCacheEfficiency` | Cache hit ratio < 50% for 10m | info |
| `ClaudeCodeUsageExporterDown` | Usage scrape success = 0 for 5m | warning |
| `ClaudeCodeNoActivity` | Zero API requests for 30m | info |

## Docker Compose Overlay

`docker-compose.enterprise.yaml` uses `include:` to pull in `../claudefana/docker-compose.otel.yaml` (the core stack). It then:

1. **Overrides Prometheus** — mounts `prometheus-enterprise.yml` (adds scrape targets for both exporters)
2. **Overrides Grafana** — mounts `dashboards/` (enterprise dashboards replace core dashboard)
3. **Adds `jira-tempo-exporter`** — port 9102, env vars from `.env`, refreshes every 300s, 30-day lookback
4. **Adds `graph-enrichment`** — port 9101, env vars from `.env`, refreshes every 3600s

Both exporters depend on Prometheus (they query it to discover which users have Claude Code telemetry before fetching external data).

## Graph Enrichment Exporter

### Purpose
Queries Microsoft Graph API for organizational metadata of Claude Code users, enabling PromQL joins to slice OTEL metrics by department, manager, job title, and rollup manager.

### How It Works
1. Queries Prometheus for distinct `user_email` values from `claude_code_cost_usage_USD_total` (30-day lookback)
2. For each discovered user, fetches their Graph API profile + manager (1 API call per user, manager inlined via `$expand`)
3. Walks up the management chain to resolve the "rollup manager" — the person whose direct manager is a CxO
4. Builds an org hierarchy tree (rollup → manager → user) with orphan detection
5. Exports all data as Prometheus gauge metrics

### Metrics Exported

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `user_info` | Gauge (always 1) | user_email, display_name, department, job_title, manager_name, manager_email, rollup_name, rollup_email, company, office, city, country, employee_id | User org metadata — join key for all PromQL org queries |
| `org_tree_node` | Gauge (always 1) | node_id, parent_id, node_label, node_type (rollup/manager/user) | Hierarchy tree for the tree panel plugin |
| `graph_enrichment_users_total` | Gauge | — | Count of exported users |
| `graph_enrichment_last_refresh_timestamp` | Gauge | — | Unix timestamp of last refresh |
| `graph_enrichment_refresh_errors_total` | Gauge | — | Error count |
| `graph_enrichment_api_calls_total` | Gauge | — | Graph API calls in last refresh |
| `graph_enrichment_org_headcount` | Gauge | — | Total enabled users in Azure AD tenant |

### Key Design Decisions
- **Scoped to Claude Code users only** — only enriches users found in Prometheus, not the entire tenant
- **Rollup cache** — persistent across refresh cycles; shared management chains resolve with zero additional API calls
- **CxO detection** — regex-based: matches "Chief *", CEO/CTO/CIO/CFO/COO, "President" (but not "Vice President")
- **Orphan handling** — nodes whose parent isn't in the tree get re-parented under an "Unknown" root node
- **Config via env vars or YAML** — Docker uses env vars; local testing uses `config.yaml`

### Azure AD Requirements
- App Registration with `User.Read.All` (Application) permission
- Admin consent required
- Client credentials flow (no user interaction)

## Jira/Tempo Exporter

### Purpose
Pulls issue resolution, story points, and Tempo worklog data for Claude Code users, enabling cost-per-ticket and AI-time-vs-billable-time correlation.

### How It Works
1. Queries Prometheus for distinct `user_email` values (same pattern as graph enrichment)
2. Resolves emails → Jira usernames (cached)
3. Queries Jira REST API for resolved/created issues scoped to those users (paginated, 5000 cap)
4. Queries Tempo API for worklogs, filtering to target users
5. Optionally collects Tempo team memberships (expensive: 1 + N API calls)
6. Collects lightweight org-wide issue counts (2 API calls, no pagination)

### Metrics Exported

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `jira_issues_resolved_total` | Gauge | user_email, project, issue_type, priority | Resolved issues in lookback window |
| `jira_story_points_resolved_total` | Gauge | user_email, project | Story points resolved |
| `jira_issues_created_total` | Gauge | user_email, project, issue_type | Created issues in lookback window |
| `tempo_time_logged_seconds` | Gauge | user_email, project, issue_key | Tempo worklog time |
| `tempo_time_logged_by_user_seconds` | Gauge | user_email | Total Tempo time per user |
| `tempo_team_member_info` | Gauge (always 1) | user_email, username, team_name, team_id | Team membership |
| `jira_issues_resolved_org_total` | Gauge | — | Org-wide resolved count |
| `jira_issues_created_org_total` | Gauge | — | Org-wide created count |
| `jira_exporter_last_refresh_timestamp` | Gauge | — | Last refresh timestamp |
| `jira_exporter_refresh_errors_total` | Gauge | — | Error count |
| `jira_exporter_users_total` | Gauge | — | Users with exported metrics |
| `jira_exporter_cc_users_total` | Gauge | — | Claude Code users tracked |

### Key Design Decisions
- **Uses `httpx` instead of `requests`** — Jira Cloud WAF blocks the `requests` User-Agent
- **SSL verification disabled** — for on-prem Jira instances with self-signed certs
- **Story points field is configurable** — `STORY_POINTS_FIELD` env var (must match your Jira instance)
- **Tempo team collection is opt-in** — `COLLECT_TEAMS=true` triggers 1+N API calls (expensive)
- **Scoped to Claude Code users** — only fetches Jira/Tempo data for users with OTEL telemetry

## Enterprise Adoption Dashboard

**Title:** Claude Code Adoption | **UID:** `claude-code-adoption` | **Default range:** 30 days

3 cascading template variables: `$rollup_name` → `$manager_name` → `$user_email` (all sourced from `user_info` metric). Plus `$developer_headcount` (manual dropdown for adoption rate denominator).

Dashboard links to a companion "User Explorer" dashboard with variable passthrough.

### Row 1: Adoption & Outcomes (14 panels)
- **Adoption Rate** — gauge: active users / developer headcount, thresholds at 20/50/75%
- **Active Users** / **Dev Headcount** / **Commits** / **Total Sessions** / **Avg Sessions/User** / **Lines Added** — stat panels
- **Edit Acceptance Rate** — gauge: accepted / total edit decisions
- **Claude Code Versions** / **Platform Distribution** — donut pie charts
- **Adoption Over Time** — timeseries: active, returning, new users
- **Lines of Code Modified** — stacked timeseries (added/removed)
- **Top Users by Cost** / **Top Job Titles by Cost** — bar gauges

### Row 2: Cost & Usage (7 panels)
- **Total Spend** / **Cost/User** / **Cost/Session** / **Cost/Commit** — stat panels
- **Tool Usage** — horizontal bar chart
- **Cost by Model** — stacked area timeseries
- **Model Mix** — donut pie chart

### Row 3: Organization (7 panels, collapsed)
- **Spend by Department** / **Active Users by Department** — donut pie charts (requires graph-enrichment)
- **Active Users by Rollup Manager** / **Spend by Rollup Manager** — horizontal bar charts (clickable drill-down)
- **Usage by Job Title** — horizontal bar chart
- **Developer Scorecard** — full-width table: Name, Department, Job Title, Manager, Rollup, Cost, Active Time, Commits, Lines Added, Issues Resolved, Story Points, Tempo Hours, AI/Billable %
- **Org Hierarchy Tree** — `equansdatahub-tree-panel` plugin: rollup→manager→user with 7 aggregated metric columns

### Row 4: User Deep Dive (5 panels, collapsed)
- **Sessions Per Day** / **Active Time Per Day** / **Cost Per Session Trend** / **Lines Per Dollar** / **User Prompts Over Time** — timeseries

### Row 5: System Health (4 panels, collapsed)
- **Token Usage by Type** / **Token Usage by Model** — stacked timeseries
- **API Latency by Model** — timeseries
- **Cache Hit Ratio** — gauge

## User Explorer Dashboard

**Title:** Claude Code User Explorer | **UID:** `claude-code-user-explorer` | **Default range:** 30 days

Same 3 cascading variables. Designed as a drill-through from the adoption dashboard for per-user analysis.

### Row 1: User Profile & Summary (7 panels)
- **User Profile** — table showing display_name, department, job_title, manager, rollup
- **Total Spend** / **Sessions** / **Active Time** / **Lines Added** / **Cost/Session** — stats
- **Cache Hit %** — gauge

### Row 2: Activity Over Time (2 panels)
- **Sessions Per Day** / **Active Time Per Day** — timeseries

### Row 3: Cost Over Time (2 panels)
- **Cost Over Time** (stacked by model) / **Cost Per Session Trend** — timeseries

### Row 4: Output Over Time (2 panels)
- **Lines Modified** / **Lines Per Dollar** — timeseries

### Row 5: Model & Token Usage (3 panels)
- **Model Mix Over Time** (100% stacked) / **Token Usage by Type** / **Cache Hit Ratio Over Time** — timeseries

### Row 6: Tool Usage & Edit Behavior (6 panels, collapsed)
- **Tool Usage Breakdown** / **Tool Usage Over Time** / **Edit vs Write Decisions** / **User Prompts Over Time** — timeseries + bar chart
- **Edit vs Write Tool** / **Model Mix (by Cost)** — donut pies
- Uses session-join pattern: tools don't carry `user_email`, so queries join via `session_id`

## PromQL Join Patterns

The central pattern enabling all org-level queries:

```promql
# Slice any Claude Code metric by department
sum by (department)(
    claude_code_cost_usage_USD_total
    * on(user_email) group_left(department)
    user_info{rollup_name=~"$rollup_name", manager_name=~"$manager_name"}
)
```

The `user_info` metric (always value 1) acts as a label bridge — `group_left` pulls org labels onto OTEL metrics via the shared `user_email` key.

For org tree panels, 3-level PromQL aggregation:
1. **User level** — direct metric by `user_email`, `label_replace` to `node_id`
2. **Manager level** — `sum by (manager_email)` via `user_info`, `label_replace` to `node_id`
3. **Rollup level** — `sum by (rollup_email)` via `user_info`, `label_replace` to `node_id`

Combined with `or` and filtered by `* on(node_id) group_left() max by (node_id)(org_tree_node)` to exclude orphan nodes.

## Kubernetes Deployment

Kustomize base manifests in `k8s/base/` deploy all 6 services to any Kubernetes cluster.

### What's included
- **namespace.yaml** — `claudefana` namespace
- **configmaps.yaml** — OTEL Collector pipeline, Prometheus scrape config + alerts, Grafana datasources + dashboard provisioning (5 ConfigMaps)
- **deployments.yaml** — 6 Deployments (otel-collector, prometheus, loki, grafana, jira-tempo-exporter, graph-enrichment)
- **services.yaml** — 6 ClusterIP Services
- **pvcs.yaml** — Prometheus (50Gi) + Grafana (5Gi), `storageClassName` commented out for user to set
- **kustomization.yaml** — generates `grafana-dashboard-json` ConfigMap from `dashboards/*.json`

### What's NOT included (must be provided by overlay)
- **Ingresses** — `ingresses.yaml.example` provides an AWS ALB template; users must customize for their ingress controller and hostnames
- **Secrets** — 3 secrets must be created manually: `claudefana-grafana`, `claudefana-jira`, `claudefana-graph`
- **Container registry** — exporter images use placeholder `your-registry/`; users must build, push, and update the image refs
- **SSO/OAuth** — Grafana defaults to anonymous access; SSO requires a Kustomize patch to add OAuth env vars with org-specific tenant ID, client ID, hostnames

### Values to customize
| Value | File | What to set |
|-------|------|-------------|
| Container registry URL | `deployments.yaml` | Replace `your-registry/` with your ECR/GCR/ACR URL |
| `STORY_POINTS_FIELD` | `deployments.yaml` | Your Jira custom field ID (default: `customfield_10016`) |
| `storageClassName` | `pvcs.yaml` | Uncomment and set to your cluster's StorageClass |
| Ingress hostnames + annotations | `ingresses.yaml.example` | Copy to overlay, set real hostnames and ingress controller |

## Quick Start

1. Clone claudefana core as sibling: `git clone https://github.com/JuanjoFuchs/claudefana.git`
2. Clone this repo alongside it
3. `cp .env.example .env` and configure credentials
4. `docker compose -f docker-compose.enterprise.yaml up -d`
5. Open Grafana at http://localhost:3000

## Known Limitations

1. **Thinking tokens missing from OTEL** — 3-10x visible output, counts toward limits but not exported
2. **No project/repo context** — no OTEL label for which codebase a session targets
3. **Story points field varies** — `STORY_POINTS_FIELD` must match your Jira custom field ID
4. **Tempo API versions vary** — exporter tries v4 POST then falls back to v3 GET
5. **Graph API rate limits** — large orgs may need to increase `REFRESH_INTERVAL`
6. **`equansdatahub-tree-panel` plugin required** — must be installed in Grafana for org hierarchy tree
7. **SSL verification disabled in Jira exporter** — workaround for on-prem self-signed certs
8. **Prometheus pinned to v2.55.1** — v3.x silently drops high-cardinality metrics from `prometheus_client`; see `ai-docs/prometheus-v3-scrape-bug.md`
