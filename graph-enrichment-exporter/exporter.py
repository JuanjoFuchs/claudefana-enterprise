"""
Microsoft Graph API User Enrichment Exporter for Prometheus.

Exports a `user_info` gauge metric with organizational labels
(department, job_title, manager, company, office, city, country)
keyed by user_email. This enables PromQL joins like:

    sum by (department)(
        claude_code_cost_usage_USD_total
        * on(user_email) group_left(department)
        user_info
    )
"""

import json as _json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

import msal
import requests
import yaml
from prometheus_client import Gauge, start_http_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("graph-enrichment")

# Prometheus metric: value is always 1, labels carry the org data
USER_INFO = Gauge(
    "user_info",
    "User organizational metadata from Microsoft Graph API",
    [
        "user_email",
        "display_name",
        "department",
        "job_title",
        "manager_name",
        "manager_email",
        "rollup_name",
        "rollup_email",
        "company",
        "office",
        "city",
        "country",
        "employee_id",
    ],
)

ORG_TREE_NODE = Gauge(
    "org_tree_node",
    "Organizational tree node for hierarchy visualization",
    ["node_id", "parent_id", "node_label", "node_type"],
)

# ---------------------------------------------------------------------------
# Rollup Manager Resolution
#
# Walk up the org chart to find the "rollup manager" — the CxO direct report.
# CxO = anyone with "Chief" in their title, or "President" (but NOT
# "Vice President" or "Senior Vice President").  A CxO can report to
# another CxO (e.g. CTO → CIO), so we keep climbing until the manager
# is a CxO, then the current person is the rollup manager.
# ---------------------------------------------------------------------------


def _is_cxo(job_title: str) -> bool:
    """Check if a job title indicates a CxO-level executive.

    Matches: Chief *, CTO, CIO, CFO, COO, CEO, President
    Does NOT match: Vice President, Senior Vice President, VP
    """
    title = (job_title or "").lower().strip()
    if not title:
        return False
    # "Chief" anything
    if re.search(r"\bchief\b", title):
        return True
    # Common abbreviations
    if re.search(r"\b(ceo|cto|cio|cfo|coo|cmo|cpo)\b", title):
        return True
    # "President" but NOT "Vice President"
    if re.search(r"\bpresident\b", title) and not re.search(r"\bvice\s*president\b", title):
        return True
    return False


# Persistent across refresh cycles so subsequent runs are near-free
_rollup_cache: dict[str, tuple[str, str]] = {}


def resolve_rollup(token: str, start_email: str, api_calls: list[int]) -> tuple[str, str]:
    """Walk up the management chain to find the rollup manager.

    Rollup manager = the person whose direct manager is a CxO.
    If the person IS a CxO, they are their own rollup.
    Returns (rollup_name, rollup_email).  Uses _rollup_cache so shared
    branches resolve with zero additional API calls.
    """
    if start_email in _rollup_cache:
        return _rollup_cache[start_email]

    chain: list[tuple[str, str]] = []  # (email, display_name)
    current_email = start_email

    for _ in range(15):  # depth safety limit
        # Hit cache mid-chain → everyone below shares the same rollup
        if current_email in _rollup_cache:
            result = _rollup_cache[current_email]
            for email, _ in chain:
                _rollup_cache[email] = result
            return result

        # Fetch person + their manager (with jobTitle) in one call
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(current_email)}"
            f"?$select=displayName,mail,jobTitle"
            f"&$expand=manager($select=displayName,mail,jobTitle)"
        )
        api_calls[0] += 1
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        name = data.get("displayName") or ""
        title = data.get("jobTitle") or ""
        chain.append((current_email, name))

        manager = data.get("manager")
        if not manager:
            # Top of org (no manager) — this person is the rollup
            result = (name, current_email)
            for email, _ in chain:
                _rollup_cache[email] = result
            return result

        mgr_title = manager.get("jobTitle") or ""
        mgr_email = (manager.get("mail") or "").lower()

        if _is_cxo(mgr_title):
            # This person's manager is a CxO → this person is the rollup
            result = (name, current_email)
            for email, _ in chain:
                _rollup_cache[email] = result
            return result

        # If this person IS a CxO, they are their own rollup
        if _is_cxo(title):
            result = (name, current_email)
            for email, _ in chain:
                _rollup_cache[email] = result
            return result

        if not mgr_email:
            break
        current_email = mgr_email

    # Couldn't resolve
    result = ("", "")
    for email, _ in chain:
        _rollup_cache[email] = result
    return result

USERS_TOTAL = Gauge("graph_enrichment_users_total", "Total users exported")
LAST_REFRESH = Gauge("graph_enrichment_last_refresh_timestamp", "Unix timestamp of last successful refresh")
REFRESH_ERRORS = Gauge("graph_enrichment_refresh_errors_total", "Total refresh errors")
GRAPH_API_CALLS = Gauge("graph_enrichment_api_calls_total", "Total Graph API calls made in last refresh")
ORG_HEADCOUNT = Gauge("graph_enrichment_org_headcount", "Total enabled users in the Azure AD tenant")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_access_token(config: dict) -> str:
    """Acquire an app-only token using client credentials flow."""
    azure = config["azure"]
    authority = f"https://login.microsoftonline.com/{azure['tenant_id']}"

    app = msal.ConfidentialClientApplication(
        azure["client_id"],
        authority=authority,
        client_credential=azure["client_secret"],
    )

    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result.get('error_description', result)}")

    return result["access_token"]


def get_claude_code_users(prometheus_url: str, lookback: str = "30d") -> set[str]:
    """Query Prometheus for emails of users with Claude Code telemetry.

    Uses a range query (max_over_time) to include users whose series have
    gone stale — not just those actively sending telemetry right now.
    """
    try:
        query = f"group by (user_email) (max_over_time(claude_code_cost_usage_USD_total[{lookback}]))"
        url = f"{prometheus_url}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        resp = _json.loads(urllib.request.urlopen(url, timeout=10).read())
        if resp.get("status") == "success":
            emails = {
                item["metric"]["user_email"].lower()
                for item in resp["data"]["result"]
                if item["metric"].get("user_email")
            }
            log.info(f"Discovered {len(emails)} Claude Code users from Prometheus (lookback={lookback})")
            return emails
    except Exception as e:
        log.warning(f"Could not query Prometheus for Claude Code users: {e}")
    return set()


def fetch_user_by_email(token: str, email: str) -> dict | None:
    """Fetch a single user's org data + manager from Graph API."""
    headers = {"Authorization": f"Bearer {token}"}
    select = "displayName,mail,userPrincipalName,department,jobTitle,officeLocation,city,country,companyName,employeeId"
    expand = "manager($select=displayName,mail)"
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(email)}?$select={select}&$expand={expand}"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            log.warning(f"  User not found in Graph API: {email}")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"  Failed to fetch user {email}: {e}")
        return None


def fetch_org_headcount(token: str) -> int:
    """Get total enabled users in the tenant — single lightweight API call."""
    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    url = "https://graph.microsoft.com/v1.0/users/$count?$filter=accountEnabled eq true"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return int(resp.text)
    except Exception as e:
        log.warning(f"Could not fetch org headcount: {e}")
        return 0


def refresh_metrics(config: dict):
    """Refresh user_info metrics — only for Claude Code users found in Prometheus."""
    prometheus_url = config.get("exporter", {}).get("prometheus_url", "http://localhost:9090")
    target_emails = get_claude_code_users(prometheus_url)

    if not target_emails:
        log.info("No Claude Code users found in Prometheus — nothing to enrich")
        USERS_TOTAL.set(0)
        LAST_REFRESH.set(time.time())
        return

    log.info(f"Enriching {len(target_emails)} Claude Code users from Microsoft Graph API...")

    try:
        token = get_access_token(config)

        # Fetch org headcount (1 lightweight API call)
        headcount = fetch_org_headcount(token)
        if headcount:
            ORG_HEADCOUNT.set(headcount)
            log.info(f"  Org headcount: {headcount} enabled users")

        # Clear existing metrics
        USER_INFO._metrics.clear()
        ORG_TREE_NODE._metrics.clear()

        # Mutable counter so resolve_vp can increment it
        api_calls = [1]  # headcount call
        exported = 0

        # Collect hierarchy for tree visualization
        rollup_managers = {}  # rollup_email → rollup_name
        managers = {}         # manager_email → (manager_name, rollup_email)
        users = {}            # user_email → (display_name, manager_email)
        cxo_emails = set()    # emails of CxOs (detected from self-rollup users)

        # Coerce None → "" for all label values
        def s(val):
            return str(val) if val else ""

        for email in sorted(target_emails):
            user = fetch_user_by_email(token, email)
            api_calls[0] += 1
            if not user:
                continue

            # Manager is inlined via $expand — no extra API calls
            manager = user.get("manager") or {}
            manager_name = manager.get("displayName") or ""
            manager_email = (manager.get("mail") or "").lower()

            # Resolve rollup manager (walks up chain with caching)
            rollup_name, rollup_email = resolve_rollup(token, email, api_calls)

            USER_INFO.labels(
                user_email=email,
                display_name=s(user.get("displayName")),
                department=s(user.get("department")),
                job_title=s(user.get("jobTitle")),
                manager_name=manager_name,
                manager_email=manager_email,
                rollup_name=rollup_name,
                rollup_email=rollup_email,
                company=s(user.get("companyName")),
                office=s(user.get("officeLocation")),
                city=s(user.get("city")),
                country=s(user.get("country")),
                employee_id=s(user.get("employeeId")),
            ).set(1)
            exported += 1
            log.info(f"  {email} → {s(user.get('displayName'))} (rollup: {rollup_name})")

            # Collect hierarchy data for tree visualization
            if rollup_email:
                rollup_managers[rollup_email] = rollup_name
            if manager_email:
                managers[manager_email] = (manager_name, rollup_email)
            users[email] = (s(user.get("displayName")), manager_email)
            # If user is their own rollup, their manager is a CxO
            if rollup_email == email and manager_email:
                cxo_emails.add(manager_email)

        # Emit org tree nodes (each person appears once at their highest role)
        # Build tree nodes in memory first, then detect orphans
        tree_nodes = []  # (node_id, parent_id, node_label, node_type)

        # Rollup managers → root nodes
        for r_email, r_name in rollup_managers.items():
            tree_nodes.append((r_email, "", r_name, "rollup"))
        # Managers → parent = rollup (skip CxOs — they're above rollups)
        for m_email, (m_name, r_email) in managers.items():
            if m_email not in rollup_managers and m_email not in cxo_emails:
                tree_nodes.append((m_email, r_email, m_name, "manager"))
        # Users → parent = manager (if manager is CxO, parent = rollup instead)
        for u_email, (u_name, m_email) in users.items():
            if u_email in rollup_managers or u_email in managers:
                continue
            parent = m_email if m_email not in cxo_emails else ""
            tree_nodes.append((u_email, parent, u_name, "user"))

        # Detect orphans: nodes whose parent_id is non-empty but not in the tree
        all_node_ids = {n[0] for n in tree_nodes}
        has_orphans = any(
            n[1] and n[1] not in all_node_ids for n in tree_nodes
        )
        if has_orphans:
            tree_nodes.append(("unknown", "", "Unknown", "rollup"))
            log.info("  Added 'Unknown' root node for orphaned tree entries")

        # Emit all tree nodes, re-parenting orphans under "unknown"
        for node_id, parent_id, node_label, node_type in tree_nodes:
            if parent_id and parent_id not in all_node_ids and node_id != "unknown":
                parent_id = "unknown"
            ORG_TREE_NODE.labels(node_id=node_id, parent_id=parent_id,
                                 node_label=node_label, node_type=node_type).set(1)

        USERS_TOTAL.set(exported)
        GRAPH_API_CALLS.set(api_calls[0])
        LAST_REFRESH.set(time.time())
        log.info(f"Exported metrics for {exported} users ({api_calls[0]} API calls, {len(_rollup_cache)} cached rollup paths)")

    except Exception as e:
        log.error(f"Refresh failed: {e}")
        REFRESH_ERRORS.inc()


def refresh_loop(config: dict):
    """Background thread that periodically refreshes metrics."""
    interval = config.get("exporter", {}).get("refresh_interval", 3600)
    while True:
        refresh_metrics(config)
        log.info(f"Next refresh in {interval}s")
        time.sleep(interval)


def main():
    config_path = os.environ.get("CONFIG_PATH", "/etc/graph-enrichment/config.yaml")

    # Also support config via individual env vars (for Docker)
    if not os.path.exists(config_path):
        config = {
            "azure": {
                "tenant_id": os.environ["AZURE_TENANT_ID"],
                "client_id": os.environ["AZURE_CLIENT_ID"],
                "client_secret": os.environ["AZURE_CLIENT_SECRET"],
            },
            "exporter": {
                "port": int(os.environ.get("EXPORTER_PORT", "9101")),
                "refresh_interval": int(os.environ.get("REFRESH_INTERVAL", "3600")),
                "prometheus_url": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
            },
        }
    else:
        config = load_config(config_path)

    port = config.get("exporter", {}).get("port", 9101)

    # Start Prometheus HTTP server
    start_http_server(port)
    log.info(f"Graph Enrichment Exporter listening on :{port}/metrics")

    # Initial refresh + background loop
    refresh_loop(config)


if __name__ == "__main__":
    main()
