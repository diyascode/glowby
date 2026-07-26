"""
Glowby — multi-agent misinformation detection & fact-checking.

v1 scope: paste a link, get a fact-check.
Pipeline: ingest -> Sorting Gate/Router (13 buckets) -> evidence (parallel)
-> category judge engine (13 rubrics, truth score 0.0-9.9) -> Output agent
(headline = MIN, safety collapse) -> Postgres cache + permalinks.

v0.9: async job queue with progress stages, Design 2 list UI with Design 3
reel toggle, shareable permalinks (/r/<key>), report-a-mistake.
"""

import os
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agents.evidence import gather_evidence
from app.agents.ingest import IngestError, ingest
from app.agents.judge import judge_with_rubric
from app.agents.output import build_report
from app.agents.router import (
    MODEL as ROUTER_MODEL,
    TAXONOMY_VERSION,
    RouterError,
    route_claims,
    select_for_verification,
)
from app.storage import (
    add_usage,
    canonical_key,
    get_cached,
    looks_like_url,
    save_result,
    save_route_audit,
    text_key,
    today_usage,
)

VERSION = "0.14.0"

# evidence+judgment run for the top N claims by risk (cost control)
MAX_CLAIMS_WITH_EVIDENCE = 3

# ---- armor knobs (all overridable via Railway Variables) ----
DAILY_BUDGET_USD = float(os.environ.get("GLOWBY_DAILY_BUDGET_USD", "10"))
COST_PER_CHECK_EST = float(os.environ.get("GLOWBY_COST_PER_CHECK_EST", "0.15"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("GLOWBY_RATE_LIMIT_PER_HOUR", "8"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("GLOWBY_JOB_TIMEOUT_SECONDS", "480"))

# ---- bot protection (Cloudflare Turnstile) ----
# Dormant until BOTH keys are set as Railway Variables:
#   TURNSTILE_SITE_KEY   (public, goes into the page)
#   TURNSTILE_SECRET_KEY (secret, used server-side to verify)
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")


def _verify_turnstile(token: str, ip: str) -> bool:
    """Server-side captcha check. True when valid or captcha disabled."""
    if not TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur

    try:
        data = _up.urlencode({
            "secret": TURNSTILE_SECRET_KEY,
            "response": token,
            "remoteip": ip,
        }).encode()
        req = _ur.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
        )
        with _ur.urlopen(req, timeout=10) as resp:
            out = _json.loads(resp.read().decode())
        return bool(out.get("success"))
    except Exception:
        return False

# per-IP sliding-window rate limiter (in-process; fine at beta scale)
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_HOUR:
            return True
        q.append(now)
        return False

app = FastAPI(
    title="Glowby",
    description="Paste a link, get a fact-check.",
    version=VERSION,
)

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "app.html")
_template_cache = None


def _page() -> str:
    global _template_cache
    if _template_cache is None:
        with open(_TEMPLATE_PATH, encoding="utf-8") as f:
            _template_cache = f.read().replace(
                "__TURNSTILE_SITE_KEY__", TURNSTILE_SITE_KEY
            )
    return _template_cache


# ------------------------------------------------------------ job queue
# In-process queue: fine for beta scale; jobs live ~2 minutes. A crashed
# deploy loses in-flight jobs only — results are cached in Postgres.

_jobs: dict = {}
_jobs_lock = threading.Lock()
MAX_JOBS_KEPT = 500


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {})
        job.update(fields)
        if len(_jobs) > MAX_JOBS_KEPT:  # drop oldest
            for k in list(_jobs)[: len(_jobs) - MAX_JOBS_KEPT]:
                _jobs.pop(k, None)


def _publish_partial(job_id: str, result: dict, claims: list) -> None:
    """Push a live snapshot so the UI can show claims as they finish."""
    import copy

    _set_job(job_id, partial={
        "platform": result.get("platform"),
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "posted_date": result.get("posted_date"),
        "transcript_source": result.get("transcript_source"),
        "claims": copy.deepcopy(claims),
    })


def _run_pipeline(job_id: str, url: str, url_key: str) -> None:
    try:
        t0 = time.time()
        if url_key.startswith("text:"):
            # typed claim: no video to fetch — enter at the router
            _set_job(job_id, status="running", stage="routing")
            text = url.strip()
            result = {
                "url": None,
                "platform": "typed claim",
                "title": (text[:90] + "…") if len(text) > 90 else text,
                "uploader": "typed into Glowby",
                "duration_seconds": 0,
                "transcript": text,
                "transcript_source": "typed",
            }
        else:
            _set_job(job_id, status="running", stage="fetching")
            result = ingest(url)
        result["url_key"] = url_key
        t_fetch = time.time() - t0

        _set_job(job_id, stage="routing")
        posted = result.get("posted_date")
        claims = route_claims(
            result["transcript"],
            title=result["title"],
            platform=result["platform"],
            uploader=result["uploader"],
        )
        for c in claims:
            c["posted_date"] = posted
        try:
            save_route_audit(url_key, url, claims, ROUTER_MODEL, TAXONOMY_VERSION)
        except Exception:
            pass

        t_route = time.time() - t0 - t_fetch

        def _verify(i: int) -> None:
            try:
                claims[i]["evidence"] = gather_evidence(claims[i]["claim"])
            except Exception:
                claims[i]["evidence"] = {"fact_checks": [], "web_sources": []}
            try:
                claims[i]["verdict"] = judge_with_rubric(claims[i], claims[i]["evidence"])
            except Exception:
                claims[i]["verdict"] = {
                    "truth_score": None,
                    "verdict_state": "unverifiable",
                    "verdict": "Judge step failed; try again.",
                    "evidence_strength": "none",
                    "key_sources": [],
                }
            # live update: this claim's card fills in immediately
            claims[i]["verifying"] = False
            _publish_partial(job_id, result, claims)

        selected = select_for_verification(claims, MAX_CLAIMS_WITH_EVIDENCE)
        for i in selected:
            claims[i]["verifying"] = True
        _set_job(job_id, stage="judging")
        _publish_partial(job_id, result, claims)
        if selected:
            # stagger launches ~0.5s apart: a burst of simultaneous API
            # calls can trip rate limits (the Lindsey Graham incident);
            # the evidence agent's retry ladder covers the rest
            def _verify_staggered(args):
                pos, idx = args
                time.sleep(pos * 0.5)
                _verify(idx)

            with ThreadPoolExecutor(max_workers=len(selected)) as ex:
                list(ex.map(_verify_staggered, enumerate(selected)))

        _set_job(job_id, stage="assembling")
        t_verify = time.time() - t0 - t_fetch - t_route
        for c in claims:
            c.pop("verifying", None)
        result["claims"] = claims
        result = build_report(result)
        result["timings"] = {
            "fetch_s": round(t_fetch, 1),
            "route_s": round(t_route, 1),
            "verify_s": round(t_verify, 1),
            "total_s": round(time.time() - t0, 1),
        }
        result["cached"] = False
        save_result(url_key, url, result)
        _set_job(job_id, status="done", result=result)
    except (IngestError, RouterError) as e:
        _set_job(job_id, status="error", error=str(e))
    except Exception:
        _set_job(job_id, status="error",
                 error="Unexpected error while checking this link.")


# ------------------------------------------------------------ routes


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return _page()


@app.get("/r/{key:path}", response_class=HTMLResponse)
def permalink_page(key: str) -> str:
    # same single-page app; its JS loads /api/result/<key>
    return _page()


class CheckRequest(BaseModel):
    url: str
    force: bool = False  # true = ignore the cache and re-run (recheck)
    captcha_token: str = ""  # Turnstile token (required when captcha is on)


@app.post("/api/check")
def api_check(req: CheckRequest, request: Request):
    """Start a check of a video URL OR a typed claim."""
    raw = (req.url or "").strip()
    if not looks_like_url(raw):
        if len(raw) < 12:
            return JSONResponse(
                status_code=422,
                content={"detail": "Type a full claim to check (a sentence), "
                                   "or paste a video link."},
            )
        url_key = text_key(raw)
    else:
        url_key = canonical_key(raw)
    cached = None if req.force else get_cached(url_key)
    if cached is not None:
        cached.setdefault("url_key", url_key)
        if "report" not in cached:  # results stored before v0.9
            build_report(cached)
        return cached

    # ---- armor: cached results above stay free & unlimited; fresh runs
    # must pass the bot check, per-visitor rate limit, and daily budget ----
    if not _verify_turnstile(req.captcha_token, _client_ip(request)):
        return JSONResponse(
            status_code=403,
            content={"detail": (
                "Bot check failed — please try again (the checkbox may "
                "have expired)."
            )},
        )
    if _rate_limited(_client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"detail": (
                f"You've reached the limit of {RATE_LIMIT_PER_HOUR} new "
                "checks per hour. Already-checked videos are always "
                "available instantly — or come back in a bit."
            )},
        )
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return JSONResponse(
            status_code=503,
            content={"detail": (
                "Glowby has reached its daily budget for new checks — "
                "they'll resume tomorrow. Already-checked videos still "
                "load instantly."
            )},
        )
    add_usage(COST_PER_CHECK_EST)

    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="queued", stage="fetching", started=time.time())
    threading.Thread(
        target=_run_pipeline, args=(job_id, req.url, url_key), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return JSONResponse(status_code=404, content={"detail": "Unknown job."})
    # watchdog: a job stuck past the timeout reports an honest error
    started = job.get("started")
    if job.get("status") in ("queued", "running") and started             and time.time() - started > JOB_TIMEOUT_SECONDS:
        job = {"status": "error",
               "error": "This check took too long and was stopped. "
                        "Please try again."}
    job.pop("started", None)
    return job


@app.get("/api/result/{key:path}")
def api_result(key: str):
    cached = get_cached(key)
    if cached is None:
        return JSONResponse(status_code=404, content={"detail": "No stored result."})
    cached.setdefault("url_key", key)
    if "report" not in cached:
        build_report(cached)
    return cached


@app.post("/api/ingest")
def api_ingest(req: CheckRequest):
    """Transcript only (kept for testing the ingest stage in isolation)."""
    try:
        return ingest(req.url)
    except IngestError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while processing this link."},
        )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "glowby", "version": VERSION}
