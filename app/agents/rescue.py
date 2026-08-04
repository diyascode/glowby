"""
Rescue tier — the locksmith on retainer.

When the free download path (yt-dlp) gets bot-blocked by TikTok or
Instagram, this module asks a professional scraping API (EnsembleData)
for the video's direct CDN link — which is served without a bot wall.
The pipeline then proceeds exactly as if the download had worked.

Armor:
- Dormant until GLOWBY_RESCUE_TOKEN is set in Railway Variables.
- Hard daily call cap (GLOWBY_RESCUE_DAILY_CALLS, default 40) counted
  in the same daily_events table the dashboard reads — rescue spend is
  visible and bounded, never a surprise bill.
- Every failure returns None: the caller falls back to the existing
  honest error. Rescue can only ever ADD successes.
"""

import json
import os
import urllib.parse
import urllib.request

RESCUE_TOKEN = os.environ.get("GLOWBY_RESCUE_TOKEN", "")
RESCUE_DAILY_CALLS = int(os.environ.get("GLOWBY_RESCUE_DAILY_CALLS", "40"))

_ED_BASE = "https://ensembledata.com/apis"


def _allowed() -> bool:
    """Token present and today's rescue count under the cap."""
    if not RESCUE_TOKEN:
        return False
    try:
        from app.storage import event_stats

        used = (event_stats().get("rescue") or {}).get("today", 0)
        return used < RESCUE_DAILY_CALLS
    except Exception:
        return True  # storage down should not disable rescues


def _count() -> None:
    try:
        from app.storage import record_event

        record_event("rescue")
    except Exception:
        pass


def _ed_get(path: str, params: dict, timeout: int = 25):
    """One EnsembleData call -> parsed JSON data, or None."""
    q = dict(params)
    q["token"] = RESCUE_TOKEN
    full = f"{_ED_BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(full, headers={"User-Agent": "glowby/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        return body.get("data") if isinstance(body, dict) else None
    except Exception:
        return None


def _first_url(*candidates):
    """First non-empty URL from nested url_list shapes."""
    for c in candidates:
        if not c:
            continue
        if isinstance(c, str):
            return c
        if isinstance(c, dict):
            lst = c.get("url_list") or []
            if lst:
                return lst[0]
    return None


def rescue_media(url: str, platform: str):
    """Blocked URL in -> {media_url, title, uploader, duration_seconds,
    posted_date} out, or None. Counts against the daily rescue cap."""
    if platform not in ("tiktok", "instagram") or not _allowed():
        return None
    _count()
    if platform == "tiktok":
        return _rescue_tiktok(url)
    return _rescue_instagram(url)


def _rescue_tiktok(url: str):
    data = _ed_get("/tt/post/info", {"url": url})
    if not isinstance(data, dict):
        return None
    # aweme_detail shape (or already unwrapped)
    d = data.get("aweme_detail") or data
    video = d.get("video") or {}
    media = _first_url(
        video.get("play_addr"),
        video.get("download_addr"),
        video.get("download_no_watermark_addr"),
    )
    if not media:
        return None
    author = d.get("author") or {}
    ts = d.get("create_time")
    posted = None
    if ts:
        try:
            import datetime

            posted = datetime.datetime.utcfromtimestamp(
                int(ts)).strftime("%Y-%m-%d")
        except Exception:
            posted = None
    return {
        "media_url": media,
        "title": (d.get("desc") or "(untitled)")[:200],
        "uploader": author.get("nickname")
        or author.get("unique_id") or "(unknown)",
        "duration_seconds": int((video.get("duration") or 0) / 1000)
        if video.get("duration") else 0,
        "posted_date": posted,
    }


def _rescue_instagram(url: str):
    """Instagram Reels via the same provider. The endpoint wants the
    post SHORTCODE (instagram.com/reel/<code>/), not the URL. Response
    shapes vary by endpoint version, so this parses defensively."""
    import re

    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    data = _ed_get("/instagram/post/details",
                   {"code": m.group(1), "n_comments_to_fetch": 0})
    if not isinstance(data, dict):
        return None
    media = _first_url(
        data.get("video_url"),
        (data.get("video_versions") or [{}])[0].get("url")
        if data.get("video_versions") else None,
    )
    if not media:
        return None
    user = data.get("user") or data.get("owner") or {}
    caption = data.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    return {
        "media_url": media,
        "title": (caption or "(Instagram reel)")[:200],
        "uploader": user.get("username") or "(unknown)",
        "duration_seconds": int(data.get("video_duration") or 0),
        "posted_date": None,
    }
