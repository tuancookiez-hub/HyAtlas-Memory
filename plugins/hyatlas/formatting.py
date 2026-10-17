"""Prompt formatting for search hits: the lines the agent reads before a reply.

Kept free of Hermes imports so the rules can be tested without the host installed.
"""

from __future__ import annotations

import re
from typing import Any, Dict

# A stored event date is YYYY-MM-DD or YYYY-MM. Anything else is dropped, so a
# stray word in the field never reaches the prompt as if it were a date.
_EVENT_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")

# How many superseded versions a hit shows, and how much of each one.
_MAX_HISTORY = 2
_HISTORY_CHARS = 160


def iso_date(unix_seconds: Any) -> str:
    """Return the YYYY-MM-DD (UTC) for a Unix timestamp, or "" when it has none.

    Search hits carry gmt_created as Unix seconds. A missing, zero or
    non-numeric value yields "" so callers can omit the date rather than
    print a placeholder.
    """
    try:
        ts = int(unix_seconds)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def format_hit_line(m: Dict[str, Any], limit: int) -> str:
    """Build the prompt lines for one search hit, or "" when it has no content.

    The first line is the hit itself, with its layer, its dates and its score.
    The date tells the agent when a fact happened or is due, and when it was
    saved, so an old status does not read as current. Up to two superseded
    versions follow as "was:" lines, newest first, so the agent can see what
    changed. Each line is kept short to protect the prompt budget.
    """
    content = m.get("content", "")
    if not content:
        return ""
    snippet = content[:limit]

    date = iso_date(m.get("gmt_created"))
    event = str(m.get("event_date") or "")
    if not _EVENT_DATE_RE.match(event):
        event = ""
    if event:
        when = f", happened {event}"
        if date and date != event:
            when += f", saved {date}"
    else:
        when = f", {date}" if date else ""

    layer = m.get("layer", "")
    score = m.get("score", 0.0)
    lines = [f"- ({layer}{when}, score {score:.2f}) {snippet}"]

    shown = 0
    for entry in m.get("history") or []:
        if shown >= _MAX_HISTORY:
            break
        if not isinstance(entry, dict):
            continue
        old = str(entry.get("content") or "")
        if not old:
            continue
        shown += 1
        line = f"    was: {old[:_HISTORY_CHARS]}"
        old_date = str(entry.get("date") or "")
        if old_date:
            line += f" (until {old_date})"
        lines.append(line)

    return "\n".join(lines)
