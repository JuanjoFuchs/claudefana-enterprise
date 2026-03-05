# AGENTS.md

Enterprise extensions for [claudefana](https://github.com/JuanjoFuchs/claudefana). Adds 2 Python exporters (Microsoft Graph + Jira/Tempo) and 2 Grafana dashboards (40+ panel adoption dashboard + per-user explorer). Pulls in the core stack via Docker Compose `include:`.

**CRITICAL: You MUST read the required files BEFORE taking action.** This is not optional.

## Required Reading by Task

| User asks about... | READ THIS FIRST | Then act |
|---|---|---|
| Understanding the project | @PROJECT_UNDERSTANDING.md | Explore codebase |
| Adding/modifying dashboard panels | @PROJECT_UNDERSTANDING.md (dashboard sections) | Use grep/targeted reads on `dashboards/*.json` — never load full JSON files |
| Modifying the Graph enrichment exporter | @graph-enrichment-exporter/exporter.py | Edit Python script |
| Modifying the Jira/Tempo exporter | @jira-tempo-exporter/exporter.py | Edit Python script |
| Adding/removing Docker services | @docker-compose.enterprise.yaml | Edit compose file |
| Changing metrics scrape targets | @prometheus-enterprise.yml | Edit Prometheus config |
| Azure AD / Graph API setup | @graph-enrichment-exporter/config.yaml.example, @.env.example | Edit config/env |
| Jira / Tempo configuration | @.env.example, @docker-compose.enterprise.yaml | Edit env/compose |
| PromQL join patterns (org slicing) | @PROJECT_UNDERSTANDING.md (PromQL section) | Write queries using `user_info` group_left pattern |
| Org hierarchy tree panel | @ai-docs/org-tree-panel-implementation.md | Edit tree PromQL or exporter tree logic |
| Kubernetes deployment | @k8s/base/kustomization.yaml, @k8s/base/deployments.yaml | Edit K8s manifests |
| OTEL Collector pipeline | @PROJECT_UNDERSTANDING.md (OTEL section), @k8s/base/configmaps.yaml | Edit collector config |
| Prometheus alerts | @k8s/base/configmaps.yaml (prometheus-alerts section) | Edit alert rules |
| Prometheus version/scrape issues | @ai-docs/prometheus-v3-scrape-bug.md | Check version compatibility |

**Do not skip this step.** Read the linked file first, then act.

## Architecture

```
claudefana core (included via compose)
┌──────────────────────────────────────────────┐
│ Claude Code ──▶ OTEL Collector ──▶ Prometheus ──▶ Grafana │
│                      │                 ▲   ▲              │
│                      ▼                 │   │              │
│                    Loki ──────────▶ Grafana │              │
└──────────────────────────────────────────────┘
                                         │   │
                          graph-enrichment   jira-tempo-exporter
                          (:9101)            (:9102)
```

- **graph-enrichment** — queries Microsoft Graph for user profiles + org tree, exports `user_info` and `org_tree_node` gauges. Scoped to Claude Code users found in Prometheus.
- **jira-tempo-exporter** — queries Jira REST + Tempo APIs for issues, story points, worklogs. Scoped to Claude Code users found in Prometheus.
- Both exporters use the `user_email` label as the universal join key with OTEL metrics.

## Conventions

- Compose: `docker compose -f docker-compose.enterprise.yaml up -d` (includes core automatically).
- K8s: `kubectl apply -k k8s/base/`. Secrets created manually, exporter images use `your-registry/` placeholder.
- Dashboard JSONs in `dashboards/` are large — **never read full files**. Use grep to find panels/sections, then make targeted edits.
- Graph enrichment exporter deps: `msal`, `requests`, `prometheus_client`, `pyyaml`. Jira exporter deps: `httpx`, `prometheus_client`, `pyyaml`, `python-dotenv`.
- All org-level PromQL uses `* on(user_email) group_left(...) user_info{...}` join pattern.
- Org tree panels use 3-level aggregation (user + manager + rollup) combined with `or` and `label_replace`.
- No secrets in the repo. Credentials via `.env` (Docker) or K8s Secrets (K8s).
- Config changes require `docker compose restart <service>` or `kubectl rollout restart`.

## Workflow

```
1. READ    → Check the routing table above, read required files
2. SEARCH  → grep/glob for related code or config
3. PLAN    → Propose approach before making changes
4. IMPLEMENT → Edit existing files, follow conventions above
5. VERIFY  → `docker compose config` (validates compose), `kubectl apply -k k8s/base/ --dry-run=client` (validates K8s), or restart stack and check logs
```

## Current State

- Stack works end-to-end: core OTEL + Graph enrichment + Jira/Tempo → enterprise dashboards
- Adoption dashboard: 40+ panels across 5 rows (Adoption & Outcomes, Cost & Usage, Organization, User Deep Dive, System Health)
- User Explorer dashboard: 20+ panels across 6 rows (per-user drill-through)
- Org Hierarchy Tree requires `equansdatahub-tree-panel` Grafana plugin
- 10 Prometheus alert rules for usage limits, burn rate, cache efficiency, exporter health
- K8s manifests in `k8s/base/` — Kustomize base with 6 deployments, services, PVCs, 5 configmaps. Ingresses provided as example only.
- Prometheus pinned to v2.55.1 — v3.x silently drops high-cardinality metrics (see `ai-docs/prometheus-v3-scrape-bug.md`)
- No CI/CD — manual `docker compose up` or `kubectl apply -k`
- No tests — validation is "start the stack and check Grafana"
