import pytest
from fastapi.testclient import TestClient

from app import main, pipeline
from app.schemas import CheckResponse


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("text", ["", "   \n  ", "x" * 10_001])
def test_rejects_empty_or_long_text(client, text):
    assert client.post("/check", json={"text": text}).status_code == 422


def test_check_ok(client, monkeypatch):
    async def fake(text, url):
        return CheckResponse(claims=[], checked_at="2026-09-25T00:00:00Z")

    monkeypatch.setattr(pipeline, "check_text", fake)
    r = client.post("/check", json={"text": "x" * 10_000, "url": None})
    assert r.status_code == 200 and r.json()["claims"] == []


def test_extraction_failure_is_502(client, monkeypatch):
    async def fake(text, url):
        raise pipeline.ExtractionFailed("claim extraction timed out after 30s")

    monkeypatch.setattr(pipeline, "check_text", fake)
    r = client.post("/check", json={"text": "hello"})
    assert r.status_code == 502 and "timed out" in r.json()["detail"]


def test_cors_blocks_unknown_origin(client):
    r = client.options("/check", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
