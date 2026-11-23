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
import os
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
            # v0.66.51: why it flipped, and what actually changed underneath
            cur.execute("ALTER TABLE qa_runs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE qa_runs ADD COLUMN IF NOT EXISTS detail TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE qa_runs ADD COLUMN IF NOT EXISTS url TEXT NOT NULL DEFAULT ''")
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
# v0.66.51: not every flip is a disagreement. The verdict WORD is decided
# by cut-offs (8.0 / 7.5 / 4.0), so a score moving from 7.5 to 7.4 changes
# "Mostly accurate" to "Mixed" — a tenth of a point stepping over a line we
# drew, not Glowby changing its mind. Counting those together made the card
# report 49% flipped when most of it was the measuring tape wobbling
# (Inderpreet, Oct 29). Three kinds now, and the card counts them apart.
_BAND_ORDER = ("Inaccurate", "Mixed", "Mostly accurate", "Accurate")
# one band apart and under a full point: the boundary moved, not the judge.
# (Checked against Inderpreet's Oct 29 list: this puts 8.2->7.5, 8.5->7.9
# and the 7.5->7.4 in wobble, and leaves 3.9->7.5, 8.3->7.0, 7.5->5.5 and
# 5.5->3.9 as real — three and four, which is what the eye says too.)
WOBBLE_DRIFT = float(os.environ.get("GLOWBY_QA_WOBBLE", "1.0"))
REAL_DRIFT = float(os.environ.get("GLOWBY_QA_REAL", "2.0"))


def band_gap(old_word: str, new_word: str):
    """Pure: how many band edges separate two verdict words, or None when
    one of them is off the scale (Unverified / a special card)."""
    if old_word not in _BAND_ORDER or new_word not in _BAND_ORDER:
        return None
    return abs(_BAND_ORDER.index(new_word) - _BAND_ORDER.index(old_word))


def flip_kind(cmp: dict):
    """Pure: '' (no flip), 'wobble', 'real', or 'lost' (it stopped being
    scoreable at all — an evidence-hunt failure, not a judging one)."""
    if not cmp.get("flipped"):
        return ""
    gap = band_gap(cmp.get("old_word") or "", cmp.get("new_word") or "")
    if gap is None:
        return "lost"
    drift = abs(float(cmp.get("drift") or 0))
    if gap >= 2 or drift >= REAL_DRIFT:
        return "real"
    if drift < WOBBLE_DRIFT:
        return "wobble"
    return "real"


def claim_diff(old: dict, new: dict, limit: int = 8) -> list:
    """Pure: which individual claims changed their verdict. This is the
    'what happened' behind a flip — the headline word moving tells you
    nothing about WHICH claim moved. Matched on the claim text, because
    claim order is not stable between runs. The re-check itself is still
    never saved; only this compact diff is."""
    def _index(r):
        out = {}
        for c in (r.get("claims") or []):
            if not isinstance(c, dict):
                continue
            key = (c.get("claim") or "").strip().lower()[:160]
            v = c.get("verdict") or {}
            if key:
                out[key] = {"claim": (c.get("claim") or "")[:160],
                            "verdict": (v.get("verdict_state") or v.get("verdict") or "")[:40],
                            "score": v.get("truth_score")}
        return out
    a, b = _index(old), _index(new)
    rows = []
    for key, ca in a.items():
        cb = b.get(key)
        if cb is None:
            rows.append({"claim": ca["claim"], "old": ca["verdict"], "new": "(not found this time)",
                         "old_score": ca["score"], "new_score": None})
        elif cb["verdict"] != ca["verdict"] or cb["score"] != ca["score"]:
            rows.append({"claim": ca["claim"], "old": ca["verdict"], "new": cb["verdict"],
                         "old_score": ca["score"], "new_score": cb["score"]})
    for key, cb in b.items():
        if key not in a:
            rows.append({"claim": cb["claim"], "old": "(not found before)", "new": cb["verdict"],
                         "old_score": None, "new_score": cb["score"]})
    return rows[:limit]


def compare(old: dict, new: dict) -> dict:
    """Pure: the headline comparison between a stored and a fresh result."""
    from app.agents.summary import canonical_lead
    ro, rn = (old.get("report") or {}), (new.get("report") or {})
    so, sn = ro.get("headline_score"), rn.get("headline_score")
    wo, wn = canonical_lead(old) or "", canonical_lead(new) or ""
    drift = round(float(sn) - float(so), 2) if so is not None and sn is not None else None
    flipped = bool(wo and wn and wo != wn)
    out = {"old_score": so, "new_score": sn, "old_state": ro.get("headline_state"), "new_state": rn.get("headline_state"),
           "old_word": wo, "new_word": wn, "drift": drift, "flipped": flipped}
    out["kind"] = flip_kind(out)
    out["detail"] = claim_diff(old, new)
    return out


def _record(url_key: str, title: str, cmp: dict, seconds: float, error: str = "", url: str = "") -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        detail = json.dumps(cmp.get("detail") or [])[:4000]
    except Exception:
        detail = ""
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO qa_runs (url_key, title, old_score, new_score, old_state, new_state, old_word, new_word, drift, flipped, seconds, error, kind, detail, url) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (url_key, title[:120], cmp.get("old_score"), cmp.get("new_score"), cmp.get("old_state"), cmp.get("new_state"),
                         cmp.get("old_word"), cmp.get("new_word"), cmp.get("drift"), bool(cmp.get("flipped")), seconds, error[:200],
                         cmp.get("kind") or "", detail, (url or "")[:500]))
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
                _record(key, str(old.get("title") or ""), {}, time.time() - t1, error=str(e)[:200], url=url)
                continue
            if not new:
                _record(key, str(old.get("title") or ""), {}, time.time() - t1, error="no result", url=url)
                continue
            cmp = compare(old, new)
            _record(key, str(old.get("title") or ""), cmp, time.time() - t1, url=url)
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


def recheck_one(run_pipeline, job_result, url_key: str) -> dict:
    """v0.66.51: re-run ONE stored result on demand and return the fresh
    comparison, so a flip on the card can be re-tested there and then
    instead of waiting for tonight. Same rules as the nightly run: the
    fresh result is never saved, and the comparison is recorded."""
    conn = _ensure()
    if conn is None:
        return {"ok": False, "detail": "no database"}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url, result FROM checks WHERE url_key = %s", (url_key,))
            row = cur.fetchone()
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}
    if not row or not isinstance(row[1], dict):
        return {"ok": False, "detail": "no stored result for that key"}
    url, old = row[0], row[1]
    jid = "qa-" + uuid.uuid4().hex[:8]
    t1 = time.time()
    try:
        run_pipeline(jid, url, url_key, prior_claims=(old.get("claims") or None), qa=True)
        new = job_result(jid)
    except Exception as e:
        _record(url_key, str(old.get("title") or ""), {}, time.time() - t1, error=str(e)[:200], url=url)
        return {"ok": False, "detail": str(e)[:200]}
    if not new:
        _record(url_key, str(old.get("title") or ""), {}, time.time() - t1, error="no result", url=url)
        return {"ok": False, "detail": "the re-check produced no result"}
    cmp = compare(old, new)
    _record(url_key, str(old.get("title") or ""), cmp, time.time() - t1, url=url)
    return {"ok": True, "url": url, "title": old.get("title") or "", **cmp,
            "seconds": round(time.time() - t1, 1)}


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
            cur.execute("SELECT day::text, url_key, title, old_score, new_score, old_word, new_word, drift, "
                        "coalesce(kind,''), coalesce(detail,''), coalesce(url,''), id FROM qa_runs "
                        "WHERE (flipped OR abs(drift) >= 1.5) AND day > CURRENT_DATE - make_interval(days => %s) ORDER BY id DESC LIMIT 40", (days,))
            flips = []
            for r in cur.fetchall():
                try:
                    det = json.loads(r[9]) if r[9] else []
                except Exception:
                    det = []
                flips.append({"day": r[0], "url_key": r[1], "title": r[2], "old_score": r[3], "new_score": r[4],
                              "old_word": r[5], "new_word": r[6], "drift": r[7],
                              # kind is blank on rows recorded before v0.66.51 — work it out on the way out
                              "kind": r[8] or flip_kind({"flipped": bool(r[5] and r[6] and r[5] != r[6]),
                                                         "old_word": r[5], "new_word": r[6], "drift": r[7]}),
                              "detail": det, "url": r[10], "id": r[11]})
            # v0.66.51: count the kinds apart. "49% flipped" was true and
            # useless; what matters is how many were real disagreements.
            cur.execute("SELECT coalesce(kind,''), count(*) FROM qa_runs WHERE flipped "
                        "AND day > CURRENT_DATE - make_interval(days => %s) GROUP BY 1", (days,))
            kinds = {(k or "unclassified"): int(n) for k, n in cur.fetchall()}
        return {"days": by_day, "flips": flips, "kinds": kinds,
                "last": _STATE["last"], "running": _STATE["running"]}
    except Exception:
        return {"days": [], "flips": [], "last": _STATE["last"], "running": _STATE["running"]}


# ------------------------------------------------- review them all
# v0.66.59: "how do I review all of these" — by not reading 150 rows one
# at a time. Two questions answer most of it in one screen:
#   1. How many of those flips survive the NEW bands? Both scores are
#      stored, so every past re-check can be re-scored against today's
#      cut-offs without waiting a fortnight for fresh data.
#   2. When a verdict did move, WHAT moved underneath — the same claims
#      scoring differently, or a different set of claims being found?
#      Those have completely different fixes. (Inderpreet, Nov 11)

def reclassify(days: int = 14) -> dict:
    """Re-score every stored re-check against the bands in force now."""
    from app import bands as _b
    conn = _ensure()
    if conn is None:
        return {"rows": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT old_score, new_score, old_word, new_word, flipped "
                        "FROM qa_runs WHERE error = '' AND old_score IS NOT NULL "
                        "AND new_score IS NOT NULL AND day > CURRENT_DATE - make_interval(days => %s)",
                        (days,))
            rows = cur.fetchall()
    except Exception as e:
        return {"rows": 0, "error": str(e)[:160]}
    was = now = 0
    kinds = {"real": 0, "wobble": 0, "lost": 0}
    for o_s, n_s, o_w, n_w, flip in rows:
        if flip:
            was += 1
        # the word each score WOULD get today, stickiness included
        ob = _b.band_of(o_s)
        nb = _b.band_of(n_s, ob)
        if ob == nb:
            continue
        now += 1
        cmp = {"flipped": True, "old_word": _b.LEADS[ob], "new_word": _b.LEADS[nb],
               "drift": (float(n_s) - float(o_s)) if o_s is not None and n_s is not None else None}
        kinds[flip_kind(cmp) or "real"] = kinds.get(flip_kind(cmp) or "real", 0) + 1
    return {"rows": len(rows), "flips_before": was, "flips_now": now,
            "kinds_now": kinds, "cuts": list(_b.cuts()), "sticky": _b.STICKY_MARGIN,
            "removed": max(0, was - now),
            "pct_before": round(was / len(rows) * 100) if rows else 0,
            "pct_now": round(now / len(rows) * 100) if rows else 0}


def patterns(days: int = 14, limit: int = 12) -> dict:
    """What actually moved underneath the flips, in aggregate."""
    conn = _ensure()
    if conn is None:
        return {"rows": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title, url_key, coalesce(detail,'') FROM qa_runs "
                        "WHERE flipped AND coalesce(detail,'') <> '' "
                        "AND day > CURRENT_DATE - make_interval(days => %s) ORDER BY id DESC LIMIT 400",
                        (days,))
            rows = cur.fetchall()
    except Exception as e:
        return {"rows": 0, "error": str(e)[:160]}
    moves, repeat, same_set, changed_set, no_detail = {}, {}, 0, 0, 0
    for title, key, raw in rows:
        try:
            det = json.loads(raw) or []
        except Exception:
            det = []
        if not det:
            no_detail += 1
            continue
        # a claim that appeared or vanished means the EXTRACTION moved;
        # everything else means the same claims were judged differently
        vanished = any("not found" in str(c.get("old", "")) + str(c.get("new", "")) for c in det)
        if vanished:
            changed_set += 1
        else:
            same_set += 1
        for c in det:
            a, b = str(c.get("old", "?"))[:30], str(c.get("new", "?"))[:30]
            if a != b:
                moves[f"{a} → {b}"] = moves.get(f"{a} → {b}", 0) + 1
            t = (c.get("claim") or "")[:90]
            if t:
                repeat[t] = repeat.get(t, 0) + 1
    top_moves = sorted(moves.items(), key=lambda x: -x[1])[:limit]
    top_claims = [(t, n) for t, n in sorted(repeat.items(), key=lambda x: -x[1])[:limit] if n > 1]
    verdict = ("the same claims are being judged differently — the evidence hunt is the variable"
               if same_set > changed_set * 2 else
               "the claim set itself keeps changing — extraction is the variable"
               if changed_set > same_set * 2 else
               "both the claim set and the judgements move — look at the examples")
    return {"rows": len(rows), "same_claims": same_set, "changed_claims": changed_set,
            "no_detail": no_detail, "top_moves": top_moves, "repeat_offenders": top_claims,
            "reading": verdict}
