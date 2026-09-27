"""Web search behind one function, search(). Swap the provider by rewriting this module.

Currently: Tavily Search API via httpx. Results are restricted to domains in sources.yaml.
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel

from app import config, usage

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"

_client: httpx.AsyncClient | None = None
# (path, mtime) -> parsed groups, so sources.yaml is only re-parsed when it changes.
_groups_cache: tuple[Path, float, dict[str, list[str]]] | None = None

# Domains in this sources.yaml group are tertiary sources: usable, but a negative verdict
# shouldn't rest on them alone.
REFERENCE_GROUP = "reference"


class SearchError(Exception):
    pass


class SearchResult(BaseModel):
    title: str
    url: str
    content: str
    domain: str
    published_date: str | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20)
    return _client


def load_groups(path: Path | None = None) -> dict[str, list[str]]:
    """sources.yaml as {group: [domain, ...]}, lowercased and deduplicated within each group."""
    path = path or config.get_settings().sources_file
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {"sources": data}
    groups = {}
    for name, items in data.items():
        items = items if isinstance(items, list) else [items]
        groups[str(name)] = sorted({str(d).strip().lower() for d in items if d and str(d).strip()})
    return groups


def load_domains(path: Path | None = None) -> list[str]:
    """All domains from sources.yaml, merged across groups, deduplicated and sorted."""
    return sorted({d for group in load_groups(path).values() for d in group})


def _get_groups() -> dict[str, list[str]]:
    global _groups_cache
    path = config.get_settings().sources_file
    mtime = path.stat().st_mtime
    if _groups_cache is None or _groups_cache[:2] != (path, mtime):
        groups = load_groups(path)
        _groups_cache = (path, mtime, groups)
        log.info("loaded %d trusted domains from %s", sum(map(len, groups.values())), path)
    return _groups_cache[2]


def get_domains() -> list[str]:
    """All trusted domains; sources.yaml is re-parsed only when its modification time changes."""
    return sorted({d for group in _get_groups().values() for d in group})


def get_reference_domains() -> list[str]:
    return _get_groups().get(REFERENCE_GROUP, [])


def domains_hash(domains: list[str], reference: list[str] = ()) -> str:
    """Stable hash of the source lists (comments and ordering in the YAML don't matter)."""
    payload = json.dumps([sorted(domains), sorted(reference)])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def site_of(domain: str, trusted: list[str]) -> str:
    """The trusted-list entry a host belongs to (en.wikipedia.org -> wikipedia.org).
    Longest match wins, so factcheck.afp.com stays separate from afp.com."""
    matches = [t for t in trusted if domain == t or domain.endswith("." + t)]
    return max(matches, key=len) if matches else domain


def iso_date(value: str | None) -> str | None:
    """Tavily dates come as ISO or RFC 2822 ("Mon, 20 Jul 2026 00:00:00 GMT"). Return YYYY-MM-DD."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def domain_of(url: str) -> str:
    # A fully qualified host can end in "." ("britannica.com."); drop it so site and
    # reference-group matching still work.
    host = (urlparse(url).hostname or "").rstrip(".")
    return host.removeprefix("www.")


_URL_DATE_RES = [
    re.compile(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)"),  # /2026/7/15/
    re.compile(r"/((?:19|20)\d{2})-(\d{2})-(\d{2})(?:/|-|$)"),  # /2026-07-15/
    re.compile(r"/((?:19|20)\d{2})(\d{2})(\d{2})-"),  # /20221017-slug
]


def date_from_url(url: str) -> str | None:
    """Many news URLs embed the publication date. Used when the API gives none."""
    path = urlparse(url).path
    for pattern in _URL_DATE_RES:
        if m := pattern.search(path):
            try:
                return date(*map(int, m.groups())).isoformat()
            except ValueError:
                continue
    return None


async def search(
    query: str,
    *,
    domains: list[str],
    max_results: int = 10,
    topic: Literal["general", "news"] = "general",
) -> list[SearchResult]:
    """topic="news" searches news articles, which come with publication dates."""
    key = config.get_settings().require_key("tavily_api_key")
    usage.current().searches += 1
    try:
        resp = await get_client().post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": query,
                "max_results": max_results,
                "include_domains": domains,
                "search_depth": "basic",
                "topic": topic,
            },
        )
    except httpx.HTTPError as e:
        raise SearchError(f"Tavily request failed: {type(e).__name__}") from None
    if resp.status_code != 200:
        body = resp.text[:200].replace(key, "***")  # in case the API echoes the key
        raise SearchError(f"Tavily returned HTTP {resp.status_code}: {body}")

    results = []
    for r in resp.json().get("results", []):
        url = r.get("url")
        if not url:
            continue
        results.append(
            SearchResult(
                title=r.get("title") or url,
                url=url,
                content=r.get("content") or "",
                domain=domain_of(url),
                published_date=iso_date(r.get("published_date")) or date_from_url(url),
            )
        )
    return results
