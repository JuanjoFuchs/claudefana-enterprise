# Prometheus 3.x Scrape Bug: Drops High-Cardinality Metrics

## Problem

Prometheus 3.10.0 (`prom/prometheus:latest` as of 2026-03-04) silently drops high-cardinality gauge metrics from the Python `prometheus_client` library (v0.21.1). Only low-cardinality metrics (process_*, python_*, simple gauges without labels) are ingested. Metrics like `user_info` (13 labels, 70 series) and `org_tree_node` (4 labels, 89 series) are completely dropped.

## Symptoms

- `scrape_samples_scraped` shows only ~21 (process/python/simple metrics) instead of 180+
- Target health shows `up=1`, no `lastError`, scrape duration ~2ms
- `promtool check metrics` reports NO errors on the exporter output
- `wget` from inside Prometheus pod retrieves full 218-line response with all metrics
- The metric names appear in `targets/metadata` but `count(user_info)` returns empty
- Even a trivial 2-entry test gauge (`test_labeled_gauge`) with 3 labels is dropped

## Root Cause

Unknown exact root cause in Prometheus 3.10.0. Likely a regression in the scrape parser or TSDB ingestion pipeline. The issue is NOT:
- Content negotiation (tested with forced `text/plain; version=0.0.4`)
- OpenMetrics format (tested with custom handler bypassing OpenMetrics)
- Compression (`enable_compression: false` had no effect)
- Label count or values (even 3-label metrics with ASCII-only values are dropped)
- TSDB corruption (fresh PVC with empty TSDB still fails)
- Sample/series limits (none configured, defaults are unlimited)
- Metric relabeling (none configured)
- Network issues (wget from Prometheus pod gets full response)

## Fix

**Downgrade to Prometheus v2.55.1.** This immediately resolves the issue.

### Steps taken:

1. Changed `k8s/deployments.yaml`:
   ```yaml
   image: prom/prometheus:v2.55.1  # was prom/prometheus:latest (v3.10.0)
   ```

2. Changed Prometheus args (v2.55 doesn't support `--web.enable-otlp-receiver` as a standalone flag):
   ```yaml
   args:
     - "--config.file=/etc/prometheus/prometheus.yml"
     - "--storage.tsdb.path=/prometheus"
     - "--web.enable-lifecycle"
     - "--enable-feature=otlp-write-receiver"
   # Removed: --web.enable-otlp-receiver (not supported in v2)
   # Removed: --enable-feature=otlp-deltatocumulative (v3 only)
   ```

3. Removed `otlp:` config section from `k8s/configmaps.yaml` prometheus.yml (v3-only syntax):
   ```yaml
   # REMOVED - not supported in v2.55:
   # otlp:
   #   promote_resource_attributes:
   #     - service.name
   #     - service.version
   ```

4. **CRITICAL**: Deleted the Prometheus PVC (`kubectl delete pvc prometheus-data`) and recreated it. The v3 TSDB format on the PVC is incompatible with v2.55. Without this step, v2.55 silently fails to ingest new data.

5. Added `strategy: type: Recreate` to Prometheus deployment (PVC can't be accessed by two pods simultaneously, so RollingUpdate causes lock errors).

### After fix:
- `scrape_samples_scraped` shows 80+ for graph-enrichment (was 21)
- `count(user_info)` returns 70 (was 0)
- `count(org_tree_node)` returns 89 (was 0)

## Verification Script

```bash
kubectl exec deployment/prometheus -n claudefana -- sh -c '
wget -q -O - "http://localhost:9090/api/v1/query?query=scrape_samples_scraped" 2>&1
' | python -c "
import json, sys
for r in json.load(sys.stdin)['data']['result']:
    print(f'{r[\"metric\"].get(\"job\",\"?\")}: {r[\"value\"][1]}')
"
```

## Key Debugging Learnings

1. **subPath ConfigMap mounts don't hot-reload.** `/-/reload` re-reads the file from disk, but with `subPath`, the kubelet doesn't update the file when the ConfigMap changes. Must restart the pod.

2. **Test with a separate Prometheus pod.** Creating a minimal `kubectl run` Prometheus pod with a simple config was the breakthrough — it proved the exporter was fine and the main Prometheus was the problem.

3. **`scrape_samples_scraped` is what Prometheus PARSED, not what the exporter served.** If this is lower than expected, the issue is in Prometheus's parsing/ingestion, not the exporter.

4. **Prometheus v3 TSDB is incompatible with v2.** Downgrading requires deleting the PVC data. v2.55 will replay v3 WAL segments without errors but won't ingest new data correctly.

5. **`promtool check metrics` validates format but doesn't test the actual scrape pipeline.** It can show "no errors" while Prometheus's internal scraper drops the same metrics.
