"""
Pre-check crawler — the "2-second experience" engine.

Glowby fact-checks trending news videos BEFORE anyone pastes them, so
the videos people are most likely to check are already in the cache and
load instantly. Runs as a background thread inside the web app.

Knobs (Railway Variables — all optional):
  GLOWBY_PRECHECK=off               turn the crawler off entirely
  GLOWBY_PRECHECK_INTERVAL_HOURS    how often to crawl (default 6)
  GLOWBY_PRECHECK_PER_RUN           max fresh videos per crawl (default 3)
  GLOWBY_PRECHECK_BUDGET_SHARE      max share of the daily budget the
                                    crawler may spend (default 0.4 —
                                    it can never starve real visitors)

Crawls YouTube's News & Politics trending chart (where misinformation
lives), skips videos longer than 4 minutes, and never re-checks a video
already in the cache.
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

from app.storage import add_usage, canonical_key, get_cached, recent_failed_urls, today_usage

ENABLED = os.environ.get("GLOWBY_PRECHECK", "on").lower() in (
    "on", "true", "1", "yes")
INTERVAL_HOURS = float(os.environ.get("GLOWBY_PRECHECK_INTERVAL_HOURS", "6"))
PER_RUN = int(os.environ.get("GLOWBY_PRECHECK_PER_RUN", "3"))
BUDGET_SHARE = float(os.environ.get("GLOWBY_PRECHECK_BUDGET_SHARE", "0.4"))
MAX_DURATION_SECONDS = 240  # short-form only; a 40-min stream isn't v1
STARTUP_DELAY_SECONDS = 180  # let a fresh deploy settle before crawling

_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_duration_seconds(s):
    """YouTube's ISO-8601 durations (PT1M30S) -> seconds. None if unreadable."""
    m = _DUR.fullmatch((s or "").strip())
    if not m:
        return None
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def fetch_trending(api_key: str, max_results: int = 25) -> list:
    """Trending News & Politics videos from the YouTube Data API."""
    params = urllib.parse.urlencode({
        "part": "contentDetails",
        "chart": "mostPopular",
        "videoCategoryId": "25",  # News & Politics
        "regionCode": "US",
        "maxResults": max_results,
        "key": api_key,
    })
    url = "https://www.googleapis.com/youtube/v3/videos?" + params
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return parse_trending(data)


def parse_trending(data: dict) -> list:
    out = []
    for it in (data or {}).get("items", []):
        vid = it.get("id")
        dur = iso_duration_seconds(
            ((it.get("contentDetails") or {}).get("duration")))
        if not vid or dur is None:
            continue
        out.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration_seconds": dur,
        })
    return out


_FAILED: dict = {}  # url -> when it last failed (this process)
SKIP_FAILED_DAYS = 7


def pick_candidates(videos: list, limit: int, is_cached, failed: set | None = None) -> list:
    """Short, not-yet-checked videos, up to `limit` — never one that
    failed in the last week (a geo-blocked trending video used to be
    retried every run, each time counted and charged as a check)."""
    picks = []
    failed = failed or set()
    for v in videos:
        if v["duration_seconds"] > MAX_DURATION_SECONDS:
            continue
        if is_cached(v["url"]):
            continue
        if v["url"] in failed or time.time() - _FAILED.get(v["url"], 0) < SKIP_FAILED_DAYS * 86400:
            continue
        picks.append(v)
        if len(picks) >= limit:
            break
    return picks


def crawl_once(run_pipeline, daily_budget: float, cost_per_check: float, job_status=None) -> int:
    """One crawl pass. Returns how many videos were pre-checked (a check
    is counted and charged only when the pipeline actually finished)."""
    api_key = os.environ.get("YOUTUBE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return 0
    try:
        videos = fetch_trending(api_key)
    except Exception:
        return 0
    try:
        from app.boost import precheck_per_run as _ppr
        per_run = _ppr(PER_RUN)
    except Exception:
        per_run = PER_RUN
    picks = pick_candidates(
        videos, per_run,
        lambda u: get_cached(canonical_key(u)) is not None,
        failed=recent_failed_urls(SKIP_FAILED_DAYS))
    done = 0
    for v in picks:
        try:
            _, spent = today_usage()
        except Exception:
            break
        # the crawler only spends its SHARE of the budget — real
        # visitors always keep the rest
        budget = daily_budget() if callable(daily_budget) else daily_budget
        if spent + cost_per_check > budget * BUDGET_SHARE:
            break
        job_id = "pre-" + uuid.uuid4().hex[:8]
        try:
            run_pipeline(job_id, v["url"], canonical_key(v["url"]))
        except Exception:
            _FAILED[v["url"]] = time.time()
            continue
        status = job_status(job_id) if job_status else "done"
        if status == "done":
            add_usage(cost_per_check)  # counted + charged only when it ran
            done += 1
        else:
            _FAILED[v["url"]] = time.time()
    return done


def start_precheck(run_pipeline, daily_budget: float, cost_per_check: float, job_status=None) -> None:
    """Start the background crawler thread (no-op when disabled)."""
    if not ENABLED or PER_RUN <= 0:
        return

    def loop():
        time.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                crawl_once(run_pipeline, daily_budget, cost_per_check, job_status)
            except Exception:
                pass
            time.sleep(max(60.0, INTERVAL_HOURS * 3600))

    threading.Thread(target=loop, daemon=True, name="glowby-precheck").start()
