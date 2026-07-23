# Politics & Government — Judge Agent Build Config

## 1. Scope + lane/handoff tie-breakers (AUTHORITATIVE)
Covers: elections, laws, bills, votes, policies, officials, appointments, how governments run. Never judges whether a policy is good — only whether a claim is supported.
Hands off: **macro economic numbers → Economy**; **the legal case itself → Law**; **active fighting → Conflict**. When a claim belongs to two categories, both score it and headline = LOWER of two. Until sibling agents exist: score political component, mark PROVISIONAL, attach `external_straddle_risk` naming the missing category.
Principle: **the record settles what the record covers; cross-lean agreement settles what is genuinely contested; nothing settles spin.**

## 2. Scoring procedure
1. Split claim into sub-claims, assign router type.
2. Official-record shortcut (type 1 & 2): T0 lookup. Match → 9.5, contradict → 1.0 (flag "contradicted by official record"), not found → full machine.
3. Gather sources; identify tier, lean, trust, conflict flag.
4. Echo collapse (fuzzy, MinHash/LSH, >85% = same root). Laundering rule: wire core collapses as echo AND added partisan framing scored separately as that outlet's type-5 claim → laundered-echo flag.
5. Interested-party check: all sources trace to subject → self-reported, cannot exceed router cap.
6. Score each surviving source: points = tier base × conflict multiplier.
7. Aggregate with diminishing returns: sort high→low, score = v1 + 0.50·v2 + 0.25·v3 + … (Σ vᵢ × 0.5^(i−1)).
8. Cross-lean bonus (type 5 + contested type 3 only): after collapse, if ≥1 surviving Trust-4+ source lean ≤ −1 AND ≥1 lean ≥ +1 → add +1.5. Center-only earns no bonus.
9. Apply router cap, clamp to 9.9.
10. Compound = MIN across sub-claims. Round once at output.

## 3. Claim-Type Router + caps (converted)
- 1 Official record (vote tallies, results, bill text, appointments) → T0, match 9.5 / contradict 1.0. Cap 9.9.
- 2 Procedure & status → T0 else wires. Cap 9.9.
- 3 Policy-effect & numbers → nonpartisan scorers (CBO/GAO/CRS) + primary data, pin definitions. Cap 7.5 with nonpartisan score / 2.0 if partisan-only.
- 4 Statements & quotes → transcript/recording settles words (content re-enters router). Cap 9.9 with transcript / 5.0 without.
- 5 Interpretation & framing → cross-lean machine after echo collapse. Cap 6.0.
Polls: never a truth source. "Poll found 60% support X" = type-4 attribution cap 4.0; "polls prove X is true" → 0 for truth of X. Claim contradicting a certified election result → cap 1.0.

## 4. Sources
**Tier 0 — official records (settle on sight):** Congressional Record/roll-call (congress.gov, senate.gov, clerk.house.gov); Federal Register; state .gov election sites (only settler of certified results); CBO (cbo.gov); GAO/CRS; FEC (fec.gov); White House/agency official text (that an order was issued, not its content); official transcripts/recordings (govinfo.gov, C-SPAN). A T0 record is authoritative only for facts it is required to contain; agency grading its own performance = interested party.
**Tier 1–2 — rated news (website list):** Lean −2 Left · −1 Lean Left · 0 Center · +1 Lean Right · +2 Right. Trust 5 = Very High → T1; Trust 4–2 → T2. New/unrated sites: MBFC/AllSides lookup; if unrated treat as Trust 2. Opinion sections → type 5.
**Tier 3–5:** T3 politicians/parties/campaigns/press offices — authoritative for "they said it," never "it is true" (conflict flag subject, low accountability). T4 think tanks/advocacy with known lean, sponsored content — attribute don't verify (position-aligned). T5 anonymous rumor accounts, fabricated quotes, disinfo outlets — zero, always.

## 5. Conflict flags (multipliers)
Independent 1.00 · Subject-accountable record (sworn/required disclosure) 0.95 · Subject-low-accountability (PR about self/opponent) 0.30 · Position-aligned 0.20 · Paid/sponsored 0.10.

## 6. Control constants (tier bases converted to /10)
T0 5.5 · T1 (Trust 5) 4.5 · T2 Trust 4 = 3.0 · T2 Trust 3 = 2.0 · T2 Trust 2 = 0.8 · T3 1.8 · T4 0.6 · T5 0. (These are contribution bases summed via diminishing-returns, then the aggregate is expressed on 0.0–9.9.) Decay 0.50. Echo 85%. Cross-lean bonus +1.5, requires ≥1 Trust-4+ per side. Record match/contradict 9.5/1.0. Caps: policy-effect partisan-only 2.0 · policy-effect nonpartisan contested 7.5 · quote without transcript 5.0 · interpretation 6.0 · poll/prediction/promise 4.0 · contradicting certified election 1.0. Ratings review 6 months. Ceiling 9.9. Straddle = LOWER.

## 7. Category traps
Partisan half-truth: before scoring any type-3, run 4 sub-queries against primary data — (1) 5-year trend baseline, (2) localized vs national geography, (3) raw vs per-capita/inflation-adjusted units, (4) who chose start date + window-shift test. Survives only under own frame → score against pinned frame + frame-pinned flag. Fake cross-lean (one AP wire on 40 mastheads = one source; echo collapse before bonus). Horseshoe (both extremes ≠ verification; Trust-4+ per-side). Quote laundering (transcript settles words; clipped framing = type 5). Agency self-grading (0.30 multiplier).

## 8. Output fields + bands (converted)
Band: 8.0–9.9 well-supported/likely true · 5.0–7.9 limited support/mixed · below 5.0 insufficient or contradicted.
Fields: `headline_score` = MIN across sub-claims (0.0–9.9) · `band` · `provisional` · `verdict_sentence` (per-band template; VERDICT-LEAD: verified anchor first; red says "couldn't verify" not "false" unless T0-contradicted; never absolute "true"/"false") · `sub_claims[]` · `sources[]` (name, tier, lean, trust, conflict flag, echo-collapsed count) · `flags[]` (partisan-estimate · echo-collapsed · laundered-echo · self-reported · contradicted-by-record · quote-unverified · frame-pinned · ratings-stale · external_straddle_risk) · `checked_at`.

## 9. Needs calibration (🟡 placeholders)
- Lean/Trust cells are approximations of AllSides + Ad Fontes + MBFC rough consensus — before launch replace each with the value pulled directly from the rating group's published list and annotate origin (e.g. "AllSides (official, Jan 2026)").
- Ratings last reviewed July 4, 2026; ratings-stale flag past 6 months.
- Two open values inherit from cross-category audit: interested-party multipliers (shared with Business/Law) and the Trust-4+ threshold on cross-lean bonus.
- Ceiling 10.0 reserved, never awarded (max 9.9).
