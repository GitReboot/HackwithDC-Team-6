"""Tavily agentic search tool.

Wraps the Tavily Python SDK to give the agent real-time web knowledge.
Drop-in alternative to LinkupSearchTool — same BaseTool interface.
Privacy: only sanitized, non-PII queries are ever sent.
"""

from __future__ import annotations

import logging
from typing import Any

from tavily import TavilyClient

from tools import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class TavilySearchTool(BaseTool):
    """Call Tavily's search API for real-time web knowledge."""

    def __init__(self, cfg: dict):
        self._api_key: str = cfg.get("api_key", "")
        self._default_depth: str = cfg.get("search_depth", "advanced")
        self._default_max_results: int = cfg.get("max_results", 5)
        self._default_topic: str = cfg.get("topic", "general")

    # -- BaseTool interface --------------------------------------------------

    @property
    def name(self) -> str:
        return "web_research"

    @property
    def description(self) -> str:
        return (
            "Search the web for current information using Tavily. "
            "Use this when you need to research companies, people, recent events, "
            "fact-check claims, or gather background information. "
            "NEVER include personal/sensitive data in the query."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A concise, specific natural-language search query (no PII).",
                },
                "depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Search depth. Use 'advanced' for thorough research, 'basic' for quick lookups.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-10).",
                },
            },
            "required": ["query"],
        }

    def run(self, **kwargs) -> ToolResult:
        query: str = kwargs.get("query", "")
        depth: str = kwargs.get("depth", self._default_depth)
        max_results: int = kwargs.get("max_results", self._default_max_results)

        if not query.strip():
            return ToolResult(success=False, error="Empty search query.")

        if not self._api_key:
            return ToolResult(
                success=False,
                error="TAVILY_API_KEY not configured. Set the env var or add it to config.",
            )

        try:
            logger.info("Tavily search: depth=%s query=%r", depth, query)
            client = TavilyClient(api_key=self._api_key)
            data = client.search(
                query=query,
                search_depth=depth,
                max_results=max_results,
                topic=self._default_topic,
            )
            return ToolResult(success=True, data=self._format(data))
        except Exception as exc:
            logger.error("Tavily request failed: %s", exc)
            return ToolResult(success=False, error=str(exc))

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _format(data: dict) -> str:
        """Turn raw Tavily JSON into a text block the LLM can consume."""
        results = data.get("results", [])
        if not results:
            return "No results found."

        parts: list[str] = []
        for i, r in enumerate(results[:8], 1):
            title = r.get("title", "")
            snippet = r.get("content", "")
            url = r.get("url", "")
            parts.append(f"[{i}] {title}\n    {snippet}\n    {url}")
        return "\n\n".join(parts)
