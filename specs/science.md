# Science & Environment — Judge Agent Build Config

## 1. Scope + lane/handoff tie-breakers (AUTHORITATIVE)
Owns: climate science, weather records, emissions measurements, energy technology, biodiversity, conservation, space/astronomy, physics, chemistry, ecology, geology, environmental incidents, non-medical research findings.
- **Health handoff**: human disease, treatment, vaccines, medication, clinical trials, nutrition, public-health advice.
- **Politics handoff**: regulation, legislation, climate policy, public spending, treaties, officials, political blame.
- **Business handoff**: a company's claims about its own emissions, ESG, product efficiency, greenwashing, lawsuits.
- **History & Geography handoff**: definition-heavy superlatives (longest river, largest desert, first discovery), historic events, border disputes — shared definition-pin gate.
Straddle handshake: score science component, mark PROVISIONAL, attach `external_straddle_risk`. Straddle rule = LOWER of two.
Principle: **count distinct roots, not leaves** — 40 articles about one study are one piece of evidence.

## 2. Scoring procedure (evidence-counting machine, cost order)
1. Normalize + split into sub-claims → normalized claim object; route by type.
2. Known-contradicted check (cheap): clamp to ConsensusCap 2.0.
3. Synthesis-settled shortcut (type-1): direct match to current Tier-1 synthesis → 9.5 without the curve. Contradiction → consensus cap.
4. Record-lookup shortcut (type-2 data): agency datasets. Match → 9.5, contradict → 1.0.
5. Gather sources, identify tier.
6. Echo collapse: shared DOI / links / fuzzy ≥85% → independence flag 0 on duplicates. No-primary-source piece cannot count as independent.
7. Retraction & correction check (Retraction Watch/Crossref): retraction → zero everything resting on it; expression of concern → down-weight.
8. Score each surviving source: contribution = tier weight × quality multiplier × retraction multiplier × independence flag.
9. Aggregate on saturating curve: E = Σ contributions; **raw = 9.9 × E / (E + K), K = 20**. (Curve constants operate on the raw evidence sum; final result expressed /10.)
10. Apply caps; clamp to 9.9.
11. Compound = MIN across sub-claims. Compute in decimals, round once.

## 3. Claim-Type Router + caps (converted)
- 1 Settled science → synthesis-settled shortcut 9.5; contradiction → consensus cap 2.0. Cap 9.9.
- 2 Measurement/data fact → agency lookup, match 9.5 / contradict 1.0. Cap 9.9.
- 3 New research finding → single-study cap 5.5, preprint cap 4.0; retraction + causal-gap check.
- 4 Causal & impact claim → cap 7.5 without causal-grade evidence; association evidence can never carry a causation claim past supported-with-limits.
- 5 Projection/model → cap 6.0; a projection is not a fact.
"New study proves X causes Y" = types 3 AND 4 stacked; both caps apply, compound takes MIN.

## 4. Source table (auto-detected from live DBs) — tier weights
Weights (feed the curve, not /10): T1 40 · T2 25 · T3 20 · T4 8 · T5 3 · T0 0.
- **T1** Cochrane, IPCC (ipcc.ch), National Academies, review/meta-analysis in Q1 journals — settled/synthesized science. Detected: DOI→review type via Crossref, or curated IGO list.
- **T2** nature.com, science.org, cell.com, pnas.org, thelancet.com, GRL, ES&T — original peer-reviewed research. Valid DOI + DOAJ + above SJR 25th percentile; ×journal-quality multiplier 0.5–1.2. Guards: Retraction Watch Hijacked Journal Checker + predatory watchlists → demote to T0 (rapid-drop rule).
- **T3** nasa.gov, noaa.gov, usgs.gov, epa.gov, nih.gov, cdc.gov, who.int, wmo.int, iucn.org — primary data/official statements. Domain on curated .gov/IGO allowlist; distinguish data from policy framing.
- **T4** Scientific American, Quanta, MIT Tech Review, science desks of NYT/Guardian/BBC — accurate relay only, counts only if it links a primary/DOI source and isn't echoing a counted original.
- **T5** general reputable outlets without science desk — very weak relay; pure echo → 0.
- **T0** advocacy NGOs (environmental OR industry), blogs, content farms, unrated — 0, flagged, never counted. Advocacy symmetry: agenda not conclusion zeroes weight.
Global source routing: T3 allowlist routes by location_scope (Met Office, IMD, JMA, EEA...); expands WHERE, never lowers the bar.

## 5. Control constants (converted where /10)
Quality multiplier 0.5–1.2 (default 1.0). SJR floor 25th percentile. Retraction multiplier 0. Independence flag 0 for echo. Echo threshold 85%. Saturation K = 20 (operates on evidence sum). Consensus-contradiction cap 2.0. Known-contradicted clamp 2.0. Single-study cap 5.5. Preprint cap 4.0. Causal-without-causal-evidence cap 7.5. Projection/model cap 6.0. Causal-gap trigger ≥ 2 ladder steps. Record match/contradict 9.5/1.0. Synthesis-settled 9.5. Ratings review interval 6 months. Source-diversity floor for green: ≥2 independent roots (exempt: type-1 synthesis-settled, type-2 record). Manipulated-graphic cap 1.0 (CONFIRMED by media-check specialist only). Ceiling 9.9. Straddle = LOWER.

## 6. States/bands (converted)
Public: green 8.0–9.9 · orange 5.0–7.9 · red below 5.0. Internal verdict language: strongly-supported / supported / mixed-emerging / weak / unsupported.
`verdict_state`: strongly-supported · supported · supported-with-limits · mixed-emerging · weak · unsupported-contradicted · record-settled.

## 7. Category traps (must actively check)
University press-release problem (trace past release to paper; mismatch → press-release-inflation). Causal gap (ladder: causes > increases risk > associated > correlates > projects/simulates; verb ≥2 above endpoint → supported-with-limits). Single-study syndrome. Superseded science (current edition wins; else superseded-source). Cherry-picked windows (full-record trend, window-shift, raw vs adjusted). Record-source mismatch (prefer dataset over screenshots). AI-generated content (every citation must RESOLVE in Crossref/DOAJ; else fabricated-citation, treated unsupported). Fabricated graphics → route image to media-check specialist; graphic-only = unverified until it returns; only CONFIRMED manipulation forces cap 1.0.

## 8. Output fields
`headline_score` = MIN across sub-claims (0.0–9.9) · `verdict_state` · `band` · `verdict_sentence` (VERDICT-LEAD; no "false"/"misleading") · `provisional` · `sub_claims[]` · `evidence_roots[]` (DOI/dataset ID, tier, quality multiplier, retraction status, supports/contradicts/context) · `source_panel[]` (incl. "+N copies dropped", flagged T0) · `causal_gap` · `data_nature` (observational · experimental_rct · computational_model · paleoclimate_proxy · historical_instrumental) · `regional_source_route` · `audit_trace_id` · `flags[]` · `checked_at`.

## 9. Needs calibration (🟡 placeholders)
- T4 domains rated reliable in NewsGuard/MBFC/Wikipedia perennial-sources — "seeded, approximate, verify against live ratings before launch."
- Ratings last reviewed July 4, 2026; store last-reviewed per row; ratings-stale past 6 months.
- Ceiling 10.0 reserved, never awarded (max 9.9).
