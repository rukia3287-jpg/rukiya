"""Redact configured credentials, including exception tracebacks, at output time."""
from __future__ import annotations
import json
import logging
import os


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, fmt=None, *, secrets=None):
        super().__init__(fmt)
        names = ('DISCORD_TOKEN', 'OPENROUTER_API_KEY', 'GEMINI_API_KEY', 'TOKEN_JSON', 'CLIENT_SECRET_JSON')
        values = secrets if secrets is not None else [os.getenv(name, '') for name in names]
        credentials = {value for value in values if isinstance(value, str) and value}
        def collect_fields(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() in {'access_token', 'refresh_token', 'client_secret', 'private_key', 'api_key', 'token'} and isinstance(child, str) and child:
                        credentials.add(child)
                    else:
                        collect_fields(child)
            elif isinstance(value, list):
                for child in value:
                    collect_fields(child)
        for value in list(credentials):
            try:
                collect_fields(json.loads(value))
            except (ValueError, TypeError):
                pass
        self.secrets = sorted(credentials, key=len, reverse=True)

    def format(self, record):
        rendered = super().format(record)
        for secret in self.secrets:
            rendered = rendered.replace(secret, '[REDACTED]')
        return rendered
