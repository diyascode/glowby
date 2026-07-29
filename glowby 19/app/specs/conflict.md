# Conflict, War & Security — Judge Agent Build Config

## 1. Scope + lane/handoff tie-breakers (AUTHORITATIVE)
Owns: whether events occurred, attribution, casualty/damage figures, territorial control, force posture/capabilities, live security incidents, terror attacks.
- **Law (dual-tag)**: conduct/legality — "war crime" is a legal judgment; capped low here until an official body rules, AND scored by Law; headline = lower.
- **Politics**: peace talks, diplomacy, sanctions politics, intent-and-strategy claims that are really political analysis.
- **History**: past wars → History; live wars → here; live-conflict historical interpretation dual-tags.
- **Technology**: weapons-system SPEC claims → Tech's matrix; battlefield PERFORMANCE stays here.
Straddle = LOWER of two (born here). Axis is **alignment to a belligerent**, not left/right lean — per-conflict roster multiplier scaled by self-serving level.
Principle: **same-side sources are one source; only non-aligned corroborate.**

## 2. Scoring procedure
1. Resolve conflict context. Unmapped → cold-start (§6).
2. Split into atomic claims (e.g. occurrence + casualty + attribution + legality); route each to seven types (§3).
3. Tag each source: reliability tier (§4) × alignment factor (§6). Base = strongest qualifying source for that type.
4. Echo collapse (war): same original OR same-side repeats count once; cross-language collapse mandatory (translation = same root).
5. Fog (per sub-claim): FogFactor = 1 − 0.30 × (1 − Maturity), Maturity = MIN(1, non-aligned ÷ 3). Corroboration +0.4/non-aligned, cap +1.2.
6. Assembly: Raw = (Base × Alignment + Corroboration + EvidenceQualityAdjustments) × Fog − PrecisionPenalty; Final = MAX(0, MIN(Raw, Cap, 9.9)); caps in order §6. Compound = MIN across sub-claims. Round once.

## 3. Claim-Type Router (7 types; authoritative source per type = §4; caps §6)
1 Event occurrence — belligerent credible on OWN ops (costly admission); no default cap. 2 Attribution — belligerent-only cap if one-sided; **ATTRIBUTION FLOOR: no attribution >6.0 without ≥1 non-aligned forensics/investigation source**. 3 Casualty & damage — heavy fog early. 4 Territorial control — aligned-outlet maps never mature it; high scores need recent geolocation or on-ground wire. 5 Force & capability — belligerent credible on OWN deployments. 6 Conduct & legality — dual-tags Law; WORDING: "conduct allegation pending investigation," never "war crime" until legal standard met. 7 Intent & strategy — named analysts.

## 4. Reliability matrix (base scores /10, source family × claim type)
Columns = Occurr/Attrib/Casualty/Territ/Force/Conduct/Intent:
- IGO/monitoring (UN-OHCHR, ICRC, OPCW, IAEA): 9.0/8.2/8.5/7.8/7.5/8.8/6.0
- Intl wire on-ground (Reuters, AP, AFP): 8.8/7.8/7.5/8.0/7.2/7.0/5.8
- OSINT/verification (geolocation, munition ID): 8.6/8.0/6.0/8.4/7.8/6.5/4.5
- Humanitarian NGO (MSF-class): 8.2/6.5/8.0/6.0/5.0/7.8/5.0
- Conflict-data aggregators (ACLED, UCDP; aggregate/trend): 7.5/6.0/8.2/7.0/6.0/5.5/5.0
- Defence think tank/analyst (IISS, SIPRI arms): 7.0/6.8/5.5/7.2/7.5/6.0/6.8
- Local/regional press: 6.8/6.0/6.2/6.5/5.5/5.8/5.2
- Belligerent official (govt/military/state media): 6.0/4.0/3.5/6.2/5.5/3.0/5.5
- Unverified UGC: 4.0/3.0/2.8/3.5/3.0/2.5/2.5
Alignment is NOT in the matrix — it's the roster multiplier. Aggregate/trend claims also take the History definition gate. Bottom row never anchors.

## 5. Evidence object model + genealogy
Score only from structured EVIDENCE OBJECTS (never article text): original_source (genealogy), geolocation_status, actor_alignment + self_serving_direction, media_auth_status. **Unauthenticated media = NOTHING.** Genealogy rule: laundering (copied text, coordinated posting, fake local outlets, same-side translations) never independent; unresolvable → source-genealogy-uncertain BLOCKS high-confidence.

## 6. Control constants (converted)
Ceiling/floor 9.9/0. Alignment: non-aligned 1.00 · self-admission 1.00 · neutral 0.85 · favourable 0.55 · highly self-serving 0.30 (bottom = fleet CONFLICT_MULT). Fog: max reduction 0.30 / maturity 3 non-aligned. Corroboration +0.4/source, cap +1.2. Caps: belligerent-only 4.5 · consensus-contradiction 2.0 · conduct/legality 3.5 · intent/strategy 4.0. PrecisionPenalty (casualty): exact early −1.0 to −2.0 · range/"at least" −0 to −0.5 · registry post-maturity −0. Attribution floor 6.0. EvidenceQualityAdjustments (CLAMP ±1.0): authenticated primary media / methodology-transparent OSINT +0.5 · unvetted satellite w/o secondary verify −1.0 · partially authenticated −0.5 · single-source local tally −0.5. Alignment confidence low reduces max. Cold-start (unmapped): all belligerent-adjacent sources treated aligned, sub-claims capped COLD_START_CEILING 4.5, auto-route human review. Roster review active 1mo/dormant 6mo. Echo 85% (+ cross-script embedding cosine ≥0.88). Straddle = LOWER.

## 7. Traps
Information operation (count SIDES; same-side collapse to one). Recycled footage (media specialist, halt rule; unauthenticated clip never evidence). Precise-early-number (point figure more fog than range; report ranges attributed). Claim-of-responsibility (attribution INPUT not verdict; denials self-serving; denial vs matured geolocation → contradiction cap). Costly-admission (against-interest ×1.00). Both-sides-lie symmetry (mirrored claims must score identically). Stale-roster (versioned, per-conflict dated).

## 8. Safety layer & human-review bands (before public display)
Victim dignity (no graphic detail; dead never named from unofficial sources; POW images never amplified). Atrocity-claim caution (do-not-amplify; state verification status, never restate horror as likely). Live-operations caution / **TACTICAL GEOSPATIAL FREEZE**: never amplify real-time force locations beyond the sourced claim (not targeting intelligence); precise coordinates/sub-meter geocodes for active military movements STRIPPED from public fields, only generalized region shown — unless aged >48h or published by anchor wire.
Human-review bands: AUTO-PUBLISH · HUMAN-REVIEW (mass-casualty without ≥2 non-aligned confirmations; viral one-sided above threshold) · HOLD (CBRN/nuclear-facility → specialist routing, OPCW/IAEA) · EMERGENCY-ESCALATION (reprisal-risk atrocity).

## 9. Output fields + bands
Band: site colors, fog-affected verdicts name provisionality.
`headline_score` = MIN across sub-claims (straddles publish LOWER category) · `verdict_state` (independently-verified · provisionally-reported · one-side-only · figure-provisional · attribution-unestablished · conduct-pending-ruling · contradicts-verification · unverified) · `verdict_sentence` (VERDICT-LEAD confirmed first; casualty ranges attributed; no intent language) · `provisional` (maturity <1 OR straddle) · `conflict_context` (ROSTER VERSION) · `confirmed[]`/`not_established[]` · `fog_status` · `lift_conditions`/`lower_conditions` · `recheck_at` · `safety_display` · `alignment_panel` · `regional_source_route` · `flags[]` (fog-provisional · belligerent-only-cap · info-op-pattern · source-genealogy-uncertain · roster-stale · cbrn-escalated · do-not-amplify · external_straddle_risk) · `audit_trace_id` · `checked_at`.

## 10. Needs calibration (🟡 placeholders)
- Alignment-table co-ratification with History's flat AlignmentMult (0.50) at cross-category audit; PROPOSED: History keeps 0.50 for deep-historical; live-conflict/dispute claims adopt this graduated table or dual-tag here.
- Conflict roster = GOVERNED data (owner, cadence, per-mapping alignment_confidence, evidence_for_alignment, change log, dispute workflow); versioned per live conflict; roster-stale flags alignment verdicts.
- Matrix ratings reviewed July 5, 2026 (wire/press via MBFC + NewsGuard; OSINT on methodology transparency).
- Ceiling 10.0 documented, never awarded (max 9.9).
