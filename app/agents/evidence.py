"""
Evidence agent — gathers receipts for a single claim.

Two sources, combined:
1. Google Fact Check Tools API (free): has a professional fact-checker
   (PolitiFact, Reuters, Snopes, ...) already reviewed this claim?
   Degrades gracefully to [] if the API is not enabled or errors.
2. Claude with the web-search tool: finds and quotes current sources,
   each tagged with a stance: supports | refutes | mixed | context.

Returns:
{
  "fact_checks": [{"publisher", "title", "url", "rating", "review_date"}],
  "web_sources": [{"source", "url", "quote", "stance"}],
}
Never raises for a single claim's failure — the pipeline continues with
whatever evidence was found; total failure returns empty lists.
"""

import json
import os
import re
import urllib.parse
import urllib.request

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_WEB_SEARCHES = 3
MAX_WEB_SOURCES = 4
MAX_FACT_CHECKS = 3

VALID_STANCES = {"supports", "refutes", "mixed", "context"}


def gather_evidence(claim: str) -> dict:
    """Main entry point: one claim in, evidence bundle out.

    The two evidence hunts (fact-check database + web search) run in
    parallel — they are independent network calls.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(search_fact_check_db, claim)
        web = ex.submit(search_web_evidence, claim)
        return {"fact_checks": fc.result(), "web_sources": web.result()}


# ------------------------------------------------ Google Fact Check Tools


def search_fact_check_db(claim: str) -> list:
    """Query Google's Fact Check Tools API. Empty list on any failure."""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return []
    params = urllib.parse.urlencode(
        {"query": claim[:250], "key": api_key, "languageCode": "en"}
    )
    url = f"https://factchecktools.googleapis.com/v1alpha1/claims:search?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    return parse_fact_check_response(data)


def parse_fact_check_response(data: dict) -> list:
    """Flatten the API's claims[].claimReview[] into simple dicts."""
    out = []
    if not isinstance(data, dict):
        return out
    for item in data.get("claims", []):
        if not isinstance(item, dict):
            continue
        for review in item.get("claimReview", []) or []:
            if not isinstance(review, dict) or not review.get("url"):
                continue
            out.append(
                {
                    "publisher": (review.get("publisher") or {}).get("name", "")
                    or "(unknown)",
                    "title": review.get("title", "") or item.get("text", "")[:120],
                    "url": review["url"],
                    "rating": review.get("textualRating", "") or "(unrated)",
                    "review_date": (review.get("reviewDate", "") or "")[:10],
                }
            )
            if len(out) >= MAX_FACT_CHECKS:
                return out
    return out


# ------------------------------------------------ Claude web search


PROMPT = """You are the evidence agent for Glowby, a fact-checking service. \
Search the web for evidence about this claim and report what credible \
sources say. Prefer scientific bodies, major news organizations, government \
agencies, and academic sources. Look for evidence AGAINST the claim as well \
as for it — do not stop at the first agreeing source.

Claim: "{claim}"

After searching, respond with ONLY a JSON array (no prose, no code fences) \
of at most {max_sources} sources, each:
{{"source": "publisher name", "url": "https://...", "quote": "short relevant \
quote or finding from that source", "stance": "supports|refutes|mixed|context"}}

"stance" is the source's relationship TO THE CLAIM: supports = source agrees \
claim is true; refutes = source contradicts the claim; mixed = partially \
true; context = background that helps judge it. Use only URLs that appeared \
in your search results. If you find no relevant sources, return []."""


def search_web_evidence(claim: str) -> list:
    """Ask Claude (with web search) for stance-tagged sources. [] on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=2500,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": MAX_WEB_SEARCHES,
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": PROMPT.format(
                        claim=claim[:500], max_sources=MAX_WEB_SOURCES
                    ),
                }
            ],
        )
    except Exception:
        return []

    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    return parse_web_evidence(raw)


def parse_web_evidence(raw: str) -> list:
    """Parse the model's JSON array of sources robustly. [] if unreadable."""
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out = []
    for item in data[:MAX_WEB_SOURCES]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url.startswith("http"):
            continue
        stance = str(item.get("stance", "context")).lower().strip()
        if stance not in VALID_STANCES:
            stance = "context"
        out.append(
            {
                "source": str(item.get("source", "")).strip() or "(unknown)",
                "url": url,
                "quote": str(item.get("quote", "")).strip()[:400],
                "stance": stance,
            }
        )
    return out
