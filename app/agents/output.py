"""
Output agent — the newsroom editor. Implements Glowby_Output_Agent_Spec
rules as deterministic code (no AI call — assembly rules are law, not
judgment, so they are enforced in code where they cannot drift).

Responsibilities:
- HEADLINE SCORE = the MINIMUM truth score across judged claims.
  One true claim can never launder a false video. Unscored (null)
  verdicts don't enter the MIN, but if NOTHING was scorable the
  headline is null ("unverified"), never a number.
- Headline state + plain-language label from the score bands.
- SAFETY COLLAPSE: if any claim is public_safety_risk and its verdict
  did not confirm it via official channels, the whole report carries a
  safety notice and the headline collapses to the safety language —
  a fake evacuation order must never read as "6.2, mixed".
- Verdict-language hygiene: strips banned intensifiers from judge
  sentences (obviously, clearly, definitely, undeniably, 100%) — the
  evidence speaks, not adjectives.
- Counts + share text for the UI and share cards.

Adds result["report"]:
{
  "headline_score": 2.0 | None,
  "headline_state": "accurate"|"mostly_accurate"|"mixed"|"misleading"|
                    "unverified"|"safety_alert",
  "headline_label": human sentence,
  "share_text": one-liner for share cards,
  "counts": {"claim_units", "judged", "not_judged", "parked"},
  "safety_notice": str | None,
}
"""

import os
import re

BANNED_INTENSIFIERS = re.compile(
    r"\b(obviously|clearly|definitely|undeniably|absolutely|100%|"
    r"without a doubt|certainly)\b\s*",
    re.IGNORECASE,
)

FORWARD_LABELS = {"factual", "prediction"}

STATE_BANDS = [
    (8.0, "accurate", "The judged claims in this video check out."),
    (7.5, "mostly_accurate", "The judged claims mostly check out."),
    (4.0, "mixed", "This video mixes accurate and questionable claims."),
    (0.0, "misleading", "This video contains claims contradicted by evidence."),
]

UNVERIFIED_LABEL = (
    "Glowby could not verify the claims in this video — no reliable "
    "evidence was found either way."
)

NO_CLAIMS_LABEL = "Nothing to fact-check — no claims in this video."

ALL_PARKED_LABEL = (
    "Nothing to fact-check — nothing here can be true or false."
)

# NOTHING-TO-CHECK KINDS (founder design, Sep 2026): when every unit was
# parked at the gate, the card shows ONE word for why — a verdict chip,
# not an empty dial and a paragraph. The dominant parked label picks it.
NOTHING_KINDS = {
    "opinion": "Opinion",
    "satire": "Satire",
    "fiction-joke": "Joke",
    "personal-experience": "Personal story",
    "question": "Question",
    "advertisement": "Ad",
    "no-claim": "No claims",
}


def nothing_kind(claims) -> str:
    """Pure: the dominant parked gate label (ties → first seen), or
    "no-claim" when the router returned nothing at all."""
    tally = {}
    for c in claims or []:
        g = str(c.get("gate_label") or "no-claim")
        if g in NOTHING_KINDS:
            tally[g] = tally.get(g, 0) + 1
    if not tally:
        return "no-claim"
    return max(tally, key=lambda g: tally[g])

SAFETY_LABEL = (
    "⚠ This video contains emergency or safety instructions that could "
    "NOT be confirmed through official channels. Do not act on it; check "
    "official sources."
)

# states in which an UNCONFIRMED safety instruction must collapse the
# report. "not_scoreable" is deliberately absent: a definitional shrug on
# some other claim is not an emergency (the waterfall video, Sept 2026).
UNSAFE_VERDICT_STATES = {"unverifiable", "insufficient", "contradicted",
                         "provisional"}
# the collapse is for INSTRUCTIONS people might act on within minutes —
# evacuate, shelter, boil water, drink/take this, it's safe to go back —
# never for a depiction ("a person slid down a waterfall") that merely
# happens to be dangerous
_INSTRUCTION_RE = re.compile(
    r"\b(evacuat\w*|shelter|boil[- ]water|all[- ]clear|do not (drink|eat|go|use|enter|return)|"
    r"don't (drink|eat|go|use|enter|return)|(is|are) safe to|(is|are) not safe|avoid (the|all|any)|"
    r"stay (indoors|inside|away|home)|leave (the area|immediately|now)|(drink|take|swallow|inject|apply) \w+ (to|for) (cure|treat|prevent)|"
    r"cures?|treats?|prevents?|antidote|warning:|alert:|emergency|missing (person|child)|amber alert|"
    r"you (should|must|need to|have to)|everyone (should|must|needs to))\b", re.I)


def is_safety_instruction(claim_text: str) -> bool:
    """Pure (unit-tested): does this claim read as an instruction or
    warning a person might act on, rather than a description?"""
    return bool(_INSTRUCTION_RE.search(claim_text or ""))

# HEADLINE RULE (Diya, Sept 15: "lots of scores are too harsh — the MIN
# rule is ineffective"). "blend" (default): the headline is the weighted
# mean of the counting claims, false claims (< 4.0) weighing double, with
# three caps — any false central claim caps the video at FALSE_CAP (it
# can never be green), a false HIGH/CRITICAL-risk claim caps it at
# DANGEROUS_FALSE_CAP (misleading), and any non-green central claim caps
# it at NOT_PERFECT_CAP (never "accurate" with a questionable claim in
# it). A majority-false video still lands in misleading; one wrong claim
# among true ones lands in mixed instead of cratering. "min" restores
# the old lowest-claim rule.
HEADLINE_RULE = os.environ.get("GLOWBY_HEADLINE_RULE", "blend").strip().lower()
FALSE_CAP = 5.9
DANGEROUS_FALSE_CAP = 3.9
NOT_PERFECT_CAP = 7.9
FALSE_BAND = 4.0

# a low-risk side detail can cap the headline down to this floor, but
# never below it — "mostly checks out" is the worst a wrong aside can do
SIDE_DETAIL_FLOOR = 7.5
# PROVISIONAL IS NOT QUESTIONABLE: a claim that is credibly reported and
# disputed by nobody — merely not yet independently confirmed — keeps its
# own card score, but cannot drag the HEADLINE below this floor. "Mixes
# accurate and questionable claims" was being printed over videos where
# nothing was questionable (the iPhone 18 Pro aperture, Sept 2026).
PROVISIONAL_FLOOR = 6.0


def blend_score(vals, claims) -> float:
    """Pure (unit-tested): the blended headline for the counting claims.
    vals are the per-claim headline weights (provisional floor applied);
    claims are the matching claim dicts, for risk levels."""
    if not vals:
        return 0.0
    num = den = 0.0
    for v in vals:
        w = 2.0 if v < FALSE_BAND else 1.0   # a false claim weighs double
        num += v * w
        den += w
    score = num / den
    if any(v < FALSE_BAND for v in vals):
        dangerous = any(
            v < FALSE_BAND and (c.get("risk_level") in ("high", "critical"))
            for v, c in zip(vals, claims))
        score = min(score, DANGEROUS_FALSE_CAP if dangerous else FALSE_CAP)
    if any(v < 7.5 for v in vals):
        score = min(score, NOT_PERFECT_CAP)
    return max(0.0, min(9.9, score))


def _headline_weight(c):
    """The score a claim contributes to the headline MIN."""
    v = c["verdict"]
    score = v["truth_score"]
    if v.get("verdict_state") == "provisional" and not _has_disputing_source(c):
        return max(score, PROVISIONAL_FLOOR)
    return score


def _has_disputing_source(c):
    ev = c.get("evidence") or {}
    for w in ev.get("web_sources") or []:
        if (w or {}).get("stance") in ("refutes", "mixed"):
            return True
    return bool(ev.get("fact_checks"))


def build_report(result: dict) -> dict:
    """Attach the assembled report to a pipeline result. Returns result."""
    claims = result.get("claims") or []

    judged = [c for c in claims if c.get("verdict")]
    forward = [c for c in claims if c.get("gate_label") in FORWARD_LABELS]
    parked = [c for c in claims if c.get("gate_label") not in FORWARD_LABELS]
    not_judged = [c for c in forward if not c.get("verdict")]

    # verdict-language hygiene (spec: intensifiers never ship)
    for c in judged:
        v = c["verdict"]
        if v.get("verdict"):
            v["verdict"] = BANNED_INTENSIFIERS.sub("", v["verdict"]).strip()

    # CENTRALITY-GATED MIN WITH SIDE-DETAIL CAP: main-point claims (plus
    # any high/critical-risk side claim — the anti-smuggling backstop)
    # count at FULL weight. A harmless side detail can't drag the video
    # into "mixed/misleading" territory... but it DOES cap the headline
    # at SIDE_DETAIL_FLOOR ("mostly checks out"): a video with a wrong
    # side detail can be green, never near-perfect.
    scored = [c for c in judged if c["verdict"].get("truth_score") is not None]
    counting = [
        c for c in scored
        if c.get("central", True) or c.get("risk_level") in ("high", "critical")
    ]
    if counting:
        # sides that fully check out (accurate band, >= 8.0) leave the
        # headline alone; a questionable side enters clamped up to the
        # floor — it caps, never craters
        side_vals = [
            max(c["verdict"]["truth_score"], SIDE_DETAIL_FLOOR)
            for c in scored
            if c not in counting and c["verdict"]["truth_score"] < 8.0
        ]
        main_vals = [_headline_weight(c) for c in counting]
    else:  # nothing central was scorable — every side claim counts fully
        side_vals = []
        main_vals = [_headline_weight(c) for c in scored]
    if not main_vals:
        headline = None
        uncapped = None
    elif HEADLINE_RULE == "min":
        uncapped = round(min(main_vals), 1)
        headline = round(min(main_vals + side_vals), 1)
    else:
        uncapped = round(blend_score(main_vals, counting or scored), 1)
        headline = uncapped
        if side_vals:
            headline = round(min(headline, min(side_vals)), 1)
    # disclosure: a side claim scored below the headline (raw), i.e. it
    # was softened by the floor or simply sits under the main claims
    side_lower = headline is not None and any(
        c["verdict"]["truth_score"] < headline
        for c in scored if c not in counting
    )
    side_capped = (
        headline is not None and uncapped is not None and headline < uncapped
    )

    # safety collapse (spec: named critical protocol)
    safety_notice = None
    for c in judged:
        if (c.get("public_safety_risk")
                and c["verdict"].get("verdict_state") in UNSAFE_VERDICT_STATES
                and is_safety_instruction(c.get("claim", ""))):
            safety_notice = SAFETY_LABEL
            break

    nothing = None  # set when there was nothing checkable at all
    if safety_notice:
        state, label = "safety_alert", SAFETY_LABEL
        headline = None  # a green number over a red warning is a contradiction
    elif headline is None:
        # say the TRUE reason there's no score: no claims at all, only
        # non-factual content, or real claims that couldn't be verified
        if not claims:
            state, label = "unverified", NO_CLAIMS_LABEL
            nothing = "no-claim"
        elif not forward:
            state, label = "unverified", ALL_PARKED_LABEL
            nothing = nothing_kind(parked)
        else:
            state, label = "unverified", UNVERIFIED_LABEL
    else:
        state, label = "misleading", STATE_BANDS[-1][2]
        for cutoff, s, text in STATE_BANDS:
            if headline >= cutoff:
                state, label = s, text
                break
        # CONTESTED-DRIVER LABEL: when the score-setting claim is merely
        # CONTESTED (partly_supported — experts genuinely disagree) and
        # every other counting claim verified green, "mixes accurate and
        # questionable claims" smears the whole video. Same honest number,
        # truthful sentence: nothing here is false.
        low_w = min((_headline_weight(c) for c in counting), default=None)
        if state in ("mixed", "mostly_accurate") and counting:
            drivers = [c for c in counting
                       if _headline_weight(c) == low_w]
            others = [c for c in counting if c not in drivers]
            if (drivers and others
                    and all(c["verdict"].get("verdict_state")
                            == "partly_supported" for c in drivers)
                    and all(c["verdict"]["truth_score"] >= 8.0
                            for c in others)):
                # "disputed by experts" is only true when the driver's
                # evidence actually holds both sides; a partly-supported
                # claim with no disputing source is merely partly confirmed
                # (the oil-at-$100 nitpick, Sept 2026)
                if all(_has_disputing_source(c) for c in drivers):
                    label = ("The main claims check out. One claim is genuinely "
                             "disputed by experts — and that pulls the score "
                             "down.")
                else:
                    label = ("The main claims check out. One claim is only "
                             "partly confirmed — and that pulls the score "
                             "down.")
        if counting and state in ("mixed", "mostly", "mostly_accurate"):
            drivers = [c for c in counting if _headline_weight(c) == low_w]
            if drivers and all(c["verdict"].get("verdict_state") == "provisional"
                               and not _has_disputing_source(c) for c in drivers):
                label = ("The main claims check out. One claim is credibly "
                         "reported but not yet independently confirmed — "
                         "nothing here is disputed.")
        if side_capped:
            label += (" The score is capped because a side detail didn't "
                      "fully check out - see below.")
        elif side_lower:
            label += " A side detail scored lower - see below."

    title = (result.get("title") or "this video").strip()
    if headline is None:
        tail = (("nothing to fact-check (" + NOTHING_KINDS[nothing].lower() + ")")
                if nothing else "unverified — no reliable evidence found")
        share_text = f"Glowby checked “{title}”: {tail}."
    else:
        share_text = (
            f"Glowby checked “{title}”: {headline}/10 — {state.replace('_', ' ')}."
        )

    result["report"] = {
        "headline_score": headline,
        "headline_state": state,
        "headline_label": label,
        "nothing_to_check": nothing,
        "share_text": share_text,
        "counts": {
            "claim_units": len(claims),
            "judged": len(judged),
            "scored": len(scored),  # verdicts WITH a number (not-scoreable excluded)
            "not_judged": len(not_judged),
            "parked": len(parked),
        },
        "safety_notice": safety_notice,
    }
    return result
