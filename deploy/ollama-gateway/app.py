"""Small authenticated OpenAI-compatible gateway for a private Ollama instance."""
from __future__ import annotations

import hmac
import os
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Rukiya Local LLM Gateway", docs_url=None, redoc_url=None)
GATEWAY_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "").strip()
OLLAMA_URL = os.getenv("OLLAMA_INTERNAL_URL", "http://ollama:11434").rstrip("/")
REQUEST_TIMEOUT = max(5.0, float(os.getenv("LOCAL_LLM_TIMEOUT", "60")))


@app.on_event("startup")
async def validate_configuration() -> None:
    if len(GATEWAY_API_KEY) < 32:
        raise RuntimeError("LOCAL_LLM_API_KEY must be at least 32 characters; refusing to start")


def require_key(authorization: str | None) -> None:
    if not GATEWAY_API_KEY:
        raise HTTPException(status_code=503, detail="Gateway is not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, GATEWAY_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "rukiya-local-llm-gateway"}


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)) -> Any:
    require_key(authorization)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f"{OLLAMA_URL}/v1/models")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Ollama is unreachable") from exc
    return JSONResponse(status_code=response.status_code, content=_json_or_error(response))


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    require_key(authorization)
    try:
        content_length = int(request.headers.get("content-length", "0") or "0")
    except ValueError:
        content_length = 0
    if content_length > 1_000_000:
        raise HTTPException(status_code=413, detail="Request body too large")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    payload["stream"] = False

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Ollama request timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Ollama is unreachable") from exc
    return JSONResponse(status_code=response.status_code, content=_json_or_error(response))


def _json_or_error(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"error": {"message": "Invalid response from Ollama"}}
