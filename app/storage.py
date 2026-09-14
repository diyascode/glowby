"""
Result cache — "check once, serve forever."

Why: if 10,000 people paste the same viral reel, the agent pipeline
(transcription + Claude + web search) should run ONCE. Every later
request is served from Postgres in milliseconds, for free.

Two pieces:
1. canonical_key(url): different URL spellings of the SAME video must
   map to the same cache key. youtu.be/abc, youtube.com/watch?v=abc,
   and youtube.com/watch?v=abc&si=tracking are all "youtube:abc".
2. get_cached / save_result: a single `checks` table in Postgres
   (Railway provides DATABASE_URL). If the database is missing or
   down, Glowby still works — it just runs the pipeline every time.
"""

import json
import os
import re
import urllib.parse

# strip these query params when canonicalizing "other" URLs — pure tracking
TRACKING_PARAMS = {
    "si", "feature", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "fbclid", "gclid", "igsh", "igshid", "ref", "ref_src", "s", "t",
    # Facebook share-sheet tails (the same reel came in as
    # facebook.com/reel/ID?fs=e AND fb.watch/CODE?mibextid=… — two keys,
    # two runs, two scores; Sep 2026)
    "fs", "mibextid", "rdid", "share_url", "sfnsn", "vh", "extid", "paipv",
    "eav", "_rdr", "wtsid", "refsrc", "app", "locale", "mibextid",
}

# short links that only a redirect can name: resolved BEFORE keying
SHORT_LINK_HOSTS = ("fb.watch", "vm.tiktok.com", "vt.tiktok.com")
_SHORT_PATH_RE = re.compile(r"^/(share/[rvp]|s|l\.php|watch)/?", re.I)


# ------------------------------------------------------------ canonical keys


def canonical_key(url: str) -> str:
    """Map every spelling of the same video to one stable cache key."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path or ""
    query = urllib.parse.parse_qs(parsed.query or "")

    # --- YouTube: youtu.be/ID, /watch?v=ID, /shorts/ID, /embed/ID, /live/ID
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        if vid:
            return f"youtube:{vid}"
    if host.endswith("youtube.com"):
        if path == "/watch" and query.get("v"):
            return f"youtube:{query['v'][0]}"
        m = re.match(r"^/(shorts|embed|live)/([A-Za-z0-9_-]{5,})", path)
        if m:
            return f"youtube:{m.group(2)}"

    # --- TikTok: /@user/video/1234567890, /t/SHORTCODE, vm.tiktok.com/CODE
    if host.endswith("tiktok.com"):
        m = re.search(r"/(?:video|photo)/(\d+)", path)
        if m:
            return f"tiktok:{m.group(1)}"
        segs = [s for s in path.split("/") if s]
        # short-link prefixes: the CODE is the next segment, never the
        # prefix itself (/t/ABC and /t/XYZ are DIFFERENT videos)
        if len(segs) > 1 and segs[0].lower() in ("t", "v", "embed"):
            return f"tiktok:{segs[1]}"
        if segs:
            return f"tiktok:{segs[0]}"

    # --- Facebook: /reel/ID, /videos/ID, /USER/videos/ID, /watch?v=ID,
    # /watch/?v=ID, /video.php?v=ID, /photo?fbid=ID, fb.watch/CODE
    if host == "fb.watch":
        code = path.strip("/").split("/")[0]
        if code:
            return f"facebook:short:{code}"
    if host.endswith("facebook.com") or host.endswith("fb.com"):
        m = re.search(r"/(?:reel|reels|videos|video)/(\d{6,})", path)
        if m:
            return f"facebook:{m.group(1)}"
        if query.get("v") and re.fullmatch(r"\d{6,}", query["v"][0]):
            return f"facebook:{query['v'][0]}"
        if query.get("fbid") and re.fullmatch(r"\d{6,}", query["fbid"][0]):
            return f"facebook:{query['fbid'][0]}"
        m = re.match(r"^/share/([rvp])/([A-Za-z0-9_-]+)", path)
        if m:
            return f"facebook:short:{m.group(2)}"

    # --- Instagram: /reel/CODE, /reels/CODE, /p/CODE, /tv/CODE
    if host.endswith("instagram.com"):
        m = re.match(r"^/(?:[A-Za-z0-9_.]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,})", path)
        if m:
            return f"instagram:{m.group(1)}"

    # --- X / Twitter: /user/status/1234567890
    if host in ("x.com", "twitter.com") or host.endswith(".twitter.com"):
        m = re.search(r"/status/(\d+)", path)
        if m:
            return f"x:{m.group(1)}"

    # --- everything else: normalized URL minus tracking params
    kept = {k: v for k, v in query.items() if k.lower() not in TRACKING_PARAMS}
    clean_query = urllib.parse.urlencode(sorted(kept.items()), doseq=True)
    return f"url:{host}{path.rstrip('/')}" + (f"?{clean_query}" if clean_query else "")


def is_short_link(url: str) -> bool:
    """A link whose real video only a redirect reveals (fb.watch,
    facebook.com/share/r/…, vm.tiktok.com). These get resolved first so
    the share-sheet copy and the address-bar copy share ONE cache key."""
    parsed = urllib.parse.urlparse((url or "").strip())
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host in SHORT_LINK_HOSTS:
        return True
    if (host.endswith("facebook.com") or host.endswith("fb.com")) and \
            re.match(r"^/share/[rvp]/", parsed.path or ""):
        return True
    return False


def resolve_short_link(url: str, fetch=None, timeout: float = 6.0) -> str:
    """Follow the redirect of a share link to the address it stands for.
    Returns the original URL on any failure — a miss costs one extra
    run, never a broken check. `fetch(url) -> final_url` is injectable
    (the workspace can't reach Facebook; Railway can)."""
    url = (url or "").strip()
    if not is_short_link(url):
        return url
    hit = _RESOLVED.get(url)
    if hit:
        return hit
    try:
        if fetch is None:
            import urllib.request

            req = urllib.request.Request(
                url, method="HEAD",
                headers={"User-Agent": "Mozilla/5.0 (compatible; Glowby/1.0)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                final = r.geturl()
        else:
            final = fetch(url)
    except Exception:
        return url
    final = _unwrap_login(final or "")
    if not final or is_short_link(final):
        return url
    if len(_RESOLVED) > 5000:
        _RESOLVED.clear()
    _RESOLVED[url] = final
    return final


def _unwrap_login(final: str) -> str:
    """Facebook bounces logged-out visitors to /login/?next=<the reel>;
    the reel address is what we want."""
    p = urllib.parse.urlparse(final)
    if "/login" in (p.path or ""):
        nxt = urllib.parse.parse_qs(p.query or "").get("next")
        if nxt:
            return nxt[0]
    return final


_RESOLVED: dict = {}


def legacy_key(url: str) -> str:
    """The pre-v0.64.2 generic key ("url:host/path?query-minus-tracking")
    for hosts that now get an ID key — so results stored last week are
    still found instead of re-run."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path or ""
    query = urllib.parse.parse_qs(parsed.query or "")
    old_tracking = {"si", "feature", "utm_source", "utm_medium", "utm_campaign",
                    "utm_term", "utm_content", "fbclid", "gclid", "igsh", "igshid",
                    "ref", "ref_src", "s", "t"}
    kept = {k: v for k, v in query.items() if k.lower() not in old_tracking}
    clean_query = urllib.parse.urlencode(sorted(kept.items()), doseq=True)
    return f"url:{host}{path.rstrip('/')}" + (f"?{clean_query}" if clean_query else "")


def text_key(text: str) -> str:
    """Stable cache key for a TYPED claim: normalized, hashed.
    'The moon is cheese' and '  the MOON is cheese ' share one key."""
    import hashlib

    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return "text:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]


def looks_like_url(s: str) -> bool:
    s = (s or "").strip()
    if s.startswith(("http://", "https://")):
        return True
    # bare domains like youtube.com/watch?v=... typed without the scheme
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/|$)", s.lower()))


# ------------------------------------------------------------ postgres cache


_conn = None


def _get_conn():
    """Lazy single connection. None if no DATABASE_URL or connect fails."""
    global _conn
    if _conn is not None:
        try:
            with _conn.cursor() as cur:  # cheap liveness probe
                cur.execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None  # reconnect below

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    # Railway/Heroku style postgres:// -> psycopg wants postgresql://
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    try:
        import psycopg

        _conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        with _conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    url_key    TEXT PRIMARY KEY,
                    url        TEXT NOT NULL,
                    result     JSONB NOT NULL,
                    hits       INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        return _conn
    except Exception:
        _conn = None
        return None


def cache_available() -> bool:
    """Is the results database reachable right now? A miss while it is
    down looks exactly like a first check — this tells them apart."""
    return _get_conn() is not None


def get_cached(url_key: str, max_age_days: int = 0):
    """Return the stored result dict for this key, or None. Counts the hit.

    max_age_days > 0: treat results older than that as expired (return
    None so the pipeline re-checks with fresh evidence). Used on the
    check path so old verdicts don't outlive the news cycle; permalinks
    pass 0 so a share link ALWAYS keeps working.
    """
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checks SET hits = hits + 1 WHERE url_key = %s "
                "RETURNING result, created_at, "
                "created_at < now() - make_interval(days => %s)",
                (url_key, max_age_days if max_age_days > 0 else 0),
            )
            row = cur.fetchone()
        if not row:
            return None
        if max_age_days > 0 and row[2]:
            return None  # expired: caller re-runs and overwrites
        result = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        result["cached"] = True
        result["first_checked_at"] = row[1].isoformat()
        return result
    except Exception:
        return None


def save_result(url_key: str, url: str, result: dict) -> None:
    """Store a fresh pipeline result. Silently no-ops on failure."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        clean = {k: v for k, v in result.items()
                 if k not in ("cached", "first_checked_at",
                              "user_question", "user_answer")}
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checks (url_key, url, result) VALUES (%s, %s, %s) "
                "ON CONFLICT (url_key) DO UPDATE SET result = EXCLUDED.result, "
                "created_at = now()",
                (url_key, url, json.dumps(clean)),
            )
    except Exception:
        pass


# ------------------------------------------------------------ route audits
# Spec §3.10: every classification explainable and reproducible.


def save_route_audit(item_key: str, url: str, claims: list,
                     model_version: str, taxonomy_version: str) -> None:
    """Store one audit row per routed claim. Silently no-ops on failure."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_audits (
                    id BIGSERIAL PRIMARY KEY,
                    item_key TEXT NOT NULL,
                    url TEXT NOT NULL,
                    claim_id INTEGER NOT NULL,
                    claim_text TEXT NOT NULL,
                    gate_label TEXT,
                    primary_bucket TEXT,
                    secondary_bucket TEXT,
                    confidence REAL,
                    risk_level TEXT,
                    developing_story BOOLEAN,
                    public_safety_risk BOOLEAN,
                    reason_for_bucket TEXT,
                    signals_used TEXT,
                    model_version TEXT,
                    taxonomy_version TEXT,
                    human_review_required BOOLEAN,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            for idx, c in enumerate(claims):
                cur.execute(
                    """
                    INSERT INTO route_audits (
                        item_key, url, claim_id, claim_text, gate_label,
                        primary_bucket, secondary_bucket, confidence,
                        risk_level, developing_story, public_safety_risk,
                        reason_for_bucket, signals_used, model_version,
                        taxonomy_version, human_review_required
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        item_key, url, idx, c.get("claim", ""),
                        c.get("gate_label"), c.get("bucket"),
                        c.get("secondary_bucket"), c.get("confidence"),
                        c.get("risk_level"), c.get("developing_story", False),
                        c.get("public_safety_risk", False),
                        c.get("reason", ""), "ai_routing",
                        model_version, taxonomy_version,
                        c.get("human_review_required", False),
                    ),
                )
    except Exception:
        pass


def list_recent_checks(limit: int = 12) -> list:
    """Most recently checked items for the sidebar. [] on failure."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url_key, result->>'title', "
                "result->'report'->>'headline_score', "
                "result->'report'->>'headline_state', "
                "(result->>'answer_mode') = 'true', "
                "result->'scam'->>'risk', "
                "result->>'fresh_reason', hits "
                "FROM checks "
                "WHERE coalesce(result->>'content_rating', 'general') = 'general' "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        from app.agents.safety import mask_profanity
        out = []
        for r in rows:
            out.append({
                "url_key": r[0],
                "title": mask_profanity((r[1] or "(untitled)")[:80]),
                "score": float(r[2]) if r[2] is not None else None,
                "state": r[3] or "unverified",
                "answer": bool(r[4]),
                "scam": r[5] if r[5] in ("medium", "high") else None,
                "fresh_reason": r[6],
                "hits": int(r[7] or 0),
            })
        return out
    except Exception:
        return []


# ------------------------------------------------------------ quality loop
# Spec: corrections policy needs TRACKING — a report that vanishes into
# email is a promise; a report row with a status is a system.


def save_mistake_report(url_key: str, url: str, message: str,
                        contact: str = "", kind: str = "wrong") -> bool:
    """Store a user mistake report. False when storage is unavailable."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mistake_reports (
                    id BIGSERIAL PRIMARY KEY,
                    url_key TEXT,
                    url TEXT,
                    message TEXT NOT NULL,
                    contact TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    resolution TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    resolved_at TIMESTAMPTZ
                )
                """
            )
            cur.execute("ALTER TABLE mistake_reports ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'wrong'")
            cur.execute(
                "INSERT INTO mistake_reports (url_key, url, message, contact, kind) "
                "VALUES (%s, %s, %s, %s, %s)",
                (url_key[:200], url[:500], message[:2000], contact[:200],
                 kind if kind in ("wrong", "inappropriate") else "wrong"),
            )
        return True
    except Exception:
        return False


def list_mistake_reports(status: str = "") -> list:
    """Reports newest-first, optionally filtered by status. [] on failure."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE mistake_reports ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'wrong'")
            if status:
                cur.execute(
                    "SELECT id, url_key, url, message, contact, status, "
                    "resolution, created_at, resolved_at, kind FROM mistake_reports "
                    "WHERE status = %s ORDER BY id DESC LIMIT 200", (status,))
            else:
                cur.execute(
                    "SELECT id, url_key, url, message, contact, status, "
                    "resolution, created_at, resolved_at, kind FROM mistake_reports "
                    "ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall()
        return [{
            "id": r[0], "url_key": r[1], "url": r[2], "message": r[3],
            "contact": r[4], "status": r[5], "resolution": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "resolved_at": r[8].isoformat() if r[8] else None,
            "kind": r[9] or "wrong",
        } for r in rows]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def resolve_mistake_report(report_id: int, status: str, note: str = "") -> bool:
    """Mark a report reviewed/fixed/rejected with a resolution note."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mistake_reports SET status = %s, resolution = %s, "
                "resolved_at = now() WHERE id = %s",
                (status[:40], note[:2000], report_id),
            )
        return True
    except Exception:
        return False


def quality_stats() -> dict:
    """Aggregate quality metrics from stored checks. Each metric is
    fetched independently so one missing table (e.g. mistake_reports on
    a fresh database) never blanks the whole dashboard."""
    conn = _get_conn()
    if conn is None:
        return {}
    out = {}

    def q(sql, params=None):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None

    r = q("SELECT count(*), COALESCE(sum(hits), 0) FROM checks")
    if r:
        out["checks_stored"] = r[0][0]
        out["total_views"] = int(r[0][1])
    r = q("SELECT result->'report'->>'headline_state', count(*) "
          "FROM checks GROUP BY 1 ORDER BY 2 DESC")
    if r is not None:
        out["verdict_distribution"] = {(x[0] or "unknown"): x[1] for x in r}
    r = q("SELECT round(avg((result->'timings'->>'total_s')::float)::numeric, 1) "
          "FROM checks WHERE result->'timings'->>'total_s' IS NOT NULL "
          "AND result->>'transcript_source' IS DISTINCT FROM 'typed'")
    if r:
        out["avg_check_seconds"] = float(r[0][0]) if r[0][0] is not None else None
    r = q("SELECT status, count(*) FROM mistake_reports GROUP BY 1")
    if r is not None:
        out["reports_by_status"] = {x[0]: x[1] for x in r}
    return out


# ------------------------------------------------------------ daily usage
# Armor: the cost kill-switch needs to know how much was spent today.


def add_usage(est_cost: float) -> None:
    """Record one fresh check's estimated cost. No-ops on failure."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    day DATE PRIMARY KEY,
                    checks INTEGER NOT NULL DEFAULT 0,
                    est_cost NUMERIC NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                "INSERT INTO daily_usage (day, checks, est_cost) "
                "VALUES (CURRENT_DATE, 1, %s) "
                "ON CONFLICT (day) DO UPDATE SET "
                "checks = daily_usage.checks + 1, "
                "est_cost = daily_usage.est_cost + EXCLUDED.est_cost",
                (est_cost,),
            )
    except Exception:
        pass


def daily_usage_series(days: int = 14) -> list:
    """Fresh checks + est cost per day, oldest first. [] on failure."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.day::date::text, COALESCE(u.checks, 0),
                       COALESCE(u.est_cost, 0)
                FROM generate_series(
                    CURRENT_DATE - %s::int + 1, CURRENT_DATE, '1 day'
                ) AS d(day)
                LEFT JOIN daily_usage u ON u.day = d.day
                ORDER BY d.day
                """,
                (days,),
            )
            rows = cur.fetchall()
        return [{"day": r[0], "checks": r[1], "est_cost": float(r[2])}
                for r in rows]
    except Exception:
        return []


def admin_recent_checks(limit: int = 25) -> list:
    """Recent checks with hits + timestamps for the admin page."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url_key, url, result->>'title', "
                "result->'report'->>'headline_score', "
                "result->'report'->>'headline_state', "
                "(result->>'answer_mode') = 'true', hits, "
                "result->'timings'->>'total_s', created_at, "
                "result->>'fresh_reason' "
                "FROM checks ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [{
            "url_key": r[0], "url": r[1],
            "title": (r[2] or "(untitled)")[:90],
            "score": float(r[3]) if r[3] is not None else None,
            "state": r[4] or "unverified",
            "answer": bool(r[5]),
            "hits": r[6],
            "seconds": float(r[7]) if r[7] else None,
            "created_at": r[8].isoformat() if r[8] else None,
            "fresh_reason": r[9],
        } for r in rows]
    except Exception:
        return []


# ------------------------------------------------------------ visitors
# Privacy-first unique-visitor counting: each visit stores a one-way
# hash that includes the DATE, so the same person hashes differently
# tomorrow — counts exist, tracking is impossible, no IPs are stored.


def record_visitor(visitor_hash: str) -> None:
    """Count one visitor for today. No-ops on failure."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_visitors (
                    day DATE NOT NULL DEFAULT CURRENT_DATE,
                    visitor TEXT NOT NULL,
                    PRIMARY KEY (day, visitor)
                )
                """
            )
            cur.execute(
                "INSERT INTO daily_visitors (day, visitor) "
                "VALUES (CURRENT_DATE, %s) ON CONFLICT DO NOTHING",
                (visitor_hash[:64],),
            )
    except Exception:
        pass


def record_event(kind: str) -> None:
    """Count one UI event (e.g. a result copied). No-ops on failure."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_events (
                    day DATE NOT NULL DEFAULT CURRENT_DATE,
                    kind TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, kind)
                )
                """
            )
            cur.execute(
                "INSERT INTO daily_events (day, kind, count) "
                "VALUES (CURRENT_DATE, %s, 1) "
                "ON CONFLICT (day, kind) DO UPDATE SET "
                "count = daily_events.count + 1",
                (kind[:40],),
            )
    except Exception:
        pass


def event_stats() -> dict:
    """{kind: {'today': n, 'total': n}} for all counted events."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, sum(count), "
                "sum(count) FILTER (WHERE day = CURRENT_DATE) "
                "FROM daily_events GROUP BY kind"
            )
            return {r[0]: {"total": int(r[1]), "today": int(r[2] or 0)}
                    for r in cur.fetchall()}
    except Exception:
        return {}


def visitor_total() -> int:
    """All-time visitors served (each person counts once per day visited)."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM daily_visitors")
            return cur.fetchone()[0]
    except Exception:
        return 0


def visitor_series(days: int = 14) -> dict:
    """{'YYYY-MM-DD': count} unique visitors per day. {} on failure."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT day::text, count(*) FROM daily_visitors "
                "WHERE day >= CURRENT_DATE - %s::int + 1 GROUP BY day",
                (days,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        return {}


def total_fresh_checks():
    """All-time count of fresh checks run (summed across every day)."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(checks), 0) FROM daily_usage")
            return int(cur.fetchone()[0])
    except Exception:
        return 0


def today_usage():
    """(checks, est_cost) for today. (0, 0.0) if unavailable."""
    conn = _get_conn()
    if conn is None:
        return (0, 0.0)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT checks, est_cost FROM daily_usage WHERE day = CURRENT_DATE"
            )
            row = cur.fetchone()
        return (row[0], float(row[1])) if row else (0, 0.0)
    except Exception:
        return (0, 0.0)


# ------------------------------------------------------------ monthly uniques
# A second code that rotates MONTHLY (never across months, no IP stored)
# so "unique visitors this month" is a real number, not daily counts
# summed. Grand total = sum of monthly uniques, labeled as such.


def record_visitor_month(month_hash: str) -> None:
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monthly_visitors (
                    month TEXT NOT NULL,
                    visitor TEXT NOT NULL,
                    PRIMARY KEY (month, visitor)
                )
                """
            )
            cur.execute(
                "INSERT INTO monthly_visitors (month, visitor) "
                "VALUES (to_char(CURRENT_DATE, 'YYYY-MM'), %s) ON CONFLICT DO NOTHING",
                (month_hash[:64],),
            )
    except Exception:
        pass


def visitor_monthly() -> list:
    """[{'month': 'YYYY-MM', 'unique': n}] newest first. [] on failure."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT month, count(*) FROM monthly_visitors "
                        "GROUP BY month ORDER BY month DESC")
            return [{"month": r[0], "unique": r[1]} for r in cur.fetchall()]
    except Exception:
        return []


# ------------------------------------------------------------ calendar
def month_calendar(month: str) -> list:
    """Per-day rows for a 'YYYY-MM' month: unique visitors, fresh checks,
    est cost. Every day of the month is present (zeros where quiet)."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH days AS (
                  SELECT generate_series(
                    to_date(%s || '-01', 'YYYY-MM-DD'),
                    (to_date(%s || '-01', 'YYYY-MM-DD') + interval '1 month' - interval '1 day')::date,
                    '1 day')::date AS day
                ),
                v AS (SELECT day, count(*) AS visitors FROM daily_visitors GROUP BY day)
                SELECT d.day::text, COALESCE(v.visitors, 0),
                       COALESCE(u.checks, 0), COALESCE(u.est_cost, 0)
                FROM days d
                LEFT JOIN v ON v.day = d.day
                LEFT JOIN daily_usage u ON u.day = d.day
                ORDER BY d.day
                """,
                (month, month),
            )
            return [{"day": r[0], "visitors": int(r[1]), "checks": int(r[2]),
                     "est_cost": float(r[3])} for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def day_detail(day: str) -> dict:
    """One day: visitors, fresh checks, est cost, events, and the checks
    stored that day (title + state + views) for the admin calendar."""
    conn = _get_conn()
    out = {"day": day, "visitors": 0, "checks": 0, "est_cost": 0.0,
           "events": {}, "stored": []}
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM daily_visitors WHERE day = %s::date", (day,))
            out["visitors"] = int(cur.fetchone()[0])
            cur.execute("SELECT checks, est_cost FROM daily_usage WHERE day = %s::date", (day,))
            r = cur.fetchone()
            if r:
                out["checks"], out["est_cost"] = int(r[0]), float(r[1])
            cur.execute("SELECT kind, count FROM daily_events WHERE day = %s::date", (day,))
            out["events"] = {r[0]: int(r[1]) for r in cur.fetchall()}
            try:
                cur.execute("SELECT kind, count(*) FROM score_feedback "
                            "WHERE created_at::date = %s::date GROUP BY kind", (day,))
                out["feedback"] = {r[0]: int(r[1]) for r in cur.fetchall()}
            except Exception:
                conn.rollback()
                out["feedback"] = {}
            cur.execute(
                """
                SELECT url_key, result->>'title', result->'report'->>'headline_state',
                       result->'report'->>'headline_score', hits
                FROM checks WHERE created_at::date = %s::date
                ORDER BY created_at DESC LIMIT 100
                """, (day,))
            out["stored"] = [{"url_key": r[0], "title": (r[1] or "")[:90],
                              "state": r[2], "score": r[3], "views": int(r[4] or 0)}
                             for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out

# ---- score feedback (Fair / Harsh / Wrong) ----
# A signal for the maintainers, never a vote on the score. One tap per
# device per result is enforced client-side; the server keeps a salted
# device hash only to de-duplicate, never an IP.

FEEDBACK_KINDS = ("fair", "harsh", "wrong", "ai_missed", "false_alarm",
                  "scam_missed", "scam_false_alarm")
FLAG_KINDS_SQL = "('harsh','wrong','ai_missed','false_alarm','scam_missed','scam_false_alarm')"


def save_feedback(url_key: str, kind: str, claim_idx, note: str,
                  device_hash: str) -> bool:
    if kind not in FEEDBACK_KINDS:
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS score_feedback (
                    id BIGSERIAL PRIMARY KEY,
                    url_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    claim_idx INTEGER,
                    note TEXT,
                    device_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # same device, same result, same claim -> update, not append
            cur.execute(
                "SELECT id FROM score_feedback WHERE url_key=%s AND device_hash=%s "
                "AND claim_idx IS NOT DISTINCT FROM %s ORDER BY id DESC LIMIT 1",
                (url_key[:300], device_hash[:64], claim_idx))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE score_feedback SET kind=%s, note=COALESCE(NULLIF(%s,''), note), "
                    "created_at=now() WHERE id=%s",
                    (kind, (note or "")[:300], row[0]))
            else:
                cur.execute(
                    "INSERT INTO score_feedback (url_key, kind, claim_idx, note, device_hash) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (url_key[:300], kind, claim_idx, (note or "")[:300], device_hash[:64]))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def feedback_summary(days: int = 30) -> dict:
    """Totals and harsh-rate over the window: (harsh+wrong)/all taps."""
    conn = _get_conn()
    out = {"days": days, "total": 0, "harsh_rate": None, "all_time": {}}
    for k in FEEDBACK_KINDS:
        out[k] = 0
        out["all_time"][k] = 0
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, count(*) FROM score_feedback "
                "WHERE created_at >= now() - (%s || ' days')::interval GROUP BY kind",
                (str(int(days)),))
            for k, n in cur.fetchall():
                if k in out:
                    out[k] = int(n)
            cur.execute("SELECT kind, count(*) FROM score_feedback GROUP BY kind")
            for k, n in cur.fetchall():
                if k in out["all_time"]:
                    out["all_time"][k] = int(n)
        out["total"] = out["fair"] + out["harsh"] + out["wrong"]
        if out["total"]:
            out["harsh_rate"] = round((out["harsh"] + out["wrong"]) / out["total"], 3)
        out["ai_flags"] = out["ai_missed"] + out["false_alarm"]
        out["scam_flags"] = out["scam_missed"] + out["scam_false_alarm"]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def feedback_daily(days: int = 14) -> list:
    """[{day, fair, harsh, wrong}] for the last N days, oldest first,
    every day present (zeros included) so the chart has a full axis."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d::date::text,
                       count(*) FILTER (WHERE f.kind='fair'),
                       count(*) FILTER (WHERE f.kind='harsh'),
                       count(*) FILTER (WHERE f.kind='wrong')
                FROM generate_series(current_date - (%s - 1), current_date, '1 day') AS d
                LEFT JOIN score_feedback f ON f.created_at::date = d::date
                GROUP BY d ORDER BY d
                """, (int(days),))
            return [{"day": r[0], "fair": int(r[1]), "harsh": int(r[2]), "wrong": int(r[3])}
                    for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def list_feedback(limit: int = 100, only_flags: bool = True) -> list:
    """Newest first; flags = harsh/wrong. Joins the stored check's title."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.url_key, f.kind, f.claim_idx, f.note, f.status,
                       f.created_at, c.result->>'title',
                       c.result->'report'->>'headline_score'
                FROM score_feedback f
                LEFT JOIN checks c ON c.url_key = f.url_key
                WHERE (%s = false OR f.kind IN """ + FLAG_KINDS_SQL + """)
                ORDER BY f.id DESC LIMIT %s
                """, (only_flags, int(limit)))
            return [{"id": r[0], "url_key": r[1], "kind": r[2], "claim_idx": r[3],
                     "note": r[4] or "", "status": r[5],
                     "created_at": r[6].isoformat() if r[6] else None,
                     "title": (r[7] or "")[:100], "score": r[8]}
                    for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def resolve_feedback(fid: int, status: str) -> bool:
    if status not in ("new", "reviewed", "rule_added", "score_was_right", "dismissed", "evidence_gap", "product_idea"):
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE score_feedback SET status=%s WHERE id=%s", (status, int(fid)))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ---- weekly flag review (proposals only; never changes a score) ----

def pending_flags(limit: int = 60) -> list:
    """Harsh/wrong flags not yet reviewed, oldest first."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, url_key, kind, claim_idx, note FROM score_feedback "
                "WHERE kind IN " + FLAG_KINDS_SQL + " AND status = 'new' "
                "ORDER BY id ASC LIMIT %s", (int(limit),))
            return [{"id": r[0], "url_key": r[1], "kind": r[2], "claim_idx": r[3], "note": r[4] or ""}
                    for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def load_result_quiet(url_key: str):
    """Stored result without counting a view (the review is not a reader)."""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT result FROM checks WHERE url_key = %s", (url_key,))
            row = cur.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


# ---- scam-engine audit trail ----
# Every engine run keeps its trace: the score, verdict, the capped
# dimensions and floors, how many lookups ran, where it came from (app or
# partner API) — and a hash of the text, never the text itself. A partner
# or a reader disputing a verdict quotes the audit_trace_id; the admin
# page looks it up. This is the audit-trace module the design note asked
# to reuse, in the shape the fact-check side already uses (a JSONB doc).

def save_scam_audit(trace_id: str, source: str, rep: dict, text_sha: str = "") -> bool:
    conn = _get_conn()
    if conn is None or not trace_id:
        return False
    try:
        aud = rep.get("audit") or {}
        doc = {"score": rep.get("scam_risk_score"), "verdict": rep.get("verdict"), "confidence": rep.get("confidence"),
               "scam_types": rep.get("scam_types"), "dims": aud.get("dims"), "floors": aud.get("floors"),
               "queries": aud.get("queries"), "model_extracted": aud.get("model_extracted"),
               "injection_attempt": aud.get("injection_attempt"), "status": rep.get("analysis_status"),
               "codes": [f.get("code") for f in (rep.get("risk_factors") or [])],
               "verification": {k: v for k, v in (rep.get("verification") or {}).items() if k != "sources"},
               "seconds": rep.get("seconds")}
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_audits (
                    trace_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    source TEXT,
                    score INTEGER,
                    verdict TEXT,
                    text_sha TEXT,
                    doc JSONB NOT NULL
                )
                """)
            cur.execute(
                "INSERT INTO scam_audits (trace_id, source, score, verdict, text_sha, doc) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (trace_id) DO NOTHING",
                (trace_id[:32], (source or "")[:20], doc["score"], (doc["verdict"] or "")[:40], (text_sha or "")[:64], json.dumps(doc)))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def load_scam_audit(trace_id: str) -> dict | None:
    conn = _get_conn()
    if conn is None or not trace_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trace_id, created_at, source, score, verdict, doc FROM scam_audits WHERE trace_id = %s", (trace_id[:32],))
            r = cur.fetchone()
        if not r:
            return None
        return {"trace_id": r[0], "created_at": r[1].isoformat() if r[1] else None, "source": r[2],
                "score": r[3], "verdict": r[4], "doc": r[5]}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def list_shadow_scams(limit: int = 50, min_score: int = 40) -> list:
    """Recent checks whose SHADOW scam verdict reached the card threshold —
    what readers would have seen. Titles are the stored (redacted) ones."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT url_key, created_at, result->>'title', result->>'platform',
                       (result->'scam_shadow'->>'score')::int, result->'scam_shadow'->>'verdict',
                       result->'scam_shadow'->'pattern_names', result->'scam_shadow'->>'confidence_label',
                       result->'scam_shadow'->'report'->>'audit_trace_id'
                FROM checks
                WHERE result ? 'scam_shadow' AND (result->'scam_shadow'->>'score') ~ '^[0-9]+$'
                  AND (result->'scam_shadow'->>'score')::int >= %s
                ORDER BY created_at DESC LIMIT %s
                """, (int(min_score), int(limit)))
            return [{"url_key": r[0], "created_at": r[1].isoformat() if r[1] else None, "title": (r[2] or "")[:120],
                     "platform": r[3], "score": r[4], "verdict": r[5], "pattern_names": r[6] or [], "confidence": r[7],
                     "trace_id": r[8]} for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def scam_audit_stats(days: int = 30) -> dict:
    """Verdict distribution over the window, by source — the admin tile."""
    conn = _get_conn()
    out = {"days": days, "total": 0, "by_verdict": {}, "by_source": {}}
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT verdict, source, count(*) FROM scam_audits WHERE created_at >= now() - (%s || ' days')::interval GROUP BY verdict, source", (str(int(days)),))
            for v, src, n in cur.fetchall():
                out["by_verdict"][v or "?"] = out["by_verdict"].get(v or "?", 0) + int(n)
                out["by_source"][src or "?"] = out["by_source"].get(src or "?", 0) + int(n)
                out["total"] += int(n)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out


# ---- Glowby's own scam dataset: redacted, consented, human-reviewed ----
# Every reader flag on the scam lens ("It's a scam" / "Not a scam") and
# every admin-confirmed trace can become a sample — but only REDACTED
# (codes, cards, SSNs, emails, addresses, phones replaced by typed
# placeholders), with its source and consent recorded, and only counted
# as ground truth after a person marks it verified. Raw evidence and the
# training set are separate tables; nothing users submit is ever trained
# on automatically.

SAMPLE_SOURCES = ("user_flag", "admin_trace", "admin_paste", "partner_api")


def save_scam_sample(redacted_text: str, label: str, channel: str, scam_types, requested_actions,
                     risk_signals, payment_method, source_type: str, consent: str,
                     trace_id: str = "", url_key: str = "", note: str = "", label_confidence: float = 0.5) -> int | None:
    conn = _get_conn()
    if conn is None or not redacted_text or label not in ("scam", "ok") or source_type not in SAMPLE_SOURCES:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_samples (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    observed_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    redacted_text TEXT NOT NULL,
                    channel TEXT,
                    label TEXT NOT NULL,
                    scam_types JSONB,
                    requested_actions JSONB,
                    risk_signals JSONB,
                    payment_method TEXT,
                    source_type TEXT NOT NULL,
                    consent TEXT NOT NULL,
                    trace_id TEXT,
                    url_key TEXT,
                    note TEXT,
                    label_confidence REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    human_verified BOOLEAN NOT NULL DEFAULT false,
                    reviewed_at TIMESTAMPTZ
                )
                """)
            cur.execute("SELECT id FROM scam_samples WHERE redacted_text = %s AND label = %s LIMIT 1", (redacted_text[:4000], label))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "INSERT INTO scam_samples (redacted_text, channel, label, scam_types, requested_actions, risk_signals, payment_method, "
                "source_type, consent, trace_id, url_key, note, label_confidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (redacted_text[:4000], (channel or "")[:20], label, json.dumps(list(scam_types or [])[:6]),
                 json.dumps(list(requested_actions or [])[:6]), json.dumps(list(risk_signals or [])[:10]),
                 (payment_method or None), source_type, (consent or "")[:80], (trace_id or "")[:32], (url_key or "")[:300],
                 (note or "")[:300], float(label_confidence or 0)))
            return cur.fetchone()[0]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def review_scam_sample(sample_id: int, status: str, label: str = "") -> bool:
    """A person decides: verified (with the final label), rejected, or back
    to pending. Only verified samples ever reach the corpus."""
    if status not in ("verified", "rejected", "pending"):
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            if label in ("scam", "ok"):
                cur.execute("UPDATE scam_samples SET status=%s, label=%s, human_verified=%s, label_confidence=%s, reviewed_at=now() WHERE id=%s",
                            (status, label, status == "verified", 0.97 if status == "verified" else 0.5, int(sample_id)))
            else:
                cur.execute("UPDATE scam_samples SET status=%s, human_verified=%s, reviewed_at=now() WHERE id=%s",
                            (status, status == "verified", int(sample_id)))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def list_scam_samples(status: str = "pending", limit: int = 100) -> list:
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, created_at, observed_date, redacted_text, channel, label, scam_types, requested_actions, risk_signals, "
                "payment_method, source_type, consent, trace_id, note, label_confidence, status, human_verified "
                "FROM scam_samples WHERE (%s = '' OR status = %s) ORDER BY id DESC LIMIT %s", (status or "", status or "", int(limit)))
            out = []
            for r in cur.fetchall():
                out.append({"id": r[0], "created_at": r[1].isoformat() if r[1] else None, "observed_date": str(r[2]) if r[2] else None,
                            "redacted_text": r[3], "channel": r[4], "label": r[5], "scam_types": r[6] or [], "requested_actions": r[7] or [],
                            "risk_signals": r[8] or [], "payment_method": r[9], "source_type": r[10], "consent": r[11],
                            "trace_id": r[12], "note": r[13] or "", "label_confidence": r[14], "status": r[15], "human_verified": bool(r[16])})
            return out
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def scam_sample_stats() -> dict:
    conn = _get_conn()
    out = {"pending": 0, "verified": 0, "rejected": 0, "verified_scam": 0, "verified_ok": 0}
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, label, count(*) FROM scam_samples GROUP BY status, label")
            for st, lab, n in cur.fetchall():
                out[st] = out.get(st, 0) + int(n)
                if st == "verified":
                    out["verified_" + lab] = out.get("verified_" + lab, 0) + int(n)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out


# ---- the exam: validation and hidden cases live HERE, never in the repo ----
# The dev split ships with the code (app/data/scam_corpus.json). The
# validation and hidden splits are uploaded by a person and stored in
# Postgres; the hidden split is never returned to a caller — only run.

def save_exam_cases(cases: list, split: str, replace: bool = False) -> int:
    if split not in ("validation", "hidden"):
        return 0
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_exam_cases (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    split TEXT NOT NULL,
                    label TEXT NOT NULL,
                    text TEXT NOT NULL,
                    expected JSONB,
                    expected_types JSONB,
                    required_codes JSONB,
                    shape TEXT,
                    source TEXT
                )
                """)
            cur.execute("ALTER TABLE scam_exam_cases ADD COLUMN IF NOT EXISTS fingerprint TEXT")
            cur.execute("ALTER TABLE scam_exam_cases ADD COLUMN IF NOT EXISTS licence TEXT")
            cur.execute("ALTER TABLE scam_exam_cases ADD COLUMN IF NOT EXISTS reviewer TEXT")
            cur.execute("ALTER TABLE scam_exam_cases ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
            cur.execute("ALTER TABLE scam_exam_cases ADD COLUMN IF NOT EXISTS safe_action TEXT")
            if replace:
                cur.execute("DELETE FROM scam_exam_cases WHERE split = %s", (split,))
            n = 0
            for c in cases:
                if c.get("label") not in ("scam", "ok", "ambiguous", "insufficient") or not c.get("text"):
                    continue
                from app.agents.scamexam import fingerprint
                fp = fingerprint(c["text"])
                cur.execute("SELECT 1 FROM scam_exam_cases WHERE text = %s OR fingerprint = %s LIMIT 1", (c["text"][:2000], fp))
                if cur.fetchone():
                    continue  # the same — or a lightly rewritten — message must not appear in two splits
                cur.execute(
                    "INSERT INTO scam_exam_cases (split, label, text, expected, expected_types, required_codes, shape, source, fingerprint, licence, reviewer, reviewed_at, safe_action) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (split, c["label"], c["text"][:2000], json.dumps(c.get("expected") or []), json.dumps(c.get("expected_types") or []),
                     json.dumps(c.get("required_codes") or []), (c.get("shape") or "")[:40], (c.get("source") or "")[:40], fp,
                     (c.get("licence") or "")[:60], (c.get("reviewer") or "")[:60], (c.get("reviewed_at") or "")[:20], (c.get("safe_action") or "")[:200]))
                n += 1
        return n
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def load_exam_cases(split: str) -> list:
    """Used by the runner only. The hidden split's texts never leave the
    server: the runner returns rates, not cases."""
    conn = _get_conn()
    if conn is None or split not in ("validation", "hidden"):
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT label, text, expected, expected_types, required_codes, shape, source FROM scam_exam_cases WHERE split = %s ORDER BY id", (split,))
            return [{"label": r[0], "text": r[1], "expected": r[2] or [], "expected_types": r[3] or [], "required_codes": r[4] or [],
                     "shape": r[5] or "", "source": r[6] or "", "split": split} for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def exam_case_counts() -> dict:
    conn = _get_conn()
    out = {"validation": 0, "hidden": 0}
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT split, count(*) FROM scam_exam_cases GROUP BY split")
            for sp, n in cur.fetchall():
                out[sp] = int(n)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def save_review(doc: dict) -> int | None:
    """Store one review document; mark its flags 'reviewed' with the
    model's suggestion attached (the human still decides)."""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS flag_reviews (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    model TEXT,
                    flags INTEGER,
                    summary TEXT,
                    doc JSONB NOT NULL
                )
                """)
            cur.execute("ALTER TABLE score_feedback ADD COLUMN IF NOT EXISTS suggestion TEXT")
            cur.execute("ALTER TABLE score_feedback ADD COLUMN IF NOT EXISTS review_id BIGINT")
            cur.execute(
                "INSERT INTO flag_reviews (model, flags, summary, doc) VALUES (%s, %s, %s, %s) RETURNING id",
                (",".join(doc.get("models_used") or []) or doc.get("model_requested"),
                 len(doc.get("entries") or []), doc.get("summary"), json.dumps(doc)))
            rid = cur.fetchone()[0]
            for e in doc.get("entries") or []:
                fid = (e.get("flag") or {}).get("id")
                rv = e.get("review") or {}
                if fid and not rv.get("error"):
                    cur.execute(
                        "UPDATE score_feedback SET status='reviewed', suggestion=%s, review_id=%s WHERE id=%s",
                        (rv.get("assessment"), rid, int(fid)))
        return rid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def latest_review():
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, created_at, model, flags, summary, doc FROM flag_reviews ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
        if not r:
            return None
        doc = r[5] if isinstance(r[5], dict) else json.loads(r[5])
        doc.update({"id": r[0], "stored_at": r[1].isoformat() if r[1] else None,
                    "model": r[2], "flags": r[3]})
        # live statuses so decided flags show as decided
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM score_feedback WHERE review_id = %s", (r[0],))
            st = {row[0]: row[1] for row in cur.fetchall()}
        for e in doc.get("entries") or []:
            fid = (e.get("flag") or {}).get("id")
            e["status"] = st.get(fid, "new")
        return doc
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def last_review_at():
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max(created_at) FROM flag_reviews")
            r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


# ---- moderation (admin): keep a result out of Trending, or delete it ----

def hide_from_trending(url_key: str) -> bool:
    """Stamp a stored result 'mature' so Trending never lists it; the
    share link keeps working."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checks SET result = jsonb_set(result::jsonb, '{content_rating}', '\"mature\"'::jsonb, true) "
                "WHERE url_key = %s", (url_key,))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def delete_result(url_key: str) -> bool:
    """Remove a stored result entirely (share link stops working)."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM checks WHERE url_key = %s", (url_key,))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ---- detector calibration runs (admin tool) ----

def save_calibration(doc: dict):
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS calibration_runs (id BIGSERIAL PRIMARY KEY, "
                        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), doc JSONB NOT NULL)")
            cur.execute("INSERT INTO calibration_runs (doc) VALUES (%s) RETURNING id", (json.dumps(doc),))
            return cur.fetchone()[0]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def latest_calibration():
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, created_at, doc FROM calibration_runs ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
        if not r:
            return None
        doc = r[2] if isinstance(r[2], dict) else json.loads(r[2])
        doc["id"] = r[0]
        doc["stored_at"] = r[1].isoformat() if r[1] else None
        return doc
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def reader_labelled_media(limit: int = 200) -> list:
    """Videos readers said were AI (ai_missed) or real (false_alarm) —
    candidates for the calibration set. Reader labels are not certain;
    the admin picks which to trust."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.kind, f.url_key, f.note, f.created_at, c.result->>'url', c.result->>'title',
                       c.result->'authenticity'->>'origin_result'
                FROM score_feedback f LEFT JOIN checks c ON c.url_key = f.url_key
                WHERE f.kind IN ('ai_missed','false_alarm')
                ORDER BY f.id DESC LIMIT %s
                """, (int(limit),))
            return [{"label": "ai" if r[0] == "ai_missed" else "real", "url_key": r[1], "note": r[2] or "",
                     "created_at": r[3].isoformat() if r[3] else None, "url": r[4], "title": (r[5] or "")[:90],
                     "lane_said": r[6]} for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
