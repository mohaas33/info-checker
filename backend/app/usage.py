"""Per-request counters for cost drivers (LLM calls, tokens, searches).

A ContextVar keeps counts separate per request, including across asyncio.gather tasks
(child tasks share the same Usage object because it is mutable).
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class Usage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    searches: int = 0
    factcheck_lookups: int = 0

    def summary(self) -> str:
        return (
            f"llm_calls={self.llm_calls} prompt_tokens={self.prompt_tokens} "
            f"completion_tokens={self.completion_tokens} searches={self.searches} "
            f"factcheck_lookups={self.factcheck_lookups}"
        )


_current: ContextVar[Usage | None] = ContextVar("usage", default=None)


def start() -> Usage:
    usage = Usage()
    _current.set(usage)
    return usage


def current() -> Usage:
    """Return the active Usage, or a throwaway one when called outside a request."""
    return _current.get() or Usage()
