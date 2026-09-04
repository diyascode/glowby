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

# The evidence agent SEARCHES AND QUOTES — it does not judge. Set
# GLOWBY_EVIDENCE_MODEL=claude-haiku-4-5 (Railway Variables) to run the
# hunt on the fast model: ~3-8s faster per claim. The judges stay on
# the main model and re-read every quote, so verdict quality is theirs.
# Keep the fast setting only if the golden set scores identically.
MODEL = (os.environ.get("GLOWBY_EVIDENCE_MODEL")
         or os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5"))
# round one is LEAN (easy claims finish fast); the deep second round —
# which now also fires on weak evidence, not just empty — carries the
# hard claims. Speed for the easy 80%, MORE scrutiny for the hard 20%.
MAX_WEB_SEARCHES = 2
MAX_WEB_SOURCES = 4
MAX_FACT_CHECKS = 3

VALID_STANCES = {"supports", "refutes", "mixed", "context"}


def gather_evidence(claim: str) -> dict:
    """Main entry point: one claim in, evidence bundle out.

    The two evidence hunts (fact-check database + web search) run in
    parallel. A TECHNICAL failure of the web search is reported as
    search_failed=True — never disguised as "no sources found."
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(search_fact_check_db, claim)
        web = ex.submit(search_web_evidence, claim)
        web_result = web.result()
    if web_result is None:  # technical failure after retries
        return {"fact_checks": fc.result(), "web_sources": [],
                "search_failed": True, "search_rounds": 1}
    fact_checks = fc.result()
    # STRONG-ENOUGH TEST: real evidence = a professional fact-check, or
    # at least one web source that actually supports/refutes/mixes.
    # Background-only ("context") findings are NOT enough to judge on.
    stances = {w.get("stance") for w in (web_result or [])}
    strong_enough = bool(fact_checks) or bool(stances & {"supports", "refutes", "mixed"})
    if strong_enough:
        return {"fact_checks": fact_checks, "web_sources": web_result,
                "search_rounds": 1}
    # ROUND 2 — escalation: round one found nothing, or found only weak
    # background. Hunt again, harder, from different angles, before any
    # verdict is reached.
    deeper = search_web_evidence(claim, deep=True)
    if deeper is not None and web_result:
        # keep round one's context sources alongside the deep findings
        seen = {w["url"] for w in deeper}
        deeper = deeper + [w for w in web_result if w["url"] not in seen]
        deeper = deeper[:MAX_WEB_SOURCES]
    if deeper is None:
        return {"fact_checks": fact_checks, "web_sources": [],
                "search_failed": True, "search_rounds": 2}
    return {"fact_checks": fact_checks, "web_sources": deeper,
            "search_rounds": 2}


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

If the claim is about what a LAW, RULING, STUDY, or DOCUMENT says: search snippets are NOT enough — use web_fetch to OPEN the primary source page and read it before quoting. Quote the document's OPERATIVE text — the part that directly addresses the claim's core assertion (e.g. for a statute: the clause listing prohibited acts, starting from its first sentence — not just penalty clauses). Partial quotes of the wrong section create false verdicts downstream.

Claim: "{claim}"

After searching, respond with ONLY a JSON array (no prose, no code fences) \
of at most {max_sources} sources, each:
{{"source": "publisher name", "url": "https://...", "quote": "short relevant \
quote or finding from that source", "stance": "supports|refutes|mixed|context"}}

"stance" is the source's relationship TO THE CLAIM: supports = source agrees \
claim is true; refutes = source contradicts the claim; mixed = partially \
true; context = background that helps judge it. Use only URLs that appeared \
in your search results. If you find no relevant sources, return []."""


SEARCH_TOOLS_FULL = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAX_WEB_SEARCHES,
    },
    {
        # lets the agent OPEN a page and read it in full — search
        # snippets alone caused a wrong verdict once (18 USC 112)
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 1,
        "max_content_tokens": 20000,
    },
]
SEARCH_TOOLS_BASIC = SEARCH_TOOLS_FULL[:1]  # web_search only


DEEP_PROMPT_SUFFIX = """

THIS IS A SECOND-ROUND DEEP SEARCH — the first round found NOTHING. Before \
concluding nothing exists: (1) rephrase the claim into 2-3 different search \
angles (key entities, alternate wordings, the event it implies); (2) \
explicitly search for hoax/debunk coverage ("<claim topic> hoax", "<claim \
topic> fact check", "did <event> happen"); (3) hunt SMALL and LOCAL sources — local newspapers, school/city \
meeting minutes, niche journals and preprints, PDFs, specialist forums \
with named authors: obscure-but-true claims live there; (4) check whether \
the claim is the KIND that would certainly produce major coverage if true (a death of a \
public figure, a disaster, a new law) — if so and you still find silence, \
return a "context" source documenting what you searched and found absent, \
quoting the most relevant page you DID find (e.g. the person's live \
official page, recent news about them). Only return [] if you truly \
exhausted these angles."""


def search_web_evidence(claim: str, deep: bool = False):
    """Ask Claude (with web search) for stance-tagged sources.

    Returns a list (possibly empty = genuinely nothing found), or None
    when every attempt failed TECHNICALLY (rate limit, tool rejection).
    Retry ladder: full tools -> wait -> full tools -> basic tools.
    deep=True runs the escalated second-round hunt (more searches,
    reformulated angles, hoax checks).
    """
    import time as _time

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    attempts = [
        (SEARCH_TOOLS_FULL, 0),
        (SEARCH_TOOLS_FULL, 8),   # rate-limit windows are short; wait it out
        (SEARCH_TOOLS_BASIC, 4),  # in case the fetch tool itself is rejected
    ]
    prompt_text = PROMPT.format(claim=claim[:500], max_sources=MAX_WEB_SOURCES)
    if deep:
        prompt_text += DEEP_PROMPT_SUFFIX
    for tools, wait in attempts:
        if deep:  # deeper round gets more searches AND more page-reads
            tools = [dict(t) for t in tools]
            tools[0]["max_uses"] = 4
            if len(tools) > 1:
                tools[1]["max_uses"] = 1
        if wait:
            _time.sleep(wait)
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=2500,
                tools=tools,
                messages=[{"role": "user", "content": prompt_text}],
            )
        except Exception:
            continue
        raw = "".join(
            b.text for b in message.content if getattr(b, "type", "") == "text"
        )
        return parse_web_evidence(raw)
    return None


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
