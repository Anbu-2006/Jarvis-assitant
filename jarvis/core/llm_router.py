"""
JARVIS LLM Router — NVIDIA Llama-powered intelligence engine.

Provider:
    1. NVIDIA NIM     (primary — ultra-fast Llama 3.3 70B)

The router handles retries and error recovery for the single provider.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("jarvis.llm_router")

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
    """Routes LLM requests across multiple free providers with auto-fallback.

    Usage:
        router = LLMRouter()
        response = await router.generate("What is Python?", system="You are helpful.")
        response = await router.generate_json("Classify: open terminal", system="...")
    """

    def __init__(self):
        self._providers: list[ProviderState] = []
        # Initialize providers based on available API keys
        # Provider 1: NVIDIA NIM (ultra-fast primary)
        nvidia_key = os.getenv("NVIDIA_API_KEY", "")
        if nvidia_key and nvidia_key != "your-nvidia-api-key-here":
            self._providers.append(ProviderState(name="nvidia", priority=1))
            log.info("✓ NVIDIA provider registered (primary)")

        if not self._providers:
            log.error("No LLM providers configured! Set at least NVIDIA_API_KEY in .env")

        # Sort by priority
        self._providers.sort(key=lambda p: p.priority)
        self._http_client: Optional[httpx.AsyncClient] = None

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
                "total_requests": p.total_requests,
                "total_errors": p.total_errors,
                "consecutive_errors": p.consecutive_errors,
            }
            for p in self._providers
        ]

    # ------------------------------------------------------------------
    # Core generation methods
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str = "",
        model_override: str = "",
        temperature: float = 0.7,
    ) -> str:
        """Generate a text response using the best available provider.

        Args:
            prompt: The user message / prompt text.
            system: Optional system instruction.
            model_override: Force a specific model (provider-specific).
            temperature: Sampling temperature (0.0–1.0).

        Returns:
            The generated text response.

        Raises:
            RuntimeError: If all providers are exhausted.
        """
        if not self._http_client:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )

        available = [p for p in self._providers if p.is_available()]
        if not available:
            # All providers rate-limited — wait for the one that recovers soonest
            soonest = min(self._providers, key=lambda p: p.rate_limited_until)
            wait_time = soonest.rate_limited_until - time.time()
            if wait_time > 0:
                log.warning(f"All providers rate-limited. Waiting {wait_time:.0f}s for {soonest.name}...")
                await asyncio.sleep(min(wait_time, 30))
            available = [soonest]

        last_error = None
        for provider in available:
            try:
                result = await self._call_provider(
                    provider, prompt, system, model_override, temperature
                )
                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if "rate" in error_str or "429" in error_str or "quota" in error_str:
                    provider.mark_rate_limited(cooldown_seconds=60)
                elif "503" in error_str or "unavailable" in error_str:
                    provider.mark_rate_limited(cooldown_seconds=30)
                else:
                    provider.mark_rate_limited(cooldown_seconds=15)
                log.warning(f"[{provider.name}] Failed: {e} — trying next provider")

        raise RuntimeError(f"All LLM providers exhausted. Last error: {last_error}")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
    ) -> dict:
        """Generate a JSON response. Parses the output automatically.

        Falls back to text generation + manual parsing if structured output
        is not supported by the fallback provider.
        """
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=60.0)

        available = [p for p in self._providers if p.is_available()]
        if not available:
            soonest = min(self._providers, key=lambda p: p.rate_limited_until)
            wait_time = soonest.rate_limited_until - time.time()
            if wait_time > 0:
                await asyncio.sleep(min(wait_time, 30))
            available = [soonest]

        last_error = None
        for provider in available:
            try:
                result = await self._call_provider_json(
                    provider, prompt, system, temperature
                )
                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if "rate" in error_str or "429" in error_str or "quota" in error_str:
                    provider.mark_rate_limited(cooldown_seconds=60)
                else:
                    provider.mark_rate_limited(cooldown_seconds=15)
                log.warning(f"[{provider.name}] JSON gen failed: {e} — trying next")

        raise RuntimeError(f"All LLM providers exhausted for JSON gen. Last error: {last_error}")

    async def generate_with_history(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 0,
    ) -> str:
        """Generate a response using conversation history.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
            system: Optional system instruction.
            temperature: Sampling temperature.

        Returns:
            The generated text response.
        """
        if not self._http_client:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )

        available = [p for p in self._providers if p.is_available()]
        if not available:
            soonest = min(self._providers, key=lambda p: p.rate_limited_until)
            wait_time = soonest.rate_limited_until - time.time()
            if wait_time > 0:
                await asyncio.sleep(min(wait_time, 30))
            available = [soonest]

        last_error = None
        for provider in available:
            try:
                result = await self._call_provider_history(
                    provider, messages, system, temperature, max_tokens
                )
                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if "rate" in error_str or "429" in error_str or "quota" in error_str:
                    provider.mark_rate_limited(cooldown_seconds=60)
                else:
                    provider.mark_rate_limited(cooldown_seconds=15)
                log.warning(f"[{provider.name}] History gen failed: {e} — trying next")

        raise RuntimeError(f"All providers exhausted for history gen. Last error: {last_error}")

    async def generate_vision(
        self,
        prompt: str,
        image_b64: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """Generate a response using a multimodal vision model.

        Args:
            prompt: Text prompt asking about the image.
            image_b64: Base64-encoded PNG/JPEG image data.
            system: Optional system instruction.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
        """
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=60.0)

        available = [p for p in self._providers if p.is_available()]
        if not available:
            soonest = min(self._providers, key=lambda p: p.rate_limited_until)
            wait_time = soonest.rate_limited_until - time.time()
            if wait_time > 0:
                await asyncio.sleep(min(wait_time, 30))
            available = [soonest]

        last_error = None
        for provider in available:
            try:
                result = await self._call_provider_vision(
                    provider, prompt, image_b64, system, temperature, max_tokens
                )
                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if "rate" in error_str or "429" in error_str or "quota" in error_str:
                    provider.mark_rate_limited(cooldown_seconds=60)
                else:
                    provider.mark_rate_limited(cooldown_seconds=15)
                log.warning(f"[{provider.name}] Vision gen failed: {e} — trying next")

        raise RuntimeError(f"All providers exhausted for vision gen. Last error: {last_error}")

    # ------------------------------------------------------------------
    # Provider-specific call implementations
    # ------------------------------------------------------------------

    async def _call_provider(
        self,
        provider: ProviderState,
        prompt: str,
        system: str,
        model_override: str,
        temperature: float,
    ) -> str:
        """Dispatch a single-prompt request to the correct provider."""
        if provider.name == "nvidia":
            return await self._openai_compat_generate(
                provider.name, prompt, system, model_override, temperature
            )
        else:
            raise ValueError(f"Unknown provider: {provider.name}")

    async def _call_provider_json(
        self,
        provider: ProviderState,
        prompt: str,
        system: str,
        temperature: float,
    ) -> dict:
        """Dispatch a JSON-output request to the correct provider."""
        # OpenAI-compatible providers: request JSON via system prompt
        enhanced_system = (system or "") + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        raw = await self._openai_compat_generate(
            provider.name, prompt, enhanced_system, "", temperature
        )
        return self._parse_json(raw)

    async def _call_provider_history(
        self,
        provider: ProviderState,
        messages: list[dict],
        system: str,
        temperature: float,
        max_tokens: int = 0,
    ) -> str:
        """Dispatch a multi-turn conversation request to the correct provider."""
        return await self._openai_compat_generate_history(
            provider.name, messages, system, temperature, max_tokens
        )

    async def _call_provider_vision(
        self,
        provider: ProviderState,
        prompt: str,
        image_b64: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Dispatch a multimodal vision request to the correct provider."""
        # Fast path: Use Gemini 1.5 Flash if available (10x faster for screenshots)
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if gemini_key:
            return await self._gemini_generate_vision(
                gemini_key, prompt, image_b64, system, temperature, max_tokens
            )
            
        # Fallback to NVIDIA Llama 3.2 Vision
        return await self._openai_compat_generate_vision(
            provider.name, prompt, image_b64, system, temperature, max_tokens
        )
        
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
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        
        mime_type = "image/png"
        if image_b64.startswith("/9j/"):
            mime_type = "image/jpeg"
            
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
        
        resp = await self._http_client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            return ""

    # ------------------------------------------------------------------
    # OpenAI-compatible implementation (NVIDIA)
    # ------------------------------------------------------------------

    _PROVIDER_CONFIG = {
        "nvidia": {
            "base_url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "key_env": "NVIDIA_API_KEY",
            "default_model": "meta/llama-3.3-70b-instruct",
        },
    }

    async def _openai_compat_generate(
        self,
        provider_name: str,
        prompt: str,
        system: str,
        model_override: str,
        temperature: float,
    ) -> str:
        """Call an OpenAI-compatible API (NVIDIA)."""
        cfg = self._PROVIDER_CONFIG[provider_name]
        api_key = os.getenv(cfg["key_env"], "")
        model = model_override or cfg["default_model"]

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }

        resp = await self._http_client.post(cfg["base_url"], json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    async def _openai_compat_generate_history(
        self,
        provider_name: str,
        messages: list[dict],
        system: str,
        temperature: float,
        max_tokens: int = 0,
    ) -> str:
        """Call an OpenAI-compatible API with conversation history."""
        cfg = self._PROVIDER_CONFIG[provider_name]
        api_key = os.getenv(cfg["key_env"], "")

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        for msg in messages:
            api_messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": cfg["default_model"],
            "messages": api_messages,
            "temperature": temperature,
        }
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens

        resp = await self._http_client.post(cfg["base_url"], json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    async def _openai_compat_generate_vision(
        self,
        provider_name: str,
        prompt: str,
        image_b64: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call an OpenAI-compatible API with multimodal vision input."""
        cfg = self._PROVIDER_CONFIG[provider_name]
        api_key = os.getenv(cfg["key_env"], "")
        
        # Use the Llama 3.2 90B Vision Instruct model on NVIDIA NIM
        model = "meta/llama-3.2-90b-vision-instruct"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        
        # Determine mime type from basic signature or default to png
        mime_type = "image/png"
        if image_b64.startswith("/9j/"):
            mime_type = "image/jpeg"

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

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = await self._http_client.post(cfg["base_url"], json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

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
        """Clean up HTTP resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
