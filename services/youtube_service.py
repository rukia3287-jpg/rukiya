# services/ai_service.py
import os
import asyncio
import logging
import socket
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_BASE = "https://api.openrouter.ai"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


class AIService:
    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None, session: aiohttp.ClientSession = None):
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE") or DEFAULT_OPENROUTER_BASE).rstrip("/")
        self.api_key = api_key or os.environ.get(OPENROUTER_API_KEY_ENV)
        self._session = session
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY not set; OpenRouter calls will fail")

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def generate_response(self, prompt: str, model: str = "deepseek_r1", max_tokens: int = 300, temperature: float = 0.8) -> Optional[str]:
        """
        Call OpenRouter chat completions with retries and robust DNS error handling.
        Returns string or None.
        """
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # quick debug: show host we will attempt
        try:
            host = self.base_url.split("//")[-1].split("/")[0]
            logger.debug(f"OpenRouter base host: {host}")
        except Exception:
            host = None

        # retries with exponential backoff
        max_attempts = 4
        backoff = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload, headers=headers, timeout=30) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.error(f"OpenRouter API returned HTTP {resp.status}: {text}")
                        return None
                    data = await resp.json()
                    # extract content robustly
                    content = None
                    try:
                        content = data.get("choices", [])[0].get("message", {}).get("content")
                    except Exception:
                        content = None
                    if not content:
                        # fallback patterns
                        content = (data.get("output") or [{}])[0].get("content", [{}])[0].get("text") if data.get("output") else None
                    if content:
                        return content.strip()
                    logger.error("OpenRouter response missing content field")
                    return None

            except aiohttp.ClientConnectorError as e:
                # typical when DNS fails or connection refused
                logger.warning(f"OpenRouter network error (attempt {attempt}): {e}")
                # If underlying socket error present, log errno
                if getattr(e, "__cause__", None):
                    logger.debug(f"Underlying error cause: {e.__cause__}")
            except (socket.gaierror, OSError) as e:
                # socket.gaierror errno -2 -> Name or service not known
                logger.warning(f"OpenRouter socket/DNS error (attempt {attempt}): {e} (errno={getattr(e, 'errno', None)})")
            except asyncio.TimeoutError:
                logger.warning(f"OpenRouter request timed out (attempt {attempt})")
            except Exception as e:
                logger.exception(f"Unexpected error contacting OpenRouter (attempt {attempt}): {e}")

            # If not last attempt, sleep with backoff
            if attempt < max_attempts:
                await asyncio.sleep(backoff)
                backoff *= 2

        # final failure
        logger.error("OpenRouter generate_response failed after retries. Check DNS/network/OPENROUTER_BASE and OPENROUTER_API_KEY.")
        # extra diagnostic: try to resolve host synchronously (helpful in logs)
        if host:
            try:
                ip = socket.gethostbyname(host)
                logger.info(f"Resolved {host} -> {ip}")
            except Exception as e:
                logger.warning(f"Could not resolve {host}: {e}")
        return None
