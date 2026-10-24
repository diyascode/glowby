"""The credit window (v0.66.37). Anthropic granted $200 of promotional
credit on Oct 21, 2026 that expires Nov 17, 2026; until then the app
spends more on the things it usually rations, and on Nov 18 everything
snaps back to normal by itself — nothing to remember to turn off.

While active (today <= GLOWBY_BOOST_UNTIL, Pacific):
  * the daily budget is GLOWBY_BOOST_BUDGET_USD (50) instead of the normal 30
  * the trending crawler pre-checks GLOWBY_BOOST_PRECHECK_PER_RUN (6) per run instead of 3
  * the consistency harness re-checks GLOWBY_QA_DAILY (30) stored results a night

Set GLOWBY_BOOST_UNTIL to an earlier date to end it sooner.
"""
from __future__ import annotations

import os
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _PT = None

UNTIL = os.environ.get("GLOWBY_BOOST_UNTIL", "2026-11-17").strip()


def today() -> date:
    return datetime.now(_PT).date() if _PT else date.today()


def active(now: date | None = None) -> bool:
    try:
        return (now or today()) <= date.fromisoformat(UNTIL)
    except Exception:
        return False


def daily_budget(base: float) -> float:
    return float(os.environ.get("GLOWBY_BOOST_BUDGET_USD", "50")) if active() else base


def precheck_per_run(base: int) -> int:
    return int(os.environ.get("GLOWBY_BOOST_PRECHECK_PER_RUN", "6")) if active() else base


def qa_per_day() -> int:
    return int(os.environ.get("GLOWBY_QA_DAILY", "30")) if active() else int(os.environ.get("GLOWBY_QA_DAILY_AFTER", "0"))


def status() -> dict:
    return {"active": active(), "until": UNTIL, "budget_usd": daily_budget(0.0) if active() else None,
            "precheck_per_run": precheck_per_run(0) if active() else None, "qa_per_day": qa_per_day()}
