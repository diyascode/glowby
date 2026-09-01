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
import urllib.request

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
TIMEOUT_S = 30
MONTHLY_CAP = int(os.environ.get("GLOWBY_VISION_MONTHLY_CAP", "900"))
RECYCLED_GAP_DAYS = 30  # older than the post by this much = flagged

# in-memory monthly counter (resets on redeploy — conservative enough
# under the 10% headroom left below Google's free tier)
_counter = {"month": "", "count": 0}


def available():
    return bool((os.environ.get("GOOGLE_VISION_KEY") or "").strip())


def _month():
    return time.strftime("%Y-%m")


def _budget_ok():
    m = _month()
    if _counter["month"] != m:
        _counter["month"], _counter["count"] = m, 0
    return _counter["count"] < MONTHLY_CAP


def _spend():
    _counter["count"] += 1


def _not_assessed(reason):
    return {"assessment_status": "not_assessed", "evidence": [],
            "earliest": None, "context_note": None, "reason": reason}


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
    try:
        _spend()
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return _failed(f"vision call failed: {type(e).__name__}")

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
    return {"assessment_status": "completed", "evidence": evidence,
            "earliest": earliest, "context_note": note, "reason": None}
