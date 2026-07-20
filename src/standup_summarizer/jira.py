"""Check Jira issue status via the Atlassian Cloud REST API.

Given issue keys parsed from stand-up notes, report which ones are "done"
(status category 'done' — covers Done, Closed, Resolved and custom done-mapped
statuses). Auth is HTTP Basic (email + API token). Uses only urllib so it adds
no dependency. Any per-issue lookup error is swallowed (that issue is treated as
not-done) so a Jira hiccup never fails the daily run.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .config import JiraConfig


def _auth_header(cfg: JiraConfig) -> str:
    raw = f"{cfg.email}:{cfg.api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def issue_status(cfg: JiraConfig, key: str) -> tuple[str, str] | None:
    """Return (status_name, status_category_key) for the issue, or None on error.

    e.g. ("In Review", "indeterminate") or ("Done", "done"). The category key is
    one of 'new' | 'indeterminate' | 'done'.
    """
    url = f"{cfg.base_url}/rest/api/3/issue/{key}?fields=status"
    req = urllib.request.Request(
        url, headers={"Authorization": _auth_header(cfg), "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return None
    status = data.get("fields", {}).get("status", {}) or {}
    name = status.get("name", "") or ""
    category = status.get("statusCategory", {}).get("key", "") or ""
    return name, category


def issue_done(cfg: JiraConfig, key: str) -> bool | None:
    """Return True/False if the issue's status category is 'done'; None on error."""
    st = issue_status(cfg, key)
    return None if st is None else st[1] == "done"
