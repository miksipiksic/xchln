"""Provider lookup. Adding a provider means adding one entry here."""

from __future__ import annotations

from app.config import DEFAULT_PROVIDER
from app.providers.base import Provider
from app.providers.llm import LlmProvider
from app.providers.mock import MockProvider

_PROVIDERS: dict[str, Provider] = {
    "mock": MockProvider(),
    "llm": LlmProvider(),
}


def get_provider(name: str) -> Provider:
    return _PROVIDERS.get(name) or _PROVIDERS[DEFAULT_PROVIDER]
