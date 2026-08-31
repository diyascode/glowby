"""
Media Authenticity Engine — Stage 1 (Day 1 of the v2 build plan).

Answers a SECOND question, separate from claim accuracy: does this media
carry evidence of being synthetically generated? Two lanes, never merged.

Day-1 scope (free, local, no vendor accounts):
- C2PA Content Credentials: read AND validate; a validated AI-generation
  assertion is VERIFIED provenance (the only thing that earns the badge).
- Weak/declared labels: "made with AI" captions, #AIart tags, visible
  Sora/Veo watermark text in the OCR channel -> declared_ai (no badge).
- Metadata generator tags: byte-scan for generator names in EXIF/XMP
  regions of raw image bytes (weak, easily altered -> declared_ai).

Locked rules (from the reviewed plan):
- Categories, never percentages. No numeric likelihood is produced here.
- Evidence hierarchy, not weighted blending: verified > declared. Later
  stages (forensic, context) slot BELOW declared in authority.
- Absence proves nothing: "no_synthetic_signal" never renders as
  "genuine"; there is no green checkmark for authenticity.
- Failure is typed: readers unavailable or files unreadable produce
  status "partial"/"failed", never an invented finding.
"""

import base64
import re

# ---- vocabulary (the data model, locked) ----
STATUS_NOT_ASSESSED = "not_assessed"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

ORIGIN_VERIFIED = "verified_ai_provenance"
ORIGIN_DECLARED = "declared_ai"
ORIGIN_LIKELY = "likely_synthetic"        # stage 2 (forensic) territory
ORIGIN_INCONCLUSIVE = "inconclusive"
ORIGIN_NO_SIGNAL = "no_synthetic_signal"

# authority order: earlier wins (hierarchy, not weights)
_ORIGIN_RANK = [ORIGIN_VERIFIED, ORIGIN_DECLARED, ORIGIN_LIKELY,
                ORIGIN_INCONCLUSIVE, ORIGIN_NO_SIGNAL]

# display text per category (v1 provisional copy)
DISPLAY = {
    ORIGIN_VERIFIED: "AI-generated — verified provenance",
    ORIGIN_DECLARED: "Creator or platform labeled this content as AI-generated",
    ORIGIN_LIKELY: "Strong synthetic-media signals",
    ORIGIN_INCONCLUSIVE: "Authenticity could not be determined",
    ORIGIN_NO_SIGNAL: "No synthetic signal detected (this does not confirm authenticity)",
}

# ---- weak/declared label patterns (conservative on purpose:
# a false "AI-labeled" claim is worse than a miss) ----
CAPTION_PATTERNS = [
    r"\bmade with ai\b", r"\bai[- ]generated\b", r"\bcreated with ai\b",
    r"#aiart\b", r"#aigenerated\b", r"#madewithai\b", r"#aivideo\b",
    r"\bgenerated (?:by|with) (?:ai|sora|veo|midjourney|dall[- ]?e)\b",
]
# OCR channel: visible watermark text burned into frames. Bare tool names
# only count in the VISUAL channel (a caption merely mentioning "Sora"
# could be about the tool; a watermark IS the tool's signature).
OCR_PATTERNS = [
    r"\bmade with ai\b", r"\bai[- ]generated\b",
    r"\bsora\b", r"\bveo\b", r"\bpika\b", r"\bkling\b",
]
# generator names to byte-scan for in EXIF/XMP regions of raw images
METADATA_GENERATORS = [
    b"Midjourney", b"DALL-E", b"DALLE", b"Stable Diffusion",
    b"Adobe Firefly", b"Sora", b"Imagen", b"FLUX",
]
_META_SCAN_BYTES = 131072  # metadata lives in the head of the file


def _evidence(provider, signal_type, explanation, raw_score=None,
              band=None, source_link=None):
    return {
        "provider": provider,
        "signal_type": signal_type,
        "raw_score": raw_score,
        "band": band,
        "explanation": explanation,
        "source_link": source_link,
    }


# ------------------------------------------------------------ C2PA
def check_c2pa(image_bytes):
    """Read + validate Content Credentials. Returns (origin|None, evidence,
    ok) where ok=False means the reader itself was unavailable/failed
    (typed, not a finding)."""
    try:
        import io
        import json as _json

        import c2pa
        try:
            reader = c2pa.Reader("image/jpeg", io.BytesIO(image_bytes))
            manifest = _json.loads(reader.json())
        except Exception:
            # no manifest present — the normal case; platforms strip them
            return None, None, True
        # validation: any recorded failure means the chain is NOT trusted
        status = manifest.get("validation_status") or []
        valid = not [s for s in status
                     if "failure" in str(s.get("code", "")).lower()]
        active = manifest.get("manifests", {}).get(
            manifest.get("active_manifest", ""), {})
        blob = _json.dumps(active).lower()
        ai_made = ("trainedalgorithmicmedia" in blob
                   or "compositewithtrainedalgorithmicmedia" in blob)
        if ai_made and valid:
            return ORIGIN_VERIFIED, _evidence(
                "c2pa", "verified_provenance",
                "Validated Content Credentials record AI generation."), True
        if ai_made:
            return ORIGIN_DECLARED, _evidence(
                "c2pa", "declared_provenance",
                "Content Credentials record AI generation but the "
                "signature chain could not be validated."), True
        # a validated camera chain is evidence TOWARD genuine, never proof
        if valid and "c2pa.created" in blob:
            return None, _evidence(
                "c2pa", "capture_provenance",
                "Validated capture credentials present (evidence toward "
                "authentic capture; not proof of content)."), True
        return None, None, True
    except Exception:
        return None, None, False  # reader unavailable/failed: typed


# ------------------------------------------------------------ labels
def check_labels(caption="", ocr_text=""):
    """Declared/weak evidence: creator or platform said it's AI."""
    found = []
    cap = (caption or "").lower()
    for pat in CAPTION_PATTERNS:
        if re.search(pat, cap):
            found.append(_evidence(
                "labels", "caption_label",
                f"Caption/title contains an AI label (pattern: {pat})."))
            break
    ocr = (ocr_text or "").lower()
    for pat in OCR_PATTERNS:
        if re.search(pat, ocr):
            found.append(_evidence(
                "labels", "visible_watermark_text",
                f"On-screen text contains AI watermark wording "
                f"(pattern: {pat})."))
            break
    return found


def check_metadata(image_bytes):
    """Weak: generator names embedded in EXIF/XMP head bytes."""
    head = (image_bytes or b"")[:_META_SCAN_BYTES]
    for name in METADATA_GENERATORS:
        if name in head:
            return _evidence(
                "metadata", "generator_tag",
                f"Image metadata names a generator ({name.decode()}). "
                "Metadata is easily altered; treated as declared, "
                "not verified.")
    return None


# ------------------------------------------------------------ assemble
def assess_stage1(caption="", ocr_text="", image_b64=None):
    """Run every free Day-1 check; resolve by hierarchy, never weights."""
    evidence = []
    origins = []
    status = STATUS_COMPLETED

    img = None
    if image_b64:
        try:
            img = base64.b64decode(image_b64)
        except Exception:
            status = STATUS_PARTIAL

    if img:
        c_origin, c_ev, c_ok = check_c2pa(img)
        if not c_ok:
            status = STATUS_PARTIAL
        if c_ev:
            evidence.append(c_ev)
        if c_origin:
            origins.append(c_origin)
        m_ev = check_metadata(img)
        if m_ev:
            evidence.append(m_ev)
            origins.append(ORIGIN_DECLARED)

    label_ev = check_labels(caption, ocr_text)
    if label_ev:
        evidence.extend(label_ev)
        origins.append(ORIGIN_DECLARED)

    origin = ORIGIN_NO_SIGNAL
    for o in _ORIGIN_RANK:
        if o in origins:
            origin = o
            break

    return {
        "assessment_status": status,
        "origin_result": origin,
        "manipulation_scope": "whole_media" if origin in
        (ORIGIN_VERIFIED, ORIGIN_DECLARED) else "unknown",
        "display": DISPLAY[origin],
        "show_ai_badge": origin == ORIGIN_VERIFIED,  # verified ONLY
        "evidence": evidence,
        "stage": 1,
    }


# ------------------------------------------------------------ stage 2 merge
def merge_stage2(stage1, stage2, gate_reason=None):
    """Fold a Stage-2 forensic result into a Stage-1 assessment.

    Hierarchy, never weights: the combined origin is the highest-ranked
    of the two. Forensic evidence is appended to the panel. The badge
    rule is untouched (verified provenance only). A not_assessed/failed
    Stage 2 changes nothing except a typed note.
    """
    out = dict(stage1 or {})
    out.setdefault("evidence", [])
    out.setdefault("origin_result", ORIGIN_NO_SIGNAL)
    s2 = stage2 or {}
    out["stage2_status"] = s2.get("assessment_status", STATUS_NOT_ASSESSED)
    if gate_reason:
        out["stage2_gate_reason"] = gate_reason
    if s2.get("assessment_status") != STATUS_COMPLETED:
        return out
    out["evidence"] = list(out["evidence"]) + list(s2.get("evidence") or [])
    s2_origin = s2.get("origin") or ORIGIN_NO_SIGNAL
    cur = out.get("origin_result") or ORIGIN_NO_SIGNAL
    for o in _ORIGIN_RANK:
        if o in (cur, s2_origin):
            out["origin_result"] = o
            break
    if s2.get("manipulation_scope"):
        out["manipulation_scope"] = s2["manipulation_scope"]
    if s2.get("frames_analyzed"):
        out["frames_analyzed"] = s2["frames_analyzed"]
    out["display"] = DISPLAY.get(out["origin_result"], out.get("display"))
    out["show_ai_badge"] = out.get("origin_result") == ORIGIN_VERIFIED
    out["stage"] = 2
    return out
