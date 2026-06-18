"""Single source of truth for the data shapes (NFR-10).

Holds both:
  * the fetch output — grouped Slack messages per person (SRS Section 6.1), and
  * the summary record produced downstream (SRS Section 6.2).

Defining them once means a new field is a localized change that flows through
fetch.py -> summarize.py -> sheets.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Fetch output (SRS 6.1) — grouped messages
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """A single Slack message kept for the standup."""

    ts: str
    """Slack timestamp, e.g. ``"1718500000.000100"`` — also the message's unique id."""

    text: str
    user_id: str
    is_thread_reply: bool = False

    @property
    def epoch(self) -> float:
        """Unix epoch seconds, for ordering."""
        return float(self.ts)


@dataclass
class PersonStandup:
    """All of one person's messages for the run window, in time order."""

    user_id: str
    display_name: str
    messages: list[Message] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The person's messages joined into one block for the reasoning engine."""
        return "\n".join(m.text for m in self.messages)


# A mapping from Slack user id -> that person's grouped standup (SRS 6.1).
GroupedMessages = dict[str, PersonStandup]
