"""
Sorting Gate & Router — implements Glowby_Sorting_Gate_Router_Spec v1.2.

Position: Ingest (transcript) -> THIS STAGE -> evidence + category judges.

Does two things per the spec:
1. THE GATE: decides whether each statement is a real, checkable
   assertion. Labels: factual | prediction | opinion | satire |
   no-claim | personal-experience | question | fiction-joke |
   advertisement. Only factual and prediction move forward; everything
   else is tagged and parked so it never gets "debunked."
2. THE ROUTER: splits the transcript into claim units (SEMANTIC UNITY
   RULE: conditionals, comparative dependencies, and causal claims stay
   ONE unit) and files each into one of the 13 topic buckets, with
   optional secondary bucket (dual-tag / straddle), routing confidence,
   risk level, developing-story flag, and public_safety_risk cross-tag.

Every routed claim gets an audit record saved to Postgres (spec §3.10).

The cheap-signals shortcut (URL slugs, section names) is skipped in v1:
video transcripts carry none of those signals, so routing is AI-driven
with the bucket cards. Known-satire list still runs first (cheap).
"""

import json
import os
import re

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
TAXONOMY_VERSION = "glowby-13buckets-v1"
MAX_CLAIMS = 5
CONFIDENCE_REVIEW_THRESHOLD = 0.70  # spec §8 knob

GATE_LABELS = {
    "factual", "prediction", "opinion", "satire", "no-claim",
    "personal-experience", "question", "fiction-joke", "advertisement",
}
FORWARD_LABELS = {"factual", "prediction"}  # only these get verified

BUCKETS = {
    "politics", "health", "science", "economy", "business", "technology",
    "law", "conflict", "education", "society_culture", "sports",
    "entertainment", "history_geography", "other",
}
RISK_LEVELS = {"low", "medium", "high", "critical"}
RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# spec §2.1 — known-satire list (living list; grow it)
KNOWN_SATIRE_SOURCES = {
    "the onion", "onion", "babylon bee", "the babylon bee", "clickhole",
    "the daily mash", "waterford whispers", "the beaverton", "newsthump",
    "the borowitz report", "duffel blog", "reductress", "hard drive",
    "the hard times", "betoota advocate",
}


class RouterError(Exception):
    """Raised when routing fails entirely. Message is user-facing."""


PROMPT = """You are the Sorting Gate & Router for Glowby, a fact-checking \
service. Process this social-media video transcript in three steps.

STEP 1 — SPLIT into claim units. Extract up to {max_claims} distinct \
assertions worth examining (most consequential first; health/safety first). \
SEMANTIC UNITY RULE: conditional statements (if X then Y), comparative \
dependencies, and causal claims ("X caused Y") are ONE claim unit — never \
split a linkage into fragments; route by the core action clause or dual-tag.

STEP 2 — GATE each unit with exactly one label:
- "factual": a checkable assertion about the world → moves forward
- "prediction": future outcome. Scheduled/dated events ("the law takes \
effect in 2028") and model-backed projections (IPCC/CBO-class) count; \
unanchored speculation with no data basis ("immortality by 2090") does NOT \
— label it "no-claim" instead. Predictions move forward.
- "opinion": value judgment ("best mayor we've had") → parked
- "satire": parody/comedy, even without source (absurd framing, parody \
markers, comedic context) → parked. NEVER auto-"debunk" satire.
- "no-claim": recipes, greetings, vibes, unscoreable speculation → parked
- "personal-experience": "my flight was cancelled" — unverifiable as \
stated → parked. BUT a personal story asserting a general fact ("lemon \
water cured my kidneys — it detoxes you") contains a FACTUAL claim: extract \
the general claim.
- "question" / "fiction-joke" / "advertisement" → parked

STEP 3 — ROUTE each forward claim into ONE primary bucket (what the claim \
is MAINLY about, not which words appear):
- politics: elections, laws, policies, officials, how governments run. \
Health-policy/funding fights → politics; the disease/treatment itself → health.
- health: diseases, treatments, drugs, public health, medical research.
- science: research, space, climate, environment, energy. Climate POLICY \
fight → politics; a medical study → health.
- economy: the broad economy, markets, rates, inflation, jobs, central banks.
- business: specific companies, products, deals, executives. Economy-wide \
trends → economy; what a technology does → technology.
- technology: software, hardware, AI, internet, cybersecurity — the tech \
itself. The company's business move → business.
- law: crimes, arrests, trials, courts, lawsuits, policing. The ruling and \
legal standard → law; the political fight about it → politics.
- conflict: wars, military, terrorism, national security. Peace \
talks/diplomacy lean politics.
- education: schools, universities, students, tuition, curricula, \
admissions. Research findings → science/health; education POLITICS → politics.
- society_culture: social issues, religion, lifestyle, identity, \
traditions. Use a clearer bucket when one fits.
- sports: games, athletes, teams, scores. Athlete injury: game impact → \
sports; medical causation → health.
- entertainment: movies, TV, music, celebrities, media. A studio's \
business deal → business.
- history_geography: historical events, historical figures, geography \
facts, "first/oldest/largest ever" heritage claims.
- other: a real claim with no good home.

Dual-tag when a claim genuinely touches two buckets (one fact, two \
lenses): set secondary_bucket. A tear between two buckets IS the answer.

Also per claim:
- confidence: 0.0-1.0 that the primary bucket is right
- risk_level: "low" | "medium" | "high" | "critical". Elevate for: medical \
instruction, election mechanics, emergency instruction, named-person \
accusation, financial urgency. A false movie date is low; a false insulin \
dosage is critical.
- public_safety_risk: true for evacuation orders, shelter/boil-water/ \
all-clear instructions, disaster warnings, missing-person alerts — claims \
people act on within minutes.
- developing_story: true for elections, wars, court rulings, market moves, \
disasters, deaths, arrests — facts that can shift within hours.
- reason: one line on why this bucket.

Video title: {title}
Platform: {platform}
Uploader: {uploader}

Transcript:
\"\"\"{transcript}\"\"\"

Respond with ONLY a JSON array (no prose, no code fences), one element per \
claim unit:
{{"claim": "...", "quote": "verbatim-ish words from transcript", \
"gate_label": "...", "bucket": "...", "secondary_bucket": null, \
"confidence": 0.0, "risk_level": "...", "public_safety_risk": false, \
"developing_story": false, "reason": "..."}}
For parked units (opinion/satire/etc.) still include the element with its \
gate_label; bucket may be "other". If the transcript contains nothing worth \
examining, return []."""


def route_claims(transcript: str, title: str = "", platform: str = "",
                 uploader: str = "") -> list:
    """Gate + split + route a transcript. Returns list of claim dicts."""
    if not transcript or not transcript.strip():
        raise RouterError("No transcript text to analyze.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RouterError("The router is not configured (missing ANTHROPIC_API_KEY).")

    # spec §2.1 — known-satire source check, cheapest catch, before any AI
    if _is_known_satire(uploader) or _is_known_satire(title):
        return [{
            "claim": "(entire item)",
            "quote": title or "(untitled)",
            "gate_label": "satire",
            "bucket": "other",
            "secondary_bucket": None,
            "confidence": 1.0,
            "risk_level": "low",
            "public_safety_risk": False,
            "developing_story": False,
            "reason": "Source matches the known-satire list.",
            "human_review_required": False,
        }]

    import anthropic

    prompt = PROMPT.format(
        max_claims=MAX_CLAIMS,
        title=title or "(unknown)",
        platform=platform or "(unknown)",
        uploader=uploader or "(unknown)",
        transcript=transcript[:15000],
    )
    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RouterError(f"The router failed to run. (Details: {str(e)[:200]})")

    raw = "".join(
        b.text for b in message.content if getattr(b, "type", "") == "text"
    )
    claims = parse_router_response(raw)
    if claims is None:
        raise RouterError("The router returned an unreadable response.")
    return claims


def _is_known_satire(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(s in t for s in KNOWN_SATIRE_SOURCES) if t else False


# ------------------------------------------------------------ selection


def select_for_verification(claims: list, limit: int = 3) -> list:
    """Indexes of forward claims to verify: risk first, then confidence."""
    forward = [
        i for i, c in enumerate(claims) if c.get("gate_label") in FORWARD_LABELS
    ]
    forward.sort(
        key=lambda i: (
            RISK_ORDER.get(claims[i].get("risk_level", "low"), 3),
            -float(claims[i].get("confidence", 0.0)),
        )
    )
    return forward[:limit]


# ------------------------------------------------------------ parsing


def parse_router_response(raw: str):
    """Parse and validate the router's JSON array. None if unreadable."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    out = []
    for item in data[:MAX_CLAIMS]:
        if not isinstance(item, dict) or not item.get("claim"):
            continue
        gate = str(item.get("gate_label", "no-claim")).lower().strip()
        if gate not in GATE_LABELS:
            gate = "no-claim"
        bucket = str(item.get("bucket", "other")).lower().strip().replace("-", "_")
        if bucket not in BUCKETS:
            bucket = "other"
        secondary = item.get("secondary_bucket")
        if secondary is not None:
            secondary = str(secondary).lower().strip().replace("-", "_")
            if secondary not in BUCKETS or secondary == bucket:
                secondary = None
        risk = str(item.get("risk_level", "low")).lower().strip()
        if risk not in RISK_LEVELS:
            risk = "low"
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        claim = {
            "claim": str(item["claim"]).strip(),
            "quote": str(item.get("quote", "")).strip(),
            "gate_label": gate,
            "bucket": bucket,
            "secondary_bucket": secondary,
            "confidence": round(conf, 2),
            "risk_level": risk,
            "public_safety_risk": bool(item.get("public_safety_risk", False)),
            "developing_story": bool(item.get("developing_story", False)),
            "reason": str(item.get("reason", "")).strip(),
        }
        # spec §6 — router-level review triggers
        claim["human_review_required"] = (
            (conf < CONFIDENCE_REVIEW_THRESHOLD and secondary is None)
            or risk == "critical"
        )
        out.append(claim)
    return out
