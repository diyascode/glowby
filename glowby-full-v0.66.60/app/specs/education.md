# Education Agent — distilled config (v1.0 DRAFT, Claude-authored, native 0.0–10.0)

**Purpose:** judge one claim about schools, universities, students, teachers, curricula, tuition, admissions, enrollment, rankings, degrees, accreditation. Returns score (0.0–10.0, 9.9 ceiling, 10.0 never awarded), verdict state, one-line verdict, source panel.

**Core insight:** education has a strong official record layer (NCES/IPEDS-class data, accreditor databases, tuition schedules) — record claims are lookups. The viral content is anecdote-inflation, rankings-as-fact, and accusations. One-line principle: *an anecdote can illustrate, but only the record can generalize.*

## Lane tie-breakers (authoritative)
- University research finding → Science/Health; the schooling itself → Education
- Education law/funding/political fight → Politics; what changes in schools → Education
- Curriculum culture-war → Society&Culture (often dual-tag); what a curriculum document actually says → Education
- Ed-tech: what the tech does → Technology; company finances → Business; classroom use/outcomes → Education
- College sports: games → Sports; eligibility/admissions/academics → Education
- Campus crime/lawsuit → Law; institutional policy response → Education

## Claim-type router (4 branches)
1. **Record claims** (enrollment, tuition, graduation rates, test scores, rankings positions, accreditation, admission rates) → official-record lookup; record settles.
2. **Research claims** (learning methods, outcomes, interventions) → evidence-hierarchy scoring; single-study cap.
3. **Institutional-conduct claims** ("school did/teaches/banned X", personnel actions) → interested-party model; verify against primary documents (board minutes, curriculum standards, filings), not screenshots.
4. **Predictions** → attributed-projection scoring, cap 4.0; unanchored speculation parks at gate.

## Evidence hierarchy (score by BEST level that actually supports the claim)
- E1 (cap 9.9): official records — NCES/IPEDS-class stats, education-department data, accreditor databases, published tuition schedules, board-approved curriculum documents, official filings
- E2 (cap 8.5): peer-reviewed education research, meta-analyses, official statistics bureaus
- E3 (cap 7.0): institutional statements (→ 4.0 when contested and self-serving); rankings publishers (methodology-bound, cap 6.5 🟡)
- E4 (cap 6.0): quality journalism incl. education press
- E5 (cap 3.5): advocacy orgs, unions/associations on contested matters — attribute, never settle
- E6 (cap 2.0): viral anecdote, single-classroom stories, worksheet screenshots

## Traps (actively check)
- **Anecdote-to-trend inflation:** representativeness multiplier — national/official 1.00 · multi-district 0.70 · single school/classroom 0.40 🟡. Apply when claim scope ("schools", "kids today") exceeds evidence scope.
- **Rankings-as-fact:** named ranker → methodology-bound record claim (cap 6.5); bare superlative → DEPENDS ON DEFINITION, null score, neutral band.
- **Stale figures:** tuition/enrollment/rankings change annually — vintage check; old figure asserted as current scores against the current record.
- **Curriculum-screenshot citogenesis:** verify against the actual curriculum document/board record; mutually-citing outlets = ONE source.
- **Press-release study inflation:** score the study, not the press office headline.

## Harm gates (regardless of score)
- **Minors' privacy:** claims identifying minor students never amplified; identifying details never in verdicts/share cards.
- **Named-educator accusations:** guilt gate as in Law — allegation ≠ finding; provisional cap 7.0 until official findings; presumption language mandatory.
- **School-emergency instructions** (lockdown/closure/threat): public_safety_risk critical — verify ONLY against official district/emergency channels; unverified → never amplified, never repeated as possibly valid.
- **Financial-urgency scams** (scholarship/loan-forgiveness/admissions-consulting): urgency + payment + unverifiable authority → contradicted-band language + warning line.

## Bands & states
- 8.0–9.9 Supported — "The official record / strong evidence confirms…"
- 5.0–7.9 Provisional / partly supported — "Credibly reported but not officially settled…"
- 2.6–4.9 Insufficient — "The available evidence does not support…"
- 0.0–2.5 Contradicted — "The record contradicts this claim…"
- null — DEPENDS ON DEFINITION / NOT SCOREABLE (bare superlatives; taste/identity)
- **headline_score = MIN across sub-claims; straddle publishes LOWER of two categories.**

## Constants
Ceiling 9.9 · single-study 5.5 · prediction 4.0 · provisional 7.0 · contested self-serving statement 4.0 · PR-only contested conduct 1.5 · rankings cap 6.5 🟡 · representativeness 1.00/0.70/0.40 🟡 · CONFLICT_MULT 0.30 🟡

## Output
Fleet audit schema: claim_id, category=education, score|null, verdict_state, one-line verdict, source panel with levels, caps_applied, harm_gates_triggered, representativeness_multiplier, model_version, taxonomy_version, checked_at.

## Needs calibration
rankings cap 6.5 · representativeness multipliers · CONFLICT_MULT 0.30 · (supersede this entire file if the original Education spec is found)
