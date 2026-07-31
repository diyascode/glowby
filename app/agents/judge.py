"""
Category Judge Engine — the 13 specialist judges.

ONE engine, 13 rubrics: each routed claim is judged under its category's
distilled spec (app/specs/<bucket>.md — Health's evidence hierarchy,
Law's guilt gate, Sports' official-record rule, and so on). The rubric
text is injected into the judge's instructions, so the specs ARE the
software's rulebook.

Output per claim — ONE number, the TRUTH SCORE:
{
  "truth_score": 8.7 | None,   # 0.0-9.9, higher = better supported.
                               # 9.9 is the ceiling; 10.0 never awarded.
                               # None for unverifiable/not-scoreable —
                               # a non-answer never wears a number.
  "verdict_state": "supported" | "partly_supported" | "provisional" |
                   "insufficient" | "contradicted" | "unverifiable" |
                   "not_scoreable",
  "verdict": one-sentence plain-language ruling,
  "evidence_strength": "strong" | "moderate" | "thin" | "none",
  "key_sources": [urls from the evidence only],
}

Hard rules enforced in CODE (not trusted to the model):
- no evidence -> unverifiable, truth_score None, no model call
- truth_score clamped to 0.0-9.9, one decimal
- key_sources restricted to URLs actually present in the evidence
"""

import json
import os
import re

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
SPECS_DIR = os.path.join(os.path.dirname(__file__), "..", "specs")

VALID_STATES = {
    "supported", "partly_supported", "provisional", "insufficient",
    "contradicted", "unverifiable", "not_scoreable",
}
NULL_SCORE_STATES = {"unverifiable", "not_scoreable"}
VALID_STRENGTH = {"strong", "moderate", "thin", "none"}
MAX_KEY_SOURCES = 3
MAX_RUBRIC_CHARS = 9000

GENERIC_RUBRIC = """Generic scoring rules (no category rubric available):
- Score by the best evidence level that actually supports the claim.
- Multiple independent authoritative sources agreeing: 8.0-9.9.
- Credible but thin or single-source support: 5.0-7.9.
- Evidence insufficient or mixed without resolution: 2.6-4.9.
- Evidence clearly contradicts the claim: 0.0-2.5.
- Mutually-citing outlets count as ONE source (echo collapse).
- Interested parties (the subject of the claim) cannot settle a
  contested claim about themselves."""

_rubric_cache = {}


def load_rubric(bucket: str) -> str:
    """Load the distilled category rubric; fall back to generic rules."""
    bucket = (bucket or "other").lower()
    if bucket in _rubric_cache:
        return _rubric_cache[bucket]
    path = os.path.join(SPECS_DIR, f"{bucket}.md")
    rubric = GENERIC_RUBRIC
    try:
        with open(path, encoding="utf-8") as f:
            rubric = f.read()[:MAX_RUBRIC_CHARS]
    except OSError:
        pass
    _rubric_cache[bucket] = rubric
    return rubric


def _no_evidence(evidence) -> bool:
    if not isinstance(evidence, dict):
        return True
    return not (evidence.get("fact_checks") or evidence.get("web_sources"))


PROMPT = """You are the {bucket} category judge for Glowby, a fact-checking \
service. Judge the claim below using ONLY the evidence provided and your \
category rubric. Do not use outside knowledge to settle the claim; the \
rubric tells you how to weigh the evidence.

=== YOUR CATEGORY RUBRIC (authoritative — follow its evidence hierarchy, \
score bands, caps, and harm gates) ===
{rubric}
=== END RUBRIC ===

Fleet-wide rules (always apply):
- TRUTH SCORE is 0.0-9.9, one decimal. Higher = better supported by \
evidence. 9.9 is the ceiling; never award 10.0.
- Apply every relevant cap from the rubric (single-study caps, provisional \
caps, prediction caps, interested-party rules). If a cap applies, the score \
may not exceed it.
- verdict_state vocabulary: "supported" (roughly 8.0-9.9), \
"partly_supported" (true in part), "provisional" (credibly reported, not \
settled), "insufficient" (evidence does not support), "contradicted" \
(evidence contradicts; roughly 0.0-2.5), "unverifiable" (cannot be judged \
from this evidence), "not_scoreable" (depends on definition / guilt gate / \
matter of taste — the rubric's null-score cases).
- If verdict_state is "unverifiable" or "not_scoreable", truth_score MUST \
be null. Refusing to score is correct in those cases.
- The verdict sentence is plain language a teenager understands, and must \
reflect what the evidence shows, including caution words required by the \
rubric (e.g. presumption language for accusations).
- ABSENCE IS NOT CONTRADICTION: never rule "contradicted" because the \
evidence quotes do not mention the claim's assertion — partial quotes prove \
nothing about what the full source says. "contradicted" requires a source \
explicitly stating the OPPOSITE of the claim. If the evidence does not \
directly address the claim's core assertion, rule "insufficient" or \
"unverifiable" instead. Never assert what a document "does not say" unless \
the evidence includes the document's complete relevant section.
- BUT SILENCE CAN SPEAK (expected-coverage test): when the claim is of a \
kind that would CERTAINLY produce major, easily-findable coverage if true \
(the death of a public figure, a major disaster, a landmark law, a \
record-shattering event) AND a two-round search ({search_rounds} rounds ran \
for this claim) still found no trace of it, that silence is genuine \
evidence AGAINST the claim: rule "insufficient" with a LOW score (1.5-3.5) \
and say plainly: "if this were true, major coverage would exist — none was \
found."
- BURDEN OF PROOF ON ASSERTIONS: a claim that asserts something WORKS, IS \
TRUE, or HAPPENED carries the burden of proof. If the hunt (after \
{search_rounds} rounds) found no supporting evidence for an asserted \
treatment effect, product claim, or factual assertion whose evidence \
SHOULD exist if real (studies, records, coverage), rule "insufficient" \
with a low score (2.0-3.5): "no evidence supports this claim" IS a \
verdict, not a shrug. Note this cuts one way: lack of support lowers the \
score; it never justifies "contradicted" without an explicitly refuting \
source.
- RESERVE "unverifiable" for the genuinely uninvestigable: private/personal \
matters with no public record, claims too vague to pin down, or quiet \
local/niche matters where silence proves nothing either way. It is the \
exception, never the default.

- "NOT RULED OUT" IS NOT CONTRADICTION: a source saying evidence \
"has not ruled out" or "cannot exclude" a possibility does NOT refute a \
claim that the possibility is unsupported. Absence of absolute certainty \
is the normal state of science, not evidence against a consensus claim.
- CONSENSUS OVER SINGLE VOICE: judge scientific and medical claims by \
the WEIGHT of peer-reviewed evidence and the agreement of multiple \
independent scientific bodies. One institution's current website \
phrasing — even a famous one — never outweighs the broader evidence \
base and other major scientific bodies. If one authority's wording \
conflicts with the wider consensus, SAY SO in the verdict and score by \
the consensus, noting the outlier.
- TEMPORAL FAIRNESS: the truth score protects a viewer acting on this \
claim TODAY. But when the claim was accurate at the time the video was \
posted (posted: {posted_date}) and was later outdated by events, the \
verdict sentence MUST say so ("accurate when this video was posted; \
outdated since ..."). Expired truth reads as partly_supported with a \
mid-high score; a claim that was NEVER true reads much lower. Also watch \
the reverse trick: old footage or old claims resurfacing as if current — \
if the posting date makes "recent"-sounding claims stale, say that. This \
applies ESPECIALLY to RUNNING TALLIES (career goals, follower counts, \
death tolls, prices): compare the number to its value ON THE POSTED DATE, \
not today's value. A tally that was right when posted and has since grown \
is expired truth (partly_supported, mid-high score) — never "contradicted."

Claim (routed to {bucket}{secondary_note}, risk level {risk_level}): \
"{claim}"

Evidence — professional fact-checker reviews:
{fact_checks}

Evidence — web sources (stance toward the claim):
{web_sources}

Respond with ONLY a JSON object (no prose, no code fences):
{{"truth_score": 8.2 or null, "verdict_state": "...", "verdict": "one \
sentence", "evidence_strength": "strong|moderate|thin|none", \
"key_sources": ["url1", "url2"], "why_unverifiable": null}}
why_unverifiable: null unless verdict_state is unverifiable/not_scoreable — \
then exactly one of: "no_sources_found" (nothing relevant surfaced), \
"sources_dont_address_claim" (sources exist but none speak to the core \
assertion), "conflicting_sources" (credible sources disagree without \
resolution), "too_new_to_verify" (developing story; reliable sources \
haven't caught up), "speculation_no_data" (unanchored prediction, no data \
basis), "depends_on_definition" (superlative/undefined terms), \
"guilt_gate" (allegation awaiting official findings).
key_sources: up to {max_sources} URLs copied EXACTLY from the evidence \
above — never invent one."""


def judge_with_rubric(claim: dict, evidence: dict) -> dict:
    """Judge one routed claim under its category rubric."""
    bucket = claim.get("bucket", "other")

    # a TECHNICAL search failure still short-circuits — no judge can rule
    # on a hunt that never ran
    if isinstance(evidence, dict) and evidence.get("search_failed"):
        return {
            "truth_score": None,
            "verdict_state": "unverifiable",
            "verdict": "Glowby's evidence search hit a technical problem "
            "for this claim — this is not a judgment about the claim. "
            "Use 'Re-check this video' to try again.",
            "evidence_strength": "none",
            "key_sources": [],
            "why_unverifiable": "search_error",
        }
    # NOTE: an EMPTY-but-successful hunt no longer short-circuits. The
    # category judge rules on it under its rubric's burden-of-proof
    # logic (the peptide lesson: "no evidence supports this" is a LOW
    # SCORE for an asserted treatment claim, not a shrug).

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "truth_score": None,
            "verdict_state": "unverifiable",
            "verdict": "The judge engine is not configured.",
            "evidence_strength": "none",
            "key_sources": [],
        }

    import anthropic

    secondary = claim.get("secondary_bucket")
    prompt = PROMPT.format(
        bucket=bucket,
        rubric=load_rubric(bucket),
        secondary_note=f" (also touches {secondary})" if secondary else "",
        risk_level=claim.get("risk_level", "low"),
        claim=str(claim.get("claim", ""))[:500],
        fact_checks=_format_fact_checks(evidence),
        web_sources=_format_web_sources(evidence),
        max_sources=MAX_KEY_SOURCES,
        search_rounds=evidence.get("search_rounds", 1) if isinstance(evidence, dict) else 1,
        posted_date=claim.get("posted_date") or "unknown",
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            temperature=0,  # same claim + same evidence -> same verdict
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return {
            "truth_score": None,
            "verdict_state": "unverifiable",
            "verdict": "The judge engine had a temporary problem; try again.",
            "evidence_strength": "none",
            "key_sources": [],
        }

    raw = "".join(
        b.text for b in message.content if getattr(b, "type", "") == "text"
    )
    verdict = parse_judge_response(raw, allowed_urls=_collect_urls(evidence))
    if verdict is None:
        return {
            "truth_score": None,
            "verdict_state": "unverifiable",
            "verdict": "The judge engine returned an unreadable response.",
            "evidence_strength": "none",
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


def clamp_truth_score(value):
    """0.0-9.9, one decimal; None passes through (null score is legal)."""
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(9.9, score)), 1)


def parse_judge_response(raw: str, allowed_urls=None):
    """Parse and validate the judge's JSON verdict. None if unreadable."""
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

    state = str(data.get("verdict_state", "")).lower().strip()
    if state not in VALID_STATES:
        return None

    score = clamp_truth_score(data.get("truth_score"))
    # hard rule: null-score states never carry a number; scored states must
    if state in NULL_SCORE_STATES:
        score = None
    elif score is None:
        return None

    strength = str(data.get("evidence_strength", "")).lower().strip()
    if strength not in VALID_STRENGTH:
        strength = "none" if state in NULL_SCORE_STATES else "thin"

    sources = []
    for u in data.get("key_sources") or []:
        u = str(u).strip()
        if not u.startswith("http"):
            continue
        if allowed_urls is not None and u not in allowed_urls:
            continue
        sources.append(u)
        if len(sources) >= MAX_KEY_SOURCES:
            break

    WHY_VOCAB = {
        "no_sources_found", "sources_dont_address_claim",
        "conflicting_sources", "too_new_to_verify", "speculation_no_data",
        "depends_on_definition", "guilt_gate", "search_error",
    }
    why = data.get("why_unverifiable")
    if state in NULL_SCORE_STATES:
        why = str(why or "").lower().strip()
        if why not in WHY_VOCAB:
            why = "no_sources_found"
    else:
        why = None

    return {
        "truth_score": score,
        "verdict_state": state,
        "verdict": str(data.get("verdict", "")).strip()[:500],
        "evidence_strength": strength,
        "key_sources": sources,
        "why_unverifiable": why,
    }
