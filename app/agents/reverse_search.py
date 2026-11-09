"""
Media Authenticity Engine — Stage 3 (Day 3 of the v2 build plan).

Reverse image search via Google Cloud Vision Web Detection. This lane
catches the trick no AI detector can: REAL footage recycled with a new
caption ("this is happening RIGHT NOW") when it actually first appeared
years earlier or somewhere else entirely.

Design rules (locked, from the reviewed plan):
- CONTEXT evidence ranks BELOW forensic in the hierarchy and never
  changes the AI-origin category — an old real photo is not "synthetic";
  it is real media used misleadingly. It gets its own context finding.
- HONEST LANGUAGE: we report the "earliest credible matching appearance
  located", never "the origin" — the open web cannot prove origin.
- DORMANT WITHOUT A KEY: no GOOGLE_VISION_KEY -> typed not_assessed.
- FREE-TIER GUARD: a monthly call cap (default 900, under Google's
  1,000 free lookups) so this lane can never generate a bill.
- Never break a check: every failure is typed, nothing is invented.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
TIMEOUT_S = 30
MONTHLY_CAP = int(os.environ.get("GLOWBY_VISION_MONTHLY_CAP", "900"))
RECYCLED_GAP_DAYS = 30  # older than the post by this much = flagged

# v0.66.57: this counter used to live only in memory and reset on every
# redeploy. That was harmless while Vision had no billing account and
# simply refused past the free tier — but the moment billing is attached,
# Google stops refusing and starts charging, and a counter that forgets
# itself ten times a day is no cap at all. It is now kept in the database
# so a month means a month. (Inderpreet, Nov 9)
_counter = {"month": "", "count": 0}


def _load_count(m: str) -> int:
    try:
        from app.marketing import setting
        return int(setting(f"vision:used:{m}") or 0)
    except Exception:
        return 0


def _save_count(m: str, n: int) -> None:
    try:
        from app.marketing import set_setting
        set_setting(f"vision:used:{m}", str(n))
    except Exception:
        pass


def _sync(m: str) -> None:
    """Pull the stored total in whenever the month turns or we just booted."""
    if _counter["month"] != m:
        _counter["month"], _counter["count"] = m, _load_count(m)


def usage() -> dict:
    """Where this month's free allowance stands — for the system check."""
    m = _month()
    _sync(m)
    return {"month": m, "used": _counter["count"], "cap": MONTHLY_CAP,
            "left": max(0, MONTHLY_CAP - _counter["count"])}


def available():
    return bool((os.environ.get("GOOGLE_VISION_KEY") or "").strip())


def _month():
    return time.strftime("%Y-%m")


def _budget_ok():
    m = _month()
    _sync(m)
    return _counter["count"] < MONTHLY_CAP


def _spend():
    m = _month()
    _sync(m)
    _counter["count"] += 1
    _save_count(m, _counter["count"])


def _not_assessed(reason):
    return {"assessment_status": "not_assessed", "evidence": [],
            "earliest": None, "context_note": None, "reason": reason}


def _health(ok, err=""):
    """Report to the API ledger. A lane that fails silently is a lane
    nobody fixes (v0.66.54)."""
    try:
        from app.apihealth import note
        note("reverse_search", ok, err)
    except Exception:
        pass


def selftest(image_b64: str = None) -> dict:
    """Admin diagnostic: one real Vision call, with Google's exact answer.
    Uses a 1x1 JPEG — enough to prove the key and the API are live."""
    if not available():
        return {"ok": False, "stage": "config", "detail": "GOOGLE_VISION_KEY is not set"}
    img = image_b64 or (
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
        "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
        "wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAA"
        "AAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
    key = (os.environ.get("GOOGLE_VISION_KEY") or "").strip()
    body = json.dumps({"requests": [{"image": {"content": img},
                                     "features": [{"type": "WEB_DETECTION", "maxResults": 1}]}]}).encode()
    req = urllib.request.Request(f"{VISION_URL}?key={key}", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=min(TIMEOUT_S, 20)) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        return {"ok": False, "stage": "call", "detail": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"ok": False, "stage": "call", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    resp = (payload.get("responses") or [{}])[0]
    if resp.get("error"):
        return {"ok": False, "stage": "api", "detail": json.dumps(resp["error"])[:300]}
    return {"ok": True, "stage": "parsed", "detail": "Vision answered and the response parsed"}


def _failed(reason):
    return {"assessment_status": "failed", "evidence": [],
            "earliest": None, "context_note": None, "reason": reason}


# ------------------------------------------------------------ dates
_URL_DATE = re.compile(
    r"(?:^|[/_.-])(20[0-2]\d)[/_-](0[1-9]|1[0-2])(?:[/_-](0[1-9]|[12]\d|3[01]))?(?:[/_.-]|$)")
_TITLE_YEAR = re.compile(r"\b(20[0-2]\d)\b")


def extract_date(url="", title=""):
    """Pure function (unit-tested): best-effort date from a URL path or
    page title. URL year+month is a strong signal; a bare year in a
    title is weak and only used when the URL gives nothing. Returns
    ISO date string or None. Never guesses beyond what is written."""
    m = _URL_DATE.search(url or "")
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3) or "01"
        return f"{y}-{mo}-{d}"
    m = _TITLE_YEAR.search(title or "")
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _domain(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").replace("www.", "")


def pick_earliest(pages, posted_date=None):
    """Pure function (unit-tested): from web-detection page matches,
    find the earliest DATED appearance. pages = [{url, pageTitle}].
    Returns (earliest_dict_or_None, context_note_or_None).

    The note fires ONLY when the earliest appearance predates the
    video's posting date by more than RECYCLED_GAP_DAYS — same-week
    coverage of a real event is normal, not suspicious."""
    dated = []
    for p in pages or []:
        u = p.get("url") or ""
        d = extract_date(u, p.get("pageTitle") or "")
        if d:
            dated.append({"date": d, "url": u, "domain": _domain(u),
                          "title": (p.get("pageTitle") or "")[:120]})
    if not dated:
        return None, None
    dated.sort(key=lambda x: x["date"])
    earliest = dated[0]
    note = None
    if posted_date:
        try:
            t_early = time.mktime(time.strptime(earliest["date"], "%Y-%m-%d"))
            t_post = time.mktime(time.strptime(str(posted_date)[:10], "%Y-%m-%d"))
            if (t_post - t_early) > RECYCLED_GAP_DAYS * 86400:
                note = ("Earliest credible matching appearance located: "
                        f"{earliest['date']} ({earliest['domain']}) — this "
                        "footage appeared online well before this post. "
                        "It may be older media reused in a new context.")
        except Exception:
            pass
    return earliest, note


# ------------------------------------------------------------ the call
def analyze(image_b64, posted_date=None):
    """Reverse-search one image/frame. Returns typed result with
    context evidence; never a verdict about AI origin."""
    if not available():
        return _not_assessed("no GOOGLE_VISION_KEY configured")
    if not _budget_ok():
        return _not_assessed("monthly free-tier budget reached")
    try:
        base64.b64decode(image_b64)
    except Exception:
        return _failed("image bytes unreadable")
    key = os.environ.get("GOOGLE_VISION_KEY").strip()
    body = json.dumps({"requests": [{
        "image": {"content": image_b64},
        "features": [{"type": "WEB_DETECTION", "maxResults": 15}],
    }]}).encode()
    req = urllib.request.Request(
        f"{VISION_URL}?key={key}", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    # v0.66.54: this lane used to swallow its own errors — the exception
    # type and nothing else, reported to nobody. Google's console showed
    # 100% errors on Cloud Vision while Glowby had no idea anything was
    # wrong, which is the third time the same silent-failure pattern has
    # cost us (Hive, the fact-check lane, now this). Google's own words
    # are kept, and every call reports to the health ledger.
    try:
        _spend()
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        detail = f"HTTP {e.code}: {body}"
        _health(False, detail[:160])
        return _failed(f"vision call failed: {detail}")
    except Exception as e:
        _health(False, f"{type(e).__name__}: {str(e)[:120]}")
        return _failed(f"vision call failed: {type(e).__name__}: {str(e)[:120]}")
    _health(True)

    try:
        web = (payload.get("responses") or [{}])[0].get("webDetection") or {}
    except Exception:
        return _failed("unexpected response shape")
    pages = web.get("pagesWithMatchingImages") or []
    full = web.get("fullMatchingImages") or []
    earliest, note = pick_earliest(pages, posted_date)

    evidence = []
    if earliest:
        evidence.append({
            "provider": "google_vision", "signal_type": "earliest_appearance",
            "raw_score": None, "band": None,
            "explanation": ("Earliest credible matching appearance "
                            f"located: {earliest['date']} "
                            f"({earliest['domain']}). This is the "
                            "earliest dated match found, not proof of "
                            "origin."),
            "source_link": earliest["url"],
        })
    elif full or pages:
        evidence.append({
            "provider": "google_vision", "signal_type": "matches_found",
            "raw_score": None, "band": None,
            "explanation": (f"{len(full) + len(pages)} matching "
                            "appearance(s) found on the web; none "
                            "carried a readable date."),
            "source_link": (pages[0].get("url") if pages else None),
        })
    else:
        evidence.append({
            "provider": "google_vision", "signal_type": "no_matches",
            "raw_score": None, "band": None,
            "explanation": ("No matching appearances found on the open "
                            "web. Absence proves nothing about origin "
                            "or authenticity."),
            "source_link": None,
        })
    seen, plist = set(), []
    for pg in (pages or [])[:12]:
        u = str(pg.get("url") or "")
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        plist.append({"url": u, "title": str(pg.get("pageTitle") or "")[:120],
                      "domain": re.sub(r"^https?://(www\.)?", "", u).split("/")[0].lower()})
    return {"assessment_status": "completed", "evidence": evidence,
            "earliest": earliest, "context_note": note, "reason": None,
            "pages": plist[:8], "match_count": len(full) + len(pages)}
