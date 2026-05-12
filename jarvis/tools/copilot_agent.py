"""
JARVIS Copilot Agent — GitHub Copilot CLI subprocess bridge.

Gives JARVIS the ability to dispatch fully autonomous coding tasks
to GitHub Copilot CLI (`gh copilot`), which acts as an AI coding agent
that can write code, debug projects, run terminals, and build full apps.

Usage:
    from jarvis.tools.copilot_agent import CopilotAgent, write_agents_md
    
    agent = CopilotAgent()
    async for line in agent.run_task("Build a FastAPI todo app", working_dir="~/Desktop/todo-app"):
        print(line)
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

log = logging.getLogger("jarvis.copilot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default max lines of output to buffer for LLM summarization
MAX_SUMMARY_BUFFER = 40

# Pattern that signals the app is running (Copilot follows AGENTS.md instruction)
RUNNING_AT_PATTERN = "RUNNING_AT="

# How often (in output lines) to yield a progress event
PROGRESS_INTERVAL = 15


# ---------------------------------------------------------------------------
# AGENTS.md writer
# ---------------------------------------------------------------------------

def write_agents_md(project_dir: str, task: str, stack: str = "", mode: str = "build") -> Path:
    """Write an AGENTS.md file that GitHub Copilot CLI natively reads for instructions.

    This is the Copilot equivalent of the existing .antigravity_instructions.md system.
    Copilot reads AGENTS.md automatically before executing any task in the directory.

    Args:
        project_dir: Absolute path to the project directory.
        task: The full task description from the user.
        stack: Optional tech stack hint (e.g. "React + Vite + TypeScript").
        mode: "build" for new projects, "debug" for existing projects.

    Returns:
        Path to the written AGENTS.md file.
    """
    if mode == "debug":
        instructions = (
            "- INVESTIGATE and FIX bugs. Do not ask clarifying questions.\n"
            "- Read the existing codebase first, understand the structure.\n"
            "- Run tests if they exist. Fix failures.\n"
            "- Do NOT delete or rewrite entire files unless absolutely necessary.\n"
            "- After fixing, verify everything runs without errors.\n"
            "- Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT "
            "(the actual port) or RUNNING_AT=FIXED if no server is involved."
        )
    else:
        instructions = (
            "- BUILD THIS NOW. Do not ask clarifying questions.\n"
            f"- Tech stack: {stack or 'Use the most appropriate stack for the task.'}\n"
            "- Write complete, working code files — not plans, not specs, not outlines.\n"
            "- If it is a web app: make it look polished and professional. Modern UI, clean layout, "
            "responsive design.\n"
            "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
            "- Ensure it runs with a single command (npm run dev, python main.py, etc.).\n"
            "- After building, start the dev server or run the app to verify it loads without errors.\n"
            "- IMPORTANT: Your LAST line of output MUST be exactly: "
            "RUNNING_AT=http://localhost:PORT (the actual port the server is using).\n"
            "- If the task is not a server, end with: RUNNING_AT=COMPLETE"
        )

    content = f"""# JARVIS Task Instructions

## Task
{task}

## Rules
{instructions}

## Quality Standards
- No hardcoded secrets or placeholder credentials
- Handle errors gracefully with clear error messages
- Code must be clean, readable, and well-commented
- All imports must be valid and installed packages
"""

    agents_path = Path(project_dir) / "AGENTS.md"
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    agents_path.write_text(content, encoding="utf-8")
    log.info(f"AGENTS.md written to {agents_path}")
    return agents_path


# ---------------------------------------------------------------------------
# Core Copilot Agent class
# ---------------------------------------------------------------------------

class CopilotAgent:
    """Wraps GitHub Copilot CLI (`gh copilot`) as an async streaming subprocess.

    GitHub Copilot CLI is a terminal-native AI coding agent that can:
    - Write and edit code across multiple files
    - Run shell commands and interpret output
    - Debug projects by exploring their structure
    - Build complete applications from scratch
    - Install dependencies, start servers, verify functionality

    The agent runs with --yolo (full permissions) in autopilot mode, making it
    fully autonomous — no approval prompts interrupt the workflow.
    """

    def __init__(self):
        # Verify gh copilot is available
        self._gh_path = shutil.which("gh")
        if not self._gh_path:
            log.warning("gh CLI not found in PATH. Copilot agent will not be available.")
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}

    @property
    def available(self) -> bool:
        """True if gh CLI is installed and accessible."""
        return self._gh_path is not None

    async def run_task(
        self,
        task: str,
        working_dir: str,
        mode: str = "autopilot",
        model: Optional[str] = None,
        max_continues: int = 20,
        task_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Run a Copilot task and stream output lines.

        Args:
            task: Natural language description of the coding task.
            working_dir: Directory to run the task in (project root).
            mode: "autopilot" (fully autonomous) or "plan" (shows plan first).
            model: Optional model override (e.g. "claude-sonnet-4-5", "gpt-4o").
            max_continues: Max autopilot continuation messages (default 20).
            task_id: Optional ID to track/cancel this task.

        Yields:
            Lines of output from Copilot CLI, including a special
            "__PROGRESS__" prefix for progress events and "__DONE__" on completion.
        """
        if not self.available:
            yield "__ERROR__ gh CLI not installed. Cannot run Copilot agent."
            return

        working_path = Path(working_dir).expanduser().resolve()
        working_path.mkdir(parents=True, exist_ok=True)

        # Build the command
        # Note: flags after `--` are passed directly to Copilot CLI, not gh
        cmd = [
            self._gh_path, "copilot", "--",
            "--yolo",                                        # all permissions: tools, paths, URLs
            "--mode", mode,                                  # autopilot = fully autonomous
            "--max-autopilot-continues", str(max_continues),
            "-s",                                            # silent: only agent output, no stats
            "--output-format", "text",
            "--no-ask-user",                                 # never pause to ask user questions
            "-p", task,                                      # execute this prompt
        ]

        if model:
            cmd.extend(["--model", model])

        log.info(f"Copilot task starting in {working_path}: {task[:80]}")
        log.info(f"Command: {' '.join(cmd[:6])} ... -p '{task[:40]}'")

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(working_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )

            if task_id:
                self._active_procs[task_id] = proc

            line_count = 0
            output_buffer = []
            running_at = None

            # Stream stdout
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                if not line:
                    continue

                line_count += 1
                output_buffer.append(line)

                # Keep buffer bounded
                if len(output_buffer) > MAX_SUMMARY_BUFFER * 2:
                    output_buffer = output_buffer[-MAX_SUMMARY_BUFFER:]

                # Detect completion signal
                if RUNNING_AT_PATTERN in line:
                    running_at = line.strip()
                    yield f"__RUNNING_AT__ {running_at}"

                # Emit progress marker every N lines
                if line_count % PROGRESS_INTERVAL == 0:
                    yield f"__PROGRESS__ {line_count} lines processed"

                yield line

            # Drain stderr (usually Copilot internal logs)
            stderr_data = await proc.stderr.read()
            if stderr_data:
                stderr_text = stderr_data.decode(errors="replace").strip()
                if stderr_text and "warn" not in stderr_text.lower():
                    log.debug(f"Copilot stderr: {stderr_text[:200]}")

            await proc.wait()
            exit_code = proc.returncode

            if exit_code == 0:
                yield f"__DONE__ exit_code=0 lines={line_count}"
                if running_at:
                    yield f"__SUMMARY__ Task complete. {running_at}"
                else:
                    yield f"__SUMMARY__ Task complete after {line_count} output lines."
            else:
                yield f"__ERROR__ Copilot exited with code {exit_code}"

        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
            yield "__ERROR__ Task was cancelled."
            raise
        except FileNotFoundError:
            yield "__ERROR__ gh CLI not found. Make sure it's installed and in PATH."
        except Exception as e:
            log.error(f"Copilot task failed: {e}", exc_info=True)
            yield f"__ERROR__ {str(e)}"
        finally:
            if task_id and task_id in self._active_procs:
                del self._active_procs[task_id]

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running Copilot task by ID."""
        proc = self._active_procs.get(task_id)
        if not proc or proc.returncode is not None:
            return False
        try:
            proc.terminate()
            await asyncio.sleep(0.5)
            if proc.returncode is None:
                proc.kill()
            log.info(f"Cancelled Copilot task: {task_id}")
            return True
        except Exception as e:
            log.error(f"Failed to cancel task {task_id}: {e}")
            return False

    def get_active_tasks(self) -> list[str]:
        """Return list of currently running task IDs."""
        return [
            tid for tid, proc in self._active_procs.items()
            if proc.returncode is None
        ]

    async def run_task_collected(
        self,
        task: str,
        working_dir: str,
        mode: str = "autopilot",
        model: Optional[str] = None,
    ) -> dict:
        """Run a task and collect all output (non-streaming). Returns dict with summary."""
        lines = []
        running_at = None
        error = None

        async for line in self.run_task(task, working_dir, mode=mode, model=model):
            lines.append(line)
            if line.startswith("__RUNNING_AT__"):
                running_at = line.replace("__RUNNING_AT__", "").strip()
            elif line.startswith("__ERROR__"):
                error = line.replace("__ERROR__", "").strip()

        return {
            "lines": [l for l in lines if not l.startswith("__")],
            "running_at": running_at,
            "error": error,
            "success": error is None,
            "total_lines": len(lines),
        }


# ---------------------------------------------------------------------------
# Convenience function for quick dispatch (used by main.py)
# ---------------------------------------------------------------------------

async def dispatch_copilot_task(
    task: str,
    working_dir: str,
    ws,
    llm,
    history: list,
    voice_state: dict,
    task_id: Optional[str] = None,
    stream_tts_fn=None,
    model: Optional[str] = None,
) -> None:
    """Full async pipeline: run Copilot task + narrate progress via TTS.

    This is the main integration point called from main.py's voice_handler.
    It runs in the background (asyncio.create_task) so JARVIS can speak
    immediately and then narrate progress as Copilot works.

    Args:
        task: The coding task description.
        working_dir: Project directory.
        ws: WebSocket connection to stream audio to.
        llm: LLMRouter instance for summarizing output.
        history: Conversation history list.
        voice_state: Mutable state dict for last response tracking.
        task_id: Optional task registry ID.
        stream_tts_fn: The stream_tts_response coroutine from main.py.
        model: Optional Copilot model override.
    """
    from jarvis.tools.copilot_agent import CopilotAgent, write_agents_md

    agent = CopilotAgent()
    if not agent.available:
        msg = "GitHub Copilot CLI is not installed. Cannot run the coding task, sir."
        if stream_tts_fn:
            await stream_tts_fn(ws, msg, msg)
        return

    output_lines = []
    running_at = None
    progress_narrated_at = 0
    start_time = time.time()

    try:
        async for line in agent.run_task(
            task=task,
            working_dir=working_dir,
            mode="autopilot",
            model=model,
            task_id=task_id,
        ):
            if line.startswith("__RUNNING_AT__"):
                running_at = line.replace("__RUNNING_AT__", "").strip()
                continue
            elif line.startswith("__ERROR__"):
                error_msg = line.replace("__ERROR__", "").strip()
                log.error(f"Copilot error: {error_msg}")
                report = f"There was an issue with the build, sir: {error_msg[:120]}"
                if stream_tts_fn:
                    await stream_tts_fn(ws, report, report)
                if history is not None:
                    history.append({"role": "assistant", "content": f"[Copilot error]: {report}"})
                return
            elif line.startswith("__DONE__") or line.startswith("__SUMMARY__"):
                continue
            elif line.startswith("__PROGRESS__"):
                # Narrate progress every ~45 seconds
                elapsed = time.time() - start_time
                if elapsed - progress_narrated_at > 45 and llm and stream_tts_fn:
                    progress_narrated_at = elapsed
                    recent = "\n".join(output_lines[-8:])
                    try:
                        narration = await llm.generate(
                            prompt=f"Recent Copilot output:\n{recent}",
                            system=(
                                "You are JARVIS giving a brief status update while GitHub Copilot "
                                "builds something. Summarize what's happening in 1 sentence. "
                                "Natural voice, no markdown, no technical jargon. "
                                "Example: 'Still working — looks like it is setting up the database now.'"
                            ),
                            temperature=0.3,
                            max_tokens=60,
                            thinking=False,
                        )
                        if narration.strip():
                            await stream_tts_fn(ws, narration.strip(), narration.strip())
                    except Exception as e:
                        log.debug(f"Progress narration failed: {e}")
                continue

            output_lines.append(line)
            # Bound buffer
            if len(output_lines) > 60:
                output_lines = output_lines[-40:]

        # ── Task complete ──────────────────────────────────────────────────
        elapsed_total = int(time.time() - start_time)
        final_output = "\n".join(output_lines[-MAX_SUMMARY_BUFFER:])

        # Build final spoken summary
        if llm and final_output.strip():
            try:
                summary = await llm.generate(
                    prompt=(
                        f"Task: {task}\n\n"
                        f"Copilot output (last section):\n{final_output}\n\n"
                        f"{'App is running at: ' + running_at if running_at else ''}\n"
                        f"Time taken: {elapsed_total}s"
                    ),
                    system=(
                        "You are JARVIS reporting that a coding task just completed. "
                        "Give a concise, natural 1-2 sentence spoken summary. "
                        "No markdown. No bullet points. No technical file names. "
                        "If there's a running URL, mention it naturally. "
                        "Example: 'Done. I've built the budget tracker app — it's running at localhost 5173.'"
                    ),
                    temperature=0.3,
                    max_tokens=100,
                    thinking=False,
                )
                final_report = summary.strip()
            except Exception:
                if running_at:
                    final_report = f"Done, sir. The build is complete. {running_at.replace('RUNNING_AT=', 'Running at ')}"
                else:
                    final_report = f"Done, sir. The task completed in {elapsed_total} seconds."
        else:
            if running_at:
                final_report = f"Done. {running_at.replace('RUNNING_AT=', 'Running at ')}"
            else:
                final_report = "The task is complete, sir."

        if stream_tts_fn:
            await stream_tts_fn(ws, final_report, final_report)

        if voice_state is not None:
            voice_state["last_response"] = final_report
        if history is not None:
            history.append({"role": "assistant", "content": f"[Copilot result]: {final_report}"})

    except asyncio.CancelledError:
        msg = "Task was cancelled, sir."
        if stream_tts_fn:
            await stream_tts_fn(ws, msg, msg)
    except Exception as e:
        log.error(f"dispatch_copilot_task error: {e}", exc_info=True)
        err = f"Something went wrong with the build, sir: {str(e)[:80]}"
        if stream_tts_fn:
            await stream_tts_fn(ws, err, err)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_copilot_agent: Optional[CopilotAgent] = None


def get_copilot_agent() -> CopilotAgent:
    """Get or create the global CopilotAgent singleton."""
    global _copilot_agent
    if _copilot_agent is None:
        _copilot_agent = CopilotAgent()
    return _copilot_agent
