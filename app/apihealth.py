"""API & keys health for the Security page (v0.66.35). Passive and cheap:
nothing here makes an extra network call.

* note(service, ok, err): every outbound call the app already makes
  (Anthropic, OpenAI, Brave, Turnstile, the proxy/yt-dlp, the rescue
  scraper, TikTok, LinkedIn) reports whether it worked. Kept in memory
  and upserted to Postgres at most once a minute per service, so the
  Security page can show "last ok / last error / the error" per service
  even after a redeploy.
* count(kind): daily security counters (rate-limit hits, failed bot
  checks, partner calls, partner denials) in the same daily_events
  table the UI events use, under a "sec:" prefix.
* key_ages(): how long each secret has been in place, without ever
  seeing it: a salted fingerprint per secret is kept; when it changes,
  "rotated" is logged. The salt is random, generated once, stored in
  the database — not derived from any secret.
* scan_source(): a boot-time scan of the deployed code for anything
  shaped like an API key. Should always be empty.
* partner keys: identifiable in logs by a short id that is a hash of
  the key, never the key; per-key daily cap.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import secrets as _secrets
import threading
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(__file__)
SERVICES = ("anthropic", "openai", "brave", "turnstile", "proxy", "rescue", "hive", "tiktok", "linkedin", "google_factcheck")
ROTATE_DAYS = int(os.environ.get("GLOWBY_KEY_ROTATE_DAYS", "90"))
PARTNER_DAILY_CAP = int(os.environ.get("GLOWBY_PARTNER_DAILY_CAP", "500"))

_MEM: dict = {}
_LOCK = threading.Lock()
_LAST_WRITE: dict = {}


def _hive_key_source():
    """Which variable name holds the Hive key (never the value)."""
    try:
        from app.agents.hive_detect import key_source
        return key_source()
    except Exception:
        return None


def _db():
    from app.storage import _get_conn
    return _get_conn()


_READY = {"ok": False}


def _ensure():
    conn = _db()
    if conn is None:
        return None
    if _READY["ok"]:
        return conn
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS service_health (
                service TEXT PRIMARY KEY, last_ok TIMESTAMPTZ, last_err TIMESTAMPTZ, err TEXT NOT NULL DEFAULT '',
                ok_count INTEGER NOT NULL DEFAULT 0, err_count INTEGER NOT NULL DEFAULT 0)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS secret_ledger (
                name TEXT PRIMARY KEY, fp TEXT NOT NULL, since TIMESTAMPTZ NOT NULL DEFAULT now(), rotations INTEGER NOT NULL DEFAULT 0)""")
        _READY["ok"] = True
        return conn
    except Exception:
        return None


# ---------------------------------------------------------------- services
def note(service: str, ok: bool, err: str = "") -> None:
    """Record one outbound call's outcome. Never raises, never blocks."""
    try:
        now = time.time()
        with _LOCK:
            m = _MEM.setdefault(service, {"last_ok": None, "last_err": None, "err": "", "ok": 0, "fail": 0})
            if ok:
                m["last_ok"] = now; m["ok"] += 1
            else:
                m["last_err"] = now; m["fail"] += 1; m["err"] = (err or "")[:200]
            due = now - _LAST_WRITE.get(service, 0) > 60
            if due:
                _LAST_WRITE[service] = now
        if due:
            threading.Thread(target=_flush, args=(service,), daemon=True).start()
    except Exception:
        pass


def _flush(service: str) -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        with _LOCK:
            m = dict(_MEM.get(service) or {})
            ok_n, fail_n = m.get("ok", 0), m.get("fail", 0)
            _MEM[service]["ok"] = 0; _MEM[service]["fail"] = 0
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO service_health (service, last_ok, last_err, err, ok_count, err_count)
                VALUES (%s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s)
                ON CONFLICT (service) DO UPDATE SET
                  last_ok = GREATEST(service_health.last_ok, EXCLUDED.last_ok),
                  last_err = GREATEST(service_health.last_err, EXCLUDED.last_err),
                  err = CASE WHEN EXCLUDED.last_err IS NOT NULL AND (service_health.last_err IS NULL OR EXCLUDED.last_err >= service_health.last_err) THEN EXCLUDED.err ELSE service_health.err END,
                  ok_count = service_health.ok_count + EXCLUDED.ok_count,
                  err_count = service_health.err_count + EXCLUDED.err_count""",
                        (service, m.get("last_ok"), m.get("last_err"), m.get("err", ""), ok_n, fail_n))
    except Exception:
        pass


def services() -> list:
    """Per service: configured?, last ok, last error, the error, counts."""
    rows = {}
    conn = _ensure()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT service, last_ok, last_err, err, ok_count, err_count FROM service_health")
                for r in cur.fetchall():
                    rows[r[0]] = {"last_ok": r[1].isoformat() if r[1] else None, "last_err": r[2].isoformat() if r[2] else None,
                                  "err": r[3], "ok_count": int(r[4]), "err_count": int(r[5])}
        except Exception:
            pass
    env = os.environ
    configured = {"anthropic": bool(env.get("ANTHROPIC_API_KEY")), "openai": bool(env.get("OPENAI_API_KEY")),
                  "brave": bool(env.get("BRAVE_SEARCH_KEY")), "turnstile": bool(env.get("TURNSTILE_SECRET_KEY")),
                  "proxy": bool(env.get("WEBSHARE_PROXY_USERNAME") or env.get("PROXY_URL")), "rescue": bool(env.get("GLOWBY_RESCUE_TOKEN") or env.get("SCRAPECREATORS_KEY")),
                  "hive": bool(_hive_key_source()),
                  "tiktok": bool(env.get("GLOWBY_TIKTOK_CLIENT_KEY")), "linkedin": bool(env.get("GLOWBY_LINKEDIN_CLIENT_ID")),
                  "google_factcheck": bool(env.get("GOOGLE_API_KEY") or env.get("YOUTUBE_API_KEY"))}
    out = []
    with _LOCK:
        mem = {k: dict(v) for k, v in _MEM.items()}
    for s in SERVICES:
        r = dict(rows.get(s) or {"last_ok": None, "last_err": None, "err": "", "ok_count": 0, "err_count": 0})
        m = mem.get(s)
        if m:  # the minute not yet flushed
            if m["last_ok"] and (not r["last_ok"] or m["last_ok"] > datetime.fromisoformat(r["last_ok"]).timestamp()):
                r["last_ok"] = datetime.fromtimestamp(m["last_ok"], tz=timezone.utc).isoformat()
            if m["last_err"] and (not r["last_err"] or m["last_err"] > datetime.fromisoformat(r["last_err"]).timestamp()):
                r["last_err"] = datetime.fromtimestamp(m["last_err"], tz=timezone.utc).isoformat(); r["err"] = m["err"]
            r["ok_count"] += m["ok"]; r["err_count"] += m["fail"]
        lo, le = r["last_ok"], r["last_err"]
        state = "unused" if not (lo or le) else ("failing" if le and (not lo or le > lo) else "ok")
        out.append({"service": s, "configured": configured.get(s, False), "state": state, **r})
    return out


# ----------------------------------------------------------------- counters
SEC_KINDS = ("rate_limited", "bot_check_failed", "partner_call", "partner_denied", "partner_capped", "admin_forbidden")


def count(kind: str, key_id: str = "") -> None:
    """One security event today (kind in SEC_KINDS; partner events also
    counted under the key's id so a pilot can see its own numbers)."""
    try:
        from app.storage import record_event
        if kind in SEC_KINDS:
            record_event("sec:" + kind)
        if key_id:
            record_event(f"pk:{key_id}:{kind}"[:40])
    except Exception:
        pass


def counters(days: int = 7) -> dict:
    """{kind: {'today': n, '7d': n}} for the sec:* events, plus per-key usage."""
    conn = _db()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT kind, sum(count) FILTER (WHERE day = CURRENT_DATE), sum(count) FROM daily_events "
                        "WHERE (kind LIKE 'sec:%%' OR kind LIKE 'pk:%%') AND day > CURRENT_DATE - make_interval(days => %s) GROUP BY kind", (days,))
            out = {"sec": {}, "keys": {}}
            for kind, today, total in cur.fetchall():
                if kind.startswith("sec:"):
                    out["sec"][kind[4:]] = {"today": int(today or 0), "week": int(total or 0)}
                else:
                    _, kid, what = (kind.split(":") + ["", ""])[:3]
                    out["keys"].setdefault(kid, {})[what] = {"today": int(today or 0), "week": int(total or 0)}
            return out
    except Exception:
        return {}


def key_calls_today(key_id: str) -> int:
    conn = _db()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count FROM daily_events WHERE day = CURRENT_DATE AND kind = %s", (f"pk:{key_id}:partner_call",))
            r = cur.fetchone()
            return int(r[0]) if r else 0
    except Exception:
        return 0


# ------------------------------------------------------------- partner keys
def key_id(key: str) -> str:
    """Pure: a short, log-safe id for a partner key (hash, never the key)."""
    return hashlib.sha256(("pk|" + (key or "")).encode()).hexdigest()[:8]


def partner_check(key: str, partners: list) -> tuple:
    """(allowed, reason, key_id). Counts the call or the denial."""
    kid = key_id(key)
    if not key or key not in partners:
        count("partner_denied")
        return False, "forbidden", kid
    if key_calls_today(kid) >= PARTNER_DAILY_CAP:
        count("partner_capped", kid)
        return False, "daily cap reached for this key", kid
    count("partner_call", kid)
    return True, "", kid


# ---------------------------------------------------------------- key ages
SECRET_VARS = ("GLOWBY_ADMIN_KEY", "GLOWBY_ADMIN_PASSWORD", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_SEARCH_KEY", "HIVE_API_KEY",
               "TURNSTILE_SECRET_KEY", "GLOWBY_PARTNER_KEYS", "GLOWBY_RESCUE_TOKEN", "WEBSHARE_PROXY_PASSWORD",
               "GLOWBY_TIKTOK_CLIENT_SECRET", "GLOWBY_LINKEDIN_CLIENT_SECRET", "GOOGLE_API_KEY", "DATABASE_URL")


def _salt() -> str:
    from app.marketing import setting, set_setting
    s = setting("sec:salt", "")
    if not s:
        s = _secrets.token_hex(16)
        set_setting("sec:salt", s)
    return s


def fingerprint_secret(value: str, salt: str) -> str:
    return hashlib.sha256((salt + "|" + value).encode()).hexdigest()[:16]


def record_secrets() -> list:
    """At boot: fingerprint every set secret; log a rotation when the
    fingerprint changed. Returns the ledger."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        salt = _salt()
        with conn.cursor() as cur:
            cur.execute("SELECT name, fp FROM secret_ledger")
            have = {r[0]: r[1] for r in cur.fetchall()}
            for name in SECRET_VARS:
                v = os.environ.get(name, "")
                if not v:
                    continue
                fp = fingerprint_secret(v, salt)
                if name not in have:
                    cur.execute("INSERT INTO secret_ledger (name, fp) VALUES (%s, %s) ON CONFLICT DO NOTHING", (name, fp))
                elif have[name] != fp:
                    cur.execute("UPDATE secret_ledger SET fp=%s, since=now(), rotations=rotations+1 WHERE name=%s", (fp, name))
        return key_ages()
    except Exception:
        return []


def key_ages() -> list:
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, since, rotations, EXTRACT(EPOCH FROM (now() - since))/86400.0 FROM secret_ledger ORDER BY name")
            return [{"name": r[0], "since": r[1].isoformat() if r[1] else None, "rotations": int(r[2]),
                     "days": int(float(r[3] or 0)), "overdue": float(r[3] or 0) > ROTATE_DAYS} for r in cur.fetchall()]
    except Exception:
        return []


# ------------------------------------------------------------- source scan
_KEY_SHAPES = (
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abp]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("Brave key", re.compile(r"\bBSA[A-Za-z0-9_\-]{24,}")),
    ("Postgres URL with password", re.compile(r"postgres(?:ql)?://[^:\s]+:[^@\s]{4,}@")),
    ("hard-coded secret", re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*=\s*['\"][A-Za-z0-9_\-]{24,}['\"]")),
)


def scan_source(root: str = _ROOT) -> list:
    """Anything shaped like a real key in the deployed code. Masked."""
    hits = []
    for pat in ("*.py", "agents/*.py", "templates/*.html", "*.md", "data/*.json"):
        for p in sorted(glob.glob(os.path.join(root, pat))):
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if "scan_source" in line or "_KEY_SHAPES" in line or "re.compile" in line:
                    continue
                for label, rx in _KEY_SHAPES:
                    m = rx.search(line)
                    if m:
                        s = m.group(0)
                        hits.append({"file": os.path.relpath(p, root), "line": i, "kind": label, "masked": s[:6] + "…" + s[-3:]})
    return hits


_SCAN = {"at": None, "hits": None}


def boot_scan() -> list:
    _SCAN["hits"] = scan_source(); _SCAN["at"] = datetime.now(timezone.utc).isoformat()
    return _SCAN["hits"]


def report() -> dict:
    from app.marketing import setting
    return {"services": services(), "counters": counters(), "key_ages": key_ages(), "rotate_days": ROTATE_DAYS,
            "hive_key_variable": _hive_key_source(),
            "partner_daily_cap": PARTNER_DAILY_CAP,
            "scan": {"at": _SCAN["at"], "hits": _SCAN["hits"] if _SCAN["hits"] is not None else scan_source()},
            "dependency_audit": setting("sec:pip_audit", "")}
