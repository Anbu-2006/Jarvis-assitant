"""
JARVIS MCP Client — Connects the main server to the MCP tool server.

This module spawns the MCP server as a subprocess and provides
async functions to call any MCP tool from the main API.

Usage:
    from jarvis.mcp_client import MCPClient

    client = MCPClient()
    await client.start()
    result = await client.call_tool("get_system_info")
    await client.stop()
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Any

log = logging.getLogger("jarvis.mcp_client")


class MCPClient:
    """Lightweight MCP client that calls tools from the MCP server directly.

    Instead of spawning a subprocess (which adds complexity), we import the
    MCP server's tools directly as Python functions. This is simpler, faster,
    and avoids IPC overhead.
    """

    def __init__(self):
        self._tools: dict[str, callable] = {}
        self._started = False
        self._schema_cache: str | None = None
        self._exit_stack = None

    async def start(self):
        """Load all MCP tools from the server module."""
        if self._started:
            return

        try:
            from jarvis.mcp_server import (
                # System Information
                get_system_time, get_system_info, get_battery_info, get_top_processes,
                # System Control
                open_application, open_url, search_web, control_volume,
                lock_screen, take_screenshot, get_clipboard, set_clipboard,
                # Network
                get_wifi_info, list_open_ports, kill_port, docker_status,
                # Web Intelligence
                get_latest_news, summarize_website, research_topic, search_codebase,
                # Workspace
                setup_workspace, open_news_website,
                # File Management
                create_file, read_file, write_file, delete_file, list_files, find_files,
                # Advanced System
                get_screen_brightness, set_screen_brightness, get_installed_apps,
                get_startup_apps, empty_recycle_bin, shutdown_computer,
                get_disk_usage, get_network_info, ping_host,
                # Development
                git_status, run_script, pip_install,
                # Math & Conversion
                calculate, convert_units,
                # Window Management
                list_open_windows, close_application, send_notification,
                # Utility
                get_weather, set_timer,
            )

            self._tools = {
                # System Information
                "get_system_time": get_system_time,
                "get_system_info": get_system_info,
                "get_battery_info": get_battery_info,
                "get_top_processes": get_top_processes,
                # System Control
                "open_application": open_application,
                "open_url": open_url,
                "search_web": search_web,
                "control_volume": control_volume,
                "lock_screen": lock_screen,
                "take_screenshot": take_screenshot,
                "get_clipboard": get_clipboard,
                "set_clipboard": set_clipboard,
                # Network
                "get_wifi_info": get_wifi_info,
                "list_open_ports": list_open_ports,
                "kill_port": kill_port,
                "docker_status": docker_status,
                # Web Intelligence
                "get_latest_news": get_latest_news,
                "summarize_website": summarize_website,
                "research_topic": research_topic,
                "search_codebase": search_codebase,
                # Workspace
                "setup_workspace": setup_workspace,
                "open_news_website": open_news_website,
                # File Management
                "create_file": create_file,
                "read_file": read_file,
                "write_file": write_file,
                "delete_file": delete_file,
                "list_files": list_files,
                "find_files": find_files,
                # Advanced System
                "get_screen_brightness": get_screen_brightness,
                "set_screen_brightness": set_screen_brightness,
                "get_installed_apps": get_installed_apps,
                "get_startup_apps": get_startup_apps,
                "empty_recycle_bin": empty_recycle_bin,
                "shutdown_computer": shutdown_computer,
                "get_disk_usage": get_disk_usage,
                "get_network_info": get_network_info,
                "ping_host": ping_host,
                # Development
                "git_status": git_status,
                "run_script": run_script,
                "pip_install": pip_install,
                # Math & Conversion
                "calculate": calculate,
                "convert_units": convert_units,
                # Window Management
                "list_open_windows": list_open_windows,
                "close_application": close_application,
                "send_notification": send_notification,
                # Utility
                "get_weather": get_weather,
                "set_timer": set_timer,
            }

            self._started = True
            log.info(f"MCP client loaded {len(self._tools)} tools")

            # Load external MCP servers asynchronously so we don't block startup
            import contextlib
            self._exit_stack = contextlib.AsyncExitStack()
            asyncio.create_task(self._load_external_servers())

        except ImportError as e:
            log.error(f"Failed to import MCP server: {e}")
        except Exception as e:
            log.error(f"MCP client init failed: {e}")

    async def _load_external_servers(self):
        """Dynamically load official open-source MCP servers (e.g., Puppeteer)."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            log.warning("mcp package not installed, skipping external servers")
            return

        # Use Windows-compatible command array for npx
        servers = [
            ("cmd", ["/c", "npx", "-y", "@modelcontextprotocol/server-puppeteer"]),
            ("cmd", ["/c", "npx", "-y", "@modelcontextprotocol/server-memory"]),
            ("cmd", ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "e:\\Antigravity\\Jarvis-assitant"]),
            ("python", ["-m", "mcp_pyautogui_server"]),
        ]

        for cmd, args in servers:
            try:
                params = StdioServerParameters(command=cmd, args=args)
                stdio_transport = await self._exit_stack.enter_async_context(stdio_client(params))
                read, write = stdio_transport
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                
                tools_response = await session.list_tools()
                
                def make_wrapper(sess, name, desc):
                    async def wrapper(**kwargs):
                        res = await sess.call_tool(name, arguments=kwargs)
                        if hasattr(res, "content"):
                            return "\n".join([c.text for c in res.content if getattr(c, "type", "") == "text"])
                        return str(res)
                    wrapper.__name__ = name
                    wrapper.__doc__ = desc or f"External MCP tool: {name}"
                    return wrapper

                for t in tools_response.tools:
                    self._tools[t.name] = make_wrapper(session, t.name, t.description)
                    
                self._schema_cache = None # Invalidate cache to include new tools
                log.info(f"Loaded external MCP server: {args[-1]} with {len(tools_response.tools)} tools")
            except Exception as e:
                log.error(f"Failed to load MCP server {args[-1]}: {e}")

    async def stop(self):
        """Clean up resources."""
        self._tools.clear()
        self._started = False

    @property
    def available_tools(self) -> list[str]:
        """Return list of available tool names."""
        return list(self._tools.keys())

    @property
    def tool_descriptions(self) -> str:
        """Return a formatted string of tool names for injection into system prompts."""
        if not self._tools:
            return "No MCP tools loaded."
        return "Available system tools: " + ", ".join(self._tools.keys())

    def get_tool_schemas(self) -> str:
        """Generate ultra-compact tool schemas for LLM system prompt injection.
        
        Drastically reduced token size by stripping parameter types and defaults.
        """
        if self._schema_cache is not None:
            return self._schema_cache

        import inspect
        if not self._tools:
            return ""
        
        lines = []
        for name, func in self._tools.items():
            sig = inspect.signature(func)
            params = []
            for pname in sig.parameters.keys():
                params.append(pname)
            
            param_str = ", ".join(params)
            doc = (func.__doc__ or "").split("\n")[0].strip()[:60]  # First line, truncated
            lines.append(f"- {name}({param_str}): {doc}")
        
        self._schema_cache = "\n".join(lines)
        return self._schema_cache

    async def call_tool(self, tool_name: str, **kwargs) -> str:
        """Call an MCP tool by name with keyword arguments."""
        if not self._started:
            await self.start()

        func = self._tools.get(tool_name)
        if not func:
            return f"Unknown tool: {tool_name}. Available: {', '.join(self._tools.keys())}"

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(**kwargs)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: func(**kwargs))
            return str(result)
        except Exception as e:
            log.error(f"MCP tool '{tool_name}' failed: {e}")
            return f"Tool error: {str(e)}"

    async def route_action(self, action_type: str, target: str) -> Optional[str]:
        """Route an ACTION tag to the appropriate MCP tool."""
        if not self._started:
            await self.start()

        action_lower = action_type.lower()
        target_lower = target.lower().strip()

        # System info queries
        if action_lower in ("system_info", "sysinfo", "system"):
            return await self.call_tool("get_system_info")
        if action_lower in ("battery", "power"):
            return await self.call_tool("get_battery_info")
        if action_lower in ("processes", "top", "cpu"):
            return await self.call_tool("get_top_processes", count=5)
        if action_lower in ("time", "clock"):
            return await self.call_tool("get_system_time")

        # Clipboard
        if action_lower == "clipboard_get":
            return await self.call_tool("get_clipboard")
        if action_lower == "clipboard_set":
            return await self.call_tool("set_clipboard", text=target)

        # Network
        if action_lower in ("wifi", "network"):
            return await self.call_tool("get_wifi_info")
        if action_lower in ("ports", "listening"):
            return await self.call_tool("list_open_ports")
        if action_lower == "kill_port":
            try:
                return await self.call_tool("kill_port", port=int(target))
            except ValueError:
                return f"Invalid port number: {target}"
        if action_lower == "docker":
            return await self.call_tool("docker_status")
        if action_lower in ("network_info", "ip"):
            return await self.call_tool("get_network_info")
        if action_lower == "ping":
            return await self.call_tool("ping_host", host=target)

        # Workspace
        if action_lower in ("workspace", "setup_workspace"):
            return await self.call_tool("setup_workspace", mode=target)

        # News
        if action_lower in ("news", "headlines"):
            return await self.call_tool("get_latest_news", category=target or "general")

        # Screenshot
        if action_lower == "screenshot":
            return await self.call_tool("take_screenshot")

        # Web intelligence
        if action_lower == "summarize_url":
            return await self.call_tool("summarize_website", url=target)
        if action_lower == "research":
            return await self.call_tool("research_topic", query=target)
        if action_lower == "search_code":
            return await self.call_tool("search_codebase", pattern=target)

        # File Management
        if action_lower == "list_files":
            return await self.call_tool("list_files", directory=target or ".")
        if action_lower == "read_file":
            return await self.call_tool("read_file", filepath=target)
        if action_lower == "find_files":
            return await self.call_tool("find_files", name=target)

        # Advanced System
        if action_lower == "brightness":
            return await self.call_tool("get_screen_brightness")
        if action_lower == "set_brightness":
            try:
                return await self.call_tool("set_screen_brightness", level=int(target))
            except ValueError:
                return "Invalid brightness level."
        if action_lower == "installed_apps":
            return await self.call_tool("get_installed_apps")
        if action_lower == "startup_apps":
            return await self.call_tool("get_startup_apps")
        if action_lower == "recycle_bin":
            return await self.call_tool("empty_recycle_bin")
        if action_lower in ("shutdown", "restart", "sleep"):
            return await self.call_tool("shutdown_computer", action=action_lower)
        if action_lower == "disk_usage":
            return await self.call_tool("get_disk_usage")

        # Development
        if action_lower == "git_status":
            return await self.call_tool("git_status", path=target or ".")
        if action_lower == "run_script":
            return await self.call_tool("run_script", command=target)
        if action_lower == "pip_install":
            return await self.call_tool("pip_install", package=target)

        # Math
        if action_lower in ("calculate", "math"):
            return await self.call_tool("calculate", expression=target)
        if action_lower == "convert":
            parts = target.split()
            if len(parts) >= 3:
                try:
                    return await self.call_tool("convert_units", value=float(parts[0]), from_unit=parts[1], to_unit=parts[-1])
                except ValueError:
                    return "Format: <value> <from_unit> to <to_unit>"

        # Window Management
        if action_lower == "list_windows":
            return await self.call_tool("list_open_windows")
        if action_lower == "close_app":
            return await self.call_tool("close_application", app_name=target)
        if action_lower == "notify":
            return await self.call_tool("send_notification", title="JARVIS", message=target)

        # Weather
        if action_lower == "weather":
            return await self.call_tool("get_weather", city=target or "auto")

        # Timer
        if action_lower == "timer":
            try:
                secs = int(target)
                return await self.call_tool("set_timer", seconds=secs)
            except ValueError:
                return "Specify timer duration in seconds."

        return None


# Module-level singleton
_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    """Get or create the global MCP client singleton."""
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClient()
    return _mcp_client
