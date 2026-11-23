# Glowby Sorting Gate & Router — distilled config (from Spec v1.2, 2026-07-06)

> Score note: all scores converted from the spec's 0–99/0–100 convention to Glowby's 0.0–10.0 one-decimal scale. Probability-style confidences (e.g. 0.70) unchanged.

## 1. Purpose + position
Labels each incoming CLAIM: (1) gate — is there a real checkable assertion? (2) router — file it into one of 12 topic buckets. Never decides truth. Position: Media Ingest Layer → THIS STAGE → twelve category agents. Text enters directly; media arrives as claims[] from ingest.

**Claim-level rule:** categorize the CHECKABLE CLAIM, not the article/post/video. Split items into claim units FIRST; each claim gets its own bucket, optional second bucket, confidence, risk level, developing-story flag, audit record.

## 2. Processing steps (in order, cost order)
**Stage 0 — Claim gate:**
1. Known-satire list check (The Onion, Babylon Bee, …) → label satire, stop. Living list.
2. Satire beyond the list (satire gets reposted without its source; ingest defers ALL satire judgment here): parody-account markers, absurd framing, comedic context, source mismatch ("CNN" headline on no CNN property). Suspected-but-uncertain → conservative handling + review; never auto-cleared, never auto-"debunked".
3. Claim-type label (AI returns ONE label + confidence): **factual** (continue) · **opinion** · **satire** · **no-claim** · **prediction** (routable; label travels so the fleet prediction cap of 4.0 [spec: 40] applies from the start) · **personal experience** · **question** · **fiction/joke** · **advertisement**. Only factual and prediction move forward; the rest are tagged and parked, never "debunked."
   - PREDICTION-HORIZON check (beyond ~PREDICTION_HORIZON_MONTHS): three-way test, never automatic park — (a) scheduled/dated events route normally; (b) model-backed projections with authoritative forecast record (IPCC-, CBO-class) route as attributed projections; (c) unanchored speculation → unscoreable-speculation, parked at gate.

**Sorting flow:**
4. Split into claim units — **SEMANTIC UNITY RULE**: conditionals (if→then), comparative dependencies, and causal claims are preserved as ONE unit, routed by core action clause or dual-tagged, never cleaved into fragments (the categories' causal lanes score the LINKAGE).
5. Collect cheap signals (no AI): URL section/slug, headline + first paragraph, named entities, keyword scan vs each bucket's signal-word list → rough score per bucket.
6. Pick bucket: cheap signals clearly agree → assign, SKIP the AI. Shortcut formula (LOCKED, constants calibrate): eligible = (primary bucket keyword score ≥ MARGIN × runner-up) AND (entity confidence ≥ ENTITY_MIN). Otherwise AI call: headline + first para + entities + 12 bucket cards → single best bucket, confidence, one-line reason. Sort on what the claim is MAINLY about, not which words appear.
7. Confidence check: high → file. Torn between two → **dual-tag** (not a failure): feeds the **straddle machinery** — both categories score it; published **headline = LOWER of the two**. Low confidence, one clear guess → file + low-confidence flag. Fits nothing → **Other** bucket with RISK-TIED SLAs: critical/high risk OR developing → human escalation within 15 minutes; medium/low → weekly taxonomy batch.
8. Assign preliminary risk level: low · medium · high · critical (pattern-based; category agent refines). Risk drives display care and review routing, NEVER the truth score.
9. Set developing_story flag (elections, wars, court rulings, market moves, disasters, deaths, arrests): requires very recent sources downstream + schedules a recheck.
10. Save audit record (schema §6 below).

## 3. The 12 buckets (compressed cards)
1. **Politics & government** — elections, laws, policies, officials. Health/money-policy stays Politics if mainly the political fight.
2. **Health & medicine** — diseases, treatments, drugs, public health. Policy fights → Politics; diet/lifestyle → Society unless medical claim.
3. **Science & environment** — research, space, climate, energy. Medical study → Health; climate policy → Politics.
4. **Economy & finance** — broad economy, rates, inflation, central banks. One company → Business; budget politics → Politics.
5. **Business & industry** — specific companies, deals, executives. Economy-wide trends → Economy; tech product → Technology.
6. **Technology** — software, AI, gadgets, cybersecurity (the tech itself). Business move → Business; regulation → Politics.
7. **Law, crime & justice** — crimes, trials, courts, lawsuits. Policy-fight ruling → Politics; Law keeps case + legal process.
8. **Conflict, war & security** — wars, military, terrorism (the fighting/security side). Peace talks/diplomacy → Politics.
9. **Education** — schooling itself. University research finding → Science/Health.
10. **Society & culture** — social catch-all; if a clearer bucket fits, use it.
11. **Sports** — the games themselves. Stadium-funding fight → Politics/Business.
12. **Entertainment & media** — the works/people/culture side. Studio business deal → Business.
Plus **Other** — real claim, no bucket fits. Signal-word lists live in versioned taxonomy config; every misfile adds a word.

**Tie-breakers (Authority rule: the 12 category specs' lane/handoff tables are AUTHORITATIVE; on conflict, the spec's lane table wins):** Health policy vs medicine → law/funding/regulation = Politics, disease/treatment/safety = Health · AI company launch → what tech does = Technology, revenue/valuation = Business · Court ruling on election law → ruling/legal standard = Law, campaign impact = Politics · Climate law → science/effects = Science, bill/vote = Politics · Athlete injury → team/game impact = Sports, medical causation = Health · Genuinely both → dual-tag; straddle machinery scores both; headline = lower of the two.

## 4. Public safety (cross-tag, NOT a 13th bucket)
**public_safety_risk** rides on any claim: disasters, fires, crashes, evacuation orders, missing-person alerts, infrastructure failures. Disaster claims decompose into existing buckets.
**Emergency-instruction protocol (named critical protocol):** false evacuation/shelter/boil-water/all-clear = critical-risk BY DEFINITION. Verify ONLY against official emergency channels; while unverified NEVER amplified; verdict states only that no official instruction could be confirmed — never repeats the instruction as possibly valid.

## 5. Human review (router reviews its OWN uncertainty)
Fires when: confidence < 0.70 with no clean dual-tag · risk_level = critical · satire suspected but unresolvable · claim lands in Other repeatedly · gate/router disagree. Topical triggers (elections, medical, deaths, war) deliberately NOT here — category review bands own those.

## 6. Audit record schema
item_id, claim_id, claim_text, gate_label, primary_bucket, secondary_bucket|null, confidence, risk_level, developing_story, public_safety_risk, reason_for_bucket, signals_used, model_version, taxonomy_version, human_review_required.

## 7. Knobs
- Confidence threshold (auto-file vs review): **0.70**
- Cheap-signal shortcut: MARGIN, ENTITY_MIN (see Needs calibration)
- Risk-pattern list (elevates preliminary risk): medical instruction · election mechanics · emergency instruction · named-person accusation · financial urgency
- Other-bucket SLA: critical/high or developing → 15 min escalation; else weekly batch
- Developing-story sweep: live-stream/breaking wire → 15-min sweep; standard article on ongoing situation → 2–4 h (source-type-driven; category recheck logic layers on top)
- taxonomy_version: versioned, carried in every audit record
- MAX_CLAIMS_PER_ITEM: inherits ingest layer's knob (8); text items same cap
- Fleet prediction cap: **4.0** (spec: 40; converted to 0.0–10.0 scale) — applied via the gate's prediction label

## Builder rules
No truth scores or source ratings here — labels, risk, routing only. Load tie-breakers from category specs' config. Dual-tag feeds straddle machinery (headline = lower; both in detail drawer). Audit records drive the improvement loop.

## Needs calibration (🟡)
- MARGIN = 2.5 🟡
- ENTITY_MIN = 0.85 🟡
- PREDICTION_HORIZON_MONTHS = 12 🟡
