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

    # --- TikTok: /@user/video/1234567890, vm.tiktok.com/SHORTCODE
    if host.endswith("tiktok.com"):
        m = re.search(r"/video/(\d+)", path)
        if m:
            return f"tiktok:{m.group(1)}"
        code = path.strip("/").split("/")[0]
        if code:
            return f"tiktok:{code}"

    # --- X / Twitter: /user/status/1234567890
    if host in ("x.com", "twitter.com") or host.endswith(".twitter.com"):
        m = re.search(r"/status/(\d+)", path)
        if m:
            return f"x:{m.group(1)}"

    # --- everything else: normalized URL minus tracking params
    kept = {k: v for k, v in query.items() if k.lower() not in TRACKING_PARAMS}
    clean_query = urllib.parse.urlencode(sorted(kept.items()), doseq=True)
    return f"url:{host}{path.rstrip('/')}" + (f"?{clean_query}" if clean_query else "")


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


def get_cached(url_key: str):
    """Return the stored result dict for this key, or None. Counts the hit."""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checks SET hits = hits + 1 WHERE url_key = %s "
                "RETURNING result, created_at",
                (url_key,),
            )
            row = cur.fetchone()
        if not row:
            return None
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
                "ON CONFLICT (url_key) DO NOTHING",
                (url_key, url, json.dumps(clean)),
            )
    except Exception:
        pass
