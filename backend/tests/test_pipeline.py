import asyncio
import logging
import re

import pytest

from app import factcheck_api, llm, pipeline, search
from app.factcheck_api import FactCheckReview
from app.schemas import ExtractedClaim, ExtractionOutput, VerificationOutput
from app.search import SearchResult

TEXT = (
    "The Eiffel Tower is 330 metres tall. "
    "It is the most beautiful building in Europe. "
    "Visitor numbers will double by 2030. "
    "Paris has 20 million residents."
)

CLAIMS = [
    # Deliberately out of text order, to check sorting.
    ExtractedClaim(original_text="Paris has 20 million residents.", claim="Paris has 20 million residents.",
                   type="factual", search_queries=["Paris population"]),
    ExtractedClaim(original_text="The Eiffel Tower is 330 metres tall.", claim="The Eiffel Tower is 330 metres tall.",
                   type="factual", search_queries=["Eiffel Tower height"]),
    ExtractedClaim(original_text="It is the most beautiful building in Europe.",
                   claim="The Eiffel Tower is the most beautiful building in Europe.", type="opinion"),
    ExtractedClaim(original_text="Visitor numbers will double by 2030.",
                   claim="Eiffel Tower visitor numbers will double by 2030.", type="prediction"),
]


def result(url, content="snippet", date=None):
    return SearchResult(title=f"Title {url}", url=url, content=content,
                        domain=search.domain_of(url), published_date=date)


class Fakes:
    """Scripted stand-ins for the LLM, Tavily and Google. Records what they were called with."""

    def __init__(self):
        self.claims = list(CLAIMS)
        self.verdicts: dict[str, VerificationOutput | Exception] = {}
        self.search_results: dict[str, list[SearchResult] | Exception] = {}
        self.fact_checks: list[FactCheckReview] = []
        self.llm_calls: list[tuple[str, str]] = []
        self.delays: dict[str, float] = {}
        self.searches: list[tuple[str, str]] = []

    async def complete_json(self, system, user, schema_model, model):
        self.llm_calls.append((system, user))
        if schema_model is ExtractionOutput:
            return ExtractionOutput(claims=self.claims)
        claim = user.split("\n")[1]
        await asyncio.sleep(self.delays.get(claim, 0))
        v = self.verdicts.get(claim) or VerificationOutput(
            verdict="supported", explanation="Confirmed [1].", confidence="high", cited_evidence=[1])
        if isinstance(v, Exception):
            raise v
        return v

    async def search(self, query, *, domains, max_results=10, topic="general"):
        self.searches.append((query, topic))
        r = self.search_results.get(f"{topic}:{query}", self.search_results.get(query))
        if isinstance(r, Exception):
            raise r
        return r if r is not None else [result(f"https://reuters.com/{query.replace(' ', '-')}")]

    async def search_fact_checks(self, query, max_claims=5):
        return self.fact_checks


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(llm, "complete_json", f.complete_json)
    monkeypatch.setattr(search, "search", f.search)
    monkeypatch.setattr(factcheck_api, "search_fact_checks", f.search_fact_checks)
    return f


def by_claim(resp):
    return {c.claim: c for c in resp.claims}


# ---------- end to end ----------


async def test_claims_in_text_order_and_not_checkable(fakes):
    resp = await pipeline.check_text(TEXT)
    assert [c.original_text for c in resp.claims] == [
        "The Eiffel Tower is 330 metres tall.",
        "It is the most beautiful building in Europe.",
        "Visitor numbers will double by 2030.",
        "Paris has 20 million residents.",
    ]
    types_verdicts = [(c.type, c.verdict) for c in resp.claims]
    assert types_verdicts == [("factual", "supported"), ("opinion", "not_checkable"),
                              ("prediction", "not_checkable"), ("factual", "supported")]
    # Only the 2 factual claims reach verification (+1 extraction call).
    assert len(fakes.llm_calls) == 3
    assert resp.claims[1].sources == []


async def test_one_claim_failing_does_not_fail_request(fakes):
    fakes.verdicts["Paris has 20 million residents."] = llm.LLMError("bad output")
    resp = await pipeline.check_text(TEXT)
    c = by_claim(resp)
    assert c["Paris has 20 million residents."].verdict == "unverifiable"
    assert "error" in c["Paris has 20 million residents."].explanation
    assert c["The Eiffel Tower is 330 metres tall."].verdict == "supported"


async def test_search_failure_isolated(fakes):
    fakes.search_results["Paris population"] = search.SearchError("HTTP 500")
    resp = await pipeline.check_text(TEXT)
    c = by_claim(resp)
    assert c["Paris has 20 million residents."].verdict == "unverifiable"
    assert c["The Eiffel Tower is 330 metres tall."].verdict == "supported"


async def test_claim_timeout(fakes, test_settings):
    test_settings.claim_timeout_s = 0.05
    fakes.delays["Paris has 20 million residents."] = 1
    resp = await pipeline.check_text(TEXT)
    slow = by_claim(resp)["Paris has 20 million residents."]
    assert slow.verdict == "unverifiable" and "timed out" in slow.explanation
    assert by_claim(resp)["The Eiffel Tower is 330 metres tall."].verdict == "supported"


async def test_request_budget_caps_all_claims(fakes, test_settings):
    test_settings.request_budget_s = 0.1
    test_settings.claim_timeout_s = 45
    for c in CLAIMS:
        fakes.delays[c.claim] = 5
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await pipeline.check_text(TEXT)
    assert loop.time() - t0 < 1
    assert all(c.verdict in ("unverifiable", "not_checkable") for c in resp.claims)


async def test_cache_hit_skips_all_calls(fakes):
    first = await pipeline.check_text(TEXT)
    calls = len(fakes.llm_calls)
    second = await pipeline.check_text("  " + TEXT.replace(" ", "  ") + "\n")
    assert second == first
    assert len(fakes.llm_calls) == calls


async def test_cache_misses_when_model_changes(fakes, test_settings):
    await pipeline.check_text(TEXT)
    calls = len(fakes.llm_calls)
    test_settings.verification_model = "another-model"
    await pipeline.check_text(TEXT)
    assert len(fakes.llm_calls) > calls


async def test_partial_failure_not_cached(fakes):
    fakes.verdicts["Paris has 20 million residents."] = llm.LLMError("bad output")
    await pipeline.check_text(TEXT)
    del fakes.verdicts["Paris has 20 million residents."]
    resp = await pipeline.check_text(TEXT)
    assert by_claim(resp)["Paris has 20 million residents."].verdict == "supported"


async def test_extraction_failure_raises(fakes, monkeypatch):
    async def broken(*a, **k):
        raise llm.LLMError("garbage")

    monkeypatch.setattr(llm, "complete_json", broken)
    with pytest.raises(pipeline.ExtractionFailed):
        await pipeline.check_text(TEXT)


async def test_extraction_parses_fenced_think_output(monkeypatch):
    """Real complete_json + parsing, only the raw HTTP completion is faked."""
    raw = ('<think>The user wants claims. {not json}</think>\n```json\n'
           '{"claims": [{"original_text": "Paris has 20 million residents.", '
           '"claim": "Paris has 20 million residents.", "type": "factual", '
           '"search_queries": ["Paris population"]}]}\n```')

    async def fake_raw(messages, model):
        return raw

    monkeypatch.setattr(llm, "_raw_completion", fake_raw)
    claims = await pipeline.extract_claims(TEXT, None, "2026-09-25")
    assert claims[0].type == "factual" and claims[0].search_queries == ["Paris population"]


async def test_max_claims_enforced(fakes, test_settings):
    test_settings.max_claims = 2
    resp = await pipeline.check_text(TEXT)
    assert len(resp.claims) == 2


# ---------- prompts get dates ----------


async def test_verification_prompt_has_today_and_published_dates(fakes):
    fakes.search_results["Eiffel Tower height"] = [
        result("https://reuters.com/eiffel", "330 m", date="2024-03-15T10:00:00Z"),
        result("https://apnews.com/eiffel", "tall"),
    ]
    await pipeline.check_text(TEXT)
    system, user = next(c for c in fakes.llm_calls
                        if c[1].startswith("Claim to check:\nThe Eiffel Tower is 330"))
    assert re.search(r"Today's date is \d{4}-\d{2}-\d{2}", system)
    assert "Published: 2024-03-15" in user
    assert "Published: unknown" in user


# ---------- citation validation ----------


def ev(n):
    return pipeline.EvidenceItem(n, f"T{n}", "pub", f"https://x.org/{n}", "s", None)


def test_invalid_citations_dropped_and_renumbered():
    out = VerificationOutput(verdict="contradicted", confidence="high", cited_evidence=[3, 9],
                             explanation="Sources say no [3]. Also [9] and [1, 3].")
    fixed, cited = pipeline.apply_citations(out, [ev(1), ev(2), ev(3)])
    assert [e.n for e in cited] == [3, 1]  # cited_evidence order first, then inline
    assert fixed.cited_evidence == [3, 1]
    assert fixed.verdict == "contradicted"
    assert fixed.explanation == "Sources say no [1]. Also and [2, 1]."


def test_zero_valid_citations_downgrades():
    out = VerificationOutput(verdict="supported", confidence="high", cited_evidence=[7],
                             explanation="Confirmed [7].")
    fixed, cited = pipeline.apply_citations(out, [ev(1), ev(2)])
    assert cited == []
    assert fixed.verdict == "unverifiable" and fixed.confidence == "low"
    assert "Downgraded" in fixed.explanation


def test_unverifiable_without_citations_left_alone():
    out = VerificationOutput(verdict="unverifiable", confidence="medium", cited_evidence=[],
                             explanation="Nothing relevant.")
    fixed, _ = pipeline.apply_citations(out, [ev(1)])
    assert fixed.verdict == "unverifiable" and fixed.confidence == "medium"


async def test_downgrade_end_to_end(fakes):
    fakes.verdicts["The Eiffel Tower is 330 metres tall."] = VerificationOutput(
        verdict="contradicted", confidence="high", cited_evidence=[42], explanation="Wrong [42].")
    c = by_claim(await pipeline.check_text(TEXT))["The Eiffel Tower is 330 metres tall."]
    assert c.verdict == "unverifiable" and c.confidence == "low" and c.sources == []


async def test_no_evidence_skips_llm(fakes):
    fakes.search_results["Paris population"] = []
    resp = await pipeline.check_text(TEXT)
    assert by_claim(resp)["Paris has 20 million residents."].verdict == "unverifiable"
    assert not any("Paris has 20" in u for _, u in fakes.llm_calls[1:])


# ---------- evidence ----------


def test_evidence_dedup_order_and_budget():
    reviews = [FactCheckReview(publisher="Snopes", rating="False", url="https://snopes.com/a/",
                               claim_text="x")]
    results = [
        result("https://www.snopes.com/a"),  # duplicate of the fact-check
        result("https://reuters.com/b#section", "r" * 5000),
        result("https://reuters.com/b"),  # duplicate after normalization
        result("https://snopes.com/old-url.htm", "same article"),
        result("https://snopes.com/new-url", "Same  article"),  # same content, other URL
    ] + [result(f"https://site{i}.com/x", f"{i} " + "a" * 590) for i in range(30)]
    items = pipeline.build_evidence(reviews, [results])
    assert items[0].fact_check is not None
    assert items[1].url == "https://reuters.com/b#section"
    assert len(items[1].snippet) <= pipeline.SNIPPET_CHARS + 4
    assert [e.url for e in items].count("https://snopes.com/new-url") == 0
    assert [e.n for e in items] == list(range(1, len(items) + 1))
    assert sum(len(e.snippet) for e in items) <= pipeline.EVIDENCE_BUDGET_CHARS
    assert len({pipeline._url_key(e.url) for e in items}) == len(items)


async def test_relevant_fact_checks_returned(fakes):
    fakes.fact_checks = [FactCheckReview(publisher="PolitiFact", rating="False",
                                         url="https://politifact.com/p", claim_text="Paris 20M")]
    resp = await pipeline.check_text(TEXT)
    c = by_claim(resp)["Paris has 20 million residents."]
    # Fake verdict cites [1], which is the fact-check (they come first).
    assert [f.publisher for f in c.existing_fact_checks] == ["PolitiFact"]
    assert c.sources[0].url == "https://politifact.com/p"


async def test_debug_log_has_evidence_and_verdict(fakes, caplog):
    caplog.set_level(logging.DEBUG, logger="app.pipeline")
    await pipeline.check_text(TEXT)
    assert "evidence for claim" in caplog.text and "[1] Title https://reuters.com" in caplog.text
    assert "raw verdict for claim" in caplog.text
    assert "fake-" not in caplog.text  # no keys


# ---------- spans ----------


@pytest.mark.parametrize("original, expected", [
    ("Paris has 20 million residents.", "Paris has 20 million residents."),
    ("paris  has 20\nmillion residents", "Paris has 20 million residents"),
    ("Paris has twenty million people", "Paris has 20 million residents."),  # fallback: best sentence
])
def test_locate_span(original, expected):
    start, span = pipeline.locate_span(TEXT, original, "Paris population")
    assert span == expected
    assert TEXT[start:start + len(span)] == span


def test_locate_span_curly_quotes():
    text = 'He said "we will win" today.'
    start, span = pipeline.locate_span(text, "He said “we will win”", "")
    assert span == 'He said "we will win"' and start == 0


# ---------- source diversity, news query, reference sites ----------


def test_at_most_two_articles_per_site():
    trusted = ["wikipedia.org", "reuters.com"]
    results = [result(f"https://en.wikipedia.org/wiki/{i}", f"wiki {i}") for i in range(3)] + [
        result("https://de.wikipedia.org/wiki/x", "wiki de"),
        result("https://reuters.com/a", "r a"),
    ]
    items = pipeline.build_evidence([], [results], trusted)
    assert [e.publisher for e in items] == ["wikipedia.org", "wikipedia.org", "reuters.com"]


def test_fact_checks_not_capped_per_site():
    reviews = [FactCheckReview(publisher="Snopes", rating="False", url=f"https://snopes.com/{i}",
                               claim_text=f"c{i}") for i in range(3)]
    assert len(pipeline.build_evidence(reviews, [])) == 3


def test_interleave_gives_every_query_space():
    general = [result(f"https://g{i}.com/x", f"{i} " + "g" * 590) for i in range(20)]
    news = [result(f"https://n{i}.com/x", f"{i} " + "n" * 590, date="2026-07-20") for i in range(5)]
    items = pipeline.build_evidence([], [general, news])
    assert sum(e.published == "2026-07-20" for e in items) >= 5
    assert items[1].url == "https://n0.com/x"


async def test_news_query_only_for_time_sensitive(fakes):
    fakes.claims = [
        ExtractedClaim(original_text="The Eiffel Tower is 330 metres tall.",
                       claim="The Eiffel Tower is 330 metres tall.", type="factual",
                       search_queries=["Eiffel Tower height"], time_sensitive=False),
        ExtractedClaim(original_text="Paris has 20 million residents.",
                       claim="Paris has 20 million residents.", type="factual",
                       search_queries=["Paris population", "Paris residents 2026"], time_sensitive=True),
    ]
    await pipeline.check_text(TEXT)
    assert sorted(fakes.searches) == sorted([
        ("Eiffel Tower height", "general"),
        ("Paris population", "general"), ("Paris residents 2026", "general"),
        ("Paris population", "news"),
    ])


def ref_ev(n, reference):
    return pipeline.EvidenceItem(n, f"T{n}", "wikipedia.org" if reference else "reuters.com",
                                 f"https://x.org/{n}", "s", None, reference=reference)


@pytest.mark.parametrize("verdict", ["contradicted", "misleading"])
def test_negative_verdict_on_reference_only_gets_low_confidence(verdict):
    out = VerificationOutput(verdict=verdict, confidence="high", cited_evidence=[1],
                             explanation="Wiki says no [1].")
    fixed, _ = pipeline.apply_citations(out, [ref_ev(1, True), ref_ev(2, False)])
    assert fixed.verdict == verdict and fixed.confidence == "low"
    assert "reference sites" in fixed.explanation


def test_reference_plus_primary_keeps_confidence():
    out = VerificationOutput(verdict="contradicted", confidence="high", cited_evidence=[1, 2],
                             explanation="No [1][2].")
    fixed, _ = pipeline.apply_citations(out, [ref_ev(1, True), ref_ev(2, False)])
    assert fixed.confidence == "high"


def test_supported_on_reference_only_keeps_confidence():
    out = VerificationOutput(verdict="supported", confidence="high", cited_evidence=[1],
                             explanation="Yes [1].")
    fixed, _ = pipeline.apply_citations(out, [ref_ev(1, True)])
    assert fixed.confidence == "high"


def test_reference_sites_flagged_in_prompt():
    items = pipeline.build_evidence([], [[result("https://en.wikipedia.org/wiki/X", "w"),
                                          result("https://reuters.com/y", "r")]],
                                    ["wikipedia.org", "reuters.com"], ["wikipedia.org"])
    assert items[0].reference and "Reference site | Publisher: wikipedia.org" in items[0].render()
    assert not items[1].reference and "Article | Publisher: reuters.com" in items[1].render()


def test_duplicate_from_news_query_backfills_date():
    general = [result("https://www.reuters.com/a", "same story")]
    news = [result("https://reuters.com/a/", "same story", date="2026-07-21")]
    items = pipeline.build_evidence([], [general, news])
    assert len(items) == 1 and items[0].published == "2026-07-21"


async def test_news_results_win_site_slots(fakes):
    fakes.claims = [ExtractedClaim(original_text="Paris has 20 million residents.",
                                   claim="Paris has 20 million residents.", type="factual",
                                   search_queries=["Paris population"], time_sensitive=True)]
    fakes.search_results["general:Paris population"] = [
        result(f"https://apnews.com/old{i}", f"undated {i}") for i in range(3)]
    fakes.search_results["news:Paris population"] = [
        result(f"https://apnews.com/new{i}", f"dated {i}", date="2026-07-01") for i in range(3)]
    evidence = await pipeline.gather_evidence(fakes.claims[0], ["apnews.com"], [])
    assert [e.published for e in evidence] == ["2026-07-01", "2026-07-01"]


def test_locate_span_dashes():
    text = "COVID\u201119 was declared a pandemic \u2014 in March 2020."
    start, span = pipeline.locate_span(text, "COVID-19 was declared a pandemic - in March 2020", "")
    assert start == 0 and span == "COVID\u201119 was declared a pandemic \u2014 in March 2020"
