"""Read Jira issue state via the Atlassian Cloud REST API.

Given issue keys parsed from stand-up notes, report:
  * which ones are "done" (status category 'done' — covers Done, Closed,
    Resolved and custom done-mapped statuses),
  * the issue detail used by the daily summary (summary line, description,
    status),
  * its comments,
  * and its development info (branch / commit / pull request) via the
    dev-status API that backs the "Development" panel in the Jira UI.

Auth is HTTP Basic (email + API token). Uses only urllib so it adds no
dependency. Any per-issue lookup error is swallowed (that issue is treated as
not-done / unknown) so a Jira hiccup never fails the daily run.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from .config import JiraConfig


def _auth_header(cfg: JiraConfig) -> str:
    raw = f"{cfg.email}:{cfg.api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _get(cfg: JiraConfig, url: str) -> dict | None:
    """GET `url` and return the decoded JSON body, or None on any failure."""
    req = urllib.request.Request(
        url, headers={"Authorization": _auth_header(cfg), "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return None


def adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format value (description/comment) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (adf_to_text(n) for n in node) if t)
    if isinstance(node, dict):
        parts = [node.get("text") or "", adf_to_text(node.get("content"))]
        return " ".join(p for p in parts if p)
    return ""


def adf_media_names(node) -> list[str]:
    """Filenames of images/files embedded in an ADF value, in document order.

    `adf_to_text` flattens a screenshot-only comment to "" — correctly, since it
    holds no text — which makes an attached screenshot invisible to anything
    counting characters. Scoring treats a screenshot as a real ticket update, so
    it needs to see that the media node is there.
    """
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += adf_media_names(item)
    elif isinstance(node, dict):
        if node.get("type") == "media":
            attrs = node.get("attrs") or {}
            found.append(str(attrs.get("alt") or attrs.get("id") or "attachment"))
        found += adf_media_names(node.get("content"))
    return found


def issue_status(cfg: JiraConfig, key: str) -> tuple[str, str] | None:
    """Return (status_name, status_category_key) for the issue, or None on error.

    e.g. ("In Review", "indeterminate") or ("Done", "done"). The category key is
    one of 'new' | 'indeterminate' | 'done'.
    """
    data = _get(cfg, f"{cfg.base_url}/rest/api/3/issue/{key}?fields=status")
    if data is None:
        return None
    status = data.get("fields", {}).get("status", {}) or {}
    name = status.get("name", "") or ""
    category = status.get("statusCategory", {}).get("key", "") or ""
    return name, category


def issue_detail(cfg: JiraConfig, key: str) -> dict | None:
    """Return {id, key, summary, description, status_name, status_category}.

    `description` is the flattened description text ("" when the field is
    empty). Returns None if the issue can't be read (not found / no access).
    """
    fields_param = "summary,description,status,assignee,priority,attachment,issuetype,parent"
    data = _get(cfg, f"{cfg.base_url}/rest/api/3/issue/{key}?fields={fields_param}")
    if data is None:
        return None
    fields = data.get("fields", {}) or {}
    status = fields.get("status", {}) or {}
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    attachments = [a.get("filename", "") for a in (fields.get("attachment") or []) if a.get("filename")]
    return {
        "id": str(data.get("id", "") or ""),
        "key": data.get("key", key) or key,
        "summary": (fields.get("summary") or "").strip(),
        "description": adf_to_text(fields.get("description")).strip(),
        "status_name": status.get("name", "") or "",
        "status_category": (status.get("statusCategory", {}) or {}).get("key", "") or "",
        "issue_type": ((fields.get("issuetype") or {}).get("name") or "").strip(),
        # Sub-tasks routinely carry no description of their own — the context
        # lives on the parent. Callers that judge "is this documented" need to
        # be able to look there.
        "parent_key": ((fields.get("parent") or {}).get("key") or "").strip(),
        "assignee": assignee.get("displayName", "") or "",
        "assignee_id": assignee.get("accountId", "") or "",
        "priority": priority.get("name", "") or "",
        "attachments": attachments,
    }


def issue_done(cfg: JiraConfig, key: str) -> bool | None:
    """Return True/False if the issue's status category is 'done'; None on error."""
    st = issue_status(cfg, key)
    return None if st is None else st[1] == "done"


def issue_comments(cfg: JiraConfig, key: str) -> list[dict] | None:
    """Return [{author, created, body}] newest-first, or None on lookup error."""
    url = f"{cfg.base_url}/rest/api/3/issue/{key}/comment?orderBy=-created&maxResults=100"
    data = _get(cfg, url)
    if data is None:
        return None
    return [
        {
            "author": ((c.get("author") or {}).get("displayName") or "").strip(),
            # Jira 'created' looks like "2026-07-21T10:15:30.123+0530" — the
            # first 10 chars are the calendar date.
            "created": str(c.get("created", "") or ""),
            "body": adf_to_text(c.get("body")).strip(),
            # A screenshot-only comment flattens to an empty body; this is how a
            # caller can tell "said nothing" from "posted evidence".
            "media": adf_media_names(c.get("body")),
        }
        for c in data.get("comments", []) or []
    ]


def issue_commented_on(cfg: JiraConfig, key: str, date_iso: str) -> bool | None:
    """True if the issue has any comment created on `date_iso` (YYYY-MM-DD).

    Returns None on lookup error.
    """
    comments = issue_comments(cfg, key)
    if comments is None:
        return None
    return any(c["created"][:10] == date_iso for c in comments)


# --------------------------------------------------------------------------
# Development info (branch / commit / pull request)
# --------------------------------------------------------------------------
# The dev-status API is what powers the "Development" panel in the Jira issue
# view. It is keyed by the *numeric* issue id (not the KEY) and needs the
# application type of the linked SCM. We discover the live types from the
# summary endpoint first so a single detail call per data type is enough.
_DEV_BASE = "/rest/dev-status/1.0/issue"
_DEV_FALLBACK_TYPES = ("GitHub", "bitbucket", "stash", "gitlab")


def _dev_instance_types(cfg: JiraConfig, issue_id: str) -> dict[str, list[str]]:
    """{data_type: [application types present]} from the dev-status summary."""
    data = _get(cfg, f"{cfg.base_url}{_DEV_BASE}/summary?issueId={issue_id}")
    summary = (data or {}).get("summary") or {}
    found: dict[str, list[str]] = {}
    for data_type in ("repository", "branch", "pullrequest"):
        section = summary.get(data_type) or {}
        if not (section.get("overall") or {}).get("count"):
            continue
        types = list((section.get("byInstanceType") or {}).keys())
        found[data_type] = types or list(_DEV_FALLBACK_TYPES)
    return found


def _dev_detail(cfg: JiraConfig, issue_id: str, app_type: str, data_type: str) -> list[dict]:
    """One dev-status detail call; returns its `detail` list (empty on error)."""
    url = (f"{cfg.base_url}{_DEV_BASE}/detail"
           f"?issueId={issue_id}&applicationType={app_type}&dataType={data_type}")
    data = _get(cfg, url)
    return (data or {}).get("detail") or []


def issue_dev_info(cfg: JiraConfig, issue_id: str) -> dict:
    """Return {branches: [name], commits: [id], pull_requests: [{...}]}.

    Every field degrades to an empty list when the issue has no linked SCM data
    (or the dev-status API is unavailable), so callers can render "not linked".
    Pull request entries are {id, name, url, status} with status upper-cased
    (OPEN / MERGED / DECLINED).
    """
    info: dict[str, list] = {"branches": [], "commits": [], "pull_requests": []}
    if not issue_id:
        return info

    wanted = _dev_instance_types(cfg, issue_id)
    if not wanted:  # summary unavailable — probe the common providers directly
        wanted = {"repository": list(_DEV_FALLBACK_TYPES),
                  "branch": list(_DEV_FALLBACK_TYPES),
                  "pullrequest": list(_DEV_FALLBACK_TYPES)}

    branches: dict[str, None] = {}
    commits: dict[str, None] = {}
    prs: dict[str, dict] = {}

    def absorb(node: dict) -> None:
        for b in node.get("branches") or []:
            if name := (b.get("name") or "").strip():
                branches.setdefault(name, None)
        for c in node.get("commits") or []:
            # displayId is the short hash Jira shows; fall back to the full id.
            if cid := (c.get("displayId") or c.get("id") or "").strip():
                commits.setdefault(cid, None)
        for p in node.get("pullRequests") or []:
            url = (p.get("url") or "").strip()
            pid = (p.get("id") or "").strip()
            prs.setdefault(url or pid, {
                "id": pid,
                "name": (p.get("name") or "").strip(),
                "url": url,
                "status": (p.get("status") or "").strip().upper(),
            })

    for data_type, app_types in wanted.items():
        for app_type in app_types:
            details = _dev_detail(cfg, issue_id, app_type, data_type)
            for det in details:
                absorb(det)
                for repo in det.get("repositories") or []:
                    absorb(repo)
            if details:
                break  # this provider answered — no need to probe the others

    info["branches"] = list(branches)
    info["commits"] = list(commits)
    info["pull_requests"] = list(prs.values())
    return info


# --------------------------------------------------------------------------
# JQL search (issues assigned to a developer, etc.)
# --------------------------------------------------------------------------
def search_issues(cfg: JiraConfig, jql: str, fields: str = "summary,status,assignee,priority",
                  max_results: int = 100) -> list[dict]:
    """Return matching issues [{id, key, fields}] via /rest/api/3/search/jql.

    The classic /search endpoint was removed by Atlassian; this uses the
    token-paginated /search/jql replacement. Returns [] on any error so a Jira
    hiccup never fails the run.
    """
    base = f"{cfg.base_url}/rest/api/3/search/jql"
    out: list[dict] = []
    token: str | None = None
    while len(out) < max_results:
        params = {"jql": jql, "maxResults": min(100, max_results - len(out)), "fields": fields}
        if token:
            params["nextPageToken"] = token
        data = _get(cfg, base + "?" + urllib.parse.urlencode(params))
        if not data:
            break
        out.extend(data.get("issues", []) or [])
        token = data.get("nextPageToken")
        if not token:
            break
    return out[:max_results]
