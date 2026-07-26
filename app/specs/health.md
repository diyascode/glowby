# Health & Medicine — Judge Agent Build Config

## 1. Scope + lane/handoff tie-breakers (AUTHORITATIVE)
Covers: diseases, treatments, drugs, supplements, nutrition claims, public health, medical research.
Hands off: health **policy fights → Politics**; a pharma company's **business claims → Business**; **"new study shows" framing of non-medical science → Science**.
Straddle handshake: until those agents ship, score the medical component, mark headline PROVISIONAL, attach `external_straddle_risk` naming the missing category. Straddle rule = LOWER of the two category scores.
Defining principle: **evidence outranks source** — echo is not evidence; 50 sites repeating a claim that contradicts the evidence base count for nearly nothing.

## 2. Scoring procedure (cost order)
1. Normalize + split into sub-claims (preserve original wording), route each by type.
2. Red-flag fast-fail scan + known-contradicted list.
3. Record-lookup shortcut (registry-type claims).
4. Pillar 1 — Evidence strength (retrieve in strict order, stop early, score by **best level that actually supports it**, not the level promoters cite). Contradiction short-circuit.
5. Echo collapse + single-study cap (trace coverage to underlying studies; 85% similarity + DOI/PMID matching).
6. Pillar 2 — Consensus & convergence (independent bodies, different countries/funders).
7. Pillar 3 — Source reliability (supporting layer only, ±1.0, never overrides Pillars 1–2).
8. Combine: Pillar 1+2 set range, Pillar 3 adjusts within ±1.0; apply caps; clamp to 9.9. Compound claim = MIN across sub-claims. Compute in decimals, round once.

## 3. Score bands/states (converted to 0.0–10.0)
Public colors: green 8.0–9.9 · orange 5.0–7.9 · red below 5.0. State label does the fine work.
- **Well-supported** 8.0–9.9: strong evidence + international consensus.
- **Supported with limits** 6.0–7.9: evidence supports a NARROWER version than the wording (most common shape of health misinfo). Verdict: "the wording overstates the evidence."
- **Contradicted** 0.0–2.0: strong evidence/official records against. Only state that says more than "couldn't verify." Never phrased "misleading."
- **Mixed / contested** 4.0–5.5: real experts actively disagree.
- **Insufficient evidence** ≤4.0 + label: "not enough quality evidence yet." NOT the same as false.
- **Record-settled** 9.5 or 1.0: registry facts (type-1 shortcut).

## 4. Evidence hierarchy — Pillar 1 (GRADE-style, base scores /10)
- E1 systematic review / meta-analysis of RCTs (Cochrane) — 9.0
- E1g major clinical guideline / regulator label matching claim — 8.5–9.5
- E2 individual well-designed RCT — 7.5
- E3 large observational / cohort — 6.0
- E4 small study, case series, animal/test-tube — 3.5
- E5 mechanism reasoning — 2.0
- E6 anecdote, testimonial, single expert opinion — 1.0
Conflicting-evidence protocol: higher rank does NOT auto-win; weigh sample size/power, funding COI, claim type (for safety/harm, large real-world epidemiology carries extra weight). Surviving conflict → mixed/contested, never well-supported.

## 5. Source hierarchy — Pillar 3 (roles, not just tiers)
Reliability = tier base + transparency(5 checks ×0.8) − penalties, capped 9.9. Bands: 8.5–9.9 High-anchor · 6.5–8.4 Good · 4.5–6.4 Moderate-verify · 2.5–4.4 Low-caution · 0–2.4 Avoid.
Tier defs: 1 Evidence synthesis (Cochrane, NICE, UpToDate, PubMed = 9.9) · 2 Gov/intergov (CDC, NIH, FDA, WHO, NHS, EMA, MedlinePlus = 9.2) · 3 Academic center/society (Mayo, Hopkins, Harvard Health = 8.2; Cleveland Clinic, AAP = 7.4) · 4 Reviewed consumer publisher (Healthline, MNT, Verywell, Drugs.com = 5.5; WebMD = 4.7) · 5 Health journalism (STAT, KFF = 5.4) · 6 Flagged (Natural News, Mercola, GreenMedInfo, Health Impact News = 0).
Tier bases: 6.0/5.2/4.2/3.0/2.2/0.5. Penalties: COI −1.5/level; debunked −3.0; pseudoscience −4.0 (mixed −1.0); NewsGuard Red −2.0. Transparency +0.8 each (named author, review process, cites literature, dated, funding disclosed).
Roles (per claim): evidence (can move score) · record (settles registry facts) · context (explains, never overrides) · claim_origin (zero evidential weight). COI is two-level: site (sheet column) AND article (sponsored/advertiser content → COI 2 + claim_origin for that page).
Global source routing: search by geography (WHO ICTRP + ClinicalTrials.gov; national regulators). Expands WHERE, never lowers the evidence bar; grade universally on E1–E6.

## 6. Harm gates, caps, review triggers (converted)
- Red-flag: catastrophic single flag (page sells the thing; universal-efficacy "works for everyone/no side effects") → cap 2.0. Ordinary flags: 2+ → cap 2.0.
- Known-contradicted list (bleach/MMS, vaccine-autism, homeopathy for serious disease) → clamp 0.0–2.0, no further search.
- Contradiction short-circuit: high-level review/guideline against → clamp 0.0–2.0, skip remaining pillars.
- Record match/contradict → 9.5 / 1.0.
- Single-study cap (no replication/review) → 6.0. Preprint-only cap → 4.0. Retracted study + everything resting on it → 0.
- Type caps: Established reach 9.9; Genuinely contested cap 5.5; Insufficient evidence cap 4.0. Nutrition/lifestyle lane cap 7.5. Dosage/advice: matches guideline 9.5 / else 2.0. Poll/attribution not applicable here.
- `claim_strength_gap`: ordinal ladder steps (cures/reverses > prevents > treats > reduces risk > associated with). Gap ≥ 2 ladder steps triggers supported-with-limits.
- **Safety line** (fixed): "This is not medical advice; decisions about treatment belong with a qualified clinician." Appended whenever claim involves: stopping/changing prescribed treatment, dosage, replacing clinician therapy, pregnancy, infants/children, elderly, cancer, heart/kidney disease, psychiatric medication, vaccines, emergency symptoms. Stricter verdict language regardless of score. Glowby scores public claims, never personalized advice.

## 7. Output fields
`headline_score` = MIN across sub-claims (0.0–9.9) · `verdict_state` · `band` · `verdict_sentence` (VERDICT-LEAD: anchor first, gap second; never "misleading") · `provisional` · `sub_claims[]` · `evidence_roots[]` (DOI/PMID/registry ID, level, supports/contradicts/mixed/context, retraction status, author-COI) · `source_panel[]` (role, tier, band, conflict) · `claim_strength_gap` · `regional_source_route` · `audit_trace_id` · `flags[]` · `checked_at`.

## 8. Needs calibration (🟡 placeholders)
- Source-sheet stance & NewsGuard columns seeded from MBFC/NewsGuard — verify against live ratings before launch; store last-reviewed date per row; flag ratings-stale past 6 months.
- Ratings last reviewed July 4, 2026.
- Score ceiling 10.0 reserved, never awarded (max 9.9).
