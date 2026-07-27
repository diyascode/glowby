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

NO_CLAIMS_LABEL = (
    "Glowby found no checkable factual claims in this video — "
    "nothing to verify."
)

ALL_PARKED_LABEL = (
    "This video contains only opinion, satire, or other non-factual "
    "content — nothing here can be true or false, so there is nothing "
    "to fact-check."
)

SAFETY_LABEL = (
    "⚠ This video contains emergency or safety instructions that could "
    "NOT be confirmed through official channels. Do not act on it; check "
    "official sources."
)

UNSAFE_VERDICT_STATES = {"unverifiable", "insufficient", "contradicted",
                         "not_scoreable", "provisional"}

# a low-risk side detail can cap the headline down to this floor, but
# never below it — "mostly checks out" is the worst a wrong aside can do
SIDE_DETAIL_FLOOR = 7.5


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
        # headline alone; a questionable side enters the MIN clamped up
        # to the floor — it caps, never craters
        effective = [c["verdict"]["truth_score"] for c in counting] + [
            max(c["verdict"]["truth_score"], SIDE_DETAIL_FLOOR)
            for c in scored
            if c not in counting and c["verdict"]["truth_score"] < 8.0
        ]
    else:  # nothing central was scorable — every side claim counts fully
        effective = [c["verdict"]["truth_score"] for c in scored]
    headline = round(min(effective), 1) if effective else None
    # disclosure: a side claim scored below the headline (raw), i.e. it
    # was softened by the floor or simply sits under the main claims
    side_lower = headline is not None and any(
        c["verdict"]["truth_score"] < headline
        for c in scored if c not in counting
    )
    side_capped = (
        headline is not None and counting
        and headline < round(min(
            c["verdict"]["truth_score"] for c in counting), 1)
    )

    # safety collapse (spec: named critical protocol)
    safety_notice = None
    for c in judged:
        if c.get("public_safety_risk") and (
            c["verdict"].get("verdict_state") in UNSAFE_VERDICT_STATES
        ):
            safety_notice = SAFETY_LABEL
            break

    if safety_notice:
        state, label = "safety_alert", SAFETY_LABEL
    elif headline is None:
        # say the TRUE reason there's no score: no claims at all, only
        # non-factual content, or real claims that couldn't be verified
        if not claims:
            state, label = "unverified", NO_CLAIMS_LABEL
        elif not forward:
            state, label = "unverified", ALL_PARKED_LABEL
        else:
            state, label = "unverified", UNVERIFIED_LABEL
    else:
        state, label = "misleading", STATE_BANDS[-1][2]
        for cutoff, s, text in STATE_BANDS:
            if headline >= cutoff:
                state, label = s, text
                break
        if side_capped:
            label += (" The score is capped because a side detail didn't "
                      "fully check out - see below.")
        elif side_lower:
            label += " A side detail scored lower - see below."

    title = (result.get("title") or "this video").strip()
    if headline is None:
        tail = ("no checkable claims found" if not forward
                else "unverified — no reliable evidence found")
        share_text = f"Glowby checked “{title}”: {tail}."
    else:
        share_text = (
            f"Glowby checked “{title}”: {headline}/10 — {state.replace('_', ' ')}."
        )

    result["report"] = {
        "headline_score": headline,
        "headline_state": state,
        "headline_label": label,
        "share_text": share_text,
        "counts": {
            "claim_units": len(claims),
            "judged": len(judged),
            "not_judged": len(not_judged),
            "parked": len(parked),
        },
        "safety_notice": safety_notice,
    }
    return result
