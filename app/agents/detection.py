"""
Media detection orchestrator — every signal, one place, one order.

Before this module the three call sites (gated check, AI-only check,
private path) each re-implemented the detector calls, and the face pass
had slipped under the wrong `elif`. Now one function runs the lane:

  1. Hive on the sampled frames (or the image)               ~3.6c / 0.6c
  2. ADAPTIVE SECOND PASS: if the detector's strongest frame sat in the
     weak/uncertain band (0.10-0.90) and extra frames exist, run six
     more from different positions — spend only on uncertain videos
  3. Audio: a short clip through the same model (voice cloning)   ~0.6c
  4. Face-specific deepfake pass when a person is likely and the
     dedicated key exists
  5. FORENSIC SECOND OPINION: a reasoning model looks for tells a pixel
     classifier does not (text drift, morphing, physics, watermarks).
     It can raise a clean result to "unclear"; it can never on its own
     produce "strong synthetic signals". Two independent methods
     agreeing is noted as such.                                   ~0.5c
  6. Reverse image search (context lane) — unless the caller forbids it
     (the private path never pushes a person's image to a web matcher)

Hierarchy, never weights: verified > declared > likely > inconclusive >
no signal. Every step is typed on failure and the lane never breaks a
check.
"""

from app.agents import hive_detect, reverse_search
from app.agents.authenticity import (
    ORIGIN_INCONCLUSIVE, ORIGIN_LIKELY, ORIGIN_NO_SIGNAL, merge_stage2,
)

SECOND_PASS_LOW = 0.10   # below: clearly nothing; no second pass
SECOND_PASS_HIGH = 0.90  # at/above: already strong; no second pass


def _needs_second_pass(s2: dict) -> bool:
    if not isinstance(s2, dict) or s2.get("assessment_status") != "completed":
        return False
    top = s2.get("top_score")
    return top is not None and SECOND_PASS_LOW <= float(top) < SECOND_PASS_HIGH


def _opinion_evidence(op: dict) -> dict:
    band = {"low": "none", "medium": "weak", "high": "strong"}[op["likelihood"]]
    tells = "; ".join(op.get("tells") or [])
    real = "; ".join(op.get("real_tells") or [])
    expl = op.get("summary") or ""
    if tells:
        expl += f" Tells: {tells}."
    if real:
        expl += f" Camera-like: {real}."
    if op.get("generator_watermark"):
        expl += f" Generator watermark seen: {op['generator_watermark']}."
    return {"provider": "claude_vision", "signal_type": "consistency_review",
            "raw_score": None, "band": band, "likelihood": op["likelihood"],
            "explanation": expl.strip()[:900], "source_link": None}


def combine_opinion(au: dict, op: dict) -> dict:
    """Pure (unit-tested): fold the reasoner's opinion into the
    assessment. Rules:
    - opinion 'high' + detector no-signal/weak/inconclusive -> inconclusive
      ("two methods disagree; unclear"), never 'likely'
    - opinion 'high' + detector likely -> likely, noted as two methods agreeing
    - opinion 'low' + detector likely -> likely (the classifier stands), noted
    - a generator watermark seen by the reasoner counts as a DECLARED-style
      label only if it names a tool; still capped at inconclusive here
    Nothing here ever lowers a verified/declared origin."""
    out = dict(au or {})
    out.setdefault("evidence", [])
    out["evidence"] = list(out["evidence"]) + [_opinion_evidence(op)]
    cur = out.get("origin_result") or ORIGIN_NO_SIGNAL
    if cur in ("verified_ai_provenance", "declared_ai"):
        return out
    if op["likelihood"] == "high":
        if cur == ORIGIN_LIKELY:
            out["methods_agree"] = True
        else:
            out["origin_result"] = ORIGIN_INCONCLUSIVE
            out["display"] = "authenticity unclear — the two methods disagree"
            out["methods_disagree"] = True
    elif op["likelihood"] == "low" and cur == ORIGIN_LIKELY:
        out["methods_disagree"] = True
    return out


def run_media_detection(au: dict, *, frames=None, extra_frames=None, image_b64=None,
                        audio_b64=None, reason="", allow_reverse=True, posted_date=None,
                        person_hint="", forensic=True, opinion_fn=None) -> dict:
    """Run the whole lane. Returns the updated authenticity dict."""
    au = dict(au or {})
    frames = list(frames or [])[:6]
    extra_frames = list(extra_frames or [])[:6]
    if not hive_detect.available():
        au["stage2_status"] = "failed"
        au["stage2_reason"] = "detector not configured"
        return au

    # 1. primary pass
    try:
        if image_b64:
            s2 = hive_detect.detect_image(image_b64)
        elif frames:
            s2 = hive_detect.detect_video_frames(frames)
        else:
            s2 = None
    except Exception as e:
        s2 = {"assessment_status": "failed", "origin": None, "evidence": [],
              "reason": f"detector error: {e}"}
    if s2 is None:
        au["stage2_status"] = "failed"
        au["stage2_reason"] = "no frames or image were available to analyze"
        return au
    au = merge_stage2(au, s2, reason)
    au["top_score"] = s2.get("top_score")

    # 2. adaptive second pass on uncertain videos
    if extra_frames and _needs_second_pass(s2):
        try:
            s2b = hive_detect.detect_video_frames(extra_frames)
            if s2b.get("assessment_status") == "completed":
                for ev in s2b.get("evidence") or []:
                    ev["signal_type"] = "forensic_video_frames_pass2"
                au = merge_stage2(au, s2b, reason)
                au["frames_analyzed"] = (s2.get("frames_analyzed") or 0) + (s2b.get("frames_analyzed") or 0)
                au["top_score"] = max(float(s2.get("top_score") or 0), float(s2b.get("top_score") or 0))
                au["second_pass"] = True
        except Exception:
            pass

    # 3. audio
    if audio_b64 and hive_detect.audio_available():
        try:
            sa = hive_detect.detect_audio(audio_b64)
            if sa.get("assessment_status") == "completed":
                au = merge_stage2(au, sa, reason)
                if sa.get("origin") == ORIGIN_LIKELY:
                    au["manipulation_scope"] = "voice"
        except Exception:
            pass

    # 4. face-specific pass
    try:
        if hive_detect.deepfake_available() and hive_detect.likely_has_person(person_hint or ""):
            sdf = (hive_detect.detect_deepfake_faces(image_b64) if image_b64
                   else (hive_detect.detect_deepfake_frames(frames) if frames else None))
            if sdf is not None:
                au = merge_stage2(au, sdf, reason)
    except Exception:
        pass

    # 5. forensic second opinion (video frames only; images too if present)
    if forensic and (frames or image_b64):
        try:
            fn = opinion_fn
            if fn is None:
                from app.agents.vision import forensic_opinion as fn
            op = fn(frames if frames else [image_b64])
            if op:
                au = combine_opinion(au, op)
        except Exception:
            pass

    # 6. reverse search (context)
    if allow_reverse and reverse_search.available():
        try:
            f0 = image_b64 or (frames[0] if frames else None)
            if f0:
                s3 = reverse_search.analyze(f0, posted_date=posted_date)
                if s3.get("assessment_status") == "completed":
                    au.setdefault("evidence", [])
                    au["evidence"] = list(au["evidence"]) + list(s3["evidence"])
                    if s3.get("earliest"):
                        au["earliest"] = s3["earliest"]
                    if s3.get("context_note"):
                        au["context_note"] = s3["context_note"]
        except Exception:
            pass
    au["stage"] = 2
    return au
