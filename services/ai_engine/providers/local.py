"""Optional text-only Ollama/llama.cpp compatible loopback adapter.

No weights, subprocesses, downloads, proxy forwarding or tool execution in the bot.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

import aiohttp

from services.ai_engine.models import AIProviderResult
from services.ai_engine.providers.base import AIProvider


class LocalProvider(AIProvider):
    name = "local"

    def __init__(self, config):
        self.config = config
        self.model = config.local_ai_model
        self.url = config.local_ai_url.rstrip('/')
        self.session = None
        self.capacity = asyncio.Semaphore(1)
        try:
            url = urlsplit(self.url)
            self.valid = (url.scheme in {'http', 'https'} and url.hostname in {'127.0.0.1', '::1', 'localhost'}
                          and not (url.username or url.password or url.query or url.fragment))
        except ValueError:
            self.valid = False

    def is_configured(self):
        return bool(self.config.local_ai_enabled and self.model and self.valid)

    async def generate(self, messages, max_tokens=150, temperature=.85, **kwargs):
        started = time.perf_counter()
        if not self.is_configured():
            return AIProviderResult('', self.name, error='Local runtime is disabled or unconfigured')
        try:
            async with self.capacity:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.local_ai_timeout))
                async with self.session.post(self.url + '/chat/completions', json={
                    'model': self.model, 'messages': messages, 'max_tokens': min(2000, max(16, max_tokens)),
                    'temperature': temperature}, allow_redirects=False) as response:
                    if response.status != 200:
                        return AIProviderResult('', self.name, error=f'Local HTTP status {response.status}')
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) > 131072:
                            return AIProviderResult('', self.name, error='Local response too large')
                    data = json.loads(body)
                    message = data['choices'][0]['message']
                    text = message.get('content')
                    if not isinstance(text, str) or not text.strip() or message.get('tool_calls'):
                        return AIProviderResult('', self.name, error='Local runtime returned unusable text')
                    return AIProviderResult(text[:12000], self.name, latency_ms=(time.perf_counter()-started)*1000)
        except Exception as exc:
            return AIProviderResult('', self.name, error=f'Local inference failed: {type(exc).__name__}')

    async def search_grounded(self, query, **kwargs):
        return AIProviderResult('', self.name, error='Local runtime cannot verify live information')

    async def health_check(self):
        # Configuration readiness only; never load a model for a health probe.
        return self.is_configured()

    async def aclose(self):
        if self.session is not None:
            await self.session.close()
            self.session = None
