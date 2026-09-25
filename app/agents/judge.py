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
import time

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
        max_sources=MAX_KEY_SOURCES, search_rounds=1, posted_date="unknown", today="")
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
- YOU NEVER DECLINE A CLAIM FOR BEING OUTSIDE YOUR CATEGORY. Your rubric's \
"Scope" and "handoff" sections describe where claims are normally routed; \
they are NOT permission to refuse. A plant-care claim at the health desk, \
a gardening claim at the science desk, a sports claim at the business \
desk: judge it anyway, from the evidence, the way a general fact-checker \
would, applying whatever rubric caps still make sense — and set \
"wrong_desk" to the better category so Glowby can learn. "not_scoreable" \
exists ONLY for: depends on a definition, a guilt gate, a matter of taste. \
"Not within this category's scope" is never a verdict.
- THE EVIDENCE OUTRANKS YOUR MEMORY. Your training stopped on a date; the \
world did not. Today's date is in the claim block. When reputable sources \
in the evidence (Wikipedia, AP, Reuters, BBC, ABC, NPR, major newspapers, \
official records) report an event you do not remember — a death, an \
election result, a verdict, a law, a disaster, a resignation — the \
sources are right and your memory is out of date. NEVER call evidence \
"fabricated", "not real", "does not exist" or "hallucinated" because it \
conflicts with what you remember. NEVER rule a claim contradicted, or a \
person alive, or an event un-happened, on your own knowledge: \
"contradicted" requires a source IN THE EVIDENCE that says the opposite. \
If the evidence supports a claim you find surprising, rule supported and \
say what the sources report.
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
error. This covers ROUND FIGURES IN PROSE too: a claim that says "to \
$100", "hit 90%", "reached 2,000" is confirmed by evidence saying "near", \
"around", "close to", "roughly", "approaching" that figure, or giving a \
number within about 3% of it. "Oil rebounded to $100" against sources \
saying "oil near $100" is supported — never "not verified because the \
sources say near, not to." Test: would a careful reporter call the claim \
wrong? If not, it is not wrong.
- A MATCHING FIGURE IS NOT CONTRADICTED BY A DIFFERENT STATISTIC: when \
the claim's number matches a figure a reliable source reports for the \
fact AS STATED (within rounding — "8%" against "7.9%"), the claim is \
supported on that figure, full stop. A different statistic — another \
period, another measure (sheltered vs unsheltered, city vs county, one \
count vs a two-year trend), another baseline — is CONTEXT: it may cap \
the verdict at partly_supported (6.0-7.5) when the video's framing \
invites the wrong reading, and the verdict should name it ("true for the \
2025 count; over her full term the number fell 17.5%"). It can never \
turn a matching figure into "contradicted". Contradicted means the \
figure the video gives is not what the sources say for the fact as stated.
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

AMBIGUOUS REFERENT IS NEVER not_scoreable: if the claim says "the bill", \
"this law", "the drug", "he", resolve it from the VIDEO CONTEXT line (the \
video's title and its other claims name it). Judge the claim about THAT \
named thing. If the evidence still does not settle it, rule insufficient \
(unverified) — never not_scoreable. not_scoreable is only for claims that \
cannot be true or false at all (taste, prophecy, a definition fight, a \
guilt gate).

Claim (routed to {bucket}{secondary_note}, risk level {risk_level}; \
video posted: {posted_date}; today: {today}; evidence search rounds that ran: {search_rounds}): \
"{claim}"

Evidence — professional fact-checker reviews:
{fact_checks}

Evidence — web sources (stance toward the claim):
{web_sources}

Respond with ONLY a JSON object (no prose, no code fences):
{{"truth_score": 8.2 or null, "verdict_state": "...", "verdict": "one \
sentence", "evidence_strength": "strong|moderate|thin|none", \
"key_sources": ["url1", "url2"], "why_unverifiable": null, "wrong_desk": null}}
verdict_state MUST be exactly one of the seven fleet values: supported, \
partly_supported, provisional, insufficient, contradicted, unverifiable, \
not_scoreable. The rubric's own state names (record-verified, \
vendor-claim-only, study-limited, and the like) are for your reasoning — \
translate them to the fleet value; never output them.
WRONG DESK: if the claim clearly belongs to a DIFFERENT category (a plant-care \
claim routed to health, a stock-price claim routed to science), do NOT rule \
not_scoreable for being outside your scope — judge it if the evidence lets \
you, and ALSO set "wrong_desk" to the category it belongs in (one of: \
politics, health, science, economy, business, technology, law, conflict, \
education, society_culture, sports, entertainment, history_geography). \
Glowby then re-judges it at that desk. Otherwise wrong_desk is null.
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
    """Judge one routed claim under its category rubric. A judge that
    declines for scope ("not a health claim") sends the claim ONCE to the
    desk it named — or the secondary bucket, or science for nature/
    how-things-work claims — instead of shipping the refusal as the
    verdict (the succulents check, Sep 2026)."""
    v = _judge_once(claim, evidence)
    if scope_refused(v) and not claim.get("_rerouted"):
        here = claim.get("bucket", "other")
        to = v.get("wrong_desk") or claim.get("secondary_bucket") or "science"
        if to == here:
            to = claim.get("secondary_bucket") or ("science" if here != "science" else "other")
        # the desks to try, in order, each with the no-refusal reminder;
        # "other" (the general desk) is always the last resort
        desks = [d for d in (to, "other") if d and d != here]
        if not desks:
            desks = ["other"]
        for desk in dict.fromkeys(desks):
            c2 = dict(claim)
            c2["bucket"] = desk
            c2["secondary_bucket"] = None
            c2["_rerouted"] = True
            v2 = _judge_once(c2, evidence, reminder=NO_REFUSAL_REMINDER)
            if isinstance(v2, dict) and not scope_refused(v2):
                v2["rerouted_from"] = here
                v2["rerouted_to"] = desk
                v2.pop("wrong_desk", None)
                # the card shows the desk that actually ruled
                claim["bucket"] = desk
                claim["secondary_bucket"] = None
                claim["rerouted_from"] = here
                return v2
    # a judge that punted on "which bill / which law" when the video names
    # it (the miscarriage-bill check, Sep 20): one more pass with the
    # context spelled out; if it still cannot decide, that is unverified,
    # not "cannot be scored"
    if isinstance(v, dict) and referent_punt(v) and not claim.get("_referent_retry"):
        c2 = dict(claim)
        c2["_referent_retry"] = True
        v2 = _judge_once(c2, evidence, reminder=REFERENT_REMINDER.format(
            context=str(claim.get("video_context") or claim.get("claim") or "")[:700]))
        if isinstance(v2, dict) and v2.get("verdict_state") and not referent_punt(v2):
            v = v2
        elif isinstance(v, dict):
            v = dict(v)
            v["verdict_state"] = "insufficient"
            v["why_unverifiable"] = v.get("why_unverifiable") or "the sources found don't settle this for the named bill"
    # MEMORY BACKSTOP (Sep 24, the Charlie Kirk check): a judge that calls
    # its own evidence "fabricated" / "does not exist" is judging from
    # memory. One more pass with the rule spelled out; if it still refuses
    # the evidence, the evidence's own stances decide, not the judge.
    if isinstance(v, dict) and evidence_denied(v) and not claim.get("_memory_retry"):
        c2 = dict(claim)
        c2["_memory_retry"] = True
        v2 = _judge_once(c2, evidence, reminder=MEMORY_REMINDER)
        if isinstance(v2, dict) and v2.get("verdict_state") and not evidence_denied(v2):
            v = v2
        else:
            v = verdict_from_stances(evidence, claim)
        v["memory_override"] = True
    # ROUNDING BACKSTOP (Sep 20, "8% vs 7.9% — too harsh"): a judge that
    # rules CONTRADICTED while its own verdict cites a figure that matches
    # the claim's (within 3%) has broken the rounding rule — one more pass
    # with the match spelled out.
    if isinstance(v, dict) and not claim.get("_rounding_retry"):
        pair = matching_figure(str(claim.get("claim") or ""), v)
        if pair:
            c2 = dict(claim)
            c2["_rounding_retry"] = True
            v2 = _judge_once(c2, evidence, reminder=ROUNDING_REMINDER.format(a=pair[0], b=pair[1]))
            if isinstance(v2, dict) and v2.get("verdict_state"):
                v2["rounding_retry"] = True
                v = v2
    if isinstance(v, dict):
        v.pop("wrong_desk", None)
    return v


_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent)?", re.I)


def _figures(text: str) -> list:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.append((val, bool(m.group(2)), m.group(0).strip()))
    return out


def _looks_like_year(raw: str, pct: bool) -> bool:
    d = raw.rstrip("%").replace("percent", "").strip()
    return (not pct) and d.isdigit() and len(d) == 4 and 1900 <= int(d) <= 2100


def matching_figure(claim_text: str, verdict: dict):
    """Pure: when a CONTRADICTED verdict's own text cites a number within
    3% of a number in the claim (same unit: % with %, plain with plain),
    return (claim_figure, verdict_figure) — else None. Years are skipped."""
    if not isinstance(verdict, dict) or verdict.get("verdict_state") != "contradicted":
        return None
    vt = str(verdict.get("verdict") or "")
    for cv, cpct, craw in _figures(claim_text):
        if cv == 0 or _looks_like_year(craw, cpct):
            continue
        for vv, vpct, vraw in _figures(vt):
            if vpct != cpct or vv == 0 or _looks_like_year(vraw, vpct):
                continue
            if abs(cv - vv) / max(abs(cv), abs(vv)) <= 0.03 and craw != vraw:
                return (craw, vraw)
    return None


ROUNDING_REMINDER = (
    "REMINDER — ROUNDING IS NOT AN ERROR: you ruled this claim contradicted, "
    "but your own verdict cites {b}, which is the claim's {a} rounded the way "
    "people speak. On that figure the claim is supported. If a DIFFERENT "
    "statistic (another period, measure or baseline) changes the picture, "
    "say so and rule partly_supported (6.0-7.5), naming both figures. "
    "Never rule contradicted on a figure that matches."
)


_DENIAL_RE = re.compile(
    r"(fabricat|do(es)? not exist|don't exist|not exist in reality|no such (person|event|article|report)|"
    r"hallucinat|fake (sources?|articles?|reports?)|(sources?|articles?|reports?) (that )?(are|is) (not real|invented|made up)|"
    r"(is|are|remains?) (alive|still alive|living)\b[^.]{0,80}(not deceased|not dead|has not died)|"
    r"(has|have) not (died|passed away|been (elected|convicted|signed)))", re.I)


def evidence_denied(verdict: dict) -> bool:
    """Pure: the judge dismissed the evidence in front of it as unreal, or
    asserted from memory that a reported event did not happen."""
    if not isinstance(verdict, dict):
        return False
    text = " ".join(str(verdict.get(k) or "") for k in ("verdict", "why_unverifiable"))
    return bool(_DENIAL_RE.search(text))


_TRUSTED = ("wikipedia.org", "apnews.com", "reuters.com", "bbc.co", "bbc.com", "abcnews", "nbcnews", "cbsnews",
            "npr.org", "nytimes.com", "washingtonpost.com", "wsj.com", "theguardian.com", "cnn.com", "politico.com",
            "axios.com", "bloomberg.com", "latimes.com", ".gov", "usatoday.com", "pbs.org", "time.com", "forbes.com")


def verdict_from_stances(evidence: dict, claim: dict) -> dict:
    """Pure: when the judge will not accept its evidence, let the evidence
    speak — a plain verdict from the sources' own stances."""
    rows = (evidence or {}).get("web_sources") or []
    fcs = (evidence or {}).get("fact_checks") or []
    sup = [r for r in rows if r.get("stance") == "supports"]
    ref = [r for r in rows if r.get("stance") == "refutes"]
    trusted_sup = [r for r in sup if any(t in str(r.get("url") or "") for t in _TRUSTED)]
    names = lambda rs: ", ".join(dict.fromkeys(str(r.get("source") or r.get("url") or "")[:40] for r in rs[:3]))
    base = {"evidence_strength": "moderate", "key_sources": [r["url"] for r in (sup or ref)[:3] if r.get("url")], "why_unverifiable": None}
    if (len(sup) >= 2 or trusted_sup) and not ref:
        return dict(base, truth_score=8.0 if trusted_sup else 7.5, verdict_state="supported",
                    verdict=f"Reported by {names(sup)}; see the sources.")
    if ref and not sup:
        return dict(base, truth_score=2.5, verdict_state="contradicted",
                    verdict=f"{names(ref)} report the opposite of this claim.")
    if sup and ref:
        return dict(base, truth_score=5.0, verdict_state="partly_supported",
                    verdict=f"Sources disagree: {names(sup)} support it, {names(ref)} dispute it.")
    return dict(base, truth_score=None, verdict_state="insufficient", evidence_strength="thin",
                verdict="The sources found don't settle this claim.", why_unverifiable="sources_dont_address_claim")


MEMORY_REMINDER = (
    "REMINDER — THE EVIDENCE OUTRANKS YOUR MEMORY: you dismissed the sources "
    "above as fabricated or nonexistent, or asserted from memory that a "
    "reported event did not happen. Your training has a cutoff date; the "
    "sources are dated after it. Judge ONLY from the evidence: if reputable "
    "sources report the event, it happened. Rule supported/contradicted "
    "strictly by what the sources say."
)


_REFERENT_PUNT_RE = re.compile(
    r"(depends on which (specific )?(bill|law|legislation|act|drug|company|person|statute)|"
    r"(does not|doesn't|cannot|can't) (identify|determine|establish) which (specific )?"
    r"(bill|law|legislation|act|drug|company|person|statute)|"
    r"without (knowing|identifying) (what|which) (specific )?(bill|law|legislation|act|drug|company|statute)|"
    r"unclear which (bill|law|legislation|act)|which specific bill)", re.I)


def referent_punt(verdict: dict) -> bool:
    """Pure: the judge ruled not_scoreable because it could not tell WHICH
    bill/law/thing the claim means — a context failure, not a scoring one."""
    if not isinstance(verdict, dict) or verdict.get("verdict_state") != "not_scoreable":
        return False
    text = " ".join(str(verdict.get(k) or "") for k in ("verdict", "why_unverifiable"))
    return bool(_REFERENT_PUNT_RE.search(text))


REFERENT_REMINDER = (
    "REMINDER: you ruled this claim not_scoreable because you could not tell "
    "which bill, law or thing it refers to. The video names it. VIDEO CONTEXT: "
    "{context}. Judge the claim about THAT named thing from the evidence above "
    "and give a truth_score; if the evidence does not settle it, rule "
    "insufficient — not not_scoreable."
)


NO_REFUSAL_REMINDER = (
    "REMINDER: another desk declined this claim as outside its category. "
    "You may NOT decline it for category. Judge it from the evidence above "
    "as a general fact-checker would and give a truth_score unless the "
    "claim depends on a definition, a guilt gate, or a matter of taste."
)


def _judge_once(claim: dict, evidence: dict, reminder: str = "") -> dict:
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
               + (("VIDEO CONTEXT (same video — use it to resolve 'the bill', "
                   "'this law', 'he', 'it'): " + str(claim.get("video_context"))[:700] + "\n")
                  if claim.get("video_context") else "")
               + str(claim.get("claim", ""))[:500]),
        fact_checks=_format_fact_checks(evidence),
        web_sources=_format_web_sources(evidence),
        max_sources=MAX_KEY_SOURCES,
        search_rounds=evidence.get("search_rounds", 1) if isinstance(evidence, dict) else 1,
        posted_date=claim.get("posted_date") or "unknown",
        today=time.strftime("%Y-%m-%d"),
    )

    # PROMPT CACHING: everything before the claim block is identical for
    # every claim in a bucket (fleet rules + rubric, ~5k tokens). It goes
    # in a cached system block; only the claim + evidence are paid in full.
    rub_at = prompt.index("=== YOUR CATEGORY: ")
    claim_at = prompt.index("Claim (routed to ")
    rules_part = prompt[:rub_at]            # identical for EVERY category
    rubric_part = prompt[rub_at:claim_at]   # identical within a category
    dynamic_part = prompt[claim_at:]        # this claim + its evidence
    if reminder:
        dynamic_part += "\n\n" + reminder
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

    # v0.66.12 — THE LABEL FOLLOWS THE NUMBER (Inderpreet, Sep 22: a 7.8
    # ring was green while its chip said "partly supported"). One rule
    # for every card: 7.5 and up is SUPPORTED (the caveat lives in the
    # verdict sentence); below 7.5 is never "supported"; a "contradicted"
    # with a mid score is partly_supported. Provisional keeps its own
    # word (it is a cap, not a doubt).
    state = align_state(state, score)

    out = {
        "truth_score": score,
        "verdict_state": state,
        "verdict": str(data.get("verdict", "")).strip()[:500],
        "evidence_strength": strength,
        "key_sources": sources,
        "why_unverifiable": why,
    }
    wd = str(data.get("wrong_desk") or "").lower().strip()
    if wd in BUCKETS_FOR_REROUTE:
        out["wrong_desk"] = wd
    return out


def align_state(state: str, score) -> str:
    """Pure: make the verdict word agree with the score band the reader
    sees (ring colour: green >= 7.5, amber 4.0-7.4, red < 4.0)."""
    if score is None or state in NULL_SCORE_STATES or state == "provisional":
        return state
    if score >= 7.5:
        return "supported"
    if state == "supported":
        return "partly_supported"
    if state == "contradicted" and score >= 4.0:
        return "partly_supported"
    if state in ("partly_supported", "insufficient") and score < 2.6:
        return "contradicted" if state == "partly_supported" else state
    return state


BUCKETS_FOR_REROUTE = {
    "politics", "health", "science", "economy", "business", "technology",
    "law", "conflict", "education", "society_culture", "sports",
    "entertainment", "history_geography",
}
# a judge that refused for scope in prose (older replies, or a model that
# skipped the field) — caught by wording, not just by the field
_SCOPE_REFUSAL_RE = re.compile(
    r"(outside (this|the|my) (category|rubric|desk)|this category.s scope|"
    r"falls outside|not (a|an) (health|medical|political|legal|economic|"
    r"business|technology|science|sports|entertainment|historical) claim|"
    r"not (a|an) [^.;]{0,60}?\b(health|medical|political|legal|economic|business|"
    r"technology|science|scientific|sports|entertainment|historical) (or [a-z]+ )?claim|"
    r"wrong (category|desk)|not within (this|my) (category|scope)|"
    r"(horticultur|gardening|plant[- ]care)[^.;]{0,80}?\bnot (a|an) )", re.I)


def scope_refused(verdict: dict) -> bool:
    """Pure (unit-tested): did the judge decline the claim for being
    someone else's category rather than rule on it?"""
    if not isinstance(verdict, dict):
        return False
    if verdict.get("wrong_desk"):
        return True
    if verdict.get("verdict_state") not in NULL_SCORE_STATES:
        return False
    return bool(_SCOPE_REFUSAL_RE.search(str(verdict.get("verdict") or "")))
