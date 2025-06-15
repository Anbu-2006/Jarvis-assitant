"""
JARVIS Project Scanner — scan a directory for project context.

Reads key project files to give JARVIS awareness of what's in a project
before handing it to Copilot or the LLM for reasoning.

Usage:
    from jarvis.tools.project_scanner import scan_project, summarize_project

    context = scan_project("~/Desktop/my-app")
    summary = await summarize_project("~/Desktop/my-app", llm_router)
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.project_scanner")

# Files to read for project context (in priority order)
CONTEXT_FILES = [
    "README.md", "readme.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    ".antigravity_instructions.md",
    "AGENTS.md",
]

# Max characters to read per context file
MAX_FILE_CHARS = 1500

# Dirs to exclude from file listings
EXCLUDED_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".pytest_cache",
}


def scan_project(directory: str, max_depth: int = 2) -> dict:
    """Scan a project directory and return structured context.

    Args:
        directory: Path to the project directory.
        max_depth: How deep to traverse for file listing.

    Returns:
        dict with keys:
            - path: absolute path
            - name: directory name
            - exists: bool
            - stack: detected tech stack (e.g. "Python", "Node.js/React")
            - context_files: dict of {filename: content_excerpt}
            - top_level: list of top-level files/dirs
            - file_count: approx total file count
            - has_git: bool
            - errors: list of encountered errors
    """
    result = {
        "path": "",
        "name": "",
        "exists": False,
        "stack": "Unknown",
        "context_files": {},
        "top_level": [],
        "file_count": 0,
        "has_git": False,
        "errors": [],
    }

    try:
        p = Path(directory).expanduser().resolve()
        result["path"] = str(p)
        result["name"] = p.name

        if not p.exists():
            result["errors"].append(f"Directory not found: {directory}")
            return result

        result["exists"] = True
        result["has_git"] = (p / ".git").exists()

        # Read context files
        for fname in CONTEXT_FILES:
            fpath = p / fname
            if fpath.exists() and fpath.is_file():
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    if len(content) > MAX_FILE_CHARS:
                        content = content[:MAX_FILE_CHARS] + "\n... (truncated)"
                    result["context_files"][fname] = content
                except Exception as e:
                    result["errors"].append(f"Could not read {fname}: {e}")

        # Top-level listing
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
            result["top_level"] = [
                {"name": e.name, "type": "dir" if e.is_dir() else "file"}
                for e in entries
                if e.name not in EXCLUDED_DIRS and not e.name.startswith(".")
            ][:30]
        except Exception as e:
            result["errors"].append(f"Directory listing failed: {e}")

        # Detect tech stack
        result["stack"] = _detect_stack(p, result["context_files"])

        # Approximate file count
        try:
            count = sum(
                1 for _ in p.rglob("*")
                if _.is_file() and not any(excl in _.parts for excl in EXCLUDED_DIRS)
            )
            result["file_count"] = min(count, 9999)
        except Exception:
            pass

    except Exception as e:
        result["errors"].append(f"Scan failed: {e}")
        log.error(f"project_scanner error: {e}")

    return result


def _detect_stack(project_path: Path, context_files: dict) -> str:
    """Detect the technology stack from project files."""
    stacks = []

    if "package.json" in context_files:
        pkg = context_files["package.json"]
        if "react" in pkg.lower():
            stacks.append("React")
        elif "vue" in pkg.lower():
            stacks.append("Vue.js")
        elif "next" in pkg.lower():
            stacks.append("Next.js")
        elif "svelte" in pkg.lower():
            stacks.append("Svelte")
        if "typescript" in pkg.lower() or '"ts"' in pkg.lower():
            stacks.append("TypeScript")
        if not stacks:
            stacks.append("Node.js")

    if "pyproject.toml" in context_files or "requirements.txt" in context_files:
        content = context_files.get("pyproject.toml", "") + context_files.get("requirements.txt", "")
        if "fastapi" in content.lower():
            stacks.append("Python/FastAPI")
        elif "flask" in content.lower():
            stacks.append("Python/Flask")
        elif "django" in content.lower():
            stacks.append("Python/Django")
        elif "streamlit" in content.lower():
            stacks.append("Python/Streamlit")
        else:
            stacks.append("Python")

    if "Cargo.toml" in context_files:
        stacks.append("Rust")

    if "go.mod" in context_files:
        stacks.append("Go")

    if "pom.xml" in context_files or "build.gradle" in context_files:
        stacks.append("Java")

    return " + ".join(stacks) if stacks else "Unknown"


def format_context_for_prompt(scan_result: dict) -> str:
    """Format a scan result as a concise string for LLM injection."""
    if not scan_result["exists"]:
        return f"Project directory not found: {scan_result['path']}"

    lines = [
        f"Project: {scan_result['name']}",
        f"Path: {scan_result['path']}",
        f"Stack: {scan_result['stack']}",
        f"Files: ~{scan_result['file_count']}",
        f"Git: {'Yes' if scan_result['has_git'] else 'No'}",
    ]

    if scan_result["top_level"]:
        items = [
            f"[DIR] {e['name']}" if e["type"] == "dir" else e["name"]
            for e in scan_result["top_level"][:15]
        ]
        lines.append("Top-level: " + ", ".join(items))

    for fname in ["README.md", ".antigravity_instructions.md", "AGENTS.md"]:
        if fname in scan_result["context_files"]:
            excerpt = scan_result["context_files"][fname][:400]
            lines.append(f"\n--- {fname} ---\n{excerpt}")
            break  # Only show the most relevant one

    return "\n".join(lines)


def format_context_for_voice(scan_result: dict) -> str:
    """Format scan result as a brief voice-friendly string."""
    if not scan_result["exists"]:
        return f"I couldn't find that project directory, sir."

    name = scan_result["name"]
    stack = scan_result["stack"]
    count = scan_result["file_count"]

    if stack == "Unknown":
        return f"I found the {name} project with about {count} files."
    return f"I found the {name} project — it's a {stack} project with about {count} files."


async def summarize_project(directory: str, llm_router) -> str:
    """Scan a project and have the LLM give a brief voice summary.

    Args:
        directory: Path to the project directory.
        llm_router: LLMRouter instance.

    Returns:
        1-2 sentence voice-friendly summary string.
    """
    scan = scan_project(directory)

    if not scan["exists"]:
        return format_context_for_voice(scan)

    context = format_context_for_prompt(scan)

    try:
        summary = await llm_router.generate(
            prompt=f"Project context:\n{context}",
            system=(
                "You are JARVIS. Given this project context, give a 1-2 sentence voice summary "
                "of what the project is and what it does. Natural voice, no markdown, no lists. "
                "Example: 'This is a React TypeScript app with about 40 files — looks like a dashboard project.'"
            ),
            temperature=0.3,
            max_tokens=80,
            thinking=False,
        )
        return summary.strip()
    except Exception:
        return format_context_for_voice(scan)
