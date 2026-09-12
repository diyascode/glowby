"""
Weekly flag review — the judge of the judges.

Readers tap Harsh or Wrong under a score (and can flag a single claim).
Once a week (or on demand from the admin page) this agent takes every
unreviewed flag, pulls the full record behind it — the claim, the
verdict, the evidence the judge actually had, the reader's note — and
asks the strongest available model a fixed set of questions:

  1. Was the score defensible ON THAT EVIDENCE?
  2. If not: which existing rule was misapplied, or what new rule would
     have prevented it?
  3. Is this a rule fix, an evidence-search gap, a product idea, or a
     case where the reader is simply wrong?

It writes one short entry per flag plus a summary, and saves the review.
It only PROPOSES: it never changes a score, a stored result, or a rule.
The humans (Diya + Claude in the build sessions) decide what becomes a
rule — that is why the proposed rule is written in the rulebook's own
plain-language form, ready to paste.

Model: GLOWBY_REVIEW_MODEL (default claude-fable-5-1, the strongest tier;
~7 cents per flag). Falls back to the Sonnet judge model if the account
cannot use it. Runs against the same daily budget as everything else.
"""

import json
import os
import re
import time

REVIEW_MODEL = os.environ.get("GLOWBY_REVIEW_MODEL", "claude-fable-5-1")
FALLBACK_MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_FLAGS_PER_RUN = int(os.environ.get("GLOWBY_REVIEW_MAX_FLAGS", "60"))
COST_PER_FLAG_EST = {"claude-fable-5-1": 0.07}  # others ~0.02

ASSESSMENTS = ("score_was_right", "rule_fix", "evidence_gap", "product_idea", "cannot_tell")

PROMPT = """You are the review judge for Glowby, a fact-checking service for \
social-media videos. A reader flagged one of Glowby's scores as {kind}. \
Your job is to decide whether the reader has a point — judged ONLY on the \
evidence Glowby actually had at the time — and, if so, what would have \
prevented the miss. You only propose; humans decide.

Glowby's scale: truth score 0.0-9.9. States: supported (8.0-9.9), \
partly_supported, provisional (credibly reported, not yet confirmed), \
insufficient (evidence does not support, 2.0-4.9), contradicted (0.0-2.5), \
unverifiable / not_scoreable (no number). Headline = the lowest central \
claim, with floors: a wrong side detail caps at 7.5; an undisputed \
provisional claim cannot pull the headline below 6.0.

Glowby's shared rules include, among others: absence is not contradiction; \
silence can speak (big-news claims with no trace go low); burden of proof \
on assertions; thin is not false (undisputed thin evidence = 5.0-6.5); \
temporal fairness and monotonic tallies (right when posted = 6.0-7.5); \
the right ballpark is not a lie; rounding is not an error; announced is \
not predicted; judge the claim's own arithmetic; consensus over single \
voice; corroboration counts; contested claims pin to 4.5-5.5; AI-generated \
footage caps depiction claims at 5.5; same-video evidence counts.

=== THE FLAG ===
Reader's tap: {kind}{claim_note}
Reader's note: {note}

=== THE VIDEO ===
Title: {title}
Headline score: {headline_score} — "{headline_label}"

=== THE CLAIM(S) UNDER REVIEW ===
{claims_block}

Answer with ONLY a JSON object (no prose, no code fences):
{{"assessment": "score_was_right" | "rule_fix" | "evidence_gap" | "product_idea" | "cannot_tell",
 "reader_has_a_point": true | false,
 "reasoning": "2-4 plain sentences a teenager understands: what the evidence supported, what the judge did, and whether the reader is right",
 "misapplied_rule": "name of the existing rule that was misapplied, or null",
 "proposed_rule_name": "SHORT NAME IN CAPS or null",
 "proposed_rule": "the rule in Glowby's plain-language form (one paragraph, starts with the name, states the boundary and the score band), or null",
 "fair_score_estimate": 7.5 or null,
 "confidence": "high" | "medium" | "low"}}

Rules for you: never propose loosening a safety rule (public-safety, \
medical harm, accusations of crimes). "evidence_gap" means the sources \
Glowby needed exist but its search did not find them. "product_idea" is \
for flags that reveal a missing feature rather than a wrong score. Say \
"score_was_right" plainly when the reader is wrong."""


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _claims_block(result: dict, claim_idx) -> str:
    claims = result.get("claims") or []
    if claim_idx is not None and 0 <= claim_idx < len(claims):
        picked = [(claim_idx, claims[claim_idx])]
    else:
        picked = [(i, c) for i, c in enumerate(claims) if c.get("verdict")][:6]
    out = []
    for i, c in picked:
        v = c.get("verdict") or {}
        ev = c.get("evidence") or {}
        srcs = []
        for w in (ev.get("web_sources") or [])[:6]:
            srcs.append(f'  - {w.get("source", "?")} [{w.get("stance", "?")}]: "{(w.get("quote") or "")[:220]}" — {w.get("url", "")}')
        for f in (ev.get("fact_checks") or [])[:3]:
            srcs.append(f'  - FACT-CHECK {f.get("publisher", "?")} rated "{f.get("rating", "?")}" — {f.get("url", "")}')
        out.append(
            f"Claim {i + 1} (category {c.get('bucket', '?')}, central={c.get('central', True)}, risk={c.get('risk_level', '?')}):\n"
            f'  "{c.get("claim", "")}"\n'
            f"  Verdict: {v.get('verdict_state')} · score {v.get('truth_score')} · strength {v.get('evidence_strength')}\n"
            f"  Judge's sentence: {v.get('verdict', '')}\n"
            f"  Evidence the judge had ({len(srcs)} items):\n" + ("\n".join(srcs) if srcs else "  (none)")
        )
    return "\n\n".join(out) if out else "(no judged claims stored)"


def parse_review(raw: str) -> dict | None:
    """Pure (unit-tested): the model's JSON -> validated dict or None."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b == -1 or b < a:
            return None
        text = text[a:b + 1]
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    a = str(d.get("assessment", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if a not in ASSESSMENTS:
        a = "cannot_tell"
    est = d.get("fair_score_estimate")
    try:
        est = None if est is None else round(max(0.0, min(9.9, float(est))), 1)
    except (TypeError, ValueError):
        est = None
    conf = str(d.get("confidence", "")).lower()
    return {
        "assessment": a,
        "reader_has_a_point": bool(d.get("reader_has_a_point")),
        "reasoning": str(d.get("reasoning") or "")[:900],
        "misapplied_rule": (str(d.get("misapplied_rule"))[:120] if d.get("misapplied_rule") else None),
        "proposed_rule_name": (str(d.get("proposed_rule_name"))[:80] if d.get("proposed_rule_name") else None),
        "proposed_rule": (str(d.get("proposed_rule"))[:1200] if d.get("proposed_rule") else None),
        "fair_score_estimate": est,
        "confidence": conf if conf in ("high", "medium", "low") else "low",
    }


def review_one(flag: dict, result: dict, client=None, model=None) -> dict:
    """One flag -> review entry. Never raises; a failure is typed."""
    client = client or _client()
    if client is None:
        return {"error": "no ANTHROPIC_API_KEY"}
    model = model or REVIEW_MODEL
    rep = result.get("report") or {}
    idx = flag.get("claim_idx")
    prompt = PROMPT.format(
        kind=flag.get("kind", "harsh").upper(),
        claim_note=(f" (on claim {idx + 1})" if isinstance(idx, int) else " (on the whole video)"),
        note=(flag.get("note") or "(none)")[:300],
        title=(result.get("title") or "")[:160],
        headline_score=rep.get("headline_score"),
        headline_label=(rep.get("headline_label") or "")[:200],
        claims_block=_claims_block(result, idx if isinstance(idx, int) else None),
    )
    used = model
    for attempt, m in enumerate((model, FALLBACK_MODEL)):
        try:
            msg = client.messages.create(model=m, max_tokens=900, temperature=0,
                                         messages=[{"role": "user", "content": prompt}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            parsed = parse_review(raw)
            used = m
            if parsed is None:
                return {"error": "unreadable review", "model": used}
            parsed["model"] = used
            return parsed
        except Exception as e:
            err = str(e)
            # model not available to this key -> try the fallback once
            if attempt == 0 and any(t in err.lower() for t in ("not_found", "not found", "permission", "model", "403", "404")):
                continue
            return {"error": err[:200], "model": m}
    return {"error": "review failed", "model": used}


def summarize(entries: list) -> dict:
    counts = {k: 0 for k in ASSESSMENTS}
    counts["error"] = 0
    for e in entries:
        r = e.get("review") or {}
        if r.get("error"):
            counts["error"] += 1
        else:
            counts[r.get("assessment", "cannot_tell")] += 1
    proposals = [e for e in entries if (e.get("review") or {}).get("proposed_rule")]
    line = (f"{len(entries)} flag(s): {counts['rule_fix']} rule fix, {counts['evidence_gap']} evidence gap, "
            f"{counts['product_idea']} product idea, {counts['score_was_right']} score was right, "
            f"{counts['cannot_tell']} unclear" + (f", {counts['error']} failed" if counts['error'] else ""))
    return {"counts": counts, "summary": line, "proposals": len(proposals)}


def run_review(flags: list, load_result, client=None, model=None) -> dict:
    """flags: list of feedback rows (id, url_key, kind, claim_idx, note).
    load_result(url_key) -> stored result dict or None. Returns the full
    review document (entries + summary + model + timing)."""
    t0 = time.time()
    entries = []
    client = client or _client()
    for f in flags[:MAX_FLAGS_PER_RUN]:
        res = None
        try:
            res = load_result(f.get("url_key") or "")
        except Exception:
            res = None
        if not res:
            entries.append({"flag": f, "review": {"error": "stored result not found"}})
            continue
        rv = review_one(f, res, client=client, model=model)
        entries.append({"flag": {"id": f.get("id"), "url_key": f.get("url_key"), "kind": f.get("kind"),
                                 "claim_idx": f.get("claim_idx"), "note": f.get("note") or "",
                                 "title": (res.get("title") or "")[:120],
                                 "score": (res.get("report") or {}).get("headline_score")},
                        "review": rv})
    used_models = sorted({(e["review"] or {}).get("model") for e in entries if (e["review"] or {}).get("model")})
    doc = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "model_requested": model or REVIEW_MODEL, "models_used": used_models,
           "entries": entries, "seconds": round(time.time() - t0, 1)}
    doc.update(summarize(entries))
    per = COST_PER_FLAG_EST.get(doc["model_requested"], 0.02)
    doc["est_cost"] = round(per * sum(1 for e in entries if not (e["review"] or {}).get("error")), 2)
    return doc
