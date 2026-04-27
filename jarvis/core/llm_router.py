"""
JARVIS LLM Router — DeepSeek V4 Flash intelligence engine.

Provider:
    1. NVIDIA NIM (DeepSeek V4 Flash — ultra-fast reasoning with thinking)

Uses the OpenAI-compatible SDK with streaming and deep-thinking support.
The router handles retries, rate-limit recovery, and reasoning filtering.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, AsyncGenerator

import httpx

log = logging.getLogger("jarvis.llm_router")

# ---------------------------------------------------------------------------
# Model Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "deepseek-ai/deepseek-v4-flash"
VISION_MODEL = "meta/llama-3.2-90b-vision-instruct"
BASE_URL = "https://integrate.api.nvidia.com/v1"

# ---------------------------------------------------------------------------
# Provider Configuration
# ---------------------------------------------------------------------------

@dataclass
class ProviderState:
    """Track the health/availability of a single LLM provider."""
    name: str
    priority: int
    rate_limited_until: float = 0.0
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0

    def is_available(self) -> bool:
        """Check if this provider is currently usable."""
        return time.time() > self.rate_limited_until

    def mark_rate_limited(self, cooldown_seconds: int = 60):
        """Temporarily disable this provider after a rate limit hit."""
        self.rate_limited_until = time.time() + cooldown_seconds
        self.consecutive_errors += 1
        self.total_errors += 1
        log.warning(
            f"[{self.name}] Rate limited — cooling down for {cooldown_seconds}s "
            f"(errors: {self.consecutive_errors})"
        )

    def mark_success(self):
        """Reset error tracking after a successful request."""
        self.consecutive_errors = 0
        self.total_requests += 1


# ---------------------------------------------------------------------------
# LLM Router
# ---------------------------------------------------------------------------

class LLMRouter:
    """Routes LLM requests through NVIDIA NIM with DeepSeek V4 Flash.

    Features:
        - Streaming responses for fast time-to-first-token
        - Deep thinking/reasoning with configurable effort
        - Automatic retry and rate-limit recovery
        - Vision support via Gemini Flash or NVIDIA Vision models

    Usage:
        router = LLMRouter()
        response = await router.generate("What is Python?", system="You are helpful.")
        data = await router.generate_json("Classify: open terminal", system="...")
    """

    def __init__(self):
        self._providers: list[ProviderState] = []
        nvidia_key = os.getenv("NVIDIA_API_KEY", "")
        if nvidia_key and nvidia_key != "your-nvidia-api-key-here":
            self._providers.append(ProviderState(name="nvidia", priority=1))
            log.info("✓ DeepSeek V4 Flash registered via NVIDIA NIM (primary)")

        if not self._providers:
            log.error("No LLM providers configured! Set NVIDIA_API_KEY in .env")

        self._providers.sort(key=lambda p: p.priority)
        self._client = None  # AsyncOpenAI — lazy init
        self._http_client: Optional[httpx.AsyncClient] = None  # For Gemini vision

    def _get_client(self):
        """Lazy-initialize the AsyncOpenAI client."""
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=BASE_URL,
                api_key=os.getenv("NVIDIA_API_KEY", ""),
                timeout=60.0,
                max_retries=2,
            )
        return self._client

    def _get_http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize httpx client for non-OpenAI APIs (Gemini)."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=60.0,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self._http_client

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    def get_status(self) -> list[dict]:
        """Return the current status of all registered providers."""
        return [
            {
                "name": p.name,
                "priority": p.priority,
                "available": p.is_available(),
                "model": DEFAULT_MODEL,
                "total_requests": p.total_requests,
                "total_errors": p.total_errors,
                "consecutive_errors": p.consecutive_errors,
            }
            for p in self._providers
        ]

    async def _wait_for_provider(self) -> ProviderState:
        """Wait for an available provider, handling rate limits."""
        available = [p for p in self._providers if p.is_available()]
        if available:
            return available[0]

        soonest = min(self._providers, key=lambda p: p.rate_limited_until)
        wait_time = soonest.rate_limited_until - time.time()
        if wait_time > 0:
            log.warning(f"All providers rate-limited. Waiting {wait_time:.0f}s...")
            await asyncio.sleep(min(wait_time, 30))
        return soonest

    def _handle_error(self, provider: ProviderState, error: Exception):
        """Classify an error and apply appropriate cooldown."""
        error_str = str(error).lower()
        if "rate" in error_str or "429" in error_str or "quota" in error_str:
            provider.mark_rate_limited(cooldown_seconds=60)
        elif "503" in error_str or "unavailable" in error_str:
            provider.mark_rate_limited(cooldown_seconds=30)
        else:
            provider.mark_rate_limited(cooldown_seconds=15)
        log.warning(f"[{provider.name}] Failed: {error}")

    # ------------------------------------------------------------------
    # Internal: Streaming collection with thinking filter
    # ------------------------------------------------------------------

    async def _stream_and_collect(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        thinking: bool = True,
        reasoning_effort: str = "low",
    ) -> str:
        """Stream a completion from DeepSeek V4 Flash and collect the result.

        Filters out reasoning/thinking tokens — only returns the final content.
        Streaming internally gives faster time-to-first-token vs blocking.
        """
        client = self._get_client()
        use_model = model or DEFAULT_MODEL

        kwargs = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        # Enable thinking/reasoning for DeepSeek V4 Flash
        if thinking and "deepseek" in use_model.lower():
            kwargs["extra_body"] = {
                "chat_template_kwargs": {
                    "thinking": True,
                    "reasoning_effort": reasoning_effort,
                }
            }

        stream = await client.chat.completions.create(**kwargs)

        full_content = ""
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            # Skip reasoning/thinking tokens — only collect final output
            reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if reasoning:
                continue  # Internal reasoning, don't include in output
            if delta.content is not None:
                full_content += delta.content

        return full_content.strip()

    async def _stream_tokens(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        thinking: bool = True,
        reasoning_effort: str = "low",
    ) -> AsyncGenerator[str, None]:
        """Yield content tokens one by one (filtering out reasoning tokens).

        Use this for real-time streaming to the frontend.
        """
        client = self._get_client()
        use_model = model or DEFAULT_MODEL

        kwargs = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if thinking and "deepseek" in use_model.lower():
            kwargs["extra_body"] = {
                "chat_template_kwargs": {
                    "thinking": True,
                    "reasoning_effort": reasoning_effort,
                }
            }

        stream = await client.chat.completions.create(**kwargs)

        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if reasoning:
                continue
            if delta.content is not None:
                yield delta.content

    # ------------------------------------------------------------------
    # Public generation methods
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str = "",
        model_override: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        thinking: bool = True,
        reasoning_effort: str = "low",
    ) -> str:
        """Generate a text response using DeepSeek V4 Flash.

        Args:
            prompt: The user message / prompt text.
            system: Optional system instruction.
            model_override: Force a specific model.
            temperature: Sampling temperature (0.0–1.0).
            max_tokens: Maximum output tokens.
            thinking: Enable deep thinking (DeepSeek V4 Flash feature).
            reasoning_effort: "low", "medium", or "high".

        Returns:
            The generated text response.
        """
        provider = await self._wait_for_provider()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            result = await self._stream_and_collect(
                messages=messages,
                model=model_override or DEFAULT_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
            provider.mark_success()
            return result
        except Exception as e:
            self._handle_error(provider, e)
            raise RuntimeError(f"LLM generation failed: {e}")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
    ) -> dict:
        """Generate a JSON response. Parses output automatically.

        Uses no-thinking mode for fast classification tasks.
        """
        enhanced_system = (system or "") + "\n\nRespond ONLY with valid JSON. No markdown, no explanation, no code fences."

        raw = await self.generate(
            prompt,
            system=enhanced_system,
            temperature=temperature,
            max_tokens=512,
            thinking=False,  # Fast path for JSON classification
        )
        return self._parse_json(raw)

    async def generate_with_history(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 256,
        thinking: bool = True,
        reasoning_effort: str = "low",
    ) -> str:
        """Generate a response using conversation history.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
            system: Optional system instruction.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
            thinking: Enable deep thinking.
            reasoning_effort: "low", "medium", or "high".

        Returns:
            The generated text response.
        """
        provider = await self._wait_for_provider()

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        for msg in messages:
            api_messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        try:
            result = await self._stream_and_collect(
                messages=api_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
            provider.mark_success()
            return result
        except Exception as e:
            self._handle_error(provider, e)
            raise RuntimeError(f"History generation failed: {e}")

    async def generate_stream(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        thinking: bool = True,
        reasoning_effort: str = "low",
    ) -> AsyncGenerator[str, None]:
        """Stream content tokens for real-time output.

        Yields only final content tokens (reasoning is filtered out).
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        async for token in self._stream_tokens(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        ):
            yield token

    async def generate_vision(
        self,
        prompt: str,
        image_b64: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """Generate a response from an image using a vision model.

        Fast path: Gemini 1.5 Flash if GEMINI_API_KEY is set.
        Fallback: NVIDIA Llama 3.2 90B Vision via OpenAI-compat API.
        """
        # Fast path: Gemini 1.5 Flash (10x faster for screenshots)
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if gemini_key:
            return await self._gemini_generate_vision(
                gemini_key, prompt, image_b64, system, temperature, max_tokens
            )

        # Fallback: NVIDIA Vision model via OpenAI SDK
        provider = await self._wait_for_provider()
        try:
            result = await self._nvidia_vision(
                prompt, image_b64, system, temperature, max_tokens
            )
            provider.mark_success()
            return result
        except Exception as e:
            self._handle_error(provider, e)
            raise RuntimeError(f"Vision generation failed: {e}")

    async def generate_completion(
        self,
        system: str,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """Backward-compatible alias for generate(). Used by research module."""
        return await self.generate(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=True,
            reasoning_effort="medium",
        )

    # ------------------------------------------------------------------
    # Vision implementations
    # ------------------------------------------------------------------

    async def _nvidia_vision(
        self,
        prompt: str,
        image_b64: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call NVIDIA Vision model via OpenAI-compat SDK."""
        client = self._get_client()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        mime_type = "image/jpeg" if image_b64.startswith("/9j/") else "image/png"

        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_b64}"
                    }
                }
            ]
        })

        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()

    async def _gemini_generate_vision(
        self,
        api_key: str,
        prompt: str,
        image_b64: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Google Gemini 1.5 Flash natively for ultra-fast vision."""
        http = self._get_http_client()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"

        mime_type = "image/jpeg" if image_b64.startswith("/9j/") else "image/png"

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": image_b64
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        if system:
            payload["systemInstruction"] = {
                "parts": [{"text": system}]
            }

        headers = {"Content-Type": "application/json"}
        resp = await http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            return ""

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Robustly parse JSON from LLM output (handles markdown fences)."""
        text = raw.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        return json.loads(text)

    async def close(self):
        """Clean up all HTTP resources."""
        if self._client:
            await self._client.close()
            self._client = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# ---------------------------------------------------------------------------
# Module-level instance for backward compatibility
# ---------------------------------------------------------------------------

llm_router = None  # Initialized on first import if API key is set

def get_router() -> LLMRouter:
    """Get or create the global LLMRouter instance."""
    global llm_router
    if llm_router is None:
        llm_router = LLMRouter()
    return llm_router
