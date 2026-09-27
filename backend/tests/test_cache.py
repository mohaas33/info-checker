import time
from datetime import datetime, timezone

from app import cache
from app.cache import ResultCache, make_key
from app.schemas import CheckResponse, ClaimResult

BASE = dict(extraction_model="m1", verification_model="m2", sources_hash="s1", max_claims=8)


def response() -> CheckResponse:
    return CheckResponse(
        claims=[ClaimResult(
            original_text="Paris is in France.", claim="Paris is in France.", type="factual",
            verdict="supported", explanation="Yes [1].", confidence="high",
        )],
        checked_at=datetime.now(timezone.utc),
    )


def test_key_ignores_whitespace_differences():
    assert make_key("Paris  is\nin France. ", **BASE) == make_key("Paris is in France.", **BASE)


def test_key_keeps_case():
    assert make_key("US", **BASE) != make_key("us", **BASE)


def test_key_changes_with_models_sources_and_max_claims():
    k = make_key("text", **BASE)
    for change in [
        {"extraction_model": "other"},
        {"verification_model": "other"},
        {"sources_hash": "other"},
        {"max_claims": 3},
    ]:
        assert make_key("text", **{**BASE, **change}) != k, change


async def test_roundtrip_and_miss(tmp_path):
    c = ResultCache(tmp_path / "c.db", ttl_hours=24)
    key = make_key("text", **BASE)
    assert await c.get(key) is None
    r = response()
    await c.put(key, r)
    assert await c.get(key) == r
    assert await c.get(make_key("text", **{**BASE, "verification_model": "new"})) is None


async def test_expired_entry_is_ignored(tmp_path, monkeypatch):
    c = ResultCache(tmp_path / "c.db", ttl_hours=24)
    real_time = time.time
    monkeypatch.setattr(cache.time, "time", lambda: real_time() - 25 * 3600)
    await c.put("k", response())
    monkeypatch.setattr(cache.time, "time", real_time)
    assert await c.get("k") is None
