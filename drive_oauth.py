"""OAuth (user-credential) Google Drive uploader.

Lets the project upload files directly to *your* Drive (unlike the service
account, which has no Drive storage). One-time setup:

  1. Google Cloud Console -> APIs & Services -> Credentials ->
       Create Credentials -> OAuth client ID -> Application type: "Desktop app".
  2. Download the JSON and save it as: credentials/oauth_client.json
  3. OAuth consent screen -> User type "External" -> add your Google account
     under "Test users".
  4. Authorize once (opens a browser):
       .venv/Scripts/python.exe drive_oauth.py auth

The refresh token is cached at credentials/oauth_token.json (gitignored), so
later runs upload without prompting.

Programmatic use:
    from drive_oauth import get_service, find_or_create_folder, upsert_file
"""

from __future__ import annotations

import sys
from pathlib import Path

# Full Drive scope so the app can reuse an existing folder, upsert by name,
# and delete its own leftovers. (Owner-as-test-user avoids app verification.)
SCOPES = ["https://www.googleapis.com/auth/drive"]

ROOT = Path(__file__).parent
CLIENT_FILE = ROOT / "credentials" / "oauth_client.json"
TOKEN_FILE = ROOT / "credentials" / "oauth_token.json"


def get_service():
    """Return an authenticated Drive v3 service, running the consent flow if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_FILE.exists():
                sys.exit(
                    f"Missing OAuth client at {CLIENT_FILE}.\n"
                    "Create a 'Desktop app' OAuth client in Google Cloud Console, "
                    "download the JSON, and save it there (see this file's docstring)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def find_or_create_folder(svc, name: str, parent: str | None = None) -> str:
    """Return the id of a folder named `name`, creating it if absent."""
    q = (
        f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    if parent:
        q += f" and '{parent}' in parents"
    found = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def upsert_file(svc, path, folder_id: str, mime: str = "application/pdf") -> tuple[str, str]:
    """Upload `path` into `folder_id`, replacing any same-named file (no duplicates)."""
    from googleapiclient.http import MediaFileUpload

    name = Path(path).name
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    existing = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    if existing:
        fid = existing[0]["id"]
        svc.files().update(fileId=fid, media_body=media).execute()
        return fid, "updated"
    created = svc.files().create(
        body={"name": name, "parents": [folder_id]}, media_body=media, fields="id"
    ).execute()
    return created["id"], "created"


def delete_by_name(svc, name: str, folder_id: str) -> int:
    """Delete every file named `name` in `folder_id`. Returns count deleted."""
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    files = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    for f in files:
        svc.files().delete(fileId=f["id"]).execute()
    return len(files)


if __name__ == "__main__":
    # `python drive_oauth.py auth` -> run/verify the consent flow.
    get_service()
    print(f"Authorized. Token cached at {TOKEN_FILE}")
