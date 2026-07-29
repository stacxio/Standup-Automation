"""Load and normalise a meeting transcript for gap analysis (access method A).

Method A means the transcript text is supplied directly — an exported Otter
`.txt` file or pasted text — so there is no Otter login or page scrape. Otter's
plain-text export interleaves speaker headers and their spoken lines, e.g.

    Soma Pani  0:03
    Yesterday I finished the login screen and started the API client.

    Raghul  1:20
    I closed the DOB bugs and deployed to prod.

parse_transcript() turns that into [{speaker, text}] segments. When the text has
no recognisable speaker headers (a plain paste), the whole thing becomes one
unattributed segment so the pipeline still works.
"""

from __future__ import annotations

import re
from pathlib import Path

# A speaker header ends in a timestamp: "Name  12:34" or "Name 1:02:03".
_SPEAKER = re.compile(r"^\s*(?P<name>.+?)\s+\d{1,2}:\d{2}(?::\d{2})?\s*$")


def load_text(*, path: str | None = None, text: str | None = None) -> str:
    """Return raw transcript text from a file path or an inline string."""
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    if text is not None:
        return text
    raise ValueError("Provide either path= or text=")


def _is_header(line: str) -> str | None:
    """Return the speaker name if `line` is a 'Name  mm:ss' header, else None.

    A real header is a short label (a name), not a spoken sentence that happens
    to end with a time, so anything longer than a few words is treated as speech.
    """
    m = _SPEAKER.match(line)
    if not m:
        return None
    name = m.group("name").strip()
    return name if 0 < len(name.split()) <= 5 else None


def parse_transcript(raw: str) -> list[dict]:
    """Return [{speaker, text}] from Otter-style text; speaker '' if unlabelled."""
    segments: list[dict] = []
    speaker = ""
    buf: list[str] = []

    def flush() -> None:
        joined = " ".join(b.strip() for b in buf if b.strip()).strip()
        if joined:
            segments.append({"speaker": speaker, "text": joined})

    for line in raw.splitlines():
        name = _is_header(line)
        if name is not None:
            flush()
            speaker, buf = name, []
        else:
            buf.append(line)
    flush()

    if not segments and raw.strip():  # plain paste with no headers
        segments.append({"speaker": "", "text": raw.strip()})
    return segments


def speakers(segments: list[dict]) -> list[str]:
    """Distinct non-empty speaker labels, in first-appearance order."""
    seen: dict[str, None] = {}
    for s in segments:
        if s["speaker"]:
            seen.setdefault(s["speaker"], None)
    return list(seen)


def render(segments: list[dict]) -> str:
    """Flatten segments back to 'Speaker: text' lines for the engine prompt."""
    return "\n".join(f"{s['speaker'] or 'Unknown'}: {s['text']}" for s in segments)
