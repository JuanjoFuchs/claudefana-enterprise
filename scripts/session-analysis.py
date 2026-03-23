"""
Claude Code ROI - Session Efficiency Analysis

Queries Loki and Prometheus to produce a Markdown report on per-session
efficiency, context growth curves, and prompt quality.

Designed to run as an ephemeral pod inside the claudefana namespace:
    kubectl run session-report --rm -i --restart=Never -n claudefana \
      --image=python:3.12-slim -- python3 - < scripts/session-analysis.py

Environment variables:
    LOKI_URL          (default http://loki:3100)
    PROMETHEUS_URL    (default http://prometheus:9090)
    LOOKBACK_DAYS     (default 7)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

NOW = datetime.now(timezone.utc)
START = NOW - timedelta(days=LOOKBACK_DAYS)

# Nanosecond epoch strings for Loki
START_NS = str(int(START.timestamp() * 1e9))
END_NS = str(int(NOW.timestamp() * 1e9))


def _log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def prom_query(expr):
    """Instant query against Prometheus. Returns the result list."""
    url = PROMETHEUS_URL + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        resp = json.loads(urllib.request.urlopen(url, timeout=30).read())
    except Exception as exc:
        _log(f"[WARN] Prometheus query failed: {exc}")
        return []
    if resp.get("status") != "success":
        _log(f"[WARN] Prometheus non-success: {resp.get('error', resp.get('status'))}")
        return []
    return resp.get("data", {}).get("result", [])


def loki_query(query, start=START_NS, end=END_NS, limit=5000):
    """Query Loki query_range endpoint. Returns stream results."""
    params = {
        "query": query,
        "start": start,
        "end": end,
        "limit": str(limit),
        "direction": "forward",
    }
    url = LOKI_URL + "/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    try:
        resp = json.loads(urllib.request.urlopen(url, timeout=60).read())
    except Exception as exc:
        _log(f"[WARN] Loki query failed: {exc}")
        return []
    if resp.get("status") != "success":
        _log(f"[WARN] Loki non-success: {resp.get('error', resp.get('status'))}")
        return []
    return resp.get("data", {}).get("result", [])


def parse_loki_values(streams):
    """Extract log entry dicts from Loki stream results.

    Each stream has a list of [timestamp, line] pairs.  We parse each line
    as JSON and yield the resulting dict.
    """
    entries = []
    for stream in streams:
        for ts, line in stream.get("values", []):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


# ---------------------------------------------------------------------------
# Data collection — Loki
# ---------------------------------------------------------------------------

def collect_api_requests():
    """Return list of dicts with per-request fields from api_request events."""
    _log("Querying Loki for api_request events ...")
    streams = loki_query('{service_name="claude-code"} | json | event_name="api_request"')
    entries = parse_loki_values(streams)
    _log(f"  -> {len(entries)} api_request entries")

    results = []
    for e in entries:
        results.append({
            "session_id": e.get("session_id", ""),
            "user_email": e.get("user_email", ""),
            "model": e.get("model", ""),
            "input_tokens": _int(e.get("input_tokens")),
            "cache_read_tokens": _int(e.get("cache_read_tokens")),
            "cache_creation_tokens": _int(e.get("cache_creation_tokens")),
            "output_tokens": _int(e.get("output_tokens")),
            "cost_usd": _float(e.get("cost_usd")),
        })
    return results


def collect_user_prompts():
    """Return list of dicts with per-prompt fields from user_prompt events."""
    _log("Querying Loki for user_prompt events ...")
    streams = loki_query('{service_name="claude-code"} | json | event_name="user_prompt"')
    entries = parse_loki_values(streams)
    _log(f"  -> {len(entries)} user_prompt entries")

    results = []
    for e in entries:
        results.append({
            "user_email": e.get("user_email", ""),
            "session_id": e.get("session_id", ""),
            "prompt_length": _int(e.get("prompt_length")),
        })
    return results


# ---------------------------------------------------------------------------
# Data collection — Prometheus
# ---------------------------------------------------------------------------

def collect_prom_session_aggregates():
    """Return dict of session_id -> {cost, tool_calls, lines_added, accepted_edits}."""
    lb = LOOKBACK_DAYS

    _log("Querying Prometheus for per-session aggregates ...")
    cost_results = prom_query(
        f'sum by (session_id) (max_over_time(claude_code_cost_usage_USD_total[{lb}d]))'
    )
    tool_results = prom_query(
        f'sum by (session_id) (max_over_time(claude_code_tool_calls_total[{lb}d]))'
    )
    lines_results = prom_query(
        f'sum by (session_id) (max_over_time(claude_code_lines_of_code_count_total{{type="added"}}[{lb}d]))'
    )
    edits_results = prom_query(
        f'sum by (session_id) (max_over_time(claude_code_code_edit_tool_decision_total{{decision="accept"}}[{lb}d]))'
    )

    sessions = {}

    def _merge(results, key):
        for r in results:
            sid = r["metric"].get("session_id", "")
            if not sid:
                continue
            sessions.setdefault(sid, {"cost": 0, "tool_calls": 0, "lines_added": 0, "accepted_edits": 0})
            sessions[sid][key] = _float(r["value"][1]) if len(r.get("value", [])) > 1 else 0

    _merge(cost_results, "cost")
    _merge(tool_results, "tool_calls")
    _merge(lines_results, "lines_added")
    _merge(edits_results, "accepted_edits")

    _log(f"  -> {len(sessions)} sessions from Prometheus")
    return sessions


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_div(a, b):
    return a / b if b else None


def build_session_table(prom_sessions, api_requests):
    """Enrich Prometheus session data with user info from Loki."""
    # Map session -> user from api_request entries
    session_user = {}
    session_max_context = {}

    for req in api_requests:
        sid = req["session_id"]
        if sid and req["user_email"]:
            session_user[sid] = req["user_email"]
        context = req["input_tokens"] + req["cache_read_tokens"]
        if context > session_max_context.get(sid, 0):
            session_max_context[sid] = context

    rows = []
    for sid, data in prom_sessions.items():
        cost = data["cost"]
        tc = data["tool_calls"]
        la = data["lines_added"]
        ae = data["accepted_edits"]
        rows.append({
            "session_id": sid,
            "user": session_user.get(sid, "unknown"),
            "cost": cost,
            "tool_calls": int(tc),
            "cost_per_tool_call": _safe_div(cost, tc),
            "lines_added": int(la),
            "accepted_edits": int(ae),
            "cost_per_edit": _safe_div(cost, ae),
            "cost_per_line": _safe_div(cost, la),
            "max_context": session_max_context.get(sid, 0),
        })
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows


def compute_org_median_cost_per_tool_call(rows):
    values = [r["cost_per_tool_call"] for r in rows if r["cost_per_tool_call"] is not None]
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2 == 0:
        return (values[mid - 1] + values[mid]) / 2
    return values[mid]


def detect_breakpoint(api_requests, org_median):
    """Find context size where $/tool-call first exceeds 2x the org median.

    Approximation: bucket requests by context window size (input + cache_read),
    compute average cost per request in each bucket, and find the threshold.
    """
    if org_median is None or org_median <= 0:
        return None

    threshold = org_median * 2

    # Group by session, sort requests by order, track cumulative cost/calls
    session_reqs = {}
    for req in api_requests:
        sid = req["session_id"]
        if sid:
            session_reqs.setdefault(sid, []).append(req)

    # Collect (context_size, cost) pairs across all sessions
    context_cost_pairs = []
    for sid, reqs in session_reqs.items():
        for req in reqs:
            context = req["input_tokens"] + req["cache_read_tokens"]
            cost = req["cost_usd"]
            if context > 0 and cost > 0:
                context_cost_pairs.append((context, cost))

    if not context_cost_pairs:
        return None

    context_cost_pairs.sort(key=lambda x: x[0])

    # Sliding window: bucket into 10k-token bands
    BAND = 10000
    buckets = {}
    for ctx, cost in context_cost_pairs:
        band = (ctx // BAND) * BAND
        buckets.setdefault(band, []).append(cost)

    for band in sorted(buckets.keys()):
        costs = buckets[band]
        avg_cost = sum(costs) / len(costs)
        if avg_cost > threshold:
            return band

    return None


def compute_prompt_quality(prompts):
    """Per-user prompt statistics."""
    user_data = {}
    for p in prompts:
        user = p["user_email"] or "unknown"
        user_data.setdefault(user, []).append(p["prompt_length"])

    rows = []
    for user, lengths in user_data.items():
        total = len(lengths)
        avg_len = sum(lengths) / total if total else 0
        short = sum(1 for l in lengths if l < 20)
        rows.append({
            "user": user,
            "prompts": total,
            "avg_length": avg_len,
            "short_pct": (short / total * 100) if total else 0,
        })
    rows.sort(key=lambda r: r["prompts"], reverse=True)
    return rows


def build_context_curves(api_requests, session_rows):
    """For the top 5 sessions by cost, build per-request context curves."""
    top_sessions = [r["session_id"] for r in session_rows[:5]]
    session_user = {r["session_id"]: r["user"] for r in session_rows}

    session_reqs = {}
    for req in api_requests:
        sid = req["session_id"]
        if sid in top_sessions:
            session_reqs.setdefault(sid, []).append(req)

    curves = []
    for sid in top_sessions:
        reqs = session_reqs.get(sid, [])
        cum_cost = 0.0
        rows = []
        for i, req in enumerate(reqs, 1):
            cum_cost += req["cost_usd"]
            rows.append({
                "request_num": i,
                "cache_read_tokens": req["cache_read_tokens"],
                "cost": req["cost_usd"],
                "cumulative_cost": cum_cost,
            })
        curves.append({
            "session_id": sid,
            "user": session_user.get(sid, "unknown"),
            "rows": rows,
        })
    return curves


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def fmt_usd(v):
    if v is None:
        return "N/A"
    return f"${v:.4f}"


def fmt_tokens(v):
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)


def truncate_id(sid, length=12):
    return sid[:length] if len(sid) > length else sid


def render_report(session_rows, org_median, breakpoint_tokens, prompt_rows, curves):
    lines = []

    lines.append("# Claude Code ROI -- Session Efficiency Report")
    lines.append("")
    lines.append(
        f"**Period:** Last {LOOKBACK_DAYS} days | "
        f"**Generated:** {NOW.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    lines.append("")

    # -- Session Efficiency Summary --
    lines.append("## Session Efficiency Summary")
    lines.append("")
    lines.append(
        "| Session ID | User | Cost | Tool Calls | $/Tool-Call | "
        "Lines Added | Accepted Edits | Max Context |"
    )
    lines.append(
        "|------------|------|------|------------|-------------|"
        "-------------|----------------|-------------|"
    )
    for r in session_rows[:20]:
        lines.append(
            f"| {truncate_id(r['session_id'])} "
            f"| {r['user']} "
            f"| {fmt_usd(r['cost'])} "
            f"| {r['tool_calls']} "
            f"| {fmt_usd(r['cost_per_tool_call'])} "
            f"| {r['lines_added']} "
            f"| {r['accepted_edits']} "
            f"| {fmt_tokens(r['max_context'])} |"
        )
    lines.append("")

    if not session_rows:
        lines.append("*No session data found for this period.*")
        lines.append("")

    # -- Context Efficiency Breakpoint --
    lines.append("## Context Efficiency Breakpoint")
    lines.append("")
    if org_median is not None:
        lines.append(f"Org median $/tool-call: {fmt_usd(org_median)}")
    else:
        lines.append("Org median $/tool-call: N/A (insufficient data)")

    if breakpoint_tokens is not None:
        lines.append(
            f"Recommended context limit: {fmt_tokens(breakpoint_tokens)} tokens "
            f"(where $/tool-call exceeds 2x median)"
        )
    else:
        lines.append(
            "Recommended context limit: Not detected "
            "(cost stays within 2x median across all context sizes, or insufficient data)"
        )
    lines.append("")

    # -- Prompt Quality by User --
    lines.append("## Prompt Quality by User")
    lines.append("")
    lines.append("| User | Prompts | Avg Length | Short% (<20 chars) |")
    lines.append("|------|---------|------------|---------------------|")
    for r in prompt_rows:
        lines.append(
            f"| {r['user']} "
            f"| {r['prompts']} "
            f"| {r['avg_length']:.0f} "
            f"| {r['short_pct']:.1f}% |"
        )
    lines.append("")

    if not prompt_rows:
        lines.append("*No prompt data found for this period.*")
        lines.append("")

    # -- Context Growth Curves --
    lines.append("## Context Growth Curves (Top 5 Sessions by Cost)")
    lines.append("")
    if not curves:
        lines.append("*No context growth data available.*")
        lines.append("")

    for curve in curves:
        lines.append(f"### Session {truncate_id(curve['session_id'])} ({curve['user']})")
        lines.append("")
        lines.append("| Request # | Cache Read Tokens | Cost | Cumulative Cost |")
        lines.append("|-----------|-------------------|------|-----------------|")
        for row in curve["rows"]:
            lines.append(
                f"| {row['request_num']} "
                f"| {fmt_tokens(row['cache_read_tokens'])} "
                f"| {fmt_usd(row['cost'])} "
                f"| {fmt_usd(row['cumulative_cost'])} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _log(f"Session Efficiency Analysis - lookback {LOOKBACK_DAYS}d")
    _log(f"  Loki:       {LOKI_URL}")
    _log(f"  Prometheus: {PROMETHEUS_URL}")
    _log(f"  Range:      {START.isoformat()} -> {NOW.isoformat()}")
    _log("")

    api_requests = collect_api_requests()
    prompts = collect_user_prompts()
    prom_sessions = collect_prom_session_aggregates()

    session_rows = build_session_table(prom_sessions, api_requests)
    org_median = compute_org_median_cost_per_tool_call(session_rows)
    breakpoint_tokens = detect_breakpoint(api_requests, org_median)
    prompt_rows = compute_prompt_quality(prompts)
    curves = build_context_curves(api_requests, session_rows)

    report = render_report(session_rows, org_median, breakpoint_tokens, prompt_rows, curves)
    print(report)

    _log("")
    _log(f"Report complete. {len(session_rows)} sessions, {len(api_requests)} API requests, {len(prompts)} prompts.")


if __name__ == "__main__":
    main()
