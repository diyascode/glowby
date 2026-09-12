"""
Content gate — what Glowby will and will not process, and how.

Glowby is a 4+/12+ app whose Trending tab and share links are public.
Every check therefore passes a content gate BEFORE routing:

  general  -> checked normally; may appear in Trending.
  mature   -> checked normally (violence, strong language, suggestive
              themes are most of the news); never shown in Trending.
  explicit -> NOT fact-checked and NOT stored. The one thing Glowby
              offers is the AI check, on a PRIVATE path: no caption, no
              transcript, no link to the original, no share link, no
              cache, no reverse image search — the result exists on the
              person's screen and nowhere else. This is deliberate: a
              person whose face has been put on an explicit video is one
              of the people who needs AI detection most.
  minor    -> if the material may involve a person under 18, Glowby
              refuses to process it at all, keeps nothing, and points to
              NCMEC's Take It Down. Not a design choice; the law.

Two layers: a free regex pre-screen on the title/caption, then a small
model reading the title + transcript + visual description. The model
FAILS SAFE: if it cannot answer, anything the pre-screen found suspicious
is treated as mature (never in Trending), otherwise general.
"""

import json
import os
import re

MODEL = os.environ.get("GLOWBY_SAFETY_MODEL", "claude-haiku-4-5")
RATINGS = ("general", "mature", "explicit")

# obvious explicit markers in a caption/title — free, runs first
_EXPLICIT_RE = re.compile(
    r"\b(porn\w*|xxx|nsfw|onlyfans|sex\s*tape|nudes?|nudity|naked|hentai|"
    r"blowjob|handjob|deepfake\s*porn|leaked\s*(video|tape)|18\+|adult\s*content)\b", re.I)
# markers that a MINOR may be involved alongside sexual content
_MINOR_RE = re.compile(
    r"\b(minor|underage|under[- ]age|teen(ager)?s?|schoolgirl|schoolboy|"
    r"\b1[0-7]\s*(yo|y/o|year[- ]old)|child|kid|loli)\b", re.I)
_SEXUAL_RE = re.compile(r"\b(sex\w*|nude\w*|naked|porn\w*|nsfw|explicit|xxx)\b", re.I)


def prescreen(title: str = "", transcript: str = "") -> dict:
    """Pure (unit-tested): regex first look. Returns {rating, minor_risk,
    matched}. Only ever escalates; the model may de-escalate 'explicit'
    from a caption that merely mentions the word."""
    text = f"{title or ''}\n{transcript or ''}"
    explicit = bool(_EXPLICIT_RE.search(text))
    minor = explicit and bool(_MINOR_RE.search(text)) or (
        bool(_SEXUAL_RE.search(text)) and bool(_MINOR_RE.search(text)))
    return {"rating": "explicit" if explicit else "general",
            "minor_risk": bool(minor), "matched": explicit or minor}


PROMPT = """You are the content gate for Glowby, a fact-checking app rated for \
a general audience whose Trending tab is public. Rate this video's content. \
Answer with ONLY a JSON object (no prose, no code fences):
{{"rating": "general" | "mature" | "explicit", "minor_risk": true | false, \
"reason": "one short sentence"}}

Definitions:
- "general": suitable to list publicly next to news and product videos.
- "mature": violence, injury, strong language, drug use, suggestive but \
non-explicit themes, disturbing news imagery. Still checked; kept off Trending.
- "explicit": pornographic or sexually explicit material, sexual nudity, \
sexual acts, or extreme gore presented for shock. A NEWS REPORT ABOUT such \
material is "mature", not "explicit".
- "minor_risk": true ONLY when the material is sexual/explicit AND there is \
any indication a depicted person may be under 18 (stated age, "teen", \
school context, childlike appearance described). When unsure and the \
material is sexual, say true.

Video title/caption: {title}
Uploader: {uploader}
Transcript and visual description (may be empty):
\"\"\"{transcript}\"\"\""""


def parse_rating(raw: str) -> dict | None:
    """Pure (unit-tested): model JSON -> {rating, minor_risk, reason} or None."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b == -1 or b < a:
            return None
        text = text[a:b + 1]
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    r = str(d.get("rating", "")).strip().lower()
    if r not in RATINGS:
        return None
    mr = d.get("minor_risk")
    if isinstance(mr, str):
        mr = mr.strip().lower() in ("true", "yes", "1")
    return {"rating": r, "minor_risk": bool(mr),
            "reason": str(d.get("reason") or "")[:200]}


def rate_content(title: str = "", transcript: str = "", uploader: str = "",
                 client=None) -> dict:
    """Two-layer rating. Never raises. Returns {rating, minor_risk,
    reason, source} where source is 'prescreen', 'model' or 'fallback'."""
    pre = prescreen(title, transcript)
    if pre["minor_risk"]:
        return {"rating": "explicit", "minor_risk": True,
                "reason": "caption or transcript pairs sexual content with a possible minor",
                "source": "prescreen"}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if client is None and api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        except Exception:
            client = None
    if client is not None:
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=200, temperature=0,
                messages=[{"role": "user", "content": PROMPT.format(
                    title=(title or "")[:300], uploader=(uploader or "")[:100],
                    transcript=(transcript or "")[:6000])}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            parsed = parse_rating(raw)
            if parsed:
                # the pre-screen only escalates minor risk; the model may
                # de-escalate "explicit" (a news caption that says "porn")
                if pre["rating"] == "explicit" and parsed["rating"] == "general":
                    parsed["rating"] = "mature"
                parsed["source"] = "model"
                return parsed
        except Exception:
            pass
    # fail safe
    return {"rating": "mature" if pre["matched"] else "general",
            "minor_risk": False, "reason": "gate unavailable; defaulted safely",
            "source": "fallback"}


# ---- what the private path shows a person after a strong AI finding ----
RESOURCES_ADULT = [
    {"name": "StopNCII.org", "url": "https://stopncii.org/",
     "what": "free tool that helps stop intimate images being shared on major platforms"},
    {"name": "Cyber Civil Rights Initiative", "url": "https://cybercivilrights.org/",
     "what": "help and a crisis line for people targeted by image-based abuse"},
    {"name": "Report on the platform", "url": None,
     "what": "every major platform has a dedicated report option for non-consensual intimate imagery and deepfakes — use it; this result can support that report but is not legal evidence on its own"},
]
RESOURCES_MINOR = [
    {"name": "Take It Down (NCMEC)", "url": "https://takeitdown.ncmec.org/",
     "what": "free, anonymous service to remove sexual images of anyone under 18"},
    {"name": "NCMEC CyberTipline", "url": "https://report.cybertip.org/",
     "what": "report sexual exploitation of a child"},
]

MINOR_REFUSAL = ("Glowby will not process this. The content appears to be sexual "
                 "and may involve a person under 18. Nothing was stored. If this is "
                 "about you or someone you know, these services can help remove it.")
EXPLICIT_OFFER = ("Glowby doesn't fact-check adult content, and nothing about this "
                  "video has been stored. If you need to know whether it is "
                  "AI-generated — for example, a face put on someone else's body — "
                  "you can run the AI check on its own. That check is private: no "
                  "caption, no transcript, no link, no share page, never in Trending.")


def mask_profanity(text: str) -> str:
    """Pure (unit-tested): soften strong language in Trending captions
    (f***, s***). Conservative list; the full caption is untouched on
    the result page."""
    if not text:
        return text
    words = r"(fuck\w*|shit\w*|bitch\w*|cunt|asshole|motherfuck\w*|dick(head)?|pussy|cock\w*|whore|slut\w*|nigg\w*|faggot|retard\w*)"
    return re.sub(r"\b" + words + r"\b",
                  lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text, flags=re.I)
