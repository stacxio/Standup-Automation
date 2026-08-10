"""Pull meetings out of Otter, normalised onto one shape for the archive agent.

There are three ways into an Otter account, and they suit different plans, so
this module hides all three behind one `source.list_meetings()` /
`source.fetch()` / `source.download_audio()` interface:

  * **official** — the Otter Public API (`https://api.otter.ai/v1`, Bearer key).
    Documented and stable, but Enterprise-only: ask your Otter account manager
    to enable it, then Integrations -> Developer -> Create key.
  * **web** — the internal API the otter.ai web app itself calls, driven with
    your account email/password (the route the community `otterai` clients
    take). Works on any plan, but it is unofficial: Otter can change or block
    it without notice, so treat a sudden failure here as expected, not a bug.
  * **inbox** — no API at all. You export from Otter by hand into a folder and
    the agent files what it finds. Needs no credentials and cannot break, which
    is why it stays available as the fallback for the other two.

Payload shapes differ between the two APIs (and Otter has changed field names
before), so every field is read through `_pick()` over a list of plausible
names rather than one hardcoded key. Transcripts are rendered back into Otter's
plain-text export format so `transcript.parse_transcript()` handles them
identically to a file you exported yourself.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import OtterConfig

AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".mp4", ".aac", ".ogg", ".webm")
TEXT_EXTS = (".txt", ".vtt", ".srt", ".md")
# Otter's own exports are usually named with the meeting date somewhere.
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")


# --------------------------------------------------------------------------
# Normalised meeting
# --------------------------------------------------------------------------
@dataclass
class Meeting:
    """One meeting, however it was obtained.

    `transcript` is Otter-export text (speaker header + spoken lines), so it can
    be handed straight to `transcript.parse_transcript()`. `audio_url` /
    `audio_path` are resolution hints for `download_audio()`, not the media.
    """

    id: str
    title: str
    started: dt.datetime
    duration_sec: int = 0
    attendees: list[str] = field(default_factory=list)
    transcript: str = ""
    summary: str = ""
    action_items: list[str] = field(default_factory=list)
    audio_ext: str = "mp3"
    audio_url: str | None = None
    audio_path: Path | None = None
    source: str = "otter"

    @property
    def date(self) -> dt.date:
        return self.started.date()

    @property
    def duration_hms(self) -> str:
        minutes, seconds = divmod(max(int(self.duration_sec or 0), 0), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


# --------------------------------------------------------------------------
# Tolerant payload readers (field names differ between the two APIs)
# --------------------------------------------------------------------------
def _pick(data, *names, default=None):
    """First present, non-empty value among `names`; `default` if none match."""
    if not isinstance(data, dict):
        return default
    for name in names:
        value = data.get(name)
        if value not in (None, "", [], {}):
            return value
    return default


def as_datetime(value) -> dt.datetime:
    """Epoch seconds/milliseconds or an ISO-8601 string -> aware datetime.

    Falls back to 'now' rather than raising: a meeting with an unreadable
    timestamp should still be archived (under today), not abort the run.
    """
    if value in (None, "", []):
        return dt.datetime.now().astimezone()
    if isinstance(value, bool):
        return dt.datetime.now().astimezone()
    if isinstance(value, (int, float)) or str(value).strip().lstrip("-").isdigit():
        epoch = float(value)
        if epoch > 1e11:  # milliseconds
            epoch /= 1000.0
        try:
            return dt.datetime.fromtimestamp(epoch).astimezone()
        except (OverflowError, OSError, ValueError):
            return dt.datetime.now().astimezone()
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now().astimezone()
    return parsed.astimezone()


def _people(value) -> list[str]:
    """Attendee names from a list of strings or of {name|display_name|email} dicts."""
    names: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(_pick(item, "name", "display_name", "full_name", "email", default="")).strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def _stamp(seconds) -> str:
    """Seconds -> Otter's 'm:ss' / 'h:mm:ss' header timestamp."""
    total = int(float(seconds or 0))
    minutes, secs = divmod(max(total, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render_transcript(segments: list[dict]) -> str:
    """Render [{speaker, text, start}] as an Otter plain-text export.

    Matching Otter's own export format means the archived transcript.txt is
    byte-comparable with a hand export, and every downstream agent
    (gap_report, verify_standup) can consume it unchanged.
    """
    lines: list[str] = []
    for seg in segments:
        speaker = str(seg.get("speaker") or "Unknown").strip()
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines += [f"{speaker}  {_stamp(seg.get('start'))}", text, ""]
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _segments(payload: dict) -> list[dict]:
    """Pull transcript segments out of whichever container this payload uses."""
    raw = _pick(payload, "transcript", "transcripts", "segments", "utterances", default=[])
    if isinstance(raw, dict):
        raw = _pick(raw, "segments", "utterances", "data", "items", default=[])
    if isinstance(raw, str):  # already-rendered text
        return [{"speaker": "", "text": raw, "start": 0}]
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        speaker = _pick(item, "speaker", "speaker_name", "speaker_id", default="")
        if isinstance(speaker, dict):
            speaker = _pick(speaker, "name", "display_name", default="")
        out.append({
            "speaker": str(speaker or "").strip(),
            "text": str(_pick(item, "text", "transcript", "content", default="")).strip(),
            "start": _pick(item, "start", "start_time", "start_offset", "offset", default=0),
        })
    return [s for s in out if s["text"]]


def _bullets(value) -> list[str]:
    """Normalise an action-item / outline block to a flat list of strings."""
    if isinstance(value, dict):
        value = _pick(value, "items", "data", "action_items", default=[])
    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(_pick(item, "text", "title", "content", "summary", default="")).strip()
        else:
            text = ""
        if text:
            out.append(text)
    return out


def _summary_text(payload: dict) -> str:
    """Otter's AI summary, plus its outline if one is present, as markdown."""
    summary = _pick(payload, "summary", "abstract_summary", "takeaways", "overview", default="")
    if isinstance(summary, dict):
        summary = _pick(summary, "text", "content", "summary", default="")
    parts = [str(summary).strip()] if summary else []
    outline = _bullets(_pick(payload, "outline", "outlines", "chapters", default=[]))
    if outline:
        parts.append("## Outline\n\n" + "\n".join(f"- {o}" for o in outline))
    insights = _bullets(_pick(payload, "insights", default=[]))
    if insights:
        parts.append("## Insights\n\n" + "\n".join(f"- {i}" for i in insights))
    return "\n\n".join(parts).strip()


def to_meeting(payload: dict, *, source: str) -> Meeting:
    """Normalise one conversation/speech payload into a Meeting."""
    started = as_datetime(_pick(
        payload, "started_at", "start_time", "created_at", "start", "date", "modified_time"
    ))
    duration = _pick(payload, "duration", "duration_sec", "duration_seconds", "length", default=0)
    try:
        duration = int(float(duration))
    except (TypeError, ValueError):
        duration = 0
    audio = _pick(payload, "audio_url", "download_url", "media_url", "audio", default=None)
    return Meeting(
        id=str(_pick(payload, "id", "otid", "speech_id", "conversation_id", default="")).strip(),
        title=str(_pick(payload, "title", "name", "subject", default="")).strip() or "Untitled meeting",
        started=started,
        duration_sec=duration,
        attendees=_people(_pick(
            payload, "attendees", "participants", "speakers", "shared_with", default=[]
        )),
        transcript=render_transcript(_segments(payload)),
        summary=_summary_text(payload),
        action_items=_bullets(_pick(payload, "action_items", "actionItems", "todos", default=[])),
        audio_url=audio if isinstance(audio, str) else None,
        source=source,
    )


# --------------------------------------------------------------------------
# Official Public API (Bearer key, Enterprise workspaces)
# --------------------------------------------------------------------------
class OfficialOtterSource:
    """Client for `https://api.otter.ai/v1` — the documented Public API.

    Endpoints used: `GET /workspace` (to discover the workspace id when
    OTTER_WORKSPACE_ID is unset), `GET /workspace/{id}/conversations`
    (reverse-chronological, cursor-paginated) and
    `GET /conversations/{id}?include=all`.
    """

    name = "official"

    def __init__(self, cfg: OtterConfig) -> None:
        import requests

        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {cfg.api_key}",
            "Accept": "application/json",
        })
        self._workspace = cfg.workspace_id

    def _get(self, path: str, **params) -> dict:
        resp = self.http.get(f"{self.cfg.api_base}{path}", params=params or None, timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def workspace_id(self) -> str:
        if not self._workspace:
            body = self._get("/workspace")
            data = _pick(body, "data", "workspace", default=body)
            if isinstance(data, list):
                data = data[0] if data else {}
            self._workspace = str(_pick(data, "id", "workspace_id", default="")).strip()
            if not self._workspace:
                raise RuntimeError(
                    "Could not resolve the Otter workspace id from GET /workspace — "
                    "set OTTER_WORKSPACE_ID in .env."
                )
        return self._workspace

    def list_meetings(self, since: dt.date, until: dt.date, limit: int = 50) -> list[dict]:
        """Conversation stubs started within [since, until], newest first.

        The listing is reverse-chronological, so paging stops as soon as a page
        predates `since` — no need to walk the whole workspace history.
        """
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(20):  # page guard
            params = {"limit": min(limit, 50)}
            if cursor:
                params["cursor"] = cursor
            body = self._get(f"/workspace/{self.workspace_id()}/conversations", **params)
            items = _pick(body, "data", "conversations", "items", "results", default=[])
            if isinstance(items, dict):
                items = _pick(items, "conversations", "items", default=[])
            if not items:
                break
            exhausted = False
            for item in items:
                started = as_datetime(_pick(
                    item, "started_at", "start_time", "created_at", "date", default=None
                )).date()
                if started > until:
                    continue
                if started < since:
                    exhausted = True
                    break
                out.append(item)
                if len(out) >= limit:
                    return out
            cursor = _pick(body, "next_cursor", "cursor", default=None) or _pick(
                _pick(body, "meta", "pagination", default={}), "next_cursor", "cursor"
            )
            if exhausted or not cursor:
                break
        return out

    def fetch(self, stub: dict) -> Meeting:
        conv_id = str(_pick(stub, "id", "conversation_id", "otid", default="")).strip()
        body = self._get(f"/conversations/{conv_id}", include="all")
        payload = _pick(body, "data", "conversation", default=body)
        if isinstance(payload, dict):
            payload = {**stub, **payload}  # stub carries listing-only fields
        return to_meeting(payload, source="otter:official")

    def download_audio(self, meeting: Meeting) -> tuple[bytes, str] | None:
        """(bytes, extension) for the recording, or None if unavailable."""
        url = meeting.audio_url or f"{self.cfg.api_base}/conversations/{meeting.id}/audio"
        try:
            resp = self.http.get(url, timeout=600)
            resp.raise_for_status()
            kind = (resp.headers.get("content-type") or "").lower()
            if "json" in kind:  # a pointer to signed storage, not the media
                signed = _pick(resp.json(), "url", "download_url", "audio_url", default=None)
                if not signed:
                    return None
                resp = self.http.get(signed, timeout=600)
                resp.raise_for_status()
                kind = (resp.headers.get("content-type") or "").lower()
            return resp.content, _ext_for(kind, meeting.audio_ext)
        except Exception as exc:  # noqa: BLE001 — a missing recording must not fail the archive
            print(f"  (audio unavailable for {meeting.id}: {type(exc).__name__}: {exc})")
            return None


def _ext_for(content_type: str, fallback: str = "mp3") -> str:
    for kind, ext in (("mpeg", "mp3"), ("mp4", "m4a"), ("m4a", "m4a"), ("wav", "wav"),
                      ("aac", "aac"), ("ogg", "ogg"), ("webm", "webm")):
        if kind in content_type:
            return ext
    return fallback


# --------------------------------------------------------------------------
# Internal web API (email + password, any plan, unofficial)
# --------------------------------------------------------------------------
class WebOtterSource:
    """Client for the API the otter.ai web app calls — unofficial.

    Flow: `GET /login` with HTTP Basic auth sets the session cookies (including
    `csrftoken`) and returns the user id; `GET /speeches` lists, `GET /speech`
    details, and `POST /bulk_export` returns the media. Every authenticated
    call carries the `x-csrftoken` header the web app sends.
    """

    name = "web"

    def __init__(self, cfg: OtterConfig) -> None:
        import requests

        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers.update({"referer": "https://otter.ai/", "Accept": "application/json"})
        self._userid: str | None = None

    def login(self) -> str:
        if self._userid:
            return self._userid
        self.http.auth = (self.cfg.email, self.cfg.password)
        resp = self.http.get(
            f"{self.cfg.web_base}/login", params={"username": self.cfg.email}, timeout=60
        )
        resp.raise_for_status()
        self._userid = str(_pick(resp.json(), "userid", "user_id", default="")).strip()
        if not self._userid:
            raise RuntimeError("Otter web login succeeded but returned no userid.")
        return self._userid

    def _get(self, path: str, **params) -> dict:
        params["userid"] = self.login()
        resp = self.http.get(
            f"{self.cfg.web_base}{path}",
            params=params,
            headers={"x-csrftoken": self.http.cookies.get("csrftoken", "")},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def list_meetings(self, since: dt.date, until: dt.date, limit: int = 50) -> list[dict]:
        body = self._get("/speeches", folder=0, page_size=max(limit, 45), source="owned")
        items = _pick(body, "speeches", "data", default=[])
        out = []
        for item in items:
            started = as_datetime(_pick(
                item, "start_time", "created_at", "modified_time", default=None
            )).date()
            if since <= started <= until:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def fetch(self, stub: dict) -> Meeting:
        otid = str(_pick(stub, "otid", "speech_id", "id", default="")).strip()
        body = self._get("/speech", otid=otid)
        payload = _pick(body, "speech", "data", default=body)
        if isinstance(payload, dict):
            payload = {**stub, **payload}
        meeting = to_meeting(payload, source="otter:web")
        if not meeting.id:
            meeting.id = otid
        return meeting

    def download_audio(self, meeting: Meeting) -> tuple[bytes, str] | None:
        try:
            resp = self.http.post(
                f"{self.cfg.web_base}/bulk_export",
                json={"formats": "mp3", "speech_otid_list": [meeting.id]},
                headers={"x-csrftoken": self.http.cookies.get("csrftoken", ""),
                         "referer": "https://otter.ai/"},
                timeout=600,
            )
            resp.raise_for_status()
            data = resp.content
            if data[:2] == b"PK":  # bulk_export returns a zip when it feels like it
                with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                    audio = [n for n in bundle.namelist() if n.lower().endswith(AUDIO_EXTS)]
                    if not audio:
                        return None
                    return bundle.read(audio[0]), Path(audio[0]).suffix.lstrip(".")
            return data, "mp3"
        except Exception as exc:  # noqa: BLE001
            print(f"  (audio unavailable for {meeting.id}: {type(exc).__name__}: {exc})")
            return None


# --------------------------------------------------------------------------
# Inbox (hand-exported files — no credentials, cannot break)
# --------------------------------------------------------------------------
class InboxSource:
    """Read meetings from a folder of Otter exports.

    Files belonging to one meeting are grouped by filename stem, so
    `2026-08-06 Daily Standup.txt` + `.mp3` (+ an optional `*.summary.md`)
    become a single meeting. The date is taken from the filename when it
    contains one, else from the file's modification time.
    """

    name = "inbox"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def list_meetings(self, since: dt.date, until: dt.date, limit: int = 50) -> list[dict]:
        if not self.folder.is_dir():
            return []
        groups: dict[str, dict] = {}
        for path in sorted(self.folder.iterdir()):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in AUDIO_EXTS + TEXT_EXTS:
                continue
            stem = path.stem
            if stem.lower().endswith(".summary"):
                stem = stem[: -len(".summary")]
            group = groups.setdefault(stem, {"stem": stem, "files": []})
            group["files"].append(path)

        out = []
        for group in groups.values():
            day = _date_from(group["stem"], group["files"])
            if since <= day <= until:
                group["date"] = day.isoformat()
                out.append(group)
        out.sort(key=lambda g: g["date"], reverse=True)
        return out[:limit]

    def fetch(self, stub: dict) -> Meeting:
        files = list(stub["files"])
        started = as_datetime(f"{stub['date']}T09:00:00")
        transcript_text, summary = "", ""
        audio: Path | None = None
        for path in files:
            suffix = path.suffix.lower()
            if suffix in AUDIO_EXTS:
                audio = path
            elif path.stem.lower().endswith(".summary") or suffix == ".md":
                summary = path.read_text(encoding="utf-8", errors="replace")
            elif suffix in TEXT_EXTS:
                transcript_text = path.read_text(encoding="utf-8", errors="replace")
        return Meeting(
            id=_slug_id(stub["stem"]),
            title=_title_from(stub["stem"]),
            started=started,
            transcript=transcript_text,
            summary=summary.strip(),
            audio_path=audio,
            audio_ext=(audio.suffix.lstrip(".") if audio else "mp3"),
            source="otter:inbox",
        )

    def download_audio(self, meeting: Meeting) -> tuple[bytes, str] | None:
        if not meeting.audio_path or not meeting.audio_path.exists():
            return None
        return meeting.audio_path.read_bytes(), meeting.audio_path.suffix.lstrip(".")


def _date_from(stem: str, files: list[Path]) -> dt.date:
    match = _DATE_IN_NAME.search(stem)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    newest = max((f.stat().st_mtime for f in files), default=0)
    return dt.datetime.fromtimestamp(newest).date() if newest else dt.date.today()


def _title_from(stem: str) -> str:
    """Filename stem minus its date prefix, e.g. '2026-08-06 Daily Standup'."""
    title = _DATE_IN_NAME.sub("", stem).strip(" -_.")
    return title or "Meeting"


def _slug_id(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "meeting"


# --------------------------------------------------------------------------
def build_source(cfg: OtterConfig | None, inbox: Path | None = None):
    """Pick a source: an explicit inbox wins, else the configured Otter backend.

    Returns None when neither is usable, so the caller can print one actionable
    message instead of every layer guessing at the cause.
    """
    if inbox is not None:
        return InboxSource(inbox)
    if cfg is None:
        return None
    if cfg.backend == "web":
        return WebOtterSource(cfg)
    return OfficialOtterSource(cfg)
