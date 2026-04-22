"""
OS Context Module for JARVIS.
Provides passive omniscience to the LLM by reading active windows,
background applications, and media state (Spotify).
"""

import logging
from typing import Dict, List, Optional
import pygetwindow as gw

log = logging.getLogger("jarvis.os_context")

def get_spotify_state(windows) -> str:
    """Check window titles to see if Spotify is playing."""
    for w in windows:
        # Spotify's class name is often omitted or generic, so we search by title.
        # When stopped/paused, it's just "Spotify" or "Spotify Free" / "Spotify Premium".
        # When playing, it becomes "Artist - Song" or "Song - Artist".
        title = w.title
        if not title:
            continue
        
        # Check if the title belongs to the Spotify process
        # Wait, since we don't have process-to-window easily without win32process,
        # we can just use heuristics.
        # But wait, we can just look for a window named "Spotify Premium" or "Spotify Free".
        if "Spotify" in title and " - " not in title:
            return "Spotify is open but paused."
            
    # If we couldn't find a plain "Spotify" window, let's see if there's a window 
    # that matches the typical music format but isn't Chrome/Edge/Discord.
    # Note: Actually Spotify window titles can just be "Song - Artist".
    # A more robust way to find Spotify using pygetwindow:
    return "Spotify status unknown."

def get_os_context(work_session=None) -> str:
    """Build a comprehensive string of the current OS state."""
    # Try to grab clipboard safely
    clipboard_text = ""
    try:
        import pyperclip
        cb = pyperclip.paste()
        if cb and isinstance(cb, str):
            cb_clean = cb.encode('ascii', 'ignore').decode().strip()
            if cb_clean:
                clipboard_text = cb_clean[:300]
                if len(cb_clean) > 300:
                    clipboard_text += "..."
    except Exception:
        pass

    try:
        active_window = gw.getActiveWindow()
        active_title = active_window.title if active_window else "Desktop / Unknown"
        
        all_windows = gw.getAllWindows()
        
        # Extract unique application names based on window titles
        open_apps = set()
        spotify_state = "Spotify is not running."
        browser_context = ""
        
        for w in all_windows:
            title = w.title.strip()
            if not title:
                continue
                
            if "Google Chrome" in title or "Microsoft Edge" in title or "Mozilla Firefox" in title:
                app_name = "Chrome" if "Chrome" in title else "Edge" if "Edge" in title else "Firefox"
                open_apps.add(app_name)
                # Parse the tab title out of the window title
                # e.g., "Understanding Quantum Physics - YouTube - Google Chrome"
                tab_title = title.replace(" - Google Chrome", "").replace(" - Personal - Microsoft Edge", "").replace(" - Microsoft Edge", "").replace(" - Mozilla Firefox", "")
                if w.isActive:
                    browser_context = f"Looking at '{tab_title}' in {app_name}"
            elif "Visual Studio Code" in title:
                open_apps.add("VS Code")
            elif "Discord" in title:
                open_apps.add("Discord")
            elif "Notepad" in title:
                open_apps.add("Notepad")
            elif "Edge" in title:
                open_apps.add("Edge")
            elif "Slack" in title:
                open_apps.add("Slack")
                
            # Spotify detection
            if title.startswith("Spotify"):
                open_apps.add("Spotify")
                if title in ["Spotify", "Spotify Premium", "Spotify Free"]:
                    spotify_state = "Spotify is paused."
            elif " - " in title and not any(app in title for app in ["Chrome", "Edge", "Discord", "Code"]):
                # Heuristic for playing Spotify song (Spotify changes its title to "Artist - Song")
                # Need to be careful not to trigger on things like "Document - Word"
                if len(title.split(" - ")) == 2 and w.height > 0:
                    # Let's assume this is Spotify if Spotify is running
                    spotify_state = f"Playing '{title}' on Spotify"
                    open_apps.add("Spotify")

        context_lines = [
            "[SYSTEM STATE]",
            f"Active Window: \"{active_title}\"",
        ]
        
        if open_apps:
            context_lines.append(f"Open Apps: {', '.join(sorted(open_apps))}")
            
        if browser_context:
            context_lines.append(f"Browser: {browser_context}")
            
        context_lines.append(f"Media: {spotify_state}")
        
        if clipboard_text:
            context_lines.append(f"Clipboard (Recent Copy): \"{clipboard_text}\"")
        
        if work_session and work_session.working_dir:
            context_lines.append(f"Antigravity Agent: Active in {work_session.working_dir}")
            
        return "\n".join(context_lines)
        
    except Exception as e:
        log.warning(f"Failed to get OS context: {e}")
        return "[SYSTEM STATE]\nUnavailable"

if __name__ == "__main__":
    print(get_os_context())
