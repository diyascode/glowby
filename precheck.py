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

from app.storage import add_usage, canonical_key, get_cached, today_usage

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


def pick_candidates(videos: list, limit: int, is_cached) -> list:
    """Short, not-yet-checked videos, up to `limit`."""
    picks = []
    for v in videos:
        if v["duration_seconds"] > MAX_DURATION_SECONDS:
            continue
        if is_cached(v["url"]):
            continue
        picks.append(v)
        if len(picks) >= limit:
            break
    return picks


def crawl_once(run_pipeline, daily_budget: float, cost_per_check: float) -> int:
    """One crawl pass. Returns how many videos were pre-checked."""
    api_key = os.environ.get("YOUTUBE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return 0
    try:
        videos = fetch_trending(api_key)
    except Exception:
        return 0
    picks = pick_candidates(
        videos, PER_RUN,
        lambda u: get_cached(canonical_key(u)) is not None)
    done = 0
    for v in picks:
        try:
            _, spent = today_usage()
        except Exception:
            break
        # the crawler only spends its SHARE of the budget — real
        # visitors always keep the rest
        if spent + cost_per_check > daily_budget * BUDGET_SHARE:
            break
        add_usage(cost_per_check)
        job_id = "pre-" + uuid.uuid4().hex[:8]
        try:
            run_pipeline(job_id, v["url"], canonical_key(v["url"]))
            done += 1
        except Exception:
            pass
    return done


def start_precheck(run_pipeline, daily_budget: float, cost_per_check: float) -> None:
    """Start the background crawler thread (no-op when disabled)."""
    if not ENABLED or PER_RUN <= 0:
        return

    def loop():
        time.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                crawl_once(run_pipeline, daily_budget, cost_per_check)
            except Exception:
                pass
            time.sleep(max(60.0, INTERVAL_HOURS * 3600))

    threading.Thread(target=loop, daemon=True, name="glowby-precheck").start()
