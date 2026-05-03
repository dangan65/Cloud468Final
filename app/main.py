import hashlib
import logging
import os
import random
import string
import time

import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="URL Shortener",
    description="A production-grade URL shortener built with FastAPI + Redis",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Redis connection — initialized on startup with retry logic
# ---------------------------------------------------------------------------
redis_client: redis.Redis | None = None

CHARS = string.ascii_letters + string.digits  # base-62 alphabet
CODE_LENGTH = 7


def build_redis_client() -> redis.Redis:
    host = os.getenv("REDIS_HOST", "redis")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return redis.Redis(host=host, port=port, decode_responses=True)


@app.on_event("startup")
async def startup_event() -> None:
    """Connect to Redis with exponential back-off so the API waits for Redis
    to be healthy rather than crashing immediately on a cold start."""
    global redis_client
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            client = build_redis_client()
            client.ping()
            redis_client = client
            logger.info("Connected to Redis successfully on attempt %d", attempt)
            return
        except redis.ConnectionError as exc:
            wait = 2 ** attempt  # 2s, 4s, 8s …
            logger.warning(
                "Redis not ready (attempt %d/%d). Retrying in %ds. Error: %s",
                attempt,
                max_retries,
                wait,
                exc,
            )
            time.sleep(min(wait, 30))
    raise RuntimeError("Could not connect to Redis after %d retries" % max_retries)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ShortenRequest(BaseModel):
    url: HttpUrl
    ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days default


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str
    original_url: str
    ttl_seconds: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def generate_short_code(url: str) -> str:
    """Derive a deterministic 7-char base-62 code from the URL's MD5 hash.

    On a hash collision (different URL already occupies the slot) we mix in
    random characters until we find a free slot — a simple open-addressing
    strategy that keeps things stateless.
    """
    digest = hashlib.md5(url.encode()).digest()
    code = "".join(CHARS[b % len(CHARS)] for b in digest[:CODE_LENGTH])

    existing = redis_client.get(f"url:{code}")
    if existing is None or existing == url:
        return code

    # Collision resolution: randomise the last two characters
    logger.warning("Hash collision detected for code %s, resolving...", code)
    for _ in range(100):
        suffix = "".join(random.choices(CHARS, k=2))
        candidate = code[:5] + suffix
        existing = redis_client.get(f"url:{candidate}")
        if existing is None or existing == url:
            return candidate

    raise RuntimeError("Could not resolve hash collision after 100 attempts")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", tags=["meta"])
async def root():
    return {
        "service": "URL Shortener",
        "docs": "/docs",
        "health": "/health",
        "shorten": "POST /shorten",
    }


@app.get("/health", tags=["meta"])
async def health_check():
    """Liveness probe used by Docker HEALTHCHECK and load balancers."""
    try:
        redis_client.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}")


@app.post("/shorten", response_model=ShortenResponse, tags=["shortener"])
async def shorten_url(body: ShortenRequest, request: Request):
    """Accept a long URL and return a shortened version.

    - Stores the mapping in Redis with a configurable TTL.
    - Uses MD5-based deterministic hashing so the same URL always maps to the
      same code (idempotent), with collision resolution for rare conflicts.
    """
    url_str = str(body.url)
    code = generate_short_code(url_str)

    redis_client.setex(f"url:{code}", body.ttl_seconds, url_str)
    logger.info("Shortened %s → %s (ttl=%ds)", url_str, code, body.ttl_seconds)

    base = str(request.base_url).rstrip("/")
    return ShortenResponse(
        short_code=code,
        short_url=f"{base}/{code}",
        original_url=url_str,
        ttl_seconds=body.ttl_seconds,
    )


@app.get("/{code}", tags=["shortener"])
async def redirect(code: str):
    """Resolve a short code to the original URL and issue a 301 redirect.

    301 (Moved Permanently) allows browsers and CDNs to cache the redirect,
    reducing latency on repeat visits.
    """
    url = redis_client.get(f"url:{code}")
    if not url:
        raise HTTPException(
            status_code=404,
            detail=f"Short code '{code}' not found or has expired.",
        )
    return RedirectResponse(url=url, status_code=301)
