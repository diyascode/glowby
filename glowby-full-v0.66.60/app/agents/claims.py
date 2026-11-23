"""
Claim extraction agent — reads a transcript, returns the checkable claims.

Uses Claude to identify 1–5 CHECKABLE factual claims, skipping opinion,
prediction, and satire.

Rubric (also embedded in the prompt):
- CHECKABLE: a statement about the world that could be verified true or
  false with evidence. ("The vaccine contains microchips.")
- NOT checkable: opinion ("this is stupid"), prediction ("X will win next
  year"), pure value judgment, obvious satire/jokes, personal experience
  that can't be verified ("this worked for me").

Every claim gets a **checkability score, 0.0–10.0 decimal** (product
decision: all Glowby agent scores are 0.0–10.0, one decimal place):
how cleanly this statement can be verified with public evidence.

Returns a list of dicts:
{
  "claim":        concise restatement of the factual claim,
  "quote":        the (near-)verbatim words from the transcript,
  "category":     "health" | "science" | "politics" | "history" |
                  "statistics" | "person" | "product" | "other",
  "checkability": 8.2,          # 0.0-10.0, one decimal
  "why_checkable": one-line reason
}
Raises ClaimExtractionError with a human-readable message on failure.
"""

import json
import os
import re

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_TRANSCRIPT_CHARS = 15_000
MAX_CLAIMS = 5


class ClaimExtractionError(Exception):
    """Raised when claim extraction fails. Message is user-facing."""


PROMPT = """You are the claim-extraction agent for Glowby, a fact-checking \
service. Read the transcript of a social-media video and extract the factual \
claims worth fact-checking.

RUBRIC — a claim is CHECKABLE only if it is a statement about the world that \
could be verified true or false with public evidence.
Extract: factual assertions about health, science, history, statistics, \
people, events, products, laws.
Skip: opinions ("this is stupid"), predictions ("X will win next year"), \
value judgments, jokes/satire, vague vibes ("they don't want you to know"), \
and unverifiable personal anecdotes ("this worked for me") — though a \
personal anecdote used to assert a general fact ("lemon water cured my \
kidney disease, it detoxes your kidneys") DOES contain a checkable general \
claim (that lemon water treats kidney disease).

Extract at most {max_claims} claims — the most consequential ones (health \
and safety first). If the transcript contains no checkable claims, return an \
empty list.

Score each claim's CHECKABILITY from 0.0 to 10.0 (one decimal place): how \
cleanly it can be verified with public evidence. 9.0+ = precise, well-studied \
("vitamin C prevents scurvy"); 5.0 = checkable but fuzzy wording; below 3.0 \
= barely checkable.

Video title: {title}
Platform: {platform}

Transcript:
\"\"\"{transcript}\"\"\"

Respond with ONLY a JSON array (no prose, no code fences), each element:
{{"claim": "...", "quote": "...", "category": "health|science|politics|history|statistics|person|product|other", "checkability": 0.0, "why_checkable": "..."}}"""


def extract_claims(transcript: str, title: str = "", platform: str = "") -> list:
    """Main entry point: transcript in, list of claim dicts out."""
    if not transcript or not transcript.strip():
        raise ClaimExtractionError("No transcript text to analyze.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClaimExtractionError(
            "Claim extraction is not configured (missing ANTHROPIC_API_KEY)."
        )

    import anthropic

    prompt = PROMPT.format(
        max_claims=MAX_CLAIMS,
        title=title or "(unknown)",
        platform=platform or "(unknown)",
        transcript=transcript[:MAX_TRANSCRIPT_CHARS],
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise ClaimExtractionError(
            f"The claim-extraction agent failed to run. (Details: {str(e)[:200]})"
        )

    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    claims = parse_claims_response(raw)
    if claims is None:
        raise ClaimExtractionError(
            "The claim-extraction agent returned an unreadable response."
        )
    return claims


# ------------------------------------------------------------ parsing


VALID_CATEGORIES = {
    "health", "science", "politics", "history",
    "statistics", "person", "product", "other",
}


def parse_claims_response(raw: str):
    """Parse the model's JSON array robustly. Returns list, or None if unreadable."""
    if raw is None:
        return None
    text = raw.strip()
    # strip accidental code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # isolate the outermost JSON array if there is surrounding prose
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

    claims = []
    for item in data[:MAX_CLAIMS]:
        if not isinstance(item, dict) or not item.get("claim"):
            continue
        category = str(item.get("category", "other")).lower().strip()
        if category not in VALID_CATEGORIES:
            category = "other"
        claims.append(
            {
                "claim": str(item["claim"]).strip(),
                "quote": str(item.get("quote", "")).strip(),
                "category": category,
                "checkability": clamp_score(item.get("checkability")),
                "why_checkable": str(item.get("why_checkable", "")).strip(),
            }
        )
    return claims


def clamp_score(value) -> float:
    """Normalize any score to Glowby's 0.0-10.0 one-decimal scale."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    score = max(0.0, min(10.0, score))
    return round(score, 1)
