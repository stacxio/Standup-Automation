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


def issue_done(cfg: JiraConfig, key: str) -> bool | None:
    """Return True/False if the issue's status category is 'done'; None on error."""
    url = f"{cfg.base_url}/rest/api/3/issue/{key}?fields=status"
    req = urllib.request.Request(
        url, headers={"Authorization": _auth_header(cfg), "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return None
    cat = (
        data.get("fields", {})
        .get("status", {})
        .get("statusCategory", {})
        .get("key", "")
    )
    return cat == "done"
