"""
JARVIS Calendar Access — Windows stub.

On macOS, JARVIS reads Apple Calendar via AppleScript. On Windows,
there is no universal zero-config calendar API. This module provides
stub implementations that return sensible defaults.

To enable real calendar access on Windows, integrate with:
- Microsoft Outlook COM automation (win32com)
- Microsoft Graph API (requires OAuth)
- Local .ics file parsing
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger("jarvis.calendar")

_stub_warned = False


async def refresh_cache():
    """Refresh the event cache. Stub on Windows."""
    global _stub_warned
    if not _stub_warned:
        log.info("Calendar integration not available on Windows — stub mode. Events will show as empty.")
        _stub_warned = True


async def get_todays_events() -> list[dict]:
    """Get today's events from cache. Returns empty on Windows."""
    await refresh_cache()
    return []


async def get_upcoming_events(hours: int = 4) -> list[dict]:
    """Get events in the next N hours. Returns empty on Windows."""
    return []


async def get_next_event() -> dict | None:
    """Get the single next upcoming event. Returns None on Windows."""
    return None


async def get_calendar_names() -> list[str]:
    """Get list of all calendar names. Returns empty on Windows."""
    return []


def format_events_for_context(events: list[dict]) -> str:
    """Format events as context for the LLM."""
    if not events:
        return "Calendar integration not available on Windows."
    lines = []
    for evt in events:
        if evt.get("all_day"):
            entry = f"  All day — {evt['title']}"
        else:
            entry = f"  {evt['start']} — {evt['title']}"
        if evt.get("calendar"):
            entry += f" [{evt['calendar']}]"
        lines.append(entry)
    return "\n".join(lines)


def format_schedule_summary(events: list[dict]) -> str:
    """Format a brief voice-friendly summary of the schedule."""
    if not events:
        return "Calendar is not connected on this system, sir. No events to show."

    count = len(events)
    if count == 1:
        evt = events[0]
        if evt.get("all_day"):
            return f"You have one all-day event: {evt['title']}."
        return f"You have one event: {evt['title']} at {evt['start']}."

    summaries = []
    for evt in events[:5]:
        if evt.get("all_day"):
            summaries.append(f"{evt['title']} all day")
        else:
            summaries.append(f"{evt['title']} at {evt['start']}")

    result = f"You have {count} events today. "
    result += ". ".join(summaries[:3])
    if count > 3:
        result += f". And {count - 3} more."
    return result
