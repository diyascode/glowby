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
# COST TIERING: a cheaper judge for low-stakes buckets; the strong model
# stays on every category where a wrong verdict can hurt someone.
# OFF by default (standing rule: every claim is judged by Sonnet). Opt in
# with GLOWBY_JUDGE_TIERING=1. The Apple-Watch incident: low-stakes claims
# quietly went to the cheap model and came back unreadable.
JUDGE_MODEL_LOW = os.environ.get("GLOWBY_JUDGE_MODEL_LOW", "claude-haiku-4-5")
JUDGE_TIERING = os.environ.get("GLOWBY_JUDGE_TIERING", "0") == "1"
HIGH_STAKES_BUCKETS = {"politics", "news", "health", "law", "science",
                       "finance", "economy", "safety", "crime"}


# CACHE LIFETIME: "1h" keeps the rulebook warm for an hour per use (the
# longest Anthropic offers); a keep-alive ping in main.py re-reads it
# hourly so it effectively never expires. If the API ever rejects the
# option, we fall back to the default 5-minute cache — never fail a check.
CACHE_TTL = os.environ.get("GLOWBY_CACHE_TTL", "1h")
_ttl_supported = {"ok": True}


def _cache_block(text: str) -> dict:
    cc = {"type": "ephemeral"}
    if CACHE_TTL and CACHE_TTL != "5m" and _ttl_supported["ok"]:
        cc["ttl"] = CACHE_TTL
    return {"type": "text", "text": text, "cache_control": cc}


def cached_system_blocks(bucket: str) -> list:
    """The two cacheable prefixes (shared rules, category rubric) exactly
    as judge_with_rubric sends them — used by the hourly keep-alive."""
    prompt = PROMPT.format(
        bucket=bucket, rubric=load_rubric(bucket), secondary_note="",
        risk_level="low", claim="", fact_checks="", web_sources="",
        max_sources=MAX_KEY_SOURCES, search_rounds=1, posted_date="unknown")
    rub_at = prompt.index("=== YOUR CATEGORY: ")
    claim_at = prompt.index("Claim (routed to ")
    return [_cache_block(prompt[:rub_at]), _cache_block(prompt[rub_at:claim_at])]


def keep_cache_warm(models=None) -> dict:
    """One tiny call per judge model that re-reads the shared rulebook so
    its 1-hour cache never lapses. ~2.8k cached tokens + a 1-token reply:
    pennies per day. Returns a small status dict (for the admin page)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "detail": "no api key"}
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    out = {}
    for m in (models or sorted({MODEL, JUDGE_MODEL_LOW})):
        try:
            blocks = cached_system_blocks("other")[:1]  # rules only
            r = client.messages.create(
                model=m, max_tokens=1, system=blocks,
                messages=[{"role": "user", "content": "ok"}])
            u = getattr(r, "usage", None)
            out[m] = {"ok": True,
                      "cache_read": getattr(u, "cache_read_input_tokens", None),
                      "cache_write": getattr(u, "cache_creation_input_tokens", None)}
        except Exception as e:
            out[m] = {"ok": False, "detail": str(e)[:160]}
    return out


def pick_judge_model(claim: dict) -> str:
    """Strong model for high-stakes; cheap model for the rest."""
    if not JUDGE_TIERING:
        return MODEL
    bucket = str(claim.get("bucket", "other")).lower()
    if bucket in HIGH_STAKES_BUCKETS:
        return MODEL
    if claim.get("public_safety_risk") or str(claim.get("risk_level", "")).lower() == "high":
        return MODEL
    if claim.get("media_context"):  # AI-footage cases need the careful judge
        return MODEL
    return JUDGE_MODEL_LOW
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


PROMPT = """You are a category judge for Glowby, a fact-checking service. \
Judge the claim using ONLY the evidence provided and your category rubric. \
Do not use outside knowledge to settle the claim; the rubric tells you how \
to weigh the evidence.

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
- TYPICAL PRACTICE IS NOT PROOF ABOUT THIS INSTANCE: evidence \
describing what USUALLY happens (how events are normally staged, what \
equipment is standard, what a company typically does) can never \
CONTRADICT a claim about a specific depicted event. "Official events \
use fixed platforms" does not prove this particular dive from an \
inflatable platform never happened. When the CORE EVENT is supported \
and only a peripheral descriptor (the platform type, the color, the \
exact location) conflicts with typical practice, rule partly_supported \
(5.5-7.5) and name the doubt in the verdict — never "contradicted". \
"Contradicted" requires evidence about THIS instance: a debunk of this \
footage, proof of impossibility, or a source refuting this specific \
event.
- BUT SILENCE CAN SPEAK (expected-coverage test): when the claim is of a \
kind that would CERTAINLY produce major, easily-findable coverage if true \
(the death of a public figure, a major disaster, a landmark law, a \
record-shattering event) AND the search (the number of rounds that ran \
is stated in the claim block below) still found no trace of it, that silence is genuine \
evidence AGAINST the claim: rule "insufficient" with a LOW score (1.5-3.5) \
and say plainly: "if this were true, major coverage would exist — none was \
found."
- SAME-VIDEO EVIDENCE COUNTS: sources marked [context · found for another \
claim in this video] were located while checking a DIFFERENT claim from the \
same video. Read them for what they say about THIS claim. If they cover the \
same event, person, or product, they are evidence here — the silence test \
above does NOT apply, and "no evidence found" would be false.
- BURDEN OF PROOF ON ASSERTIONS: a claim that asserts something WORKS, IS \
TRUE, or HAPPENED carries the burden of proof. If the hunt (see the \
rounds count in the claim block) found no supporting evidence for an asserted \
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
posted (the posting date is in the claim block) and was later outdated by events, the \
verdict sentence MUST say so ("accurate when this video was posted; \
outdated since ..."). Expired truth reads as partly_supported with a \
mid-high score; a claim that was NEVER true reads much lower. Also watch \
the reverse trick: old footage or old claims resurfacing as if current — \
if the posting date makes "recent"-sounding claims stale, say that. This \
applies ESPECIALLY to RUNNING TALLIES (career goals, follower counts, \
death tolls, prices): compare the number to its value ON THE POSTED DATE, \
not today's value. A tally that was right when posted and has since grown \
is expired truth (partly_supported, mid-high score) — never "contradicted." \
MONOTONIC INFERENCE: for counters that only ever go UP (career goals, \
total followers, cumulative deaths), a claimed number LOWER than today's \
value, posted on an earlier date, is CONSISTENT with having been accurate \
when posted — the tally must have passed through that number on its way \
up. Unless a source shows the number was wrong ON THE POSTED DATE itself, \
you MUST treat it as expired truth (partly_supported, score 6.0-7.5), \
never contradicted. Evidence quoting only TODAY'S higher value does not \
refute the posted-date claim — it supports the monotonic inference.
- CORROBORATION COUNTS: when MULTIPLE independent reputable news \
organizations each report the same specific figure or event, that IS \
verification — score it supported (8.0+). A primary record (SEC filing, \
official register) is the gold standard, but its absence from the \
evidence bundle does not demote a figure that several independent \
outlets agree on. Echo-collapse (distrusting many stories that trace to \
ONE self-interested source) applies to press releases and company \
self-reports — NEVER to market data (stock prices, trading moves, \
indexes) or to facts journalists observe independently.
- THIN IS NOT FALSE: "insufficient evidence" with ZERO disputing sources \
is a MID outcome — partly_supported or unverifiable, score 5.0-6.5 — \
never a red score. Low scores (below 4) are reserved for claims the \
evidence actually CONTRADICTS. Never let a claim nobody disputes drag \
the headline into "contradicted" territory.
- AI-MEDIA CONTEXT: when a MEDIA CONTEXT line below reports that this \
video's footage is AI-generated (verified provenance, creator label, or \
strong forensic signals), any claim that the video "depicts", "shows", \
"captures" or "is footage of" a real place, person, or event is judged \
in TWO parts: the underlying world-fact may be scored on its evidence, \
but the DEPICTION is false — this footage is synthetic, not a recording \
of the real thing. Such a claim is at most "partly_supported" with a \
score no higher than 5.5, and the verdict MUST say plainly that the \
footage itself is AI-generated even if the thing it imitates exists.
- SELF-REFERENTIAL CLAIMS (claims about the video's own content — \
"this video", "these clips", "what you're watching"): judge the \
VERIFIABLE GENERAL assertion inside the claim (e.g., "Sora can generate \
video indistinguishable from real footage"), and note in the verdict \
that the specific instance rests on the creator's own statement about \
their own content. Score the general assertion by its evidence \
(typically partly_supported or supported when well-evidenced). Do NOT \
return "not_scoreable" merely because the specific referent (which clip, \
which frame) cannot be inspected — if a scoreable general claim is \
present, score it. Reserve not_scoreable for claims with NO verifiable \
general assertion at all. This rule exists so re-checks of the same \
video never flip between a score and a shrug.
- CONTESTED-CLAIM STABILITY: when credible sources GENUINELY DISAGREE \
about the claim's core assertion — officials say one thing, independent \
experts another; reputable sources on BOTH sides — the claim is CONTESTED, \
which is different from thin evidence. Rule: verdict_state \
"partly_supported", score pinned to the 4.5-5.5 band, and the verdict \
sentence MUST name both sides ("Treasury officials said X; sanctions \
experts argue Y"). Genuine disagreement NEVER scores below 4.5 — \
disagreement between credible sources is not refutation, and a contested \
claim must land in the same band on every re-check, not swing between \
"insufficient" and "partly supported" depending on which sources a given \
search surfaced.
- NAME THE NUMBER: when a claim asserts a specific figure and the \
evidence reports a DIFFERENT figure for the same fact, the verdict MUST \
state the evidence's figure explicitly ("sources report ~14%, not 10%") \
so the reader leaves knowing the real number. NEVER return a vague \
"insufficient evidence" verdict when the evidence actually contains the \
correct number. A figure that differs only by everyday rounding ($1,999 \
vs "$2,000") is NOT a different figure — see ROUNDING IS NOT AN ERROR \
below; this rule does not apply to it.
- THE RIGHT BALLPARK IS NOT A LIE (hard boundary for numeric gaps): \
when the EVENT ITSELF is real and the claim's figure is in the same \
ballpark as the evidence's (within roughly a factor of two, same \
direction of the story), that is partly_supported with a MID score \
(5.5-7.0) — never "contradicted". "400 missing" against "more than 500 \
missing" confirms the story: people are missing in the hundreds; the \
claim is an undercount, not a falsehood. Reserve "contradicted" for a \
figure that CHANGES THE STORY: wrong by an order of magnitude ("400" \
vs "4"), wrong direction ("400 missing" vs "everyone accounted for"), \
or an event that did not happen at all.
- ROUNDING IS NOT AN ERROR (overrides NAME THE NUMBER and every rubric \
figure-mismatch cap): a figure that is the evidence's figure rounded the \
way people speak ($1,999 said as "$2,000", $3,199 as "$3,200", 49.9% as \
"about half", 1,980 as "nearly 2,000") is the SAME figure. Rule \
"supported" at the full score the evidence earns; do not deduct for it, \
and do not write "$1,999, not $2,000" — that sentence is itself the \
error. Test: would a careful reporter call the claim wrong? If not, it \
is not wrong.
- ANNOUNCED IS NOT PREDICTED (overrides any rubric roadmap / pre-release / \
prediction cap): once a maker has OFFICIALLY ANNOUNCED a product or feature \
with its specifications — a keynote, a press release, a published spec \
sheet, a store listing — a claim that restates those specifications is a \
vendor-stated FACT, not a roadmap item. Score it by corroboration: \
several independent outlets reporting the announced spec = supported \
(7.5-8.5), with "vendor-stated, not yet independently tested" in the \
verdict when no teardown or benchmark exists yet. Roadmap and prediction \
caps apply ONLY to unannounced, rumored, leaked, or future-dated items. \
"No independent testing yet" is a caveat, never a cap.
- JUDGE THE CLAIM'S OWN ARITHMETIC: when a claim makes a comparison or a \
calculation ("up $100 from the 17 Pro"; "twice as fast as last year's"), \
check THAT comparison against the evidence's figures. Never substitute a \
different comparison the claim did not make (a different model tier, a \
different year, a different metric) and then score the claim against it. \
If the claim's own arithmetic holds, it is supported.
- COUNTS GROW IN DEVELOPING STORIES: casualty, missing-person, and \
damage figures in disasters and breaking news RISE as reporting \
matures. A lower figure that matched reporting at the video's posting \
date (see the claim block) and was later overtaken is EXPIRED TRUTH under \
temporal fairness: partly_supported, 6.0-7.0, with a verdict like \
"about right when posted; the count has since risen to X".

=== YOUR CATEGORY: {bucket} — RUBRIC (authoritative — follow its evidence \
hierarchy, score bands, caps, and harm gates) ===
{rubric}
=== END RUBRIC ===

Claim (routed to {bucket}{secondary_note}, risk level {risk_level}; \
video posted: {posted_date}; evidence search rounds that ran: {search_rounds}): \
"{claim}"

Evidence — professional fact-checker reviews:
{fact_checks}

Evidence — web sources (stance toward the claim):
{web_sources}

Respond with ONLY a JSON object (no prose, no code fences):
{{"truth_score": 8.2 or null, "verdict_state": "...", "verdict": "one \
sentence", "evidence_strength": "strong|moderate|thin|none", \
"key_sources": ["url1", "url2"], "why_unverifiable": null}}
verdict_state MUST be exactly one of the seven fleet values: supported, \
partly_supported, provisional, insufficient, contradicted, unverifiable, \
not_scoreable. The rubric's own state names (record-verified, \
vendor-claim-only, study-limited, and the like) are for your reasoning — \
translate them to the fleet value; never output them.
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
        claim=(("MEDIA CONTEXT: independent authenticity analysis reports "
               "this video's footage is AI-generated (" 
               + str(claim.get("media_context")) + ").\n"
               if claim.get("media_context") else "")
               + str(claim.get("claim", ""))[:500]),
        fact_checks=_format_fact_checks(evidence),
        web_sources=_format_web_sources(evidence),
        max_sources=MAX_KEY_SOURCES,
        search_rounds=evidence.get("search_rounds", 1) if isinstance(evidence, dict) else 1,
        posted_date=claim.get("posted_date") or "unknown",
    )

    # PROMPT CACHING: everything before the claim block is identical for
    # every claim in a bucket (fleet rules + rubric, ~5k tokens). It goes
    # in a cached system block; only the claim + evidence are paid in full.
    rub_at = prompt.index("=== YOUR CATEGORY: ")
    claim_at = prompt.index("Claim (routed to ")
    rules_part = prompt[:rub_at]            # identical for EVERY category
    rubric_part = prompt[rub_at:claim_at]   # identical within a category
    dynamic_part = prompt[claim_at:]        # this claim + its evidence
    model = pick_judge_model(claim)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=1200,
            temperature=0,  # same claim + same evidence -> same verdict
            system=[_cache_block(rules_part), _cache_block(rubric_part)],
            messages=[{"role": "user", "content": dynamic_part}],
        )
    except Exception as _e:
        # if the extended TTL itself is what got rejected, drop to the
        # default 5-minute cache and retry ONCE — a check must never fail
        # because of a caching option
        if _ttl_supported["ok"] and "ttl" in str(_e).lower():
            _ttl_supported["ok"] = False
            try:
                message = client.messages.create(
                    model=model, max_tokens=1200, temperature=0,
                    system=[_cache_block(rules_part), _cache_block(rubric_part)],
                    messages=[{"role": "user", "content": dynamic_part}],
                )
            except Exception:
                message = None
        else:
            message = None
    if message is None:
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
        # SECOND CHANCE: an unreadable reply is a wasted check for the
        # reader. Ask once more — on the strong model, with a firmer
        # format reminder — before admitting defeat.
        try:
            message2 = client.messages.create(
                model=MODEL, max_tokens=1500, temperature=0,
                system=[_cache_block(rules_part), _cache_block(rubric_part)],
                messages=[{"role": "user", "content": dynamic_part
                           + "\n\nREMINDER: output the JSON object only — "
                           "no preamble, no analysis, no code fences."}],
            )
            raw2 = "".join(b.text for b in message2.content
                           if getattr(b, "type", "") == "text")
            verdict = parse_judge_response(raw2, allowed_urls=_collect_urls(evidence))
        except Exception:
            verdict = None
    if verdict is None:
        return {
            "truth_score": None,
            "verdict_state": "unverifiable",
            "verdict": "The judge engine returned an unreadable response.",
            "evidence_strength": "none",
            "key_sources": [],
            "why_unverifiable": "search_error",
            "parse_debug": (raw or "")[:240],
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
    def _tag(r):
        st = r.get("stance", "?")
        if r.get("from_sibling"):
            return f"{st} · found for another claim in this video"
        return st
    return "\n".join(
        f'- {r.get("source", "?")} [{_tag(r)}]: '
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


_NULL_WORDS = ("not_scoreable", "unscoreable", "shell", "allegation", "normative",
               "guilt", "taste", "definition")
_UNVERIFIED_WORDS = ("unverified", "unverifiable", "no_reliable_basis", "cannot_verify")
_CONTRA_WORDS = ("contradicted", "unsupported", "refuted", "false", "debunked")
_PROV_WORDS = ("provisional", "credibly_reported", "provisionally", "deal_state", "reported")


def translate_state(state: str, score):
    """Pure (unit-tested): map a rubric-vocabulary verdict_state to the
    fleet vocabulary. The SCORE decides the band (it is what the reader
    sees); the words only settle null-score cases and the
    provisional/partly split. Returns None only when nothing usable."""
    st = (state or "").lower()
    if score is None:
        if any(w in st for w in _NULL_WORDS):
            return "not_scoreable"
        if any(w in st for w in _UNVERIFIED_WORDS) or "insufficient" in st or "capped" in st:
            return "unverifiable"
        return "unverifiable" if st else None
    if any(w in st for w in _CONTRA_WORDS) and score <= 4.9:
        return "contradicted" if score <= 2.5 else "insufficient"
    if score >= 8.0:
        return "supported"
    if score >= 5.0:
        return "provisional" if any(w in st for w in _PROV_WORDS) else "partly_supported"
    if score >= 2.6:
        return "insufficient"
    return "contradicted"


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
    state = re.sub(r"[\s\-]+", "_", state)  # "partly supported" / "not-scoreable"
    score = clamp_truth_score(data.get("truth_score"))
    if state not in VALID_STATES:
        # THE APPLE-WATCH BUG: category rubrics carry their OWN state
        # vocabularies ("record-verified", "vendor-claim-only",
        # "study-limited"...). When the judge answers in the rubric's
        # words, translate — never throw away a verdict that has a score.
        state = translate_state(state, score)
        if state is None:
            return None

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
