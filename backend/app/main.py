"""FastAPI app. Run from backend/:  .venv/bin/uvicorn app.main:app --port 8000"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app import config, pipeline, search
from app.config import ConfigError
from app.schemas import CheckRequest, CheckResponse

settings = config.get_settings()
# LOG_LEVEL applies to our own loggers only. Third-party libraries stay at INFO, so
# LOG_LEVEL=DEBUG never dumps HTTP-client internals (which could include request details).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
log.setLevel(settings.log_level.upper())


def check_startup_config() -> None:
    """Fail fast on missing keys or models instead of on the first request."""
    s = config.get_settings()
    s.require_key("nebius_api_key")
    s.require_key("tavily_api_key")
    s.get_key("google_factcheck_api_key")  # optional; surfaces file errors and permission warnings
    for name in ("extraction_model", "verification_model"):
        if not getattr(s, name):
            raise ConfigError(f"{name.upper()} is not set in backend/.env")
    search.get_domains()
    if not s.allowed_origins_list:
        log.warning("ALLOWED_ORIGINS is empty; browsers will be blocked by CORS")


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_startup_config()
    log.info("ready: extraction=%s verification=%s origins=%s",
             settings.extraction_model, settings.verification_model, settings.allowed_origins_list)
    yield


app = FastAPI(title="Info Checker", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
async def health() -> dict:
    s = config.get_settings()
    return {
        "status": "ok",
        "extraction_model": s.extraction_model,
        "verification_model": s.verification_model,
        "fact_check_api": bool(s.get_key("google_factcheck_api_key")),
    }


@app.post("/check", response_model=CheckResponse)
async def check(req: CheckRequest) -> CheckResponse:
    try:
        return await pipeline.check_text(req.text, req.url)
    except pipeline.ExtractionFailed as e:
        log.error("%s", e)
        raise HTTPException(status_code=502, detail=f"Could not analyze the text: {e}") from None
