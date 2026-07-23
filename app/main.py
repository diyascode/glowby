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
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
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
from app.storage import canonical_key, get_cached, save_result, save_route_audit

VERSION = "0.9.3"

# evidence+judgment run for the top N claims by risk (cost control)
MAX_CLAIMS_WITH_EVIDENCE = 3

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
            _template_cache = f.read()
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


def _run_pipeline(job_id: str, url: str, url_key: str) -> None:
    try:
        _set_job(job_id, status="running", stage="fetching")
        result = ingest(url)
        result["url_key"] = url_key

        _set_job(job_id, stage="routing")
        claims = route_claims(
            result["transcript"],
            title=result["title"],
            platform=result["platform"],
            uploader=result["uploader"],
        )
        try:
            save_route_audit(url_key, url, claims, ROUTER_MODEL, TAXONOMY_VERSION)
        except Exception:
            pass

        _set_job(job_id, stage="judging")

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

        selected = select_for_verification(claims, MAX_CLAIMS_WITH_EVIDENCE)
        if selected:
            with ThreadPoolExecutor(max_workers=len(selected)) as ex:
                list(ex.map(_verify, selected))

        _set_job(job_id, stage="assembling")
        result["claims"] = claims
        result = build_report(result)
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


@app.post("/api/check")
def api_check(req: CheckRequest):
    """Start a check. Cached -> full result immediately; else a job id."""
    url_key = canonical_key(req.url)
    cached = None if req.force else get_cached(url_key)
    if cached is not None:
        cached.setdefault("url_key", url_key)
        if "report" not in cached:  # results stored before v0.9
            build_report(cached)
        return cached

    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="queued", stage="fetching")
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
