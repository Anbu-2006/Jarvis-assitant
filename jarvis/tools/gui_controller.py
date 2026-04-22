"""
JARVIS GUI Controller — Full OS-level screen interaction.

Capabilities:
- Click anywhere on screen by coordinates
- Click on UI elements by finding them visually
- Type text into any focused field
- Keyboard shortcuts and hotkeys
- Window management (focus, minimize, maximize, close, resize)
- Scroll in any direction
- Drag and drop
- Screenshot and element finding
"""

import time
import logging
import asyncio
import subprocess
from typing import Optional, Dict

import pyautogui
import keyboard as kb_lib

try:
    from pywinauto import Desktop
except ImportError:
    Desktop = None

log = logging.getLogger("jarvis.gui")

# Safety settings
pyautogui.PAUSE = 0.3
pyautogui.FAILSAFE = True  # Move mouse to top-left corner to abort


class GUIController:
    """Full OS-level GUI interaction controller."""

    def __init__(self):
        self._screen_w, self._screen_h = pyautogui.size()

    # ── Mouse Actions ─────────────────────────────────────────────────────

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1):
        """Click at screen coordinates."""
        log.info(f"Click ({button}) at ({x}, {y}) x{clicks}")
        pyautogui.click(x, y, button=button, clicks=clicks)

    def double_click(self, x: int, y: int):
        """Double-click at screen coordinates."""
        log.info(f"Double-click at ({x}, {y})")
        pyautogui.doubleClick(x, y)

    def right_click(self, x: int, y: int):
        """Right-click at screen coordinates."""
        log.info(f"Right-click at ({x}, {y})")
        pyautogui.rightClick(x, y)

    def move_mouse(self, x: int, y: int, duration: float = 0.3):
        """Move mouse to coordinates."""
        pyautogui.moveTo(x, y, duration=duration)

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.5):
        """Drag from one point to another."""
        log.info(f"Drag ({start_x},{start_y}) → ({end_x},{end_y})")
        pyautogui.moveTo(start_x, start_y, duration=0.2)
        pyautogui.mouseDown()
        pyautogui.moveTo(end_x, end_y, duration=duration)
        pyautogui.mouseUp()

    def scroll(self, clicks: int = 3, x: int = None, y: int = None):
        """Scroll at current or specified position. Positive=up, Negative=down."""
        pyautogui.scroll(clicks, x=x, y=y)

    def get_mouse_position(self) -> Dict[str, int]:
        """Get current mouse position."""
        x, y = pyautogui.position()
        return {"x": x, "y": y}

    # ── Keyboard Actions ──────────────────────────────────────────────────

    def type_text(self, text: str, interval: float = 0.03):
        """Type text character by character."""
        log.info(f"Typing: {text[:50]}...")
        pyautogui.write(text, interval=interval)

    def type_unicode(self, text: str):
        """Type text that may contain unicode/special characters."""
        import pyperclip
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")

    def press_key(self, key: str):
        """Press a single key."""
        log.info(f"Press key: {key}")
        pyautogui.press(key)

    def hotkey(self, *keys: str):
        """Press a keyboard shortcut (e.g. 'ctrl', 'c')."""
        log.info(f"Hotkey: {'+'.join(keys)}")
        pyautogui.hotkey(*keys)

    def press_enter(self):
        pyautogui.press("enter")

    def press_tab(self):
        pyautogui.press("tab")

    def press_escape(self):
        pyautogui.press("escape")

    def select_all(self):
        pyautogui.hotkey("ctrl", "a")

    def copy(self):
        pyautogui.hotkey("ctrl", "c")

    def paste(self):
        pyautogui.hotkey("ctrl", "v")

    def undo(self):
        pyautogui.hotkey("ctrl", "z")

    def save(self):
        pyautogui.hotkey("ctrl", "s")

    # ── Window Management ─────────────────────────────────────────────────

    def focus_window(self, title_pattern: str) -> bool:
        """Bring a window to front by title (regex pattern)."""
        if not Desktop:
            log.warning("pywinauto not available")
            return False
        try:
            windows = Desktop(backend="uia").windows(title_re=f".*{title_pattern}.*")
            if not windows:
                windows = Desktop(backend="win32").windows(title_re=f".*{title_pattern}.*")
            if windows:
                windows[0].set_focus()
                log.info(f"Focused: {windows[0].window_text()}")
                return True
            return False
        except Exception as e:
            log.error(f"Focus window failed: {e}")
            return False

    def list_windows(self) -> list[str]:
        """List all visible window titles."""
        if not Desktop:
            return []
        try:
            windows = Desktop(backend="uia").windows()
            return [w.window_text() for w in windows if w.window_text().strip()]
        except Exception:
            return []

    def minimize_window(self):
        """Minimize the active window."""
        pyautogui.hotkey("win", "down")

    def maximize_window(self):
        """Maximize the active window."""
        pyautogui.hotkey("win", "up")

    def close_window(self):
        """Close the active window."""
        pyautogui.hotkey("alt", "F4")

    def switch_app(self):
        """Alt+Tab to switch between apps."""
        pyautogui.hotkey("alt", "tab")

    def show_desktop(self):
        """Win+D to show desktop."""
        pyautogui.hotkey("win", "d")

    def snap_left(self):
        """Snap window to left half."""
        pyautogui.hotkey("win", "left")

    def snap_right(self):
        """Snap window to right half."""
        pyautogui.hotkey("win", "right")

    # ── Volume Control ────────────────────────────────────────────────────

    def volume_up(self, steps: int = 5):
        for _ in range(steps):
            kb_lib.press_and_release("volume up")

    def volume_down(self, steps: int = 5):
        for _ in range(steps):
            kb_lib.press_and_release("volume down")

    def volume_mute(self):
        kb_lib.press_and_release("volume mute")

    # ── System Actions ────────────────────────────────────────────────────

    def lock_screen(self):
        """Lock the workstation."""
        import ctypes
        ctypes.windll.user32.LockWorkStation()

    def screenshot(self, region=None) -> Optional[str]:
        """Take a screenshot, save to temp, return path."""
        import tempfile
        path = tempfile.mktemp(suffix=".png")
        img = pyautogui.screenshot(region=region)
        img.save(path)
        return path

    def find_on_screen(self, image_path: str, confidence: float = 0.8):
        """Find an image on screen, return center coordinates or None."""
        try:
            location = pyautogui.locateOnScreen(image_path, confidence=confidence)
            if location:
                center = pyautogui.center(location)
                return {"x": center.x, "y": center.y}
        except Exception as e:
            log.warning(f"Image search failed: {e}")
        return None

    def click_image(self, image_path: str, confidence: float = 0.8) -> bool:
        """Find and click on an image on screen."""
        pos = self.find_on_screen(image_path, confidence)
        if pos:
            self.click(pos["x"], pos["y"])
            return True
        return False

    # ── Browser Interaction ───────────────────────────────────────────────

    def browser_new_tab(self):
        pyautogui.hotkey("ctrl", "t")

    def browser_close_tab(self):
        pyautogui.hotkey("ctrl", "w")

    def browser_navigate(self, url: str):
        """Navigate to URL in current browser."""
        pyautogui.hotkey("ctrl", "l")  # Focus address bar
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")  # Select all
        pyautogui.write(url, interval=0.02)
        pyautogui.press("enter")

    def browser_back(self):
        pyautogui.hotkey("alt", "left")

    def browser_forward(self):
        pyautogui.hotkey("alt", "right")

    def browser_refresh(self):
        pyautogui.press("f5")

    def browser_search(self, query: str):
        """Open new tab and search."""
        self.browser_new_tab()
        time.sleep(0.3)
        pyautogui.write(query, interval=0.02)
        pyautogui.press("enter")


# Module-level singleton
gui = GUIController()
