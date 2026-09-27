"""The fact-checking pipeline: extract claims -> gather evidence -> verify -> validate citations."""

import asyncio
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import zip_longest
from urllib.parse import urlsplit, urlunsplit

from app import cache, config, factcheck_api, llm, prompts, search, usage
from app.schemas import (
    CHECKABLE_TYPES,
    CheckResponse,
    ClaimResult,
    ExistingFactCheck,
    ExtractedClaim,
    ExtractionOutput,
    Source,
    VerificationOutput,
)

log = logging.getLogger(__name__)

SNIPPET_CHARS = 600
EVIDENCE_BUDGET_CHARS = 8000
RESULTS_PER_QUERY = 10  # Tavily "basic" costs the same for 5 or 10 results
MAX_PER_SITE = 2  # keep evidence diverse: at most this many articles per site
MAX_QUERIES_PER_CLAIM = 3  # up to 2 English + 1 in the text's own language

NOT_CHECKABLE_NOTE = {
    "opinion": "This is an opinion, so it can't be confirmed or refuted with evidence.",
    "prediction": "This is a prediction about the future, so it can't be checked yet.",
}


class ExtractionFailed(Exception):
    pass


# ---------- evidence ----------


@dataclass
class EvidenceItem:
    n: int
    title: str
    publisher: str
    url: str
    snippet: str
    published: str | None
    fact_check: factcheck_api.FactCheckReview | None = None
    reference: bool = False  # from the "reference" group in sources.yaml

    def render(self) -> str:
        kind = ("Existing fact-check" if self.fact_check
                else "Reference site" if self.reference else "Article")
        return (
            f"[{self.n}] {self.title}\n"
            f"    {kind} | Publisher: {self.publisher} | Published: {self.published or 'unknown'}"
            f" | URL: {self.url}\n"
            f"    {self.snippet}"
        )


def _url_key(url: str) -> str:
    """Normalize a URL for deduplication: lowercase host, no www, fragment, or trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit(("", host, parts.path.rstrip("/"), parts.query, ""))


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


def _interleave(groups: list[list]) -> list:
    """Round-robin merge, so every query (including the news query) gets space in the budget."""
    return [x for row in zip_longest(*groups) for x in row if x is not None]


def build_evidence(
    reviews: list[factcheck_api.FactCheckReview],
    result_groups: list[list[search.SearchResult]],
    trusted: list[str] = (),
    reference: list[str] = (),
) -> list[EvidenceItem]:
    """Numbered, deduplicated evidence list within a size budget: fact-checks first, then
    articles from all queries interleaved, with at most MAX_PER_SITE articles per site."""
    items: list[EvidenceItem] = []
    seen: dict[str, EvidenceItem] = {}
    per_site: Counter[str] = Counter()
    used = 0

    candidates = [
        (r.url, r.title or f"Fact-check by {r.publisher}", r.publisher,
         f'Fact-check of the claim "{r.claim_text}". Rating: "{r.rating}".',
         search.iso_date(r.review_date), r)
        for r in reviews
    ] + [
        (r.url, r.title, search.site_of(r.domain, list(trusted)), r.content, r.published_date, None)
        for r in _interleave(result_groups)
    ]
    for url, title, site, snippet, published, review in candidates:
        snippet = _truncate(snippet, SNIPPET_CHARS)
        # Same page under several URLs (common on fact-check sites) counts once.
        keys = {_url_key(url), f"{site}|{snippet[:200].lower()}"}
        if dup := next((seen[k] for k in keys if k in seen), None):
            # The news query often returns a page the general query already found, but with
            # a publication date. Keep the date.
            dup.published = dup.published or published
            continue
        if not review and per_site[site] >= MAX_PER_SITE:
            continue
        if used + len(snippet) > EVIDENCE_BUDGET_CHARS:
            break
        item = EvidenceItem(len(items) + 1, _truncate(title, 200), site, url, snippet,
                            published, review, reference=not review and site in reference)
        seen.update(dict.fromkeys(keys, item))
        used += len(snippet)
        if not review:
            per_site[site] += 1
        items.append(item)
    return items


async def gather_evidence(claim: ExtractedClaim, domains: list[str],
                          reference: list[str]) -> list[EvidenceItem]:
    queries = [q for q in claim.search_queries if q.strip()][:MAX_QUERIES_PER_CLAIM] or [claim.claim]

    tasks = [search.search(q, domains=domains, max_results=RESULTS_PER_QUERY) for q in queries]
    if claim.time_sensitive:
        # News results carry publication dates, which "outdated" / "old as new" checks need.
        tasks.append(search.search(queries[0], domains=domains, max_results=RESULTS_PER_QUERY,
                                   topic="news"))
    fc_result, *search_results = await asyncio.gather(
        factcheck_api.search_fact_checks(queries[0]), *tasks, return_exceptions=True)

    reviews: list[factcheck_api.FactCheckReview] = []
    if isinstance(fc_result, BaseException):
        log.warning("fact-check lookup failed (continuing without it): %s", fc_result)
    else:
        reviews = fc_result

    failures = [r for r in search_results if isinstance(r, BaseException)]
    groups = [r for r in search_results if not isinstance(r, BaseException)]
    if failures and not groups and not reviews:
        raise failures[0]  # every source failed; don't pretend we found nothing
    for f in failures:
        log.warning("one search query failed (continuing): %s", f)
    log.debug("queries %s (news query: %s) -> %s search results, %d fact-checks",
              queries, claim.time_sensitive, [len(g) for g in groups], len(reviews))

    if claim.time_sensitive and not isinstance(search_results[-1], BaseException):
        # All news results go before the general ones, so dated articles win the per-site
        # slots over undated copies of the same site's coverage.
        groups = [search_results[-1] + _interleave(groups[:-1])]
    return build_evidence(reviews, groups, domains, reference)


# ---------- citation validation ----------

_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
MIN_QUOTE_WORD_SHARE = 0.9  # fallback when a quote isn't an exact substring


def _norm_quote_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES).casefold()
    return " ".join(s.replace("[...]", " ").split())


def quote_in(quote: str, text: str) -> bool:
    """True if `quote` was copied from `text`: an exact match after normalizing case,
    whitespace and quote marks, or (for small copying slips) at least 90% of its words
    present in the text. Language-agnostic, so non-English explanations still work."""
    q = _norm_quote_text(quote).strip(" \"'.…")
    if len(q) < 8:
        return False
    t = _norm_quote_text(text)
    if q in t:
        return True
    words = re.findall(r"\w+", q)
    if len(words) < 4:
        return False
    vocab = set(re.findall(r"\w+", t))
    return sum(w in vocab for w in words) / len(words) >= MIN_QUOTE_WORD_SHARE


def resolve_citations(out: VerificationOutput, evidence: list[EvidenceItem],
                      numbers: list[int]) -> dict[int, int | None]:
    """For each cited number, the evidence item that actually contains its quoted fact:
    the same number, a different one (misattributed citation), or None (unsupported)."""
    texts = {e.n: f"{e.title} {e.snippet}" for e in evidence}
    quotes: dict[int, list[str]] = {}
    for q in out.quotes:
        quotes.setdefault(q.n, []).append(q.quote)
    resolved: dict[int, int | None] = {}
    for n in numbers:
        own = quotes.get(n, [])
        if n in texts and any(quote_in(q, texts[n]) for q in own):
            resolved[n] = n
            continue
        homes = sorted({m for q in own for m, t in texts.items() if quote_in(q, t)})
        resolved[n] = homes[0] if homes else None
    return resolved


def apply_citations(
    out: VerificationOutput, evidence: list[EvidenceItem]
) -> tuple[VerificationOutput, list[EvidenceItem]]:
    """Check every citation against the evidence, then renumber to match the sources shown
    to the user.

    Each cited number needs a quote copied from that item. A citation whose quote is found
    only in a different item is moved there; one with no matching quote (or pointing to a
    nonexistent item) is dropped. Downgrade to unverifiable if no valid citation remains."""
    by_n = {e.n: e for e in evidence}
    inline = [int(n) for m in _CITE_RE.finditer(out.explanation) for n in m.group(1).split(",")]
    numbers = list(dict.fromkeys([*out.cited_evidence, *inline]))
    target = resolve_citations(out, evidence, numbers)

    moved = {n: t for n, t in target.items() if t is not None and t != n}
    dropped = sorted(n for n, t in target.items() if t is None)
    if moved:
        log.warning("moved misattributed citations (cited -> item with the quote): %s", moved)
    if dropped:
        log.warning("dropped citations without a supporting quote (or nonexistent): %s", dropped)

    cited = list(dict.fromkeys(target[n] for n in numbers if target[n] is not None))
    renumber = {n: i + 1 for i, n in enumerate(cited)}

    def fix(m: re.Match) -> str:
        nums = dict.fromkeys(renumber[target[int(n)]] for n in m.group(1).split(",")
                             if target.get(int(n)) is not None)
        return f"[{', '.join(map(str, nums))}]" if nums else ""

    explanation = _CITE_RE.sub(fix, out.explanation)
    explanation = re.sub(r"(\[[\d, ]+\])(?:\s*\1)+", r"\1", explanation)  # "[1][1]" -> "[1]"
    explanation = re.sub(r"\s+([.,;:])", r"\1", re.sub(r" {2,}", " ", explanation)).strip()
    verdict, confidence = out.verdict, out.confidence
    if verdict != "unverifiable" and not cited:
        log.warning("verdict %r had no valid citations; downgrading to unverifiable", verdict)
        verdict, confidence = "unverifiable", "low"
        explanation += (" (Downgraded to unverifiable: the verdict wasn't backed by any"
                        " of the retrieved sources.)")
    elif verdict in ("contradicted", "misleading") and all(by_n[n].reference for n in cited):
        # Enforced here too, not just in the prompt: a negative verdict needs more than Wikipedia.
        if confidence != "low":
            log.info("verdict %r rests only on reference sites; confidence -> low", verdict)
            confidence = "low"
            explanation += " (Low confidence: based only on reference sites such as Wikipedia.)"
    fixed = out.model_copy(update=dict(verdict=verdict, confidence=confidence,
                                       explanation=explanation, cited_evidence=cited))
    return fixed, [by_n[n] for n in cited]


# ---------- spans ----------

# One-to-one replacements (so offsets stay valid). Keep in sync with extension/matcher.js.
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'",
                         "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-"})
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")


def locate_span(text: str, original: str, claim: str) -> tuple[int, str]:
    """Find the claim's exact span in text, returning (start offset, span).

    Tries an exact match, then a match ignoring case, whitespace and curly quotes, then
    falls back to the sentence with the most words in common. Offset -1 means not found.
    """
    original = original.strip()
    if original and (i := text.find(original)) != -1:
        return i, original

    words = original.translate(_QUOTES).split()
    if words:
        pattern = r"\s+".join(re.escape(w) for w in words)
        m = re.search(pattern, text.translate(_QUOTES), re.IGNORECASE)  # same length, so offsets match
        if m:
            return m.start(), text[m.start():m.end()]

    target = set(re.findall(r"\w+", f"{original} {claim}".lower()))
    best, best_score = None, 0
    for m in _SENTENCE_RE.finditer(text):
        score = len(target & set(re.findall(r"\w+", m.group(0).lower())))
        if score > best_score:
            best, best_score = m, score
    if best:
        start = best.start() + len(best.group(0)) - len(best.group(0).lstrip())
        return start, best.group(0).strip()
    return -1, original


# ---------- per-claim processing ----------


def _unverifiable(c: ExtractedClaim, span: str, note: str) -> ClaimResult:
    return ClaimResult(original_text=span, claim=c.claim, type=c.type, verdict="unverifiable",
                       explanation=note, confidence="low")


async def verify_claim(c: ExtractedClaim, span: str, domains: list[str], reference: list[str],
                       today: str) -> ClaimResult:
    evidence = await gather_evidence(c, domains, reference)
    evidence_block = "\n\n".join(e.render() for e in evidence)
    log.debug("evidence for claim %r:\n%s", c.claim, evidence_block or "(none)")

    if not evidence:
        out = VerificationOutput(verdict="unverifiable", confidence="low", cited_evidence=[],
                                 explanation="No relevant sources were found among the trusted sites.")
    else:
        s = config.get_settings()
        out = await llm.complete_json(
            prompts.VERIFICATION_SYSTEM.format(today=today),
            prompts.verification_user(c.claim, span, evidence_block),
            VerificationOutput,
            model=s.verification_model,
        )
        log.debug("raw verdict for claim %r: %s", c.claim, out.model_dump_json())

    out, cited = apply_citations(out, evidence)
    return ClaimResult(
        original_text=span,
        claim=c.claim,
        type=c.type,
        verdict=out.verdict,
        explanation=out.explanation,
        confidence=out.confidence,
        sources=[Source(title=e.title, url=e.url, publisher=e.publisher) for e in cited],
        # Google returns loose matches; only show fact-checks the verifier found relevant.
        existing_fact_checks=[
            ExistingFactCheck(publisher=e.fact_check.publisher, rating=e.fact_check.rating,
                              url=e.fact_check.url)
            for e in cited if e.fact_check
        ],
    )


async def process_claim(
    c: ExtractedClaim, span: str, domains: list[str], reference: list[str], today: str,
    sem: asyncio.Semaphore, deadline: float,
) -> tuple[ClaimResult, bool]:
    """Returns (result, ok). Never raises: failures become 'unverifiable' with a note."""
    if c.type not in CHECKABLE_TYPES:
        return ClaimResult(original_text=span, claim=c.claim, type=c.type, verdict="not_checkable",
                           explanation=NOT_CHECKABLE_NOTE[c.type], confidence="high"), True

    s = config.get_settings()
    async with sem:
        # The timeout starts once the claim gets a slot, and never runs past the request budget.
        timeout = min(s.claim_timeout_s, deadline - time.monotonic())
        if timeout <= 0:
            log.warning("request time budget used up before claim %r started", c.claim)
            return _unverifiable(c, span, "Not checked: the request ran out of time."), False
        try:
            async with asyncio.timeout(timeout):
                return await verify_claim(c, span, domains, reference, today), True
        except TimeoutError:
            log.warning("claim timed out after %.0fs: %r", timeout, c.claim)
            return _unverifiable(c, span, f"Not checked: timed out after {timeout:.0f}s."), False
        except Exception as e:
            log.exception("claim failed: %r", c.claim)
            return _unverifiable(c, span, f"Not checked: an error occurred ({type(e).__name__})."), False


# ---------- entry point ----------


async def extract_claims(text: str, url: str | None, today: str) -> list[ExtractedClaim]:
    s = config.get_settings()
    system = prompts.EXTRACTION_SYSTEM.format(
        today=today, page=url or "unknown", max_claims=s.max_claims)
    try:
        async with asyncio.timeout(s.extraction_timeout_s):
            out = await llm.complete_json(system, prompts.extraction_user(text),
                                          ExtractionOutput, model=s.extraction_model)
    except TimeoutError:
        raise ExtractionFailed(f"claim extraction timed out after {s.extraction_timeout_s:.0f}s") from None
    except llm.LLMError as e:
        raise ExtractionFailed(f"claim extraction failed: {e}") from None
    return out.claims[: s.max_claims]


async def check_text(text: str, url: str | None = None) -> CheckResponse:
    s = config.get_settings()
    started = time.monotonic()
    deadline = started + s.request_budget_s
    domains = search.get_domains()
    reference = search.get_reference_domains()
    result_cache = cache.ResultCache(s.cache_db, s.cache_ttl_hours)
    key = cache.make_key(text, extraction_model=s.extraction_model,
                         verification_model=s.verification_model,
                         sources_hash=search.domains_hash(domains, reference), max_claims=s.max_claims)

    if cached := await result_cache.get(key):
        log.info("cache hit (%d claims)", len(cached.claims))
        return cached

    u = usage.start()
    today = date.today().isoformat()
    claims = await extract_claims(text, url, today)

    # Sort by position in the text so the panel lists claims in reading order.
    located = [(c, *locate_span(text, c.original_text, c.claim)) for c in claims]
    located.sort(key=lambda t: t[1] if t[1] >= 0 else len(text))

    sem = asyncio.Semaphore(s.max_concurrency)
    outcomes = await asyncio.gather(*(
        process_claim(c, span, domains, reference, today, sem, deadline) for c, _, span in located
    ))  # gather keeps input order

    response = CheckResponse(claims=[r for r, _ in outcomes],
                             checked_at=datetime.now(timezone.utc))
    all_ok = all(ok for _, ok in outcomes)
    if all_ok:
        await result_cache.put(key, response)
    log.info("check done: claims=%d failed=%d elapsed=%.1fs cached=%s %s",
             len(outcomes), sum(not ok for _, ok in outcomes),
             time.monotonic() - started, all_ok, u.summary())
    return response
