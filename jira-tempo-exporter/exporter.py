"""
Jira + Tempo Prometheus Exporter for Claude Code adoption correlation.

Exports issue resolution, story points, and Tempo worklog metrics
keyed by user_email, enabling PromQL joins with Claude Code OTEL data:

    claude_code_cost_usage_USD_total / jira_issues_resolved_total
    → cost per resolved ticket

    tempo_time_logged_seconds / claude_code_active_time_seconds_total
    → ratio of logged hours to AI-assisted time
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from prometheus_client import Gauge, Histogram, start_http_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("jira-tempo-exporter")

# ── Prometheus Metrics ──────────────────────────────────────

ISSUES_RESOLVED = Gauge(
    "jira_issues_resolved_total",
    "Issues resolved in the configured time window",
    ["user_email", "project", "issue_type", "priority"],
)

STORY_POINTS_RESOLVED = Gauge(
    "jira_story_points_resolved_total",
    "Story points resolved in the configured time window",
    ["user_email", "project"],
)

ISSUES_CREATED = Gauge(
    "jira_issues_created_total",
    "Issues created in the configured time window",
    ["user_email", "project", "issue_type"],
)

TEMPO_TIME_LOGGED = Gauge(
    "tempo_time_logged_seconds",
    "Tempo worklog time in seconds",
    ["user_email", "project", "issue_key"],
)

TEMPO_TIME_BY_USER = Gauge(
    "tempo_time_logged_by_user_seconds",
    "Total Tempo worklog time per user in seconds",
    ["user_email"],
)

TEMPO_TEAM_MEMBER = Gauge(
    "tempo_team_member_info",
    "Tempo team membership (value=1, labels carry team data)",
    ["user_email", "username", "team_name", "team_id"],
)

RESOLUTION_TIME = Histogram(
    "jira_issue_resolution_time_hours",
    "Time from issue creation to resolution in hours",
    ["user_email", "project", "issue_type", "priority"],
    buckets=[1, 2, 4, 8, 16, 24, 48, 72, 120, 168, 336, 720],
)

# Org-wide lightweight counts (1 API call each, no pagination)
ISSUES_RESOLVED_ORG = Gauge(
    "jira_issues_resolved_org_total",
    "Total issues resolved org-wide in the configured time window",
)
ISSUES_CREATED_ORG = Gauge(
    "jira_issues_created_org_total",
    "Total issues created org-wide in the configured time window",
)

# Exporter health metrics
LAST_REFRESH = Gauge("jira_exporter_last_refresh_timestamp", "Unix timestamp of last successful refresh")
REFRESH_ERRORS = Gauge("jira_exporter_refresh_errors_total", "Total refresh errors")
USERS_EXPORTED = Gauge("jira_exporter_users_total", "Total users with exported metrics")
CC_USERS_TRACKED = Gauge("jira_exporter_cc_users_total", "Claude Code users being tracked")

# ── Config ──────────────────────────────────────────────────

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9102"))
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "300"))  # 5 min default
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
STORY_POINTS_FIELD = os.environ.get("STORY_POINTS_FIELD", "customfield_13303")
# Comma-separated list of Jira project keys to track (empty = all projects)
PROJECT_FILTER = [p.strip() for p in os.environ.get("PROJECT_FILTER", "").split(",") if p.strip()]
# Prometheus URL for discovering Claude Code users (scoped collection)
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
# Set to "true" to collect all Tempo team memberships (111+ API calls)
COLLECT_TEAMS = os.environ.get("COLLECT_TEAMS", "false").lower() == "true"

# ── HTTP Client ─────────────────────────────────────────────

import ssl
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

client = httpx.Client(
    headers={
        "Authorization": f"Bearer {JIRA_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    verify=_ssl_ctx,
    timeout=30,
)


def jira_get(path):
    """GET from Jira REST API."""
    url = f"{JIRA_URL}{path}"
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Jira API error: {path} → {e}")
        return None


def jira_post(path, body):
    """POST to Jira REST API."""
    url = f"{JIRA_URL}{path}"
    try:
        r = client.post(url, json=body)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Jira API error: POST {path} → {e}")
        return None


# ── User email cache ────────────────────────────────────────

_user_email_cache = {}


def get_user_email(username):
    """Look up a Jira username's email address (cached)."""
    if username in _user_email_cache:
        return _user_email_cache[username]

    user = jira_get(f"/rest/api/2/user?username={quote(username)}")
    if user and user.get("emailAddress"):
        email = user["emailAddress"].lower()
        _user_email_cache[username] = email
        return email

    _user_email_cache[username] = username.lower()
    return username.lower()


def get_email_to_username():
    """Build a reverse map from the user email cache: email → username."""
    return {email: username for username, email in _user_email_cache.items()}


# ── Claude Code user discovery ─────────────────────────────

def get_claude_code_users():
    """Query Prometheus for user_email labels present in Claude Code metrics."""
    try:
        # Use cost metric as it's the most reliably present across all Claude Code versions
        r = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": "group by (user_email) (claude_code_cost_usage_USD_total)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "success":
            return {
                item["metric"]["user_email"].lower()
                for item in data["data"]["result"]
                if item["metric"].get("user_email")
            }
    except Exception as e:
        log.warning(f"Could not query Prometheus for Claude Code users: {e}")
    return set()


def resolve_emails_to_usernames(emails):
    """Resolve a set of emails to Jira usernames for JQL filtering."""
    reverse = get_email_to_username()
    result = {}

    for email in emails:
        # Check reverse cache first
        if email in reverse:
            result[email] = reverse[email]
            continue

        # Search Jira for users matching the email prefix
        prefix = email.split("@")[0]
        users = jira_get(f"/rest/api/2/user/search?username={quote(prefix)}&maxResults=10")
        if users and isinstance(users, list):
            for u in users:
                if u.get("emailAddress", "").lower() == email:
                    uname = u.get("name", u.get("key", ""))
                    _user_email_cache[uname] = email
                    result[email] = uname
                    break

        if email not in result:
            log.warning(f"Could not resolve Jira username for {email}")

    return result


# ── Data collection ─────────────────────────────────────────

def collect_resolved_issues(usernames=None):
    """Query Jira for resolved issues in the lookback window.

    Args:
        usernames: If provided, scope JQL to only these Jira usernames.
    """
    scope = f" for {len(usernames)} users" if usernames else " (org-wide)"
    log.info(f"Collecting resolved issues (last {LOOKBACK_DAYS}d){scope}...")

    clauses = [f"resolved >= -{LOOKBACK_DAYS}d"]
    if PROJECT_FILTER:
        clauses.append(f"project IN ({', '.join(PROJECT_FILTER)})")
    if usernames:
        user_list = ", ".join(usernames.values())
        clauses.append(f"assignee IN ({user_list})")

    jql = quote(" AND ".join(clauses) + " ORDER BY resolved DESC")
    fields = f"assignee,project,issuetype,priority,created,resolutiondate,{STORY_POINTS_FIELD}"

    start_at = 0
    max_results = 100
    total = None
    all_issues = []

    while total is None or start_at < total:
        result = jira_get(
            f"/rest/api/2/search?jql={jql}&fields={fields}"
            f"&maxResults={max_results}&startAt={start_at}"
        )
        if not result or "issues" not in result:
            break

        total = result.get("total", 0)
        all_issues.extend(result["issues"])
        start_at += max_results

        if start_at >= 5000:  # Safety cap
            log.warning("Hit 5000 issue safety cap")
            break

    log.info(f"Found {len(all_issues)} resolved issues")

    # Clear old metrics
    ISSUES_RESOLVED._metrics.clear()
    STORY_POINTS_RESOLVED._metrics.clear()
    RESOLUTION_TIME._metrics.clear()

    # Aggregate by user
    user_points = {}  # (email, project) → points

    for issue in all_issues:
        f = issue.get("fields", {})
        assignee = f.get("assignee")
        if not assignee:
            continue

        username = assignee.get("name", assignee.get("key", ""))
        email = get_user_email(username)
        project = (f.get("project") or {}).get("key", "unknown")
        issue_type = (f.get("issuetype") or {}).get("name", "unknown")
        priority = (f.get("priority") or {}).get("name", "unknown")

        # Resolution time histogram
        created_str = f.get("created")
        resolved_str = f.get("resolutiondate")
        if created_str and resolved_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                resolved_dt = datetime.fromisoformat(resolved_str.replace("Z", "+00:00"))
                hours = (resolved_dt - created_dt).total_seconds() / 3600
                if hours > 0:
                    RESOLUTION_TIME.labels(
                        user_email=email,
                        project=project,
                        issue_type=issue_type,
                        priority=priority,
                    ).observe(hours)
            except (ValueError, TypeError):
                pass

        ISSUES_RESOLVED.labels(
            user_email=email,
            project=project,
            issue_type=issue_type,
            priority=priority,
        ).inc()

        # Story points
        points = f.get(STORY_POINTS_FIELD)
        if points and isinstance(points, (int, float)):
            key = (email, project)
            user_points[key] = user_points.get(key, 0) + points

    for (email, project), points in user_points.items():
        STORY_POINTS_RESOLVED.labels(user_email=email, project=project).set(points)

    return len(all_issues)


def collect_created_issues(usernames=None):
    """Query Jira for created issues in the lookback window.

    Args:
        usernames: If provided, scope JQL to only these Jira usernames.
    """
    scope = f" for {len(usernames)} users" if usernames else " (org-wide)"
    log.info(f"Collecting created issues (last {LOOKBACK_DAYS}d){scope}...")

    clauses = [f"created >= -{LOOKBACK_DAYS}d"]
    if PROJECT_FILTER:
        clauses.append(f"project IN ({', '.join(PROJECT_FILTER)})")
    if usernames:
        user_list = ", ".join(usernames.values())
        clauses.append(f"assignee IN ({user_list})")

    jql = quote(" AND ".join(clauses) + " ORDER BY created DESC")
    fields = "assignee,project,issuetype"

    start_at = 0
    max_results = 100
    total = None

    ISSUES_CREATED._metrics.clear()

    count = 0
    while total is None or start_at < total:
        result = jira_get(
            f"/rest/api/2/search?jql={jql}&fields={fields}"
            f"&maxResults={max_results}&startAt={start_at}"
        )
        if not result or "issues" not in result:
            break

        total = result.get("total", 0)

        for issue in result["issues"]:
            f = issue.get("fields", {})
            assignee = f.get("assignee")
            if not assignee:
                continue

            username = assignee.get("name", assignee.get("key", ""))
            email = get_user_email(username)
            project = (f.get("project") or {}).get("key", "unknown")
            issue_type = (f.get("issuetype") or {}).get("name", "unknown")

            ISSUES_CREATED.labels(
                user_email=email,
                project=project,
                issue_type=issue_type,
            ).inc()
            count += 1

        start_at += max_results
        if start_at >= 5000:
            break

    log.info(f"Found {count} created issues with assignees")
    return count


def collect_tempo_worklogs(target_emails=None):
    """Query Tempo for worklogs in the lookback window.

    Args:
        target_emails: If provided, only emit metrics for these emails.
    """
    scope = f" (filtering to {len(target_emails)} users)" if target_emails else ""
    log.info(f"Collecting Tempo worklogs (last {LOOKBACK_DAYS}d){scope}...")

    today = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    # Use Tempo search endpoint (POST)
    result = jira_post(
        "/rest/tempo-timesheets/4/worklogs/search",
        {"from": from_date, "to": today},
    )

    if not result:
        # Fallback to GET
        result = jira_get(
            f"/rest/tempo-timesheets/3/worklogs?dateFrom={from_date}&dateTo={today}"
        )

    entries = []
    if isinstance(result, list):
        entries = result
    elif isinstance(result, dict):
        entries = result.get("results", result.get("worklogs", []))

    log.info(f"Found {len(entries)} Tempo worklogs")

    TEMPO_TIME_LOGGED._metrics.clear()
    TEMPO_TIME_BY_USER._metrics.clear()

    user_totals = {}

    # Build reverse lookup to skip expensive email resolution for non-target users
    # Use lowercase for comparison — Jira and Tempo may differ in case
    target_usernames = None
    if target_emails:
        reverse = get_email_to_username()
        target_usernames = {uname.lower() for email, uname in reverse.items() if email in target_emails}
        log.info(f"  Tempo fast-path filter: {len(target_usernames)} target usernames")

    for entry in entries:
        worker = entry.get("worker", {})
        username = worker if isinstance(worker, str) else worker.get("name", worker.get("key", ""))
        if not username:
            continue

        # Fast-path: skip workers we know aren't in our target set
        if target_usernames and username.lower() not in target_usernames:
            continue

        email = get_user_email(username)

        # Double-check email filter (handles edge cases where username wasn't in cache)
        if target_emails and email not in target_emails:
            continue

        issue = entry.get("issue", {})
        issue_key = issue.get("key", "unknown") if isinstance(issue, dict) else str(issue)
        # Extract project from issue key (e.g., "PROJ-123" → "PROJ")
        project = issue_key.rsplit("-", 1)[0] if "-" in issue_key else "unknown"
        seconds = entry.get("timeSpentSeconds", 0)

        TEMPO_TIME_LOGGED.labels(
            user_email=email,
            project=project,
            issue_key=issue_key,
        ).inc(seconds)

        user_totals[email] = user_totals.get(email, 0) + seconds

    for email, total in user_totals.items():
        TEMPO_TIME_BY_USER.labels(user_email=email).set(total)

    return len(entries)


def collect_tempo_teams():
    """Query Tempo for team membership. Expensive: 1 + N API calls for N teams."""
    if not COLLECT_TEAMS:
        log.info("Skipping Tempo teams (COLLECT_TEAMS=false)")
        return 0
    log.info("Collecting Tempo team membership...")

    teams = jira_get("/rest/tempo-teams/2/team")
    if not teams:
        return 0

    team_list = teams if isinstance(teams, list) else teams.get("results", [])

    TEMPO_TEAM_MEMBER._metrics.clear()
    member_count = 0

    for team in team_list:
        team_id = str(team.get("id", ""))
        team_name = team.get("name", "unknown")

        members = jira_get(f"/rest/tempo-teams/2/team/{team_id}/member")
        if not members:
            continue

        mem_list = members if isinstance(members, list) else members.get("results", [])

        for m in mem_list:
            member = m.get("member", m)
            username = member.get("name", member.get("key", ""))
            if not username:
                continue

            email = get_user_email(username)

            TEMPO_TEAM_MEMBER.labels(
                user_email=email,
                username=username,
                team_name=team_name,
                team_id=team_id,
            ).set(1)
            member_count += 1

    log.info(f"Exported {member_count} team memberships across {len(team_list)} teams")
    return member_count


# ── Org-wide stats (lightweight) ─────────────────────────────

def collect_org_wide_stats():
    """Get org-wide issue counts with a single API call each (maxResults=0)."""
    log.info("Collecting org-wide stats...")

    project_clause = ""
    if PROJECT_FILTER:
        project_clause = f" AND project IN ({', '.join(PROJECT_FILTER)})"

    # Resolved count
    jql = quote(f"resolved >= -{LOOKBACK_DAYS}d{project_clause}")
    result = jira_get(f"/rest/api/2/search?jql={jql}&maxResults=0")
    if result:
        ISSUES_RESOLVED_ORG.set(result.get("total", 0))
        log.info(f"  Org-wide resolved: {result.get('total', 0)}")

    # Created count
    jql = quote(f"created >= -{LOOKBACK_DAYS}d{project_clause}")
    result = jira_get(f"/rest/api/2/search?jql={jql}&maxResults=0")
    if result:
        ISSUES_CREATED_ORG.set(result.get("total", 0))
        log.info(f"  Org-wide created: {result.get('total', 0)}")


# ── Main loop ───────────────────────────────────────────────

def refresh_all():
    """Run all collectors, scoped to Claude Code users when available."""
    log.info("Starting refresh cycle...")
    try:
        # Discover which users have Claude Code metrics
        cc_emails = get_claude_code_users()
        CC_USERS_TRACKED.set(len(cc_emails))

        if cc_emails:
            log.info(f"Scoping to {len(cc_emails)} Claude Code users: {cc_emails}")
            usernames = resolve_emails_to_usernames(cc_emails)

            resolved = collect_resolved_issues(usernames)
            created = collect_created_issues(usernames)
            worklogs = collect_tempo_worklogs(cc_emails)
        else:
            log.info("No Claude Code users found in Prometheus — skipping scoped collection")
            resolved = 0
            created = 0
            worklogs = 0

        # Lightweight org-wide counts (2 API calls total)
        collect_org_wide_stats()

        # Team membership (opt-in, expensive)
        members = collect_tempo_teams()

        USERS_EXPORTED.set(len(_user_email_cache))
        LAST_REFRESH.set(time.time())

        log.info(
            f"Refresh complete: {resolved} resolved, {created} created, "
            f"{worklogs} worklogs, {members} team members, "
            f"{len(cc_emails)} CC users tracked"
        )
    except Exception as e:
        log.error(f"Refresh failed: {e}", exc_info=True)
        REFRESH_ERRORS.inc()


def main():
    if not JIRA_URL or not JIRA_TOKEN:
        log.error("JIRA_URL and JIRA_API_TOKEN environment variables are required")
        return

    log.info(f"Jira Tempo Exporter starting on :{EXPORTER_PORT}")
    log.info(f"  Jira URL: {JIRA_URL}")
    log.info(f"  Lookback: {LOOKBACK_DAYS} days")
    log.info(f"  Refresh interval: {REFRESH_INTERVAL}s")
    log.info(f"  Story points field: {STORY_POINTS_FIELD}")
    log.info(f"  Prometheus URL: {PROMETHEUS_URL}")
    log.info(f"  Collect teams: {COLLECT_TEAMS}")
    if PROJECT_FILTER:
        log.info(f"  Project filter: {PROJECT_FILTER}")

    # Verify connectivity
    me = jira_get("/rest/api/2/myself")
    if not me:
        log.error("Cannot connect to Jira API. Check JIRA_URL and JIRA_API_TOKEN.")
        return
    log.info(f"  Connected as: {me.get('displayName')} ({me.get('emailAddress')})")

    start_http_server(EXPORTER_PORT)
    log.info(f"Prometheus metrics available at :{EXPORTER_PORT}/metrics")

    while True:
        refresh_all()
        log.info(f"Next refresh in {REFRESH_INTERVAL}s")
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
