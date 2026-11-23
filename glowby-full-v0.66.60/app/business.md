# Business & Industry Judge — Distilled Config (Spec v2.2, 2026-07-04)

> Conversion: spec 0–99 → Glowby 0.0–9.9 (÷10, one decimal). Multipliers unchanged; compute in decimals, round once at output.

## 1. Scope & tie-breakers (AUTHORITATIVE)
- Owns: named companies, products, deals, M&A, executives, layoffs, market share, funding rounds, sector dynamics.
- Economy handoff: macro, markets, policy — "Tesla stock rose 5%" is Economy; "Tesla delivered 1.8M cars" is Business. A stock-moving earnings beat dual-tags: earnings verify here, market reaction in Economy; compound takes the LOWER.
- Law handoff: the legal case itself — guilt, rulings, sentences. Business scores that a suit EXISTS (court record); Law owns whether the conduct occurred.
- Technology handoff: the tech itself — specs, benchmarks, capabilities. The corporate move (launch, pricing, deal) stays here.
- Straddle handshake: score the business component, mark PROVISIONAL, attach external_straddle_risk. Straddle rule: LOWER of the two category scores.
- Principle: *coverage is not corroboration — ask who the surviving source IS before counting it.* Two-axis sourcing: reliability tier (accountability) × conflict flag (stake), reassigned per claim.

## 2. Scoring procedure (in order)
1. Normalize; split into sub-claims (own type, check, cap, score). Headline boasts are almost always type 3 or 9 — split first.
2. Entity resolution before evidence: exact legal entity, parent/subsidiary, ticker + exchange, jurisdiction, aliases, SPAC/shell status. Ambiguous → low confidence, entity-ambiguous flag, never a guess.
3. Metric-class gate, THEN filing-match shortcut: classify accounting_standard (GAAP, IFRS, Non-GAAP-Adjusted, self-defined). GAAP/IFRS figures: EDGAR/registry lookup — match 9.5, contradict 1.0, flagged. Non-GAAP in a filing gets the QUOTE treatment (reported-as-stated), never fact treatment; performance claims on self-defined metrics route to type 3, cap 2.0.
4. Verify the release is real: must exist on the company IR domain or as an 8-K before counting even as T3; unconfirmable → fake-release flag, unsupported.
5. Gather sources; assign tier and conflict flag. Page-level rule: the outlet name is not the unit of trust (Forbes staff T2; contributor/sponsored not).
6. Echo collapse, THEN interested-party check: collapse syndicated copies to one root (85% similarity incl. semantic copies, wire bylines, citations). If EVERY survivor collapses to the subject → self-reported, bound by the router cap.
7. Contribution = tier base × conflict multiplier.
8. Aggregate with diminishing returns: sort high→low; score = v1 + 0.50·v2 + 0.25·v3 + …
9. Apply router cap; clamp to 9.9. Compound = MIN across sub-claims.

## 3. Tiers & conflict multipliers
- Tier bases T0/T1/T2/T3/T4/T5 = 5.5, 4.5, 3.0, 1.8, 0.6, 0.0.
- Multipliers: Independent 1.00; Subject-accountable 0.95 (sworn/audited filings); Subject-low-accountability 0.30 (releases, IR decks, blogs, call statements); Position-conflicted 0.20; Paid-by-subject 0.10; Unverifiable 0.00.

## 4. Claim types & caps
1. Disclosed public-co fact — shortcut match 9.5, contradict 1.0; wrong period → cap 4.5 + period-mismatch flag.
2. Private-company fact — AUDITED-PRIVATE SHORTCUT: authenticated audited statement (named auditor, opinion letter, verifiable channel) anchors at 6.0; no-audit cap 6.0; company-only cap 2.5 (deliberately above the 2.0 performance cap).
3. Operational/performance ("#1", market share, self-defined DAU/MAU) — cap 2.0 without independent measurement; definition-pin the metric.
4. Deal/valuation — private paper valuation cap 6.0; single-party announcement cap 3.5; REGULATORY CONTINGENCY CHECK: announced/signed deal under active FTC/DOJ challenge → clamp 5.5 + deal-state-contingent flag until closing filings. Deal-state: announced ≠ signed ≠ approved ≠ closed.
5. Legal/conduct shell — record verifies EXISTENCE never guilt; cap 1.5 on party-PR only; conduct straddles to Law.
6. Product launch/availability — state-exact only (announced, preorder, beta, limited geo, GA); capability straddles Tech/Science.
7. Executive/labor event — 8-K officer change 9.5; cap 1.5 on anonymous rumor; layoff-state precision (planned, announced, WARN-filed, completed).
8. ESG — cap 3.5 unaudited company report; future target → type 9; climate mechanics straddle Science.
9. Forward-looking/strategy — unified fleet prediction cap 4.0; attribute, never verify.
10. Sector trend — cap 4.0 single anecdote; dataset-backed can exceed 8.0.

## 5. Evidence hierarchy
- T0: SEC EDGAR (10-K, 10-Q, 8-K, S-1, DEF 14A), audited financial statements, PACER/court records, USPTO, FTC, DOJ Antitrust, State AG filings, corporate registries (Companies House, SEDAR+, ASIC) routed by jurisdiction → regional_source_route.
- T1: Reuters, Bloomberg News, AP business desk, WSJ, FT, The Economist.
- T2: CNBC, Fortune, Forbes STAFF only, The Information, trade press, Crunchbase/PitchBook (aggregated self-reports — verify originals).
- T3: press releases, IR decks, earnings-call transcripts, company blogs (attribution only, never verification).
- T4: vendor-sponsored quadrants, paid wire distribution, self-published market-sizing, sell-side notes.
- T5: pump sites, anonymous forums, rumor aggregators (zero).
- Page labels classified before scoring: filing/official record, staff reporting, wire copy, contributor/opinion, sponsored/advertorial, company-owned, aggregator, anonymous/rumor.

## 6. Named traps (all actively checked)
Self-report laundering (signature trap); metric-definition trap; superlative ladder (verdict reports claimed rung vs measured gap); fake/fabricated releases (IR domain → EDGAR 8-K → then T3; paid wire proves distribution, not authenticity); allegation-as-fact trap; greenwashing/ESG; trade-press laundering route (trades keep Independent flag but NEVER qualify as independent measurement — cap 2.0 holds); entity & ticker confusion; wrong-period truth; restated financials (vintage rule: latest filing is the record); partnership theater (both-party confirmation or filing required); synthetic consensus (semantic echo matching); pump-and-short (attributed opinion; triggers the financial-advice guardrail).

## 7. Bands, states, output
- Bands: 8.0–9.9 green, 5.0–7.9 orange, below 5.0 red. Ceiling 9.9 (10.0 never awarded).
- verdict_state: filing-verified, filing-contradicted, independently-corroborated, self-reported-only, attributed-valuation, allegation-shell, forecast-attributed, unverified (+ straddle-provisional template).
- verdict_sentence: templates per state; "couldn't verify" never "false" except filing contradiction; no intent language ever; VERDICT-LEAD RULE — lead with what WAS verified, gap second; headline stays the MIN.
- Fields: headline_score = MIN across sub-claims (0.0–9.9); verdict_state; band; verdict_sentence; provisional; entity; sub_claims[]; filing_check (form, period, accession number, claimed vs filed value, accounting_standard); regional_source_route; source_panel[] (tier, PAGE LABEL, conflict flag, contribution, collapsed echoes + root); flags[] (self-reported-only, echo-collapsed, synthetic-consensus, fake-release, metric-self-defined, reported-as-stated, paper-valuation, period-mismatch, entity-ambiguous, deal-state-mismatch, deal-state-contingent, audit-unauthenticated, allegation-not-adjudicated, position-conflicted-source, market-moving-review, ratings-stale, external_straddle_risk); audit_trace_id; checked_at.
- Financial-advice guardrail (shared with Economy): fixed not-financial-advice line appended when a claim touches an investment/trading decision.
- Market-moving unverified claim → manual review before public display. Rating review interval 6 months → ratings-stale.

## 8. Needs calibration
No explicit 🟡 placeholders. Audit items: MBFC/NewsGuard cross-check of T1–T2 at ingest; financial-advice guardrail lawyer review (shared with Economy); escalation queue for market-moving claims, legal allegations, suspected fake releases.
