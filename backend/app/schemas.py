"""Pydantic models: the public API schema and the shapes we ask the LLM to produce."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ClaimType = Literal["factual", "quote", "opinion", "prediction"]
Verdict = Literal[
    "supported", "contradicted", "misleading", "unverifiable", "outdated", "not_checkable"
]
Confidence = Literal["low", "medium", "high"]

CHECKABLE_TYPES: set[str] = {"factual", "quote"}


# ---------- API request / response ----------


class CheckRequest(BaseModel):
    text: str
    url: str | None = None

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        from app.config import get_settings

        v = v.strip()
        if not v:
            raise ValueError("text must not be empty")
        limit = get_settings().max_text_chars
        if len(v) > limit:
            raise ValueError(f"text must be at most {limit} characters")
        return v


class Source(BaseModel):
    title: str
    url: str
    publisher: str


class ExistingFactCheck(BaseModel):
    publisher: str
    rating: str
    url: str


class ClaimResult(BaseModel):
    original_text: str  # exact span from the input, for later highlighting
    claim: str  # self-contained rewrite
    type: ClaimType
    verdict: Verdict
    explanation: str
    confidence: Confidence
    sources: list[Source] = []
    existing_fact_checks: list[ExistingFactCheck] = []


class CheckResponse(BaseModel):
    claims: list[ClaimResult]
    checked_at: datetime


# ---------- LLM output models ----------


class ExtractedClaim(BaseModel):
    original_text: str = Field(description="Exact span copied verbatim from the input text")
    claim: str = Field(description="Self-contained rewrite: pronouns resolved, who/when/where added")
    type: ClaimType
    search_queries: list[str] = Field(
        default=[], description="1-2 concise web search queries (only for factual/quote claims)"
    )
    time_sensitive: bool = Field(
        default=False,
        description="True if the claim's truth can change over time: recent events, current "
        "figures or office holders, or 'new/latest/now/currently' wording",
    )


class ExtractionOutput(BaseModel):
    claims: list[ExtractedClaim]


class VerificationOutput(BaseModel):
    verdict: Literal["supported", "contradicted", "misleading", "unverifiable", "outdated"]
    explanation: str = Field(description="1-3 plain-language sentences, citing evidence like [1]")
    confidence: Confidence
    cited_evidence: list[int] = Field(
        default=[], description="Numbers of the evidence items the verdict relies on"
    )
