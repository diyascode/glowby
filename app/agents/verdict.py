"""
Verdict agent — the judge. Weighs a claim against its gathered evidence
and issues a ruling.
 
Ratings (fixed vocabulary):
    true | mostly_true | misleading | false | unverifiable
 
Rules (also embedded in the prompt):
- The verdict must rest ONLY on the evidence provided — no outside
  knowledge, no guessing. If the evidence is empty or too thin,
  the verdict is "unverifiable". Refusing to guess is a feature.
- Confidence is 0.0-10.0, one decimal (Glowby's universal scale):
  how strongly the evidence supports THIS rating. Thin-but-consistent
  evidence -> moderate confidence; strong consensus -> high.
- key_sources may only contain URLs that appear in the evidence.
 
Returns:
{
  "rating": "false",
  "confidence": 8.2,
  "summary": one/two-sentence plain-language explanation,
  "key_sources": ["https://...", ...]   # up to 3, from the evidence only
}
"""
 
import json
import os
import re
 
MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
 
VALID_RATINGS = {"true", "mostly_true", "misleading", "false", "unverifiable"}
MAX_KEY_SOURCES = 3
 
 
def _no_evidence(evidence: dict) -> bool:
    if not isinstance(evidence, dict):
        return True
    return not (evidence.get("fact_checks") or evidence.get("web_sources"))
 
 
PROMPT = """You are the verdict agent for Glowby, a fact-checking service. \
Judge the claim below using ONLY the evidence provided. Do not use outside \
knowledge. Do not guess.
 
Rating vocabulary (pick exactly one):
- "true": evidence clearly supports the claim
- "mostly_true": accurate in substance, with minor inaccuracies or missing nuance
- "misleading": contains truth but framed to give a false impression, or mixes \
true and false elements
- "false": evidence clearly contradicts the claim
- "unverifiable": the evidence provided is insufficient, contradictory without \
resolution, or absent — when in doubt, choose this; refusing to guess is correct
 
Confidence: 0.0-10.0, one decimal place — how strongly the evidence supports \
your rating (NOT how true the claim is). Strong multi-source consensus: 8.0+. \
Thin or single-source evidence: below 6.0. If rating is "unverifiable" because \
evidence is absent, confidence reflects certainty that it cannot be verified \
from what was provided.
 
Claim: "{claim}"
 
Evidence — professional fact-checker reviews:
{fact_checks}
 
Evidence — web sources (with stance toward the claim):
{web_sources}
 
Respond with ONLY a JSON object (no prose, no code fences):
{{"rating": "...", "confidence": 0.0, "summary": "1-2 plain sentences a \
teenager would understand, mentioning what the evidence shows", \
"key_sources": ["url1", "url2"]}}
key_sources: up to {max_sources} URLs copied EXACTLY from the evidence above — \
never invent a URL. Empty list if rating is unverifiable with no evidence."""
 
 
def judge_claim(claim: str, evidence: dict) -> dict:
    """Main entry point: claim + evidence bundle in, verdict out."""
    # hard rule: no evidence -> unverifiable, no model call needed
    if _no_evidence(evidence):
        return {
            "rating": "unverifiable",
            "confidence": 9.0,
            "summary": "No professional fact-checks or credible web sources "
            "were found for this claim, so Glowby won't guess.",
            "key_sources": [],
        }
 
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "rating": "unverifiable",
            "confidence": 0.0,
            "summary": "Verdict engine is not configured.",
            "key_sources": [],
        }
 
    import anthropic
 
    evidence_urls = _collect_urls(evidence)
    prompt = PROMPT.format(
        claim=claim[:500],
        fact_checks=_format_fact_checks(evidence),
        web_sources=_format_web_sources(evidence),
        max_sources=MAX_KEY_SOURCES,
    )
 
    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return {
            "rating": "unverifiable",
            "confidence": 0.0,
            "summary": "The verdict engine had a temporary problem; try again.",
            "key_sources": [],
        }
 
    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    verdict = parse_verdict_response(raw, allowed_urls=evidence_urls)
    if verdict is None:
        return {
            "rating": "unverifiable",
            "confidence": 0.0,
            "summary": "The verdict engine returned an unreadable response.",
            "key_sources": [],
        }
    return verdict
 
 
# ------------------------------------------------------------ formatting
 
 
def _format_fact_checks(evidence: dict) -> str:
    rows = evidence.get("fact_checks") or []
    if not rows:
        return "(none found)"
    return "\n".join(
        f'- {r.get("publisher", "?")} rated it "{r.get("rating", "?")}" '
        f'({r.get("review_date", "")}) — {r.get("url", "")}'
        for r in rows
    )
 
 
def _format_web_sources(evidence: dict) -> str:
    rows = evidence.get("web_sources") or []
    if not rows:
        return "(none found)"
    return "\n".join(
        f'- {r.get("source", "?")} [{r.get("stance", "?")}]: '
        f'"{r.get("quote", "")}" — {r.get("url", "")}'
        for r in rows
    )
 
 
def _collect_urls(evidence: dict) -> set:
    urls = set()
    for r in (evidence.get("fact_checks") or []) + (evidence.get("web_sources") or []):
        if r.get("url"):
            urls.add(r["url"])
    return urls
 
 
# ------------------------------------------------------------ parsing
 
 
def clamp_score(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(10.0, score)), 1)
 
 
def parse_verdict_response(raw: str, allowed_urls=None):
    """Parse the model's JSON verdict robustly. None if unreadable."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
 
    rating = str(data.get("rating", "")).lower().strip()
    if rating not in VALID_RATINGS:
        return None
 
    sources = []
    for u in data.get("key_sources") or []:
        u = str(u).strip()
        if not u.startswith("http"):
            continue
        # never let the model invent a citation
        if allowed_urls is not None and u not in allowed_urls:
            continue
        sources.append(u)
        if len(sources) >= MAX_KEY_SOURCES:
            break
 
    return {
        "rating": rating,
        "confidence": clamp_score(data.get("confidence")),
        "summary": str(data.get("summary", "")).strip()[:500],
        "key_sources": sources,
    }
