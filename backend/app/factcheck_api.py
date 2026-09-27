"""Google Fact Check Tools API (claims:search). Optional: returns [] if no key is set."""

import logging

import httpx
from pydantic import BaseModel

from app import config, usage

log = logging.getLogger(__name__)

CLAIMS_SEARCH_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

_client: httpx.AsyncClient | None = None
_warned_no_key = False


class FactCheckError(Exception):
    pass


class FactCheckReview(BaseModel):
    publisher: str
    rating: str
    url: str
    title: str = ""
    claim_text: str = ""  # the claim as the fact-checker phrased it
    review_date: str | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def search_fact_checks(query: str, *, max_claims: int = 5) -> list[FactCheckReview]:
    global _warned_no_key
    key = config.get_settings().get_key("google_factcheck_api_key")
    if not key:
        if not _warned_no_key:
            log.warning("GOOGLE_FACTCHECK_API_KEY not configured; skipping existing fact-checks")
            _warned_no_key = True
        return []

    usage.current().factcheck_lookups += 1
    try:
        # Key goes in a header, not the URL, so it can't leak via logged URLs or errors.
        resp = await get_client().get(
            CLAIMS_SEARCH_URL,
            params={"query": query, "pageSize": max_claims, "languageCode": "en"},
            headers={"X-Goog-Api-Key": key},
        )
    except httpx.HTTPError as e:
        raise FactCheckError(f"Fact Check API request failed: {type(e).__name__}") from None
    if resp.status_code != 200:
        body = resp.text[:200].replace(key, "***")  # in case the API echoes the key
        raise FactCheckError(f"Fact Check API returned HTTP {resp.status_code}: {body}")

    reviews = []
    for claim in resp.json().get("claims", []):
        for review in claim.get("claimReview", []):
            url = review.get("url")
            rating = review.get("textualRating")
            if not url or not rating:
                continue
            publisher = review.get("publisher") or {}
            reviews.append(
                FactCheckReview(
                    publisher=publisher.get("name") or publisher.get("site") or "unknown",
                    rating=rating,
                    url=url,
                    title=review.get("title") or "",
                    claim_text=claim.get("text") or "",
                    review_date=review.get("reviewDate"),
                )
            )
    return reviews
