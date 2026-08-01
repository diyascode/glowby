# Economy & Finance — Judge Agent Build Config

## 1. Scope + lane/handoff tie-breakers (AUTHORITATIVE)
Owns: macro statistics (jobs, inflation, GDP), interest rates and central-bank actions, market prices/indexes, currencies, trade, government budgets/debt, global economic aggregates.
- **Business handoff**: a NAMED company's own numbers/deals/products/layoffs — "Tesla delivered 1.8M cars" is Business; "Tesla stock rose 5%" is Economy. Earnings-moving-markets dual-tag.
- **Politics handoff**: the policy fight around the numbers (credit/blame, whether a bill should pass). Number is Economy; blame is Politics.
- **Law handoff**: fraud charges, enforcement actions, insider-trading — the case itself.
Straddle: score economic component, mark PROVISIONAL, attach `external_straddle_risk`; LOWER of two.
Principle: **the release settles the number; cross-lean agreement settles the story; nothing settles a forecast.** A weak outlet is right if the number matches; a great outlet wrong if it doesn't.

## 2. Scoring procedure (cost order)
1. Normalize + split into claim object (metric, period, geography, units, verb); route by type.
2. Record-lookup (data): trace number to primary release. Match → 9.9, contradict → 1.2. Not locatable → SourceScore × link factor.
3. Vintage check: data gets REVISED (ALFRED archives vintages). Score against CURRENT vintage; initial-print claim contradicted by revision → score revision + revised-data flag, both numbers shown.
4. Definition-pin/spin scan on every data claim (§6).
5. Echo collapse (85%).
6. Forecast lane: never verified, only attributed. Score = MIN(forecast cap, SourceScore); names forecaster as projection.
7. Interpretation lane: cross-lean on survivors (Politics lean data): both leans ×1.00 · single ×0.78 · contested ×0.55.
8. Aggregate, cap, clamp 9.9. Compound = MIN across sub-claims. Round once.

## 3. Claim-Type Router + caps (converted)
1 Official statistic → primary release, match 9.9 / contradict 1.2 · vintage · definition-pin. Cap 9.9. 2 Market data → exchange/provider lookup; TIMESTAMP RULE (date + trading window: intraday/close/pre/after-hours + timezone, else stale-market-claim). Cap 9.9. 3 Company financial → STRADDLE with Business (SEC filing, dual-tag, lower-of-two). Cap 9.9. 4 Forecast → never verifiable; Score = MIN(cap, SourceScore); cap 4.0 (unified with Politics). 5 Interpretation → cross-lean after echo collapse; SourceScore × multiplier. 6 Causal economic → requires multiple high-quality studies or official analysis (CBO, Fed research, peer-reviewed); Science causal-gap ladder (caused > contributed to > coincided with > modeled). Cap 6.5 without support.
"Booming economy added 250k jobs, proving the plan works" = types 1 + 5; compound takes MIN.

## 4. Source table — two-layer (Layer 1 = WHO published; Layer 2 = router checks WHAT the doc does)
SourceScore = Tier base + MBFC + NewsGuard adj − spin/COI penalty, clamped 0–9.9.
Tier bases (/10): T0 primary record 9.9 · T1 international official body 9.4 · T2 market/pro data provider 9.0 · T3 top-tier financial press 8.0 · T4 general business media 6.4 · T5 opinion/advocacy/UGC 2.6.
- T0: BLS, BEA, Census, CBO, Federal Reserve, FRED/ALFRED, ECB, Bank of England, SEC EDGAR.
- T1: IMF, World Bank, OECD, Eurostat, BIS.
- T2: Bloomberg (data), LSEG/Refinitiv, S&P Global, Morningstar.
- T3: Reuters, WSJ, FT, Economist, Bloomberg News (8.9).
- T4: CNBC/MarketWatch 6.7; Barron's 7.3; Forbes/Business Insider/Yahoo Finance 6.0.
- T5: Seeking Alpha 1.2; ZeroHedge 0.
Adjustments: MBFC High/Mostly Factual/Mixed/Low = +0.6/+0.2/−0.8/−1.8 · NewsGuard 90–100/60–89/<60 = +0.3/0/−1.0 · Spin Low/Medium/High = 0/−0.6/−1.4.
Global source routing: by geography (Canada→StatCan/BoC · UK→ONS/BoE · India→RBI/MOSPI · EU→Eurostat/ECB · Japan→Stats Bureau/BoJ · China→NBS/PBOC + transparency caveat) → `regional_source_route`. T0–T2 carry no MBFC/NG (they ARE the records).

## 5. Control constants (tier bases §4, caps §3)
Data match/contradict 9.9/1.2. Primary-link present/absent ×1.00/×0.85. Forecast cap 4.0. Cross-lean 1.00/0.78/0.55. Causal cap 6.5. Echo 85%. Frame-pinned → pinned frame + flag. Revised-data → current vintage, show both. Ratings review 6mo. Ceiling 9.9. Straddle = LOWER.

## 6. Category traps + Special Risk Protocol
Spin (definition-pin: nominal vs real → nominal-not-real flag + fixed template; rate vs level; SA vs raw; total vs per-capita; window-shift). Revision trap (current vintage). Fake cross-lean via wire (collapse before multiplier). Cited-but-unlinked numbers (no-link ×0.85). Pump content (position-conflicted forecast: forecast cap AND T5 floor; conflict checks: asset ownership, affiliate/referral links, paid upsell, sponsored labels, token-founder, undisclosed positions, short-seller reports → position-conflicted-source flag). Doom content (attributed opinion). **Bank-stability (highest-risk lane)**: "Bank X insolvent/collapsing/freezing withdrawals" can CAUSE the harm; only primary support counts (FDIC, OCC, Fed supervision, NCUA, state regulators, bank's 8-K); absent → NOT VERIFIED + risk_level high + calm verdict. Scam-format (guaranteed/risk-free returns, limited-time windows, celebrity endorsements, crypto-wallet instructions, "send funds to unlock gains," AI-bot promises, fake stimulus) → fraud-pattern flag + "matches known scam formats" (description, never accusation).
Every high-risk result includes a factual risk_reason.
**Financial-advice guardrail (legal line):** whenever a claim involves an investment/asset/trading decision, append fixed line: "This is a credibility estimate of a public claim, not financial advice; investment decisions belong with a qualified adviser." Glowby never recommends money moves.

## 7. Output fields + bands (converted)
Band: 8.0–9.9 green · 5.0–7.9 orange · <5.0 red — state carried as verdict sentence.
`headline_score` = MIN across sub-claims (0.0–9.9) · `verdict_state` (record-verified · record-contradicted · revised-data · forecast-attributed · interpretation-supported · interpretation-contested · unverified) · `verdict_sentence` (VERDICT-LEAD anchor first; "couldn't verify" not "false" except record contradiction; forecasts = projections; no intent language) · `provisional` · `sub_claims[]` · `record_check` (series ID, vintage, claimed vs official, revisions) · `market_timestamp` · `regional_source_route` · `attribution_confidence` (detail-panel only) · `risk_level` + `risk_reason` (low/medium/high/severe; fires only on scam-format, bank-stability, pump/doom) · `source_panel[]` · `flags[]` (frame-pinned · nominal-not-real · revised-data · no-primary-link · stale-market-claim · position-conflicted-source · fraud-pattern · bank-stability-unverified · causal-overreach · ratings-stale · external_straddle_risk) · `audit_trace_id` · `checked_at`.

## 8. Needs calibration (🟡 placeholders)
- MBFC reliability + NewsGuard bands seeded from those datasets, approximate — refresh from live feeds at ingest (NewsGuard exact scores licensed, bands are placeholders). Spin/COI penalty is Glowby's own judgment column.
- Ratings last reviewed July 4, 2026; store last-reviewed per row; ratings-stale past 6 months.
- Ceiling 10.0 reserved, never awarded (max 9.9).
