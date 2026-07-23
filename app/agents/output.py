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

SAFETY_LABEL = (
    "⚠ This video contains emergency or safety instructions that could "
    "NOT be confirmed through official channels. Do not act on it; check "
    "official sources."
)

UNSAFE_VERDICT_STATES = {"unverifiable", "insufficient", "contradicted",
                         "not_scoreable", "provisional"}


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

    scores = [
        c["verdict"]["truth_score"]
        for c in judged
        if c["verdict"].get("truth_score") is not None
    ]
    headline = round(min(scores), 1) if scores else None

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
        state, label = "unverified", UNVERIFIED_LABEL
    else:
        state, label = "misleading", STATE_BANDS[-1][2]
        for cutoff, s, text in STATE_BANDS:
            if headline >= cutoff:
                state, label = s, text
                break

    title = (result.get("title") or "this video").strip()
    if headline is None:
        share_text = f"Glowby checked “{title}”: unverified — no reliable evidence found."
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
