"""The verdict bands — one source of truth (v0.66.58).

A claims score is a number; a reader is given a word. These are the
cut-offs that turn one into the other, and until now they lived in TWO
tables that had to be kept in step by hand: `BAND` in agents/summary.py
and `STATE_BANDS` in agents/output.py. Both now derive from here.

WHY THEY MOVED. The old edges were 8.0 / 7.5 / 4.0, which made the
bands this wide:

    Inaccurate 4.0 · Mixed 3.5 · Mostly accurate 0.5 · Accurate 2.0

"Mostly accurate" was a half-point sliver — narrower than the night-to-
night drift of the scoring itself — and the scores pile up right on top
of it (7.5 appeared over and over in the nightly flip list). A clip
landing on 7.5 was one tenth of a point from "Mixed". That, not the
judges, is most of why the consistency harness reported ~45% of
re-checks flipping their verdict word.

Two changes follow:

* **Even bands.** Defaults are now 4.0 / 6.5 / 8.5, so the widths are
  4.0 · 2.5 · 2.0 · 1.5 — tighter at the top, where the distinction
  matters more, but no sliver. The bottom edge did NOT move: the
  dangerous-false cap (3.9) sits just under it on purpose, so lowering
  it would turn a high-risk false claim into "Mixed".
* **Stickiness.** On a re-check, a label only changes when the score
  moves CLEARLY past an edge, not by a tenth. `band_of(score, prev=…)`
  keeps the previous word inside a margin of the boundary.

Both are env-tunable, and `recommend_edges()` proposes better ones from
the app's own score distribution rather than from anybody's hunch.
"""
from __future__ import annotations

import os

# low to high. Each entry: (floor, key, lead word)
KEYS = ("false", "partly true", "mostly true", "true")
LEADS = {"false": "Inaccurate", "partly true": "Mixed",
         "mostly true": "Mostly accurate", "true": "Accurate",
         "unverified": "Unverified"}
STATES = {"false": "misleading", "partly true": "mixed",
          "mostly true": "mostly_accurate", "true": "accurate"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def cuts() -> tuple:
    """(mixed_floor, mostly_floor, accurate_floor), always ordered."""
    # The BOTTOM edge stays at 4.0 and is load-bearing: the
    # dangerous-false cap sits just under it, so a high-risk false claim
    # lands in "Inaccurate". Lowering it would quietly turn those into
    # "Mixed" — the cap tests caught exactly that. Only the top two moved.
    a = _f("GLOWBY_BAND_MIXED", 4.0)
    b = _f("GLOWBY_BAND_MOSTLY", 6.5)
    c = _f("GLOWBY_BAND_ACCURATE", 8.5)
    # a typo in one Railway variable must not silently scramble every
    # verdict: sort, and keep them apart by at least a tenth
    a, b, c = sorted((a, b, c))
    b = max(b, a + 0.1)
    c = max(c, b + 0.1)
    return (round(a, 2), round(b, 2), round(c, 2))


# margin either side of an edge inside which a re-check keeps its old word
STICKY_MARGIN = _f("GLOWBY_BAND_STICKY", 0.4)


def table() -> list:
    """[(floor, key)] highest first — the shape the old tables used."""
    a, b, c = cuts()
    return [(c, "true"), (b, "mostly true"), (a, "partly true"), (0.0, "false")]


def widths() -> dict:
    a, b, c = cuts()
    return {"false": round(a, 2), "partly true": round(b - a, 2),
            "mostly true": round(c - b, 2), "true": round(10.0 - c, 2)}


def band_of(score, prev: str = None, margin: float = None):
    """Pure: the band a score falls in.

    `prev` is the band this same check had last time. When the new score
    would cross into a neighbouring band but sits within `margin` of the
    edge between them, the old band stands. A verdict should change
    because Glowby changed its mind, not because a number twitched.
    Stickiness NEVER spans two bands: a real move always lands.
    """
    if score is None:
        return "unverified"
    try:
        s = float(score)
    except Exception:
        return "unverified"
    now = "false"
    for floor, key in table():
        if s >= floor:
            now = key
            break
    if not prev or prev == now or prev not in KEYS:
        return now
    m = STICKY_MARGIN if margin is None else float(margin)
    if m <= 0:
        return now
    i, j = KEYS.index(now), KEYS.index(prev)
    if abs(i - j) != 1:
        return now  # moved further than one band: that is a real change
    edge = table()[len(KEYS) - 1 - max(i, j)][0]
    return prev if abs(s - edge) <= m else now


def lead_of(score, prev: str = None) -> str:
    return LEADS[band_of(score, prev)]


# ------------------------------------------------- edges from the data
def recommend_edges(buckets: list) -> dict:
    """Pure: propose cut-offs that sit where the scores DON'T.

    `buckets` is [{"score": 7.5, "n": 12}, …] from storage.score_histogram.
    For each edge we search a window around the current value for the
    quietest tenth — the valley between two clumps — because an edge in
    a valley is one that few results sit near, and a result that sits
    near an edge is a result that flips.
    """
    counts = {}
    for b in buckets or []:
        try:
            counts[round(float(b["score"]), 1)] = int(b["n"])
        except Exception:
            continue
    total = sum(counts.values())
    cur = cuts()
    if total < 30:  # too little to reason from; say so rather than guess
        return {"enough_data": False, "total": total, "current": cur,
                "proposed": cur, "note": "fewer than 30 scored checks — not enough to place edges"}

    def near(edge, span=0.2):
        lo, hi = edge - span, edge + span
        return sum(n for s, n in counts.items() if lo <= s <= hi)

    def density(edge):
        """How crowded a candidate edge is, counting its immediate
        neighbours too — a lone empty tenth between two spikes is not a
        valley."""
        return sum(counts.get(round(edge + d, 1), 0) for d in (-0.1, 0.0, 0.1))

    windows = [(cur[0], 2.5, 5.0), (cur[1], 5.5, 7.4), (cur[2], 7.8, 9.2)]
    proposed = []
    for current, lo, hi in windows:
        best, best_d = current, density(current)
        step = lo
        while step <= hi + 1e-9:
            d = density(round(step, 1))
            # prefer a quieter spot; on a tie stay closer to where we are
            if d < best_d or (d == best_d and abs(step - current) < abs(best - current)):
                best, best_d = round(step, 1), d
            step = round(step + 0.1, 10)
        proposed.append(best)
    a, b, c = sorted(proposed)
    b = max(b, a + 0.5)
    c = max(c, b + 0.5)
    proposed = (round(a, 2), round(b, 2), round(c, 2))
    return {"enough_data": True, "total": total,
            "current": cur, "proposed": proposed,
            "on_edge_now": sum(near(e) for e in cur),
            "on_edge_proposed": sum(near(e) for e in proposed),
            "widths_now": widths(),
            "env": {"GLOWBY_BAND_MIXED": proposed[0],
                    "GLOWBY_BAND_MOSTLY": proposed[1],
                    "GLOWBY_BAND_ACCURATE": proposed[2]}}
