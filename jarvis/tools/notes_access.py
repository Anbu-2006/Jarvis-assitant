"""
JARVIS Notes Access — Local Markdown Files (Windows-compatible).

On macOS, JARVIS used Apple Notes via AppleScript. On Windows,
notes are stored as local markdown files in data/notes/.

Can read, search, and create notes. CANNOT delete (safety).
"""

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.notes")

# Notes directory — relative to this script
NOTES_DIR = Path(__file__).parent / "data" / "notes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(title: str) -> str:
    """Convert a note title to a safe filename."""
    # Remove characters not safe for Windows filenames
    safe = re.sub(r'[<>:"/\\|?*]', '', title)
    safe = safe.strip().replace(' ', '_')
    if not safe:
        safe = f"note_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return safe[:100]  # Cap length


async def get_recent_notes(count: int = 10) -> list[dict]:
    """Get most recent notes (title + creation date + folder)."""
    notes = []
    try:
        md_files = sorted(
            NOTES_DIR.rglob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in md_files[:count]:
            stat = f.stat()
            # Use file stem as title, replacing underscores with spaces
            title = f.stem.replace('_', ' ')
            # Folder is the parent relative to NOTES_DIR
            rel_parent = f.parent.relative_to(NOTES_DIR)
            folder = str(rel_parent) if str(rel_parent) != "." else "Notes"
            notes.append({
                "title": title,
                "date": datetime.fromtimestamp(stat.st_mtime).strftime("%B %d, %Y at %I:%M %p"),
                "folder": folder,
            })
    except Exception as e:
        log.warning(f"get_recent_notes error: {e}")
    return notes


async def read_note(title_match: str) -> dict | None:
    """Read a note by title (partial match). Returns title + body."""
    try:
        match_lower = title_match.lower()
        for f in NOTES_DIR.rglob("*.md"):
            if match_lower in f.stem.lower().replace('_', ' '):
                content = f.read_text(encoding="utf-8")
                # Truncate very long notes
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                return {
                    "title": f.stem.replace('_', ' '),
                    "body": content,
                }
    except Exception as e:
        log.warning(f"read_note error: {e}")
    return None


async def search_notes_apple(query: str, count: int = 5) -> list[dict]:
    """Search notes by title or content keyword."""
    results = []
    query_lower = query.lower()
    try:
        for f in sorted(NOTES_DIR.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
            if len(results) >= count:
                break
            title = f.stem.replace('_', ' ')
            # Check title first
            if query_lower in title.lower():
                stat = f.stat()
                results.append({
                    "title": title,
                    "date": datetime.fromtimestamp(stat.st_mtime).strftime("%B %d, %Y at %I:%M %p"),
                })
                continue
            # Check content
            try:
                content = f.read_text(encoding="utf-8")
                if query_lower in content.lower():
                    stat = f.stat()
                    results.append({
                        "title": title,
                        "date": datetime.fromtimestamp(stat.st_mtime).strftime("%B %d, %Y at %I:%M %p"),
                    })
            except Exception:
                pass
    except Exception as e:
        log.warning(f"search_notes error: {e}")
    return results


async def create_apple_note(title: str, body: str, folder: str = "Notes") -> bool:
    """Create a new note as a markdown file.

    Args:
        title: Note title (becomes the filename).
        body: Note content (stored as-is, can include markdown).
        folder: Subfolder name within notes directory.
    """
    try:
        folder_path = NOTES_DIR / folder.replace(" ", "_")
        folder_path.mkdir(parents=True, exist_ok=True)

        filename = _sanitize_filename(title) + ".md"
        filepath = folder_path / filename

        # Add title as H1 header
        content = f"# {title}\n\n{body}\n"
        filepath.write_text(content, encoding="utf-8")

        log.info(f"Created note: {filepath}")
        return True
    except Exception as e:
        log.error(f"create_apple_note error: {e}")
        return False


async def get_note_folders() -> list[str]:
    """Get list of note folder names."""
    folders = ["Notes"]  # Default folder always exists
    try:
        for d in NOTES_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                folders.append(d.name.replace('_', ' '))
    except Exception as e:
        log.warning(f"get_note_folders error: {e}")
    return sorted(set(folders))
