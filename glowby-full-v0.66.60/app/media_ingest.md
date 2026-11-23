# Glowby Media Ingest Layer — distilled config (from Spec v1.2, 2026-07-06)

> Score note: AI-Likelihood converted from the spec's 0–99 to Glowby's 0.0–10.0 one-decimal scale (99 → 9.9, 30 → 3.0, 85 → 8.5). Signal weights stay as-is.

## 1. Purpose + position
Sits IN FRONT of the gate and topic router. URL (TikTok/IG/YT/bare media) or upload → (a) plain-text claim(s), (b) an authenticity result. Claims go to the gate exactly like text articles. DOES NOT: categorize, score credibility, decide true/false, or judge satire (gate's job). **Two lanes, never merged:** claim lane vs authenticity lane — stored, displayed, computed separately.

## 2. Non-negotiable rules (hard invariants)
- **R1** No fabrication: every field is traceable to captured data OR an explicit empty/failed marker.
- **R2** Model steps grounded-only: pixels/frames, transcript, OCR, caption/title/description. No outside knowledge, no identifying unlabeled people, no fact-checking.
- **R3** Strict JSON on every model call; parse fail → retry once → stage failed, empty result. Never forward free-form text.
- **R4** Failure is a typed state, not a guess.
- **R5** Authenticity ≠ claim score — separate storage/display/math; never merged.
- **R6** not_assessed ≠ clean — never renders verified/green.
- **R7** Confidence, not verdict — number + reason; only display escalation is the big "AI" badge.
- **R8** No magic numbers — all tunables in Control block, 🟡 until calibrated.
- **R9** Ceiling is **9.9** (spec: 99), never 10.0 — detection is never certain.

## 3. Pipeline (six stages, strict order)
A INGEST → B EXTRACT → C ASSEMBLE → D TRIAGE → E AUTHENTICATE (only if D fired) → F HANDOFF.
**Two-layer cache:** (1) media-authenticity cache keyed sha256 + perceptual hash — reusable within AUTH_CACHE_TTL_HOURS; detectors never re-run on known media. (2) context cache keyed media hash + text hash of caption/title/transcript/OCR — same file + new caption = different context claim; context re-evaluates when wrapping text changes.

## 4. Dependencies (settled; do not add or invent)
Local, required: yt-dlp, ffmpeg, whisper/faster-whisper, easyocr or pytesseract, yt-dlp metadata JSON, imagehash/videohash pHash, langdetect/fasttext lid, c2pa-python/c2patool. API: ONE scene/claim VLM call (hosted or local — the only common-path AI API). Branch-only APIs: reverse search (ONE of Google Cloud Vision web detection, TinEye, SerpAPI/Google Lens) · deepfake detection (ONE of Sightengine, Hive, Reality Defender, Sensity — MUST cover image+video+AUDIO, return confidence + reasons, allow audit logging; outage → typed failure, never a fake score).

## 5. Stage rules
**A Ingest:** URL → yt-dlp media + metadata (caption/title/uploader/date/duration); upload as-is. Bare audio valid (is_audio_only; no frames/OCR/scene). ffmpeg extracts audio. Hash: SHA-256 + perceptual hash (frames) + separate audio hash, normalized before hashing (catches mirrored/cropped/recompressed/screen-recorded variants; frame-match without audio-match flags possible audio replacement). Failure → {status:"ingest_failed", reason}; RIGHTS RULE: blocked/private/geo-locked IS the typed failure — never bypass access controls. Never guess missing caption/date.

**B Extract (4 channels):** transcript (Whisper, timestamped; fail → "" + status) · on-screen text (OCR every OCR_FRAME_INTERVAL_SEC, deduped) · caption/title/description (copied, no model) · scene description (ONE VLM call, SCENE_KEYFRAME_COUNT keyframes). Scene contract: describe only what is visible; never name unlabeled individuals, infer location/date/events, or judge truth; JSON only → depicts, visible_people[], visible_text_in_scene, setting, notable_actions, **appears_to_be_screenshot_of_post**, **safety_flags**{graphic_content, identifiable_minor_present, identifiable_captive_present} — grounded booleans from pixels only. Pipeline continues if ≥1 channel produced content.

**C Assemble:** ONE LLM call fusing channels → **literal** claims, **implied** claim (created by PAIRING of channels only — never world knowledge), **context** claims (from Stage E reverse search). Contract: use ONLY the four channels; no added facts, no verifying; nothing factual → no_claim; JSON only: {claims:[{text, type: literal|implied|context, source_channels:[audio|onscreen|caption|scene|reverse_search], used_as_evidence}], no_claim}. used_as_evidence = media presented as proof (feeds triage; decides whether authenticity is headline or footnote). Cap at MAX_CLAIMS_PER_ITEM; selection: implied → used_as_evidence → caption-sourced → rest. **Channel-failure confidence caps:** claim_assembly_confidence (high/med/low): audio failed on talking-head → ≤ medium; OCR failed on screenshot-heavy → ≤ medium; caption AND transcript both empty → implied claims flagged for review.

**D Triage (wide trigger, deterministic function):** FIRES if ANY: (1) named real public figure appears to speak/act — including AUDIO-ONLY voices; (2) media presented as footage of a specific real event (used_as_evidence on an event claim); (3) reverse search shows the same media with different origin/date. Three exclusions: generic humans/no specific event → not_assessed; openly-labeled synthetic (C2PA AI assertion, watermark, explicit caption) → assessed_labeled directly, no detector spent (claim still extracted and checked); unmistakable fiction only on its face. NO satire judgment here — ambiguous FIRES the branch; the gate decides satire. When in doubt, detect.

**E Authenticate (branch-only; check sha256 cache FIRST):** weighted combination of AVAILABLE signals (missing drop out, never default) → AI-Likelihood **0.0–9.9** (converted from 0–99; higher = more evidence of synthesis). All three failed → not_assessed.
- Provenance (C2PA): AI assertion → strong synthesis evidence; camera chain → toward genuine. Absence of a manifest proves NOTHING (platforms strip metadata); provenance proves what was signed, not that content is real.
- Reverse search: earlier appearance with different date/event → media REAL but OUT OF CONTEXT → signal splits: genuine → authenticity lane; mismatch → structured CONTEXT CLAIM appended to claims[]. Hits record source URL, title, snippet, first_seen_date_confidence, source_quality (high/med/low each). CONFIRMED context claim requires a machine-verifiable earlier date from ≥ medium quality; else HEDGED ("may have appeared earlier") or none.
- Deepfake detector API: weakest signal, lowest weight (DETECTOR_WEIGHT); outage → typed failure, contributes nothing.

**F Handoff:** claims[].text to gate one at a time; authenticity rides alongside, never concatenated.

## 6. Authenticity states + display
- not_assessed — never checked, or all signals failed → show NOTHING (blank ≠ clean)
- assessed_clean — AI-Likelihood ≤ CLEAN_MAX, no AI provenance → number only
- assessed_suspicious — CLEAN_MAX < x < CONFIRM_MIN → number only, NO big badge
- assessed_confirmed — ≥ CONFIRM_MIN OR dispositive AI provenance → BIG "AI" badge + number
- assessed_labeled — declared synthetic → BIG "AI" badge + number
Every assessed state carries a reason string. confirmed/labeled fire the category specs' HALT RULE (History/Conflict/Entertainment). show_big_ai_badge is the single UI boolean.

## 7. Handoff schema (field names are a contract — renaming breaks twelve handoffs)
{source{url, sha256, is_video, is_audio_only, ingested_at}, channels_present{audio,onscreen,caption,scene}, claims[{text,type,source_channels,used_as_evidence}], no_claim, screenshot_of_post, safety_flags{...}, authenticity{state, ai_likelihood /*0.0–9.9|null*/, show_big_ai_badge, reason, signals{provenance, reverse_search{ran, earlier_appearance, context_mismatch}, detector}, from_cache}, stage_status{ingest,extract,assemble,triage,authenticate: skipped|ok|failed}} + v1.2: language{detected_language, translated_to_english, translation_confidence, original_text_preserved}, av_consistency, claim_assembly_confidence, retention{retention_policy, user_upload, contains_sensitive_media}, explainability{why_this_claim_was_extracted, why_authenticity_was_or_was_not_checked, which_channels_supported_the_claim, what_failed_or_was_missing}.
Fleet-consumer contract: halt rule (all 12) · Conflict evidence objects · History miscaption check · Entertainment media_handoff_status · Sports screenshot/clip checks · harm filters read safety_flags.

## 8. Human-review triggers (own uncertainty only; topical risk defers to category bands)
assessed_suspicious · context mismatch on LOW source quality · identifiable_minor/captive flag · JSON parse fails twice in a major stage · claim assembled from a single weak channel.

## 9. Privacy/audit
Store only what processing/dedup/audit require; raw media deleted/expired per retention_policy unless user saves. Log every external call (provider/timestamp/type/result/confidence/reason). Never log API keys, private media URLs, or user files. CLEAN_MAX/CONFIRM_MIN never published (adversaries calibrate against them).

## Needs calibration (🟡)
- CLEAN_MAX = 3.0 (spec: 30) · CONFIRM_MIN = 8.5 (spec: 85) — both converted, never public
- DETECTOR_WEIGHT = 0.25 · PROVENANCE_WEIGHT = 0.40 · REVERSE_SEARCH_WEIGHT = 0.35
- OCR_FRAME_INTERVAL_SEC = 2 · SCENE_KEYFRAME_COUNT = 4 · MAX_CLAIMS_PER_ITEM = 8
- AUTH_CACHE_TTL_HOURS = 24 · CONTEXT_CACHE_TTL_HOURS = 6 · PHASH_MATCH_THRESHOLD (unset)
- WHISPER_MODEL = "base" (confirm) · DEEPFAKE_PROVIDER (choose; must cover audio) · REVERSE_SEARCH_PROVIDER (choose)
- Locked, not calibration: MAX_AI_LIKELIHOOD = 9.9 (spec: 99)
