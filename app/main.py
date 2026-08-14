"""
Glowby — multi-agent misinformation detection & fact-checking.

v1 scope: paste a link, get a fact-check.
Pipeline: ingest -> Sorting Gate/Router (13 buckets) -> evidence (parallel)
-> category judge engine (13 rubrics, truth score 0.0-9.9) -> Output agent
(headline = MIN, safety collapse) -> Postgres cache + permalinks.

v0.9: async job queue with progress stages, Design 2 list UI with Design 3
reel toggle, shareable permalinks (/r/<key>), report-a-mistake.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agents.answer import answer_followup, answer_question
from app.agents.evidence import gather_evidence, search_fact_check_db
from app.agents.ingest import IngestError, ingest
from app.agents.judge import judge_with_rubric
from app.agents.vision import describe_frames
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
    admin_recent_checks,
    canonical_key,
    daily_usage_series,
    event_stats,
    get_cached,
    record_event,
    record_visitor,
    visitor_series,
    visitor_total,
    list_mistake_reports,
    list_recent_checks,
    looks_like_url,
    quality_stats,
    resolve_mistake_report,
    save_mistake_report,
    save_result,
    save_route_audit,
    text_key,
    today_usage,
    total_fresh_checks,
)

VERSION = "0.30.0"

# evidence+judgment run for the top N claims by risk (cost control)
MAX_CLAIMS_WITH_EVIDENCE = 3

# ---- armor knobs (all overridable via Railway Variables) ----
DAILY_BUDGET_USD = float(os.environ.get("GLOWBY_DAILY_BUDGET_USD", "10"))
COST_PER_CHECK_EST = float(os.environ.get("GLOWBY_COST_PER_CHECK_EST", "0.15"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("GLOWBY_RATE_LIMIT_PER_HOUR", "25"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("GLOWBY_JOB_TIMEOUT_SECONDS", "480"))
# cached verdicts expire after this many days on the CHECK path, so a
# re-pasted link gets fresh evidence; share links (/r/) never expire.
CACHE_TTL_DAYS = int(os.environ.get("GLOWBY_CACHE_TTL_DAYS", "7"))

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


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline browser-security headers on every response."""
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "microphone=(self), camera=(), geolocation=(), payment=()")
    h.setdefault("Strict-Transport-Security",
                 "max-age=31536000; includeSubDomains")
    return resp

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
    """Push a live snapshot so the UI can show claims as they finish.

    Never raises: verifier threads mutate `claims` concurrently, and a
    rare mid-copy race must not kill the whole check — the next
    snapshot (or the final result) supersedes a skipped one anyway.
    """
    import copy

    try:
        _set_job(job_id, partial={
            "platform": result.get("platform"),
            "title": result.get("title"),
            "uploader": result.get("uploader"),
            "posted_date": result.get("posted_date"),
            "transcript_source": result.get("transcript_source"),
            "claims": copy.deepcopy(claims),
        })
    except Exception:
        pass



QUESTION_STARTERS = {
    "who", "whos", "what", "whats", "when", "where", "why", "how",
    "is", "are", "was", "were", "am", "do", "does", "did",
    "can", "could", "will", "would", "should", "has", "have", "had",
}


def _looks_like_question(text: str) -> bool:
    """Cheap, deterministic question detector for typed input."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.endswith("?"):
        return True
    first = t.split(" ", 1)[0].strip(",.!")
    return first in QUESTION_STARTERS


def _run_pipeline(job_id: str, url: str, url_key: str,
                  user_question: str = "") -> None:
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

        # THE EYES: a thin transcript with sampled frames means the video
        # makes its claims visually (animation, chart, on-screen text).
        # The vision agent describes what the video asserts; that
        # description enters the pipeline like any transcript.
        frames = result.pop("frames", None)
        if frames:
            # speculative vision may have already looked during ingest
            desc = result.pop("visual_desc", None) or describe_frames(
                frames, result.get("title") or "", result.get("uploader") or "")
            if desc:
                base = (result.get("transcript") or "").strip()
                tag = ("[WHAT THE PHOTO POST'S COVER SLIDE SHOWS] "
                       if result.get("photo_post")
                       else "[WHAT THE VIDEO VISUALLY SHOWS] ")
                visual = tag + desc
                result["transcript"] = (base + "\n\n" + visual) if base else visual
                result["transcript_source"] = (
                    (result.get("transcript_source") or "none").replace("none", "")
                    + ("+cover slide only" if result.get("photo_post")
                       else "+visual analysis")).lstrip("+")
            elif not (result.get("transcript") or "").strip():
                _set_job(job_id, status="error", error=(
                    "This video has no speech or captions, and its visuals "
                    "don't assert anything checkable."))
                return
        t_fetch = time.time() - t0

        _set_job(job_id, stage="routing")
        posted = result.get("posted_date")
        claims = route_claims(
            result["transcript"],
            title=result["title"],
            platform=result["platform"],
            uploader=result["uploader"],
        )

        # SECOND LOOK: rich audio but ZERO checkable claims in it — the
        # claim may live in on-screen text (overlay captions, headlines).
        # Open the eyes on the standby frames and route again before
        # declaring "nothing to verify."
        standby = result.pop("frames_standby", None)
        if standby and not any(
                c.get("gate_label") in ("factual", "prediction")
                for c in (claims or [])):
            desc = describe_frames(
                standby, result.get("title") or "", result.get("uploader") or "")
            if desc:
                result["transcript"] = (
                    (result.get("transcript") or "").strip()
                    + "\n\n[WHAT THE VIDEO VISUALLY SHOWS] " + desc)
                result["transcript_source"] = (
                    (result.get("transcript_source") or "")
                    + "+visual analysis").lstrip("+")
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

        # ANSWER MODE: a typed QUESTION gets a sourced answer, not a
        # rating — a question isn't true or false. Question-SHAPED input
        # ("how many...", ends in "?") triggers this deterministically;
        # the AI gate's question label is the fallback for the rest.
        if (url_key.startswith("text:") and (
                _looks_like_question(url)
                or (claims
                    and any(c.get("gate_label") == "question" for c in claims)
                    and not any(c.get("gate_label") in ("factual", "prediction")
                                for c in claims)))):
            _set_job(job_id, stage="judging")
            q = result["transcript"]
            try:
                evidence = gather_evidence(q)
            except Exception:
                evidence = {"fact_checks": [], "web_sources": []}
            ans = answer_question(q, evidence)
            result["claims"] = claims
            result["answer_mode"] = True
            result["question"] = q
            result["answer"] = ans or (
                "Glowby couldn't find enough reliable evidence to answer "
                "this question — try rephrasing it, or check back later.")
            result["evidence"] = evidence
            result["report"] = {
                "headline_score": None,
                "headline_state": "answer",
                "headline_label": "Answer",
                "share_text": f"Glowby answered: “{q}”",
                "counts": {"claim_units": len(claims), "judged": 0,
                           "not_judged": 0, "parked": len(claims)},
                "safety_notice": None,
            }
            result["timings"] = {"total_s": round(time.time() - t0, 1)}
            result["cached"] = False
            save_result(url_key, url, result)
            _set_job(job_id, status="done", result=result)
            return

        t_route = time.time() - t0 - t_fetch

        def _verify(i: int) -> None:
            done = False
            if url_key.startswith("text:"):
                # FAST LANE (typed claims): professional fact-checkers may
                # have already reviewed this exact claim — a ~1s database
                # lookup. If their review squarely settles it (a score AND
                # strong evidence), skip the slow web hunt entirely.
                # Anything less falls through to the full search.
                try:
                    fcs = search_fact_check_db(claims[i]["claim"])
                    if fcs:
                        ev = {"fact_checks": fcs, "web_sources": [],
                              "fast_lane": True}
                        v = judge_with_rubric(claims[i], ev)
                        if (v.get("truth_score") is not None
                                and v.get("evidence_strength") == "strong"):
                            claims[i]["evidence"] = ev
                            claims[i]["verdict"] = v
                            done = True
                except Exception:
                    done = False
            if not done:
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
            # stagger launches ~0.3s apart: a burst of simultaneous API
            # calls can trip rate limits (the Lindsey Graham incident);
            # the evidence agent's retry ladder covers the rest
            def _verify_staggered(args):
                pos, idx = args
                time.sleep(pos * 0.3)
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
        # "+ask": the check is DONE — now answer the user's question FROM
        # the completed analysis (attached after save, so the shared cache
        # never carries one person's question).
        if user_question:
            try:
                ans = answer_followup(user_question, _followup_context(result))
                if ans:
                    add_usage(0.02)
                    result["user_question"] = user_question
                    result["user_answer"] = ans
            except Exception:
                pass
        _set_job(job_id, status="done", result=result)
    except (IngestError, RouterError) as e:
        _set_job(job_id, status="error", error=str(e))
    except Exception:
        _set_job(job_id, status="error",
                 error="Unexpected error while checking this link.")


# ------------------------------------------------------------ routes


_INDEX_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")
_index_cache = None


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    """One unified page — the glowing checker IS the front door."""
    _count_visitor(request)
    return _page()


@app.get("/app", response_class=HTMLResponse)
def checker(request: Request) -> str:
    """The checker itself; ?u=<link> arrives prefilled from the landing."""
    _count_visitor(request)
    return _page()


_ABOUT_PATH = os.path.join(os.path.dirname(__file__), "templates", "about.html")
_about_cache = None


@app.get("/about-us", response_class=HTMLResponse)
def about_us() -> str:
    """About page: mission, values, founder, contact."""
    global _about_cache
    if _about_cache is None:
        with open(_ABOUT_PATH, encoding="utf-8") as f:
            _about_cache = f.read()
    return _about_cache


_TRUST_PATH = os.path.join(os.path.dirname(__file__), "templates", "trust.html")
_trust_cache = None


@app.get("/about", response_class=HTMLResponse)
def about() -> str:
    """Trust pages: methodology, ratings, corrections, terms, privacy."""
    global _trust_cache
    if _trust_cache is None:
        with open(_TRUST_PATH, encoding="utf-8") as f:
            _trust_cache = f.read()
    return _trust_cache


@app.get("/r/{key:path}", response_class=HTMLResponse)
def permalink_page(key: str, request: Request) -> str:
    # same single-page app; its JS loads /api/result/<key>
    _count_visitor(request)
    return _page()


class CheckRequest(BaseModel):
    url: str
    force: bool = False  # true = ignore the cache and re-run (recheck)
    captcha_token: str = ""  # Turnstile token (required when captcha is on)
    question: str = ""  # optional "+ask": what the user wants to know


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
    question = (req.question or "").strip()[:400]
    cached = None if req.force else get_cached(url_key, CACHE_TTL_DAYS)
    if cached is not None:
        cached.setdefault("url_key", url_key)
        if "report" not in cached:  # results stored before v0.9
            build_report(cached)
        # "+ask": answer the user's question from the cached check. The
        # answer is a paid AI call, so it shares the rate limit + budget.
        if question:
            if _rate_limited(_client_ip(request)):
                cached["user_question"] = question
                cached["user_answer"] = (
                    "You've hit the hourly limit, so this question wasn't "
                    "answered — but the full check is above.")
                return cached
            _, spent = today_usage()
            if spent < DAILY_BUDGET_USD:
                ans = answer_followup(question, _followup_context(cached))
                if ans:
                    add_usage(0.02)
                    cached["user_question"] = question
                    cached["user_answer"] = ans
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
        target=_run_pipeline, args=(job_id, req.url, url_key, question),
        daemon=True,
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


class FollowupRequest(BaseModel):
    url_key: str
    question: str


def _followup_context(result: dict) -> str:
    """Compact text bundle of a stored check for the follow-up agent."""
    import json as _json

    report = result.get("report") or {}
    claims = []
    for c in (result.get("claims") or [])[:8]:
        v = c.get("verdict") or {}
        claims.append({
            "claim": c.get("claim"),
            "category": c.get("gate_label"),
            "central": c.get("central"),
            "truth_score": v.get("truth_score"),
            "verdict_state": v.get("verdict_state"),
            "verdict": v.get("verdict"),
            "sources": (v.get("key_sources") or [])[:4],
        })
    bundle = {
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "platform": result.get("platform"),
        "posted_date": result.get("posted_date"),
        "headline_score": report.get("headline_score"),
        "headline_label": report.get("headline_label"),
        "claims": claims,
        "answer": result.get("answer"),
        "transcript_excerpt": (result.get("transcript") or "")[:2500],
    }
    return _json.dumps(bundle, indent=1)[:14000]


@app.post("/api/followup")
def api_followup(req: FollowupRequest, request: Request):
    """Follow-up question about a finished check. Armored: it's a paid
    AI call, so it shares the per-IP rate limit and daily budget."""
    q = (req.question or "").strip()
    if len(q) < 3:
        return JSONResponse(status_code=422, content={"detail": "Type a question."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"detail": "You've hit the hourly limit — try again soon."})
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return JSONResponse(
            status_code=503,
            content={"detail": "Glowby's daily budget is used up — back tomorrow."})
    stored = get_cached(req.url_key)
    if stored is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "This check isn't stored (it may have been "
                               "checked before saving existed). Re-run it first."})
    if "report" not in stored:
        build_report(stored)
    text = answer_followup(q, _followup_context(stored))
    if not text:
        return JSONResponse(
            status_code=500,
            content={"detail": "Couldn't answer right now — try again."})
    add_usage(0.02)  # small single-call cost
    return {"answer": text}


@app.post("/api/ingest")
def api_ingest(req: CheckRequest, request: Request):
    """Transcript only (kept for testing the ingest stage in isolation).

    Armored like /api/check: this endpoint triggers paid downloads and
    Whisper transcription, so it must never be a free side door around
    the bot check, rate limit, and daily budget.
    """
    if not _verify_turnstile(req.captcha_token, _client_ip(request)):
        return JSONResponse(status_code=403, content={"detail": "Bot check failed."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(status_code=429, content={"detail": "Rate limit reached."})
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return JSONResponse(status_code=503, content={"detail": "Daily budget reached."})
    try:
        return ingest(req.url)
    except IngestError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while processing this link."},
        )


# ------------------------------------------------------ sidebar feed
_recent_cache = {"t": 0.0, "data": []}


@app.get("/api/recent")
def api_recent():
    """Latest checks for the sidebar. Cached 60s so the DB isn't hammered."""
    now = time.time()
    if now - _recent_cache["t"] > 60:
        _recent_cache["data"] = list_recent_checks(12)
        _recent_cache["t"] = now
    return {"checks": _recent_cache["data"]}


# ------------------------------------------------------ quality loop
# Reports are STORED with a status (new -> reviewed/fixed/rejected), not
# just emailed — the corrections policy with tracking behind it.

ADMIN_KEY = os.environ.get("GLOWBY_ADMIN_KEY", "")
ADMIN_USER = os.environ.get("GLOWBY_ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("GLOWBY_ADMIN_PASSWORD", "")

# login sessions: token -> expiry. In-process, so a redeploy logs the
# admin out — acceptable; the login screen is one step away.
_admin_sessions: dict = {}
_ADMIN_SESSION_TTL = 7 * 24 * 3600  # 7 days


def _admin_ok(key: str) -> bool:
    """True for the master key OR a live login-session token."""
    if not key:
        return False
    if bool(ADMIN_KEY) and hmac.compare_digest(key, ADMIN_KEY):
        return True
    exp = _admin_sessions.get(key)
    if exp is None:
        return False
    if exp < time.time():
        _admin_sessions.pop(key, None)
        return False
    return True


_EVENT_KINDS = {"copy_result", "copy_link"}


class UiEvent(BaseModel):
    kind: str


@app.post("/api/event")
def api_event(ev: UiEvent):
    """Anonymous UI event counter (e.g. someone copied a result).
    Only allowlisted kinds are counted; nothing about who is stored."""
    if ev.kind not in _EVENT_KINDS:
        return {"ok": False}
    threading.Thread(target=record_event, args=(ev.kind,), daemon=True).start()
    return {"ok": True}


class AdminLogin(BaseModel):
    username: str = ""
    password: str


@app.post("/api/admin/login")
def api_admin_login(req: AdminLogin):
    """Username+password login. Falls back to the master key as the
    password when no GLOWBY_ADMIN_USER is configured yet."""
    ok = False
    if ADMIN_USER and ADMIN_PASSWORD:
        ok = (hmac.compare_digest(req.username.strip(), ADMIN_USER)
              and hmac.compare_digest(req.password, ADMIN_PASSWORD))
    if not ok and bool(ADMIN_KEY) and hmac.compare_digest(req.password, ADMIN_KEY):
        ok = True  # master key always unlocks, username ignored
    if not ok:
        time.sleep(1)  # slow down guessing
        return JSONResponse(status_code=403, content={"detail": "Wrong login."})
    # prune dead sessions, then issue a fresh token
    now = time.time()
    for t in [t for t, e in _admin_sessions.items() if e < now]:
        _admin_sessions.pop(t, None)
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = now + _ADMIN_SESSION_TTL
    return {"token": token, "expires_in": _ADMIN_SESSION_TTL}


# ---- privacy-first visitor counting ----
# The hash mixes the date in, so the same person hashes differently
# every day: daily counts exist, cross-day tracking is impossible,
# and no raw IPs are ever stored.


def _count_visitor(request) -> None:
    try:
        ip = _client_ip(request)
        day = time.strftime("%Y-%m-%d")
        salt = ADMIN_KEY or "glowby"
        vh = hashlib.sha256(f"{salt}:{day}:{ip}".encode()).hexdigest()[:32]
        threading.Thread(target=record_visitor, args=(vh,), daemon=True).start()
    except Exception:
        pass


class MistakeReport(BaseModel):
    url_key: str = ""
    url: str = ""
    message: str
    contact: str = ""


@app.post("/api/report")
def api_report(rep: MistakeReport, request: Request):
    msg = (rep.message or "").strip()
    if len(msg) < 10:
        return JSONResponse(status_code=422, content={
            "detail": "Tell us what looks wrong (a sentence or two)."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(status_code=429, content={"detail": "Too many requests."})
    saved = save_mistake_report(rep.url_key, rep.url, msg, rep.contact)
    return {"ok": True, "stored": saved}


@app.get("/api/admin/reports")
def api_admin_reports(key: str = "", status: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"reports": list_mistake_reports(status)}


class ResolveRequest(BaseModel):
    key: str
    report_id: int
    status: str  # reviewed | fixed | rejected
    note: str = ""


@app.post("/api/admin/resolve")
def api_admin_resolve(req: ResolveRequest):
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if req.status not in ("new", "reviewed", "fixed", "rejected"):
        return JSONResponse(status_code=422, content={"detail": "Bad status."})
    return {"ok": resolve_mistake_report(req.report_id, req.status, req.note)}


@app.get("/api/admin/stats")
def api_admin_stats(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    checks, spent = today_usage()
    stats = quality_stats()
    stats["today"] = {"fresh_checks": checks, "est_spend_usd": spent}
    return stats


@app.get("/api/admin/dashboard")
def api_admin_dashboard(key: str = ""):
    """Everything the /admin page needs in one call."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    checks, spent = today_usage()
    daily = daily_usage_series(14)
    visitors = visitor_series(14)
    for d in daily:
        d["visitors"] = visitors.get(d["day"], 0)
    today_key = time.strftime("%Y-%m-%d")
    return {
        "version": VERSION,
        "today": {
            "fresh_checks": checks,
            "est_spend_usd": round(spent, 2),
            "budget_usd": DAILY_BUDGET_USD,
            "visitors": visitors.get(today_key, 0),
        },
        "visitors_total": visitor_total(),
        "total_checks": total_fresh_checks(),
        "cache_ttl_days": CACHE_TTL_DAYS,
        "events": event_stats(),
        "stats": quality_stats(),
        "daily": daily,
        "recent": admin_recent_checks(25),
        "reports": list_mistake_reports(),
    }


@app.get("/api/admin/rescue-test")
def api_admin_rescue_test(key: str = "", url: str = ""):
    """Diagnostic: run the rescue tier on one URL and report exactly what
    came back — so an Instagram/TikTok miss can be seen, not guessed.
    Admin-only."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents.ingest import detect_platform
    from app.agents import rescue as _rescue

    platform = detect_platform(url)
    out = {
        "url": url,
        "platform": platform,
        "token_present": bool(_rescue.RESCUE_TOKEN),
        "daily_calls_used": (event_stats().get("rescue") or {}).get("today", 0),
        "daily_calls_cap": _rescue.RESCUE_DAILY_CALLS,
    }
    if not _rescue.RESCUE_TOKEN:
        out["result"] = "NO TOKEN — set GLOWBY_RESCUE_TOKEN in Railway."
        return out
    if platform == "instagram":
        import re as _re
        m = _re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
        out["shortcode"] = m.group(1) if m else None
        raw = _rescue._ed_get("/instagram/post/details",
                              {"code": m.group(1), "n_comments_to_fetch": 0}) if m else None
    elif platform == "tiktok":
        raw = _rescue._ed_get("/tt/post/info", {"url": url})
    else:
        out["result"] = f"platform '{platform}' has no rescue path."
        return out
    out["provider_returned_data"] = raw is not None
    # top-level keys only, so the response stays small and safe to view
    if isinstance(raw, dict):
        out["top_level_keys"] = sorted(raw.keys())[:40]
        out["found_video_url"] = bool(_rescue._deep_find_video(raw))
    elif isinstance(raw, list):
        out["top_level_keys"] = f"list of {len(raw)}"
        out["found_video_url"] = bool(_rescue._deep_find_video(raw))
    else:
        out["top_level_keys"] = None
        out["found_video_url"] = False
        out["hint"] = ("Provider returned nothing — token may be invalid, "
                       "out of units, or the post is private/deleted.")
    return out


_ADMIN_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "templates", "admin.html")
_admin_template_cache = None


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page() -> str:
    """Admin dashboard shell. Renders nothing sensitive until the admin
    key is entered client-side and verified against /api/admin/dashboard."""
    global _admin_template_cache
    if _admin_template_cache is None:
        with open(_ADMIN_TEMPLATE_PATH, encoding="utf-8") as f:
            _admin_template_cache = f.read()
    return _admin_template_cache


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "glowby", "version": VERSION}


# ------------------------------------------------------------ PWA assets
# These make glowby.io installable ("Add to Home Screen") on iPhone and
# Android. The service worker is deliberately cache-free so nothing is
# ever served stale. Served from root paths so scope covers the whole app.
_STATIC = os.path.join(os.path.dirname(__file__), "static")

_PWA_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "application/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-512-maskable.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


def _pwa_file(name: str) -> FileResponse:
    return FileResponse(
        os.path.join(_STATIC, name),
        media_type=_PWA_FILES[name],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest() -> FileResponse:
    return _pwa_file("manifest.webmanifest")


@app.get("/sw.js", include_in_schema=False)
def pwa_sw() -> FileResponse:
    # Service worker must never be cached long, or updates lag for a day.
    resp = _pwa_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/icon-192.png", include_in_schema=False)
def pwa_icon_192() -> FileResponse:
    return _pwa_file("icon-192.png")


@app.get("/icon-512.png", include_in_schema=False)
def pwa_icon_512() -> FileResponse:
    return _pwa_file("icon-512.png")


@app.get("/icon-512-maskable.png", include_in_schema=False)
def pwa_icon_maskable() -> FileResponse:
    return _pwa_file("icon-512-maskable.png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def pwa_apple_icon() -> FileResponse:
    return _pwa_file("apple-touch-icon.png")


# ------------------------------------------------------------ pre-check
# Trending news videos get fact-checked BEFORE anyone pastes them, so
# the most-likely pastes are cache hits (~1s). See app/precheck.py.

from app.precheck import start_precheck  # noqa: E402


@app.on_event("startup")
def _startup_precheck() -> None:
    start_precheck(_run_pipeline, DAILY_BUDGET_USD, COST_PER_CHECK_EST)
