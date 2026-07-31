# Glowby Output Agent — distilled config (from Spec v1.2, 2026-07-06)

> Score note: all scores converted from the spec's 0–99 to Glowby's 0.0–10.0 one-decimal scale (99 → 9.9, 80 → 8.0, 50 → 5.0).

## 1. Purpose + position
Final stage: turns all pipeline output into ONE card. Computes NOTHING about truth — display layer AND enforcement point for every wording/do-not-amplify/display rule. Position: Ingest → Router → 12 categories → THIS AGENT → user.
**Naming decision (Option A):** the number is a CONFIDENCE score; Glowby never says "true"/"false" — only how well-supported a claim is.

## 2. Bands (LOCKED; converted from 0–99)
- GREEN **8.0–9.9** "Well-supported" · ORANGE **5.0–7.9** "Limited support / mixed" · RED **< 5.0** "Insufficient — or contradicted". Ceiling 9.9, never 10.0. Two situations share red; WORDING distinguishes: "insufficient" = could not verify; "contradicted" = record/consensus refutes (reason_code-driven; record-/consensus-contradiction caps at **1.0–2.5**, spec: 10–25).

**Band mechanics inherited:** headline = **MIN across scoreable sub-claims**; **straddles publish the LOWER of the two categories**; compound claims never average; ceiling and category caps arrive already applied — this agent never recomputes a number, ever.

## 3. Display depth
Six public sub-bands REJECTED. Three layers: (1) band, (2) exact number always shown beside band, (3) verdict state (the real sub-structure: Health 6, Law 8, Conflict 8 states). Internal analytics tiers kept, NEVER rendered: **0.0–1.9 · 2.0–3.4 · 3.5–4.9 · 5.0–6.4 · 6.5–7.9 · 8.0–9.9** (converted).

## 4. Neutral lane (most important display rule)
Every null-score verdict renders NEUTRALLY — never red, never zero. **Null is never zero; "not established" is never "false."** Canonical: headline_score: null, band: "neutral".
- NOT SCOREABLE (unadjudicated guilt, normative claims) → "Not established — that determination belongs to the court" / "value judgment, not a checkable claim"
- DEPENDS ON DEFINITION (superlatives) → definition panel (each standard, authority, answer — no single number)
- UNSCOREABLE (gossip, privacy-gated) → "Personal claim, no reliable basis"
- unscoreable-speculation (gate's prediction-horizon park) → "far-future speculation with no verifiable basis today"
- Grey lane (opinion · satire · no-claim · question · fiction · ad) → tagged and parked, never "debunked"; satire labeled satire, not scored.
**Extraction rule:** an opinion wrapper hiding a checkable claim → park the opinion AND extract the assertion; opinion label + scored verdict side by side, separated.

## 5. Two-lane card (ingest R5 made visual)
Claim lane and authenticity lane render side by side, never blended — "claim well-supported + image AI-generated" must display without contradiction. Authenticity: confirmed/labeled → BIG "AI" badge + AI-Likelihood + reason · clean/suspicious → number only + reason · not_assessed → NOTHING (blank = "we didn't check," never "genuine"; no green checkmark for authenticity).

## 6. Card structure
1. **Headline:** band + exact number + one-line verdict; MIN across scoreable sub-claims, headline-driver identified. **SAFETY-COLLAPSE OVERRIDE:** safety_display.level = STRICTEST across ALL sub-claims — headline RENDERING constrained even when the remaining MIN is green; the score never changes.
2. **Confirmed / not-established split:** what IS independently supported first, what is NOT established second — never one flat number.
3. **Sub-claims:** each with own band, number or neutral verdict, state, one-line reason; special verdicts alongside, never averaged in.
4. **Straddle drawer:** lower score is the headline; tap shows both categories' scores + one line of reasoning each.
5. **Timestamp:** checked_at on every card; "scheduled for recheck" whenever provisional / fog-immature / developing-story.

**Mobile-first:** first screen = band + number (or neutral) · one-line verdict · user takeaway · AI badge if applicable · checked_at. LAYOUT GUARD (TAKEAWAY_CHAR_BUDGET): character budget + wrap rules keep band label + number in the first viewport (cross-script names).
**User takeaway:** one plain sentence, ASSEMBLED FROM STRUCTURED FIELDS VIA TEMPLATES, never free-generated.
**Accessibility:** color never carries meaning alone — text label + number/neutral + icon; screen-reader order: takeaway → band+number → verdict → authenticity.
**Red sub-label, always visible:** "Insufficient support" vs "Contradicted by evidence" as text.
**why_not_higher / why_not_lower / what_would_change_this:** MECHANIZED — every reason_code/flag ID maps to a pre-vetted sentence template; explanation compiles from the active arrays (higher: caps/gaps applied; lower: anchors that held).

## 7. Verdict-language rulebook (lawyer reviews ONCE, here)
- Verdict-lead: verified part first, gap second — always.
- "Couldn't verify" is never "false" — except genuine record/consensus contradiction ("contradicted by the official record / current consensus", reason_code-driven).
- No intent language ever ("misleading," "lying," "deceptive"). Describe the GAP via the juxtaposition template: "the poster claims [X]; the original states [Y]".
- Presumption language: "charged with / accused of / alleged" absent adjudication; acquittals = "not proven," never "proven innocent"; "allegation pending investigation," never "war crime," until a body rules.
- Safety over score: display levels (full · limited · neutral-only · suppress) lower what is SHOWN, never the score.
- Attributed ranges for casualty/disputed figures ("MSF estimates at least…"); as-of dating inside the sentence for anything that moves.
- Do-not-amplify at render: never restate stigmatizing wording; unverified atrocity/death/emergency claims never repeated as likely; victim names, minors, POW imagery, addresses, sealed records suppressed; geospatial freeze strips precise coordinates; unverified emergency instructions never displayed as possibly valid ("no official instruction could be confirmed").
- **Do-not-intensify:** never rewrite a claim STRONGER/more specific/more viral/more defamatory; paraphrases only soften or preserve; quote rather than paraphrase contested wording; claim_text shown as made.

**Locked base templates:** Well-supported: "Official records and independent reporting support the main claim as of [date]." · Limited/mixed: "Some evidence supports the claim, but key details are not yet independently confirmed as of [date]." · Insufficient: "Glowby could not find reliable evidence supporting this claim as of [date]." · Contradicted: "Official records or current consensus contradict this claim as of [date]." · Neutral: "This is a value judgment, question, satire, fiction, or otherwise not a checkable factual claim."

## 8. Info button
Source panel with ECHO-COLLAPSE display ("40 outlets, tracing to 1 original source") · category panels when provided (alignment/definition/evidence/insider/representativeness) · fog/provisional status · what-would-change-this (lift_conditions) · reason code + audit_trace_id · authenticity signals drawer.

## 9. Output schema + fallbacks
Renders ONLY from structured fields — never invents, recomputes, or free-generates (verdict_state × band → template ID → filled template). Card object: card_id · claim_text (as made) · headline{band, score (NULL for neutral — never zero), label, headline_driver_claim_id, verdict_sentence, user_takeaway} · subclaims[]{claim_id, text, band, score|null, verdict_state, reason_code, one_line_reason} · authenticity_lane{state, ai_likelihood|null, show_big_ai_badge, reason} · safety_display{level, suppression_reasons[]} · score_explanation{why_not_higher, why_not_lower, what_would_change_this} · checked_at · recheck_status · correction_status · cache_control_ttl · template_id · audit_trace_id. Log template ID + every suppression in the audit trail.
**Fail-safe fallbacks:** missing headline score → NEUTRAL "pending review" · missing authenticity → blank lane · missing safety level → SAFER level (limited beats full) · missing panels → "details unavailable"; the headline never blocks.
**Share-card rule:** exports always carry claim text, band + label, score, verdict, checked_at, authenticity state — non-croppable; tamper-EVIDENT QR/verification code → audit_trace_id resolving to the live card (SHARE_CRYPT_SIGNATURE).
**Correction path (day one):** report/flag control on every card; visible correction notice + correction_status. Evidence-challenge workflow and refutation machine = future (red wording split works today via reason_code).

## 10. Knobs
- Band boundaries LOCKED: green 8.0–9.9 · orange 5.0–7.9 · red < 5.0 (converted)
- CACHE_CONTROL_TTL: developing_story high-volatility → 15-min enforced client refresh · standard 🟡
- Verdict templates: generated from the §7 rulebook per band × state; template IDs logged
- Pre-launch eval: 100% on neutral rendering, two-lane independence, wording audit, safety levels, fallbacks, do-not-intensify, accessibility, share exports, safety-collapse, cross-script layout; ≥95% contradicted-vs-insufficient + multi-claim; ≥90% echo-collapse + human review.

## Needs calibration (🟡)
- CACHE_CONTROL_TTL standard value 🟡 · TAKEAWAY_CHAR_BUDGET per script family 🟡 · internal analytics tiers 🟡 (adjust freely)
- Band-boundary calibration vs human judgment + template tone-testing (needs the eval set)
