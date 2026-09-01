"""
Media Authenticity Engine — Stage 2 (Day 2 of the v2 build plan).

Forensic pixel/audio detection via Hive. This lane answers the question
Stage 1 cannot: media that carries NO label, NO provenance, NO metadata —
does the content itself show signs of synthetic generation?

Design rules (locked, from the reviewed plan):
- DISTINCT ADAPTERS, one per medium: image, video-frames, deepfake
  (per-face), audio. Each is its own function with its own key, so a
  vendor change in one never breaks another.
- DORMANT WITHOUT A KEY: no HIVE_API_KEY -> available() is False, every
  adapter returns a typed "not_assessed" result. The lane never invents
  a finding and never breaks a check.
- CATEGORIES, NEVER PERCENTAGES: vendor scores are mapped to the Stage-1
  vocabulary (likely_synthetic / inconclusive / no signal) with
  deliberately conservative thresholds. Raw scores are kept ONLY inside
  the evidence entries for the evidence panel — the headline display
  never shows a number until calibration earns one.
- FORENSIC RANKS BELOW DECLARED: a detector's opinion never outranks a
  creator's own statement or validated provenance (hierarchy in
  authenticity._ORIGIN_RANK already encodes this).
- GATED: the detector costs money per call. should_run_stage2() decides
  when the question is worth asking; everything else stays free Stage-1.
- Intimate/abusive imagery is refused upstream; minors are never
  processed (enforced before this module is reached).
"""

import base64
import json
import os
import re
import urllib.request

from app.agents.authenticity import (
    ORIGIN_LIKELY, ORIGIN_INCONCLUSIVE, ORIGIN_NO_SIGNAL,
    STATUS_NOT_ASSESSED, STATUS_COMPLETED, STATUS_FAILED,
)

HIVE_V3_BASE = "https://api.thehive.ai/api/v3"
HIVE_MODEL = os.environ.get(
    "HIVE_MODEL_SLUG", "hive/ai-generated-and-deepfake-content-detection")
HIVE_TIMEOUT_S = 45

# Conservative thresholds (calibration may later tighten/loosen these
# against the test jar — never loosen without jar evidence):
#   >= 0.90  -> likely_synthetic ("strong synthetic-media signals")
#   0.50-0.90 -> inconclusive     (detector unsure; say so, never guess)
#   < 0.50   -> no finding from this detector (absence proves nothing)
THRESH_LIKELY = 0.90
THRESH_INCONCLUSIVE = 0.50

# class names that count as "synthetic" in Hive AI-content responses
_SYNTHETIC_CLASSES = {"ai_generated", "deepfake", "yes_deepfake",
                      "yes_ai_generated", "synthetic"}
# class names that are generator attributions (kept as evidence detail)
_GENERATOR_HINTS = {"sora", "veo", "pika", "kling", "midjourney",
                    "dalle", "dall_e", "stablediffusion",
                    "stable_diffusion", "firefly", "flux", "imagen",
                    "runway", "hedra", "luma"}


# ------------------------------------------------------------ face hint
_FACE_WORDS = re.compile(
    r"\b(face|faces|man|woman|person|people|speaker|speaking|talking|"
    r"interview|says|said|president|mayor|senator|governor|celebrity|"
    r"official|spokesperson|anchor|host|influencer|selfie|portrait|"
    r"he |she |his |her )\b", re.I)


def likely_has_person(text=""):
    """Pure function (unit-tested): cheap text check — does the video's
    visual description / transcript suggest a person is on screen? The
    per-face detector only costs money when this says yes (or the user
    asked). Errs toward True on ambiguity: missing a deepfake is worse
    than one spare call."""
    return bool(_FACE_WORDS.search(text or ""))


def _key(name="HIVE_API_KEY"):
    return (os.environ.get(name) or "").strip()


def available():
    """True only when the image/video detection key exists."""
    return bool(_key())


def deepfake_available():
    """Per-face deepfake model lives in its OWN Hive project/key."""
    return bool(_key("HIVE_DEEPFAKE_KEY"))


def audio_available():
    """AI-voice model lives in its OWN Hive project/key."""
    return bool(_key("HIVE_AUDIO_KEY"))


def _not_assessed(reason):
    return {"assessment_status": STATUS_NOT_ASSESSED, "origin": None,
            "evidence": [], "reason": reason}


def _failed(reason):
    return {"assessment_status": STATUS_FAILED, "origin": None,
            "evidence": [], "reason": reason}


# ------------------------------------------------------------ parsing
def classes_to_finding(classes):
    """Pure function (unit-tested): map Hive class scores to a category.

    `classes` is a list of {"class": name, "score": float}. Returns
    (origin_or_None, top_synthetic_score, generator_hint_or_None).
    """
    top = 0.0
    gen = None
    gen_score = 0.0
    for c in classes or []:
        try:
            name = str(c.get("class", "")).lower()
            score = float(c.get("score", 0.0))
        except Exception:
            continue
        if name in _SYNTHETIC_CLASSES and score > top:
            top = score
        if name in _GENERATOR_HINTS and score > max(gen_score, 0.40):
            gen, gen_score = name, score
    if top >= THRESH_LIKELY:
        return ORIGIN_LIKELY, top, gen
    if top >= THRESH_INCONCLUSIVE:
        return ORIGIN_INCONCLUSIVE, top, gen
    return None, top, None


def _extract_class_lists(payload):
    """Walk a Hive sync response and yield every classes list found.
    Response shapes differ per model; this is deliberately tolerant."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            cl = node.get("classes")
            if isinstance(cl, list) and cl and isinstance(cl[0], dict):
                out.append(cl)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    def walk_maps(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("scores", "class_scores") and isinstance(v, dict):
                    cl = [{"class": ck, "score": cv}
                          for ck, cv in v.items()
                          if isinstance(cv, (int, float)) and 0 <= cv <= 1]
                    if cl:
                        out.append(cl)
                else:
                    walk_maps(v)
        elif isinstance(node, list):
            for v in node:
                walk_maps(v)

    walk(payload)
    walk_maps(payload)
    return out


def _post_v3(key, image_b64=None, media_url=None):
    """One call to the Hive v3 API (Bearer auth, JSON body). The combined
    AI-generated & deepfake model answers both questions in one call."""
    url = f"{HIVE_V3_BASE}/{HIVE_MODEL}"
    if image_b64 and not media_url:
        media_url = "data:image/jpeg;base64," + image_b64
    body = json.dumps({"input": {"media_url": media_url}}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        })
    with urllib.request.urlopen(req, timeout=HIVE_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _finding_to_result(origin, top, gen, provider_label):
    ev = []
    if origin:
        detail = (f" Likely generator: {gen}." if gen else "")
        ev.append({
            "provider": "hive", "signal_type": provider_label,
            "raw_score": round(top, 3),  # evidence panel only
            "band": ("strong" if origin == ORIGIN_LIKELY else "uncertain"),
            "explanation": (
                "Forensic detector reports "
                + ("strong synthetic-media signals."
                   if origin == ORIGIN_LIKELY else
                   "an uncertain result — treated as inconclusive, "
                   "never as a verdict.") + detail),
            "source_link": None,
        })
    else:
        ev.append({
            "provider": "hive", "signal_type": provider_label,
            "raw_score": round(top, 3), "band": "none",
            "explanation": ("Forensic detector found no synthetic "
                            "signal. Absence proves nothing; this never "
                            "renders as 'genuine'."),
            "source_link": None,
        })
    return {"assessment_status": STATUS_COMPLETED,
            "origin": origin or ORIGIN_NO_SIGNAL,
            "evidence": ev, "reason": None}


# ------------------------------------------------------------ adapters
def detect_image(image_b64):
    """Adapter 1: single image, AI-generated-content model."""
    if not available():
        return _not_assessed("no HIVE_API_KEY configured")
    try:
        img = base64.b64decode(image_b64)
    except Exception:
        return _failed("image bytes unreadable")
    try:
        payload = _post_v3(_key(), image_b64=image_b64)
        best = (None, 0.0, None)
        df_top = 0.0
        for classes in _extract_class_lists(payload):
            f = classes_to_finding(classes)
            if f[1] >= best[1]:
                best = f
            for c in classes:
                if str(c.get("class", "")).lower() in ("deepfake",
                                                       "yes_deepfake"):
                    try:
                        df_top = max(df_top, float(c.get("score", 0)))
                    except Exception:
                        pass
        res = _finding_to_result(*best, "forensic_image")
        if df_top >= THRESH_LIKELY:
            res["manipulation_scope"] = "face"
        return res
    except Exception as e:  # network/HTTP/parse: typed failure, no guess
        return _failed(f"detector call failed: {type(e).__name__}")


def detect_video_frames(frames_b64):
    """Adapter 2: sampled video frames through the image model.
    Aggregation: the STRONGEST frame finding stands (a deepfake is a
    deepfake even if most frames are innocent), but a single
    inconclusive frame among clean ones stays inconclusive."""
    if not available():
        return _not_assessed("no HIVE_API_KEY configured")
    if not frames_b64:
        return _not_assessed("no frames supplied")
    best = (None, 0.0, None)
    ran = 0
    for fb in frames_b64[:6]:  # cost cap: at most 6 frames per video
        try:
            base64.b64decode(fb)  # validate only
            payload = _post_v3(_key(), image_b64=fb)
            ran += 1
            for classes in _extract_class_lists(payload):
                f = classes_to_finding(classes)
                if f[1] >= best[1]:
                    best = f
        except Exception:
            continue
    if ran == 0:
        return _failed("no frame could be analyzed")
    res = _finding_to_result(*best, "forensic_video_frames")
    res["frames_analyzed"] = ran
    return res


def detect_deepfake_faces(image_b64):
    """Adapter 3: per-face deepfake model (own key/project). Catches
    deepfakes of NON-famous people — signals concentrated on a face
    are reported as face-scoped manipulation."""
    if not deepfake_available():
        return _not_assessed("no HIVE_DEEPFAKE_KEY configured")
    try:
        base64.b64decode(image_b64)  # validate only
        payload = _post_v3(_key("HIVE_DEEPFAKE_KEY"), image_b64=image_b64)
        best = (None, 0.0, None)
        for classes in _extract_class_lists(payload):
            f = classes_to_finding(classes)
            if f[1] >= best[1]:
                best = f
        res = _finding_to_result(*best, "forensic_deepfake_faces")
        if res.get("origin") == ORIGIN_LIKELY:
            res["manipulation_scope"] = "face"
        return res
    except Exception as e:
        return _failed(f"deepfake call failed: {type(e).__name__}")


def detect_deepfake_frames(frames_b64):
    """Adapter 3b: per-face deepfake across sampled video frames.
    Strongest face finding stands; cost-capped at 3 frames."""
    if not deepfake_available():
        return _not_assessed("no HIVE_DEEPFAKE_KEY configured")
    if not frames_b64:
        return _not_assessed("no frames supplied")
    best = None
    ran = 0
    for fb in frames_b64[:3]:
        r = detect_deepfake_faces(fb)
        if r.get("assessment_status") != STATUS_COMPLETED:
            continue
        ran += 1
        if best is None or (r.get("origin") == ORIGIN_LIKELY
                            and best.get("origin") != ORIGIN_LIKELY):
            best = r
    if best is None:
        return _failed("no frame could be analyzed") if ran == 0 else             _not_assessed("no completed frame analysis")
    best["frames_analyzed"] = ran
    return best


def detect_audio(audio_bytes):
    """Adapter 4: AI-voice detection (own key/project)."""
    if not audio_available():
        return _not_assessed("no HIVE_AUDIO_KEY configured")
    try:
        payload = _post_v3(_key("HIVE_AUDIO_KEY"))
        best = (None, 0.0, None)
        for classes in _extract_class_lists(payload):
            f = classes_to_finding(classes)
            if f[1] >= best[1]:
                best = f
        return _finding_to_result(*best, "forensic_audio_voice")
    except Exception as e:
        return _failed(f"audio call failed: {type(e).__name__}")


# ------------------------------------------------------------ the gate
_AI_TOPIC_RE = re.compile(
    r"\b(ai|a\.i\.|deepfake|deep fake|sora|veo|midjourney|"
    r"ai[- ]generated|artificial intelligence|synthetic)\b", re.I)
_HIGH_RISK_BUCKETS = {"politics", "news", "health", "law", "science"}


def should_run_stage2(title="", user_question="", claims=None,
                      stage1_origin=None, on_demand=False):
    """Decide whether the paid detector runs. Returns (bool, reason).

    Fires when the question is worth the money:
    - the USER asked (on-demand link) — always honored
    - the video/claims are ABOUT AI or deepfakes
    - a claim is high-risk / public-safety / high-risk bucket
      (person-presented-as-real, event-as-evidence, attributed voice
      all route through claim risk flags today)
    Never fires when Stage 1 already settled it (verified/declared —
    the creator told us; paying to double-check adds nothing).
    """
    if stage1_origin in ("verified_ai_provenance", "declared_ai"):
        return False, "already settled by provenance/label"
    if on_demand:
        return True, "user requested authenticity check"
    text = f"{title or ''} {user_question or ''}"
    if _AI_TOPIC_RE.search(text):
        return True, "content is about AI/synthetic media"
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        if c.get("public_safety_risk"):
            return True, "public-safety claim present"
        if str(c.get("risk_level", "")).lower() == "high":
            return True, "high-risk claim present"
        if str(c.get("bucket", "")).lower() in _HIGH_RISK_BUCKETS \
                and c.get("central"):
            return True, "central claim in a high-stakes category"
    return False, "no gate condition met"
