"""
JARVIS Mail Access — Windows stub (READ-ONLY intent preserved).

On macOS, JARVIS reads Apple Mail via AppleScript. On Windows,
there is no universal zero-config mail API. This module provides
stub implementations that return sensible defaults.

To enable real mail access on Windows, integrate with:
- Microsoft Outlook COM automation (win32com)
- Microsoft Graph API (requires OAuth)
- IMAP direct access (requires credentials)

IMPORTANT: This module is intentionally READ-ONLY.
No send, delete, move, or modify functions exist by design.
"""

import logging

log = logging.getLogger("jarvis.mail")

_stub_warned = False


async def _ensure_mail_running():
    """No-op on Windows."""
    global _stub_warned
    if not _stub_warned:
        log.info("Mail integration not available on Windows — stub mode.")
        _stub_warned = True


async def get_accounts() -> list[str]:
    """Get list of configured mail account names. Returns empty on Windows."""
    await _ensure_mail_running()
    return []


async def get_unread_count() -> dict:
    """Get unread message count. Returns zero on Windows.

    Returns: {"total": int, "accounts": {"AccountName": count, ...}}
    """
    await _ensure_mail_running()
    return {"total": 0, "accounts": {}}


async def get_recent_messages(count: int = 10) -> list[dict]:
    """Get most recent messages. Returns empty on Windows."""
    await _ensure_mail_running()
    return []


async def get_unread_messages(count: int = 10) -> list[dict]:
    """Get unread messages. Returns empty on Windows."""
    await _ensure_mail_running()
    return []


async def get_messages_from_account(account_name: str, count: int = 10) -> list[dict]:
    """Get recent messages from a specific account. Returns empty on Windows."""
    await _ensure_mail_running()
    return []


async def search_mail(query: str, count: int = 10) -> list[dict]:
    """Search mail by subject or sender keyword. Returns empty on Windows."""
    await _ensure_mail_running()
    return []


async def read_message(subject_match: str) -> dict | None:
    """Read the full content of a message. Returns None on Windows."""
    await _ensure_mail_running()
    return None


def format_unread_summary(unread: dict) -> str:
    """Format unread counts for voice."""
    total = unread["total"]
    if total == 0:
        return "Mail is not connected on this system, sir."

    parts = []
    for acct, count in unread["accounts"].items():
        if count > 0:
            parts.append(f"{count} in {acct}")

    if len(parts) == 1:
        return f"You have {total} unread {'message' if total == 1 else 'messages'} — {parts[0]}."
    elif parts:
        return f"You have {total} unread messages: {', '.join(parts)}."
    else:
        return f"You have {total} unread {'message' if total == 1 else 'messages'}."


def format_messages_for_context(messages: list[dict], label: str = "Recent emails") -> str:
    """Format messages as context for the LLM."""
    if not messages:
        return f"{label}: Mail not connected on Windows."

    lines = [f"{label}:"]
    for m in messages[:10]:
        read_marker = "" if m.get("read") else " [UNREAD]"
        line = f"  - {m['sender']}: {m['subject']}{read_marker}"
        if m.get("date"):
            date_str = m["date"]
            if " at " in date_str:
                date_str = date_str.split(" at ")[0].split(", ", 1)[-1] if ", " in date_str else date_str
            line += f" ({date_str})"
        lines.append(line)
    return "\n".join(lines)


def format_messages_for_voice(messages: list[dict]) -> str:
    """Format messages for voice response."""
    if not messages:
        return "Mail is not connected on this system, sir."

    count = len(messages)
    if count == 1:
        m = messages[0]
        sender = _short_sender(m["sender"])
        return f"One message from {sender}: {m['subject']}."

    summaries = []
    for m in messages[:5]:
        sender = _short_sender(m["sender"])
        summaries.append(f"{sender} regarding {m['subject']}")

    result = f"You have {count} messages. "
    result += ". ".join(summaries[:3])
    if count > 3:
        result += f". And {count - 3} more."
    return result


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string like 'John Doe <john@example.com>'."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender
