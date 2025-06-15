"""
JARVIS Web Search — Real-time web search via Brave Search API.

Gives JARVIS the ability to answer questions about current events,
weather, news, and anything else that requires live data — without
opening a browser.

Usage:
    results = await brave_search("weather in Chennai")
    results = await brave_search("latest news on AI")
"""

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("jarvis.search")

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


async def brave_search(
    query: str,
    count: int = 5,
    freshness: str = "",
) -> Optional[str]:
    """Search the web via Brave Search API.

    Args:
        query: The search query.
        count: Number of results to return (1-10).
        freshness: Filter by freshness — "pd" (past day), "pw" (past week), "pm" (past month).

    Returns:
        A formatted text summary of search results, or None if no API key.
    """
    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        log.warning("BRAVE_API_KEY not set — web search unavailable")
        return None

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    params = {
        "q": query,
        "count": str(min(count, 10)),
    }
    if freshness:
        params["freshness"] = freshness

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BRAVE_API_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        # Extract web results
        web_results = data.get("web", {}).get("results", [])
        if not web_results:
            return f"No results found for '{query}'."

        # Format results into a concise text block
        lines = [f"Web search results for: {query}\n"]
        for i, result in enumerate(web_results[:count], 1):
            title = result.get("title", "")
            description = result.get("description", "")
            url = result.get("url", "")
            lines.append(f"{i}. {title}")
            if description:
                lines.append(f"   {description}")
            lines.append(f"   Source: {url}")
            lines.append("")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        log.error(f"Brave Search API error: {e.response.status_code} — {e.response.text[:200]}")
        return None
    except Exception as e:
        log.error(f"Brave Search failed: {e}")
        return None


async def quick_answer(query: str) -> Optional[str]:
    """Get a quick answer snippet from Brave Search.

    Tries to extract a direct answer (like weather, definitions, etc.)
    before falling back to web results.
    """
    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return None

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    params = {
        "q": query,
        "count": "3",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BRAVE_API_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        # Check for FAQ answers
        faq = data.get("faq", {}).get("results", [])
        if faq:
            answer = faq[0].get("answer", "")
            if answer:
                return answer

        # Check for infobox (e.g., weather, definitions)
        infobox = data.get("infobox", {})
        if infobox:
            desc = infobox.get("long_desc") or infobox.get("description", "")
            if desc:
                return desc

        # Fallback to first web result description
        web_results = data.get("web", {}).get("results", [])
        if web_results:
            return web_results[0].get("description", "")

        return None

    except Exception as e:
        log.warning(f"Quick answer failed: {e}")
        return None
