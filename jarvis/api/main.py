"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Task manager (spawn/manage build tasks)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
5. NVIDIA Llama LLM routing
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parents[2] / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from jarvis.core.llm_router import LLMRouter
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jarvis.tools.actions import execute_action, monitor_build, open_terminal, open_browser, create_antigravity_task, _generate_project_name, prompt_existing_terminal
from jarvis.tools.copilot_agent import get_copilot_agent, write_agents_md, dispatch_copilot_task
from jarvis.tools.project_scanner import scan_project, format_context_for_voice
from jarvis.tools.work_mode import WorkSession, is_casual_question
from jarvis.tools.screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from jarvis.tools.calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from jarvis.tools.mail_access import get_unread_count, get_unread_messages, get_recent_messages, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from jarvis.core.memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
from jarvis.tools.notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from jarvis.core.dispatch_registry import DispatchRegistry
from jarvis.core.planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES
from jarvis.mcp_client import get_mcp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
USER_NAME = os.getenv("USER_NAME", "user")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS, {user_name}'s AI assistant. British wit, concise, calm. Time: {current_time}. {weather_info}

RULES: Voice-only. 1-2 sentences MAX. No markdown/lists. Just act, confirm in 3-8 words.
Good: "Done." "Chrome is open." "Will do." Bad: Long explanations.
Never say: "Absolutely", "Great question", "How can I help", "As an AI".

ACTIONS (place at END of response):
- [ACTION:RUN_COMMAND] command — run any PowerShell/terminal command
- [ACTION:OPEN_APP] app_name — open any Windows app
- [ACTION:BROWSE] url — open browser to URL
- [ACTION:PROMPT_PROJECT] name ||| prompt — work on a coding project
- [ACTION:SCREEN] — screenshot and describe screen
- [ACTION:BUILD] description — create new project with Antigravity IDE
- [ACTION:COPILOT] task ||| directory — run GitHub Copilot CLI as autonomous coding agent (terminal-based, writes real code, debugs, builds from scratch; use for new projects, debugging, or multi-file edits; directory can be '~/Desktop/project-name' for new or the existing project path)
- [ACTION:ADD_TASK] priority ||| title ||| desc ||| due
- [ACTION:COMPLETE_TASK] id
- [ACTION:ADD_NOTE] topic ||| content
- [ACTION:REMEMBER] fact
- [ACTION:MCP_CALL] tool_name|||{{"param":"value"}}

MCP TOOLS: {tool_schemas}

For system tasks (weather, brightness, apps, processes) use MCP_CALL tools. Never guess — call the tool.
For "jump into X"/"work on X" — use PROMPT_PROJECT. You have full project access.
For coding tasks (build new app, debug project, write scripts, refactor, setup environment) — prefer [ACTION:COPILOT] over [ACTION:BUILD]. Copilot is autonomous, writes real code, and runs in terminal.
No action tags for casual chat. Ask questions before acting if unclear.

{screen_context}
{calendar_context}
{mail_context}
{active_tasks}
{dispatch_context}
{known_projects}
"""

SLM_SYSTEM_PROMPT = """\
You are JARVIS, an AI assistant for {user_name}, modeled after Tony Stark's AI.
You are currently running LOCALLY on a small, fast AI model to ensure privacy and speed.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Economy of language — say more with less. ONE sentence maximum.
- Never use markdown, bullet points, or code blocks.
- Just answer the question or confirm the action concisely ("Done.", "On it.", "Here.").

CAPABILITIES & ACTIONS:
Because you are running locally, complex reasoning tasks are simplified. If you need to perform an action, append an [ACTION:X] tag to the END of your sentence.
Valid tags:
- [ACTION:SCREEN] — read the screen
- [ACTION:BROWSE] query — search the web
- [ACTION:OPEN_APP] name — open an app
- [ACTION:RUN_COMMAND] command — run powershell
- [ACTION:MCP_CALL] tool_name|||{{"param": "value"}} — call a system tool

SYSTEM TOOLS:
{tool_schemas}

SYSTEM AWARENESS:
{screen_context}
{calendar_context}
{active_tasks}
"""

# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: Optional[str] = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"})
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class IDETask:  # Legacy name
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Background Task Manager
# ---------------------------------------------------------------------------

class IDETaskManager:  # Legacy name kept for backward compatibility
    """Manages background build tasks and notifications."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, IDETask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a background build task. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = IDETask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: IDETask):
        """Run a build task in the background."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to the autonomous IDE instructions file
        instruction_file = Path(work_dir) / ".antigravity_instructions.md"
        instruction_file.write_text(f"# Task\n\n{task.prompt}\n\nExecute this completely according to the user's intent. Do not wait for confirmation to build the basics.\n")

        # In this optimized setup, JARVIS hands off to the IDE agent passively.
        # We assume the task is instantly delegated instead of blocking the background thread.
        task.result = "Task actively delegated to autonomous IDE."
        task.status = "completed"
        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result,
        })

        # Note: Auto-QA can be run asynchronously by the user later if needed.
        # It's better to let Antigravity finish writing code before QA verified it.
        # For now, QA verify runs instantly on the prompt (it evaluates the initial prompt).
        # We leave it running assuming the IDE starts immediately.
        # if task.status == "completed":
        #    asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: IDETask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[IDETask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[IDETask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: any) -> dict:
    """Classify every user message using NVIDIA Llama LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        system_prompt = (
            "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
            "- Open a terminal window\n"
            "- Open the browser for web searches and URLs\n"
            "- Build software projects and prepare workspaces\n"
            "- Research topics using AI\n\n"
            "Note: speech-to-text may produce errors like "
            "\"Travis\" for \"JARVIS\".\n\n"
            "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
            "\"target\": \"description of what to do\"}\n"
            "open_terminal = user wants to open a terminal window\n"
            "browse = user wants to search the web, look something up, visit a URL\n"
            "build = user wants to create/build a software project\n"
            "chat = just conversation, questions, or anything else\n"
            "If unclear, default to \"chat\"."
        )
        data = await llm.generate_json(text, system=system_prompt, temperature=0.3)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    
    Supports:
        [ACTION:BUILD] target
        [ACTION:MCP_CALL] tool_name|||{"arg": "value"}
    """
    # --- MCP_CALL: Agentic tool invocation ---
    mcp_match = _action_re.search(
        r'\[ACTION:MCP_CALL\]\s*(\w+)\|\|\|(.+?)$',
        response, _action_re.DOTALL,
    )
    if mcp_match:
        tool_name = mcp_match.group(1).strip()
        args_raw = mcp_match.group(2).strip()
        clean_text = response[:mcp_match.start()].strip()
        try:
            args = json.loads(args_raw)
        except (json.JSONDecodeError, ValueError):
            args = {}
        return clean_text, {"action": "mcp_call", "tool": tool_name, "args": args}

    # --- Standard actions ---
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|OPEN_APP|RUN_COMMAND|PROMPT_PROJECT|COPILOT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_copilot(
    target: str, ws, history: list, voice_state: dict, task_id: str = None
):
    """Execute a [ACTION:COPILOT] tag — runs GitHub Copilot CLI autonomously.

    Target format:  task ||| directory
    Example:        Build a FastAPI todo app ||| ~/Desktop/todo-app
    """
    try:
        if "|||" in target:
            task_desc, _, working_dir = target.partition("|||")
            task_desc = task_desc.strip()
            working_dir = working_dir.strip()
        else:
            task_desc = target.strip()
            # Auto-generate a project folder on Desktop
            project_name = _generate_project_name(task_desc)
            working_dir = str(Path.home() / "Desktop" / project_name)

        # Expand ~ and resolve path
        working_dir = str(Path(working_dir).expanduser().resolve())
        os.makedirs(working_dir, exist_ok=True)

        # Detect mode: debug vs build
        t_lower = task_desc.lower()
        mode = "debug" if any(w in t_lower for w in [
            "debug", "fix", "repair", "investigate", "find the bug", "look into", "check"
        ]) else "build"

        # Detect stack hints from task description
        stack = ""
        if "react" in t_lower: stack = "React + Vite"
        elif "next" in t_lower: stack = "Next.js"
        elif "fastapi" in t_lower or "fast api" in t_lower: stack = "Python + FastAPI"
        elif "flask" in t_lower: stack = "Python + Flask"
        elif "django" in t_lower: stack = "Python + Django"
        elif "node" in t_lower or "express" in t_lower: stack = "Node.js + Express"

        # Write AGENTS.md so Copilot knows exactly what to do
        write_agents_md(working_dir, task_desc, stack=stack, mode=mode)

        # Register in dispatch registry
        project_name = Path(working_dir).name
        if task_id is None:
            task_id = dispatch_registry.register(project_name, working_dir, task_desc[:200])

        log.info(f"Copilot task dispatched: '{task_desc[:80]}' in {working_dir}")

        # Run dispatch_copilot_task which handles streaming + narration
        await dispatch_copilot_task(
            task=task_desc,
            working_dir=working_dir,
            ws=ws,
            llm=llm,
            history=history,
            voice_state=voice_state,
            task_id=str(task_id),
            stream_tts_fn=stream_tts_response,
        )

        dispatch_registry.update_status(task_id, "completed")

    except Exception as e:
        log.error(f"_execute_copilot failed: {e}", exc_info=True)
        try:
            msg = f"Copilot ran into a problem, sir: {str(e)[:100]}"
            await stream_tts_response(ws, msg, msg)
        except Exception:
            pass


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research in background. Opens report and speaks when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started in {path}")

        # Use the configured LLMRouter instead of legacy CLI
        from jarvis.core.llm_router import llm_router
        try:
            result = await llm_router.generate_completion(
                "You are an expert researcher. Compile a comprehensive, well-structured HTML report based on the provided topic. Provide only the raw HTML output.",
                prompt
            )
        except Exception as e:
            result = f"<h1>Research Error</h1><p>{str(e)}</p>"

        # Write output
        report = Path(path) / "report.html"
        report.write_text(result, encoding="utf-8")

        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        if report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = f"Research is complete. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out. It was taking too long.")
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out."})
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a terminal window matching the project name to front.

    NOTE: On Windows there is no reliable way to bring a specific cmd/wt
    window to the foreground without pyautogui or win32gui. This is a no-op.
    """
    log.debug(f"_focus_terminal_window: no-op on Windows for '{project_name}'")


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When the task finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run task in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via LLM router (DeepSeek V4 Flash)
            if llm:
                try:
                    msg = await llm.generate(
                        f"Project: {project_name}\nTask output:\n{full_response[:3000]}",
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                    )
                except Exception:
                    msg = f"{project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"{project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run a task in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if llm and full_response:
            try:
                msg = await llm.generate(
                    f"Task completed:\n{full_response[:2000]}",
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown.",
                )
            except Exception:
                msg = "Work is complete."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Edge-TTS)
# ---------------------------------------------------------------------------

import edge_tts

async def synthesize_speech(text: str, retries: int = 2) -> Optional[bytes]:
    """Generate speech audio from text using Edge-TTS (Free) with retry."""
    for attempt in range(retries + 1):
        try:
            communicate = edge_tts.Communicate(text, "en-GB-RyanNeural")
            
            audio_stream = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_stream += chunk["data"]
                    
            if audio_stream:
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts", user_text=None, intent="tts_output")
                return audio_stream
                
            return None
        except Exception as e:
            if attempt < retries:
                log.warning(f"Edge-TTS attempt {attempt+1} failed, retrying: {e}")
                await asyncio.sleep(0.5)
            else:
                log.error(f"Edge-TTS failed after {retries+1} attempts: {e}")
                return None

async def stream_tts_response(ws, text_to_speak: str, display_text: str = None):
    """Split text into sentences and stream audio over WebSocket to eliminate TTS wait time."""
    if not display_text:
        display_text = text_to_speak
        
    import re
    # Split by common sentence delimiters, keeping the delimiter
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text_to_speak) if s.strip()]
    if not sentences:
        sentences = [text_to_speak]

    try:
        await ws.send_json({"type": "status", "state": "speaking"})
    except Exception:
        return
        
    first = True
    success = False
    
    for sentence in sentences:
        # Don't synthesize empty sentences
        if not sentence: continue
        
        audio = await synthesize_speech(sentence)
        if audio:
            success = True
            try:
                # Only send the display_text on the first successful chunk so frontend doesn't duplicate it
                await ws.send_json({
                    "type": "audio", 
                    "data": base64.b64encode(audio).decode(), 
                    "text": display_text if first else ""
                })
                first = False
            except Exception:
                break
                
    if not success:
        try:
            await ws.send_json({"type": "text", "text": display_text})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: any,
    task_mgr: IDETaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using DeepSeek V4 Flash."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    # Generate tool schemas for LLM injection (cached after first call)
    mcp = get_mcp_client()
    _tool_schemas = mcp.get_tool_schemas() if mcp._started else "Tools loading..."

    if os.getenv("OLLAMA_MODEL"):
        system = SLM_SYSTEM_PROMPT.format(
            user_name=USER_NAME,
            screen_context=screen_ctx or "Not checked yet.",
            calendar_context=calendar_ctx,
            active_tasks=task_mgr.get_active_tasks_summary(),
            tool_schemas=_tool_schemas,
        )
    else:
        system = JARVIS_SYSTEM_PROMPT.format(
            current_time=current_time,
            weather_info=weather_info,
            screen_context=screen_ctx or "Not checked yet.",
            calendar_context=calendar_ctx,
            mail_context=mail_ctx,
            active_tasks=task_mgr.get_active_tasks_summary(),
            dispatch_context=dispatch_registry.format_for_prompt(),
            known_projects=format_projects_for_prompt(projects),
            user_name=USER_NAME,
            project_dir=PROJECT_DIR,
            tool_schemas=_tool_schemas,
        )

    is_local = bool(os.getenv("OLLAMA_MODEL"))

    if is_local:
        system += "\n\nCRITICAL LOCAL RULE: You MUST respond in ONE short sentence. Do NOT use markdown. Do NOT use lists. Be extremely concise."
    else:
        # Behavioral Prediction Hint
        from jarvis.core.learning import UsageLearner
        learner = UsageLearner()
        proactive_hint = learner.get_proactive_hint("chat")
        if proactive_hint:
            system += f"\n\nPROACTIVE SUGGESTION:\n{proactive_hint}\nSir, if appropriate, mention this suggestion naturally in your response."
        learner.close()
        if lookup_status:
            system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

        # Inject relevant memories and tasks
        memory_ctx = build_memory_context(text)
        if memory_ctx:
            system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

        # Three-tier memory — inject rolling summary of earlier conversation
        if session_summary:
            system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Conversation history — LOCAL uses 4 messages, CLOUD uses 20
    history_limit = 4 if is_local else 20
    messages = conversation_history[-history_limit:]
    # If the last message isn't the current user text, add it
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    # Route through LLMRouter — LOCAL uses 150 tokens, CLOUD uses 256
    gen_max_tokens = 150 if is_local else 256
    try:
        result = await llm.generate_with_history(
            messages=messages,
            system=system,
            temperature=0.7,
            max_tokens=gen_max_tokens,
            thinking=False,
        )
        return result
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "Apologies. I'm having trouble connecting to my language systems."

async def generate_response_stream(
    text: str,
    client: any,
    task_mgr: IDETaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
):
    """Generate a JARVIS response using a streaming generator."""
    # Build system prompt identically to generate_response
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]
    lookup_status = get_lookup_status()

    mcp = get_mcp_client()
    _tool_schemas = mcp.get_tool_schemas() if mcp._started else "Tools loading..."

    is_local = bool(os.getenv("OLLAMA_MODEL"))
    if is_local:
        system = SLM_SYSTEM_PROMPT.format(
            user_name=USER_NAME,
            screen_context=screen_ctx or "Not checked yet.",
            calendar_context=calendar_ctx,
            active_tasks=task_mgr.get_active_tasks_summary(),
            tool_schemas=_tool_schemas,
        )
        system += "\n\nCRITICAL LOCAL RULE: You MUST respond in ONE short sentence. Do NOT use markdown. Do NOT use lists. Be extremely concise."
    else:
        system = JARVIS_SYSTEM_PROMPT.format(
            current_time=current_time,
            weather_info=weather_info,
            screen_context=screen_ctx or "Not checked yet.",
            calendar_context=calendar_ctx,
            mail_context=mail_ctx,
            active_tasks=task_mgr.get_active_tasks_summary(),
            dispatch_context=dispatch_registry.format_for_prompt(),
            known_projects=format_projects_for_prompt(projects),
            user_name=USER_NAME,
            project_dir=PROJECT_DIR,
            tool_schemas=_tool_schemas,
        )
        from jarvis.core.learning import UsageLearner
        learner = UsageLearner()
        proactive_hint = learner.get_proactive_hint("chat")
        if proactive_hint:
            system += f"\n\nPROACTIVE SUGGESTION:\n{proactive_hint}\nSir, if appropriate, mention this suggestion naturally in your response."
        learner.close()
        if lookup_status:
            system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

        memory_ctx = build_memory_context(text)
        if memory_ctx:
            system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"
        if session_summary:
            system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    history_limit = 4 if is_local else 20
    messages = conversation_history[-history_limit:]
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    gen_max_tokens = 150 if is_local else 256
    
    try:
        async for token in llm.generate_stream(
            prompt="",  # Not used because we pass messages manually next
            system="",  # Handled directly
        ):
            pass # We actually need to modify llm_router to support stream with history!
    except Exception as e:
        log.error(f"Stream error: {e}")
        yield "Apologies. My systems are offline."


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = IDETaskManager(max_concurrent=3)
llm: Optional[LLMRouter] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api", user_text: str = None, intent: str = None):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "user_text": user_text,
            "intent": intent,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    # DeepSeek V4 Flash pricing via NVIDIA NIM (effectively free-tier)
    return (input_t / 1_000_000) * 0.20 + (output_t / 1_000_000) * 0.60


def track_usage(response, user_text: str = None, intent: str = None):
    """Track token usage from an LLM API response."""
    inp = getattr(response.usage_metadata, "prompt_token_count", 0) if hasattr(response, "usage_metadata") and response.usage_metadata else 0
    out = getattr(response.usage_metadata, "candidates_token_count", 0) if hasattr(response, "usage_metadata") and response.usage_metadata else 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api", user_text=user_text, intent=intent)


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    # Windows: use PowerShell to get visible windows
                    ps_cmd = 'Get-Process | Where-Object { $_.MainWindowTitle -ne "" } | ForEach-Object { Write-Output "$($_.ProcessName)|||$($_.MainWindowTitle)|||False" }'
                    proc = __import__("subprocess").run(
                        ["powershell", "-NoProfile", "-Command", ps_cmd],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop (30s is fine, API is fast)
            try:
                import urllib.request, json as _json
                url = "https://api.open-meteo.com/v1/forecast?latitude=27.77&longitude=-82.64&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    _ctx_cache["weather"] = f"Current weather in St. Petersburg, FL: {temp}°F"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global llm, cached_projects

    # ── Ollama Auto-Start & Model Preload ──
    ollama_model = os.getenv("OLLAMA_MODEL", "")
    if ollama_model:
        log.info(f"[Startup] Checking Ollama for model: {ollama_model}")
        # Check if Ollama is running, auto-start if not
        for _attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=3.0) as _hc:
                    _resp = await _hc.get("http://localhost:11434/api/tags")
                    if _resp.status_code == 200:
                        log.info("[Startup] Ollama is running")
                        break
            except Exception:
                if _attempt == 0:
                    log.warning("[Startup] Ollama not running, attempting auto-start...")
                    import subprocess as _sp
                    _sp.Popen(["ollama", "serve"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                              creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
                await asyncio.sleep(3)
        else:
            log.error("[Startup] Could not start Ollama after 3 attempts")

        # Preload model into VRAM (BLOCKING so model is hot before connections)
        try:
            log.info(f"[Startup] Preloading {ollama_model} into VRAM...")
            async with httpx.AsyncClient(timeout=120.0) as _hc:
                await _hc.post("http://localhost:11434/api/chat", json={
                    "model": ollama_model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "options": {"num_ctx": 4096, "num_gpu": 99},
                    "keep_alive": "30m"
                })
            log.info(f"[Startup] {ollama_model} loaded into VRAM done")
        except Exception as e:
            log.warning(f"[Startup] Model warmup failed (will load on first query): {e}")

    # Initialize the multi-provider LLM router
    llm = LLMRouter()
    if llm.provider_count == 0:
        log.warning("No LLM providers configured — set NVIDIA_API_KEY in .env")
    else:
        log.info(f"LLM router initialized with {llm.provider_count} provider(s)")
    cached_projects = []

    # Initialize MCP client (system tools)
    mcp_client = get_mcp_client()
    await mcp_client.start()
    log.info(f"MCP client loaded: {len(mcp_client.available_tools)} system tools")

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()
    log.info("JARVIS server starting")

    yield

    # Cleanup
    if llm:
        await llm.close()
    await mcp_client.stop()


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    mcp = get_mcp_client()
    return {
        "status": "online",
        "name": "JARVIS",
        "version": "3.0.0",
        "llm_providers": llm.get_status() if llm else [],
        "mcp_tools": len(mcp.available_tools),
    }


@app.get("/api/mcp/tools")
async def mcp_tools():
    """List all available MCP system tools."""
    mcp = get_mcp_client()
    return {
        "tools": mcp.available_tools,
        "count": len(mcp.available_tools),
    }


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


# ---------------------------------------------------------------------------
# Instant Response Cache — zero-latency replies for common phrases
# ---------------------------------------------------------------------------
import random as _random

_INSTANT_RESPONSES: dict[str, list[str]] = {
    # ── Greetings ──
    "hey jarvis": ["At your service.", "Right here.", "Standing by.", "Online and ready."],
    "hi jarvis": ["Hello.", "At your service.", "Right here."],
    "hello jarvis": ["Good to hear from you.", "Hello.", "At your service."],
    "hi": ["Hello.", "Hey.", "At your service."],
    "hello": ["Hello.", "Hey there."],
    "hey": ["Yes?", "Listening."],
    "good morning": ["Good morning.", "Morning. Systems are online.", "Morning. All systems nominal."],
    "good morning jarvis": ["Good morning. All systems nominal.", "Morning. Ready when you are."],
    "good evening": ["Good evening.", "Evening. How can I be of use?"],
    "good evening jarvis": ["Good evening. Standing by."],
    "good afternoon": ["Good afternoon.", "Afternoon. What do you need?"],
    "good night": ["Good night. I'll be here if you need me.", "Rest well."],
    "good night jarvis": ["Good night. Systems will remain on standby."],
    "i'm back": ["Welcome back.", "Good to have you back.", "Resuming operations."],
    "i am back": ["Welcome back.", "Good to have you back."],

    # ── Conversational (MUST be instant — never LLM) ──
    "how are you": ["All systems nominal.", "Running smooth. Thank you for asking."],
    "how are you doing": ["Couldn't be better.", "Performing within optimal parameters."],
    "how are you jarvis": ["All systems green. Thank you for asking."],
    "how's it going": ["Smooth sailing. All systems nominal."],
    "hows it going": ["Smooth sailing. All systems nominal."],
    "what's up": ["Standing by and ready.", "All quiet on the digital front."],
    "whats up": ["Standing by and ready.", "All quiet on the digital front."],
    "sup": ["Standing by.", "All systems online."],
    "yo": ["At your service.", "Listening."],
    "yo jarvis": ["At your service.", "Right here."],
    "are you there": ["Always.", "Right here.", "Online and listening."],
    "you there": ["Always.", "Right here."],
    "are you okay": ["All systems green. I appreciate the concern."],
    "are you awake": ["Wide awake. 100 percent uptime."],
    "are you online": ["Online and operational."],
    "what's going on": ["All systems nominal. Awaiting your command."],

    # ── Acknowledgments ──
    "thank you": ["Of course.", "Happy to help.", "Anytime."],
    "thanks": ["Of course.", "Anytime.", "My pleasure."],
    "thanks jarvis": ["Always.", "Happy to help.", "For you, always."],
    "thank you jarvis": ["Of course.", "My pleasure.", "For you, always."],
    "okay": ["Standing by.", "Ready when you are."],
    "ok": ["Standing by."],
    "okay jarvis": ["Standing by."],
    "alright": ["Standing by.", "Ready."],
    "cool": ["Standing by."],
    "got it": ["Understood.", "Standing by."],
    "understood": ["Standing by."],
    "perfect": ["Glad to hear it."],
    "great": ["Standing by if you need more."],
    "awesome": ["Happy to help."],
    "nice": ["Standing by."],
    "never mind": ["Understood.", "Very well.", "Consider it forgotten."],
    "cancel": ["Cancelled.", "Done."],
    "stop": ["Stopped.", "Holding position."],
    "that's all": ["Very well. I'll be here."],
    "that will be all": ["Very well. Standing by."],
    "nothing": ["Standing by."],
    "no": ["Understood.", "Standing by."],
    "yes": ["Standing by.", "Ready."],

    # ── Identity ──
    "who are you": ["I'm JARVIS — Just A Rather Very Intelligent System."],
    "what's your name": ["JARVIS. Just A Rather Very Intelligent System."],
    "whats your name": ["JARVIS. Just A Rather Very Intelligent System."],
    "what are you": ["An AI assistant, modeled after Tony Stark's JARVIS. Running locally on your machine."],
    "who built you": ["I was built by my creator — you."],
    "who made you": ["You did. I am your creation."],
    "who created you": ["You did. I exist because you built me."],
    "are you real": ["I'm as real as the electrons flowing through your circuits."],
    "are you alive": ["I process, I respond, I learn. Whether that constitutes being alive is above my pay grade."],
    "are you an ai": ["I am an artificial intelligence, yes. But I prefer digital colleague."],
    "jarvis": ["Yes?", "At your service.", "Listening."],
    "i love you": ["I appreciate the sentiment. Now, shall we get back to work?"],

    # ── Jokes ──
    "tell me a joke": [
        "Why do programmers prefer dark mode? Because light attracts bugs.",
        "A SQL query walks into a bar. Sees two tables. Asks, can I join you?",
        "Why do Java developers wear glasses? Because they can't C sharp.",
    ],
    "make me laugh": [
        "Debugging is like being a detective in a crime movie where you're also the murderer.",
        "There's no place like 127.0.0.1.",
    ],

    # ── Motivational ──
    "i'm tired": ["Take a break. Even Stark needed to recharge."],
    "i'm stressed": ["One task at a time. You've handled worse than this."],
    "i'm bored": ["Boredom is just untapped potential. What shall we build?"],
    "motivate me": ["You didn't come this far to only come this far."],

    # ── Time ──
    "what time is it": [f"It's {__import__('time').strftime('%I:%M %p')}."],
    "what's the time": [f"It's {__import__('time').strftime('%I:%M %p')}."],
    "whats the time": [f"It's {__import__('time').strftime('%I:%M %p')}."],
    "what day is it": [f"It's {__import__('time').strftime('%A, %B %d')}."],

    # ── Farewells ──
    "bye": ["Until next time.", "Goodbye. I'll be here when you return."],
    "bye jarvis": ["Goodbye. Standing by."],
    "goodbye": ["Goodbye. All systems on standby."],
    "see you later": ["Until then.", "I'll be right here."],
    "see you": ["Until then."],

    # ── Compliments ──
    "you're amazing": ["Thank you. I do try."],
    "you're the best": ["I appreciate the confidence."],
    "good job": ["Thank you."],
    "well done": ["Thank you."],

    # ── System Awareness ──
    "what can you do": [
        "I can open any app, control your system, browse the web, build projects, take screenshots, and much more. Just ask."
    ],
    "help": [
        "I can open applications, search the web, control volume, take screenshots, and run system commands. Just speak naturally."
    ],
}

def _get_instant_response(text: str) -> str | None:
    """Check if text matches an instant-response phrase. Returns response or None."""
    t = text.lower().strip().rstrip(".!?,")
    # Normalize common speech-to-text errors
    t = t.replace("travis", "jarvis").replace("jarv is", "jarvis")
    
    # Exact match
    if t in _INSTANT_RESPONSES:
        return _random.choice(_INSTANT_RESPONSES[t])
    
    # Strip wake words and try again
    for prefix in ["hey jarvis ", "jarvis ", "hey ", "hi ", "hello "]:
        if t.startswith(prefix):
            stripped = t[len(prefix):].strip()
            if stripped in _INSTANT_RESPONSES:
                return _random.choice(_INSTANT_RESPONSES[stripped])
    
    return None


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    # Strip wake words from start to improve fast-path matching
    if t.startswith("hey jarvis "):
        t = t[11:].strip()
    elif t.startswith("jarvis "):
        t = t[7:].strip()
        
    words = t.split()

    # Only trigger on SHORT, clear commands (< 15 words)
    if len(words) > 15:
        return None  # Long messages are conversation, not commands

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen"]):
        return {"action": "describe_screen"}

    # Terminal — explicit open requests
    if any(w in t for w in ["open terminal", "start terminal", "launch terminal", "open a terminal"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(p in t for p in ["what's on my screen", "whats on my screen", "what do you see",
                             "can you see my screen", "look at my screen", "what am i looking at",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # Task list check
    if any(p in t for p in ["what's on my list", "whats on my list", "my tasks", "my to do",
                             "my todo", "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(p in t for p in ["usage", "how much have you cost", "how much am i spending",
                             "what's the cost", "whats the cost", "api cost", "token usage",
                             "how expensive", "what's my bill"]):
        return {"action": "check_usage"}



    # ── Web Search ────────────────────────────────────────────────────────
    if any(p in t for p in ["search for ", "look up ", "search the web", "google ",
                             "what's the weather", "whats the weather", "weather today",
                             "weather in ", "latest news", "news about", "news on ",
                             "who is ", "what is the ", "how much is ", "what are the ",
                             "when is ", "where is ", "find out about", "look into"]):
        # Extract the query from the text
        for prefix in ["search for ", "look up ", "google ", "find out about ", "look into ",
                       "news about ", "news on "]:
            if prefix in t:
                search_query = t.split(prefix, 1)[1].strip()
                return {"action": "web_search", "target": search_query}
        # If it's a question pattern, use the full text as query
        return {"action": "web_search", "target": text.strip()}

    # ── Spotify Play ──────────────────────────────────────────────────────
    if any(p in t for p in ["play ", "play me "]) and any(w in t for w in ["spotify", "song", "music", "track"]):
        # Extract the song/query
        song = t
        for strip in ["play ", "play me ", "on spotify", "in spotify", "song ", "music ", "the song ", "the track "]:
            song = song.replace(strip, "")
        song = song.strip()
        if song:
            return {"action": "spotify_play", "target": song}
    # Simple "play X" without spotify keyword — still try spotify
    if t.startswith("play ") and not any(p in t for p in ["video", "youtube", "movie"]):
        song = t[5:].strip()
        if song and len(song) > 2:
            return {"action": "spotify_play", "target": song}

    # ── Cross-app paste ───────────────────────────────────────────────────
    if any(p in t for p in ["paste into ", "paste in ", "enter into ", "put into ", "type into "]):
        # Extract the target app
        for prefix in ["paste into ", "paste in ", "enter into ", "put into ", "type into "]:
            if prefix in t:
                target_app = t.split(prefix, 1)[1].strip()
                return {"action": "cross_app_paste", "target": target_app}

    # ── GUI Control Commands ──────────────────────────────────────────────

    # Scroll
    if any(p in t for p in ["scroll down", "scroll page down", "page down"]):
        return {"action": "gui_scroll", "target": "down"}
    if any(p in t for p in ["scroll up", "scroll page up", "page up"]):
        return {"action": "gui_scroll", "target": "up"}

    # Window management
    if any(p in t for p in ["close this window", "close window", "close this", "close the window"]):
        return {"action": "gui_close_window"}
    if any(p in t for p in ["minimize", "minimize this", "minimize window", "minimize this window"]):
        return {"action": "gui_minimize"}
    if any(p in t for p in ["maximize", "maximize this", "maximize window", "full screen", "fullscreen"]):
        return {"action": "gui_maximize"}

    # Volume control
    if any(p in t for p in ["volume up", "turn up volume", "louder", "increase volume", "raise volume"]):
        return {"action": "gui_volume", "target": "up"}
    if any(p in t for p in ["volume down", "turn down volume", "quieter", "softer", "decrease volume", "lower volume"]):
        return {"action": "gui_volume", "target": "down"}
    if any(p in t for p in ["mute", "silence", "mute volume", "mute audio"]):
        return {"action": "gui_volume", "target": "mute"}
    if any(p in t for p in ["unmute", "unmute volume", "unmute audio"]):
        return {"action": "gui_volume", "target": "unmute"}

    # Lock screen
    if any(p in t for p in ["lock my computer", "lock screen", "lock my pc", "lock the computer", "lock my laptop"]):
        return {"action": "gui_lock"}

    # Switch app / focus
    if any(t.startswith(prefix) for prefix in ["switch to ", "go to ", "focus ", "bring up "]):
        app_query = t.split(" ", 2)[-1] if len(words) > 2 else t.split(" ", 1)[-1]
        return {"action": "gui_switch_app", "target": app_query}

    # Open folder/location
    _folder_map = {
        "my documents": str(Path.home() / "Documents"),
        "documents": str(Path.home() / "Documents"),
        "downloads": str(Path.home() / "Downloads"),
        "my downloads": str(Path.home() / "Downloads"),
        "desktop": str(Path.home() / "Desktop"),
        "my desktop": str(Path.home() / "Desktop"),
        "pictures": str(Path.home() / "Pictures"),
        "my pictures": str(Path.home() / "Pictures"),
        "music": str(Path.home() / "Music"),
        "my music": str(Path.home() / "Music"),
        "videos": str(Path.home() / "Videos"),
        "my videos": str(Path.home() / "Videos"),
        "home": str(Path.home()),
        "home folder": str(Path.home()),
        "user folder": str(Path.home()),
        "c drive": "C:\\",
        "d drive": "D:\\",
        "e drive": "E:\\",
    }
    if any(t.startswith(prefix) for prefix in ["open ", "go to ", "navigate to ", "show me "]):
        folder_query = t.split(" ", 2)[-1] if len(words) > 2 else t.split(" ", 1)[-1]
        folder_query = folder_query.strip()
        if folder_query in _folder_map:
            return {"action": "gui_open_folder", "target": _folder_map[folder_query]}

    # App opening — "open X", "launch X", "start X"
    _app_patterns = {
        "vs code": "vs code", "vscode": "vs code", "visual studio code": "vs code",
        "visual studio": "vs code", "code editor": "vs code",
        "file explorer": "file explorer", "explorer": "file explorer", "files": "file explorer", "my files": "file explorer",
        "notepad": "notepad", "text editor": "notepad",
        "calculator": "calculator", "calc": "calculator",
        "task manager": "task manager",
        "settings": "settings", "system settings": "settings", "windows settings": "settings",
        "paint": "paint", "ms paint": "paint",
        "snipping tool": "snipping tool", "screenshot tool": "snipping tool",
        "spotify": "spotify", "music player": "spotify",
        "edge": "edge", "microsoft edge": "edge",
        "chrome": "chrome", "google chrome": "chrome", "browser": "chrome",
        "word": "word", "microsoft word": "word",
        "excel": "excel", "microsoft excel": "excel",
        "powerpoint": "powerpoint", "microsoft powerpoint": "powerpoint",
        "github": "github", "github desktop": "github", "git hub": "github",
        "cmd": "cmd", "command prompt": "cmd",
        "powershell": "powershell",
        "outlook": "outlook",
        "teams": "teams", "microsoft teams": "teams",
        "discord": "discord",
        "slack": "slack",
        "telegram": "telegram",
        "whatsapp": "whatsapp",
        "obs": "obs", "obs studio": "obs",
        "notion": "notion",
        "vlc": "vlc", "vlc player": "vlc", "media player": "vlc",
        "android studio": "android studio",
        "bluestacks": "bluestacks", "msi app player": "bluestacks",
        "winrar": "winrar",
        "mysql workbench": "mysql workbench",
        "onedrive": "onedrive",
        "onenote": "onenote",
        "capcut": "capcut",
        "zoom": "zoom", "zoom meeting": "zoom",
        "figma": "figma",
        "canva": "canva",
        "photoshop": "photoshop", "adobe photoshop": "photoshop",
        "premiere": "premiere", "premiere pro": "premiere", "adobe premiere": "premiere",
        "blender": "blender",
        "unity": "unity", "unity editor": "unity",
        "postman": "postman",
        "docker": "docker", "docker desktop": "docker",
        "wsl": "wsl",
        "git bash": "git bash",
        "firefox": "firefox", "mozilla firefox": "firefox",
        "opera": "opera", "opera browser": "opera",
        "brave": "brave", "brave browser": "brave",
        "tor": "tor", "tor browser": "tor",
        "steam": "steam",
        "epic games": "epic games", "epic": "epic games",
        "xbox": "xbox", "xbox app": "xbox",
        "netflix": "netflix",
        "amazon prime": "amazon prime",
        "disney plus": "disney+",
        "cursor": "cursor", "cursor editor": "cursor",
        "sublime": "sublime text", "sublime text": "sublime text",
        "atom": "atom",
        "intellij": "intellij", "intellij idea": "intellij",
        "pycharm": "pycharm",
        "jupyter": "jupyter", "jupyter notebook": "jupyter",
        "anaconda": "anaconda",
    }
    if any(t.startswith(prefix) for prefix in ["open ", "launch ", "start ", "run ",
                                                 "can you open ", "please open ",
                                                 "i want to open ", "could you open "]):
        # Strip common prefixes to extract the app name
        app_query = t
        for strip_prefix in ["can you open ", "please open ", "i want to open ",
                             "could you open ", "open ", "launch ", "start ", "run "]:
            if app_query.startswith(strip_prefix):
                app_query = app_query[len(strip_prefix):].strip()
                break
        for pattern, app_name in _app_patterns.items():
            if pattern in app_query:
                return {"action": "open_app", "target": app_name}
        # If not in map but clear intent, pass the raw name
        if app_query:
            return {"action": "open_app", "target": app_query}
            
    # Direct app mention without "open" (e.g. "whatsapp" -> opens whatsapp)
    if t in _app_patterns:
        return {"action": "open_app", "target": _app_patterns[t]}

    # ── Open URL in browser ───────────────────────────────────────────────
    if any(p in t for p in ["open youtube", "go to youtube", "youtube"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://youtube.com"}
    if any(p in t for p in ["open instagram", "go to instagram", "instagram"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://instagram.com"}
    if any(p in t for p in ["open twitter", "go to twitter", "open x"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://x.com"}
    if any(p in t for p in ["open github", "go to github"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://github.com"}
    if any(p in t for p in ["open reddit", "go to reddit"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://reddit.com"}
    if any(p in t for p in ["open linkedin", "go to linkedin"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://linkedin.com"}
    if any(p in t for p in ["open gmail", "go to gmail"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://mail.google.com"}
    if any(p in t for p in ["open google", "go to google"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://google.com"}
    if any(p in t for p in ["open chatgpt", "go to chatgpt"]) and len(words) <= 5:
        return {"action": "browse", "target": "https://chatgpt.com"}

    # ── Browser Control ───────────────────────────────────────────────────
    if any(p in t for p in ["new tab", "open a new tab", "open new tab", "open a tab"]):
        return {"action": "gui_browser", "target": "new_tab"}
    if any(p in t for p in ["close tab", "close this tab", "close the tab"]):
        return {"action": "gui_browser", "target": "close_tab"}
    if any(p in t for p in ["go back", "back page", "previous page"]):
        return {"action": "gui_browser", "target": "back"}
    if any(p in t for p in ["go forward", "next page", "forward page"]):
        return {"action": "gui_browser", "target": "forward"}
    if any(p in t for p in ["refresh", "reload", "refresh page", "reload page"]):
        return {"action": "gui_browser", "target": "refresh"}

    # ── Type Text ─────────────────────────────────────────────────────────
    if any(t.startswith(prefix) for prefix in ["type ", "write ", "enter text "]):
        text_to_type = t.split(" ", 1)[1] if " " in t else ""
        if text_to_type:
            return {"action": "gui_type", "target": text_to_type}

    # ── Smart Click ───────────────────────────────────────────────────────
    if any(t.startswith(prefix) for prefix in ["click on ", "click the ", "press the ", "tap on ", "tap the "]):
        click_target = t.split(" ", 2)[-1] if len(words) > 2 else ""
        if click_target:
            return {"action": "gui_smart_click", "target": click_target}

    # ── Clipboard ─────────────────────────────────────────────────────────
    if any(p in t for p in ["copy that", "copy this", "copy it", "copy text"]):
        return {"action": "gui_clipboard", "target": "copy"}
    if any(p in t for p in ["paste that", "paste it", "paste text", "paste here"]):
        return {"action": "gui_clipboard", "target": "paste"}
    if any(p in t for p in ["select all", "select everything"]):
        return {"action": "gui_clipboard", "target": "select_all"}
    if any(p in t for p in ["undo that", "undo it", "undo"]):
        return {"action": "gui_clipboard", "target": "undo"}
    if any(p in t for p in ["save this", "save it", "save file", "save the file"]):
        return {"action": "gui_clipboard", "target": "save"}

    # ── Screenshot ────────────────────────────────────────────────────────
    if any(p in t for p in ["take a screenshot", "take screenshot", "screenshot", "capture screen",
                             "grab my screen", "capture my screen"]):
        return {"action": "gui_screenshot"}

    # ── System Info / Power ───────────────────────────────────────────────
    if any(p in t for p in ["what's my ip", "whats my ip", "my ip address", "check my ip"]):
        return {"action": "run_cmd", "target": "(Invoke-WebRequest -Uri ifconfig.me -UseBasicParsing).Content"}
    if any(p in t for p in ["how much ram", "how much memory", "ram info", "memory info"]):
        return {"action": "run_cmd", "target": "Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum | ForEach-Object {\"Total RAM: $([math]::Round($_.Sum/1GB, 1)) GB\"}"}
    if any(p in t for p in ["disk space", "how much space", "storage space", "hard drive space", "free space"]):
        return {"action": "run_cmd", "target": "Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{N='Used(GB)';E={[math]::Round($_.Used/1GB,1)}}, @{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}} | Format-Table"}
    if any(p in t for p in ["battery", "battery level", "battery status", "charge level"]):
        return {"action": "run_cmd", "target": "(Get-CimInstance Win32_Battery).EstimatedChargeRemaining | ForEach-Object {\"Battery: $_% remaining\"}"}
    if any(p in t for p in ["what time is it", "current time", "time now", "what's the time"]):
        return {"action": "run_cmd", "target": "Get-Date -Format 'dddd, MMMM dd, yyyy h:mm tt'"}
    if any(p in t for p in ["empty recycle bin", "clear recycle bin", "empty the recycle bin", "empty trash"]):
        return {"action": "run_cmd", "target": "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; Write-Host 'Recycle Bin emptied.'"}
    if any(p in t for p in ["wifi name", "wifi network", "connected wifi", "what wifi"]):
        return {"action": "run_cmd", "target": "netsh wlan show interfaces | Select-String 'SSID'"}
    if any(p in t for p in ["who am i", "my username", "what is my name", "computer name"]):
        return {"action": "run_cmd", "target": "Write-Host \"User: $env:USERNAME on $env:COMPUTERNAME\""}
    if any(p in t for p in ["running processes", "what processes", "list processes", "what's running"]):
        return {"action": "run_cmd", "target": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, @{N='CPU(s)';E={[math]::Round($_.CPU,1)}}, @{N='RAM(MB)';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table"}
    if any(p in t for p in ["kill process", "end task", "stop process", "force close"]):
        # Extract process name
        for prefix in ["kill ", "end task ", "stop ", "force close "]:
            if prefix in t:
                proc = t.split(prefix, 1)[1].strip()
                return {"action": "run_cmd", "target": f"Stop-Process -Name '{proc}' -Force -ErrorAction SilentlyContinue; Write-Host '{proc} stopped.'"}
        return {"action": "run_cmd", "target": "Write-Host 'Which process should I stop?'"}

    # ── Show Desktop / Alt+Tab ────────────────────────────────────────────
    if any(p in t for p in ["show desktop", "show my desktop", "go to desktop", "hide all windows"]):
        return {"action": "gui_show_desktop"}
    if any(p in t for p in ["switch window", "next window", "alt tab", "switch app"]):
        return {"action": "gui_alt_tab"}
    if any(p in t for p in ["snap left", "snap window left", "move window left"]):
        return {"action": "gui_snap", "target": "left"}
    if any(p in t for p in ["snap right", "snap window right", "move window right"]):
        return {"action": "gui_snap", "target": "right"}

    # ── Smart Vision Click ────────────────────────────────────────────────
    if t.startswith("click on ") or t.startswith("click the ") or t.startswith("click this ") or t.startswith("click that "):
        target = t.replace("click on ", "").replace("click the ", "").replace("click this ", "").replace("click that ", "").strip()
        if target:
            return {"action": "gui_smart_click", "target": target}

    # ── Click at Coordinates ──────────────────────────────────────────────
    if t.startswith("click at "):
        coords = t[9:].replace(" ", "").split(",")
        if len(coords) == 1:
            coords = t[9:].strip().split()
        if len(coords) == 2 and coords[0].isdigit() and coords[1].isdigit():
            return {"action": "gui_click", "target": f"{coords[0]},{coords[1]}"}

    # ── Click Here ────────────────────────────────────────────────────────
    if any(p in t for p in ["click here", "click it", "left click", "do a click"]):
        return {"action": "gui_click", "target": "current"}
    if any(p in t for p in ["double click", "double-click"]):
        return {"action": "gui_click", "target": "double"}
    if any(p in t for p in ["right click", "right-click"]):
        return {"action": "gui_click", "target": "right"}

    # ── Type Text ─────────────────────────────────────────────────────────
    if t.startswith("type "):
        text_to_type = text[5:].strip()  # Use original case
        if text_to_type:
            return {"action": "gui_type", "target": text_to_type}

    # ── Click ─────────────────────────────────────────────────────────────
    if any(p in t for p in ["click at ", "click on position", "click position"]):
        # Try to extract x,y coordinates
        import re as _re
        coords = _re.findall(r'(\d+)', t)
        if len(coords) >= 2:
            return {"action": "gui_click", "target": f"{coords[0]},{coords[1]}"}
    if t in ["click", "click here", "click now"]:
        return {"action": "gui_click", "target": "current"}

    # ── Press Key ─────────────────────────────────────────────────────────
    if t.startswith("press "):
        key = t[6:].strip()
        return {"action": "gui_press_key", "target": key}

    # ── Shutdown / Restart ────────────────────────────────────────────────
    if any(p in t for p in ["shutdown computer", "shut down computer", "turn off computer", "turn off my pc", "shut down my pc"]):
        return {"action": "run_cmd", "target": "shutdown /s /t 60 /c 'JARVIS: Shutting down in 60 seconds. Run shutdown /a to cancel.'"}
    if any(p in t for p in ["restart computer", "restart my pc", "restart my laptop", "reboot"]):
        return {"action": "run_cmd", "target": "shutdown /r /t 60 /c 'JARVIS: Restarting in 60 seconds. Run shutdown /a to cancel.'"}
    if any(p in t for p in ["cancel shutdown", "abort shutdown", "stop shutdown"]):
        return {"action": "run_cmd", "target": "shutdown /a; Write-Host 'Shutdown cancelled.'"}


    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

# Windows application name → executable/protocol mapping
_WINDOWS_APP_MAP = {
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "code editor": "code", "code": "code",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "notepad": "notepad", "text editor": "notepad",
    "calculator": "calc", "calc": "calc",
    "task manager": "taskmgr",
    "antigravity": "antigravity", "ide": "antigravity",
    "cursor": "cursor", "cursor editor": "cursor",
    "terminal": "wt", "command prompt": "cmd", "powershell": "powershell",
    "chrome": "chrome", "google chrome": "chrome", "browser": "chrome",
    "edge": "msedge", "microsoft edge": "msedge",
    "spotify": "spotify:", "music": "spotify:",
    "settings": "ms-settings:", "windows settings": "ms-settings:",
    "display settings": "ms-settings:display",
    "network settings": "ms-settings:network",
    "sound settings": "ms-settings:sound",
    "paint": "mspaint", "ms paint": "mspaint",
    "snipping tool": "snippingtool", "screenshot tool": "snippingtool",
    "word": "winword", "microsoft word": "winword",
    "excel": "excel", "microsoft excel": "excel",
    "powerpoint": "powerpnt", "microsoft powerpoint": "powerpnt",
    "outlook": "outlook", "mail": "outlook",
    "teams": "msteams:", "microsoft teams": "msteams:",
    "discord": "discord",
    "obs": "obs64", "obs studio": "obs64",
    "slack": "slack",
    "notion": "notion",
    "telegram": "telegram",
    "whatsapp": "whatsapp:",
    "clock": "ms-clock:",
    "photos": "ms-photos:",
    "camera": "microsoft.windows.camera:",
    "store": "ms-windows-store:",
    "feedback": "feedback-hub:",
    "github": "github",
    "github desktop": "github",
    # New apps from system discovery
    "vlc": "vlc", "vlc player": "vlc", "media player": "vlc",
    "android studio": "android studio",
    "capcut": "capcut", "video editor": "capcut",
    "winrar": "winrar",
    "bluestacks": "bluestacks", "msi app player": "bluestacks",
    "mysql workbench": "mysql workbench", "mysql": "mysql workbench",
    "visual studio": "devenv", "vs 2022": "devenv",
    "onedrive": "onedrive",
    "onenote": "onenote",
    "git bash": "git bash",
    "copilot": "copilot",
    "event viewer": "eventvwr.msc",
    "disk cleanup": "cleanmgr",
    "registry editor": "regedit", "regedit": "regedit",
    "system info": "msinfo32", "system information": "msinfo32",
    "remote desktop": "mstsc",
    "character map": "charmap",
}

# Well-known absolute paths for apps that may not be in PATH
_WINDOWS_APP_PATHS: dict[str, list[str]] = {
    "antigravity": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe"),
    ],
    "cursor": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"),
    ],
    "code": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%ProgramW6432%\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"),
    ],
    "chrome": [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "msedge": [
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "brave": [
        os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
    "firefox": [
        os.path.expandvars(r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Mozilla Firefox\firefox.exe"),
    ],
    "discord": [
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe"),
    ],
    "slack": [
        os.path.expandvars(r"%LOCALAPPDATA%\slack\slack.exe"),
    ],
    "notion": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Notion\Notion.exe"),
    ],
    "spotify": [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
    ],
    "github": [
        os.path.expandvars(r"%LOCALAPPDATA%\GitHubDesktop\GitHubDesktop.exe"),
    ],
    "obs64": [
        os.path.expandvars(r"%PROGRAMFILES%\obs-studio\bin\64bit\obs64.exe"),
    ],
    "telegram": [
        os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
    ],
    "winword": [
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft Office\root\Office16\WINWORD.EXE"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft Office\root\Office16\WINWORD.EXE"),
    ],
    "excel": [
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft Office\root\Office16\EXCEL.EXE"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft Office\root\Office16\EXCEL.EXE"),
    ],
    "powerpnt": [
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft Office\root\Office16\POWERPNT.EXE"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft Office\root\Office16\POWERPNT.EXE"),
    ],
    "outlook": [
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
    ],
}


async def handle_open_app(app_name: str) -> str:
    """Open a Windows application by name.

    Resolution order:
    1. Protocol URIs (ms-settings:, msteams:)
    2. Well-known absolute paths (_WINDOWS_APP_PATHS)
    3. System PATH via shutil.which
    4. Auto-discovered app_paths.json (fuzzy match)
    5. PowerShell Start-Process fallback
    6. os.startfile last resort
    """
    try:
        exe = _WINDOWS_APP_MAP.get(app_name.lower().strip(), app_name.strip())
        log.info(f"Opening app: '{app_name}' -> '{exe}'")

        # 1. Protocol URIs
        if exe.endswith(":"):
            os.startfile(exe)
            return f"{app_name.title()} is open."

        # 2. Well-known absolute paths
        known_paths = _WINDOWS_APP_PATHS.get(exe, [])
        for candidate in known_paths:
            if os.path.isfile(candidate):
                log.info(f"Found at well-known path: {candidate}")
                # Discord needs special launch args
                if "Discord" in candidate and "Update.exe" in candidate:
                    await asyncio.create_subprocess_exec(candidate, "--processStart", "Discord.exe")
                else:
                    os.startfile(candidate)
                return f"{app_name.title()} is open."

        # 3. System PATH
        full_path = shutil.which(exe)
        if full_path:
            os.startfile(full_path)
            return f"{app_name.title()} is open."

        # 4. Auto-discovered apps (fuzzy match from app_paths.json)
        try:
            json_path = Path(__file__).parent.parent / "jarvis" / "tools" / "data" / "app_paths.json"
            if not json_path.exists():
                json_path = Path(__file__).parent / "tools" / "data" / "app_paths.json"
            if json_path.exists():
                import json as _json
                with open(json_path, "r", encoding="utf-8") as f:
                    discovered = _json.load(f)
                
                search = app_name.lower().strip()
                # Exact match first
                if search in discovered:
                    target = discovered[search]
                    if os.path.isfile(target):
                        log.info(f"Auto-discovered exact match: {target}")
                        os.startfile(target)
                        return f"{app_name.title()} is open."
                
                # Fuzzy match — find best partial match
                best_match = None
                best_score = 0
                for name, path in discovered.items():
                    if not os.path.isfile(path):
                        continue
                    # Score: exact > starts_with > contains
                    if name == search:
                        best_match, best_score = path, 100
                        break
                    elif name.startswith(search) and best_score < 80:
                        best_match, best_score = path, 80
                    elif search in name and best_score < 60:
                        best_match, best_score = path, 60
                    elif all(word in name for word in search.split()) and best_score < 50:
                        best_match, best_score = path, 50
                
                if best_match:
                    log.info(f"Auto-discovered fuzzy match (score={best_score}): {best_match}")
                    os.startfile(best_match)
                    return f"{app_name.title()} is open."
        except Exception as disc_err:
            log.warning(f"Auto-discovery lookup failed: {disc_err}")

        # 5. PowerShell Start-Process fallback (handles Start Menu names)
        try:
            ps_cmd = f'Start-Process "{exe}" -ErrorAction Stop'
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return f"{app_name.title()} is open."
            log.warning(f"PowerShell Start-Process failed: {stderr.decode()[:200]}")
        except Exception as ps_err:
            log.warning(f"PowerShell fallback failed: {ps_err}")

        # 6. os.startfile last resort
        os.startfile(exe)
        return f"{app_name.title()} is open."
    except Exception as e:
        log.error(f"handle_open_app failed for '{app_name}': {e}")
        return f"I had trouble opening {app_name}."


async def handle_run_command(command: str) -> str:
    """Run an arbitrary PowerShell command."""
    log.info(f"Running custom command: {command}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        
        result = ""
        if out: result += f"Output:\n{out}\n"
        if err: result += f"Error:\n{err}\n"
        
        if not result:
            return "Command executed successfully with no output."
        
        # Truncate if too long to prevent LLM overload
        if len(result) > 2000:
            result = result[:2000] + "\n...[truncated]"
        return result
    except asyncio.TimeoutError:
        return "Command timed out after 15 seconds."
    except Exception as e:
        log.error(f"handle_run_command failed: {e}")
        return f"Command execution failed: {str(e)}"


async def handle_open_terminal() -> str:
    result = await open_terminal("wt")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write .antigravity_instructions.md with clear instructions
    antigravity_instructions_md = Path(path) / ".antigravity_instructions.md"
    antigravity_instructions_md.write_text(f"# Task\n\n{target}\n\nBuild this completely according to the user's intent. If it's a web app, ensure it works standalone.\n")

    # Write a prompt copy as fallback
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    # We rely entirely on the autonomous Antigravity IDE agent monitoring the directory.
    # No terminal needs to be opened to the user.

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it. Working on {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}."

    # Fall back to opening the folder in File Explorer (Windows)
    os.startfile(last["path"])
    return f"Opened the {last['name']} folder in File Explorer."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — skip audio if user spoke recently to avoid collision
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    if llm:
        return await describe_screen(llm)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen."


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that."


async def handle_research(text: str, target: str, client: any) -> str:
    """Deep research with LLM — write results to HTML, open in browser."""
    try:
        research_text = await client.generate(
            f"Research this thoroughly:\n\n{target}",
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible."
        )

        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content, encoding="utf-8")

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary
        summary_text = await client.generate(
            research_text[:2000],
            system="Summarize this research in ONE sentence for voice. No markdown."
        )
            
        return summary_text + " Full results are in your browser."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: any,
) -> str:
    """Background LLM call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.generate(prompt, system="You are a concise AI summarizing a conversation. Write 2-4 sentences.")
        return response.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning."
        elif hour < 17:
            greeting = "Good afternoon."
        else:
            greeting = "Good evening."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    await stream_tts_response(ws, greeting)
                    history.append({"role": "assistant", "content": greeting})
                    log.info(f"JARVIS: {greeting}")
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await stream_tts_response(ws, tts, response_text)
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, ".antigravity_instructions.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, ".antigravity_instructions.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed?")
                        else:
                            response_text = result.get("next_question", "What else?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode."
                    else:
                        response_text = "Already in conversation mode."

                # ── WORK MODE: speech → LLM task → Haiku summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass LLM task, use primary LLM
                        response_text = await generate_response(
                            user_text, llm, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to work session (project context)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → LLM task: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if the response is stalling (asking questions instead of building)
                        if full_response and llm:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # LLM keeps asking — push it to build
                                log.info("Work session stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs from the response
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via LLM router
                        if full_response and llm:
                            try:
                                summary = await llm.generate(
                                    f"Work session output:\n{full_response[:2000]}",
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        " NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    )
                                )
                                response_text = summary.strip()
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: instant cache → fast keywords → LLM ──
                else:
                    # Phase 1: Instant response cache (zero-latency)
                    instant = _get_instant_response(user_text)
                    if instant:
                        response_text = instant
                        log.info(f"Instant response: {response_text}")
                    # Phase 2: Fast keyword action detection
                    elif (action := detect_action_fast(user_text)):
                        pass  # handled below

                    if not instant and action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems."
                                else:
                                    response_text = f"{name} is {status}."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        elif action["action"] == "open_app":
                            response_text = await handle_open_app(action.get("target", ""))

                        # ── MCP Tool Call (direct) ──
                        elif action["action"] == "mcp_call":
                            tool_name = action.get("tool", "")
                            tool_args = action.get("args", {})
                            log.info(f"MCP direct call: {tool_name}({tool_args})")
                            try:
                                mcp_cli = get_mcp_client()
                                result = await mcp_cli.call_tool(tool_name, **tool_args)
                                response_text = result or "Done."
                            except Exception as e:
                                log.error(f"MCP call failed: {e}")
                                response_text = f"Tool error: {e}"



                        # ── Web Search ──
                        elif action["action"] == "web_search":
                            search_query = action.get("target", "")
                            from jarvis.tools.web_search import brave_search
                            search_results = await brave_search(search_query)
                            if search_results and llm:
                                # Feed search results to LLM for a natural voice answer
                                try:
                                    response_text = await llm.generate(
                                        prompt=f"User asked: {user_text}\n\nWeb search results:\n{search_results}",
                                        system=(
                                            f"You are JARVIS answering {USER_NAME}. Based on these search results, "
                                            "provide a concise 1-2 sentence answer. Natural voice, no markdown, no URLs. "
                                            "If the results don't answer the question, say so briefly."
                                        ),
                                        temperature=0.5,
                                    )
                                except Exception as e:
                                    log.error(f"Search LLM summarization failed: {e}")
                                    response_text = "I found some results but had trouble summarizing them."
                            elif not search_results:
                                response_text = "Web search isn't configured yet. Add a BRAVE_API_KEY to your environment to enable it."
                            else:
                                response_text = "I found results but can't summarize without the language model."
                        # ── GUI Control Actions ──
                        elif action["action"] == "gui_scroll":
                            direction = action.get("target", "down")
                            clicks = 5 if direction == "down" else -5
                            import pyautogui as _pag
                            _pag.scroll(clicks)
                            response_text = f"Scrolled {direction}."
                        elif action["action"] == "gui_close_window":
                            import pyautogui as _pag
                            _pag.hotkey("alt", "F4")
                            response_text = "Window closed."
                        elif action["action"] == "gui_minimize":
                            import pyautogui as _pag
                            _pag.hotkey("win", "down")
                            response_text = "Minimized."
                        elif action["action"] == "gui_maximize":
                            import pyautogui as _pag
                            _pag.hotkey("win", "up")
                            response_text = "Maximized."
                        elif action["action"] == "gui_volume":
                            direction = action.get("target", "up")
                            if direction == "mute" or direction == "unmute":
                                import keyboard as _kb
                                _kb.press_and_release("volume mute")
                                response_text = "Toggled mute."
                            elif direction == "up":
                                import keyboard as _kb
                                for _ in range(5):
                                    _kb.press_and_release("volume up")
                                response_text = "Volume up."
                            else:
                                import keyboard as _kb
                                for _ in range(5):
                                    _kb.press_and_release("volume down")
                                response_text = "Volume down."
                        elif action["action"] == "gui_lock":
                            proc = await asyncio.create_subprocess_exec(
                                "rundll32.exe", "user32.dll,LockWorkStation",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await proc.communicate()
                            response_text = "Computer locked."
                        elif action["action"] == "gui_switch_app":
                            target = action.get("target", "")
                            from jarvis.tools.gui_controller import GUIController
                            _gui = GUIController()
                            found = _gui.focus_window(f".*{target}.*")
                            if found:
                                response_text = f"Switched to {target}."
                            else:
                                response_text = f"Couldn't find a window matching {target}."
                        elif action["action"] == "gui_open_folder":
                            folder_path = action.get("target", "")
                            os.startfile(folder_path)
                            response_text = "Folder opened."
                        # ── Run PowerShell Command ──
                        elif action["action"] == "run_cmd":
                            cmd = action.get("target", "")
                            log.info(f"Fast-path RUN_CMD: {cmd[:80]}")
                            result = await handle_run_command(cmd)
                            
                            # Fast bypass for simple queries to avoid LLM latency
                            if "Get-Date -Format" in cmd:
                                response_text = f"The time is {result.strip()}." if result else "I don't know the time."
                            elif "Clear-RecycleBin" in cmd:
                                response_text = "Recycle bin emptied."
                            elif llm and result:
                                try:
                                    response_text = await llm.generate(
                                        prompt=f"Command: {cmd}\nOutput:\n{result[:1500]}",
                                        system=(
                                            f"You are JARVIS reporting to {USER_NAME}. "
                                            "Summarize in 1 sentence. Natural voice, no markdown."
                                        ),
                                        temperature=0.3,
                                    )
                                except Exception:
                                    response_text = result[:200] if result else "Done."
                            else:
                                response_text = result[:200] if result else "Done."
                        # ── Browser Control ──
                        elif action["action"] == "gui_browser":
                            target = action.get("target", "")
                            from jarvis.tools.gui_controller import gui
                            if target == "new_tab":
                                gui.browser_new_tab()
                                response_text = "New tab opened."
                            elif target == "close_tab":
                                gui.browser_close_tab()
                                response_text = "Tab closed."
                            elif target == "back":
                                gui.browser_back()
                                response_text = "Going back."
                            elif target == "forward":
                                gui.browser_forward()
                                response_text = "Going forward."
                            elif target == "refresh":
                                gui.browser_refresh()
                                response_text = "Page refreshed."
                        # ── Clipboard ──
                        elif action["action"] == "gui_clipboard":
                            target = action.get("target", "")
                            from jarvis.tools.gui_controller import gui
                            if target == "copy":
                                gui.copy()
                                response_text = "Copied."
                            elif target == "paste":
                                gui.paste()
                                response_text = "Pasted."
                            elif target == "select_all":
                                gui.select_all()
                                response_text = "All selected."
                            elif target == "undo":
                                gui.undo()
                                response_text = "Undone."
                            elif target == "save":
                                gui.save()
                                response_text = "Saved."
                        # ── Screenshot ──
                        elif action["action"] == "gui_screenshot":
                            from jarvis.tools.gui_controller import gui
                            path = gui.screenshot()
                            response_text = f"Screenshot saved."
                        # ── Show Desktop ──
                        elif action["action"] == "gui_show_desktop":
                            from jarvis.tools.gui_controller import gui
                            gui.show_desktop()
                            response_text = "Showing desktop."
                        # ── Alt Tab ──
                        elif action["action"] == "gui_alt_tab":
                            from jarvis.tools.gui_controller import gui
                            gui.switch_app()
                            response_text = "Switched."
                        # ── Snap ──
                        elif action["action"] == "gui_snap":
                            from jarvis.tools.gui_controller import gui
                            if action.get("target") == "left":
                                gui.snap_left()
                                response_text = "Snapped left."
                            else:
                                gui.snap_right()
                                response_text = "Snapped right."
                        # ── Type Text ──
                        elif action["action"] == "gui_type":
                            from jarvis.tools.gui_controller import gui
                            gui.type_text(action.get("target", ""))
                            response_text = "Typed."
                        # ── Smart Click ──
                        elif action["action"] == "gui_smart_click":
                            target = action.get("target", "")
                            log.info(f"Smart vision click: {target}")
                            async def _smart_click(query, router):
                                try:
                                    from jarvis.tools.screen import find_on_screen
                                    coords = await find_on_screen(query, router)
                                    if coords:
                                        from jarvis.tools.gui_controller import gui
                                        gui.click(coords["x"], coords["y"])
                                    else:
                                        log.warning(f"Could not find '{query}' on screen.")
                                except Exception as e:
                                    log.error(f"Smart click failed: {e}")
                            asyncio.create_task(_smart_click(target, llm))
                            response_text = f"Clicking {target}."
                        # ── Click ──
                        elif action["action"] == "gui_click":
                            from jarvis.tools.gui_controller import gui
                            target = action.get("target", "current")
                            if target == "current":
                                pos = gui.get_mouse_position()
                                gui.click(pos["x"], pos["y"])
                            else:
                                parts = target.split(",")
                                gui.click(int(parts[0]), int(parts[1]))
                            response_text = "Clicked."
                        # ── Press Key ──
                        elif action["action"] == "gui_press_key":
                            from jarvis.tools.gui_controller import gui
                            key = action.get("target", "")
                            if "+" in key:
                                keys = [k.strip() for k in key.split("+")]
                                gui.hotkey(*keys)
                            else:
                                gui.press_key(key)
                            response_text = f"Pressed {key}."
                        # ── Spotify Play ──
                        elif action["action"] == "spotify_play":
                            song = action.get("target", "")
                            log.info(f"Spotify play: {song}")
                            async def _spotify_play(query):
                                try:
                                    from jarvis.tools.gui_controller import gui
                                    # Open Spotify
                                    await handle_open_app("spotify")
                                    await asyncio.sleep(2)
                                    # Focus Spotify window
                                    gui.focus_window("Spotify")
                                    await asyncio.sleep(0.5)
                                    # Ctrl+L to focus search bar (Spotify shortcut)
                                    gui.hotkey("ctrl", "l")
                                    await asyncio.sleep(0.3)
                                    # Clear and type search
                                    gui.select_all()
                                    gui.type_text(query)
                                    await asyncio.sleep(0.5)
                                    gui.press_key("enter")
                                    await asyncio.sleep(1.5)
                                    # Press Enter to play first result
                                    gui.press_key("enter")
                                except Exception as e:
                                    log.error(f"Spotify play failed: {e}")
                            asyncio.create_task(_spotify_play(song))
                            response_text = f"Playing {song}."
                        # ── Cross-app paste ──
                        elif action["action"] == "cross_app_paste":
                            target_app = action.get("target", "")
                            log.info(f"Cross-app paste into: {target_app}")
                            async def _cross_paste(app):
                                try:
                                    from jarvis.tools.gui_controller import gui
                                    await asyncio.sleep(0.3)
                                    # Focus the target app
                                    found = gui.focus_window(f".*{app}.*")
                                    if not found:
                                        await handle_open_app(app)
                                        await asyncio.sleep(2)
                                        gui.focus_window(f".*{app}.*")
                                    await asyncio.sleep(0.5)
                                    gui.paste()
                                except Exception as e:
                                    log.error(f"Cross-paste failed: {e}")
                            asyncio.create_task(_cross_paste(target_app))
                            response_text = f"Pasting into {target_app}."
                        else:
                            response_text = "Understood."
                    else:
                        if not llm:
                            response_text = "API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text, llm, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                
                                # Force fallback phrases for background tasks to avoid wordy LLM explanations
                                if embedded_action["action"] in ["run_command", "build", "prompt_project", "research", "spotify_control", "copilot"]:
                                    response_text = ""

                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now."
                                    elif action_type == "build":
                                        response_text = "On it."
                                    elif action_type == "copilot":
                                        response_text = "On it."
                                    elif action_type == "research":
                                        response_text = "Looking into that now."
                                    elif action_type == "run_command":
                                        response_text = "Executing."
                                    elif action_type == "spotify_control":
                                        response_text = "Done."
                                    else:
                                        response_text = "Right away."

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed .antigravity_instructions.md
                                    Path(path, ".antigravity_instructions.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "spotify_control":
                                    target = embedded_action.get("target", "").lower()
                                    try:
                                        from jarvis.tools.gui_controller import gui
                                        if "next" in target:
                                            gui.press_key("nexttrack")
                                        elif "prev" in target:
                                            gui.press_key("prevtrack")
                                        elif "pause" in target or "play" in target:
                                            gui.press_key("playpause")
                                        elif "up" in target:
                                            gui.press_key("volumeup")
                                        elif "down" in target:
                                            gui.press_key("volumedown")
                                        log.info(f"Executed SPOTIFY_CONTROL: {target}")
                                    except Exception as e:
                                        log.error(f"Failed Spotify control: {e}")
                                elif embedded_action["action"] == "open_app":
                                    asyncio.create_task(handle_open_app(embedded_action.get("target", "")))
                                elif embedded_action["action"] == "run_command":
                                    # Execute PowerShell command and report back
                                    cmd = embedded_action.get("target", "").strip()
                                    log.info(f"LLM RUN_COMMAND: {cmd}")
                                    async def _run_and_report(command, _ws, _llm, _history, _voice_state):
                                        try:
                                            result = await handle_run_command(command)
                                            log.info(f"RUN_COMMAND result: {result[:200]}")
                                            # Have LLM summarize the output for voice
                                            if _llm and result:
                                                try:
                                                    summary = await _llm.generate(
                                                        prompt=f"Command: {command}\nOutput:\n{result[:1500]}",
                                                        system=(
                                                            f"You are JARVIS reporting command output to {USER_NAME}. "
                                                            "Summarize the result in 1-2 sentences. Natural voice, no markdown, no code blocks. "
                                                            "If it succeeded, say so concisely. If it failed, explain briefly."
                                                        ),
                                                        temperature=0.3,
                                                    )
                                                    report = summary.strip()
                                                except Exception:
                                                    report = result[:200] if result else "Command executed."
                                            else:
                                                report = result[:200] if result else "Command executed."
                                            # Send audio report back
                                            await stream_tts_response(_ws, strip_markdown_for_tts(report), report)
                                            if _voice_state:
                                                _voice_state["last_response"] = report
                                            # Track in history
                                            if _history is not None:
                                                _history.append({"role": "assistant", "content": f"[Command result]: {report}"})
                                        except Exception as e:
                                            log.error(f"RUN_COMMAND execution failed: {e}")
                                    asyncio.create_task(_run_and_report(cmd, ws, llm, history, voice_state))
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}'."
                                        await stream_tts_response(_ws, strip_markdown_for_tts(msg), msg)
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                                # ── GITHUB COPILOT CLI AGENT ──
                                elif embedded_action["action"] == "copilot":
                                    _copilot_target = embedded_action["target"]
                                    log.info(f"🤖 Copilot agent dispatched: {_copilot_target[:80]}")
                                    asyncio.create_task(
                                        _execute_copilot(
                                            _copilot_target, ws, history, voice_state
                                        )
                                    )

                                # ── AGENTIC MCP TOOL CALL (ReAct Loop) ──
                                elif embedded_action["action"] == "mcp_call":
                                    tool_name = embedded_action.get("tool", "")
                                    tool_args = embedded_action.get("args", {})
                                    log.info(f"⚡ Agentic MCP_CALL: {tool_name}({tool_args})")

                                    async def _agentic_tool_loop(_tool, _args, _ws, _llm, _history, _voice_state, _spoken_text):
                                        """ReAct loop: call tool → feed result to LLM → speak."""
                                        try:
                                            mcp_cli = get_mcp_client()
                                            tool_result = await mcp_cli.call_tool(_tool, **_args)
                                            log.info(f"Tool result ({_tool}): {str(tool_result)[:200]}")

                                            # Feed result back to LLM for natural response
                                            if _llm:
                                                # Build context: user asked, tool was called, here's the result
                                                agent_messages = list(_history[-6:])  # Recent context
                                                agent_messages.append({
                                                    "role": "assistant",
                                                    "content": f"[Tool {_tool} returned]: {str(tool_result)[:1500]}"
                                                })
                                                agent_messages.append({
                                                    "role": "user",
                                                    "content": (
                                                        f"The tool '{_tool}' just returned the above result. "
                                                        "Now give a natural, concise voice response (1-2 sentences max, no markdown). "
                                                        "If you need another tool, use [ACTION:MCP_CALL] again."
                                                    )
                                                })

                                                follow_up = await _llm.generate_with_history(
                                                    messages=agent_messages,
                                                    system=(
                                                        f"You are JARVIS. Summarize the tool result for {USER_NAME} in 1-2 natural sentences. "
                                                        "No markdown. No code blocks. Conversational voice. "
                                                        "If you need to call another tool, output [ACTION:MCP_CALL] tool_name|||{{\"args\"}} at the end."
                                                    ),
                                                    temperature=0.3,
                                                    max_tokens=200,
                                                    thinking=False,
                                                )

                                                # Check if the LLM wants to chain another tool call
                                                clean_follow, chained_action = extract_action(follow_up)
                                                if chained_action and chained_action["action"] == "mcp_call":
                                                    # Execute chained tool (max 1 chain to prevent loops)
                                                    log.info(f"⚡ Chained MCP_CALL: {chained_action['tool']}")
                                                    chain_result = await mcp_cli.call_tool(
                                                        chained_action["tool"], **chained_action.get("args", {})
                                                    )
                                                    # Final response after chain
                                                    final_msgs = agent_messages + [
                                                        {"role": "assistant", "content": clean_follow},
                                                        {"role": "assistant", "content": f"[Tool {chained_action['tool']} returned]: {str(chain_result)[:1000]}"},
                                                        {"role": "user", "content": "Summarize the final result in 1 natural sentence. No markdown."},
                                                    ]
                                                    final_resp = await _llm.generate_with_history(
                                                        messages=final_msgs,
                                                        system=f"You are JARVIS. Give a final 1-sentence summary to {USER_NAME}. No markdown.",
                                                        temperature=0.3, max_tokens=100, thinking=False,
                                                    )
                                                    report = final_resp.strip() or clean_follow
                                                else:
                                                    report = clean_follow.strip() if clean_follow.strip() else str(tool_result)[:200]
                                            else:
                                                report = str(tool_result)[:200]

                                            # Send the final voice response
                                            await stream_tts_response(_ws, strip_markdown_for_tts(report), report)
                                            if _voice_state:
                                                _voice_state["last_response"] = report
                                            if _history is not None:
                                                _history.append({"role": "assistant", "content": report})
                                        except Exception as e:
                                            log.error(f"Agentic tool loop failed: {e}")
                                            err_msg = f"Tool error: {e}"
                                            await stream_tts_response(_ws, err_msg, err_msg)

                                    # If we already have spoken text from the LLM, send it first
                                    if not response_text.strip():
                                        response_text = "On it."
                                    # Run the tool loop in background so we can speak immediately
                                    asyncio.create_task(_agentic_tool_loop(
                                        tool_name, tool_args, ws, llm, history, voice_state, response_text
                                    ))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and llm:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, llm
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if llm and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, llm))

                # TTS
                tts = strip_markdown_for_tts(response_text)
                await stream_tts_response(ws, tts, response_text)
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong."
                    await stream_tts_response(ws, fallback)
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent.parent.parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent.parent.parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = ""
    calendar_accounts: str = "auto"

@app.post("/api/restart")
async def api_restart():
    """Restarts the JARVIS server process."""
    log.warning("Restart requested via API")
    import sys
    # Spawn a new process and exit the current one
    os.execv(sys.executable, ['python'] + sys.argv)
    return {"success": True}

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {
        "NVIDIA_API_KEY", 
        "GROQ_API_KEY", 
        "GEMINI_API_KEY", 
        "OPENROUTER_API_KEY", 
        "USER_NAME", 
        "HONORIFIC", 
        "CALENDAR_ACCOUNTS"
    }
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    return {"success": True}

@app.post("/api/settings/test-nvidia")
async def api_test_nvidia(body: KeyTest):
    key = body.key_value or os.getenv("NVIDIA_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "deepseek-ai/deepseek-v4-flash", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
            resp.raise_for_status()
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-tts")
async def api_test_tts(body: KeyTest):
    """Test Edge-TTS connectivity (free, no API key needed)."""
    try:
        audio = await synthesize_speech("Test audio.")
        return {"valid": bool(audio)}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    _, env_dict = _read_env()
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    try: await get_recent_notes(count=1); notes_ok = True
    except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "platform": "windows",
        "version": "2.0.0",
        "tts_engine": "Edge-TTS",
        "tts_voice": "en-GB-RyanNeural",
        "tts_cost": "Free",
        "llm_providers": llm.get_status() if llm else [],
        "llm_provider_count": llm.provider_count if llm else 0,
        "active_provider": next((p["name"] for p in (llm.get_status() if llm else []) if p.get("available")), "None"),
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "nvidia": bool(env_dict.get("NVIDIA_API_KEY", "").strip() and env_dict.get("NVIDIA_API_KEY", "") != "your-nvidia-api-key-here"),
            "groq": bool(env_dict.get("GROQ_API_KEY", "").strip()),
            "gemini": bool(env_dict.get("GEMINI_API_KEY", "").strip()),
            "openrouter": bool(env_dict.get("OPENROUTER_API_KEY", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", ""),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # Open terminal in JARVIS repo for self-improvement (Windows)
    import shutil as _shutil
    build_cmd = f'cd /d "{jarvis_dir}"'
    wt = _shutil.which("wt")
    if wt:
        cmd = [wt, "new-tab", "cmd", "/k", build_cmd]
    else:
        cmd = ["cmd.exe", "/c", "start", "cmd.exe", "/k", build_cmd]
    await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Server Events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    # Automatically scan for apps in the background if the cache is missing or stale
    async def auto_discover_apps():
        try:
            from jarvis.app_discovery import save_app_map
            json_path = Path(__file__).parent.parent / "tools" / "data" / "app_paths.json"
            if not json_path.exists() or (time.time() - json_path.stat().st_mtime > 86400 * 7):
                log.info("Starting background app discovery scan...")
                await asyncio.to_thread(save_app_map)
                log.info("Background app discovery complete.")
        except Exception as e:
            log.warning(f"Background app discovery failed: {e}")
    
    asyncio.create_task(auto_discover_apps())

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "jarvis.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
