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

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_FRAMES = 6
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
(pure scenery, vibes, abstract visuals), respond with exactly: {nothing}"""


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
