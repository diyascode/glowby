"""
Vision agent — Glowby's eyes.

Some videos make their claims without words: a flight-path animation, a
chart, a map, text on screen over music. When the transcript is thin,
the ingest agent samples frames from the video and this agent describes
what the visuals ASSERT, so the claims can enter the normal pipeline
(gate -> router -> evidence -> judges) like any spoken claim.

The description is evidence-neutral: it reports what the video shows
and implies, it does not judge whether that is true — judging belongs
to the judges.
"""

import os

# COST: the eyes describe on-screen text and scenes; the small model does
# this well at a fifth of the price. Override with GLOWBY_VISION_MODEL.
MODEL = os.environ.get("GLOWBY_VISION_MODEL", "claude-haiku-4-5")
MAX_FRAMES = int(os.environ.get("GLOWBY_VISION_FRAMES", "4"))
NOTHING = "NOTHING_CHECKABLE"

PROMPT = """You are the EYES of Glowby, a fact-checking service. These are \
{n} frames sampled evenly from a short video that has little or no speech. \
Your job is to report what the video VISUALLY asserts, so it can be \
fact-checked.

Video title: {title}
Uploader: {uploader}

Describe, plainly and neutrally:
- Any on-screen text: transcribe it EXACTLY.
- Diagrams, charts, maps, trajectories, timelines: what do they depict, \
and what specific factual assertion do they make? (e.g. "the animation \
presents this as the Artemis II flight path: launch, Earth orbit, \
translunar free-return around the Moon, splashdown").
- Numbers, statistics, comparisons, before/afters, demonstrations.
- What a reasonable viewer would come away believing.

Rules: report ONLY what is visible — never invent details, labels, or \
numbers not shown. Don't judge truth; just state what is asserted. Write \
2-6 plain sentences. If the frames genuinely assert nothing checkable \
(pure scenery, vibes, abstract visuals), respond with exactly: {nothing}

SCREENSHOTS OF MESSAGES: if the image is a screenshot of a text message, \
iMessage/WhatsApp/Telegram chat, email, voicemail transcript, DM, pop-up, \
notification or social-media comment, it is ALWAYS reportable (never \
{nothing}). Start your answer with a line "[MESSAGE TEXT]" followed by an \
EXACT transcription: the sender's name, phone number or email address as \
shown; every line of the message; every link, domain, code, amount and \
phone number character for character; the app it appears in if visible. \
Then a blank line and the plain description. Never paraphrase a link."""


def describe_frames(frames: list, title: str = "", uploader: str = ""):
    """Frames (base64 JPEGs) in, visual-claims description out.

    Returns a text description, or None when the frames assert nothing
    checkable or the call fails (caller decides what to do next).
    """
    if not frames:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import anthropic

    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}
        for b64 in frames[:MAX_FRAMES]
    ]
    content.append({"type": "text", "text": PROMPT.format(
        n=len(frames[:MAX_FRAMES]),
        title=title or "(unknown)",
        uploader=uploader or "(unknown)",
        nothing=NOTHING,
    )})
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            temperature=0,  # same frames -> same description (re-check
            # consistency: the eyes must not re-word the video each run)
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        return None
    text = "".join(
        b.text for b in message.content if getattr(b, "type", "") == "text"
    ).strip()
    if not text or NOTHING in text:
        return None
    return text


# ------------------------------------------------------------ forensic second opinion
# A pixel classifier (Hive) and a reasoning model see different tells. The
# reasoner is NOT a detector: it looks for the things generators get wrong
# across frames — text that changes spelling, hands and objects that morph,
# physics that does not hold, watermarks of known tools — and reports them.
# It can raise a clean detector result to "unclear"; it can never, on its
# own, produce "strong synthetic signals".
FORENSIC_MODEL = os.environ.get("GLOWBY_FORENSIC_MODEL", "claude-haiku-4-5")
FORENSIC_PROMPT = """You are examining {n} frames sampled from one short video \
to look for signs it was AI-generated. You are NOT judging the story; only the \
pixels. Check, frame by frame and across frames:
- Text and logos: is any visible text garbled, misspelled, or does it CHANGE \
between frames? Real text stays put.
- Hands, faces, teeth, ears: extra or fused fingers, asymmetric or drifting \
features, teeth that merge.
- Objects and background: do objects morph, appear, vanish, or change shape \
between frames? Do straight lines wobble? Do reflections and shadows agree \
with the light?
- Physics and motion: impossible poses, water/fire/cloth behaving wrongly, \
limbs passing through things.
- Watermarks or captions naming a generator (Sora, Veo, Kling, Runway, Pika, \
Midjourney, "AI").
- Real-camera tells: sensor noise, motion blur, lens flare, compression that \
looks like phone footage.
Respond with ONLY a JSON object (no prose, no code fences):
{{"likelihood": "low" | "medium" | "high", "tells": ["short concrete observation", ...], \
"real_tells": ["short concrete observation", ...], "generator_watermark": "name or null", \
"summary": "one plain sentence"}}
"high" means at least two clear tells that real footage would not show. "low" \
means the frames look like ordinary camera footage. Be specific; never guess \
beyond what is visible."""


def parse_forensic(raw: str) -> dict | None:
    """Pure (unit-tested)."""
    import json as _json
    import re as _re
    if not raw:
        return None
    t = raw.strip()
    t = _re.sub(r"^```(?:json)?\s*", "", t)
    t = _re.sub(r"\s*```$", "", t)
    if not t.startswith("{"):
        a, b = t.find("{"), t.rfind("}")
        if a == -1 or b == -1 or b < a:
            return None
        t = t[a:b + 1]
    try:
        d = _json.loads(t)
    except _json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    lk = str(d.get("likelihood", "")).lower().strip()
    if lk not in ("low", "medium", "high"):
        return None
    tells = [str(x)[:140] for x in (d.get("tells") or []) if x][:6]
    real = [str(x)[:140] for x in (d.get("real_tells") or []) if x][:6]
    wm = d.get("generator_watermark")
    wm = None if (not wm or str(wm).lower() in ("null", "none", "")) else str(wm)[:40]
    if lk == "high" and len(tells) < 2 and not wm:
        lk = "medium"  # "high" needs two concrete tells (or a watermark)
    return {"likelihood": lk, "tells": tells, "real_tells": real,
            "generator_watermark": wm, "summary": str(d.get("summary") or "")[:240]}


def forensic_opinion(frames: list, client=None):
    """Frames -> parsed opinion dict, or None on any failure."""
    if not frames:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if client is None and api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    if client is None:
        return None
    use = frames[:6]
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in use]
    content.append({"type": "text", "text": FORENSIC_PROMPT.format(n=len(use))})
    try:
        msg = client.messages.create(model=FORENSIC_MODEL, max_tokens=500, temperature=0,
                                     messages=[{"role": "user", "content": content}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return parse_forensic(raw)
    except Exception:
        return None
