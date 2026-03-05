# Org Hierarchy Tree Panel - Implementation Notes

## Overview

Added an interactive org hierarchy tree to the Grafana dashboard using the `equansdatahub-tree-panel` plugin. Shows rollup manager → manager → user hierarchy with aggregated cost/time/session metrics at each level.

## Architecture

### Exporter (`graph-enrichment-exporter/exporter.py`)

**New metric: `org_tree_node`** — a Gauge with labels `node_id`, `parent_id`, `node_label`, `node_type`.

**Tree building logic** (in `refresh_metrics()`, after the user loop):

1. Collect hierarchy data during user loop:
   - `rollup_managers` dict: rollup_email → rollup_name
   - `managers` dict: manager_email → (manager_name, rollup_email)
   - `users` dict: user_email → (display_name, manager_email)
   - `cxo_emails` set: detected when `rollup_email == email` (user is own rollup), their manager is CxO

2. Build tree nodes in memory (list of tuples), then detect orphans:
   - Rollup managers → root nodes (parent_id="")
   - Managers → parent = rollup (skip CxOs)
   - Users → parent = manager (if manager is CxO, parent = "")
   - Orphan detection: any node whose parent_id is non-empty but not in the tree gets re-parented under an "Unknown" root node

3. Emit all nodes as `ORG_TREE_NODE.labels(...).set(1)`

**Rollup resolution** (`resolve_rollup()`):
- Walks up the Graph API management chain
- Rollup manager = person whose direct manager is a CxO
- CxOs are their own rollup
- Uses `_rollup_cache` (persistent module-level dict) for efficiency
- Order of checks matters: `_is_cxo(mgr_title)` before `_is_cxo(title)`

**CxO exclusion from tree:**
- CxOs are ABOVE rollup managers
- If CxOs appear in the tree, hierarchy inverts (CxO appears under their own reports)
- Detection: if `rollup_email == email and manager_email`, then `manager_email` is a CxO
- CxOs excluded from both `managers` dict and tree emission

### Dashboard (`dashboards/claude-code-dashboard.json`)

**Tree panel** (id 701) inside "🏢 Organization" row (id 700), full width.

**8 queries** (all `format: table`, `instant: true`):
- `tree`: `org_tree_node` — the hierarchy structure
- 7 metric queries: Cost, Active Time, Commits, Lines Added, Issues Resolved, Story Points, Tempo Hours

**3-level PromQL aggregation pattern** (no built-in aggregation in tree plugin):
```promql
sum by (node_id)(label_replace(
  sum by (rollup_email)(<metric> * on(user_email) group_left(rollup_email) topk by (user_email)(1, max by (user_email, rollup_email)(user_info{...}))),
  "node_id","$1","rollup_email","(.*)"))
or
sum by (node_id)(label_replace(
  sum by (manager_email)(<same pattern>),
  "node_id","$1","manager_email","(.*)"))
or
sum by (node_id)(label_replace(
  <user-level metric>,
  "node_id","$1","user_email","(.*)"))
```

**CRITICAL: `sum by (node_id)` wrapper** — `label_replace` retains source labels (`rollup_email`, etc.), so `or` sees different label sets and fails to deduplicate. Wrapping in `sum by (node_id)` strips extra labels.

**Panel options:**
- `displayedTreeDepth: 0` — collapsed to root level
- `additionalColumns: "Value #Cost,Value #Active Time,..."` — uses raw refId names
- `orderLevels: "Descending"` — highest values first

## equansdatahub-tree-panel Gotchas

1. **Column naming**: Plugin reads `additionalColumns` by RAW field name only. `organize` rename, `renameByRegex`, and `displayName` overrides are ALL ignored. Headers show correctly but data cells are empty. Use descriptive refIds so "Value #Cost" is readable.

2. **No built-in aggregation**: Parent nodes don't automatically sum children. Must pre-calculate in PromQL.

3. **`displayedTreeDepth`**: 0 = all collapsed, 1 = first level expanded, etc.

4. **Root detection**: Empty string `parent_id=""` is treated as root. Prometheus drops empty-string labels, so `parent_id` label is absent for root nodes — the plugin handles this correctly.

5. **Install via**: `GF_INSTALL_PLUGINS=equansdatahub-tree-panel` env var on Grafana container.

## "undefined" Node Fix

**Root cause**: CxO managers are correctly excluded from `org_tree_node`, but `user_info.manager_email` still references them. The manager-level PromQL aggregation produced a `node_id` for the CxO which had no match in the tree → tree plugin rendered it as "undefined".

**Fix**: Added `* on(node_id) group_left() max by (node_id)(org_tree_node)` to the manager-level branch of all 7 metric queries. This inner-joins with `org_tree_node`, filtering out any `node_id` not present in the tree.

**Before** (manager branch):
```promql
sum by (node_id)(label_replace(sum by (manager_email)(...), "node_id", "$1", "manager_email", "(.*)"))
```

**After** (manager branch):
```promql
sum by (node_id)(label_replace(sum by (manager_email)(...), "node_id", "$1", "manager_email", "(.*)") * on(node_id) group_left() max by (node_id)(org_tree_node))
```

Only the manager-level branch needs this filter. Rollup-level `node_id`s always exist in the tree (rollup managers are root nodes). User-level `node_id`s always exist (all enriched users are leaf nodes).

## Files

| File | Role |
|------|------|
| `graph-enrichment-exporter/exporter.py` | `ORG_TREE_NODE` gauge, tree building with orphan detection, CxO exclusion |
| `dashboards/claude-code-dashboard.json` | Tree panel (id 701) with 8 queries, 3-level aggregation, CxO filter on manager branch |
