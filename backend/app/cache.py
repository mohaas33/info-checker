"""SQLite cache of /check responses.

The key covers everything that changes the result: the normalized text, both model
names, MAX_CLAIMS, the trusted-domain list, and PIPELINE_VERSION (bump it when prompts or
pipeline logic change).
"""

import hashlib
import json
import time
import unicodedata
from pathlib import Path

import aiosqlite

from app.schemas import CheckResponse

PIPELINE_VERSION = "6"


def normalize_text(text: str) -> str:
    """Unicode NFC plus collapsed whitespace. Case is kept, since original_text spans
    returned from the cache must match the user's text exactly."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def make_key(
    text: str,
    *,
    extraction_model: str,
    verification_model: str,
    sources_hash: str,
    max_claims: int,
) -> str:
    parts = {
        "v": PIPELINE_VERSION,
        "text": normalize_text(text),
        "extraction_model": extraction_model,
        "verification_model": verification_model,
        "sources": sources_hash,
        "max_claims": max_claims,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


class ResultCache:
    def __init__(self, path: Path, ttl_hours: float):
        self.path = path
        self.ttl_seconds = ttl_hours * 3600

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        await db.execute(
            "CREATE TABLE IF NOT EXISTS results "
            "(key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        return db

    async def get(self, key: str) -> CheckResponse | None:
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT response FROM results WHERE key = ? AND created_at > ?",
                (key, time.time() - self.ttl_seconds),
            ) as cur:
                row = await cur.fetchone()
        finally:
            await db.close()
        return CheckResponse.model_validate_json(row[0]) if row else None

    async def put(self, key: str, response: CheckResponse) -> None:
        db = await self._connect()
        try:
            now = time.time()
            await db.execute(
                "INSERT OR REPLACE INTO results (key, response, created_at) VALUES (?, ?, ?)",
                (key, response.model_dump_json(), now),
            )
            await db.execute("DELETE FROM results WHERE created_at <= ?", (now - self.ttl_seconds,))
            await db.commit()
        finally:
            await db.close()
