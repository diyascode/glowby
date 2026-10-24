"""Consistency harness (v0.66.37): every night, re-check a sample of
stored results WITHOUT saving, and compare. Same video, same evidence
memory, a day later — does Glowby say the same thing?

Why: a fact-checker that gives 7.8 on Monday and 5.9 on Tuesday for the
same clip is the thing a reporter would catch first. Until now drift
was only visible when a reader hit "Wrong". This measures it every
night on 30 real checks and shows it in admin: average drift, how many
verdicts flipped, and the flips themselves with links, so a flip
becomes a judge rule the way reader flags do.

Rules: never overwrites a stored result (people's share links keep the
score they shared); never counts as a "check" in the daily tally;
spends only what the credit window allows (app/boost.py) and stops
when the day's budget is 60% used so readers always have room; picks
half the sample from the most-viewed checks and half at random.
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone

_DB_READY = {"ok": False}


def _db():
    from app.storage import _get_conn
    return _get_conn()


def _ensure():
    conn = _db()
    if conn is None:
        return None
    if _DB_READY["ok"]:
        return conn
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS qa_runs (
                id SERIAL PRIMARY KEY, day DATE NOT NULL DEFAULT CURRENT_DATE, at TIMESTAMPTZ NOT NULL DEFAULT now(),
                url_key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                old_score DOUBLE PRECISION, new_score DOUBLE PRECISION, old_state TEXT, new_state TEXT,
                old_word TEXT, new_word TEXT, drift DOUBLE PRECISION, flipped BOOLEAN NOT NULL DEFAULT false,
                seconds DOUBLE PRECISION, error TEXT NOT NULL DEFAULT '')""")
        _DB_READY["ok"] = True
        return conn
    except Exception:
        return None


# ------------------------------------------------------------------ pick
def sample(n: int) -> list:
    """[(url_key, url, result)] — half most-viewed, half random, from the
    last 60 days, general-rated, scoreable, not re-checked this week."""
    conn = _ensure()
    if conn is None or n <= 0:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url_key, url, result, hits FROM checks "
                        "WHERE created_at > now() - interval '60 days' "
                        "AND coalesce(result->>'content_rating','general') = 'general' "
                        "AND result->'report'->>'headline_score' IS NOT NULL "
                        "AND url LIKE 'http%%' "
                        "AND url_key NOT IN (SELECT url_key FROM qa_runs WHERE day > CURRENT_DATE - 7) "
                        "ORDER BY hits DESC LIMIT 200")
            rows = cur.fetchall()
    except Exception:
        return []
    rows = [r for r in rows if isinstance(r[2], dict) and not (r[2].get("report") or {}).get("safety_notice")]
    top = rows[: max(1, n // 2)]
    rest = rows[len(top):]
    random.shuffle(rest)
    picked = top + rest[: n - len(top)]
    return [(r[0], r[1], r[2]) for r in picked]


# ----------------------------------------------------------------- compare
def compare(old: dict, new: dict) -> dict:
    """Pure: the headline comparison between a stored and a fresh result."""
    from app.agents.summary import canonical_lead
    ro, rn = (old.get("report") or {}), (new.get("report") or {})
    so, sn = ro.get("headline_score"), rn.get("headline_score")
    wo, wn = canonical_lead(old) or "", canonical_lead(new) or ""
    drift = round(float(sn) - float(so), 2) if so is not None and sn is not None else None
    flipped = bool(wo and wn and wo != wn)
    return {"old_score": so, "new_score": sn, "old_state": ro.get("headline_state"), "new_state": rn.get("headline_state"),
            "old_word": wo, "new_word": wn, "drift": drift, "flipped": flipped}


def _record(url_key: str, title: str, cmp: dict, seconds: float, error: str = "") -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO qa_runs (url_key, title, old_score, new_score, old_state, new_state, old_word, new_word, drift, flipped, seconds, error) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (url_key, title[:120], cmp.get("old_score"), cmp.get("new_score"), cmp.get("old_state"), cmp.get("new_state"),
                         cmp.get("old_word"), cmp.get("new_word"), cmp.get("drift"), bool(cmp.get("flipped")), seconds, error[:200]))
            cur.execute("DELETE FROM qa_runs WHERE at < now() - interval '120 days'")
    except Exception:
        pass


# --------------------------------------------------------------------- run
_STATE = {"running": False, "last": None}


def run_nightly(run_pipeline, job_result, budget_fn, spent_fn, cost_per_check: float, n: int) -> dict:
    """Re-check n stored results without saving. run_pipeline(job_id, url,
    url_key, prior_claims, qa=True) must honour qa=True by not saving;
    job_result(job_id) returns the finished result dict (or None)."""
    if _STATE["running"]:
        return {"ok": False, "detail": "already running"}
    _STATE["running"] = True
    done, flips, drifts, t0 = 0, 0, [], time.time()
    try:
        for key, url, old in sample(n):
            try:
                _, spent = spent_fn()
                if spent + cost_per_check > budget_fn() * 0.6:
                    break  # leave the rest of the day's budget to readers
            except Exception:
                break
            jid = "qa-" + uuid.uuid4().hex[:8]
            t1 = time.time()
            try:
                run_pipeline(jid, url, key, prior_claims=(old.get("claims") or None), qa=True)
                new = job_result(jid)
            except Exception as e:
                _record(key, str(old.get("title") or ""), {}, time.time() - t1, error=str(e)[:200])
                continue
            if not new:
                _record(key, str(old.get("title") or ""), {}, time.time() - t1, error="no result")
                continue
            cmp = compare(old, new)
            _record(key, str(old.get("title") or ""), cmp, time.time() - t1)
            done += 1
            if cmp["flipped"]:
                flips += 1
            if cmp["drift"] is not None:
                drifts.append(abs(cmp["drift"]))
        out = {"ok": True, "rechecked": done, "flips": flips,
               "avg_drift": round(sum(drifts) / len(drifts), 2) if drifts else None, "seconds": round(time.time() - t0)}
        _STATE["last"] = {**out, "at": datetime.now(timezone.utc).isoformat()}
        return out
    finally:
        _STATE["running"] = False


def start_background(run_pipeline, job_result, budget_fn, spent_fn, cost_per_check: float, n: int) -> bool:
    if _STATE["running"]:
        return False
    threading.Thread(target=run_nightly, args=(run_pipeline, job_result, budget_fn, spent_fn, cost_per_check, n), daemon=True).start()
    return True


# ----------------------------------------------------------------- summary
def summary(days: int = 14) -> dict:
    conn = _ensure()
    if conn is None:
        return {"days": [], "flips": [], "last": _STATE["last"], "running": _STATE["running"]}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT day::text, count(*) FILTER (WHERE error = ''), count(*) FILTER (WHERE flipped), "
                        "round(avg(abs(drift))::numeric, 2), count(*) FILTER (WHERE abs(drift) >= 1.5), count(*) FILTER (WHERE error <> '') "
                        "FROM qa_runs WHERE day > CURRENT_DATE - make_interval(days => %s) GROUP BY day ORDER BY day", (days,))
            by_day = [{"day": r[0], "n": int(r[1]), "flips": int(r[2]), "avg_drift": float(r[3]) if r[3] is not None else None,
                       "big": int(r[4]), "errors": int(r[5])} for r in cur.fetchall()]
            cur.execute("SELECT day::text, url_key, title, old_score, new_score, old_word, new_word, drift FROM qa_runs "
                        "WHERE (flipped OR abs(drift) >= 1.5) AND day > CURRENT_DATE - make_interval(days => %s) ORDER BY id DESC LIMIT 40", (days,))
            flips = [{"day": r[0], "url_key": r[1], "title": r[2], "old_score": r[3], "new_score": r[4], "old_word": r[5], "new_word": r[6], "drift": r[7]} for r in cur.fetchall()]
        return {"days": by_day, "flips": flips, "last": _STATE["last"], "running": _STATE["running"]}
    except Exception:
        return {"days": [], "flips": [], "last": _STATE["last"], "running": _STATE["running"]}
