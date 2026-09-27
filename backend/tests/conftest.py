"""Tests never read backend/.env or real key files; they get fake settings instead.

Tests marked @pytest.mark.live use the real settings and hit real APIs. They are
deselected by default; run them with: pytest -m live
"""

import pytest

from app import config, factcheck_api, llm, search
from app.config import Settings

FAKE_KEYS = {
    "nebius_api_key": "fake-nebius",
    "tavily_api_key": "fake-tavily",
    "google_factcheck_api_key": "fake-google",
}


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        **FAKE_KEYS,
        extraction_model="test-extract",
        verification_model="test-verify",
        cache_db=tmp_path / "cache.db",
    )


@pytest.fixture(autouse=True)
def isolate(request, monkeypatch, test_settings):
    # Drop clients cached from earlier tests or live runs.
    for mod in (llm, search, factcheck_api):
        monkeypatch.setattr(mod, "_client", None)
    if request.node.get_closest_marker("live"):
        config.get_settings.cache_clear()
        return
    monkeypatch.setattr(config, "get_settings", lambda: test_settings)
