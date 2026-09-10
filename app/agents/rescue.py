"""
Rescue tier — the locksmith on retainer.

When the free download path (yt-dlp) gets bot-blocked by TikTok or
Instagram, this module asks a professional scraping API (Scrape Creators
when SCRAPECREATORS_KEY is set; otherwise EnsembleData via
GLOWBY_RESCUE_TOKEN) for the video's direct CDN link — which is served without a bot wall.
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
import urllib.error
import urllib.parse
import urllib.request

RESCUE_TOKEN = (os.environ.get("GLOWBY_RESCUE_TOKEN", "") or "").strip().strip("\"'")
RESCUE_DAILY_CALLS = int(os.environ.get("GLOWBY_RESCUE_DAILY_CALLS", "40"))
# Scrape Creators — the cheaper locksmith (100 free credits, then a
# one-time credit pack; ~0.2 cents per Instagram fetch). Preferred when
# its key is present; EnsembleData stays as the fallback provider.
SC_KEY = (os.environ.get("SCRAPECREATORS_KEY", "") or "").strip().strip("\"'")
_SC_BASE = "https://api.scrapecreators.com/v1"

_ED_BASE = "https://ensembledata.com/apis"


def provider() -> str:
    """Which rescue service is configured: 'scrapecreators', 'ensembledata'
    or 'none'. Scrape Creators wins when both keys exist."""
    if SC_KEY:
        return "scrapecreators"
    if RESCUE_TOKEN:
        return "ensembledata"
    return "none"


def _allowed() -> bool:
    """A provider is configured and today's rescue count is under the cap."""
    if provider() == "none":
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


# the vendor's last answer, kept for the admin diagnostic (never the token)
LAST = {"path": None, "http": None, "detail": None, "ok": None, "units_left": None, "provider": None}


def _ed_get(path: str, params: dict, timeout: int = 25):
    """One EnsembleData call -> parsed JSON data, or None. The vendor's
    HTTP status and message are kept in LAST so a failure can be
    diagnosed (bad token = 401, out of units = 402/403, throttled = 429)
    instead of guessed."""
    q = dict(params)
    q["token"] = RESCUE_TOKEN
    full = f"{_ED_BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(full, headers={"User-Agent": "glowby/1.0"})
    LAST.update({"path": path, "http": None, "detail": None, "ok": None,
                 "provider": "ensembledata"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            LAST["http"] = getattr(resp, "status", 200)
        body = json.loads(raw)
        if isinstance(body, dict):
            LAST["units_left"] = body.get("units_left") or body.get("unitsLeft")
            LAST["ok"] = True
            return body.get("data")
        LAST.update({"ok": False, "detail": "non-object response"})
        return None
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            msg = ""
        LAST.update({"http": e.code, "ok": False, "detail": msg or str(e)[:200]})
        return None
    except Exception as e:
        LAST.update({"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"})
        return None


def selftest(url: str) -> dict:
    """Admin diagnostic: one real Instagram/TikTok rescue call for the
    given link, with the vendor's exact answer. Reports token presence
    and today's cap usage — never the token itself."""
    out = {"provider": provider(),
           "scrapecreators_key_present": bool(SC_KEY),
           "ensembledata_token_present": bool(RESCUE_TOKEN),
           "token_present": bool(RESCUE_TOKEN), "token_length": len(RESCUE_TOKEN),
           "daily_cap": RESCUE_DAILY_CALLS}
    try:
        from app.storage import event_stats
        out["used_today"] = (event_stats().get("rescue") or {}).get("today", 0)
    except Exception as e:
        out["used_today"] = f"unknown ({type(e).__name__})"
    out["allowed_now"] = _allowed()
    if provider() == "none":
        out.update({"ok": False, "stage": "config",
                    "detail": "no rescue provider configured — set SCRAPECREATORS_KEY (preferred) or GLOWBY_RESCUE_TOKEN"})
        return out
    platform = "instagram" if "instagram.com" in url else "tiktok"
    if provider() == "scrapecreators":
        got = _sc_instagram(url) if platform == "instagram" else _sc_tiktok(url)
    else:
        got = _rescue_instagram(url) if platform == "instagram" else _rescue_tiktok(url)
    out["platform"] = platform
    out["vendor"] = dict(LAST)
    if got and got.get("media_url"):
        out.update({"ok": True, "stage": "media_link",
                    "title": got.get("title"), "uploader": got.get("uploader"),
                    "duration_seconds": got.get("duration_seconds"),
                    "media_host": urllib.parse.urlparse(got["media_url"]).netloc})
    else:
        http = LAST.get("http")
        hint = {401: "key/token rejected — paste the key from the provider's dashboard (Scrape Creators: SCRAPECREATORS_KEY; EnsembleData: GLOWBY_RESCUE_TOKEN)",
                402: "account out of units — top up / check the EnsembleData plan",
                403: "token refused or plan does not include this endpoint",
                404: "post not found — private account, deleted reel, or wrong link",
                429: "vendor throttled us — wait and retry"}.get(http)
        if http is None and LAST.get("ok"):
            hint = "vendor answered but no video link was in the response (private/removed reel, or response shape changed)"
        out.update({"ok": False, "stage": "vendor", "hint": hint or "see vendor.detail"})
    return out


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


def _sc_get(path: str, params: dict, timeout: int = 30):
    """One Scrape Creators call -> parsed JSON body, or None. Vendor
    status/message kept in LAST (never the key)."""
    full = f"{_SC_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"x-api-key": SC_KEY,
                                                "User-Agent": "glowby/1.0"})
    LAST.update({"path": path, "http": None, "detail": None, "ok": None,
                 "provider": "scrapecreators"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            LAST["http"] = getattr(resp, "status", 200)
        body = json.loads(raw)
        if isinstance(body, dict):
            LAST["units_left"] = body.get("credits_remaining")
            LAST["ok"] = bool(body.get("success", True))
            if not LAST["ok"]:
                LAST["detail"] = str(body.get("message") or body.get("error") or "")[:300]
            return body
        LAST.update({"ok": False, "detail": "non-object response"})
        return None
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            msg = ""
        LAST.update({"http": e.code, "ok": False, "detail": msg or str(e)[:200]})
        return None
    except Exception as e:
        LAST.update({"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"})
        return None


def parse_sc_instagram(body) -> dict | None:
    """Pure (unit-tested): Scrape Creators /v1/instagram/post body ->
    {media_url, title, uploader, duration_seconds, posted_date} or None.
    Documented shape: data.xdt_shortcode_media.{video_url, owner.username,
    video_duration, taken_at_timestamp, edge_media_to_caption.edges[0].node.text}.
    The video link is also deep-searched in case the wrapper changes."""
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    m = data.get("xdt_shortcode_media") if isinstance(data, dict) else None
    if not isinstance(m, dict):
        m = data if isinstance(data, dict) else {}
    media = m.get("video_url")
    if not media:
        vv = m.get("video_versions")
        if isinstance(vv, list) and vv and isinstance(vv[0], dict):
            media = vv[0].get("url")
    if not media:
        media = _deep_find_video(body)
    if not media or not str(media).startswith("http"):
        return None
    caption = None
    try:
        caption = m["edge_media_to_caption"]["edges"][0]["node"]["text"]
    except Exception:
        cap = m.get("caption")
        caption = cap.get("text") if isinstance(cap, dict) else cap
    owner = m.get("owner") or {}
    user = owner.get("username") if isinstance(owner, dict) else None
    dur = m.get("video_duration") or m.get("duration")
    posted = None
    ts = m.get("taken_at_timestamp") or m.get("taken_at")
    if ts:
        try:
            import datetime
            posted = datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except Exception:
            posted = None
    return {
        "media_url": str(media),
        "title": (str(caption) if caption else "(Instagram reel)")[:200],
        "uploader": str(user) if user else "(unknown)",
        "duration_seconds": int(float(dur)) if dur else 0,
        "posted_date": posted,
    }


def parse_sc_tiktok(body) -> dict | None:
    """Pure: Scrape Creators /v1/tiktok/video body -> rescue dict or None.
    Shape mirrors TikTok's aweme_detail; deep-search covers drift."""
    if not isinstance(body, dict):
        return None
    d = body.get("aweme_detail") or (body.get("data") or {}).get("aweme_detail") or body
    if not isinstance(d, dict):
        return None
    video = d.get("video") or {}
    media = _first_url(video.get("play_addr"), video.get("download_addr")) if isinstance(video, dict) else None
    if not media:
        media = _deep_find_video(body)
    if not media:
        return None
    author = d.get("author") or {}
    ts = d.get("create_time")
    posted = None
    if ts:
        try:
            import datetime
            posted = datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except Exception:
            posted = None
    dur = video.get("duration") if isinstance(video, dict) else None
    return {
        "media_url": media,
        "title": (d.get("desc") or "(untitled)")[:200],
        "uploader": (author.get("nickname") or author.get("unique_id") or "(unknown)") if isinstance(author, dict) else "(unknown)",
        "duration_seconds": int(dur / 1000) if dur and dur > 1000 else int(dur or 0),
        "posted_date": posted,
    }


def _sc_instagram(url: str):
    body = _sc_get("/instagram/post", {"url": url.split("?")[0], "trim": "true"})
    return parse_sc_instagram(body)


def _sc_tiktok(url: str):
    body = _sc_get("/tiktok/video", {"url": url})
    return parse_sc_tiktok(body)


def rescue_media(url: str, platform: str):
    """Blocked URL in -> {media_url, title, uploader, duration_seconds,
    posted_date} out, or None. Counts against the daily rescue cap."""
    if platform not in ("tiktok", "instagram") or not _allowed():
        return None
    _count()
    if provider() == "scrapecreators":
        return _sc_tiktok(url) if platform == "tiktok" else _sc_instagram(url)
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


def _deep_find_video(obj, depth=0):
    """Walk any nested dict/list and return the first plausible video
    CDN url. Instagram's response nesting varies wildly by endpoint
    version, so we search rather than guess the exact path."""
    if depth > 8:
        return None
    if isinstance(obj, str):
        s = obj
        if (s.startswith("http") and (".mp4" in s or "video" in s)
                and "http" in s):
            return s
        return None
    if isinstance(obj, dict):
        # strong signals first
        for k in ("video_url", "play_addr", "download_url", "src"):
            v = obj.get(k)
            hit = _deep_find_video(v, depth + 1)
            if hit:
                return hit
        if isinstance(obj.get("video_versions"), list) and obj["video_versions"]:
            hit = _deep_find_video(obj["video_versions"], depth + 1)
            if hit:
                return hit
        for v in obj.values():
            hit = _deep_find_video(v, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _deep_find_video(v, depth + 1)
            if hit:
                return hit
    return None


def _deep_get(obj, keys, depth=0):
    """First value found for any of `keys` anywhere in the structure."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k):
                return obj[k]
        for v in obj.values():
            hit = _deep_get(v, keys, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _deep_get(v, keys, depth + 1)
            if hit:
                return hit
    return None


def _rescue_instagram(url: str):
    """Instagram Reels via the same provider. The endpoint wants the
    post SHORTCODE (instagram.com/reel/<code>/), not the URL. The
    response is deeply nested and varies by version, so we deep-search
    it for the video link instead of assuming a fixed path."""
    import re

    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    data = _ed_get("/instagram/post/details",
                   {"code": m.group(1), "n_comments_to_fetch": 0})
    if not isinstance(data, (dict, list)):
        return None
    media = _deep_find_video(data)
    if not media:
        return None
    caption = _deep_get(data, ("caption", "text", "title"))
    if isinstance(caption, dict):
        caption = caption.get("text")
    user = _deep_get(data, ("username", "owner_username"))
    dur = _deep_get(data, ("video_duration", "duration"))
    return {
        "media_url": media,
        "title": (str(caption) if caption else "(Instagram reel)")[:200],
        "uploader": str(user) if user else "(unknown)",
        "duration_seconds": int(float(dur)) if dur else 0,
        "posted_date": None,
    }
