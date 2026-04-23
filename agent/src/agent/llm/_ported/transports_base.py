from __future__ import annotations

# Ported from Hermes-Agent/agent/transports/base.py — adapted for agent/

from abc import ABC, abstractmethod
from typing import Any

from agent.llm._ported.transports_types import NormalizedTransportResponse


class ProviderTransport(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @abstractmethod
    def convert_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        ...

    @abstractmethod
    def convert_tools(self, tools: list[dict[str, Any]]) -> Any:
        ...

    @abstractmethod
    def normalize_response(
        self,
        response: Any,
        *,
        model: str,
    ) -> NormalizedTransportResponse:
        ...
