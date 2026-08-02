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
}


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

    # --- X / Twitter: /user/status/1234567890
    if host in ("x.com", "twitter.com") or host.endswith(".twitter.com"):
        m = re.search(r"/status/(\d+)", path)
        if m:
            return f"x:{m.group(1)}"

    # --- everything else: normalized URL minus tracking params
    kept = {k: v for k, v in query.items() if k.lower() not in TRACKING_PARAMS}
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
        clean = {k: v for k, v in result.items() if k not in ("cached", "first_checked_at")}
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
                "(result->>'answer_mode') = 'true' "
                "FROM checks ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "url_key": r[0],
                "title": (r[1] or "(untitled)")[:80],
                "score": float(r[2]) if r[2] is not None else None,
                "state": r[3] or "unverified",
                "answer": bool(r[4]),
            })
        return out
    except Exception:
        return []


# ------------------------------------------------------------ quality loop
# Spec: corrections policy needs TRACKING — a report that vanishes into
# email is a promise; a report row with a status is a system.


def save_mistake_report(url_key: str, url: str, message: str,
                        contact: str = "") -> bool:
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
            cur.execute(
                "INSERT INTO mistake_reports (url_key, url, message, contact) "
                "VALUES (%s, %s, %s, %s)",
                (url_key[:200], url[:500], message[:2000], contact[:200]),
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
            if status:
                cur.execute(
                    "SELECT id, url_key, url, message, contact, status, "
                    "resolution, created_at, resolved_at FROM mistake_reports "
                    "WHERE status = %s ORDER BY id DESC LIMIT 200", (status,))
            else:
                cur.execute(
                    "SELECT id, url_key, url, message, contact, status, "
                    "resolution, created_at, resolved_at FROM mistake_reports "
                    "ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall()
        return [{
            "id": r[0], "url_key": r[1], "url": r[2], "message": r[3],
            "contact": r[4], "status": r[5], "resolution": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "resolved_at": r[8].isoformat() if r[8] else None,
        } for r in rows]
    except Exception:
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
    """Aggregate quality metrics from stored checks. {} on failure."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        out = {}
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), COALESCE(sum(hits), 0) FROM checks")
            row = cur.fetchone()
            out["checks_stored"] = row[0]
            out["total_views"] = int(row[1])
            cur.execute(
                "SELECT result->'report'->>'headline_state', count(*) "
                "FROM checks GROUP BY 1 ORDER BY 2 DESC")
            out["verdict_distribution"] = {
                (r[0] or "unknown"): r[1] for r in cur.fetchall()}
            # videos only: typed claims/questions finish much faster and
            # would flatter the number; cache hits never re-run, so they
            # were never in it.
            cur.execute(
                "SELECT round(avg((result->'timings'->>'total_s')::float)::numeric, 1) "
                "FROM checks WHERE result->'timings'->>'total_s' IS NOT NULL "
                "AND result->>'transcript_source' IS DISTINCT FROM 'typed'")
            row = cur.fetchone()
            out["avg_check_seconds"] = float(row[0]) if row and row[0] is not None else None
            cur.execute(
                "SELECT status, count(*) FROM mistake_reports GROUP BY 1")
            out["reports_by_status"] = {r[0]: r[1] for r in cur.fetchall()}
        return out
    except Exception:
        return {}


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
                SELECT d.day::text, COALESCE(u.checks, 0),
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
                "result->'timings'->>'total_s', created_at "
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
