"""Financial health inside admin (v0.66.30). Weekly, not daily.

What it costs to run Glowby, month by month: AI spend is ACTUAL (the
same per-call estimates the daily cap uses, summed from daily_usage),
everything else is a bill you log once (Railway, proxy, Apple, domain)
or a recurring amount with a status chip (estimate / actual / free).
The first boot seeds the ledger from the Financial Health tracker that
lived in a Claude artifact until Oct 2026; AI estimate entries from that
tracker are dropped because the app now knows the real number.

Ceiling: the documented $350/month band at scale; months over it are
flagged. No model calls, no network; pure bookkeeping.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

CATS = ("Hosting (Railway)", "Proxy (Webshare)", "AI — Anthropic", "AI — OpenAI", "Apple Developer", "Domain",
        "Scraper (EnsembleData)", "Legal / admin", "Other")
AI_CATS = ("AI — Anthropic", "AI — OpenAI")
CEILING = float(os.environ.get("GLOWBY_MONTHLY_CEILING_USD", "350"))
_SEED = os.path.join(os.path.dirname(__file__), "data", "money_seed.json")


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
            cur.execute("""CREATE TABLE IF NOT EXISTS money_recurring (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '', amount NUMERIC NOT NULL DEFAULT 0,
                cadence TEXT NOT NULL DEFAULT 'monthly', status TEXT NOT NULL DEFAULT 'estimate', pos INTEGER NOT NULL DEFAULT 0)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS money_entries (
                id TEXT PRIMARY KEY, day DATE NOT NULL, cat TEXT NOT NULL, amount NUMERIC NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'actual', created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("SELECT count(*) FROM money_recurring")
            if int(cur.fetchone()[0]) == 0 and os.path.exists(_SEED):
                seed = json.load(open(_SEED))
                for i, r in enumerate(seed.get("recurring") or []):
                    cur.execute("INSERT INTO money_recurring (id, name, purpose, amount, cadence, status, pos) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (r["id"], r["name"], r.get("purpose", ""), r.get("amount", 0), r.get("cadence", "monthly"), r.get("status", "estimate"), i))
                for e in seed.get("entries") or []:
                    if e.get("cat") in AI_CATS:
                        continue  # AI is actual now
                    cur.execute("INSERT INTO money_entries (id, day, cat, amount, note, kind) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (e["id"], e["date"], e["cat"], e.get("amount", 0), e.get("note", ""), e.get("kind", "estimate")))
        _READY["ok"] = True
        return conn
    except Exception:
        return None


def monthly_equiv(r: dict) -> float:
    return float(r["amount"]) / 12 if r.get("cadence") == "yearly" else float(r["amount"])


def ai_by_month(months: int = 8) -> dict:
    """{'YYYY-MM': dollars} actual AI spend from daily_usage."""
    conn = _ensure()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(day, 'YYYY-MM'), sum(est_cost), sum(checks) FROM daily_usage "
                        "WHERE day > date_trunc('month', CURRENT_DATE) - make_interval(months => %s) GROUP BY 1 ORDER BY 1", (months,))
            return {r[0]: {"usd": float(r[1] or 0), "checks": int(r[2] or 0)} for r in cur.fetchall()}
    except Exception:
        return {}


def recurring() -> list:
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, purpose, amount, cadence, status FROM money_recurring ORDER BY pos, name")
            return [{"id": r[0], "name": r[1], "purpose": r[2], "amount": float(r[3]), "cadence": r[4], "status": r[5]} for r in cur.fetchall()]
    except Exception:
        return []


def entries(months: int = 12) -> list:
    conn = _ensure()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, day::text, cat, amount, note, kind FROM money_entries "
                        "WHERE day > date_trunc('month', CURRENT_DATE) - make_interval(months => %s) ORDER BY day DESC, id DESC", (months,))
            return [{"id": r[0], "date": r[1], "cat": r[2], "amount": float(r[3]), "note": r[4], "kind": r[5]} for r in cur.fetchall()]
    except Exception:
        return []


def add_entry(day: str, cat: str, amount: float, note: str = "", kind: str = "actual") -> bool:
    conn = _ensure()
    if conn is None or cat not in CATS or kind not in ("actual", "estimate"):
        return False
    try:
        date.fromisoformat(day)
        eid = "e" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S%f")[:16]
        with conn.cursor() as cur:
            cur.execute("INSERT INTO money_entries (id, day, cat, amount, note, kind) VALUES (%s,%s,%s,%s,%s,%s)",
                        (eid, day, cat, round(float(amount), 2), (note or "")[:200], kind))
        return True
    except Exception:
        return False


def delete_entry(eid: str) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM money_entries WHERE id=%s", (eid,))
            return cur.rowcount > 0
    except Exception:
        return False


def update_recurring(rid: str, amount: float | None = None, status: str | None = None) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            if amount is not None:
                cur.execute("UPDATE money_recurring SET amount=%s WHERE id=%s", (max(0.0, float(amount)), rid))
            if status in ("estimate", "actual", "free"):
                cur.execute("UPDATE money_recurring SET status=%s WHERE id=%s", (status, rid))
        return True
    except Exception:
        return False


def summary() -> dict:
    """Everything the Money page shows."""
    ai = ai_by_month()
    rec = recurring()
    ent = entries()
    today = date.today(); cur_key = today.strftime("%Y-%m")
    months = sorted(set(list(ai) + [e["date"][:7] for e in ent] + [cur_key]))[-8:]
    by = {m: {"month": m, "ai": 0.0, "logged": 0.0, "checks": 0} for m in months}
    for m, v in ai.items():
        if m in by:
            by[m]["ai"] = round(v["usd"], 2); by[m]["checks"] = v["checks"]
    for e in ent:
        m = e["date"][:7]
        if m in by and e["cat"] not in AI_CATS:
            by[m]["logged"] += e["amount"]
    for m in by:
        by[m]["logged"] = round(by[m]["logged"], 2)
        by[m]["total"] = round(by[m]["ai"] + by[m]["logged"], 2)
        by[m]["over"] = by[m]["total"] > CEILING
    run_rate = sum(monthly_equiv(r) for r in rec if r["status"] != "free" and r["name"] not in ("Anthropic API", "OpenAI Whisper"))
    ai_month = by.get(cur_key, {}).get("ai", 0.0)
    last_month = (today.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    ai_last = by.get(last_month, {}).get("ai", 0.0)
    checks_month = by.get(cur_key, {}).get("checks", 0)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(), "ceiling": CEILING, "cats": CATS,
        "this_month": by.get(cur_key, {}), "last_month_ai": ai_last,
        "run_rate_fixed": round(run_rate, 2), "run_rate_total": round(run_rate + (ai_last or ai_month), 2),
        "cost_per_check": round(ai_month / checks_month, 4) if checks_month else None,
        "months": [by[m] for m in months], "recurring": rec, "entries": ent[:60],
        "ytd": round(sum(x["total"] for x in by.values() if x["month"].startswith(str(today.year))), 2),
        "estimates_outstanding": sum(1 for e in ent if e["kind"] == "estimate") + sum(1 for r in rec if r["status"] == "estimate"),
    }
