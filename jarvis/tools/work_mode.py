"""
JARVIS Work Mode — project-focused interaction sessions.

JARVIS can connect to any project directory and maintain context
about what the user is working on. Work involves the LLM router
for code-related queries and file-based delegation for build tasks.

The user works in their preferred IDE (Antigravity).
JARVIS writes .antigravity_instructions.md files for task handoff.
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("jarvis.work_mode")

SESSION_FILE = Path(__file__).parent / "data" / "active_session.json"


class WorkSession:
    """A project-focused session tied to a working directory.

    Tracks which project the user is working on and maintains
    context across messages. Uses the LLM router for AI-powered
    responses instead of spawning external processes.
    """

    def __init__(self):
        self._active = False
        self._working_dir: str | None = None
        self._project_name: str | None = None
        self._message_count = 0
        self._status = "idle"  # idle, working, done

    @property
    def active(self) -> bool:
        return self._active

    @property
    def project_name(self) -> str | None:
        return self._project_name

    @property
    def status(self) -> str:
        return self._status

    async def start(self, working_dir: str, project_name: str = None):
        """Start or switch to a project session."""
        self._working_dir = working_dir
        self._project_name = project_name or Path(working_dir).name
        self._active = True
        self._message_count = 0
        self._status = "idle"
        log.info(f"Work mode started: {self._project_name} ({working_dir})")

    async def send(self, user_text: str) -> str:
        """Process a work-mode message using the LLM router.

        For code-related work, the LLM generates a response using project
        context. For build tasks, instructions are written to a file for
        the IDE to pick up.
        """
        self._status = "working"

        try:
            # Read project context if available
            project_context = self._read_project_context()

            # Use the LLM router (imported at call time to avoid circular imports)
            from jarvis.core.llm_router import LLMRouter
            import os
            router = LLMRouter()

            system = (
                f"You are JARVIS working on the '{self._project_name}' project. "
                f"Working directory: {self._working_dir}\n"
                f"Project context:\n{project_context}\n\n"
                "Provide specific, actionable responses about this project. "
                "If asked to build or modify code, provide the code directly. "
                "Be concise and technical."
            )

            response = await router.generate(user_text, system=system)
            await router.close()

            self._message_count += 1
            self._status = "done"

            log.info(f"Work response for {self._project_name} ({len(response)} chars)")
            return response

        except Exception as e:
            log.error(f"Work mode error: {e}")
            self._status = "error"
            return f"Something went wrong, sir: {str(e)[:100]}"

    def _read_project_context(self) -> str:
        """Read key project files for context."""
        if not self._working_dir:
            return "No project directory set."

        context_parts = []
        work_path = Path(self._working_dir)

        # Check for common project files
        for filename in ["README.md", "package.json", "requirements.txt",
                         "Cargo.toml", "pubspec.yaml", ".antigravity_instructions.md"]:
            filepath = work_path / filename
            if filepath.exists():
                try:
                    content = filepath.read_text(encoding="utf-8")[:1000]
                    context_parts.append(f"--- {filename} ---\n{content}")
                except Exception:
                    pass

        # List top-level files
        try:
            entries = [e.name for e in sorted(work_path.iterdir())
                       if not e.name.startswith(".")][:20]
            context_parts.append(f"Files: {', '.join(entries)}")
        except Exception:
            pass

        return "\n\n".join(context_parts) if context_parts else "Empty project directory."

    async def stop(self):
        """End the work session."""
        project = self._project_name
        self._active = False
        self._working_dir = None
        self._project_name = None
        self._message_count = 0
        self._status = "idle"
        log.info(f"Work mode ended for {project}")

    def _save_session(self):
        """Persist session state so it survives restarts."""
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps({
                "project_name": self._project_name,
                "working_dir": self._working_dir,
                "message_count": self._message_count,
            }))
        except Exception as e:
            log.debug(f"Failed to save session: {e}")

    def _clear_session(self):
        """Remove persisted session."""
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def restore(self) -> bool:
        """Restore session from disk after restart. Returns True if restored."""
        try:
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text())
                self._working_dir = data["working_dir"]
                self._project_name = data["project_name"]
                self._message_count = data.get("message_count", 1)
                self._active = True
                self._status = "idle"
                log.info(f"Restored work session: {self._project_name} ({self._working_dir})")
                return True
        except Exception as e:
            log.debug(f"No session to restore: {e}")
        return False


def is_casual_question(text: str) -> bool:
    """Detect if a message is casual chat vs work-related.

    Casual questions go to the fast LLM path. Work questions use
    project-aware context.
    """
    t = text.lower().strip()

    casual_patterns = [
        "what time", "what's the time", "what day",
        "what's the weather", "weather",
        "how are you", "are you there", "hey jarvis",
        "good morning", "good evening", "good night",
        "thank you", "thanks", "never mind", "nevermind",
        "stop", "cancel", "quit work mode", "exit work mode",
        "go back to chat", "regular mode",
        "how's it going", "what's up",
        "are you still there", "you there", "jarvis",
        "are you doing it", "is it working", "what happened",
        "did you hear me", "hello", "hey",
        "how's that coming", "hows that coming",
        "any update", "status update",
    ]

    # Short greetings/acknowledgments
    if len(t.split()) <= 3 and any(w in t for w in ["ok", "okay", "sure", "yes", "no", "yeah", "nah", "cool"]):
        return True

    return any(p in t for p in casual_patterns)
