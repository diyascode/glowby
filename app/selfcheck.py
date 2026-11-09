"""System check (v0.66.52).

One question, asked every morning and on demand: is Glowby actually
working right now? Not "did a request fail" — the "didn't run" card
already answers that — but "are all the moving parts still moving".

Why it exists: over the last fortnight every problem was found by
Inderpreet noticing something odd in a screenshot — Hive refusing calls
for a week, the Google fact-check lane silent since it was built, a
nightly harness quietly spending the detector's quota. None of those
announced themselves. Each one was visible in data the app already had.
This runs the questions he kept having to ask, on a schedule, and says
plainly which ones fail.

Rules: it never spends money (no vendor calls, no checks run), it never
raises, and a check it cannot evaluate is "unknown", never "ok".
States: ok | warn | fail | unknown.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"

# services whose silence is a real problem vs ones that are optional
CORE_SERVICES = ("anthropic", "openai", "proxy")
OPTIONAL_SERVICES = ("brave", "turnstile", "rescue", "hive",
                     "google_factcheck", "tiktok", "linkedin")


def _row(cid, title, state, detail, fix=""):
    return {"id": cid, "title": title, "state": state, "detail": detail, "fix": fix}


def _age_hours(iso) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return None


# --------------------------------------------------------------- checks
def check_database() -> dict:
    try:
        from app.marketing import setting, set_setting
        stamp = str(int(time.time()))
        if not set_setting("selfcheck:ping", stamp):
            return _row("db", "Database", FAIL, "the app could not write to Postgres",
                        "check DATABASE_URL in Railway and that the Postgres service is up")
        if setting("selfcheck:ping") != stamp:
            return _row("db", "Database", FAIL, "wrote a value and read back something else")
        return _row("db", "Database", OK, "reads and writes are working")
    except Exception as e:
        return _row("db", "Database", FAIL, f"not reachable: {str(e)[:120]}",
                    "check DATABASE_URL in Railway")


def check_budget(budget_fn, usage_fn) -> dict:
    try:
        n, spent = usage_fn()
        budget = float(budget_fn())
    except Exception as e:
        return _row("budget", "Daily budget", UNKNOWN, f"could not read spend: {str(e)[:100]}")
    if budget <= 0:
        return _row("budget", "Daily budget", FAIL, "the daily budget is zero — nothing can run",
                    "set DAILY_BUDGET_USD in Railway")
    left = budget - spent
    detail = f"${spent:.2f} of ${budget:.2f} used today across {n} checks"
    if left <= 0:
        return _row("budget", "Daily budget", FAIL, detail + " — readers are being turned away",
                    "raise DAILY_BUDGET_USD, or wait for midnight Pacific")
    if left < budget * 0.1:
        return _row("budget", "Daily budget", WARN, detail + " — under a tenth left")
    return _row("budget", "Daily budget", OK, detail)


def check_services() -> list:
    """One row per configured service, from the passive health ledger. No
    call is made: this reports what the app's own traffic already found."""
    try:
        from app.apihealth import services
        rows = services()
    except Exception as e:
        return [_row("services", "APIs & keys", UNKNOWN, f"ledger unreadable: {str(e)[:100]}")]
    out = []
    for r in rows:
        name = r.get("service", "")
        if not r.get("configured"):
            if name in CORE_SERVICES:
                out.append(_row(f"svc:{name}", name, FAIL, "no key is set and nothing works without it",
                                "add the key in Railway → Variables"))
            continue
        state_s = r.get("state")
        err = (r.get("err") or "")[:140]
        if state_s == "failing":
            sev = FAIL if name in CORE_SERVICES else WARN
            out.append(_row(f"svc:{name}", name, sev, f"last call failed: {err}",
                            "open Security → API & keys and use the ask-it-now button"))
        elif state_s == "unused":
            # a key that has never been used is not proof of anything
            out.append(_row(f"svc:{name}", name, WARN,
                            "a key is set but nothing has called it since the ledger started",
                            "press the ask-it-now button on Security → API & keys"))
        else:
            age = _age_hours(r.get("last_ok"))
            if age is not None and age > 72 and name in CORE_SERVICES:
                out.append(_row(f"svc:{name}", name, WARN, f"last worked {age:.0f} hours ago"))
            else:
                out.append(_row(f"svc:{name}", name, OK,
                                f"{r.get('ok_count', 0)} ok / {r.get('err_count', 0)} failed"))
    return out


def check_failures(series_fn, today_fn) -> dict:
    """A spike in requests that never ran, measured against this app's own
    fortnight — not against a number somebody guessed."""
    try:
        days = series_fn(14) or []
        # v0.66.53: failures_today() is {'total': n, 'top': kind} — summing
        # its values added an int to a string and the check reported itself
        # unreadable on the first morning it ran. Caught by the check's own
        # rule that an unreadable check is "unknown", not "ok" (Nov 9).
        raw = today_fn() or {}
        today = int(raw.get("total") or 0) if "total" in raw else \
            sum(v for v in raw.values() if isinstance(v, (int, float)))
    except Exception as e:
        return _row("failures", "Checks that didn't run", UNKNOWN, f"unreadable: {str(e)[:100]}")
    past = [d.get("total", 0) for d in days[:-1]] or [0]
    avg = sum(past) / max(1, len(past))
    if today > max(6, avg * 3):
        return _row("failures", "Checks that didn't run", FAIL,
                    f"{today} today against a fortnight average of {avg:.1f} — something broke",
                    "open the Checks pane; the rows marked “ours to fix” are the real ones")
    if today > max(4, avg * 2):
        return _row("failures", "Checks that didn't run", WARN,
                    f"{today} today against an average of {avg:.1f}")
    return _row("failures", "Checks that didn't run", OK,
                f"{today} today, in line with the {avg:.1f} average")


def check_harness(qa_summary_fn) -> dict:
    """The consistency harness is the only thing watching for drift. If it
    stops, nothing tells us the scores moved."""
    try:
        s = qa_summary_fn(3) or {}
        days = s.get("days") or []
    except Exception as e:
        return _row("harness", "Nightly consistency run", UNKNOWN, f"unreadable: {str(e)[:100]}")
    try:
        from app import boost
        if not boost.active() and boost.qa_per_day() <= 0:
            return _row("harness", "Nightly consistency run", OK,
                        "off by design — the credit window has ended")
    except Exception:
        pass
    if not days:
        return _row("harness", "Nightly consistency run", WARN, "it has not run in the last 3 days",
                    "press Run now on the Consistency card and watch for an error")
    last = days[-1]
    if (last.get("n") or 0) <= 0:
        return _row("harness", "Nightly consistency run", WARN,
                    f"it ran on {last.get('day')} but re-checked nothing")
    drift = last.get("avg_drift")
    if drift is not None and drift >= 1.0:
        return _row("harness", "Nightly consistency run", WARN,
                    f"{last.get('n')} re-checked, average drift {drift} — the same clip is "
                    "scoring more than a point differently a day later",
                    "open a flip and read what changed underneath; if the claims are the same "
                    "and only the sources moved, the evidence hunt is the variable, not the judges")
    return _row("harness", "Nightly consistency run", OK,
                f"{last.get('n')} re-checked on {last.get('day')}, average drift {drift if drift is not None else '—'}")


def check_secrets() -> dict:
    try:
        from app.apihealth import scan_source
        hits = (scan_source() or {}).get("hits") or []
    except Exception as e:
        return _row("secrets", "Secrets in code", UNKNOWN, f"scan unavailable: {str(e)[:100]}")
    if hits:
        where = ", ".join(f"{h.get('file')}:{h.get('line')}" for h in hits[:4])
        return _row("secrets", "Secrets in code", FAIL,
                    f"{len(hits)} key-shaped string(s) in the deployed code: {where}",
                    "rotate that key now, then remove it from the file")
    return _row("secrets", "Secrets in code", OK, "nothing key-shaped in the deployed code")


def check_detector_bands() -> dict:
    """The bands decide the verdict word, so a typo in a Railway variable
    would silently change every result."""
    try:
        from app.agents import hive_detect
        b = hive_detect.thresholds()
    except Exception as e:
        return _row("bands", "Detection thresholds", UNKNOWN, f"unreadable: {str(e)[:100]}")
    if not (0 < b["weak"] < b["unclear"] < b["likely"] <= 1.0):
        return _row("bands", "Detection thresholds", FAIL,
                    f"the bands are out of order: {b}",
                    "check GLOWBY_AI_THRESH_* in Railway")
    return _row("bands", "Detection thresholds", OK,
                f"leans AI at {b['likely']}, unclear from {b['unclear']}")


# ----------------------------------------------------------------- run
def run(budget_fn, usage_fn, series_fn, today_fn, qa_summary_fn, version: str = "") -> dict:
    checks = []
    for fn in (lambda: check_database(),
               lambda: check_budget(budget_fn, usage_fn),
               lambda: check_failures(series_fn, today_fn),
               lambda: check_harness(qa_summary_fn),
               lambda: check_secrets(),
               lambda: check_detector_bands()):
        try:
            checks.append(fn())
        except Exception as e:  # a check that crashes is itself a finding
            checks.append(_row("crashed", "A check crashed", UNKNOWN, str(e)[:160]))
    try:
        checks.extend(check_services())
    except Exception as e:
        checks.append(_row("services", "APIs & keys", UNKNOWN, str(e)[:160]))

    counts = {s: sum(1 for c in checks if c["state"] == s) for s in (OK, WARN, FAIL, UNKNOWN)}
    verdict = FAIL if counts[FAIL] else (WARN if counts[WARN] else
                                         (UNKNOWN if counts[UNKNOWN] and not counts[OK] else OK))
    headline = {OK: "All clear.",
                WARN: f"{counts[WARN]} thing(s) worth a look.",
                FAIL: f"{counts[FAIL]} thing(s) are broken.",
                UNKNOWN: "Could not tell."}[verdict]
    return {"at": datetime.now(timezone.utc).isoformat(), "version": version,
            "verdict": verdict, "headline": headline, "counts": counts, "checks": checks}


def store(report: dict) -> None:
    try:
        import json
        from app.marketing import set_setting
        set_setting("selfcheck:last", json.dumps(report)[:12000])
    except Exception:
        pass


def last() -> dict | None:
    try:
        import json
        from app.marketing import setting
        raw = setting("selfcheck:last")
        return json.loads(raw) if raw else None
    except Exception:
        return None
