import json

import httpx
import pytest

from app import config, factcheck_api, search, usage


def mock_client(monkeypatch, module, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "_client", client)


# ---------- sources.yaml ----------


def test_load_domains_merges_groups_and_dedupes(tmp_path):
    f = tmp_path / "sources.yaml"
    f.write_text("a:\n  - Reuters.com\n  - bbc.com\nb:\n  - bbc.com\n  - who.int\n")
    assert search.load_domains(f) == ["bbc.com", "reuters.com", "who.int"]


def test_real_sources_yaml_has_required_seeds():
    domains = search.load_domains(config.BACKEND_DIR / "sources.yaml")
    for d in ["reuters.com", "apnews.com", "bbc.com", "snopes.com", "politifact.com",
              "factcheck.org", "fullfact.org", "who.int", "cdc.gov"]:
        assert d in domains


def test_domains_hash_ignores_order_but_not_content():
    assert search.domains_hash(["a.com", "b.com"]) == search.domains_hash(["b.com", "a.com"])
    assert search.domains_hash(["a.com"]) != search.domains_hash(["a.com", "b.com"])
    # Moving a domain into the reference group changes results, so it changes the hash.
    assert search.domains_hash(["a.com"], ["a.com"]) != search.domains_hash(["a.com"])


def test_reference_group_loaded(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("news:\n  - bbc.com\nreference:\n  - Wikipedia.org\n")
    assert search.load_groups(f) == {"news": ["bbc.com"], "reference": ["wikipedia.org"]}


def test_real_sources_yaml_marks_wikipedia_as_reference():
    groups = search.load_groups(config.BACKEND_DIR / "sources.yaml")
    assert "wikipedia.org" in groups[search.REFERENCE_GROUP]


def test_site_of_longest_match():
    trusted = ["afp.com", "factcheck.afp.com", "wikipedia.org"]
    assert search.site_of("en.wikipedia.org", trusted) == "wikipedia.org"
    assert search.site_of("factcheck.afp.com", trusted) == "factcheck.afp.com"
    assert search.site_of("www.afp.com", trusted) == "afp.com"
    assert search.site_of("notwikipedia.org", trusted) == "notwikipedia.org"


@pytest.mark.parametrize("raw, expected", [
    ("Mon, 20 Jul 2026 00:00:00 GMT", "2026-07-20"),
    ("2024-03-15T10:00:00Z", "2024-03-15"),
    ("2024-03-15", "2024-03-15"),
    ("garbage", None),
    (None, None),
])
def test_iso_date(raw, expected):
    assert search.iso_date(raw) == expected


# ---------- Tavily ----------


async def test_search_sends_domains_and_parses(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"results": [
            {"title": "T1", "url": "https://www.reuters.com/a", "content": "c1",
             "published_date": "Fri, 02 Jan 2026 07:55:00 GMT"},
            {"title": None, "url": "https://apnews.com/b", "content": None},
            {"title": "no url"},
        ]})

    mock_client(monkeypatch, search, handler)
    u = usage.start()
    results = await search.search("q", domains=["reuters.com", "apnews.com"], max_results=3)

    assert seen["body"]["include_domains"] == ["reuters.com", "apnews.com"]
    assert seen["body"]["max_results"] == 3
    assert seen["body"]["topic"] == "general"
    assert seen["auth"] == "Bearer fake-tavily"
    assert [r.domain for r in results] == ["reuters.com", "apnews.com"]
    assert results[0].published_date == "2026-01-02"
    assert results[1].title == "https://apnews.com/b" and results[1].content == ""
    assert u.searches == 1


async def test_search_error_does_not_leak_key(monkeypatch):
    mock_client(monkeypatch, search, lambda req: httpx.Response(
        401, text="Unauthorized: invalid key fake-tavily"))
    with pytest.raises(search.SearchError) as exc:
        await search.search("q", domains=["x.com"])
    assert "401" in str(exc.value)
    assert "fake-tavily" not in str(exc.value)


async def test_search_network_error(monkeypatch):
    def handler(req):
        raise httpx.ConnectTimeout("timed out", request=req)

    mock_client(monkeypatch, search, handler)
    with pytest.raises(search.SearchError, match="ConnectTimeout"):
        await search.search("q", domains=["x.com"])


# ---------- Google Fact Check ----------

GOOGLE_RESPONSE = {"claims": [
    {"text": "The moon is made of cheese", "claimReview": [
        {"publisher": {"name": "Snopes", "site": "snopes.com"},
         "url": "https://snopes.com/moon", "title": "Moon cheese?",
         "textualRating": "False", "reviewDate": "2025-03-01T00:00:00Z"},
        {"publisher": {"site": "example.org"}, "url": "https://example.org/x",
         "textualRating": "Pants on fire"},
        {"publisher": {"name": "No rating"}, "url": "https://x.org"},
    ]},
]}


async def test_fact_checks_parsed(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json=GOOGLE_RESPONSE)

    mock_client(monkeypatch, factcheck_api, handler)
    u = usage.start()
    reviews = await factcheck_api.search_fact_checks("moon cheese")

    assert [(r.publisher, r.rating) for r in reviews] == [
        ("Snopes", "False"), ("example.org", "Pants on fire")]
    assert reviews[0].claim_text == "The moon is made of cheese"
    assert seen["req"].headers["x-goog-api-key"] == "fake-google"
    assert "key=" not in str(seen["req"].url)  # key never in the URL
    assert u.factcheck_lookups == 1


async def test_fact_checks_skipped_without_key(monkeypatch, test_settings):
    test_settings.google_factcheck_api_key = None
    test_settings._keys.clear()

    def handler(req):
        raise AssertionError("should not call the API without a key")

    mock_client(monkeypatch, factcheck_api, handler)
    assert await factcheck_api.search_fact_checks("anything") == []


async def test_fact_check_error_does_not_leak_key(monkeypatch):
    mock_client(monkeypatch, factcheck_api, lambda req: httpx.Response(
        403, text="API key fake-google not valid"))
    with pytest.raises(factcheck_api.FactCheckError) as exc:
        await factcheck_api.search_fact_checks("q")
    assert "fake-google" not in str(exc.value)


# ---------- live (pytest -m live) ----------


@pytest.mark.live
async def test_live_tavily_single_query():
    """One real query, 2 results, 1 Tavily credit."""
    results = await search.search(
        "WHO declares end of COVID-19 global health emergency",
        domains=search.load_domains(),
        max_results=2,
    )
    allowed = set(search.load_domains())
    print("\n" + "\n".join(f"  {r.domain}  {r.title[:70]}  ({len(r.content)} chars)" for r in results))
    assert results, "expected at least one result"
    for r in results:
        assert any(r.domain == d or r.domain.endswith("." + d) for d in allowed), r.domain


@pytest.mark.parametrize("url, expected", [
    ("https://www.aljazeera.com/news/2026/7/15/starmer-bids-mps-goodbye", "2026-07-15"),
    ("https://www.npr.org/2026/06/22/nx-s1-5866231/keir-starmer-resigns", "2026-06-22"),
    ("https://www.france24.com/en/live-news/20221017-the-uk-is-there-a-prime-minister", "2022-10-17"),
    ("https://example.com/2024-03-05/story", "2024-03-05"),
    ("https://www.bbc.com/news/articles/clyv9qe6wj7o", None),
    ("https://example.com/2026/13/45/bad-date", None),
    ("https://example.com/item/20261399-x", None),
])
def test_date_from_url(url, expected):
    assert search.date_from_url(url) == expected


async def test_search_falls_back_to_url_date(monkeypatch):
    mock_client(monkeypatch, search, lambda req: httpx.Response(200, json={"results": [
        {"title": "t", "url": "https://npr.org/2026/06/22/x", "content": "c"}]}))
    [r] = await search.search("q", domains=["npr.org"])
    assert r.published_date == "2026-06-22"


@pytest.mark.parametrize("url, expected", [
    ("https://www.britannica.com./topic/x", "britannica.com"),
    ("https://britannica.com./topic/x", "britannica.com"),
    ("https://www.reuters.com/a", "reuters.com"),
    ("https://en.wikipedia.org/wiki/X", "en.wikipedia.org"),
])
def test_domain_of_strips_www_and_trailing_dot(url, expected):
    assert search.domain_of(url) == expected
    assert search.site_of(search.domain_of(url), ["britannica.com", "wikipedia.org", "reuters.com"]) in (
        "britannica.com", "wikipedia.org", "reuters.com")
