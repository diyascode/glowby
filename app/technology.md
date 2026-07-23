# Technology Judge — Distilled Config (Spec v2.2, 2026-07-05)

> Conversion: spec 0–99 → Glowby 0.0–9.9 (÷10, one decimal); matrix cells, bonuses, penalties likewise (+5 → +0.5); W becomes Σ(adjusted/10). Multipliers, decay rates, k unchanged. Compute in decimals, round once.

## 1. Scope & tie-breakers (AUTHORITATIVE)
Technology owns the tech itself: specs, performance, security, capabilities, adoption, roadmaps — hardware, software, AI, networks, cybersecurity. Business handoff: the corporate move (launch pricing strategy, deals, layoffs); "Chipmaker ships 8-core chip" is Tech, "Chipmaker acquires rival" is Business. Science handoff: research claims — "new algorithm proves X" with a paper routes through Science's evidence machine. Law handoff: breach lawsuits, antitrust — the case itself. Economy handoff: chip stocks, sector market moves. Straddle handshake: score the tech component, mark PROVISIONAL, attach external_straddle_risk; straddle rule = LOWER of the two. Principle: *coverage counts for almost nothing until the claim ties back to a primary artifact — a spec sheet, a CVE number, an RFC, a commit, or an independent benchmark run.* Reliability is CONDITIONAL on claim type; the router runs before any source is scored.

## 2. Scoring procedure (in order)
1. Product-version resolution: WHICH product, model, variant, software version, region. Ambiguity → low confidence + version-ambiguous flag.
2. Route to one of eight types; dual-tag torn cases, display the lower. Type 4/7 collision rule: AI-model abilities → type 7, non-AI features → type 4, never both.
3. Registry shortcut (CVE/NVD, FCC cert, RFC, published standard): match 9.5, contradict 1.0.
4. Look up EFFECTIVE reliability (matrix below), then adjust: methodology bonus +0.5; reproducibility bonus +0.5; independence penalty −1.5 (sponsored/affiliate/embargo-conflicted); pre-release sample penalty −1.0 (vendor-selected units, beta firmware, reviewer-guide constraints); no-primary-artifact penalty −2.0.
5. Echo collapse (85%, semantic copies) with the embargo distinction: press-kit rewrites collapse to ONE source; an embargoed review with its own disclosed tests counts — the method, not the calendar, decides.
6. Saturating curve: W = Σ(adjusted/10); EffectiveCeiling = best single adjusted source (never outscore your best source); Raw = Ceiling × (1 − e^(−0.9·W)).
7. Recency decay: multiplier = max(0.3, 1 − rate × age_years); rates/year Spec 0.08, Perf 0.30, Security 0.45, Capability 0.20, Adoption 0.25, Roadmap 0.50, AI 0.35, Standards 0.10. Foundational-spec bypass: active un-superseded standard → freeze at 1.00 + foundational-spec flag; REQUIRES supersession check against the standards registry; superseded → no freeze + superseded-standard flag.
8. Shipping-state scaling: unshipped hardware, no independent unit verification → ×0.60 (teardown, FCC filing, or independent benchmark lifts it). Cloud ladder: vaporware-intent ×0.40, closed-beta ×0.60, phased-rollout ×0.80, GA ×1.00 — multipliers, not caps; the type-6 cap 4.0 still clamps.
9. Contradiction override: T0/T1 refutation (prior art, failed reproduction, NVD entry) forces 1.0 regardless of coverage.
10. Compound = MIN across sub-claims.

## 3. Effective reliability matrix (Spec, Perf, Security, Capability, Adoption, Roadmap, AI, Standards)
- T0 Standards & regulators (9.6): 9.9, 9.0, 9.9, 8.4, 8.4, 7.1, 8.0, 9.9
- T1 Independent test labs (8.8): 9.0, 9.9, 9.5, 9.5, 7.6, 6.0, 9.2, 9.2
- T2 Tech journalism (7.6): 8.1, 8.3, 7.9, 8.5, 7.9, 7.0, 8.0, 7.6
- T3 Vendor primary (6.8): 9.6, 3.8, 8.0, 5.8, 4.8, 7.4, 4.5, 8.2
- T4 Analyst/market firms (6.4): 5.4, 5.8, 5.2, 5.8, 8.5, 7.0, 6.0, 5.5
- T5 Blogs/forums/leaks (4.2): 3.0, 3.0, 2.6, 3.0, 3.2, 4.0, 2.5, 2.5

Seeds — T0: IETF/RFC Editor, IEEE, W3C, ISO/IEC, Khronos, Unicode, NIST (incl. NVD), MITRE (CVE), CISA/US-CERT, FCC. T1: SPEC, MLCommons/MLPerf, UL, AV-TEST, AV-Comparatives, DXOMARK, iFixit. T2: IEEE Spectrum, Ars Technica, The Verge, Tom's Hardware, MIT Technology Review (MBFC/NG + two document signals). T3: vendor datasheets, official docs, PSIRT advisories, engineering blogs, changelogs/commits. T4: Gartner, IDC, Counterpoint, StatCounter. T5: blogs, Reddit/HN, leak aggregators, vendor marketing/PR.

## 4. Type rules & caps
Superlatives inherit the comparison evidence bar; one credible prior-art finding forces 1.0. Blanket "is secure" = unfalsifiable marketing → type 6. Type 6 (roadmap/intent): unified fleet prediction cap 4.0. Type 7 AI: ladder demo → benchmark → INDEPENDENT REPRODUCTION → deployed reality; score at the rung reached; CONTAMINATION CAP — static-public-benchmark-only claims cap 5.5 + data-contamination-risk flag; green requires dynamic evaluation (blind human preference, held-out/private sets, independent adversarial red-teaming); flag describes RISK, never asserts contamination; prompt-cherry-picking and model-version checks. Breach counts: provisional cap 5.5 until company disclosure (8-K, regulator notification) or regulator figure; score the confirmed shell separately. Type 8: "compatible" ≠ "certified" ≠ "compliant" — the exact word decides the artifact.

## 5. Named traps
Announced-vs-shipped (signature trap; paper-launch flag); benchmark-detection gaming (credible T1/T2 documentation → run loses T1 standing, vendor-conflicted + manipulated-testing-environment flag; report attributed, never asserted); vendor benchmark cherry-picking ("up to" pinned; score typical-condition reading); embargo trap; early breach counts; version truth (score the version named); exploit-detail safety line (verify a CVE exists + severity; NEVER reproduce exploit instructions, PoC code, or bypass steps); AI demo-to-reality gap; compatibility language; superlative prior-art check.

## 6. Bands, states, output
Bands: 8.0–9.9 green, 5.0–7.9 orange, below 5.0 red; ceiling 9.9 (10.0 never awarded). verdict_state: record-verified, unit-verified, independently-tested, vendor-claim-only, version-ambiguous, provisional-breach-count, prior-art-refuted, forecast-attributed, unverified. Templates per state; "couldn't verify" never "false" except the override (refuted-by-record); no intent language; VERDICT-LEAD RULE (anchor first, gap second; headline stays the MIN). Fields: headline_score = MIN across sub-claims; verdict_state; band; verdict_sentence; provisional; product; sub_claims[]; primary_artifacts[] (CVE IDs, FCC filings, RFC numbers, benchmark run IDs, teardowns); remediation_state (unpatched-active-exploit, workaround-available, patched-verified; pairs with exploit-detail-withheld); source_panel[]; flags[] (paper-launch, vendor-benchmark, manipulated-testing-environment, up-to-qualifier, embargo-collapsed, version-ambiguous, provisional-breach-count, prior-art-found, exploit-detail-withheld, sponsored-source, no-primary-artifact, data-contamination-risk, foundational-spec, superseded-standard, ratings-stale, external_straddle_risk); audit_trace_id; checked_at (Tech decays fastest). Rating review 6 months → ratings-stale. Escalation: active exploitation, safety-critical systems, medical devices, election infrastructure, aviation, automotive autonomy, large-scale outages.

## 7. Needs calibration
No explicit 🟡 placeholders. Contradiction override harmonized to 1.0 (was 0.5 in the workbook). All 48 matrix cells tunable; precompute at config load. Exploit-detail line reviewed with Health's clinician line and Economy's advice line.
