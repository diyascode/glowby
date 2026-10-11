"""Security ledger for the admin portal (v0.66.30). Weekly, not daily.

Three honest things the app CAN know and keep:

1. WHEN THE JUDGING LOGIC CHANGED. At every boot the code that decides a
   score is fingerprinted (judge, router, evidence, verdict, summary,
   vision, the rulebook .md files) and compared with the last boot. The
   ledger says: "v0.66.30 went live Oct 13 — judging logic unchanged
   since v0.66.16; app code changed." Nothing invents a git history.
2. WHICH SETTINGS CHANGED. The non-secret configuration (model names,
   flags, limits) is snapshotted the same way; secrets are recorded as
   set / unset by NAME only, never a value, never a hash of a value.
3. WHO OPENED ADMIN. Every login (success or failure), with the device
   family from the browser's user-agent, a short one-way network
   fingerprint (salted hash; never the address), and the method. Failed
   logins per day are the number that matters — a spike means guessing.

Plus a short health list (Turnstile on, rate limit, key length, partner
keys, sessions) and "sign out everywhere".
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(__file__)

JUDGING = ("agents/judge.py", "agents/router.py", "agents/evidence.py", "agents/verdict.py", "agents/summary.py",
           "agents/vision.py", "agents/claims.py", "agents/output.py", "agents/calibration.py", "agents/review.py",
           "agents/safety.py", "agents/llm.py", "router.md", "output.md", "*.md")
MODEL_VARS = ("GLOWBY_CLAUDE_MODEL", "GLOWBY_ROUTER_MODEL", "GLOWBY_JUDGE_MODEL_LOW", "GLOWBY_EVIDENCE_MODEL",
              "GLOWBY_SAFETY_MODEL", "GLOWBY_FORENSIC_MODEL", "GLOWBY_JUDGE_TIERING")
FLAG_VARS = ("GLOWBY_AUTHENTICITY", "GLOWBY_SCAM_MODE", "GLOWBY_SCAM_BUTTON", "GLOWBY_CACHE_KEEPALIVE", "GLOWBY_CACHE_TTL_DAYS",
             "GLOWBY_DAILY_BUDGET_USD", "GLOWBY_COST_PER_CHECK_EST", "GLOWBY_RATE_LIMIT_PER_HOUR", "GLOWBY_JOB_TIMEOUT_SECONDS",
             "GLOWBY_REVIEW_WEEKLY", "GLOWBY_MARKETING", "GLOWBY_LINKEDIN_AUTOPOST", "GLOWBY_TTS_MODEL", "GLOWBY_TTS_VOICE")
SECRET_VARS = ("GLOWBY_ADMIN_KEY", "GLOWBY_ADMIN_USER", "GLOWBY_ADMIN_PASSWORD", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
               "BRAVE_SEARCH_KEY", "TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY", "DATABASE_URL", "GLOWBY_PARTNER_KEYS",
               "GLOWBY_RESCUE_TOKEN", "GLOWBY_TIKTOK_CLIENT_KEY", "GLOWBY_TIKTOK_CLIENT_SECRET", "GLOWBY_LINKEDIN_CLIENT_ID",
               "GLOWBY_LINKEDIN_CLIENT_SECRET", "ENSEMBLE_API_KEY", "WEBSHARE_PROXY", "SCRAPECREATORS_KEY")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:12]


def fingerprint() -> dict:
    """Pure-ish: {relative_path: short_hash} for every code and rule file."""
    out = {}
    pats = ["main.py", "storage.py", "precheck.py", "marketing.py", "ads.py", "security.py", "money.py",
            "agents/*.py", "*.md", "templates/*.html"]
    for pat in pats:
        for p in sorted(glob.glob(os.path.join(_ROOT, pat))):
            rel = os.path.relpath(p, _ROOT).replace(os.sep, "/")
            try:
                out[rel] = _sha(p)
            except Exception:
                pass
    return out


def is_judging(rel: str) -> bool:
    return any(glob.fnmatch.fnmatch(rel, j) for j in JUDGING)


def config_snapshot() -> dict:
    """Non-secret settings by value; secrets by name, set/unset only."""
    return {
        "models": {k: os.environ.get(k, "") for k in MODEL_VARS},
        "flags": {k: os.environ.get(k, "") for k in FLAG_VARS},
        "secrets": {k: bool(os.environ.get(k)) for k in SECRET_VARS},
    }


def diff(a: dict, b: dict) -> dict:
    """Pure: what changed between two fingerprints → {judging: [...], app: [...]}."""
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {"judging": [k for k in changed if is_judging(k)], "app": [k for k in changed if not is_judging(k)]}


def config_diff(a: dict, b: dict) -> list:
    out = []
    for grp in ("models", "flags"):
        for k in sorted(set((a.get(grp) or {})) | set((b.get(grp) or {}))):
            va, vb = (a.get(grp) or {}).get(k, ""), (b.get(grp) or {}).get(k, "")
            if va != vb:
                out.append(f"{k}: {va or '(unset)'} → {vb or '(unset)'}")
    for k in sorted(set((a.get("secrets") or {})) | set((b.get("secrets") or {}))):
        va, vb = (a.get("secrets") or {}).get(k), (b.get("secrets") or {}).get(k)
        if va != vb:
            out.append(f"{k}: {'set' if vb else 'unset'}")
    return out


# ------------------------------------------------------------------ db
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
            cur.execute("""CREATE TABLE IF NOT EXISTS deploy_ledger (
                id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), version TEXT NOT NULL,
                files JSONB NOT NULL, config JSONB NOT NULL)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS admin_access (
                id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), day DATE NOT NULL DEFAULT CURRENT_DATE,
                ok BOOLEAN NOT NULL, method TEXT NOT NULL, device TEXT NOT NULL DEFAULT '', net TEXT NOT NULL DEFAULT '')""")
        _READY["ok"] = True
        return conn
    except Exception:
        return None


def record_deploy(version: str) -> dict | None:
    """At boot: add a ledger row when the code or config differs from the
    last row. Returns the row's diff summary (or None when unchanged)."""
    conn = _ensure()
    if conn is None:
        return None
    try:
        fp, cfg = fingerprint(), config_snapshot()
        with conn.cursor() as cur:
            cur.execute("SELECT version, files, config FROM deploy_ledger ORDER BY id DESC LIMIT 1")
            last = cur.fetchone()
            if last and last[0] == version and dict(last[1]) == fp and dict(last[2]) == cfg:
                return None
            cur.execute("INSERT INTO deploy_ledger (version, files, config) VALUES (%s, %s::jsonb, %s::jsonb)",
                        (version, json.dumps(fp), json.dumps(cfg)))
        return {"version": version, "diff": diff(dict(last[1]) if last else {}, fp), "config": config_diff(dict(last[2]) if last else {}, cfg)}
    except Exception:
        return None


def ledger(limit: int = 20) -> list:
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, at, version, files, config FROM deploy_ledger ORDER BY id DESC LIMIT %s", (limit + 1,))
            rows = cur.fetchall()
        out = []
        for i, r in enumerate(rows[:limit]):
            prev = rows[i + 1] if i + 1 < len(rows) else None
            d = diff(dict(prev[3]) if prev else {}, dict(r[3]))
            out.append({"at": r[1].isoformat() if r[1] else None, "version": r[2],
                        "judging_changed": d["judging"], "app_changed": d["app"],
                        "config_changed": config_diff(dict(prev[4]) if prev else {}, dict(r[4])),
                        "first": prev is None})
        return out
    except Exception:
        return []


def judging_last_changed(rows: list) -> dict | None:
    for r in rows:  # newest first
        if r["judging_changed"] and not r["first"]:
            return {"version": r["version"], "at": r["at"], "files": r["judging_changed"]}
    return None


# --------------------------------------------------------------- access
_UA_DEV = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux"))
_UA_BR = (("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Firefox", "Firefox"), ("Safari", "Safari"), ("Edg", "Edge"))


def device_family(ua: str) -> str:
    """Pure: 'iPhone · Safari' from a user-agent string; never the raw string."""
    ua = ua or ""
    dev = next((n for k, n in _UA_DEV if k in ua), "unknown device")
    br = next((n for k, n in _UA_BR if k in ua), "browser")
    if "Glowby" in ua or ("iPhone" in ua and "Safari" not in ua and "CriOS" not in ua):
        br = "Glowby app" if "iPhone" in ua or "iPad" in ua else br
    return f"{dev} · {br}"


def net_fingerprint(ip: str) -> str:
    salt = os.environ.get("GLOWBY_ADMIN_KEY", "") + "|net"
    return hashlib.sha256((salt + (ip or "")).encode()).hexdigest()[:8]


_SEEN = {}


def record_access(ok: bool, method: str, ua: str = "", ip: str = "", dedupe: bool = False) -> None:
    """Keep one row per login attempt. With dedupe (master-key use on
    plain API calls) one row per device+network per day."""
    key = (time.strftime("%Y-%m-%d"), method, device_family(ua), net_fingerprint(ip))
    if dedupe:
        if key in _SEEN:
            return
        _SEEN[key] = True
        if len(_SEEN) > 500:
            _SEEN.clear()
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admin_access (ok, method, device, net) VALUES (%s, %s, %s, %s)",
                        (bool(ok), method[:30], key[2][:60], key[3]))
            cur.execute("DELETE FROM admin_access WHERE at < now() - interval '180 days'")
    except Exception:
        pass


def access_report(days: int = 30) -> dict:
    conn = _ensure()
    if conn is None:
        return {"recent": [], "failed_by_day": [], "devices": []}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT at, ok, method, device, net FROM admin_access ORDER BY id DESC LIMIT 40")
            recent = [{"at": r[0].isoformat() if r[0] else None, "ok": r[1], "method": r[2], "device": r[3], "net": r[4]} for r in cur.fetchall()]
            cur.execute("SELECT day::text, count(*) FILTER (WHERE NOT ok), count(*) FILTER (WHERE ok) FROM admin_access "
                        "WHERE day > CURRENT_DATE - make_interval(days => %s) GROUP BY day ORDER BY day", (days,))
            byday = [{"day": r[0], "failed": int(r[1]), "ok": int(r[2])} for r in cur.fetchall()]
            cur.execute("SELECT device, net, count(*), max(at) FROM admin_access WHERE ok "
                        "AND at > now() - make_interval(days => %s) GROUP BY device, net ORDER BY max(at) DESC LIMIT 20", (days,))
            devices = [{"device": r[0], "net": r[1], "logins": int(r[2]), "last": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]
        return {"recent": recent, "failed_by_day": byday, "devices": devices}
    except Exception:
        return {"recent": [], "failed_by_day": [], "devices": []}


# --------------------------------------------------------------- health
def health(sessions: int, rate_limit: int, partner_keys: int) -> list:
    """[(label, ok, note)] — the short list worth glancing at weekly."""
    env = os.environ
    key = env.get("GLOWBY_ADMIN_KEY", "")
    items = [
        ("Admin password login", bool(env.get("GLOWBY_ADMIN_USER") and env.get("GLOWBY_ADMIN_PASSWORD")), "username + password set" if env.get("GLOWBY_ADMIN_USER") else "only the master key works — set GLOWBY_ADMIN_USER / _PASSWORD"),
        ("Master key length", len(key) >= 24, f"{len(key)} characters" + ("" if len(key) >= 24 else " — make it 24+")),
        ("Bot check (Turnstile)", bool(env.get("TURNSTILE_SECRET_KEY")), "on for fresh checks" if env.get("TURNSTILE_SECRET_KEY") else "off — anyone can start checks"),
        ("Rate limit", rate_limit > 0, f"{rate_limit} fresh checks per hour per visitor"),
        ("Daily spend cap", bool(env.get("GLOWBY_DAILY_BUDGET_USD", "1")), f"${env.get('GLOWBY_DAILY_BUDGET_USD', '30')}/day kill-switch"),
        ("Partner API keys", True, f"{partner_keys} issued"),
        ("Admin sessions alive", sessions <= 10, f"{sessions} device session(s) — sign out everywhere if that's more than you expect"),
        ("Secrets in Railway only", True, "no secret is ever in code, chat or this page — values are never shown here"),
    ]
    return [{"label": a, "ok": b, "note": c} for a, b, c in items]


def report(version: str, sessions: int, rate_limit: int, partner_keys: int) -> dict:
    rows = ledger()
    return {"version": version, "as_of": datetime.now(timezone.utc).isoformat(),
            "ledger": rows, "judging_last_changed": judging_last_changed(rows),
            "config": config_snapshot(), "access": access_report(),
            "health": health(sessions, rate_limit, partner_keys)}
