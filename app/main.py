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
import re
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.agents.answer import answer_followup, answer_question
from app.agents.authenticity import assess_stage1, merge_stage2
from app.agents import hive_detect
from app.agents import reverse_search
from app.agents.evidence import gather_evidence, search_fact_check_db
from app.agents import safety
from app.agents import scam
from app.agents.detection import run_media_detection
from app.agents.ingest import IngestError, ingest
from app.agents.judge import judge_with_rubric
from app.agents.vision import describe_frames
from app.agents.output import build_report
from app.agents.summary import one_line as _one_line
from app.agents.router import (
    MODEL as ROUTER_MODEL,
    TAXONOMY_VERSION,
    RouterError,
    route_claims,
    select_for_verification,
)
from app.storage import (
    add_usage,
    add_video_timing,
    admin_recent_checks,
    cache_available,
    canonical_key,
    patch_result,
    is_short_link,
    legacy_key,
    resolve_short_link,
    daily_usage_series,
    event_stats,
    get_cached,
    record_event,
    record_visitor,
    record_visitor_month,
    visitor_monthly,
    month_calendar,
    day_detail,
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
    save_feedback, feedback_summary, list_feedback, resolve_feedback, feedback_daily, FEEDBACK_KINDS,
    save_scam_audit, load_scam_audit, scam_audit_stats,
    save_scam_sample, review_scam_sample, list_scam_samples, scam_sample_stats,
    save_exam_cases, load_exam_cases, exam_case_counts, list_shadow_scams,
    pending_flags, load_result_quiet, save_review, latest_review, last_review_at,
    hide_from_trending, delete_result, save_calibration, latest_calibration, reader_labelled_media,
)

VERSION = "0.66.12"

# ---- Media Authenticity Engine (Day 1: Stage-1 free checks) ----
# OFF by default. Set GLOWBY_AUTHENTICITY=1 in Railway to attach the
# authenticity lane to results (categories only, never percentages).
AUTHENTICITY_ENABLED = os.environ.get("GLOWBY_AUTHENTICITY", "") == "1"

# evidence+judgment run for the top N claims by risk (cost control)
MAX_CLAIMS_WITH_EVIDENCE = 3

# ---- armor knobs (all overridable via Railway Variables) ----
DAILY_BUDGET_USD = float(os.environ.get("GLOWBY_DAILY_BUDGET_USD", "30"))
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
            ).replace(
                # SCAM CHECK BUTTON (built Sept 15, parked by Diya the same
                # day: "save it for the future"). GLOWBY_SCAM_BUTTON=1 in
                # Railway shows it; everything behind it keeps working.
                "__SCAM_BUTTON__", "" if os.environ.get("GLOWBY_SCAM_BUTTON", "").strip() == "1" else "hidden"
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


def _stop_words():
    return {"the", "a", "an", "of", "to", "in", "on", "as", "is", "was",
            "were", "and", "or", "that", "this", "it", "for", "by", "with"}


def _has_real_evidence(ev) -> bool:
    if not isinstance(ev, dict):
        return False
    if ev.get("fact_checks"):
        return True
    return any((w or {}).get("stance") in ("supports", "refutes", "mixed")
               for w in (ev.get("web_sources") or []))


def _sibling_rescue(claims, selected) -> int:
    """SIBLING RESCUE — the Cybercab incident: claims 2 and 3 of a video
    found the NHTSA press release and NYT coverage; claim 1's own search
    came back empty and the judge scored it 2.5 for "silence", which
    became the headline. Now, after every claim is judged, a claim with
    NO real evidence of its own is judged AGAIN with the sources its
    sibling claims found (tagged as such, stance downgraded to context).
    No extra search; one extra judge call, only on the failing case.
    Returns the number of claims re-judged."""
    pool, seen = [], set()
    for i in selected:
        ev = claims[i].get("evidence") or {}
        for w in ev.get("web_sources") or []:
            u = (w or {}).get("url")
            if u and u not in seen and not w.get("from_sibling"):
                seen.add(u)
                pool.append(dict(w, stance="context", from_sibling=True,
                                 sibling_claim=(claims[i].get("claim") or "")[:140]))
    if not pool:
        return 0
    rescued = 0
    for i in selected:
        c = claims[i]
        ev = c.get("evidence") or {}
        v = c.get("verdict") or {}
        if _has_real_evidence(ev):
            continue
        if v.get("verdict_state") not in ("insufficient", "unverifiable", "contradicted"):
            continue
        own = {(w or {}).get("url") for w in (ev.get("web_sources") or [])}
        extra = [w for w in pool if w["url"] not in own][:6]
        if not extra:
            continue
        ev2 = dict(ev)
        ev2["web_sources"] = list(ev.get("web_sources") or []) + extra
        ev2["sibling_pool"] = True
        try:
            v2 = judge_with_rubric(c, ev2)
        except Exception:
            continue
        if isinstance(v2, dict) and v2.get("verdict_state"):
            c["evidence"] = ev2
            c["verdict"] = v2
            c["sibling_rescued"] = True
            rescued += 1
    return rescued



def _merge_prior_evidence(claim_text: str, evidence: dict,
                          prior_claims) -> dict:
    """Union a fresh evidence bundle with the best-matching prior claim's
    stored sources (re-check memory). Dedup by URL; fresh sources first.
    Caps: 7 web sources, 4 fact-checks. Returns the evidence dict."""
    if not prior_claims or not isinstance(evidence, dict):
        return evidence
    stop = _stop_words()
    words = {w for w in (claim_text or "").lower().split() if w not in stop}
    if not words:
        return evidence
    best, best_score = None, 0.0
    for pc in prior_claims:
        pw = {w for w in str(pc.get("claim", "")).lower().split()
              if w not in stop}
        if not pw:
            continue
        overlap = len(words & pw) / len(words | pw)
        if overlap > best_score:
            best, best_score = pc, overlap
    if best is None or best_score < 0.45:
        return evidence
    prior_ev = best.get("evidence") or {}
    seen = {w.get("url") for w in (evidence.get("web_sources") or [])}
    merged_web = list(evidence.get("web_sources") or [])
    for w in (prior_ev.get("web_sources") or []):
        if w.get("url") and w["url"] not in seen:
            merged_web.append(w)
            seen.add(w["url"])
    seen_fc = {f.get("url") for f in (evidence.get("fact_checks") or [])}
    merged_fc = list(evidence.get("fact_checks") or [])
    for f in (prior_ev.get("fact_checks") or []):
        if f.get("url") and f["url"] not in seen_fc:
            merged_fc.append(f)
            seen_fc.add(f["url"])
    evidence["web_sources"] = merged_web[:7]
    evidence["fact_checks"] = merged_fc[:4]
    if len(merged_web) > len(evidence.get("web_sources") or []) or best_score:
        evidence["recheck_memory"] = True
    return evidence


# ---- last pipeline failures (admin diagnostic; never shown to readers) ----
_LAST_ERRORS = []


def _remember_error(url_key, exc, tb):
    try:
        _LAST_ERRORS.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "url_key": (url_key or "")[:120],
                             "type": type(exc).__name__, "message": str(exc)[:300],
                             "traceback": (tb or "")[-3000:]})
        del _LAST_ERRORS[:-8]
    except Exception:
        pass


# SCAM MODE — the switch that makes "add it and see what happens" safe:
#   off     the engine does not run at all
#   shadow  the engine runs on every check and records what it WOULD have
#           said (admin traces, stats, a review list) — readers see nothing
#   on      the card shows
# Default shadow: watch the verdicts for a week or two on real checks, then
# flip one Railway variable. Flip it back and nobody ever saw a card.
def scam_mode() -> str:
    m = (os.environ.get("GLOWBY_SCAM_MODE") or "shadow").strip().lower()
    return m if m in ("off", "shadow", "on") else "shadow"


def _scam_lens_start(result: dict):
    """SCAM LENS — the fourth thing Glowby looks for (app/agents/scam.py on
    app/agents/scamengine.py). A scam is a pattern (promise + pressure +
    ask), not a claim, so the truth score cannot catch it. The engine is
    text-only, so it starts in a thread as soon as the transcript exists
    and runs alongside routing and judging; the media lane's finding is
    folded in at the end (apply_media). Never allowed to break a check."""
    box = {}
    if scam_mode() == "off" and not result.get("scam_requested"):
        return None, box

    def _go():
        try:
            box["scam"] = scam.assess(
                title=result.get("title") or "",
                transcript=result.get("transcript") or "",
                uploader=result.get("uploader") or "",
                authenticity=None)
        except Exception as e:
            box["scam"] = {"risk": "none", "patterns": [], "ran": False, "error": str(e)[:120]}
    th = threading.Thread(target=_go, daemon=True)
    th.start()
    return th, box


def _scam_lens_finish(result: dict, started) -> None:
    th, box = started if started else (None, {})
    # SCAM CHECK MODE (Sept 15): the reader pressed the Scam check button —
    # the lens answers visibly for this check, whatever the global mode.
    asked = bool(result.get("scam_requested"))
    if scam_mode() == "off" and not asked:
        result["scam"] = {"risk": "none", "patterns": [], "ran": False, "mode": "off"}
        return
    try:
        if th is not None:
            th.join(timeout=25)
        sc = box.get("scam") or {"risk": "none", "patterns": [], "ran": False}
        sc = scam.apply_media(sc, result.get("authenticity") or {},
                              (result.get("title") or "") + "\n" + (result.get("transcript") or ""))
        result["scam"] = sc
        rep = sc.get("report") or {}
        aud = rep.get("audit") or {}
        # PRIVACY: a pasted, screenshotted or recorded message is someone's
        # private text — often with a victim's name, number or code in it.
        # What is STORED of it is redacted (codes, cards, SSNs, emails,
        # addresses, phones -> typed placeholders); the scammer's own
        # contact details survive in the engine's extracted evidence. Such a
        # result is never listed in Trending.
        try:
            from app.agents.scamengine import redact_pii, contains_pii
            uk = result.get("url_key") or ""
            if (uk.startswith("text:") or uk.startswith("img:") or uk.startswith("aud:")) and (
                    sc.get("risk") in ("low", "medium", "high") or contains_pii(result.get("transcript") or "")):
                result["transcript"] = redact_pii(result.get("transcript") or "")
                result["title"] = redact_pii(result.get("title") or "")
                result["content_rating"] = "private"
                result["redacted"] = True
        except Exception:
            pass
        if aud.get("model_extracted"):
            add_usage(0.003)
        if aud.get("queries"):
            add_usage(0.005 * aud["queries"])
        mode = "on" if asked else scam_mode()
        if rep.get("audit_trace_id"):
            try:
                save_scam_audit(rep["audit_trace_id"], "app" if mode == "on" else "app-shadow", rep,
                                hashlib.sha256(((result.get("title") or "") + (result.get("transcript") or "")).encode()).hexdigest()[:32])
            except Exception:
                pass
        if mode == "shadow":
            # readers see nothing; the full finding is kept on the result for
            # the admin review list, and the card renders as if it had not run
            result["scam_shadow"] = sc
            result["scam"] = {"risk": "none", "patterns": [], "ran": True, "mode": "shadow"}
    except Exception:
        result["scam"] = {"risk": "none", "patterns": [], "ran": False}


_fresh_reasons: dict = {}
_fresh_lock = threading.Lock()


def _run_pipeline(job_id: str, url: str, url_key: str,
                  user_question: str = "", prior_claims=None,
                  image_b64: str = None, detect_ai: bool = False,
                  ai_only: bool = False, audio_upload: str = None,
                  scam_check: bool = False) -> None:
    try:
        t0 = time.time()
        _rec_clip = None
        if audio_upload:
            # UPLOADED RECORDING (a voicemail, a call, a voice note): Whisper
            # hears it, the scam lens reads it, and the voice detector
            # listens for a cloned voice — the "grandma, it's me" scam.
            # PRIVACY: the recording is never stored; only its transcript.
            _set_job(job_id, status="running", stage="fetching")
            import base64 as _b64
            import tempfile as _tmp
            from app.agents.ingest import _whisper_file, _audio_clip_b64
            _data, _, _ext = audio_upload.rpartition("|")
            with _tmp.TemporaryDirectory() as _td:
                _fp = os.path.join(_td, "rec" + (_ext or ".m4a"))
                with open(_fp, "wb") as _fh:
                    _fh.write(_b64.b64decode(_data))
                try:
                    _text = _whisper_file(_fp)
                except IngestError as e:
                    _set_job(job_id, status="error", error=str(e))
                    return
                try:
                    _rec_clip = _audio_clip_b64(_fp, _td)
                except Exception:
                    _rec_clip = None
            if not _text:
                _set_job(job_id, status="done", result={
                    "url": None, "platform": "voice recording", "title": "Voice recording",
                    "uploader": "recording you uploaded", "duration_seconds": 0, "posted_date": None,
                    "transcript": None, "transcript_source": "whisper (recording)", "url_key": url_key,
                    "claims": [], "report": {"headline_score": None, "headline_state": "unverified",
                                             "headline_label": "Glowby couldn't hear any speech in that recording — try a clearer one, or type what was said.",
                                             "counts": {}}})
                return
            result = {
                "url": None, "platform": "voice recording",
                "title": ("Recording: " + _text[:80] + ("…" if len(_text) > 80 else "")),
                "uploader": "recording you uploaded", "duration_seconds": 0, "posted_date": None,
                "transcript": _text, "transcript_source": "whisper (recording)",
            }
        elif image_b64:
            # UPLOADED IMAGE (screenshot or camera photo): the EYES read
            # it, then it enters the normal pipeline like any transcript.
            # PRIVACY: the image itself is never stored — only the text
            # description Glowby derives from it.
            _set_job(job_id, status="running", stage="fetching")
            desc = describe_frames(
                [image_b64], title="(user-uploaded screenshot or photo)")
            if not desc:
                # NEVER a red error: a photo with nothing checkable gets a
                # normal, friendly result card. (App Review 2.1a, Aug 27:
                # reviewer picked an arbitrary photo, our honest "nothing
                # checkable" rendered as an error message and read as a bug.)
                result = {
                    "url": None,
                    "platform": "image",
                    "title": "Uploaded image",
                    "uploader": "uploaded image",
                    "duration_seconds": 0,
                    "posted_date": None,
                    "transcript": None,
                    "transcript_source": "visual analysis",
                    "url_key": url_key,
                    "claims": [],
                }
                build_report(result)
                result["report"]["nothing_to_check"] = None  # custom guidance below, not the chip
                result["report"]["headline_label"] = (
                    "Glowby looked at your image and didn't find a "
                    "checkable claim — no readable text, chart, or factual "
                    "statement. Try a screenshot of a post, a headline, or "
                    "a chart — or type the claim instead.")
                try:
                    save_result(url_key, result)
                except Exception:
                    pass
                _set_job(job_id, status="done", result=result)
                return
            _ttl = desc
            if desc.lstrip().startswith("[MESSAGE TEXT]"):
                # a screenshot of a text / email / chat: the title is the
                # message's first line, the exact transcription is the body
                _body = desc.lstrip()[len("[MESSAGE TEXT]"):].strip()
                _first = next((ln.strip() for ln in _body.splitlines() if ln.strip()), _body)
                _ttl = "Screenshot: " + _first
            result = {
                "url": None,
                "platform": "image",
                "title": (_ttl[:90] + "…") if len(_ttl) > 90 else _ttl,
                "uploader": "uploaded image",
                "duration_seconds": 0,
                "posted_date": None,
                "transcript": "[WHAT THE IMAGE SHOWS] " + desc,
                "transcript_source": "visual analysis",
            }
            if AUTHENTICITY_ENABLED:
                try:
                    result["authenticity"] = assess_stage1(
                        caption="", ocr_text=desc, image_b64=image_b64)
                except Exception:
                    pass  # the lane must never break a check
        elif url_key.startswith("text:"):
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
        if AUTHENTICITY_ENABLED and not url_key.startswith("text:") and not url_key.startswith("aud:"):
            try:
                _vis = ""
                _tr = result.get("transcript") or ""
                if "[WHAT THE VIDEO VISUALLY SHOWS]" in _tr:
                    _vis = _tr.split("[WHAT THE VIDEO VISUALLY SHOWS]", 1)[1]
                result["authenticity"] = assess_stage1(
                    caption=(result.get("title") or ""), ocr_text=_vis,
                    transcript=_tr, platform_label=result.get("platform_ai_label"))
            except Exception:
                pass

        # THE EYES: a thin transcript with sampled frames means the video
        # makes its claims visually (animation, chart, on-screen text).
        # The vision agent describes what the video asserts; that
        # description enters the pipeline like any transcript.
        frames = result.pop("frames", None)
        # keep a small copy for the Stage-2 forensic detector (Day 2).
        # frames_media carries frames the eyes already consumed during
        # ingest — without it, videos WITH visual analysis had nothing
        # left for the detector to look at.
        _media_frames = result.pop("frames_media", None)
        _audio_clip = result.pop("audio_clip_b64", None) or _rec_clip
        _au_frames = None
        _au_extra = None
        if AUTHENTICITY_ENABLED:
            _src = frames or _media_frames
            _au_frames = list(_src)[:6] if _src else None
            # frames 7-12 (scene-aware sampling) are held back for the
            # adaptive second pass — spent only on uncertain videos
            _au_extra = list(_src)[6:12] if _src and len(_src) > 6 else None
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

        # CONTENT GATE — before anything is routed, judged, stored or
        # listed. general: normal; mature: normal but never in Trending;
        # explicit: not fact-checked, not stored — AI check only, on a
        # PRIVATE path; possible minor: refused outright. See safety.py.
        if not url_key.startswith("text:"):
            _rating = safety.rate_content(
                result.get("title") or "", result.get("transcript") or "",
                result.get("uploader") or "")
            result["content_rating"] = _rating["rating"]
            if _rating["rating"] == "explicit":
                # never persist anything about this video
                result.pop("frames_standby", None)
                if _rating.get("minor_risk"):
                    _set_job(job_id, status="done", result={
                        "refused": "minor", "message": safety.MINOR_REFUSAL,
                        "resources": safety.RESOURCES_MINOR, "claims": [],
                        "report": {"headline_score": None, "headline_state": "refused",
                                   "headline_label": "Not processed", "counts": {}}})
                    return
                if not ai_only:
                    _set_job(job_id, status="done", result={
                        "explicit_offer": True, "message": safety.EXPLICIT_OFFER,
                        "url": url, "claims": [],
                        "report": {"headline_score": None, "headline_state": "unverified",
                                   "headline_label": "Adult content — not fact-checked",
                                   "counts": {}}})
                    return
                # PRIVATE AI-ONLY PATH: detector only; nothing kept.
                _set_job(job_id, stage="assembling")
                _auo = result.get("authenticity") or {}
                if AUTHENTICITY_ENABLED:
                    _auo = run_media_detection(
                        _auo,
                        frames=None if url_key.startswith("img:") else _au_frames,
                        extra_frames=_au_extra,
                        image_b64=image_b64 if url_key.startswith("img:") else None,
                        audio_b64=_audio_clip,
                        reason="private media-only check", allow_reverse=False,
                        posted_date=None, person_hint="person face")
                else:
                    _auo["stage2_status"] = "failed"
                    _auo["stage2_reason"] = "detector not configured"
                # no reverse image search on the private path — never push a
                # person's image into a web-matching service
                priv = {"private": True, "media_only": True, "content_rating": "explicit",
                        "url": None, "url_key": None, "title": "Private AI check",
                        "uploader": None, "transcript": "", "platform": result.get("platform"),
                        "authenticity": _auo, "claims": [],
                        "resources": safety.RESOURCES_ADULT,
                        "timings": {"total_s": round(time.time() - t0, 1)}, "cached": False}
                priv = build_report(priv)
                _set_job(job_id, status="done", result=priv)
                return

        _set_job(job_id, stage="routing")
        if scam_check:
            result["scam_requested"] = True
        _scam_started = _scam_lens_start(result)
        posted = result.get("posted_date")
        # re-check consistency: anchor claim-splitting to the prior run's
        # units so the same video carves into the same claims every time
        prior_units = [str(pc.get("claim", "")).strip()
                       for pc in (prior_claims or []) if pc.get("claim")]
        claims = route_claims(
            result["transcript"],
            title=result["title"],
            platform=result["platform"],
            uploader=result["uploader"],
            prior_units=prior_units or None,
        )

        # SECOND LOOK: rich audio but ZERO checkable claims in it — the
        # claim may live in on-screen text (overlay captions, headlines).
        # Open the eyes on the standby frames and route again before
        # declaring "nothing to verify."
        if scam_check and url_key.startswith(("text:", "img:", "aud:")):
            # SCAM CHECK on a pasted message, screenshot or recording: the
            # lens is the answer (≈4s); claims are not routed or judged.
            result.pop("frames_standby", None)
            _set_job(job_id, stage="assembling")
            result["claims"] = []
            result["scam_only"] = True
            _scam_lens_finish(result, _scam_started)
            result = build_report(result)
            result["report"]["headline_label"] = "Scam check — see the card above."
            result["report"]["nothing_to_check"] = None
            _ensure_one_line(result)
            result["timings"] = {"total_s": round(time.time() - t0, 1)}
            result["cached"] = False
            try:
                save_result(url_key, url, result)
            except Exception:
                pass
            _set_job(job_id, status="done", result=result)
            return

        if ai_only:
            # MEDIA-ONLY CHECK: the user asked "is this real?", not
            # "is this true?" — skip routing, evidence and judges (the
            # expensive part) and answer the media question alone.
            result.pop("frames_standby", None)
            _set_job(job_id, stage="assembling")
            if AUTHENTICITY_ENABLED:
                try:
                    result["authenticity"] = run_media_detection(
                        result.get("authenticity") or {},
                        frames=None if url_key.startswith("img:") else _au_frames,
                        extra_frames=_au_extra,
                        image_b64=image_b64 if url_key.startswith("img:") else None,
                        audio_b64=_audio_clip,
                        reason="media-only check", allow_reverse=True,
                        posted_date=result.get("posted_date"),
                        person_hint=(result.get("transcript") or "") + " " + (result.get("title") or ""))
                except Exception:
                    pass
            result["claims"] = []
            result["media_only"] = True
            _scam_lens_finish(result, _scam_started)
            result = build_report(result)
            result["timings"] = {"total_s": round(time.time() - t0, 1)}
            result["cached"] = False
            try:
                save_result(url_key, url, result)
            except Exception:
                pass
            _set_job(job_id, status="done", result=result)
            return

        standby = result.pop("frames_standby", None)
        if AUTHENTICITY_ENABLED and standby and not _au_frames:
            _au_frames = list(standby)[:6]
            _au_extra = list(standby)[6:12] or None
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
        # VIDEO CONTEXT (v0.66.10): every claim carries the title and its
        # sibling claims, so a judge reading "the bill" knows which bill —
        # claims were judged blind to each other and punted (Sep 20)
        _ctx = "Title: " + str(result.get("title") or "")[:160] + ". Claims from this video: " + " | ".join(
            str(c.get("claim", ""))[:140] for c in claims[:6])
        for c in claims:
            c["video_context"] = _ctx
        # TWO LANES, ONE STORY: when the media lane already knows this
        # footage is AI (verified provenance or creator label), the
        # claims lane must not contradict it.
        if AUTHENTICITY_ENABLED:
            _au0 = result.get("authenticity") or {}
            _origin0 = _au0.get("origin_result")
            _ai_known = _origin0 in ("verified_ai_provenance", "declared_ai",
                                     "likely_synthetic")
            _ctx = _au0.get("display") or "AI-generated"
            # a statement about the media's OWN origin ("this video is AI
            # generated", "this footage is real") is the AI panel's job,
            # ALWAYS — never a world-claim for the evidence search (the
            # waterfall video: it went hunting for articles about AI
            # detection and shrugged "not scoreable")
            _re_origin = re.compile(
                r"\b(this|the)\s+(video|clip|footage|image|reel|short|content)\b"
                r"[^.]{0,80}?\b(ai[- ]generated|generated|created|made|produced|"
                r"synthetic|not real|real footage|genuine|authentic|deepfake|fake)\b",
                re.I)
            for c in claims:
                if _re_origin.search(str(c.get("claim", ""))) and re.search(
                        r"\b(ai|a\.i\.|sora|veo|midjourney|dall|kling|pika|runway|"
                        r"artificial intelligence|synthetic|deepfake|real footage|"
                        r"genuine|authentic|not real|fake)\b", str(c.get("claim", "")), re.I):
                    c["gate_label"] = "media_origin"
                    c["reason"] = ("Answered by the AI panel above, which "
                                   "reports what the creator declared and "
                                   "what the detector found.")
                elif _ai_known and c.get("gate_label") in ("factual", "prediction"):
                    c["media_context"] = _ctx
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
            _scam_lens_finish(result, _scam_started)  # a pasted "is this a scam?" message is the lens's best case
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
                # re-check memory: add what the previous run found
                try:
                    claims[i]["evidence"] = _merge_prior_evidence(
                        claims[i]["claim"], claims[i]["evidence"], prior_claims)
                except Exception:
                    pass
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
        # SPEED (Sept 15): the media lane (Hive frames, audio, reverse
        # search) used to run AFTER judging — 5–15s added to the end of
        # every check it fired on. It needs the claims (for its gate), not
        # the verdicts, so it now runs alongside the evidence hunt and is
        # folded in when both are done. Same work, same result, less wait.
        _media_box = {}
        _media_thread = None
        if AUTHENTICITY_ENABLED:
            def _media_lane():
                try:
                    _au = result.get("authenticity") or {}
                    _go, _why = hive_detect.should_run_stage2(
                        title=result.get("title") or "",
                        user_question=user_question or "",
                        claims=claims,
                        stage1_origin=_au.get("origin_result"),
                        on_demand=detect_ai)
                    if _go:
                        _media_box["authenticity"] = run_media_detection(
                            _au,
                            frames=None if url_key.startswith("img:") else _au_frames,
                            extra_frames=_au_extra,
                            image_b64=image_b64 if url_key.startswith("img:") else None,
                            audio_b64=_audio_clip,
                            reason=_why, allow_reverse=True,
                            posted_date=result.get("posted_date"),
                            person_hint=((result.get("transcript") or "") + " "
                                         + (result.get("title") or "") + " "
                                         + (user_question or "")))
                except Exception:
                    pass  # the lane must never break a check
            _media_thread = threading.Thread(target=_media_lane, daemon=True)
            _media_thread.start()
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
            # second look: claims that found nothing on their own get the
            # sources their sibling claims found (see _sibling_rescue)
            try:
                if _sibling_rescue(claims, selected):
                    _publish_partial(job_id, result, claims)
            except Exception:
                pass

        _set_job(job_id, stage="assembling")
        t_verify = time.time() - t0 - t_fetch - t_route
        for c in claims:
            c.pop("verifying", None)
        result["claims"] = claims
        # STAGE 2+: the media lane — Hive frames, adaptive second pass,
        # audio, face pass, forensic second opinion, reverse search — one
        # orchestrator (app/agents/detection.py). Started above, alongside
        # the evidence hunt; folded in here. Gated; dormant without a HIVE
        # key; never allowed to break a check.
        if _media_thread is not None:
            _media_thread.join(timeout=120)
            if _media_box.get("authenticity"):
                result["authenticity"] = _media_box["authenticity"]
        _scam_lens_finish(result, _scam_started)
        # PROFILE-PHOTO CHECK: an uploaded photo of a person gets "where
        # else does this photo appear?" — what the WSJ sisters did by hand
        # to unmask their mother's online suitor. The photo is never stored.
        if url_key.startswith("img:") and image_b64 and reverse_search.available() and scam_mode() == "on":
            try:
                _sc0 = result.get("scam") or {}
                _desc0 = result.get("transcript") or ""
                if scam.wants_photo_check(_desc0.replace("[WHAT THE IMAGE SHOWS] ", ""), _sc0):
                    _rs = reverse_search.analyze(image_b64)
                    _sc1 = scam.apply_photo(_sc0 if _sc0.get("ran") else {"risk": "none", "patterns": [], "pattern_names": [], "ran": True, "score": None}, _rs)
                    result["scam"] = _sc1
            except Exception:
                pass
        result = build_report(result)
        result["timings"] = {
            "fetch_s": round(t_fetch, 1),
            "route_s": round(t_route, 1),
            "verify_s": round(t_verify, 1),
            "total_s": round(time.time() - t0, 1),
        }
        result["cached"] = False
        with _fresh_lock:
            result["fresh_reason"] = _fresh_reasons.pop(job_id, None) or "first check"
        _ensure_one_line(result)
        if result.get("transcript_source") != "typed":
            threading.Thread(target=add_video_timing, args=(result["timings"]["total_s"],), daemon=True).start()
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
    except Exception as e:
        # never hide the cause again: full traceback to the server log
        # (Railway) and the last few kept in memory for the admin
        import traceback as _tb
        _tb.print_exc()
        _remember_error(url_key, e, _tb.format_exc())
        _set_job(job_id, status="error",
                 error="Unexpected error while checking this link.")


# ------------------------------------------------------------ routes


_INDEX_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")
_index_cache = None


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    """One unified page — the glowing checker IS the front door."""
    return _page()  # visitors are counted by the page's beacon (v0.66.4)


@app.get("/app", response_class=HTMLResponse)
def checker(request: Request) -> str:
    """The checker itself; ?u=<link> arrives prefilled from the landing."""
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


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
@app.get("/security.txt", response_class=PlainTextResponse)
def security_txt() -> str:
    """RFC 9116 — tells security researchers how to reach us."""
    return (
        "Contact: mailto:hello@glowby.io\n"
        "Expires: 2027-09-01T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
        "Canonical: https://glowby.io/.well-known/security.txt\n"
        "Policy: https://glowby.io/about#terms\n"
    )


@app.get("/about", response_class=HTMLResponse)
def about() -> str:
    """Trust pages: methodology, ratings, corrections, terms, privacy."""
    global _trust_cache
    if _trust_cache is None:
        with open(_TRUST_PATH, encoding="utf-8") as f:
            _trust_cache = f.read()
    return _trust_cache


# ---- the link you text people (v0.66.7, Inderpreet: "the text message
# link feels too big, not nice") ----
# glowby.io/get previews as a COMPACT card in iMessage/WhatsApp: a small
# square icon on the left, "Glowby" and one line on the right — not the
# big App Store banner. Link previewers read the tags and never run
# JavaScript, so the card is ours; a person on an iPhone is sent straight
# to the App Store, anyone else gets a two-button page.
_GET_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Glowby</title>
<meta property="og:type" content="website">
<meta property="og:site_name" content="Glowby">
<meta property="og:title" content="Glowby">
<meta property="og:description" content="Is that video true? Share it to Glowby and get the answer in one line. Free.">
<meta property="og:url" content="https://glowby.io/get">
<meta property="og:image" content="https://glowby.io/og-icon.png">
<meta property="og:image:width" content="300"><meta property="og:image:height" content="300">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Glowby">
<meta name="twitter:description" content="Is that video true? Share it to Glowby and get the answer in one line. Free.">
<meta name="twitter:image" content="https://glowby.io/og-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#08070F">
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#08070F;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-align:center;padding:24px}
.c{max-width:340px}img{width:96px;height:96px;border-radius:22px}
h1{font-size:1.6rem;margin:14px 0 6px}p{color:#9aa0b0;margin:0 0 22px;line-height:1.45}
a.b{display:block;margin:10px 0;padding:14px 18px;border-radius:14px;font-weight:700;text-decoration:none;font-size:1rem}
a.p{background:#7c6cf6;color:#fff}a.s{background:rgba(255,255,255,.07);color:#e8eaf0;border:1px solid rgba(255,255,255,.14)}
small{display:block;margin-top:18px;color:#6b7080;font-size:.8rem}
</style></head><body><div class="c">
<img src="/icon-192.png" alt="">
<h1>Glowby</h1>
<p>Share it a TikTok, Reel or Short. About a minute later: is it true, and is the person in it real.</p>
<a class="b p" href="https://apps.apple.com/us/app/glowby/id6798336220">Get the iPhone app</a>
<a class="b s" href="/">Use it in the browser</a>
<small>Free · no account · AI-powered, results can be wrong</small>
</div>
<script>
// iPhone/iPad: straight to the App Store (previewers never run this)
if(/iPhone|iPad|iPod/.test(navigator.userAgent) && !/[?&]stay/.test(location.search)){
  location.replace("https://apps.apple.com/us/app/glowby/id6798336220");
}
</script></body></html>"""


@app.get("/get", response_class=HTMLResponse, include_in_schema=False)
def get_page() -> str:
    return _GET_HTML


# ---- glowby.io/how — how to send a video from each app (v0.66.8) ----
# The link that goes with the text message: one short page, one section
# per app, anchors so a text can point at a section (glowby.io/how#tiktok).
_HOW_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How to use Glowby</title>
<meta property="og:type" content="website"><meta property="og:site_name" content="Glowby">
<meta property="og:title" content="How to use Glowby">
<meta property="og:description" content="Share any TikTok, Reel, Facebook video or YouTube Short to Glowby. Here is where the Share button is in each app.">
<meta property="og:url" content="https://glowby.io/how">
<meta property="og:image" content="https://glowby.io/og-icon.png">
<meta property="og:image:width" content="300"><meta property="og:image:height" content="300">
<meta name="twitter:card" content="summary">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#08070F">
<style>
:root{--bg:#08070F;--ink:#e8eaf0;--muted:#9aa0b0;--brand:#7c6cf6;--surface:rgba(255,255,255,.05);--border:rgba(255,255,255,.12)}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.5}
.w{max-width:560px;margin:0 auto;padding:28px 18px 48px}
.top{display:flex;align-items:center;gap:12px;margin-bottom:6px}.top img{width:44px;height:44px;border-radius:11px}
h1{font-size:1.5rem;margin:0}.lead{color:var(--muted);margin:6px 0 18px}
.jump{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px}
.jump a{padding:7px 12px;border-radius:999px;background:var(--surface);border:1px solid var(--border);color:var(--ink);text-decoration:none;font-size:.9rem;font-weight:600}
section{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:16px 16px 6px;margin:0 0 14px;scroll-margin-top:12px}
h2{font-size:1.1rem;margin:0 0 8px}
ol{margin:0 0 10px;padding-left:22px}li{margin:4px 0}
b.k{background:rgba(124,108,246,.18);border:1px solid rgba(124,108,246,.45);color:#c4bbff;border-radius:6px;padding:1px 6px;font-weight:700;white-space:nowrap}
.note{color:var(--muted);font-size:.9rem;margin:0 0 10px}
.cta{display:block;text-align:center;margin:22px 0 0;padding:14px;border-radius:14px;background:var(--brand);color:#fff;font-weight:700;text-decoration:none}
small{display:block;text-align:center;color:#6b7080;margin-top:14px;font-size:.8rem}
</style></head><body><div class="w">
<div class="top"><img src="/icon-192.png" alt=""><h1>How to use Glowby</h1></div>
<p class="lead">Find the video&rsquo;s <b>Share</b> button, pick <b>Glowby</b>, wait about a minute. You get a one-line answer, a score, the sources, and whether the video looks AI-generated. You&rsquo;ll get a notification when it&rsquo;s done.</p>
<div class="jump"><a href="#tiktok">TikTok</a><a href="#instagram">Instagram</a><a href="#facebook">Facebook</a><a href="#youtube">YouTube</a><a href="#photo">Screenshot</a><a href="#fav">Put Glowby first</a><a href="#noapp">No app?</a></div>

<section id="tiktok"><h2>TikTok</h2><ol>
<li>On the video, tap the <b class="k">Share</b> arrow on the right.</li>
<li>In the bottom row of apps, swipe left and tap <b class="k">Glowby</b>. If you don&rsquo;t see it, tap <b class="k">More</b> and find it there.</li>
</ol><p class="note">Or tap <b>Copy link</b>, open Glowby, and paste.</p></section>

<section id="instagram"><h2>Instagram Reels</h2><ol>
<li>On the reel, tap the <b class="k">paper-plane</b> (Send) icon on the right.</li>
<li>Scroll to the bottom of that sheet and tap <b class="k">Share to&hellip;</b>, then <b class="k">Glowby</b>.</li>
</ol><p class="note">Or tap the <b>&middot;&middot;&middot;</b> menu &rarr; <b>Copy link</b>, open Glowby, and paste.</p></section>

<section id="facebook"><h2>Facebook Reels &amp; videos</h2><ol>
<li>Under the video, tap <b class="k">Share</b>.</li>
<li>Tap <b class="k">More options</b> (or <b class="k">Share to&hellip;</b>), then <b class="k">Glowby</b>.</li>
</ol><p class="note">Or tap <b>Copy link</b>, open Glowby, and paste. Public videos only &mdash; Glowby can&rsquo;t see private posts.</p></section>

<section id="youtube"><h2>YouTube Shorts &amp; videos</h2><ol>
<li>Tap <b class="k">Share</b> under the video (on a Short, the arrow on the right).</li>
<li>Tap <b class="k">More</b> in the row of apps, then <b class="k">Glowby</b>.</li>
</ol><p class="note">Or tap <b>Copy link</b>, open Glowby, and paste.</p></section>

<section id="photo"><h2>A screenshot, photo or text</h2><ol>
<li>Someone sent you a screenshot? Open it in <b class="k">Photos</b>, tap <b class="k">Share</b>, then <b class="k">Glowby</b>.</li>
<li>A claim in a message? Open Glowby and type or paste it.</li>
</ol></section>

<section id="fav"><h2>Put Glowby first in the Share menu (one time)</h2><ol>
<li>Open any Share menu and swipe the row of app icons all the way left, then tap <b class="k">More</b>.</li>
<li>Tap <b class="k">Edit</b> (top right), tap the green <b class="k">+</b> next to Glowby, then <b class="k">Done</b>.</li>
</ol><p class="note">From then on Glowby is always in the first few apps.</p></section>

<section id="noapp"><h2>No app, or on Android or a computer</h2><ol>
<li>Copy the video&rsquo;s link (every app has <b>Copy link</b> in its Share menu).</li>
<li>Open <b class="k">glowby.io</b> and paste it.</li>
</ol></section>

<a class="cta" href="/get">Get Glowby &mdash; free</a>
<small>AI-powered &middot; results can be wrong &middot; always check the sources &middot; <a href="/trust" style="color:inherit">what&rsquo;s sent</a></small>
</div></body></html>"""


@app.get("/how", response_class=HTMLResponse, include_in_schema=False)
def how_page() -> str:
    return _HOW_HTML


@app.get("/r/{key:path}", response_class=HTMLResponse)
def permalink_page(key: str, request: Request) -> str:
    # same single-page app; its JS loads /api/result/<key>
    return _page()


def _cached_ai_ran(cached: dict) -> bool:
    """Pure: did a stored result's AI media check actually finish? A
    stage-2 attempt that FAILED used to count as "already ran", so "AI
    detect only" got the old claims result back with a warning card
    instead of a fresh detector run (Inderpreet, Sep 20). Only a
    completed or partly-completed stage 2 is worth serving from cache."""
    au = (cached or {}).get("authenticity") or {}
    return au.get("stage") == 2 and au.get("stage2_status") in ("completed", "partial")


class CheckRequest(BaseModel):
    url: str = ""
    force: bool = False  # true = ignore the cache and re-run (recheck)
    captcha_token: str = ""  # Turnstile token (required when captcha is on)
    question: str = ""  # optional "+ask": what the user wants to know
    image_b64: str = ""  # uploaded screenshot/photo (JPEG, base64)
    audio_b64: str = ""  # uploaded recording (voicemail / call; m4a, mp3, wav, webm, ogg; base64)
    detect_ai: bool = False  # "+detect AI": user asked for the media check
    ai_only: bool = False  # "AI only": skip claim routing/judging entirely
    scam_check: bool = False  # "Scam check" mode: the scam lens answers VISIBLY for this check


MAX_IMAGE_B64 = 10_000_000  # ~7.5 MB decoded — client resizes first


def _clean_image_b64(data: str):
    """Strip a data-URL prefix, size-check, and lightly validate.
    Returns clean base64 or None if unusable."""
    d = (data or "").strip()
    if d.startswith("data:"):
        comma = d.find(",")
        if comma == -1:
            return None
        d = d[comma + 1:]
    if not d or len(d) > MAX_IMAGE_B64:
        return None
    import base64 as _b64
    try:  # must decode — garbage never reaches the vision agent
        raw = _b64.b64decode(d, validate=True)
    except Exception:
        return None
    if len(raw) < 1000:  # not a real photo
        return None
    return d


MAX_AUDIO_B64 = 14_000_000  # ~10 MB decoded: a few minutes of voicemail
_AUDIO_MAGIC = ((b"ftyp", 4, ".m4a"), (b"ID3", 0, ".mp3"), (b"\xff\xfb", 0, ".mp3"), (b"\xff\xf3", 0, ".mp3"),
                (b"RIFF", 0, ".wav"), (b"OggS", 0, ".ogg"), (b"\x1aE\xdf\xa3", 0, ".webm"), (b"fLaC", 0, ".flac"))


def _clean_audio_b64(data: str):
    """Strip a data-URL prefix, size-check, sniff the container. Returns
    (clean_b64, extension) or (None, None)."""
    d = (data or "").strip()
    if d.startswith("data:"):
        comma = d.find(",")
        if comma == -1:
            return None, None
        d = d[comma + 1:]
    if not d or len(d) > MAX_AUDIO_B64:
        return None, None
    import base64 as _b64
    try:
        raw = _b64.b64decode(d, validate=True)
    except Exception:
        return None, None
    if len(raw) < 2000:
        return None, None
    ext = None
    for magic, off, e in _AUDIO_MAGIC:
        if raw[off:off + len(magic)] == magic:
            ext = e
            break
    return d, (ext or ".m4a")


def _audio_key(clean_b64: str) -> str:
    return "aud:" + hashlib.sha256(clean_b64.encode()).hexdigest()[:32]


def _image_key(clean_b64: str) -> str:
    """Cache key for an uploaded image: same image -> same result."""
    return "img:" + hashlib.sha256(clean_b64.encode()).hexdigest()[:32]


@app.post("/api/check")
def api_check(req: CheckRequest, request: Request):
    """Start a check of a video URL, a typed claim, OR an uploaded image."""
    raw = (req.url or "").strip()
    image_b64 = None
    audio_b64 = None
    if req.audio_b64:
        audio_b64, _aext = _clean_audio_b64(req.audio_b64)
        if audio_b64 is None:
            return JSONResponse(
                status_code=422,
                content={"detail": "That recording couldn't be read — try an "
                                   "m4a, mp3 or wav under ~10 MB."})
        url_key = _audio_key(audio_b64)
        audio_b64 = audio_b64 + "|" + _aext  # extension travels with the data
    elif req.image_b64:
        image_b64 = _clean_image_b64(req.image_b64)
        if image_b64 is None:
            return JSONResponse(
                status_code=422,
                content={"detail": "That image couldn't be read — try a "
                                   "JPEG or PNG under ~7 MB."},
            )
        url_key = _image_key(image_b64)
    elif not looks_like_url(raw):
        if len(raw) < 12:
            return JSONResponse(
                status_code=422,
                content={"detail": "Type a full claim to check (a sentence), "
                                   "or paste a video link."},
            )
        url_key = text_key(raw)
    else:
        # SHARE LINKS: fb.watch/CODE and facebook.com/share/r/CODE are the
        # same reel as facebook.com/reel/ID — resolve first so both
        # spellings land on one key (one run, one score; Sep 2026)
        if is_short_link(raw):
            try:
                raw = resolve_short_link(raw) or raw
            except Exception:
                pass
        url_key = canonical_key(raw)
    question = (req.question or "").strip()[:400]
    cached = None if req.force else get_cached(url_key, CACHE_TTL_DAYS)
    if cached is None and not req.force and url_key.startswith(("facebook:", "instagram:")):
        # results stored under the old spelling-sensitive key still count
        try:
            _old = get_cached(legacy_key(raw), CACHE_TTL_DAYS)
        except Exception:
            _old = None
        if _old is not None:
            cached = _old
            url_key = _old.get("url_key") or url_key
    # "+detect AI" on a cached result that never ran the detector:
    # serve nothing stale — run fresh so the media check actually happens
    if cached is not None and cached.get("media_only") and not req.ai_only:
        # a stored "AI only" result has no claims — a full check must run
        cached = None
    if cached is not None and (req.detect_ai or req.ai_only) and not _cached_ai_ran(cached):
        cached = None
    if cached is not None and req.scam_check and cached.get("scam_shadow"):
        # a stored result whose scam finding was kept in the shadows: the
        # reader asked for it — reveal it, no re-run needed
        cached["scam"] = cached["scam_shadow"]
        cached["scam_requested"] = True
    if cached is not None:
        cached.setdefault("url_key", url_key)
        if "report" not in cached:  # results stored before v0.9
            build_report(cached)
        _ensure_one_line(cached, url_key)
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
        # APP LENIENCY: the iOS app's WKWebView (and App Review's
        # datacenter network) can fail the invisible captcha through no
        # fault of the user. App-page requests always carry a Referer
        # containing app=1 — let those through; the rate limit and daily
        # budget below still bound any abuse.
        _ref = request.headers.get("referer", "")
        _is_app = "app=1" in _ref
        if not _is_app:
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

    # EVIDENCE MEMORY: a re-check keeps what the last run learned. The
    # prior result's per-claim sources are merged into the fresh hunt,
    # so every re-check judges on MORE evidence, never a thinner draw —
    # this damps run-to-run score wobble on contested claims.
    prior_claims = None
    if req.force or req.detect_ai or req.ai_only:
        # detect-AI bypasses the cache too — that fresh run must keep
        # the memory (claim anchoring + evidence merge) or scores wobble
        try:
            prior = get_cached(url_key, 0)  # any age — memory, not cache
            if prior and prior.get("claims"):
                prior_claims = prior["claims"]
        except Exception:
            prior_claims = None

    job_id = uuid.uuid4().hex[:12]
    # WHY A FRESH RUN: never a mystery again (the twice-checked reel,
    # Sep 2026). Read back in admin > recent checks.
    if req.force:
        _why_fresh = "re-check"
    elif not cache_available():
        _why_fresh = "cache unreachable"
    elif req.detect_ai or req.ai_only:
        _why_fresh = "AI check requested"
    else:
        _why_fresh = "first check"
    with _fresh_lock:
        _fresh_reasons[job_id] = _why_fresh
    _set_job(job_id, status="queued", stage="fetching", started=time.time())
    threading.Thread(
        target=_run_pipeline,
        args=(job_id, (raw if looks_like_url(raw) else req.url), url_key, question, prior_claims, image_b64,
              bool(req.detect_ai) or bool(req.ai_only) or bool(audio_b64), bool(req.ai_only),
              audio_b64, bool(req.scam_check)),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}/wait")
def api_job_wait(job_id: str, max_s: int = 110):
    """Long-poll: hold the request until the job finishes (or ~110s), then
    answer with the one line that matters. The iPhone app asks for this
    through a background download, so a check that finishes while the
    phone is in a pocket still becomes a notification ("once Glowby is
    done, send a notification" — Diya, Sept 15)."""
    max_s = max(5, min(int(max_s or 110), 115))
    t_end = time.time() + max_s
    job = {}
    while time.time() < t_end:
        with _jobs_lock:
            job = dict(_jobs.get(job_id) or {})
        if not job:
            return JSONResponse(status_code=404, content={"status": "unknown"})
        if job.get("status") in ("done", "error"):
            break
        time.sleep(1.0)
    st = job.get("status") or "running"
    if st != "done":
        return {"status": st, "error": job.get("error") if st == "error" else None}
    res = job.get("result") or {}
    rep = res.get("report") or {}
    _ensure_one_line(res)
    return {"status": "done", "title": (res.get("title") or "")[:120],
            "one_line": rep.get("one_line") or "", "score": rep.get("headline_score"),
            "url_key": res.get("url_key")}


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


def _ensure_one_line(result: dict, save_key: str = None) -> None:
    """The plain-English line above the score (app/agents/summary.py).
    Written once per result; older stored results get theirs the first
    time they are opened. ~0.2¢, never fatal."""
    try:
        rep = result.get("report")
        if not isinstance(rep, dict):
            return
        if rep.get("one_line"):
            # a stored line that is not an answer (the model remarked on
            # its instructions once, Sept 15) gets rewritten on next read
            from app.agents.summary import consistent as _ok_line
            if _ok_line(rep["one_line"], result):
                return
        rep["one_line"] = _one_line(result)
        add_usage(0.002)
        if save_key:
            patch_result(save_key, result)
    except Exception:
        pass


@app.get("/api/result/{key:path}")
def api_result(key: str):
    cached = get_cached(key)
    if cached is None:
        return JSONResponse(status_code=404, content={"detail": "No stored result."})
    cached.setdefault("url_key", key)
    if "report" not in cached:
        build_report(cached)
    _ensure_one_line(cached, key)
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
        "scam_lens": ({k: (result.get("scam") or {}).get(k) for k in ("risk", "pattern_names", "promise", "ask", "impersonates", "reason")}
                      if (result.get("scam") or {}).get("risk") not in (None, "none") else None),
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
# v0.66.4: counted from the PAGE, not the server. Crawlers, link-preview
# fetchers (iMessage/Slack unfurls), uptime pings and App Review's servers
# all load "/" but never run the page's script, so they no longer count.
# The page sends an anonymous random device code (made on the device,
# stored only there, no personal info); the server hashes it with the date
# so daily counts exist, cross-day tracking is impossible, and nothing
# that identifies a device is stored. IP addresses are no longer used for
# counting at all — a phone on cellular used to count 2-3× a day as the
# carrier rotated its address, and a whole household on one Wi-Fi
# counted as one person.

_BOT_UA = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|"
                     r"whatsapp|telegram|discord|skype|slack|twitterbot|"
                     r"headless|python-requests|curl/|wget/|monitor|uptime",
                     re.I)
_VID_RE = re.compile(r"^[a-f0-9]{32}$")


class Visit(BaseModel):
    vid: str = ""


def _count_visitor(vid: str, user_agent: str = "") -> bool:
    """Pure-ish: True when the visit was counted."""
    if not _VID_RE.match(vid or "") or _BOT_UA.search(user_agent or ""):
        return False
    try:
        day = time.strftime("%Y-%m-%d")
        salt = ADMIN_KEY or "glowby"
        vh = hashlib.sha256(f"{salt}:{day}:{vid}".encode()).hexdigest()[:32]
        threading.Thread(target=record_visitor, args=(vh,), daemon=True).start()
        # monthly code: one count per device per calendar month, never
        # linkable across months (different salt input)
        month = time.strftime("%Y-%m")
        mh = hashlib.sha256(f"{salt}:month:{month}:{vid}".encode()).hexdigest()[:32]
        threading.Thread(target=record_visitor_month, args=(mh,), daemon=True).start()
        return True
    except Exception:
        return False


@app.post("/api/visit")
def api_visit(v: Visit, request: Request):
    """The page's headcount beacon. Anonymous; nothing about who is stored."""
    return {"ok": _count_visitor(v.vid, request.headers.get("user-agent", ""))}


class ScoreFeedback(BaseModel):
    url_key: str = ""
    kind: str
    claim_idx: int | None = None
    note: str = ""


@app.post("/api/feedback")
def api_feedback(fb: ScoreFeedback, request: Request):
    """Fair / Harsh / Wrong under the score. A maintainers' signal, never a
    vote: it changes nothing on the page. De-duplicated per device per
    result with a salted, date-free hash (no IP stored)."""
    kind = (fb.kind or "").strip().lower()
    if kind not in FEEDBACK_KINDS or not (fb.url_key or "").strip():
        return JSONResponse(status_code=422, content={"detail": "Bad feedback."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(status_code=429, content={"detail": "Too many requests."})
    salt = ADMIN_KEY or "glowby"
    dh = hashlib.sha256(f"{salt}:fb:{_client_ip(request)}".encode()).hexdigest()[:32]
    # claim_idx >= 0: a claim card; -1: the AI-check row; -2: the scam-lens
    # row. Scopes are part of the de-dup key, so a "Right" on the AI check
    # never overwrites a "Harsh" on the score from the same phone.
    idx = fb.claim_idx if isinstance(fb.claim_idx, int) and -2 <= fb.claim_idx < 50 else None
    ok = save_feedback(fb.url_key.strip()[:300], kind, idx, (fb.note or "").strip()[:300], dh)
    # the third data layer: a scam-lens flag on a pasted message becomes a
    # PENDING sample — redacted, source and consent recorded, and counted
    # as truth only after a person verifies it on the admin page
    if kind in ("scam_missed", "scam_false_alarm"):
        try:
            _res = get_cached(fb.url_key.strip()[:300], 0)
            _uk = fb.url_key.strip()
            if _res and (_uk.startswith("text:") or _uk.startswith("img:") or _uk.startswith("aud:")):
                from app.agents.scamengine import redact_pii
                _sc = _res.get("scam") or {}
                _rep = _sc.get("report") or {}
                _ex = _rep.get("extracted") or {}
                save_scam_sample(
                    redact_pii(_res.get("transcript") or ""), "scam" if kind == "scam_missed" else "ok",
                    "sms" if _uk.startswith("text:") else ("screenshot" if _uk.startswith("img:") else "voice"),
                    _rep.get("scam_types") or [], _rep.get("requested_actions") or [],
                    [f.get("code") for f in (_rep.get("risk_factors") or [])], _ex.get("payment_method"),
                    "user_flag", "reader tapped the flag; consent notice shown at submission",
                    trace_id=_rep.get("audit_trace_id") or "", url_key=_uk, note=(fb.note or "")[:300], label_confidence=0.5)
        except Exception:
            pass
    return {"ok": True, "stored": ok}


class ScamRequest(BaseModel):
    text: str = ""
    context: dict | None = None


@app.post("/api/scam")
def api_scam(req: ScamRequest, request: Request):
    """The scam-risk engine as an API (the B2B path): pasted text -> the
    required JSON. Keyed: x-api-key must be one of GLOWBY_PARTNER_KEYS
    (comma-separated, Railway Variables only) or the admin key. Same
    rate limiter and daily budget as everything else. The text is data;
    nothing in it is executed, fetched, or contacted."""
    key = (request.headers.get("x-api-key") or "").strip()
    partners = [k.strip() for k in (os.environ.get("GLOWBY_PARTNER_KEYS") or "").split(",") if k.strip()]
    if not key or not (key in partners or _admin_ok(key)):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(status_code=429, content={"detail": "Too many requests."})
    text = (req.text or "").strip()
    if len(text) > 12000:
        return JSONResponse(status_code=422, content={"detail": "Text too long (12,000 characters max)."})
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return JSONResponse(status_code=503, content={"detail": "Daily budget reached; try again tomorrow."})
    from app.agents import scamengine
    rep = scamengine.analyze(text, context=None)
    aud = rep.get("audit") or {}
    add_usage(0.003 * bool(aud.get("model_extracted")) + 0.005 * (aud.get("queries") or 0))
    try:
        save_scam_audit(rep.get("audit_trace_id") or "", "api", rep, hashlib.sha256(text.encode()).hexdigest()[:32])
    except Exception:
        pass
    rep.pop("audit", None)  # weights and query counts stay internal
    return rep


@app.get("/api/admin/scam/trace")
def api_admin_scam_trace(key: str = "", id: str = ""):
    """Look up one engine run by its audit_trace_id (disputes, partner
    questions). Returns the internal dimensions and floors — admin only."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    doc = load_scam_audit((id or "").strip())
    if not doc:
        return JSONResponse(status_code=404, content={"detail": "No such trace."})
    return doc


class ExamRun(BaseModel):
    key: str
    split: str = "dev"  # dev | validation | hidden
    robustness: bool = True


_EXAM_STATE = {"last": {}}


@app.post("/api/admin/scam/exam/run")
def api_admin_scam_exam_run(req: ExamRun):
    """Run one split of the exam. dev = the shipped corpus (failures
    listed); validation = uploaded (failures listed); hidden = uploaded
    (rates only — the texts never leave the server)."""
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents import scamexam, scamcal
    import json as _json
    split = req.split if req.split in scamexam.SPLITS else "dev"
    if split == "dev":
        try:
            with open(scamcal.CORPUS_PATH, encoding="utf-8") as f:
                cases = [dict(c, split="dev") for c in (_json.load(f).get("items") or [])]
        except Exception:
            cases = []
    else:
        cases = load_exam_cases(split)
    if not cases:
        return {"ok": False, "detail": f"no {split} cases — upload some first"}
    res = scamexam.run_exam(cases, split, robustness=bool(req.robustness))
    _EXAM_STATE["last"][split] = res
    return {"ok": True, "result": res}


class ExamUpload(BaseModel):
    key: str
    split: str  # validation | hidden
    text: str   # JSONL cases, 'label: text' lines, or a pasted dataset (UCI TSV / Mendeley CSV)
    replace: bool = False
    source: str = ""  # e.g. "uci-sms-spam" / "mendeley-smishing" — recorded with each case


@app.post("/api/admin/scam/exam/upload")
def api_admin_scam_exam_upload(req: ExamUpload):
    """Upload validation or hidden cases. A message already present in any
    split is skipped, so a case can never sit in two splits. Nothing is
    echoed back for the hidden split."""
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if req.split not in ("validation", "hidden"):
        return JSONResponse(status_code=422, content={"detail": "split must be validation or hidden"})
    from app.agents import scamexam, scamcal
    import json as _json
    body = req.text or ""
    cases = scamexam.parse_cases(body, default_split=req.split)
    if not cases:  # a pasted dataset (UCI tab-separated, Mendeley CSV with a header)
        cases = scamexam.parse_dataset_csv(body, default_split=req.split, source=req.source or "dataset")
    cases = [c for c in cases if c["label"] in scamexam.LABELS]
    cases, dropped = scamexam.dedupe(cases)
    # never let a dev case (or a rewrite of one) into validation or hidden
    try:
        with open(scamcal.CORPUS_PATH, encoding="utf-8") as f:
            dev_fps = {scamexam.fingerprint(c.get("text", "")) for c in (_json.load(f).get("items") or [])}
        before = len(cases)
        cases = [c for c in cases if scamexam.fingerprint(c["text"]) not in dev_fps]
        dropped += before - len(cases)
    except Exception:
        pass
    n = save_exam_cases(cases, req.split, replace=bool(req.replace))
    return {"ok": True, "added": n, "parsed": len(cases) + dropped, "duplicates_dropped": dropped, "counts": exam_case_counts()}


@app.get("/api/admin/scam/exam/latest")
def api_admin_scam_exam_latest(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"counts": exam_case_counts(), "last": _EXAM_STATE["last"]}


class SampleReview(BaseModel):
    key: str
    sample_id: int
    status: str  # verified | rejected | pending
    label: str = ""  # scam | ok (optional relabel)


@app.get("/api/admin/scam/samples")
def api_admin_scam_samples(key: str = "", status: str = "pending", limit: int = 100):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"stats": scam_sample_stats(), "items": list_scam_samples(status if status in ("pending", "verified", "rejected", "") else "pending", min(max(int(limit), 1), 500))}


@app.post("/api/admin/scam/samples/review")
def api_admin_scam_samples_review(req: SampleReview):
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"ok": review_scam_sample(req.sample_id, req.status, req.label)}


class SamplePaste(BaseModel):
    key: str
    text: str  # "scam: ..." / "ok: ..." lines — already-verified examples an admin adds by hand


@app.post("/api/admin/scam/samples/paste")
def api_admin_scam_samples_paste(req: SamplePaste):
    """Hand-written or licensed examples go in as VERIFIED (a person wrote
    or reviewed them); redacted anyway."""
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents import scamcal
    from app.agents.scamengine import redact_pii
    n = 0
    for it in scamcal.parse_items(req.text or "")[:500]:
        if it["label"] not in ("scam", "ok"):
            continue
        sid = save_scam_sample(redact_pii(it["text"]), it["label"], "paste", [], [], [], None, "admin_paste",
                               "admin-supplied; licence recorded by the admin", label_confidence=0.9)
        if sid and review_scam_sample(sid, "verified", it["label"]):
            n += 1
    return {"ok": True, "added": n}


@app.get("/api/admin/scam/samples/export")
def api_admin_scam_samples_export(key: str = ""):
    """The verified set as corpus lines — what the calibration tool reads."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    rows = list_scam_samples("verified", 2000)
    body = "\n".join(f"{r['label']}: {(r['redacted_text'] or '').replace(chr(10), ' ')}" for r in rows)
    return PlainTextResponse(body)


@app.get("/api/admin/scam/stats")
def api_admin_scam_stats(key: str = "", days: int = 30):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    out = scam_audit_stats(min(max(int(days), 1), 365))
    out["mode"] = scam_mode()
    return out


@app.get("/api/admin/scam/shadow")
def api_admin_scam_shadow(key: str = "", limit: int = 50, min_score: int = 40):
    """What the engine WOULD have shown readers while in shadow mode: the
    recent checks whose hidden verdict reached the card threshold, with
    the redacted title, score, verdict and codes — so a person can judge
    the false alarms before anyone sees a card."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"mode": scam_mode(), "items": list_shadow_scams(min(max(int(limit), 1), 200), int(min_score))}


@app.get("/api/admin/feedback")
def api_admin_feedback(key: str = "", limit: int = 100, all: int = 0, kind: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    items = list_feedback(min(max(int(limit), 1), 500), only_flags=(not all and kind not in ("fair",)))
    if kind in FEEDBACK_KINDS:
        items = [i for i in items if i.get("kind") == kind]
    elif kind == "ai":
        items = [i for i in items if i.get("kind") in ("ai_missed", "false_alarm")]
    elif kind == "scam":
        items = [i for i in items if i.get("kind") in ("scam_missed", "scam_false_alarm")]
    return {"summary": feedback_summary(30), "daily": feedback_daily(14), "items": items}


# ---- weekly flag review: the judge of the judges (proposals only) ----
_REVIEW_STATE = {"running": False, "last": None, "error": None}


def run_flag_review(reason: str = "scheduled") -> dict:
    """Review every unreviewed harsh/wrong flag. Budget-guarded; never
    changes a score or a rule. Returns the saved document (or a typed
    reason nothing ran)."""
    from app.agents.review import run_review
    if _REVIEW_STATE["running"]:
        return {"ok": False, "detail": "a review is already running"}
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return {"ok": False, "detail": "daily budget reached; review skipped"}
    flags = pending_flags()
    if not flags:
        return {"ok": True, "detail": "no unreviewed flags", "flags": 0}
    _REVIEW_STATE["running"] = True
    try:
        doc = run_review(flags, load_result_quiet)
        doc["reason"] = reason
        rid = save_review(doc)
        doc["id"] = rid
        try:
            add_usage(float(doc.get("est_cost") or 0))
        except Exception:
            pass
        _REVIEW_STATE["last"] = doc.get("created_at")
        _REVIEW_STATE["error"] = None
        return {"ok": True, "id": rid, "flags": len(flags), "summary": doc.get("summary"),
                "models_used": doc.get("models_used"), "est_cost": doc.get("est_cost")}
    except Exception as e:
        _REVIEW_STATE["error"] = str(e)[:200]
        return {"ok": False, "detail": str(e)[:200]}
    finally:
        _REVIEW_STATE["running"] = False


def _review_due() -> bool:
    """Monday, after 16:00 UTC (9am Pacific), and no review in the last 6 days."""
    if os.environ.get("GLOWBY_REVIEW_WEEKLY", "1") != "1":
        return False
    now = time.gmtime()
    if now.tm_wday != 0 or now.tm_hour < 16:
        return False
    last = last_review_at()
    if last is None:
        return True
    try:
        return (time.time() - last.timestamp()) > 6 * 86400
    except Exception:
        return True


@app.post("/api/admin/review/run")
def api_admin_review_run(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return run_flag_review(reason="manual")


@app.get("/api/admin/review/latest")
def api_admin_review_latest(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents.review import REVIEW_MODEL
    return {"review": latest_review(), "state": _REVIEW_STATE, "model": REVIEW_MODEL,
            "weekly": os.environ.get("GLOWBY_REVIEW_WEEKLY", "1") == "1", "pending": len(pending_flags())}


# ---- detector calibration (admin): a labelled set through the real lane ----
_CAL_STATE = {"running": False, "done": 0, "total": 0, "error": None}


class CalibrateRequest(BaseModel):
    key: str
    text: str  # lines: "ai <url>" / "real <url>"


@app.post("/api/admin/calibrate")
def api_admin_calibrate(req: CalibrateRequest):
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents.calibration import parse_items, run_calibration
    items = parse_items(req.text)
    if not items:
        return {"ok": False, "detail": "no items — one per line, e.g. 'ai https://...' or 'real https://...'"}
    if _CAL_STATE["running"]:
        return {"ok": False, "detail": "a calibration run is already in progress"}
    _, spent = today_usage()
    if spent >= DAILY_BUDGET_USD:
        return {"ok": False, "detail": "daily budget reached"}

    def _bg():
        _CAL_STATE.update({"running": True, "done": 0, "total": len(items), "error": None})
        try:
            doc = run_calibration(items, progress=lambda d, t: _CAL_STATE.update({"done": d, "total": t}))
            save_calibration(doc)
            try:
                add_usage(float(doc.get("est_cost") or 0))
            except Exception:
                pass
        except Exception as e:
            _CAL_STATE["error"] = str(e)[:200]
        finally:
            _CAL_STATE["running"] = False
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "started": len(items), "est_cost": round(0.045 * len(items), 2)}


@app.get("/api/admin/calibrate/candidates")
def api_admin_calibrate_candidates(key: str = ""):
    """Reader-labelled videos ("it's AI" / "it's real") as ready-to-paste
    calibration lines. Reader labels are hints, not truth — the admin
    keeps only the ones they can vouch for."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    items = reader_labelled_media()
    lines = [f"{i['label']} {i['url']}" for i in items if i.get("url")]
    return {"items": items, "lines": "\n".join(lines)}


@app.get("/api/admin/lasterror")
def api_admin_lasterror(key: str = ""):
    """The last few pipeline failures with their tracebacks (admin only)."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"errors": list(reversed(_LAST_ERRORS)), "version": VERSION}


@app.get("/api/admin/calibrate/latest")
def api_admin_calibrate_latest(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"state": _CAL_STATE, "run": latest_calibration()}


class ScamCalRequest(BaseModel):
    key: str
    text: str = ""     # optional extra lines: "scam: ..." / "ok: ..." or UCI "spam<TAB>..." / "ham<TAB>..."
    seed: bool = True  # include the shipped corpus
    verified: bool = True  # include Glowby's own human-verified samples
    learn: bool = False  # send misses + false alarms to the review model for proposals


_SCAMCAL_STATE = {"running": False, "last": None, "learn": None, "error": None}


@app.post("/api/admin/scamcal")
def api_admin_scamcal(req: ScamCalRequest):
    """Run the scam engine over a labelled corpus (rules only: free,
    instant) and report detection / false-alarm rates with every miss and
    false alarm listed. With learn=true the misses go to the review model,
    which proposes pattern shapes — proposals only; humans decide."""
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    from app.agents import scamcal
    items = (scamcal.load_seed() if req.seed else []) + scamcal.parse_any(req.text or "")
    if req.verified:
        items += scamcal.load_verified()
    if not items:
        return {"ok": False, "detail": "no items — paste lines like 'scam: <text>' / 'ok: <text>' or keep the seed corpus on"}
    doc = scamcal.run(items)
    doc["extra_items"] = len(scamcal.parse_any(req.text or ""))
    _SCAMCAL_STATE["last"] = doc
    _SCAMCAL_STATE["learn"] = None
    if req.learn and (doc.get("misses") or doc.get("false_alarms")):
        _, spent = today_usage()
        if spent >= DAILY_BUDGET_USD:
            _SCAMCAL_STATE["learn"] = {"error": "daily budget reached", "proposals": []}
        else:
            _SCAMCAL_STATE["running"] = True

            def _go():
                try:
                    _SCAMCAL_STATE["learn"] = scamcal.learn(doc)
                    add_usage(0.10)
                except Exception as e:
                    _SCAMCAL_STATE["learn"] = {"error": str(e)[:200], "proposals": []}
                finally:
                    _SCAMCAL_STATE["running"] = False
            threading.Thread(target=_go, daemon=True).start()
    return {"ok": True, "run": doc, "learning": bool(req.learn)}


@app.get("/api/admin/scamcal/latest")
def api_admin_scamcal_latest(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"state": {"running": _SCAMCAL_STATE["running"]}, "run": _SCAMCAL_STATE["last"], "learn": _SCAMCAL_STATE["learn"]}


class FeedbackResolve(BaseModel):
    key: str
    feedback_id: int
    status: str


@app.post("/api/admin/feedback/resolve")
def api_admin_feedback_resolve(req: FeedbackResolve):
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    return {"ok": resolve_feedback(req.feedback_id, req.status)}


class MistakeReport(BaseModel):
    url_key: str = ""
    url: str = ""
    message: str = ""
    contact: str = ""
    kind: str = "wrong"  # wrong (score) | inappropriate (content)


@app.post("/api/report")
def api_report(rep: MistakeReport, request: Request):
    kind = rep.kind if rep.kind in ("wrong", "inappropriate") else "wrong"
    msg = (rep.message or "").strip()
    if kind == "inappropriate" and not msg:
        msg = "Reported as inappropriate content."
    if kind == "wrong" and len(msg) < 10:
        return JSONResponse(status_code=422, content={
            "detail": "Tell us what looks wrong (a sentence or two)."})
    if _rate_limited(_client_ip(request)):
        return JSONResponse(status_code=429, content={"detail": "Too many requests."})
    saved = save_mistake_report(rep.url_key, rep.url, msg, rep.contact, kind=kind)
    return {"ok": True, "stored": saved}


class ModerateRequest(BaseModel):
    key: str
    url_key: str
    action: str  # hide | delete


@app.post("/api/admin/moderate")
def api_admin_moderate(req: ModerateRequest):
    """Keep a stored result out of Trending, or delete it outright."""
    if not _admin_ok(req.key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if req.action == "hide":
        ok = hide_from_trending(req.url_key)
    elif req.action == "delete":
        ok = delete_result(req.url_key)
    else:
        return JSONResponse(status_code=422, content={"detail": "Bad action."})
    _recent_cache["t"] = 0  # Trending refreshes on the next request
    return {"ok": ok}


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


@app.get("/api/admin/hivetest")
def api_admin_hivetest(key: str = ""):
    """One real Hive call, with the vendor's exact answer. Admin only —
    this is how we learn WHY a detector call is refused instead of
    guessing from documentation."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    out = {"authenticity_enabled": AUTHENTICITY_ENABLED,
           "key_present": bool((os.environ.get("HIVE_API_KEY") or "").strip()),
           "key_length": len((os.environ.get("HIVE_API_KEY") or "").strip()),
           "model": hive_detect.HIVE_MODEL,
           "endpoint": hive_detect.HIVE_V3_BASE}
    try:
        out["result"] = hive_detect.selftest()
    except Exception as e:
        out["result"] = {"ok": False, "stage": "crash", "detail": str(e)[:400]}
    return out


@app.get("/api/admin/rescuetest")
def api_admin_rescuetest(key: str = "", url: str = ""):
    """One real rescue-service (EnsembleData) call for an Instagram or
    TikTok link, with the vendor's exact answer: is the token valid, are
    there units left, did today's cap hit. Admin only; never reveals the
    token. Usage: /api/admin/rescuetest?key=ADMIN&url=<reel link>"""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if not url:
        return {"ok": False, "detail": "add &url=<an Instagram reel link> to test"}
    try:
        from app.agents import rescue as _rescue
        return _rescue.selftest(url)
    except Exception as e:
        return {"ok": False, "stage": "crash", "detail": str(e)[:400]}


@app.get("/api/admin/calendar")
def api_admin_calendar(key: str = "", month: str = ""):
    """Per-day visitors / fresh checks / cost for one month (admin)."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        month = time.strftime("%Y-%m")
    return {"month": month, "days": month_calendar(month)}


@app.get("/api/admin/day")
def api_admin_day(key: str = "", date: str = ""):
    """Everything about one day (admin)."""
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return JSONResponse(status_code=422, content={"detail": "date must be YYYY-MM-DD"})
    return day_detail(date)


@app.get("/api/admin/stats")
def api_admin_stats(key: str = ""):
    if not _admin_ok(key):
        return JSONResponse(status_code=403, content={"detail": "Forbidden."})
    checks, spent = today_usage()
    stats = quality_stats()
    stats["today"] = {"fresh_checks": checks, "est_spend_usd": spent}
    stats["cache_keepalive"] = _CACHE_WARM
    return stats


def _ev_brave_on() -> bool:
    try:
        from app.agents import evidence as _ev
        return bool(_ev.brave_available())
    except Exception:
        return False


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
        "visitors_monthly": visitor_monthly(),
        "total_checks": total_fresh_checks(),
        "cache_ttl_days": CACHE_TTL_DAYS,
        # v0.66.5: which evidence hunt is live. Brave = ~2-4s per claim;
        # Claude's built-in web search = ~10-20s per claim (the slow path).
        "evidence_backend": ("brave" if _ev_brave_on() else "claude_web_search"),
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
    "og-icon.png": "image/png",   # v0.66.7: small square = compact iMessage card
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


@app.get("/og-icon.png", include_in_schema=False)
def og_icon() -> FileResponse:
    return _pwa_file("og-icon.png")


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
    # CACHE KEEP-ALIVE: re-read the judges' shared rulebook every 50 min
    # so its 1-hour cache never lapses — the rulebook is then billed at
    # 10% on every real judge call, all day, every category. Cost: pennies.
    if os.environ.get("GLOWBY_CACHE_KEEPALIVE", "1") == "1":
        def _warm_loop():
            import time as _t
            _t.sleep(20)  # let the app finish booting
            while True:
                try:
                    from app.agents.judge import keep_cache_warm
                    _CACHE_WARM["last"] = keep_cache_warm()
                    _CACHE_WARM["at"] = _t.time()
                except Exception as e:
                    _CACHE_WARM["last"] = {"error": str(e)[:160]}
                try:
                    if _review_due():
                        run_flag_review(reason="weekly")
                except Exception:
                    pass
                _t.sleep(50 * 60)
        threading.Thread(target=_warm_loop, daemon=True).start()


_CACHE_WARM = {"last": None, "at": None}
