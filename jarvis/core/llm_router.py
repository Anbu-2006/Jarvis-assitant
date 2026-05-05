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

        # Ollama — Local inference
        ollama_model = os.getenv("OLLAMA_MODEL", "")
        if ollama_model:
            self._providers.append(ProviderState(name="ollama", priority=0))
            log.info(f"✓ Ollama registered: {ollama_model} (local primary)")

        nvidia_key = os.getenv("NVIDIA_API_KEY", "")
        if nvidia_key and nvidia_key != "your-nvidia-api-key-here":
            self._providers.append(ProviderState(name="nvidia", priority=1))
            log.info("✓ DeepSeek V4 Flash registered via NVIDIA NIM (cloud primary)")

        # Groq — ultra-fast inference (Llama 3.3 70B)
        groq_key = os.getenv("GROQ_API_KEY", "")
        if groq_key and groq_key != "gsk_your_groq_key_here":
            self._providers.append(ProviderState(name="groq", priority=2))
            log.info("✓ Groq registered (Llama 3.3 — fast fallback)")

        # Gemini — conversational fallback
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if gemini_key and gemini_key != "your-gemini-api-key-here":
            self._providers.append(ProviderState(name="gemini", priority=3))
            log.info("✓ Gemini 3 Flash registered (conversational fallback)")

        # OpenRouter — last resort
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        if openrouter_key:
            self._providers.append(ProviderState(name="openrouter", priority=4))
            log.info("✓ OpenRouter registered (last resort)")

        if not self._providers:
            log.error("No LLM providers configured! Set at least NVIDIA_API_KEY or GROQ_API_KEY in .env")

        self._providers.sort(key=lambda p: p.priority)
        self._client = None  # AsyncOpenAI for NVIDIA — lazy init
        self._groq_client = None  # Groq SDK — lazy init
        self._http_client: Optional[httpx.AsyncClient] = None  # For Gemini/OpenRouter

        # Groq API key rotation pool
        self._groq_keys = [
            k for k in [
                os.getenv("GROQ_API_KEY", ""),
                os.getenv("GROQ_API_KEY_2", ""),
                os.getenv("GROQ_API_KEY_3", ""),
            ] if k
        ]
        self._groq_key_index = 0

    def _get_client(self):
        """Lazy-initialize the NVIDIA AsyncOpenAI client."""
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=BASE_URL,
                api_key=os.getenv("NVIDIA_API_KEY", ""),
                timeout=60.0,
                max_retries=2,
            )
        return self._client

    def _get_groq_client(self):
        """Lazy-initialize the Groq client with key rotation."""
        if not self._groq_keys:
            return None
        if self._groq_client is None:
            try:
                from groq import AsyncGroq
                self._groq_client = AsyncGroq(
                    api_key=self._groq_keys[self._groq_key_index],
                    timeout=30.0,
                )
            except ImportError:
                log.warning("groq package not installed. Run: pip install groq")
                return None
        return self._groq_client

    def _rotate_groq_key(self, reason: str = ""):
        """Rotate to the next Groq API key."""
        if len(self._groq_keys) <= 1:
            return False
        self._groq_key_index = (self._groq_key_index + 1) % len(self._groq_keys)
        self._groq_client = None  # Force re-init with new key
        log.info(f"🔄 Rotated Groq to key #{self._groq_key_index + 1} ({reason})")
        return True

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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error = None
        for provider in self._providers:
            if not provider.is_available():
                continue
            try:
                if provider.name == "nvidia":
                    result = await self._stream_and_collect(
                        messages=messages,
                        model=model_override or DEFAULT_MODEL,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        thinking=thinking,
                        reasoning_effort=reasoning_effort,
                    )
                elif provider.name == "ollama":
                    result = await self._ollama_generate(messages, temperature, max_tokens)
                elif provider.name == "groq":
                    result = await self._groq_generate(messages, temperature, max_tokens)
                elif provider.name == "gemini":
                    result = await self._gemini_generate(messages, temperature, max_tokens)
                elif provider.name == "openrouter":
                    result = await self._openrouter_generate(messages, temperature, max_tokens)
                else:
                    continue

                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                self._handle_error(provider, e)
                log.warning(f"[{provider.name}] failed, trying next provider... ({e})")
                continue

        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

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
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        for msg in messages:
            api_messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        last_error = None
        for provider in self._providers:
            if not provider.is_available():
                continue
            try:
                if provider.name == "nvidia":
                    result = await self._stream_and_collect(
                        messages=api_messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        thinking=thinking,
                        reasoning_effort=reasoning_effort,
                    )
                elif provider.name == "ollama":
                    result = await self._ollama_generate(api_messages, temperature, max_tokens)
                elif provider.name == "groq":
                    result = await self._groq_generate(api_messages, temperature, max_tokens)
                elif provider.name == "gemini":
                    result = await self._gemini_generate(api_messages, temperature, max_tokens)
                elif provider.name == "openrouter":
                    result = await self._openrouter_generate(api_messages, temperature, max_tokens)
                else:
                    continue

                provider.mark_success()
                return result
            except Exception as e:
                last_error = e
                self._handle_error(provider, e)
                log.warning(f"[{provider.name}] history gen failed, trying next... ({e})")
                continue

        raise RuntimeError(f"All LLM providers failed for history gen. Last error: {last_error}")

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
    # Fallback Provider Implementations
    # ------------------------------------------------------------------

    async def _ollama_generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Generate using local Ollama instance — turbo-optimized for 4GB VRAM.
        
        Optimizations:
        - Streaming for fast time-to-first-token
        - num_ctx=4096 to prevent VRAM overflow on RTX 3050
        - num_gpu=99 to force ALL layers onto GPU (no CPU offload)
        - keep_alive=300 to keep model hot in VRAM for 5 minutes
        - /no_think tag to disable Qwen3's internal reasoning for speed
        """
        model_name = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        url = "http://localhost:11434/api/chat"

        # Strip any internal thinking from Qwen3 for speed
        optimized_messages = []
        for m in messages:
            content = m.get("content", "")
            if m["role"] == "user" and not content.strip().endswith("/no_think"):
                content = content.rstrip() + " /no_think"
            optimized_messages.append({"role": m["role"], "content": content})

        payload = {
            "model": model_name,
            "messages": optimized_messages,
            "stream": True,
            "keep_alive": "5m",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": 4096,
                "num_gpu": 99,
            }
        }

        log.info(f"[Ollama] Calling {model_name} (GPU-accelerated, streaming)...")
        try:
            collected = []
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            if token:
                                collected.append(token)
                            if data.get("done", False):
                                break
                        except json.JSONDecodeError:
                            continue
            
            reply = "".join(collected)
            # Strip Qwen3 think blocks if any leaked through
            import re
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
            log.info(f"[Ollama] ✓ Responded ({len(reply)} chars)")
            return reply
        except httpx.ConnectError:
            raise RuntimeError(f"Ollama is not running. Start it with: ollama serve")
        except Exception as e:
            raise RuntimeError(f"Ollama local generation failed: {e}")

    async def _groq_generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Generate using Groq (Llama 3.3 70B) with key rotation."""
        GROQ_MODELS = [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ]

        max_key_retries = max(len(self._groq_keys), 1)
        for key_attempt in range(max_key_retries):
            client = self._get_groq_client()
            if not client:
                raise RuntimeError("Groq client not available")

            for model in GROQ_MODELS:
                try:
                    log.info(f"[Groq] Trying {model} (key #{self._groq_key_index + 1})...")
                    response = await client.chat.completions.create(
                        messages=messages,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    reply = response.choices[0].message.content or ""
                    log.info(f"[Groq] ✓ {model} responded ({len(reply)} chars)")
                    return reply.strip()
                except Exception as e:
                    err_msg = str(e).lower()
                    # Model-level errors → try next model
                    if any(kw in err_msg for kw in ["rate_limit", "429", "decommissioned", "not_found", "does not exist"]):
                        log.warning(f"[Groq] {model} unavailable: {e}")
                        continue
                    # Key-level errors → rotate key
                    if any(kw in err_msg for kw in ["quota", "credits", "permission", "blocked", "403"]):
                        if self._rotate_groq_key(str(e)[:60]):
                            break  # Break model loop, retry with new key
                    raise  # Unknown error, propagate

        raise RuntimeError("All Groq models/keys exhausted")

    async def _gemini_generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Generate using Google Gemini 3 Flash."""
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        http = self._get_http_client()

        # Convert messages to Gemini format
        contents = []
        for m in messages:
            if m["role"] == "system":
                continue  # Handled separately
            contents.append({
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m.get("content") or ""}]
            })

        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        # Extract system instruction
        system_msgs = [m for m in messages if m["role"] == "system"]
        if system_msgs:
            payload["systemInstruction"] = {
                "parts": [{"text": system_msgs[0]["content"]}]
            }

        log.info(f"[Gemini] Calling {model_name}...")
        resp = await http.post(url, json=payload, headers={"Content-Type": "application/json"})

        if not resp.is_success:
            error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_msg = error_data.get("error", {}).get("message", resp.text[:200])
            raise RuntimeError(f"Gemini API error ({resp.status_code}): {err_msg}")

        data = resp.json()
        try:
            reply = data["candidates"][0]["content"]["parts"][0]["text"]
            log.info(f"[Gemini] ✓ Responded ({len(reply)} chars)")
            return reply.strip()
        except (KeyError, IndexError):
            raise RuntimeError("Gemini returned empty response")

    async def _openrouter_generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Generate using OpenRouter (free models cascade)."""
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")

        http = self._get_http_client()
        MODELS = [
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "openrouter/auto",
        ]

        for model in MODELS:
            try:
                log.info(f"[OpenRouter] Trying {model}...")
                resp = await http.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://jarvis-assistant.local",
                        "X-Title": "JARVIS AI Assistant",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    timeout=30.0,
                )

                if not resp.is_success:
                    if resp.status_code in (429, 503, 402):
                        log.warning(f"[OpenRouter] {model} rate limited/unavailable")
                        continue
                    raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")

                data = resp.json()
                choice = data.get("choices", [{}])[0]
                reply = choice.get("message", {}).get("content", "")

                if reply:
                    log.info(f"[OpenRouter] ✓ {model} responded ({len(reply)} chars)")
                    return reply.strip()
                continue  # Empty response, try next

            except RuntimeError:
                raise
            except Exception as e:
                log.warning(f"[OpenRouter] {model} error: {e}")
                continue

        raise RuntimeError("All OpenRouter models exhausted")

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
        if self._groq_client:
            await self._groq_client.close()
            self._groq_client = None
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
