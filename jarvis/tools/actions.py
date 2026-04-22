"""
JARVIS Action Executor — Windows-based system actions.

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.

Build tasks write .antigravity_instructions.md for IDE-based delegation.
"""

import asyncio
import logging
import os
import re
import shutil
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from jarvis.tools.gui_controller import GUIController

# Global GUI Controller
gui = GUIController()

log = logging.getLogger("jarvis.actions")

DESKTOP_PATH = Path.home() / "Desktop"


def _get_terminal_cmd() -> list[str]:
    """Detect the best terminal emulator available on this Windows system."""
    # Prefer Windows Terminal
    wt = shutil.which("wt")
    if wt:
        return [wt]
    # Fallback to cmd
    return ["cmd.exe", "/c", "start", "cmd.exe"]


async def open_terminal(command: str = "") -> dict:
    """Open a terminal window and optionally run a command."""
    try:
        wt = shutil.which("wt")
        if wt:
            if command:
                cmd = [wt, "new-tab", "cmd", "/k", command]
            else:
                cmd = [wt]
        else:
            if command:
                cmd = ["cmd.exe", "/c", "start", "cmd.exe", "/k", command]
            else:
                cmd = ["cmd.exe", "/c", "start", "cmd.exe"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Don't wait for the terminal to close — just launch it
        await asyncio.sleep(0.5)
        return {
            "success": True,
            "confirmation": "Terminal is open.",
        }
    except Exception as e:
        log.error(f"open_terminal failed: {e}")
        return {
            "success": False,
            "confirmation": "I had trouble opening a terminal.",
        }


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in the user's default browser."""
    try:
        # os.startfile is the most reliable way on Windows
        os.startfile(url)
        return {
            "success": True,
            "confirmation": "Pulled that up in the browser.",
        }
    except Exception:
        # Fallback to webbrowser module
        try:
            webbrowser.open(url)
            return {
                "success": True,
                "confirmation": "Pulled that up in the browser.",
            }
        except Exception as e:
            log.error(f"open_browser failed: {e}")
            return {
                "success": False,
                "confirmation": "Had trouble opening the browser.",
            }


# Keep backward compat
async def open_chrome(url: str) -> dict:
    return await open_browser(url, "chrome")


async def create_antigravity_task(project_dir: str, prompt: str) -> dict:
    """Set up the project folder and prepare instructions for Antigravity.

    Writes the prompt to .antigravity_instructions.md so the user can easily
    ask the Antigravity agent to take over the codebase.
    """
    try:
        instruction_md = Path(project_dir) / ".antigravity_instructions.md"
        instruction_md.write_text(f"# Task Specification\\n\\n{prompt}\\n\\nBuild this completely according to the user's intent. If it's a web app, ensure it works standalone.\\n")
        
        return {
            "success": True,
            "confirmation": "I've initialized the project workspace and prepared the specifications. Antigravity is ready to take over whenever you give the word.",
        }
    except Exception as e:
        log.error(f"create_antigravity_task failed: {e}")
        return {
            "success": False,
            "confirmation": "Had trouble preparing the workspace, sir.",
        }


async def prompt_existing_terminal(project_name: str, prompt: str) -> dict:
    """Send a prompt to the project's instructions file for Antigravity to pick up.
    """
    project_dir = None
    desktop = DESKTOP_PATH
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            project_dir = str(d)
            break
            
    if not project_dir:
        return {"success": False, "confirmation": f"I couldn't find the {project_name} project to update, sir."}
        
    try:
        instruction_md = Path(project_dir) / ".antigravity_instructions.md"
        with instruction_md.open("a", encoding="utf-8") as f:
            f.write(f"\n\n### Additional Instruction\n{prompt}\n")
            
        return {
            "success": True,
            "confirmation": "I've appended your new instructions for Antigravity, sir.",
        }
    except Exception as e:
        log.error(f"prompt_existing_terminal failed: {e}")
        return {
            "success": False,
            "confirmation": "Failed to update the project instructions.",
        }


async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL.

    NOTE: This is not available on Windows without browser extensions
    or debug protocol. Stubbed to return empty dict.
    """
    return {}


async def monitor_build(project_dir: str, ws=None, synthesize_fn=None) -> None:
    """Monitor a build for completion. Notify via WebSocket when done."""
    import base64

    output_file = Path(project_dir) / ".jarvis_output.txt"
    start = time.time()
    timeout = 600  # 10 minutes

    while time.time() - start < timeout:
        await asyncio.sleep(5)
        if output_file.exists():
            content = output_file.read_text()
            if "--- JARVIS TASK COMPLETE ---" in content:
                log.info(f"Build complete in {project_dir}")
                if ws and synthesize_fn:
                    try:
                        msg = "The build is complete, sir."
                        audio_bytes = await synthesize_fn(msg)
                        if audio_bytes:
                            encoded = base64.b64encode(audio_bytes).decode()
                            await ws.send_json({"type": "status", "state": "speaking"})
                            await ws.send_json({"type": "audio", "data": encoded, "text": msg})
                            await ws.send_json({"type": "status", "state": "idle"})
                    except Exception as e:
                        log.warning(f"Build notification failed: {e}")
                return

    log.warning(f"Build timed out in {project_dir}")


async def execute_action(intent: dict, projects: list = None) -> dict:
    """Route a classified intent to the right action function.

    Args:
        intent: {"action": str, "target": str} from classify_intent()
        projects: list of known project dicts for resolving working dirs

    Returns: {"success": bool, "confirmation": str, "project_dir": str | None}
    """
    action = intent.get("action", "chat")
    target = intent.get("target", "")

    if action == "open_terminal":
        result = await open_terminal()
        result["project_dir"] = None
        return result

    elif action == "browse":
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        else:
            url = f"https://www.google.com/search?q={quote(target)}"

        # Detect which browser user wants — on Windows we use default browser
        result = await open_browser(url)
        result["project_dir"] = None
        return result

    elif action == "build":
        # Create project folder on Desktop, write instructions for Antigravity
        project_name = _generate_project_name(target)
        project_dir = str(DESKTOP_PATH / project_name)
        os.makedirs(project_dir, exist_ok=True)
        result = await create_antigravity_task(project_dir, target)
        result["project_dir"] = project_dir
        return result

    elif action == "app_search":
        # Search within an application (e.g., YouTube)
        result_str = await gui.search_in_youtube(target)
        return {
            "success": True,
            "confirmation": result_str,
            "project_dir": None
        }

    elif action == "gui_interact":
        # Arbitrary GUI interaction (e.g., focus and type)
        # target format: "app_name ||| text"
        if "|||" in target:
            app, text = target.split("|||", 1)
            result_str = await gui.open_and_type(app.strip(), text.strip())
        else:
            result_str = await gui.type_text(target)
            
        return {
            "success": True,
            "confirmation": result_str,
            "project_dir": None
        }

    else:
        return {"success": False, "confirmation": "", "project_dir": None}


def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    # First: check for a quoted name like "tiktok-analytics-dashboard"
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = quoted.group(1).strip()
        # Already kebab-case or close to it
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip()
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    # Second: check for "called X" or "named X" pattern
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    # Fallback: extract meaningful words
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"
