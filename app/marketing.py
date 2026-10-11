"""Marketing on autopilot (v0.66.22) — PRODUCTION is automatic, PUBLISHING
stays human where a human's voice is the point.

Every morning (7 am Pacific) Glowby picks yesterday's most-shared real
check, renders a 20-second vertical clip of it (the "here's the check"
cut: the claim, Share → Glowby, the ring drawing to the score, the
one-line answer, the sources, the end card), writes the caption, and
drops the finished video into Diya's TIKTOK DRAFTS (TikTok's "inbox"
upload). She opens it, records her voice over it, taps post. Nothing is
ever posted to TikTok by this code: an auto-posted feed with a synthetic
voice is exactly what Glowby teaches people to see through.

Every Monday (9 am Pacific) it composes a LINKEDIN post in Dad's voice —
what Glowby checked this week, three share cards — and QUEUES it. It
posts after approval in admin, or 24 hours later when
GLOWBY_LINKEDIN_AUTOPOST=1 (the preview sits in admin the whole time).

Rules baked in (see claude/glowby-rules-and-exceptions.md):
  * only REAL stored results — never a synthetic testimonial, never an
    invented check; general-rated content only; nothing scam-flagged;
  * the caption carries the band word (Accurate / Mostly accurate /
    Mixed / Inaccurate / Unverified) and the one-line answer — it never
    calls a creator a liar or a video fake (FORBIDDEN words fall back to
    a neutral line); no accuracy percentages; no feature teasers;
  * every caption says it was made with Glowby and reminds Diya to tick
    TikTok's AI-generated-content label (the clip is machine-rendered);
  * secrets live in Railway Variables; OAuth tokens live in Postgres;
    neither is ever logged.

Everything that talks to the outside world is behind small functions
(`_http`, `tiktok_*`, `linkedin_*`) so the tests can run the whole
pipeline against fakes. The video is rendered with Pillow frames piped
into ffmpeg (ffmpeg is on Railway for yt-dlp already).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _PT = timezone(timedelta(hours=-7))

PUBLIC_URL = os.environ.get("GLOWBY_PUBLIC_URL", "https://glowby.io").rstrip("/")
ENABLED = os.environ.get("GLOWBY_MARKETING", "1") == "1"
DAILY_HOUR = int(os.environ.get("GLOWBY_MARKETING_HOUR", "7"))       # Pacific
WEEKLY_HOUR = int(os.environ.get("GLOWBY_LINKEDIN_HOUR", "9"))        # Pacific, Mondays
AUTOPOST_LINKEDIN = os.environ.get("GLOWBY_LINKEDIN_AUTOPOST", "0") == "1"
AUTOPOST_DELAY_H = 24
LINKEDIN_VERSION = os.environ.get("GLOWBY_LINKEDIN_VERSION", "202609")
KEEP_POSTS = 30

TIKTOK_KEY = os.environ.get("GLOWBY_TIKTOK_CLIENT_KEY", "")
TIKTOK_SECRET = os.environ.get("GLOWBY_TIKTOK_CLIENT_SECRET", "")
LINKEDIN_ID = os.environ.get("GLOWBY_LINKEDIN_CLIENT_ID", "")
LINKEDIN_SECRET = os.environ.get("GLOWBY_LINKEDIN_CLIENT_SECRET", "")

# words a Glowby post never uses about a creator or a clip
FORBIDDEN = re.compile(r"\b(fake|faked|liar|lying|lies|lie|scam|scammer|fraud|hoax|propaganda|idiot|stupid)\b", re.I)
HASHTAGS = "#factcheck #isthisreal #glowby"
AI_LABEL_NOTE = "Before posting: tick TikTok's “AI-generated content” label (this clip is machine-rendered) and record your voice over it."
AI_LABEL_NOTE_VOICED = "Ready to post as is — it has a synthetic narrator (not Diya). Tick TikTok's “AI-generated content” label. Or mute it and talk over it yourself."


# ====================================================================== db
def _db():
    from app.storage import _get_conn
    return _get_conn()


def _peek(url_key: str):
    """A stored result without counting a view (None if unknown)."""
    from app.storage import peek_result
    return peek_result(url_key)


_READY = {"ok": False}


def _ensure():
    conn = _db()
    if conn is None:
        return None
    if _READY["ok"]:
        return conn
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS marketing_tokens (
                platform TEXT PRIMARY KEY, data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS marketing_posts (
                id SERIAL PRIMARY KEY, platform TEXT NOT NULL, kind TEXT NOT NULL,
                url_key TEXT, caption TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                detail JSONB NOT NULL DEFAULT '{}'::jsonb, media BYTEA,
                scheduled_for TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                posted_at TIMESTAMPTZ)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS marketing_settings (
                k TEXT PRIMARY KEY, v TEXT NOT NULL)""")
        _READY["ok"] = True
        return conn
    except Exception:
        return None


def setting(k: str, default: str = "") -> str:
    conn = _ensure()
    if conn is None:
        return default
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM marketing_settings WHERE k=%s", (k,))
            r = cur.fetchone()
            return r[0] if r else default
    except Exception:
        return default


def set_setting(k: str, v: str) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO marketing_settings (k, v) VALUES (%s, %s) "
                        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, v))
        return True
    except Exception:
        return False


def paused(platform: str) -> bool:
    return setting(f"paused:{platform}", "0") == "1"


def get_token(platform: str) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM marketing_tokens WHERE platform=%s", (platform,))
            r = cur.fetchone()
            return dict(r[0]) if r else None
    except Exception:
        return None


def save_token(platform: str, data: dict) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO marketing_tokens (platform, data, updated_at) VALUES (%s, %s::jsonb, now()) "
                        "ON CONFLICT (platform) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                        (platform, json.dumps(data)))
        return True
    except Exception:
        return False


def forget_token(platform: str) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM marketing_tokens WHERE platform=%s", (platform,))
        return True
    except Exception:
        return False


def _insert_post(platform, kind, url_key, caption, status, detail, media=None, scheduled_for=None) -> int | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO marketing_posts (platform, kind, url_key, caption, status, detail, media, scheduled_for) "
                        "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
                        (platform, kind, url_key, caption, status, json.dumps(detail or {}), media, scheduled_for))
            pid = cur.fetchone()[0]
            # keep the table small: media of old rows is dropped, rows beyond KEEP_POSTS deleted
            cur.execute("UPDATE marketing_posts SET media = NULL WHERE id NOT IN "
                        "(SELECT id FROM marketing_posts ORDER BY id DESC LIMIT 14)")
            cur.execute("DELETE FROM marketing_posts WHERE id NOT IN "
                        "(SELECT id FROM marketing_posts ORDER BY id DESC LIMIT %s)", (KEEP_POSTS,))
        return pid
    except Exception:
        return None


def _update_post(pid: int, status: str | None = None, detail: dict | None = None, posted: bool = False) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            if detail is not None:
                cur.execute("UPDATE marketing_posts SET detail = detail || %s::jsonb WHERE id=%s", (json.dumps(detail), pid))
            if status:
                cur.execute("UPDATE marketing_posts SET status=%s WHERE id=%s", (status, pid))
            if posted:
                cur.execute("UPDATE marketing_posts SET posted_at = now() WHERE id=%s", (pid,))
        return True
    except Exception:
        return False


def get_post(pid: int, with_media: bool = False) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, platform, kind, url_key, caption, status, detail, scheduled_for, created_at, posted_at"
                        + (", media" if with_media else ", NULL") +
                        " FROM marketing_posts WHERE id=%s", (pid,))
            r = cur.fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def _row(r) -> dict:
    d = {"id": r[0], "platform": r[1], "kind": r[2], "url_key": r[3], "caption": r[4], "status": r[5],
         "detail": dict(r[6] or {}), "scheduled_for": r[7].isoformat() if r[7] else None,
         "created_at": r[8].isoformat() if r[8] else None, "posted_at": r[9].isoformat() if r[9] else None}
    if len(r) > 10:
        d["media"] = bytes(r[10]) if r[10] is not None else None
    return d


def list_posts(limit: int = 12) -> list:
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, platform, kind, url_key, caption, status, detail, scheduled_for, created_at, posted_at, "
                        "(media IS NOT NULL) FROM marketing_posts ORDER BY id DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = _row(r[:10]); d["has_media"] = bool(r[10]); out.append(d)
        return out
    except Exception:
        return []


def used_keys(days: int = 60) -> set:
    conn = _ensure()
    if conn is None:
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url_key FROM marketing_posts WHERE url_key IS NOT NULL "
                        "AND created_at > now() - make_interval(days => %s)", (days,))
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


# ================================================================== picker
def eligible(result: dict) -> bool:
    """Pure: may this stored check be shown in public marketing?"""
    if not isinstance(result, dict):
        return False
    rep = result.get("report") or {}
    if (result.get("content_rating") or "general") != "general":
        return False
    if rep.get("safety_notice") or rep.get("nothing_to_check"):
        return False
    if rep.get("headline_score") is None or not str(rep.get("one_line") or "").strip():
        return False
    if ((result.get("scam") or {}).get("risk") or "") in ("medium", "high"):
        return False
    if result.get("media_only") or result.get("private"):
        return False
    if not str(result.get("title") or "").strip():
        return False
    return True


def candidates(days: int, limit: int = 40) -> list:
    """[(url_key, hits, result)] most-viewed first, from the last N days."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url_key, hits, result FROM checks "
                        "WHERE created_at > now() - make_interval(days => %s) "
                        "AND coalesce(result->>'content_rating','general') = 'general' "
                        "AND result->'report'->>'headline_score' IS NOT NULL "
                        "ORDER BY hits DESC, created_at DESC LIMIT %s", (days, limit))
            return [(r[0], int(r[1] or 0), r[2]) for r in cur.fetchall()]
    except Exception:
        return []


# Which story to tell. Most-viewed is not the same as most-watchable: a
# niche product review with a Mixed score is a hard 25 seconds for a
# stranger; a clear verdict on something people argue about at dinner is
# an easy one. Pure, explainable (admin shows the reasons).
_BUCKET_PTS = {"health": 2.0, "politics": 1.5, "economy": 1.5, "society_culture": 1.5, "entertainment": 1.5,
               "sports": 1.0, "science": 1.0, "conflict": 0.5, "law": 0.5, "education": 0.5, "history_geography": 0.5,
               "technology": -1.0, "business": -1.5, "other": 0.0}
_RELATABLE = re.compile(r"\b(you|your|kids?|children|baby|phone|food|water|sleep|money|tax(es)?|price|prices|gas|rent|"
                        r"vaccines?|cancer|doctors?|school|police|cops?|president|congress|election|celebrity|viral|free|"
                        r"ban(ned)?|illegal|die[ds]?|death|arrested|earthquake|storm|hurricane|flood|fire|shooting|"
                        r"recall|poison|toxic|salary|jobs?|immigrants?|border|war|troops|hospital|drug|diet|weight|"
                        r"coffee|sugar|eggs|milk|chicken|tiktok|instagram|iphone|tesla|amazon|walmart|costco)\b", re.I)


def story_score(result: dict, hits: int = 0) -> tuple:
    """(score, [reasons]) — higher is a better story for a 25-second video."""
    import math
    rep = result.get("report") or {}
    hs = rep.get("headline_score")
    title = str(result.get("title") or "").strip()
    line = str(rep.get("one_line") or "").strip()
    why, pts = [], 0.0
    if hits:
        v = min(3.0, math.log2(1 + hits)); pts += v; why.append(f"viewed {hits}× (+{v:.1f})")
    if hs is not None:
        hs = float(hs)
        if hs <= 3.0: pts += 2.5; why.append("clear verdict: inaccurate (+2.5)")
        elif hs >= 8.0: pts += 2.0; why.append("clear verdict: accurate (+2)")
        elif hs < 5.0: pts += 1.0; why.append("leans inaccurate (+1)")
        else: pts -= 1.0; why.append("mixed — nuanced in 25s (−1)")
    buckets = [str(c.get("bucket") or "other") for c in (result.get("claims") or []) if isinstance(c, dict)]
    if buckets:
        top = max(set(buckets), key=buckets.count); v = _BUCKET_PTS.get(top, 0.0); pts += v
        why.append(f"topic {top} ({'+' if v >= 0 else ''}{v:g})")
    rel = len(set(m.group(0).lower() for m in _RELATABLE.finditer(title + " " + line)))
    if rel:
        v = min(2.0, 0.5 * rel); pts += v; why.append(f"everyday words ×{rel} (+{v:.1f})")
    words = re.findall(r"[A-Za-z']+", title)
    if len(words) > 16: pts -= 1.0; why.append("long title (−1)")
    if words and sum(len(w) for w in words) / len(words) > 6.2: pts -= 1.0; why.append("heavy words (−1)")
    if any(len(w) >= 13 for w in words): pts -= 0.5; why.append("jargon-length word (−0.5)")
    if len(re.findall(r"\d[\d,.]*", title)) > 1 or re.search(r"\$\d{2,3},\d{3}", title): pts -= 0.5; why.append("number-heavy (−0.5)")
    if len(line) > 150: pts -= 1.0; why.append("long answer (−1)")
    return round(pts, 2), why


def pick(exclude: set | None = None, fetch=candidates) -> tuple | None:
    """The best STORY among recent eligible checks — yesterday's first,
    widening to a week, a month, then all time; skips anything used."""
    exclude = set(exclude or ())
    for days in (1, 7, 30, 3650):
        best = None
        for key, hits, result in fetch(days):
            if key in exclude or not eligible(result):
                continue
            sc, why = story_score(result, hits)
            if best is None or sc > best[0]:
                best = (sc, key, result, why)
        if best:
            return best[1], best[2], {"story_score": best[0], "why": best[3]}
    return None


def top_week(n: int = 3) -> list:
    """The week's n most-viewed eligible checks (for the LinkedIn post)."""
    out, seen = [], set()
    for key, hits, result in candidates(7, 60):
        if key in seen or not eligible(result):
            continue
        seen.add(key); out.append((key, hits, result))
        if len(out) >= n:
            break
    return out


# ================================================================= caption
def lead_word(result: dict) -> str:
    from app.agents.summary import canonical_lead
    return canonical_lead(result) or "Checked"


def one_line(result: dict) -> str:
    rep = (result or {}).get("report") or {}
    line = str(rep.get("one_line") or "").strip()
    return line if not FORBIDDEN.search(line) else ""


def tiktok_caption(result: dict) -> str:
    """Pure. Never names a creator, never says fake, no percentages."""
    word = lead_word(result)
    line = one_line(result) or f"{word} — the sources are in the app."
    line = re.sub(r"\d+(\.\d+)?\s?%\s*accura\w+", "", line, flags=re.I).strip()
    cap = f"Everyone's sharing this — is it true? {line} Sources on every claim. Made with Glowby, free · glowby.io/get {HASHTAGS}"
    return cap[:2100]


def _li_escape(s: str) -> str:
    """LinkedIn's 'little text' format needs these escaped in commentary."""
    return re.sub(r"([\\\(\)\[\]\{\}<>@|~_*#])", r"\\\1", s or "")


def linkedin_text(week: list, checks_week: int) -> str:
    """Pure: Dad's weekly post. `week` = [(key, hits, result)]."""
    lines = []
    if checks_week:
        lines.append(f"This week Glowby fact-checked {checks_week} videos people sent it. The ones that kept getting shared:")
    else:
        lines.append("What Glowby fact-checked this week — the videos people kept sharing:")
    for i, (key, hits, result) in enumerate(week, 1):
        title = str(result.get("title") or "a video").strip()[:70]
        rep = result.get("report") or {}
        hs = rep.get("headline_score")
        word = lead_word(result)
        line = one_line(result)
        score = f" ({float(hs):.1f}/10)" if hs is not None else ""
        rest = re.sub(r"^" + re.escape(word) + r"\s*[—\-–:]\s*", "", line, flags=re.I) if line else ""
        rest = (rest[:1].upper() + rest[1:]) if rest else ""
        body = f"{word}{score}." + (f" {rest}" if rest else "")
        lines.append(f"{i}. “{title}” — {body} {PUBLIC_URL}/r/{key}")
    lines.append("")
    lines.append("Glowby is a free app my daughter Diya built: share any TikTok, Reel or Short to it and a minute later you get a score, a one-line answer and the sources. It says when it isn't sure. glowby.io/get")
    return "\n".join(lines)[:2900]


# ================================================================ renderer
W, H = 540, 960
_F = {}


def _font(size: int):
    if size in _F:
        return _F[size]
    from PIL import ImageFont
    f = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        try:
            f = ImageFont.truetype(p, size); break
        except Exception:
            pass
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except Exception:
            f = ImageFont.load_default()
    _F[size] = f
    return f


def _wrap(d, text, f, maxw):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _ease(t):
    t = max(0.0, min(1.0, t)); return t * t * (3 - 2 * t)


def _text(im, text, size, y, color, maxw=470, alpha=255, align="center", x0=40, spacing=1.22):
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    f = _font(size); lines = _wrap(d, text, f, maxw); lh = int(size * spacing)
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=f)
        x = (W - tw) / 2 if align == "center" else x0
        d.text((x, y + i * lh), ln, font=f, fill=tuple(color) + (int(alpha),))
    im.alpha_composite(layer)
    return y + len(lines) * lh


def _color(word: str):
    return {"Accurate": (74, 222, 128), "Mostly accurate": (74, 222, 128), "Mixed": (250, 178, 25),
            "Inaccurate": (248, 113, 113)}.get(word, (154, 160, 176))


def _domains(result: dict, n: int = 3) -> list:
    out = []
    for c in (result.get("claims") or []):
        ev = c.get("evidence") or {}
        for s in (ev.get("fact_checks") or []) + (ev.get("web_sources") or []):
            u = s.get("url") or ""
            dom = re.sub(r"^www\.", "", urllib.parse.urlparse(u).netloc.lower())
            if dom and dom not in out:
                out.append(dom)
            if len(out) >= n:
                return out
    return out


DEFAULT_BOUNDS = {"check": 4.0, "wait": 6.5, "result": 8.5, "end": 17.0, "total": 20.0}


def frame(result: dict, t: float, total: float = 20.0, bounds: dict | None = None):
    """Pure-ish: one RGBA frame of the 'here's the check' cut at time t.
    `bounds` = when each scene starts (seconds); the narrated cut sets
    them from the voice track so the picture follows the words."""
    B = dict(DEFAULT_BOUNDS); B.update(bounds or {}); B["total"] = total
    b1, b2, b3, b4 = B["check"], B["wait"], B["result"], B["end"]
    from PIL import Image, ImageDraw, ImageFilter
    rep = result.get("report") or {}
    hs = rep.get("headline_score"); word = lead_word(result); col = _color(word)
    title = str(result.get("title") or "").strip()[:110]
    line = one_line(result) or f"{word} — the sources are in the app."
    doms = _domains(result)
    im = Image.new("RGBA", (W, H), (8, 7, 15, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
    r = 200 + 10 * (1 + __import__("math").sin(t * 1.4))
    g.ellipse((270 - r, 430 - r, 270 + r, 430 + r), fill=(124, 108, 246, 60)); glow = glow.filter(ImageFilter.GaussianBlur(70))
    im.alpha_composite(glow)
    d = ImageDraw.Draw(im)
    d.text((30, 40), "glowby.io", font=_font(20), fill=(180, 170, 230))
    if t < b1:                                    # 1. the claim everyone is sharing
        a = _ease(t / 0.6)
        _text(im, "Everyone's sharing this.", 36, 250, (232, 234, 240), alpha=255 * a)
        if t > min(1.0, b1 * 0.25):
            yb = _text(im, "“" + title + "”", 28, 372, (200, 200, 215), alpha=255 * _ease((t - 1.0) / 0.6))
        if t > min(2.4, b1 * 0.6):
            _text(im, "Is it true?", 44, 600, (255, 255, 255), alpha=255 * _ease((t - 2.4) / 0.5))
    elif t < b2:                                  # 2. Share → Glowby
        a = _ease((t - b1) / 0.5)
        _text(im, "Let’s check.", 40, 230, (232, 234, 240), alpha=255 * a)
        # a share-sheet row with Glowby highlighted
        y0 = 400
        d.rounded_rectangle((40, y0, 500, y0 + 150), radius=24, fill=(28, 27, 40))
        for i, (lab, c) in enumerate((("Messages", (74, 222, 128)), ("Copy link", (120, 120, 140)), ("Glowby", (124, 108, 246)), ("More", (90, 90, 110)))):
            x = 70 + i * 110
            on = lab == "Glowby" and t > b1 + 1.0
            d.rounded_rectangle((x, y0 + 22, x + 76, y0 + 98), radius=20, fill=c if (lab != "Glowby" or on) else (60, 55, 110))
            if lab == "Glowby":
                d.ellipse((x + 20, y0 + 42, x + 56, y0 + 78), outline=(255, 255, 255), width=5)
            tw = d.textlength(lab, font=_font(16)); d.text((x + 38 - tw / 2, y0 + 108), lab, font=_font(16), fill=(200, 200, 215))
        if t > b1 + 1.3:
            d.ellipse((275 + 40, y0 + 60, 275 + 70, y0 + 90), fill=(255, 255, 255, 200))  # the tap
        _text(im, "Share → Glowby", 24, 600, (160, 150, 230), alpha=255 * _ease((t - b1 - 1.0) / 0.5))
    elif t < b3:                                  # 3. the wait, sped up
        ang = (t * 300) % 360
        d.arc((190, 350, 350, 510), start=ang, end=ang + 80, fill=(124, 108, 246), width=12)
        _text(im, "checking the claims…", 26, 560, (200, 200, 215))
        d.rounded_rectangle((165, 640, 375, 686), radius=23, fill=(28, 27, 40))
        tw = d.textlength("~1 minute later", font=_font(20)); d.text((270 - tw / 2, 651), "~1 minute later", font=_font(20), fill=(232, 234, 240))
    elif t < b4:                                  # 4. the result
        u = t - b3
        d.ellipse((160, 200, 380, 420), outline=(34, 33, 48), width=18)
        if hs is not None:
            frac = max(0.03, float(hs) / 9.9) * _ease(u / 1.4)
            d.arc((160, 200, 380, 420), start=-90, end=-90 + 360 * frac, fill=col, width=18)
            num = f"{float(hs) * _ease(u / 1.4):.1f}"
        else:
            num = "—"
        f1 = _font(72); tw = d.textlength(num, font=f1); d.text((270 - tw / 2, 268), num, font=f1, fill=col)
        if u > 1.2:
            _text(im, word.upper(), 30, 445, col, alpha=255 * _ease((u - 1.2) / 0.4))
        if u > 1.8:
            # the one-line answer types itself in
            n = int(len(line) * min(1.0, (u - 1.8) / 2.2))
            yb = _text(im, line[:n], 27, 510, (240, 240, 248), maxw=460)
        else:
            yb = 510
        if u > 4.3 and doms:
            for i, dom in enumerate(doms):
                if u > 4.3 + i * 0.35:
                    a = _ease((u - 4.3 - i * 0.35) / 0.3)
                    y = max(yb + 30, 700) + i * 46
                    d.rounded_rectangle((60, y, 480, y + 36), radius=18, fill=(28, 27, 40, int(255 * a)))
                    d.text((82, y + 8), "source · " + dom, font=_font(18), fill=(200, 200, 215, int(255 * a)))
    else:                                         # 5. end card
        u = t - b4
        yb = _text(im, "Share a video to Glowby.", 36, 300, (232, 234, 240), alpha=255 * _ease(u / 0.5))
        _text(im, "A minute later: score, answer, sources.", 24, yb + 24, (200, 200, 215), alpha=255 * _ease((u - 0.3) / 0.5))
        if u > 0.9:
            txt = "Free · glowby.io/get"; f = _font(34); tw = d.textlength(txt, font=f)
            d.rounded_rectangle((270 - tw / 2 - 22, 520, 270 + tw / 2 + 22, 584), radius=16, fill=(124, 108, 246))
            d.text((270 - tw / 2, 532), txt, font=f, fill=(255, 255, 255))
        _text(im, "link in bio", 20, 610, (160, 150, 230), alpha=255 * _ease((u - 1.4) / 0.5))
    # caption bar for muted viewers
    sub = {0: "everyone's sharing this", 1: "is it true?", 2: "share it to Glowby", 3: "a minute later", 4: "the answer, with sources", 5: "free · glowby.io/get"}
    k = 0 if t < min(2.4, b1 * 0.6) else 1 if t < b1 else 2 if t < b2 else 3 if t < b3 else 4 if t < b4 else 5
    d.rounded_rectangle((120, 880, 420, 918), radius=19, fill=(0, 0, 0, 160))
    tw = d.textlength(sub[k], font=_font(18)); d.text((270 - tw / 2, 889), sub[k], font=_font(18), fill=(255, 255, 255))
    return im


def render_clip(result: dict, out_path: str, fps: int = 30, total: float = 20.0, scale: int = 2, ffmpeg: str = "ffmpeg",
                bounds: dict | None = None, audio_path: str | None = None) -> str:
    """Render the clip to an MP4 (H.264, yuv420p). With `audio_path` the
    voice track is muxed in; without it a silent AAC track is added so
    every platform accepts the file. Frames are piped — nothing hits the
    disk but the result. Returns out_path."""
    n = int(total * fps)
    audio_in = ["-i", audio_path] if audio_path else ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", *audio_in,
           "-vf", f"scale={W * scale}:{H * scale}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", out_path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            p.stdin.write(frame(result, i / fps, total, bounds).convert("RGB").tobytes())
    finally:
        p.stdin.close()
    err = p.stderr.read().decode("utf-8", "ignore")
    if p.wait() != 0:
        raise RuntimeError("ffmpeg failed: " + err[-400:])
    return out_path


# =============================================================== narration
# A finished video, voice included, so there is always something to post.
# The voice is a plain synthetic narrator (OpenAI TTS, same key Whisper
# uses) — it is NEVER presented as Diya, and the caption + TikTok's AI
# label say the video is machine-made. Diya can post it as is on a busy
# day or talk over it when she has a minute. Cost: well under a cent.
TTS_MODEL = os.environ.get("GLOWBY_TTS_MODEL", "gpt-4o-mini-tts")   # takes style instructions; tts-1 is the plain fallback
TTS_VOICE = os.environ.get("GLOWBY_TTS_VOICE", "coral")


def narration_script(result: dict) -> list:
    """Pure: [(scene, sentence)] in the order the scenes play. Plain
    template, no model — so it can never invent a fact or a verdict."""
    rep = result.get("report") or {}
    hs = rep.get("headline_score"); word = lead_word(result)
    title = str(result.get("title") or "this video").strip()[:90].rstrip(".")
    line = one_line(result) or f"{word}. The sources are in the app."
    line = re.sub(r"^" + re.escape(word) + r"\s*[—\-–:]\s*", "", line, flags=re.I).strip() or line
    line = spoken_line(line)
    doms = _domains(result, 2)
    src = (" The sources are right there: " + " and ".join(d.replace(".com", " dot com").replace(".org", " dot org") for d in doms) + ".") if doms else " The sources are right there on every claim."
    score = f"{float(hs):.1f} out of ten." if hs is not None else "no score, it says why."
    return [
        ("intro", f"Everyone's sharing this one: {title}. Is it true?"),
        ("check", "I shared it to Glowby."),
        ("wait", "About a minute later…"),
        ("result", f"{word}. {score} {line}{src}"),
        ("end", "Glowby is free. Link in bio."),
    ]


def spoken_line(line: str, limit: int = 140) -> str:
    """Pure: the one-line answer as something a person would SAY — one
    breath, no dashes, cut at a natural joint when it runs long."""
    t = re.sub(r"\s*[—–]\s*", ", ", (line or "").strip())
    t = re.sub(r"\s+", " ", t)
    if len(t) > limit:
        cut = max(t.rfind(". ", 0, limit), t.rfind("; ", 0, limit), t.rfind(", but ", 0, limit), t.rfind(", ", 0, limit))
        if cut > 40:
            t = t[:cut]
        else:
            t = t[:limit].rsplit(" ", 1)[0]
    t = t.rstrip(" ,;:")
    if t and t[-1] not in ".!?":
        t += "."
    return t[:1].upper() + t[1:]


VOICE_STYLE = ("You are a warm, curious 17-year-old explaining something to a friend on TikTok. Natural and conversational, "
               "a little playful, never like a news anchor or an ad. Real pauses, real emphasis, a smile in the voice.")
SCENE_STYLE = {
    "intro": "Hook them: curious and slightly incredulous, rising on 'Is it true?'.",
    "check": "Casual and quick, like an aside.",
    "wait": "Light and amused, trailing off.",
    "result": "Slow down. Take a small breath before the verdict word and say it with quiet confidence; then explain plainly and kindly, like you're the friend who checked.",
    "end": "Warm and friendly, a genuine smile; unhurried.",
}


def tts_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and setting("narration", "1") == "1"


def tts_request(text: str, scene: str = "", model: str | None = None) -> dict:
    """Pure: the speech request. The instructable model gets the voice
    direction (who is talking, how this scene feels); plain tts-1 cannot
    take instructions and gets a slight speed-up instead."""
    model = model or TTS_MODEL
    req = {"model": model, "voice": TTS_VOICE, "input": text, "response_format": "mp3"}
    if model.startswith("tts-1"):
        req["speed"] = 1.05
    else:
        req["instructions"] = VOICE_STYLE + (" " + SCENE_STYLE[scene] if scene in SCENE_STYLE else "")
    return req


def tts(text: str, http=None, scene: str = "") -> bytes:
    """One sentence -> MP3 bytes via OpenAI's speech endpoint; falls back
    to tts-1 if the expressive model is refused. Raises on failure."""
    key = os.environ.get("OPENAI_API_KEY", "")
    hdr = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    st, body, _ = (http or _http)("POST", "https://api.openai.com/v1/audio/speech", hdr,
                                  json.dumps(tts_request(text, scene)).encode(), timeout=60)
    if st != 200 and not TTS_MODEL.startswith("tts-1"):
        st, body, _ = (http or _http)("POST", "https://api.openai.com/v1/audio/speech", hdr,
                                      json.dumps(tts_request(text, scene, model="tts-1")).encode(), timeout=60)
    if st != 200 or len(body) < 1000:
        raise RuntimeError(f"tts {st}: {body[:120]!r}")
    return body


def _duration(path: str, ffprobe: str = "ffprobe") -> float:
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    return float(out or 0)


def build_voice_track(result: dict, workdir: str, speak=tts, ffmpeg: str = "ffmpeg") -> tuple:
    """Synthesize each sentence, lay them on a timeline with short gaps,
    and return (audio_path, bounds, total). The picture's scene changes
    are set from where each sentence starts."""
    segs = []
    import inspect
    takes_scene = "scene" in inspect.signature(speak).parameters
    for scene, text in narration_script(result):
        fp = os.path.join(workdir, f"{scene}.mp3")
        with open(fp, "wb") as f:
            f.write(speak(text, scene=scene) if takes_scene else speak(text))
        segs.append((scene, fp, _duration(fp)))
    gap = {"intro": 0.6, "check": 0.5, "wait": 1.6, "result": 1.0, "end": 0.0}
    t, bounds, delays = 0.8, {}, []
    for scene, fp, dur in segs:
        bounds[scene] = round(t, 2)
        delays.append((fp, t))
        t += dur + gap[scene]
    total = round(t + 1.2, 2)
    # the first scene starts at 0; the narration of it starts at 0.8
    bounds = {"check": bounds["check"], "wait": bounds["wait"], "result": bounds["result"], "end": bounds["end"]}
    out = os.path.join(workdir, "voice.m4a")
    inputs, filt = [], []
    for i, (fp, at) in enumerate(delays):
        inputs += ["-i", fp]
        filt.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(at * 1000)}|{int(at * 1000)}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(delays))) + f"amix=inputs={len(delays)}:normalize=0,apad=whole_dur={total}[out]")
    cmd = [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filt), "-map", "[out]", "-c:a", "aac", "-b:a", "96k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("voice mix failed: " + r.stderr[-300:])
    return out, bounds, total


def render_narrated(result: dict, out_path: str, speak=tts, **kw) -> dict:
    """The whole video, voice included. Returns {narrated, total}; falls
    back to the silent cut if the voice step fails for any reason."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:
            audio, bounds, total = build_voice_track(result, td, speak=speak)
            render_clip(result, out_path, total=total, bounds=bounds, audio_path=audio, **kw)
        try:
            from app.storage import add_usage
            add_usage(0.01, count=False)
        except Exception:
            pass
        return {"narrated": True, "total": total}
    except Exception as e:
        render_clip(result, out_path, **kw)
        return {"narrated": False, "total": DEFAULT_BOUNDS["total"], "voice_error": str(e)[:160]}


def share_card(result: dict, key: str) -> bytes:
    """The 1200×1200 LinkedIn image for one check: ring, score, word, the
    one-line answer, the title. Pillow only."""
    from PIL import Image, ImageDraw
    rep = result.get("report") or {}
    hs = rep.get("headline_score"); word = lead_word(result); col = _color(word)
    S = 1200
    im = Image.new("RGBA", (S, S), (8, 7, 15, 255)); d = ImageDraw.Draw(im)
    d.ellipse((400, 90, 800, 490), outline=(34, 33, 48), width=30)
    if hs is not None:
        d.arc((400, 90, 800, 490), start=-90, end=-90 + 360 * max(0.03, float(hs) / 9.9), fill=col, width=30)
    num = "—" if hs is None else f"{float(hs):.1f}"
    f1 = _font(130); tw = d.textlength(num, font=f1); d.text((600 - tw / 2, 215), num, font=f1, fill=col)
    f2 = _font(54); tw = d.textlength(word.upper(), font=f2); d.text((600 - tw / 2, 530), word.upper(), font=f2, fill=col)
    line = one_line(result) or ""
    y = 640
    for ln in _wrap(d, line, _font(44), 1040)[:4]:
        tw = d.textlength(ln, font=_font(44)); d.text((600 - tw / 2, y), ln, font=_font(44), fill=(240, 240, 248)); y += 58
    title = str(result.get("title") or "").strip()[:90]
    y = max(y + 30, 930)
    for ln in _wrap(d, "“" + title + "”", _font(32), 1040)[:2]:
        tw = d.textlength(ln, font=_font(32)); d.text((600 - tw / 2, y), ln, font=_font(32), fill=(170, 170, 190)); y += 42
    f3 = _font(30); tw = d.textlength("glowby.io", font=f3); d.text((600 - tw / 2, 1130), "glowby.io", font=f3, fill=(154, 160, 176))
    out = BytesIO(); im.convert("RGB").save(out, "PNG", optimize=True); return out.getvalue()


# ==================================================================== http
def _http(method: str, url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = 60) -> tuple:
    """(status, body_bytes, headers). Never raises on HTTP errors."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


def _json(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8", "ignore") or "{}")
    except Exception:
        return {}


# =================================================================== oauth
def _state_secret() -> bytes:
    return (os.environ.get("GLOWBY_ADMIN_KEY", "") + "|" + TIKTOK_SECRET + "|" + LINKEDIN_SECRET + "|glowby-mkt").encode()


def make_state(platform: str, now: float | None = None) -> str:
    ts = str(int(now or time.time()))
    sig = hmac.new(_state_secret(), f"{platform}:{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{platform}.{ts}.{sig}"


def check_state(state: str, platform: str, now: float | None = None, max_age: int = 900) -> bool:
    try:
        plat, ts, sig = (state or "").split(".")
    except ValueError:
        return False
    if plat != platform or abs((now or time.time()) - int(ts)) > max_age:
        return False
    want = hmac.new(_state_secret(), f"{platform}:{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, want)


def redirect_uri(platform: str) -> str:
    return f"{PUBLIC_URL}/api/marketing/{platform}/callback"


def connect_url(platform: str) -> str | None:
    if platform == "tiktok":
        if not TIKTOK_KEY:
            return None
        q = {"client_key": TIKTOK_KEY, "scope": "user.info.basic,video.upload", "response_type": "code",
             "redirect_uri": redirect_uri("tiktok"), "state": make_state("tiktok")}
        return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)
    if platform == "linkedin":
        if not LINKEDIN_ID:
            return None
        q = {"response_type": "code", "client_id": LINKEDIN_ID, "redirect_uri": redirect_uri("linkedin"),
             "state": make_state("linkedin"), "scope": "openid profile w_member_social"}
        return "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(q)
    return None


def finish_connect(platform: str, code: str, state: str, http=_http) -> dict:
    """Exchange the OAuth code; store the token. Returns {ok, detail}."""
    if not check_state(state, platform):
        return {"ok": False, "detail": "state check failed — start again from admin"}
    form = {"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri(platform)}
    if platform == "tiktok":
        form.update(client_key=TIKTOK_KEY, client_secret=TIKTOK_SECRET)
        st, body, _ = http("POST", "https://open.tiktokapis.com/v2/oauth/token/",
                           {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
        j = _json(body)
        if st != 200 or not j.get("access_token"):
            return {"ok": False, "detail": f"TikTok refused the code ({st}): {j.get('error_description') or j.get('error') or '?'}"}
        tok = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""), "open_id": j.get("open_id", ""),
               "expires_at": time.time() + int(j.get("expires_in", 86400)) - 60,
               "refresh_expires_at": time.time() + int(j.get("refresh_expires_in", 365 * 86400)), "scope": j.get("scope", "")}
        save_token("tiktok", tok)
        return {"ok": True, "detail": "TikTok connected"}
    if platform == "linkedin":
        form.update(client_id=LINKEDIN_ID, client_secret=LINKEDIN_SECRET)
        st, body, _ = http("POST", "https://www.linkedin.com/oauth/v2/accessToken",
                           {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
        j = _json(body)
        if st != 200 or not j.get("access_token"):
            return {"ok": False, "detail": f"LinkedIn refused the code ({st}): {j.get('error_description') or j.get('error') or '?'}"}
        tok = {"access_token": j["access_token"], "expires_at": time.time() + int(j.get("expires_in", 60 * 86400)) - 60}
        st2, body2, _ = http("GET", "https://api.linkedin.com/v2/userinfo", {"Authorization": "Bearer " + tok["access_token"]})
        me = _json(body2)
        if st2 != 200 or not me.get("sub"):
            return {"ok": False, "detail": f"LinkedIn did not return the member id ({st2})"}
        tok["person_urn"] = "urn:li:person:" + me["sub"]; tok["name"] = me.get("name", "")
        save_token("linkedin", tok)
        return {"ok": True, "detail": f"LinkedIn connected as {tok['name'] or 'member'}"}
    return {"ok": False, "detail": "unknown platform"}


def tiktok_access_token(http=_http) -> str | None:
    tok = get_token("tiktok")
    if not tok:
        return None
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        return None
    form = {"client_key": TIKTOK_KEY, "client_secret": TIKTOK_SECRET, "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}
    st, body, _ = http("POST", "https://open.tiktokapis.com/v2/oauth/token/",
                       {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
    j = _json(body)
    if st != 200 or not j.get("access_token"):
        return None
    tok.update(access_token=j["access_token"], refresh_token=j.get("refresh_token", tok["refresh_token"]),
               expires_at=time.time() + int(j.get("expires_in", 86400)) - 60)
    save_token("tiktok", tok)
    return tok["access_token"]


# ================================================================== tiktok
def chunk_plan(size: int) -> dict:
    """Pure: TikTok's chunk rules — under 5 MB the whole file is one chunk
    of exactly its size; under 64 MB still one chunk; larger files split."""
    mb = 1024 * 1024
    if size <= 64 * mb:
        return {"video_size": size, "chunk_size": size, "total_chunk_count": 1}
    chunk = 32 * mb
    return {"video_size": size, "chunk_size": chunk, "total_chunk_count": size // chunk}


def tiktok_upload_to_drafts(mp4: bytes, http=_http) -> dict:
    """Put a finished video into the connected creator's TikTok DRAFTS
    ('inbox'). Returns {ok, publish_id|detail}. Never publishes."""
    token = tiktok_access_token(http)
    if not token:
        return {"ok": False, "detail": "TikTok not connected (or token expired) — connect in admin"}
    plan = chunk_plan(len(mp4))
    st, body, _ = http("POST", "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                       {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8"},
                       json.dumps({"source_info": dict(source="FILE_UPLOAD", **plan)}).encode())
    j = _json(body)
    data = j.get("data") or {}
    if st != 200 or not data.get("upload_url"):
        err = (j.get("error") or {})
        return {"ok": False, "detail": f"TikTok init failed ({st}): {err.get('code') or '?'} {err.get('message') or ''}".strip()}
    size = len(mp4); chunk = plan["chunk_size"]; pos = 0; idx = 0
    while pos < size:
        last = idx == plan["total_chunk_count"] - 1
        end = size if last else min(size, pos + chunk)
        part = mp4[pos:end]
        st2, body2, _ = http("PUT", data["upload_url"],
                             {"Content-Type": "video/mp4", "Content-Length": str(len(part)),
                              "Content-Range": f"bytes {pos}-{end - 1}/{size}"}, part, timeout=300)
        if st2 not in (200, 201, 206):
            return {"ok": False, "detail": f"TikTok upload failed ({st2}) at byte {pos}", "publish_id": data.get("publish_id")}
        pos = end; idx += 1
    return {"ok": True, "publish_id": data.get("publish_id"), "detail": "in TikTok drafts"}


def tiktok_status(publish_id: str, http=_http) -> dict:
    token = tiktok_access_token(http)
    if not token or not publish_id:
        return {}
    st, body, _ = http("POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                       {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8"},
                       json.dumps({"publish_id": publish_id}).encode())
    return (_json(body).get("data") or {}) if st == 200 else {"status": f"http {st}"}


# ================================================================ linkedin
def linkedin_token() -> dict | None:
    tok = get_token("linkedin")
    if not tok or time.time() > tok.get("expires_at", 0):
        return None
    return tok


def linkedin_post(text: str, images: list, http=_http) -> dict:
    """Publish a post (text + 0–9 PNG images) as the connected member."""
    tok = linkedin_token()
    if not tok:
        return {"ok": False, "detail": "LinkedIn not connected or its 60-day token expired — reconnect in admin"}
    hdr = {"Authorization": "Bearer " + tok["access_token"], "LinkedIn-Version": LINKEDIN_VERSION,
           "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"}
    urns = []
    for png in images[:9]:
        st, body, _ = http("POST", "https://api.linkedin.com/rest/images?action=initializeUpload", hdr,
                           json.dumps({"initializeUploadRequest": {"owner": tok["person_urn"]}}).encode())
        v = (_json(body).get("value") or {})
        if st not in (200, 201) or not v.get("uploadUrl"):
            return {"ok": False, "detail": f"LinkedIn image init failed ({st})"}
        st2, _, _ = http("PUT", v["uploadUrl"], {"Authorization": "Bearer " + tok["access_token"], "Content-Type": "image/png"}, png, timeout=120)
        if st2 not in (200, 201):
            return {"ok": False, "detail": f"LinkedIn image upload failed ({st2})"}
        urns.append(v["image"])
    post = {"author": tok["person_urn"], "commentary": _li_escape(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    if len(urns) == 1:
        post["content"] = {"media": {"id": urns[0], "altText": "Glowby fact-check score card"}}
    elif urns:
        post["content"] = {"multiImage": {"images": [{"id": u, "altText": "Glowby fact-check score card"} for u in urns]}}
    st, body, hdrs = http("POST", "https://api.linkedin.com/rest/posts", hdr, json.dumps(post).encode())
    if st not in (200, 201):
        return {"ok": False, "detail": f"LinkedIn post failed ({st}): {_json(body).get('message') or ''}".strip()}
    pid = hdrs.get("x-restli-id") or hdrs.get("X-RestLi-Id") or ""
    return {"ok": True, "post_urn": pid, "detail": "posted to LinkedIn"}


# ==================================================================== jobs
def run_daily(force: bool = False, render=render_clip, upload=tiktok_upload_to_drafts, url_key: str = "") -> dict:
    """Pick → render → caption → TikTok drafts. Always stores the clip so
    admin can download it even when TikTok isn't connected."""
    if paused("tiktok") and not force:
        return {"ok": False, "detail": "TikTok lane paused"}
    if url_key:  # a person chose the story in admin
        r = _peek(url_key)
        if not r or not eligible(r):
            return {"ok": False, "detail": "that check can't be used (not stored, not scoreable, or not general-rated)"}
        chosen = (url_key, r, {"story_score": story_score(r)[0], "why": ["chosen by hand"] + story_score(r)[1]})
    else:
        chosen = pick(used_keys())
    if not chosen:
        pid = _insert_post("tiktok", "daily", None, "", "skipped", {"reason": "no eligible check to show"})
        return {"ok": False, "detail": "no eligible check", "id": pid}
    key, result, meta = chosen
    cap = tiktok_caption(result)
    path = os.path.join(_tmpdir(), f"glowby-{int(time.time())}.mp4")
    voice = {"narrated": False}
    try:
        if render is render_clip and tts_available():
            voice = render_narrated(result, path)
        else:
            render(result, path)
        mp4 = open(path, "rb").read()
    except Exception as e:
        pid = _insert_post("tiktok", "daily", key, cap, "failed", {"error": f"render: {str(e)[:200]}", "title": result.get("title", "")})
        return {"ok": False, "detail": f"render failed: {str(e)[:120]}", "id": pid}
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    detail = {"title": str(result.get("title") or "")[:120], "score": (result.get("report") or {}).get("headline_score"),
              "word": lead_word(result), "bytes": len(mp4), "narrated": bool(voice.get("narrated")),
              "story_score": meta.get("story_score"), "why": meta.get("why"),
              "voice_error": voice.get("voice_error"), "seconds": voice.get("total"),
              "note": (AI_LABEL_NOTE_VOICED if voice.get("narrated") else AI_LABEL_NOTE)}
    pid = _insert_post("tiktok", "daily", key, cap, "rendered", detail, media=mp4)
    if get_token("tiktok"):
        up = upload(mp4)
        _update_post(pid, status="in_tiktok_drafts" if up.get("ok") else "upload_failed",
                     detail={"tiktok": up.get("detail"), "publish_id": up.get("publish_id")}, posted=bool(up.get("ok")))
        return {"ok": bool(up.get("ok")), "detail": up.get("detail"), "id": pid}
    return {"ok": True, "detail": "rendered — TikTok not connected, download it from admin", "id": pid}


def _checks_last_week() -> int:
    conn = _ensure()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(sum(checks),0) FROM daily_usage WHERE day > CURRENT_DATE - 7")
            return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def run_weekly(force: bool = False) -> dict:
    """Compose the LinkedIn post and queue it for approval."""
    if paused("linkedin") and not force:
        return {"ok": False, "detail": "LinkedIn lane paused"}
    week = top_week(3)
    if not week:
        pid = _insert_post("linkedin", "weekly", None, "", "skipped", {"reason": "nothing eligible this week"})
        return {"ok": False, "detail": "nothing eligible this week", "id": pid}
    text = linkedin_text(week, _checks_last_week())
    if FORBIDDEN.search(text):
        text = FORBIDDEN.sub("—", text)
    keys = [k for k, _, _ in week]
    when = datetime.now(timezone.utc) + timedelta(hours=AUTOPOST_DELAY_H)
    pid = _insert_post("linkedin", "weekly", keys[0], text,
                       "queued", {"keys": keys, "autopost": AUTOPOST_LINKEDIN,
                                  "titles": [str(r.get("title") or "")[:80] for _, _, r in week]},
                       scheduled_for=when if AUTOPOST_LINKEDIN else None)
    return {"ok": True, "detail": "queued — approve in admin" + (" or it posts in 24h" if AUTOPOST_LINKEDIN else ""), "id": pid}


def publish_linkedin(pid: int, post=linkedin_post) -> dict:
    p = get_post(pid)
    if not p or p["platform"] != "linkedin":
        return {"ok": False, "detail": "no such post"}
    if p["status"] == "posted":
        return {"ok": True, "detail": "already posted"}
    images = []
    for k in (p["detail"].get("keys") or [])[:3]:
        try:
            r = _peek(k)
            if r:
                images.append(share_card(r, k))
        except Exception:
            pass
    res = post(p["caption"], images)
    _update_post(pid, status="posted" if res.get("ok") else "post_failed", detail={"linkedin": res.get("detail"), "post_urn": res.get("post_urn")}, posted=bool(res.get("ok")))
    return res


def post_action(pid: int, action: str) -> dict:
    p = get_post(pid)
    if not p:
        return {"ok": False, "detail": "no such post"}
    if action == "skip":
        _update_post(pid, status="skipped"); return {"ok": True}
    if action == "approve" and p["platform"] == "linkedin":
        return publish_linkedin(pid)
    if action == "retry" and p["platform"] == "tiktok":
        full = get_post(pid, with_media=True)
        if not full or not full.get("media"):
            return {"ok": False, "detail": "clip no longer stored — run today's clip again"}
        up = tiktok_upload_to_drafts(full["media"])
        _update_post(pid, status="in_tiktok_drafts" if up.get("ok") else "upload_failed",
                     detail={"tiktok": up.get("detail"), "publish_id": up.get("publish_id")}, posted=bool(up.get("ok")))
        return up
    return {"ok": False, "detail": "unknown action"}


def _tmpdir() -> str:
    d = os.path.join(os.environ.get("TMPDIR", "/tmp"), "glowby-marketing")
    os.makedirs(d, exist_ok=True)
    return d


# =============================================================== scheduler
def due(now_pt: datetime, last: dict) -> list:
    """Pure: which jobs fire at this Pacific minute. `last` holds the
    date-strings of the last daily / weekly runs."""
    jobs = []
    today = now_pt.strftime("%Y-%m-%d")
    if now_pt.hour >= DAILY_HOUR and last.get("daily") != today:
        jobs.append("daily")
    if now_pt.weekday() == 0 and now_pt.hour >= WEEKLY_HOUR and last.get("weekly") != today:
        jobs.append("weekly")
    return jobs


def _tick():
    now = datetime.now(_PT)
    last = {"daily": setting("last:daily"), "weekly": setting("last:weekly")}
    for job in due(now, last):
        set_setting(f"last:{job}", now.strftime("%Y-%m-%d"))
        try:
            run_daily() if job == "daily" else run_weekly()
        except Exception as e:
            _insert_post("tiktok" if job == "daily" else "linkedin", job, None, "", "failed", {"error": str(e)[:200]})
    # LinkedIn autopost: queued + scheduled_for passed
    conn = _ensure()
    if conn is not None and AUTOPOST_LINKEDIN:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM marketing_posts WHERE platform='linkedin' AND status='queued' "
                            "AND scheduled_for IS NOT NULL AND scheduled_for <= now()")
                ids = [r[0] for r in cur.fetchall()]
            for pid in ids:
                publish_linkedin(pid)
        except Exception:
            pass


_THREAD = {"t": None}


def start():
    if not ENABLED or _THREAD["t"] is not None:
        return
    def _loop():
        time.sleep(40)
        while True:
            try:
                _tick()
            except Exception:
                pass
            time.sleep(60)
    t = threading.Thread(target=_loop, daemon=True); t.start(); _THREAD["t"] = t


def status() -> dict:
    tt, lt = get_token("tiktok"), get_token("linkedin")
    return {
        "enabled": ENABLED,
        "daily_hour_pt": DAILY_HOUR, "weekly_hour_pt": WEEKLY_HOUR, "autopost_linkedin": AUTOPOST_LINKEDIN,
        "narration": {"available": bool(os.environ.get("OPENAI_API_KEY")), "on": setting("narration", "1") == "1", "voice": TTS_VOICE, "model": TTS_MODEL},
        "tiktok": {"configured": bool(TIKTOK_KEY and TIKTOK_SECRET), "connected": bool(tt), "paused": paused("tiktok"),
                   "refresh_until": datetime.fromtimestamp(tt["refresh_expires_at"], tz=timezone.utc).date().isoformat() if tt and tt.get("refresh_expires_at") else None},
        "linkedin": {"configured": bool(LINKEDIN_ID and LINKEDIN_SECRET), "connected": bool(lt), "paused": paused("linkedin"),
                     "name": (lt or {}).get("name"),
                     "expires": datetime.fromtimestamp(lt["expires_at"], tz=timezone.utc).date().isoformat() if lt and lt.get("expires_at") else None},
        "last": {"daily": setting("last:daily"), "weekly": setting("last:weekly")},
        "redirect_uris": {"tiktok": redirect_uri("tiktok"), "linkedin": redirect_uri("linkedin")},
        "posts": list_posts(12),
        "note": AI_LABEL_NOTE,
    }
