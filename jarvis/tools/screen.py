"""
JARVIS Screen Awareness — see what's on the user's screen (Windows).

Two capabilities:
1. Window/app list via PowerShell (fast, text-based)
2. Screenshot via Pillow ImageGrab → NVIDIA vision API (sees everything)

Ported from macOS AppleScript to Windows PowerShell.
"""

import asyncio
import base64
import logging
import tempfile
from pathlib import Path

log = logging.getLogger("jarvis.screen")


async def get_active_windows() -> list[dict]:
    """Get list of visible windows with app name, window title, and frontmost status.

    Uses PowerShell to enumerate windows via Get-Process.
    Returns list of {"app": str, "title": str, "frontmost": bool}.
    """
    # PowerShell script to get visible windows
    ps_script = """
Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    public class User32 {
        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();
    }
"@
$foregroundHwnd = [User32]::GetForegroundWindow()
$procs = Get-Process | Where-Object { $_.MainWindowTitle -ne '' }
foreach ($p in $procs) {
    $isFront = ($p.MainWindowHandle -eq $foregroundHwnd)
    Write-Output "$($p.ProcessName)|||$($p.MainWindowTitle)|||$isFront"
}
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)

        if proc.returncode != 0:
            log.warning(f"get_active_windows failed: {stderr.decode()[:200]}")
            return []

        windows = []
        for line in stdout.decode().strip().split("\n"):
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                windows.append({
                    "app": parts[0].strip(),
                    "title": parts[1].strip(),
                    "frontmost": parts[2].strip().lower() == "true",
                })
        return windows

    except asyncio.TimeoutError:
        log.warning("get_active_windows timed out")
        return []
    except Exception as e:
        log.warning(f"get_active_windows error: {e}")
        return []


async def get_running_apps() -> list[str]:
    """Get list of running application names (visible windows only)."""
    ps_script = "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | Select-Object -ExpandProperty ProcessName -Unique"
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return [a.strip() for a in stdout.decode().strip().split("\n") if a.strip()]
        return []
    except Exception as e:
        log.warning(f"get_running_apps error: {e}")
        return []


async def take_screenshot(display_only: bool = True) -> str | None:
    """Take a screenshot and return base64-encoded PNG.

    Uses Pillow's ImageGrab on Windows (or PowerShell .NET fallback).

    Args:
        display_only: Ignored on Windows (always captures primary display).

    Returns:
        Base64-encoded PNG string, or None on failure.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    try:
        # Try Pillow first (fast, no subprocess)
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(tmp_path, "PNG")
        except ImportError:
            # Fallback: PowerShell .NET screenshot
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screen = [System.Windows.Forms.Screen]::PrimaryScreen
$bitmap = New-Object System.Drawing.Bitmap($screen.Bounds.Width, $screen.Bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($screen.Bounds.Location, [System.Drawing.Point]::Empty, $screen.Bounds.Size)
$bitmap.Save('{tmp_path.replace(chr(92), chr(92)+chr(92))}')
$graphics.Dispose()
$bitmap.Dispose()
"""
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode != 0 or not Path(tmp_path).exists():
                log.warning("Screenshot capture failed")
                return None

        data = Path(tmp_path).read_bytes()
        log.info(f"Screenshot captured: {len(data)} bytes")
        return base64.b64encode(data).decode()

    except asyncio.TimeoutError:
        log.warning("Screenshot timed out")
        return None
    except Exception as e:
        log.warning(f"Screenshot error: {e}")
        return None
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

async def find_on_screen(target: str, llm_router) -> dict | None:
    """Find an element on the screen using the Vision LLM.
    
    Returns:
        dict with 'x' and 'y' coordinates, or None if not found.
    """
    screenshot_b64 = await take_screenshot()
    if not screenshot_b64:
        return None

    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    prompt = (
        f"Find the following UI element on the screen: '{target}'. "
        "Return ONLY a JSON object with 'x' and 'y' keys containing the normalized coordinates "
        "(0.0 to 1.0) of the center of the element. 0.0, 0.0 is the top-left corner. "
        "If you cannot find the element, return an empty JSON object {}. No explanation, just JSON."
    )

    try:
        # Use generate_vision but we need JSON. We'll ask the model to format it.
        # Since generate_vision returns a string, we parse it manually.
        raw_response = await llm_router.generate_vision(
            prompt=prompt,
            image_b64=screenshot_b64,
            system="You are a GUI automation vision assistant.",
            temperature=0.1,
            max_tokens=100
        )
        
        # Clean up markdown JSON fences if present
        text = raw_response.strip()
        if text.startswith("```json"): text = text[7:]
        elif text.startswith("```"): text = text[3:]
        if text.endswith("```"): text = text[:-3]
        text = text.strip()
        
        import json
        data = json.loads(text)
        
        if "x" in data and "y" in data:
            x_norm = float(data["x"])
            y_norm = float(data["y"])
            return {
                "x": int(x_norm * screen_w),
                "y": int(y_norm * screen_h)
            }
        return None
    except Exception as e:
        log.warning(f"Vision find_on_screen error: {e}")
        return None


async def describe_screen(llm_router) -> str:
    """Describe what's on the user's screen.

    Uses window list + LLM summary. (Screenshot vision temporarily disabled pending multimodal router).
    """
    # Try screenshot + vision (Disabled for now as we use llm_router text only)
    # screenshot_b64 = await take_screenshot()

    # get window list and have LLM summarize
    windows = await get_active_windows()
    apps = await get_running_apps()

    if not windows and not apps:
        return "I wasn't able to see your screen, sir. Something may be blocking screen access."

    # Build a text description for LLM to summarize
    context_parts = []
    if windows:
        for w in windows:
            marker = " (ACTIVE)" if w["frontmost"] else ""
            context_parts.append(f"{w['app']}: {w['title']}{marker}")

    if apps:
        window_apps = set(w["app"] for w in windows) if windows else set()
        bg_apps = [a for a in apps if a not in window_apps]
        if bg_apps:
            context_parts.append(f"Background apps: {', '.join(bg_apps)}")

    if llm_router and context_parts:
        try:
            prompt = "Open windows:\n" + "\n".join(context_parts)
            system = (
                "You are JARVIS. Given the user's open windows and apps, summarize "
                "what they appear to be working on in 1-2 sentences. Natural voice, no markdown."
            )
            return await llm_router.generate(prompt=prompt, system=system)
        except Exception:
            pass

    # Raw fallback
    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {len(windows)} windows open across {len(set(w['app'] for w in windows))} apps."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result

    return f"Running apps: {', '.join(apps)}. Couldn't read window titles, sir."


def format_windows_for_context(windows: list[dict]) -> str:
    """Format window list as context string for the LLM."""
    if not windows:
        return ""
    lines = ["Currently open on your desktop:"]
    for w in windows:
        marker = " (active)" if w["frontmost"] else ""
        lines.append(f"  - {w['app']}: {w['title']}{marker}")
    return "\n".join(lines)
