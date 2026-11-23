"""Ad styles for the daily clip (v0.66.27). Four cuts, one engine.

Every style is a list of scenes; each scene has a spoken line (a plain
template filled from the stored check — never a model, so it can never
invent a verdict or a fact) and a picture. The voice track is made
first and the picture follows the words, so nothing waits on screen.
None of them has a "one minute later" — the wait is not the point, the
answer is.

  caught     cold open on the claim, the verdict slams in, receipts, out
  groupchat  the claim lands in the family group chat; before anyone
             replies, the verdict; "send them the receipt"
  guess      true or false? three-two-one, reveal
  receipts   verdict first, then who says so, source by source

Pick the house style in admin (Marketing card) or let it rotate. Voice:
bright, quick, amused — a friend who just found something out.
"""
from __future__ import annotations

import math
import re

from app import marketing as M

STYLES = ("caught", "groupchat", "guess", "receipts", "facebook", "friend", "oneword", "textfroma")
ROTATION = ("facebook", "friend", "groupchat", "caught")

# Two voices. The quick upbeat one for the game-ish cuts; a slower,
# dramatic, human one for the two story cuts (facebook, friend).
DRAMATIC_STYLE = ("You are a 17-year-old telling a friend across the table something that actually happened to you today. "
                  "Human, unpolished, storyteller pacing: real breaths, a dropped quieter voice right before the reveal, a beat of "
                  "silence, then the verdict landing hard. Warm and a little incredulous in the explanation, the smallest laugh "
                  "at the end. Never an announcer, never a commercial, never rushed.")
STYLE_VOICE = {"facebook": DRAMATIC_STYLE, "friend": DRAMATIC_STYLE}

VOICE_STYLE = ("You are a bright, quick, upbeat 17-year-old telling a friend something you just found out. Energetic and "
               "amused, a grin in the voice, never an announcer and never an ad read. Real emphasis, crisp pace, short pauses.")
SCENE_STYLE = {
    "claim": "Say the claim like you're quoting someone with a raised eyebrow; land on 'everyone's sharing it' with a little disbelief.",
    "bubble": "Conspiratorial and amused, like reading a text out loud to the table.",
    "share": "Quick, a beat of suspense on the pause.",
    "guess": "Game-show energy, playful; count down with bounce.",
    "stamp": "Punch the verdict word — delighted if it's inaccurate, satisfied if accurate; then the score, matter-of-fact.",
    "answer": "Plain and kind, like explaining to your mom; one breath.",
    "sources": "Confident, a touch smug on 'receipts'.",
    "end": "Warm, fast, a smile; the last word is the app's name or 'free'.",
    "reel": "Read the claim like you're reading it off the screen in disbelief, slowing on the key words; 'all over Facebook' with a sigh.",
    "hold": "Quiet, almost to yourself: 'hold on'. A pause. Then 'Glowby' plainly.",
    "dm": "Reading a friend's text out loud, a little worried; their line 'everyone needs to see this' in their voice, not yours.",
    "reply": "Calm and kind, like you decided not to fight: 'I didn't argue. I sent the link.' Let the source names land one at a time.",
    "verdict": "Drop your voice before it. Beat. Then the word, heavy and clear. The score quieter, like a receipt.",
}


_NAMES = {"reuters": "Reuters", "apnews": "the AP", "bbc": "the BBC", "nytimes": "the New York Times", "cnn": "CNN", "mayoclinic": "the Mayo Clinic",
          "harvard": "Harvard Health", "cdc": "the CDC", "who": "the World Health Organization", "snopes": "Snopes", "politifact": "PolitiFact",
          "factcheck": "FactCheck dot org", "wikipedia": "Wikipedia", "nasa": "NASA", "fda": "the FDA", "nih": "the NIH", "washingtonpost": "the Washington Post",
          "theguardian": "the Guardian", "npr": "NPR", "usatoday": "USA Today", "forbes": "Forbes", "bloomberg": "Bloomberg", "wsj": "the Wall Street Journal",
          "nbcnews": "NBC News", "abcnews": "ABC News", "cbsnews": "CBS News", "foxnews": "Fox News", "aaa": "triple A", "espn": "ESPN", "healthline": "Healthline",
          "webmd": "WebMD", "clevelandclinic": "the Cleveland Clinic", "nature": "Nature", "science": "Science", "pbs": "PBS", "axios": "Axios", "politico": "Politico",
          "thehill": "The Hill", "latimes": "the L A Times", "britannica": "Britannica", "ap": "the AP", "afp": "AFP", "fullfact": "Full Fact", "leadstories": "Lead Stories"}


def say_source(domain: str) -> str:
    """Pure: 'www.health.harvard.edu' -> 'Harvard Health'; unknown ->
    'mayoclinic dot org' style never — plain name of the site instead."""
    parts = [p for p in domain.lower().split(".") if p not in ("www", "m", "amp", "en")]
    for p in parts:
        if p in _NAMES:
            return _NAMES[p]
    core = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "the source")
    return core.replace("-", " ").title()


def _say_doms(doms: list) -> str:
    names = []
    for d in doms:
        n = say_source(d)
        if n not in names:
            names.append(n)
    return " and ".join(names)


def script(result: dict, style: str) -> list:
    """Pure: [(scene, text)] for a style."""
    rep = result.get("report") or {}
    hs = rep.get("headline_score")
    word = M.lead_word(result)
    title = str(result.get("title") or "this video").strip()[:90].rstrip(".")
    line = M.one_line(result) or f"{word}. The sources are in the app."
    line = re.sub(r"^" + re.escape(word) + r"\s*[—\-–:]\s*", "", line, flags=re.I).strip() or line
    line = M.spoken_line(line, 120)
    doms = M._domains(result, 2)
    score = f"{float(hs):.1f} out of ten." if hs is not None else "No score, and it says why."
    src = f"Receipts: {_say_doms(doms)}." if doms else "Receipts on every claim."
    who = f"Here's who says so: {_say_doms(doms)}." if doms else "Every claim has its sources attached."
    if style == "groupchat":
        return [("bubble", f"This just landed in the family group chat: {title}."),
                ("share", "Before anyone replies… Glowby."),
                ("stamp", f"{word}. {score}"),
                ("answer", line),
                ("end", "Send them the receipt. Glowby is free, link in bio.")]
    if style == "guess":
        return [("guess", f"{title}. True or false? Three, two, one."),
                ("stamp", f"{word}! {score}"),
                ("answer", line),
                ("sources", src),
                ("end", "Any video, checked. Glowby is free, link in bio.")]
    if style == "receipts":
        return [("stamp", f"{word}. {score} {title}."),
                ("sources", who),
                ("answer", line),
                ("end", "Sources on every claim. That's Glowby. Free, link in bio.")]
    if style == "facebook":
        return [("reel", f"This is all over Facebook right now: {title}."),
                ("hold", "Hold on. Share… Glowby."),
                ("verdict", f"{word}. {score}"),
                ("answer", line),
                ("sources", who),
                ("end", "Check before you share. Glowby is free, link in bio.")]
    if style == "friend":
        return [("dm", f"My friend just sent me this: {title}. Quote, everyone needs to see this."),
                ("hold", "Okay. Before I say anything… Glowby."),
                ("verdict", f"{word}. {score}"),
                ("answer", line),
                ("reply", f"So I didn't argue. I sent the link. {_say_doms(doms) if doms else 'The sources'}, right there in it."),
                ("end", "Don't argue. Send the check. Glowby is free, link in bio.")]
    if style == "oneword":
        # TikTok's "answer in one word" format: the creator asks, the AI
        # answers in one word. Glowby's lead word IS one word. Four real
        # checks: the day's pick plus three more from the week.
        rs = [result] + [r for r in (result.get("_extra") or []) if isinstance(r, dict)][:3]
        out = [("setup", "I'll ask you a question. Answer in one word.")]
        for i, r in enumerate(rs):
            t = str(r.get("title") or "this").strip()[:90].rstrip(".?")
            out.append((f"q{i}", f"{t}?"))
            out.append((f"a{i}", ""))  # silent: the stamp answers
        out.append(("end", "Glowby. One word, with receipts. Free, link in bio."))
        return out
    if style == "textfroma":
        # "text message from -A": ominous text cards over the trend sound;
        # no narration at all — add the sound in TikTok.
        return [("t1", ""), ("t2", ""), ("t3", ""), ("reveal", ""), ("end", "")]
    # caught (default)
    return [("claim", f"{title}. Everyone's sharing it."),
            ("stamp", f"{word}. {score}"),
            ("answer", line),
            ("sources", src),
            ("end", "I checked it with Glowby. It's free, link in bio.")]


GAPS = {"claim": 0.5, "bubble": 0.6, "share": 0.9, "guess": 0.3, "stamp": 0.7, "answer": 0.7, "sources": 0.6, "end": 0.0,
        "reel": 0.7, "hold": 1.1, "dm": 0.6, "verdict": 0.9, "reply": 0.6,
        "setup": 0.6, "q0": 0.5, "q1": 0.5, "q2": 0.5, "q3": 0.5, "a0": 1.7, "a1": 1.7, "a2": 1.7, "a3": 1.7,
        # textfroma is silent: each card is on screen for exactly its gap
        "t1": 2.6, "t2": 3.0, "t3": 2.4, "reveal": 3.4}
LEAD_IN = {"caught": 0.5, "groupchat": 0.9, "guess": 0.6, "receipts": 0.3, "facebook": 1.0, "friend": 0.9, "oneword": 0.6, "textfroma": 0.0}
SCENE_STYLE.update({
    "setup": "Flat and matter-of-fact, like reading the rules of a game.",
    "q0": "Ask it like you're genuinely curious, a tiny pause at the end.", "q1": "Same, a little faster.",
    "q2": "Same, almost daring it.", "q3": "The big one: slow, eyebrow raised.",
})
PRE = {"verdict": 0.6}  # the beat of dark before the verdict is spoken


# ---------------------------------------------------------------- picture
# v2 engine (Oct 12): a real phone in frame, word-by-word captions that
# light up with the voice, a push-in on every scene, a camera shake and
# a thud when the verdict stamps, sound effects under the voice (whoosh,
# thud, ding, typing), film grain and a vignette. Poppins for type.
import os
import random
import subprocess
import urllib.parse

W, H = M.W, M.H
_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_FC = {}


def font(size: int, weight: str = "bold"):
    key = (size, weight)
    if key in _FC:
        return _FC[key]
    from PIL import ImageFont
    path = os.path.join(_FONT_DIR, "Poppins-Bold.ttf" if weight == "bold" else "Poppins-Medium.ttf")
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        f = M._font(size)
    _FC[key] = f
    return f


def _wrap(d, text, f, maxw):
    return M._wrap(d, text, f, maxw)


def text(im, s, size, y, color, maxw=470, alpha=255, align="center", x0=40, spacing=1.18, weight="bold", shadow=True):
    """Wrapped text with a soft drop shadow. Returns the bottom y."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    f = font(size, weight); lines = _wrap(d, s, f, maxw); lh = int(size * spacing)
    a = int(max(0, min(255, alpha)))
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=f)
        x = (W - tw) / 2 if align == "center" else x0
        if shadow:
            d.text((x + 2, y + i * lh + 3), ln, font=f, fill=(0, 0, 0, int(a * 0.55)))
        d.text((x, y + i * lh), ln, font=f, fill=tuple(color) + (a,))
    im.alpha_composite(layer)
    return y + len(lines) * lh


_GRAIN = {}


def _grain(seed):
    """A cached grain tile (8 variants, reused) — Pillow's own noise."""
    k = seed % 8
    if k not in _GRAIN:
        from PIL import Image
        n = Image.effect_noise((W, H), 48).convert("RGBA")
        n.putalpha(Image.new("L", (W, H), 9))
        _GRAIN[k] = n
    return _GRAIN[k]


_VIG = {}


def _vignette():
    if "v" not in _VIG:
        from PIL import Image, ImageDraw, ImageFilter
        mask = Image.new("L", (W, H), 0); d = ImageDraw.Draw(mask)
        d.ellipse((-W * 0.2, -H * 0.1, W * 1.2, H * 1.1), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(120))
        dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dark.putalpha(Image.eval(mask, lambda v: int((255 - v) * 0.6)))
        _VIG["v"] = dark
    return _VIG["v"]


def bg(t, hue=(124, 108, 246), flash=0.0, glow=1.0):
    from PIL import Image, ImageDraw, ImageFilter
    base = tuple(min(255, int(c + (255 - c) * flash)) for c in (9, 8, 16))
    im = Image.new("RGBA", (W, H), base + (255,))
    if glow > 0:
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0)); g = ImageDraw.Draw(gl)
        r = 230 + 14 * math.sin(t * 1.1)
        cx, cy = 270 + 30 * math.sin(t * 0.5), 440 + 20 * math.cos(t * 0.4)
        g.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hue + (int(70 * glow),)); gl = gl.filter(ImageFilter.GaussianBlur(80))
        im.alpha_composite(gl)
    return im


def finish(im, t, shake=(0, 0)):
    """Grain + vignette + brand mark; optional camera shake (pixels)."""
    from PIL import Image, ImageDraw
    if shake != (0, 0):
        sh = Image.new("RGBA", (W, H), (9, 8, 16, 255)); sh.alpha_composite(im, (int(shake[0]), int(shake[1]))); im = sh
    im.alpha_composite(_vignette())
    im.alpha_composite(_grain(int(t * 30)))
    d = ImageDraw.Draw(im)
    d.ellipse((30, 36, 50, 56), outline=(160, 150, 230), width=3)
    d.text((58, 32), "glowby", font=font(20, "medium"), fill=(190, 182, 240))
    return im


def push(im, k, amount=0.06):
    """Slow push-in: scale (1+amount) → 1 over the scene, centered."""
    from PIL import Image
    sc = 1.0 + amount * (1.0 - k)
    if sc <= 1.001:
        return im
    nw, nh = int(W * sc), int(H * sc)
    big = im.resize((nw, nh), Image.BILINEAR)
    return big.crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))


def phone(im, screen_fn, x=90, y=92, w=360, h=676, scale=1.0, tilt=0.0):
    """An iPhone-ish device with a dynamic island; screen_fn(draw, sw, sh)
    paints the screen on its own image."""
    from PIL import Image, ImageDraw
    sw, sh = w - 24, h - 24
    scr = Image.new("RGBA", (sw, sh), (14, 14, 20, 255))
    screen_fn(ImageDraw.Draw(scr), scr, sw, sh)
    dev = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0)); dd = ImageDraw.Draw(dev)
    dd.rounded_rectangle((20, 20, 20 + w, 20 + h), radius=46, fill=(28, 28, 34, 255), outline=(70, 70, 80, 255), width=3)
    mask = Image.new("L", (sw, sh), 0); ImageDraw.Draw(mask).rounded_rectangle((0, 0, sw, sh), radius=36, fill=255)
    dev.paste(scr, (32, 32), mask)
    dd.rounded_rectangle((20 + w / 2 - 48, 44, 20 + w / 2 + 48, 70), radius=13, fill=(0, 0, 0, 255))
    if scale != 1.0:
        dev = dev.resize((int(dev.width * scale), int(dev.height * scale)), Image.BILINEAR)
    if tilt:
        dev = dev.rotate(tilt, resample=Image.BICUBIC, expand=True)
    im.alpha_composite(dev, (int(x - 20 - (dev.width - (w + 40)) / 2), int(y - 20 - (dev.height - (h + 40)) / 2)))


# ----------------------------------------------------------- captions
def word_times(segs: list, lead_in: float, gaps: dict, pre: dict | None = None) -> list:
    """Pure: [(start, end, word, scene)] estimated inside each spoken
    segment (no timestamps from the voice API, so words are spread by
    length; close enough at 30 fps)."""
    out, t = [], lead_in
    pre = pre or {}
    for scene, txt, dur in segs:
        t += pre.get(scene, 0.0)
        words = [w for w in re.split(r"\s+", txt.strip()) if w]
        inner = max(0.2, dur - 0.22)  # TTS pads the ends
        weights = [len(re.sub(r"[^\w]", "", w)) + 1.6 for w in words]
        tot = sum(weights) or 1
        cur = t + 0.08
        for w, wt in zip(words, weights):
            d = inner * wt / tot
            out.append((cur, cur + d, w, scene))
            cur += d
        t += dur + gaps.get(scene, 0.5)
    return out


def captions(im, words: list, t: float, col, y=640, size=40, per_line=3):
    """TikTok-style word captions: the current phrase, current word lit."""
    from PIL import ImageDraw
    idx = next((i for i, (a, b, w, sc) in enumerate(words) if a <= t < b), None)
    if idx is None:
        # between words: keep the last phrase on screen briefly
        past = [i for i, (a, b, w, sc) in enumerate(words) if b <= t]
        if not past or t - words[past[-1]][1] > 0.5:
            return
        idx = past[-1]; lit = False
    else:
        lit = True
    scene = words[idx][3]
    # the phrase: a window of `per_line` words around idx, same scene, aligned to groups
    same = [i for i, w in enumerate(words) if w[3] == scene]
    pos = same.index(idx); g0 = pos - pos % per_line
    group = [words[i] for i in same[g0:g0 + per_line]]
    d = ImageDraw.Draw(im); f = font(size)
    clean = [re.sub(r"^[\"“]|[\"”.,;:!?…]+$", "", w[2]) for w in group]
    gap = 14
    widths = [d.textlength(c, font=f) for c in clean]
    total = sum(widths) + gap * (len(clean) - 1)
    if total > 480:  # too wide: smaller font
        f = font(int(size * 0.8)); widths = [d.textlength(c, font=f) for c in clean]; total = sum(widths) + gap * (len(clean) - 1)
    x = (W - total) / 2
    for (a, b, w, sc), c, wd in zip(group, clean, widths):
        on = (a <= t < b) and lit
        colr = col if on else (255, 255, 255)
        d.text((x + 3, y + 4), c, font=f, fill=(0, 0, 0, 200))
        d.text((x, y), c, font=f, fill=colr)
        if on:
            d.rounded_rectangle((x - 2, y + f.size + 4, x + wd + 2, y + f.size + 9), radius=3, fill=col)
        x += wd + gap


# --------------------------------------------------------------- sound
# Tiny synthesized effects, pure Python (no numpy on the server): a
# whoosh (band-passed noise swell), a thud (dropping sine + click), a
# ding, a pop, a typing tick, a 'sent' chirp. Mixed under the voice.
SR = 44100


def _tone(dur, f0, f1=None, amp=0.5, decay=6.0):
    n = int(SR * dur); out = [0.0] * n; ph = 0.0
    for i in range(n):
        tt = i / SR
        f = f0 if f1 is None else f0 + (f1 - f0) * tt / dur
        ph += 2 * math.pi * f / SR
        out[i] = amp * math.sin(ph) * math.exp(-decay * tt)
    return out


def _noise_sweep(dur, amp=0.35, seed=1):
    """Band-passed noise with a sine envelope (a two-pole resonator)."""
    n = int(SR * dur); rnd = random.Random(seed)
    out = [0.0] * n; y1 = y2 = 0.0
    r = 0.985; w = 2 * math.pi * 1500 / SR
    a1, a2 = 2 * r * math.cos(w), -r * r
    for i in range(n):
        x = rnd.uniform(-1, 1)
        y = x + a1 * y1 + a2 * y2
        y2, y1 = y1, y
        env = math.sin(math.pi * i / n) ** 2
        out[i] = amp * 0.08 * y * env
    return out


def sfx(kind: str) -> list:
    if kind == "whoosh":
        return _noise_sweep(0.32, 0.9)
    if kind == "thud":
        body = _tone(0.45, 110, 48, 0.9, 7.0); click = _noise_sweep(0.05, 1.2, 2)
        for i, v in enumerate(click):
            body[i] += v
        return body
    if kind == "ding":
        a = _tone(0.5, 880, None, 0.25, 5.0); b = _tone(0.5, 1320, None, 0.18, 6.0)
        return [x + y for x, y in zip(a, b)]
    if kind == "pop":
        return _tone(0.12, 520, 760, 0.35, 18.0)
    if kind == "tick":
        return _noise_sweep(0.03, 0.8, 3)
    if kind == "sent":
        return _tone(0.25, 700, 1050, 0.25, 9.0)
    return [0.0]


def sfx_track(events: list, total: float, path: str) -> str:
    """events = [(time, kind, gain)] → a stereo WAV the length of the clip."""
    import wave
    from array import array
    n = int(SR * total) + SR
    buf = array("f", bytes(4 * n))
    cache = {}
    for at, kind, gain in events:
        if kind not in cache:
            cache[kind] = sfx(kind)
        s = cache[kind]; i0 = int(at * SR)
        if i0 < 0 or i0 >= n:
            continue
        for j, v in enumerate(s):
            if i0 + j >= n:
                break
            buf[i0 + j] += v * gain
    pcm = array("h", bytes(2 * 2 * n))
    for i in range(n):
        v = max(-0.9, min(0.9, buf[i]))
        pcm[2 * i] = pcm[2 * i + 1] = int(v * 32767)
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    return path


def mix(voice: str, fx: str, out: str, ffmpeg="ffmpeg"):
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", voice, "-i", fx, "-filter_complex",
           "[0:a]aresample=44100,aformat=channel_layouts=stereo,volume=1.0[v];[1:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.55[f];[v][f]amix=inputs=2:normalize=0:duration=first[out]",
           "-map", "[out]", "-c:a", "aac", "-b:a", "128k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("sfx mix failed: " + r.stderr[-300:])
    return out


# --------------------------------------------------------------- scenes
def _ring(d, cx, cy, r, hs, col, width=10):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(38, 37, 54), width=width)
    if hs is not None:
        d.arc((cx - r, cy - r, cx + r, cy + r), start=-90, end=-90 + 360 * max(0.03, float(hs) / 9.9), fill=col, width=width)


def stamp(im, word, col, hs, u, scale_from=2.4):
    """The verdict slams in like a rubber stamp (word and box tilt together)."""
    from PIL import Image, ImageDraw
    k = M._ease(min(1.0, u / 0.3))
    sc = scale_from - (scale_from - 1.0) * k
    size = int(58 * sc)
    layer = Image.new("RGBA", (W * 2, H), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    f = font(min(size, 150)); txt = word.upper(); tw = d.textlength(txt, font=f)
    alpha = int(255 * min(1, u / 0.12))
    x0, y0 = W - tw / 2, 330 - size * 0.72
    d.rounded_rectangle((x0 - 24 * sc, y0 - 6 * sc, x0 + tw + 24 * sc, y0 + size * 1.3 + 6 * sc), radius=int(10 * sc), outline=col + (alpha,), width=max(3, int(5 * sc)))
    d.text((x0, y0), txt, font=f, fill=col + (alpha,))
    layer = layer.rotate(-6, resample=Image.BICUBIC, center=(W, 330))
    layer = layer.crop((W // 2, 0, W // 2 + W, H))
    im.alpha_composite(layer)
    if u > 0.5 and hs is not None:
        text(im, f"{float(hs):.1f} / 10", 44, 430, col, alpha=255 * M._ease((u - 0.5) / 0.3))


def result_card(d, scr, sw, sh, result, col, word, hs, line, doms, u):
    """The Glowby result screen, as the app shows it: ring + lead + the
    one-line answer, then the claims with their verdict dots and sources."""
    d.rectangle((0, 0, sw, sh), fill=(9, 8, 16))
    d.text((24, 22), "glowby", font=font(18, "medium"), fill=(190, 182, 240))
    d.text((sw - 70, 24), "Share", font=font(14, "medium"), fill=(160, 150, 230))
    d.rounded_rectangle((16, 60, sw - 16, 250), radius=20, fill=(20, 19, 30))
    _ring(d, 80, 155, 46, hs if u > 0.3 else 0, col, width=10)
    f = font(28); num = "—" if hs is None else f"{float(hs) * min(1, u / 0.9):.1f}"; tw = d.textlength(num, font=f)
    d.text((80 - tw / 2, 138), num, font=f, fill=col)
    d.text((150, 92), word.upper(), font=font(20), fill=col)
    for i, ln in enumerate(_wrap(d, line, font(16, "medium"), sw - 180)[:4]):
        d.text((150, 124 + i * 22), ln, font=font(16, "medium"), fill=(236, 236, 244))
    y = 272
    d.text((24, y), "CLAIMS", font=font(13, "medium"), fill=(140, 140, 160)); y += 24
    claims = [c for c in (result.get("claims") or []) if isinstance(c, dict)][:3] or [{"text": result.get("title", ""), "verdict": {"verdict_state": "contradicted"}}]
    for i, c in enumerate(claims):
        if u < 1.2 + i * 0.35:
            break
        st = (c.get("verdict") or {}).get("verdict_state") or ""
        dot = {"supported": (74, 222, 128), "partly_supported": (250, 178, 25), "contradicted": (248, 113, 113)}.get(st, (154, 160, 176))
        d.rounded_rectangle((16, y, sw - 16, y + 74), radius=14, fill=(20, 19, 30))
        d.ellipse((30, y + 14, 44, y + 28), fill=dot)
        ctxt = str(c.get("text") or c.get("claim") or "")[:120]
        for j, ln in enumerate(_wrap(d, ctxt, font(14, "medium"), sw - 80)[:2]):
            d.text((54, y + 10 + j * 18), ln, font=font(14, "medium"), fill=(236, 236, 244))
        ev = c.get("evidence") or {}
        srcs = [s for s in (ev.get("fact_checks") or []) + (ev.get("web_sources") or []) if s.get("url")][:3]
        x = 54
        for s_ in srcs:
            nm = say_source(re.sub(r"^www\.", "", urllib.parse.urlparse(s_["url"]).netloc))
            w_ = d.textlength(nm, font=font(12, "medium")) + 18
            d.rounded_rectangle((x, y + 48, x + w_, y + 66), radius=9, fill=(40, 38, 60))
            d.text((x + 9, y + 50), nm, font=font(12, "medium"), fill=(200, 196, 240)); x += w_ + 6
        y += 84
    d.text((24, sh - 36), "every claim · sources you can tap", font=font(13, "medium"), fill=(120, 120, 140))


def fb_reel(d, scr, sw, sh, title, u):
    """Facebook Reels: top bar, the 'video', BREAKING, big caption, rail."""
    d.rectangle((0, 0, sw, sh), fill=(24, 20, 32))
    # fake video: diagonal light
    for i in range(0, sh, 6):
        v = int(28 + 10 * math.sin(i / 40 + u))
        d.line((0, i, sw, i), fill=(v, v - 4, v + 8))
    d.rectangle((0, 0, sw, 62), fill=(16, 16, 22))
    d.text((18, 18), "Reels", font=font(22), fill=(240, 240, 248))
    d.text((sw - 110, 22), "facebook", font=font(14, "medium"), fill=(66, 133, 244))
    if u > 0.35:
        b = M._ease((u - 0.35) / 0.25)
        d.rounded_rectangle((22, 100, 170, 136), radius=8, fill=(220, 40, 40, int(255 * b)))
        d.text((34, 106), "BREAKING", font=font(18), fill=(255, 255, 255))
    if u > 0.7:
        lines = _wrap(d, title.upper(), font(30), sw - 110)
        for i, ln in enumerate(lines[:6]):
            if u > 0.7 + i * 0.12:
                d.text((24, 160 + i * 40), ln, font=font(30), fill=(255, 255, 255))
    for i, lab in enumerate(("1.2M", "48K", "Share")):
        y = sh - 290 + i * 76
        d.ellipse((sw - 62, y, sw - 22, y + 40), fill=(60, 60, 72))
        if lab == "Share" and u > 2.2 and int(u * 4) % 2 == 0:
            d.ellipse((sw - 66, y - 4, sw - 18, y + 44), outline=(255, 255, 255), width=3)
        tw = d.textlength(lab, font=font(12, "medium")); d.text((sw - 42 - tw / 2, y + 46), lab, font=font(12, "medium"), fill=(220, 220, 230))
    d.text((22, sh - 80), "Forwarded · 1.2M views", font=font(14, "medium"), fill=(190, 190, 205))
    d.text((22, sh - 54), "#breaking #news #share", font=font(14, "medium"), fill=(120, 150, 240))


def chat(d, scr, sw, sh, title, u, word="", hs=None, col=None, doms=None, reply=None):
    """iMessage-style thread with Jordan: reel card, their line, then
    (reply) my Glowby link card and their 'oh. ok thanks'."""
    d.rectangle((0, 0, sw, sh), fill=(10, 10, 14))
    d.rectangle((0, 0, sw, 104), fill=(18, 18, 24))
    d.ellipse((sw / 2 - 22, 40, sw / 2 + 22, 84), fill=(90, 90, 110))
    d.ellipse((sw / 2 - 9, 50, sw / 2 + 9, 68), fill=(150, 150, 170)); d.chord((sw / 2 - 18, 64, sw / 2 + 18, 96), 180, 360, fill=(150, 150, 170))
    tw = d.textlength("Jordan", font=font(14, "medium")); d.text((sw / 2 - tw / 2, 86), "Jordan", font=font(14, "medium"), fill=(200, 200, 215))
    a = M._ease(u / 0.4)
    d.rounded_rectangle((14, 118, 214, 164), radius=22, fill=(44, 44, 56))
    d.text((32, 130), "did you see this??", font=font(17, "medium"), fill=(240, 240, 248))
    if a > 0:
        d.rounded_rectangle((14, 176, sw - 60, 352), radius=22, fill=(44, 44, 56))
        d.rectangle((28, 190, 118, 338), fill=(28, 24, 36))
        d.polygon([(60, 248), (60, 282), (88, 265)], fill=(255, 255, 255))
        for i, ln in enumerate(_wrap(d, title, font(18, "medium"), sw - 210)[:4]):
            d.text((132, 194 + i * 24), ln, font=font(18, "medium"), fill=(240, 240, 248))
        d.text((132, 320), "facebook.com/reel/…", font=font(13, "medium"), fill=(120, 150, 240))
    if u > 0.9:
        d.rounded_rectangle((14, 366, 300, 416), radius=22, fill=(44, 44, 56))
        d.text((32, 379), "everyone needs to see this", font=font(17, "medium"), fill=(240, 240, 248))
    if u > 1.5 and reply is None:
        yy = 432
        d.rounded_rectangle((14, yy, 112, yy + 48), radius=22, fill=(44, 44, 56))
        for i in range(3):
            b = 0.5 + 0.5 * math.sin(u * 6 - i * 0.9)
            d.ellipse((34 + i * 24, yy + 17, 48 + i * 24, yy + 31), fill=(150 + int(90 * b),) * 3)
    if reply is not None and reply > 0:
        x0, y0 = 50, 408
        d.rounded_rectangle((x0, y0, sw - 14, y0 + 172), radius=22, fill=(124, 108, 246))
        _ring(d, x0 + 62, y0 + 70, 40, hs, (255, 255, 255), width=8)
        f = font(24); num = "—" if hs is None else f"{float(hs):.1f}"; tw = d.textlength(num, font=f)
        d.text((x0 + 62 - tw / 2, y0 + 54), num, font=f, fill=(255, 255, 255))
        d.text((x0 + 124, y0 + 24), word.upper(), font=font(22), fill=(255, 255, 255))
        d.text((x0 + 124, y0 + 56), "glowby.io/r/…", font=font(14, "medium"), fill=(232, 228, 255))
        for i, dm in enumerate((doms or [])[:2]):
            yy = y0 + 86 + i * 22
            d.ellipse((x0 + 124, yy + 3, x0 + 138, yy + 17), fill=(255, 255, 255))
            d.line((x0 + 127, yy + 10, x0 + 130, yy + 13, x0 + 135, yy + 6), fill=(124, 108, 246), width=2)
            d.text((x0 + 144, yy), say_source(dm), font=font(14, "medium"), fill=(232, 228, 255))
        d.text((x0 + 20, y0 + 138), "checked it. sources inside.", font=font(15, "medium"), fill=(255, 255, 255))
        if reply > 1.6:
            d.rounded_rectangle((14, y0 + 186, 200, y0 + 234), radius=22, fill=(44, 44, 56))
            d.text((32, y0 + 199), "oh. ok thanks", font=font(17, "medium"), fill=(240, 240, 248))


_ICON = {}


def _icon(im, cx, cy, size, alpha):
    """The app icon (app/static/icon-512.png) with rounded corners."""
    from PIL import Image, ImageDraw
    if "i" not in _ICON:
        try:
            ic = Image.open(os.path.join(os.path.dirname(__file__), "static", "icon-512.png")).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0); ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=size // 5, fill=255)
            ic.putalpha(mask)
        except Exception:
            ic = None
        _ICON["i"] = ic
    ic = _ICON["i"]
    if ic is None:
        return
    if alpha < 1:
        ic = ic.copy(); ic.putalpha(ic.getchannel("A").point(lambda v: int(v * alpha)))
    im.alpha_composite(ic, (int(cx - size / 2), int(cy - size / 2)))


def share_sheet(im, d, u, seg, y0=430):
    d.rounded_rectangle((40, y0, 500, y0 + 150), radius=24, fill=(30, 29, 42))
    for i, (lab, c) in enumerate((("Messages", (74, 222, 128)), ("Copy link", (120, 120, 140)), ("Glowby", (124, 108, 246)), ("More", (90, 90, 110)))):
        x = 70 + i * 110; on = lab != "Glowby" or u > seg * 0.55
        d.rounded_rectangle((x, y0 + 22, x + 76, y0 + 98), radius=20, fill=c if on else (60, 55, 110))
        if lab == "Glowby":
            d.ellipse((x + 20, y0 + 42, x + 56, y0 + 78), outline=(255, 255, 255), width=5)
        tw = d.textlength(lab, font=font(14, "medium")); d.text((x + 38 - tw / 2, y0 + 108), lab, font=font(14, "medium"), fill=(200, 200, 215))
    if u > seg * 0.6:
        k = min(1.0, (u - seg * 0.6) / 0.25)
        d.ellipse((328 - 22 * k, y0 + 60 - 22 * k, 328 + 22 * k, y0 + 60 + 22 * k), outline=(255, 255, 255, int(220 * (1 - k * 0.5))), width=4)


# ---------------------------------------------------------------- frame
def frame(result: dict, t: float, style: str, bounds: dict, total: float, words: list | None = None):
    """One frame. `bounds` = {scene: start_seconds}; `words` = word timings."""
    from PIL import ImageDraw
    rep = result.get("report") or {}
    hs = rep.get("headline_score"); word = M.lead_word(result); col = M._color(word)
    title = str(result.get("title") or "").strip()[:110]
    line = M.one_line(result) or "The sources are in the app."
    line = re.sub(r"^" + re.escape(word) + r"\s*[—\-–:]\s*", "", line, flags=re.I).strip() or line
    line = line[:1].upper() + line[1:]
    doms = M._domains(result, 3)
    order = [k for k, _ in sorted(bounds.items(), key=lambda kv: kv[1])]
    cur, start = order[0], 0.0
    for k in order:
        if t >= bounds[k]:
            cur, start = k, bounds[k]
    u = t - start
    nxt = [bounds[k] for k in order if bounds[k] > start]
    seg = (nxt[0] if nxt else total) - start
    kseg = min(1.0, u / max(seg, 0.1))
    is_stamp = cur in ("stamp", "verdict", "reveal") or (style == "oneword" and cur.startswith("a") and cur[1:].isdigit())
    hit = u if (cur in ("stamp", "reveal") or (is_stamp and cur != "verdict")) else (u - 0.6 if cur == "verdict" else -1)
    flash = max(0.0, 0.45 - hit * 2.5) if hit >= 0 else 0.0
    im = bg(t, flash=flash, glow=0.6 if cur in ("reel", "dm", "reply", "answer") else 1.0)
    d = ImageDraw.Draw(im)
    shake = (0, 0)
    if 0 <= hit < 0.4:
        rnd = random.Random(int(t * 30)); amp = 14 * (1 - hit / 0.4)
        shake = (rnd.uniform(-amp, amp), rnd.uniform(-amp, amp))
    cap_y = 700
    if cur == "claim":
        text(im, "everyone's sharing this", 22, 170, (170, 160, 240), weight="medium", alpha=255 * M._ease(u / 0.3))
        text(im, "“" + title + "”", 40, 220, (245, 245, 250), alpha=255 * M._ease(u / 0.4), maxw=470)
    elif cur == "bubble":
        phone(im, lambda dd, s, sw, sh: chat(dd, s, sw, sh, title, u), scale=0.98 + 0.04 * kseg)
        cap_y = 800
    elif cur in ("share", "hold"):
        text(im, "hold on." if cur == "hold" else "before anyone replies…", 42, 220, (245, 245, 250), alpha=255 * M._ease(u / 0.3))
        share_sheet(im, d, u, seg)
    elif cur == "guess":
        text(im, "“" + title + "”", 34, 180, (245, 245, 250), alpha=255 * M._ease(u / 0.3), maxw=470)
        if u > seg * 0.42:
            text(im, "TRUE  or  FALSE?", 44, 470, (250, 178, 25), alpha=255 * M._ease((u - seg * 0.42) / 0.3))
        for i, frac in ((3, 0.62), (2, 0.75), (1, 0.88)):
            if seg * frac <= u < seg * (frac + 0.13):
                k = (u - seg * frac) / (seg * 0.13)
                f = font(int(170 - 50 * k)); tw = d.textlength(str(i), font=f)
                d.text((270 - tw / 2 + 4, 560 + 4), str(i), font=f, fill=(0, 0, 0, 180))
                d.text((270 - tw / 2, 560), str(i), font=f, fill=(255, 255, 255, int(255 * (1 - k * 0.5))))
    elif cur == "reel":
        phone(im, lambda dd, s, sw, sh: fb_reel(dd, s, sw, sh, title, u), scale=0.96 + 0.06 * kseg, tilt=-2 + 2 * kseg)
        cap_y = 800
    elif cur == "dm":
        phone(im, lambda dd, s, sw, sh: chat(dd, s, sw, sh, title, u), scale=0.96 + 0.06 * kseg)
        cap_y = 800
    elif cur in ("stamp", "verdict"):
        if cur == "verdict" and u < 0.6:
            k = 0.4 + 0.6 * abs(math.sin(u * 7))
            text(im, "…", 70, 280, (int(120 * k) + 40,) * 3)
        else:
            stamp(im, word, col, hs, max(0.0, hit), scale_from=2.6 if cur == "verdict" else 2.2)
            if style == "receipts" and hit > 1.0:
                text(im, "“" + title + "”", 26, 520, (200, 200, 215), alpha=255 * M._ease((hit - 1.0) / 0.4), maxw=460, weight="medium")
    elif cur == "answer":
        phone(im, lambda dd, s, sw, sh: result_card(dd, s, sw, sh, result, col, word, hs, line, doms, u), scale=0.96 + 0.05 * kseg)
        cap_y = 800
    elif cur == "sources":
        text(im, "receipts" if style != "receipts" else "who says so", 26, 170, (170, 160, 240), weight="medium")
        for i, dom in enumerate(doms or []):
            if u > i * 0.45:
                a = M._ease((u - i * 0.45) / 0.25); y = 240 + i * 66
                d.rounded_rectangle((60, y, 480, y + 54), radius=27, fill=(30, 29, 42, int(255 * a)))
                d.ellipse((78, y + 15, 102, y + 39), fill=(74, 222, 128, int(255 * a)))
                d.line((84, y + 27, 89, y + 33, 97, y + 21), fill=(8, 7, 15, int(255 * a)), width=3)
                d.text((114, y + 13), say_source(dom) + "  ·  " + dom, font=font(20, "medium"), fill=(240, 240, 248, int(255 * a)))
        if not doms:
            text(im, "sources attached to every claim", 30, 300, (245, 245, 250))
    elif cur == "reply":
        phone(im, lambda dd, s, sw, sh: chat(dd, s, sw, sh, title, 5.0, word=word, hs=hs, col=col, doms=doms, reply=u), scale=0.96 + 0.05 * kseg)
        cap_y = 800
    elif cur == "setup":
        a = M._ease(u / 0.3)
        text(im, "I'll ask you a question.", 40, 300, (245, 245, 250), alpha=255 * a)
        text(im, "Answer in one word.", 40, 360, (250, 178, 25), alpha=255 * M._ease((u - 0.5) / 0.3))
    elif cur.startswith("q") and cur[1:].isdigit():
        i = int(cur[1:]); rs = [result] + [r for r in (result.get("_extra") or []) if isinstance(r, dict)]
        r_ = rs[i] if i < len(rs) else result
        a = M._ease(u / 0.3)
        text(im, f"Q{i + 1}", 22, 190, (170, 160, 240), weight="medium", alpha=255 * a)
        text(im, "“" + str(r_.get("title") or "").strip()[:110].rstrip(".?") + "?”", 36, 230, (245, 245, 250), alpha=255 * a, maxw=470)
        d.rounded_rectangle((50, 560, 490, 616), radius=28, fill=(30, 29, 42))
        d.text((74, 575), "glowby is checking…", font=font(20, "medium"), fill=(154, 160, 176))
        for k in range(3):
            b = 0.5 + 0.5 * math.sin(t * 6 - k * 0.9)
            d.ellipse((420 + k * 18, 582, 430 + k * 18, 592), fill=(150 + int(90 * b),) * 3)
    elif style == "oneword" and cur.startswith("a") and cur[1:].isdigit():
        i = int(cur[1:]); rs = [result] + [r for r in (result.get("_extra") or []) if isinstance(r, dict)]
        r_ = rs[i] if i < len(rs) else result
        w_ = M.lead_word(r_); c_ = M._color(w_); h_ = (r_.get("report") or {}).get("headline_score")
        text(im, "“" + str(r_.get("title") or "").strip()[:110].rstrip(".?") + "?”", 26, 170, (200, 200, 215), maxw=470, weight="medium")
        stamp(im, "Mostly" if w_ == "Mostly accurate" else w_, c_, h_, u, scale_from=2.2)
        if w_ == "Mostly accurate" and u > 0.4:
            text(im, "accurate", 30, 395, c_, alpha=255 * M._ease((u - 0.4) / 0.3))
        text(im, "one word. receipts in the app.", 20, 560, (154, 160, 176), weight="medium", alpha=255 * M._ease((u - 0.9) / 0.3))
    elif style == "textfroma" and cur in ("t1", "t2", "t3"):
        msg = {"t1": "I know what you forwarded last night.", "t2": "“" + title[:100] + "”", "t3": "It wasn't true.    —A"}[cur]
        n = int(len(msg) * min(1.0, u / max(0.6, seg * 0.6)))
        lines_n = len(_wrap(d, msg, font(32), 420))
        d.text((60, 296), "Unknown number · 2:14 AM", font=font(16, "medium"), fill=(154, 160, 176))
        d.rounded_rectangle((40, 330, 500, 350 + 44 * lines_n), radius=22, fill=(44, 44, 56))
        text(im, msg[:n], 32, 346, (245, 245, 250), maxw=420, shadow=False)
    elif cur == "reveal":
        stamp(im, word, col, hs, u, scale_from=2.6)
        text(im, "—A was just Glowby.", 34, 520, (245, 245, 250), alpha=255 * M._ease((u - 1.0) / 0.3))
        text(im, "it checks any video. free. link in bio.", 22, 580, (170, 160, 240), alpha=255 * M._ease((u - 1.6) / 0.3), weight="medium")
    else:  # end
        a = M._ease(u / 0.35)
        _icon(im, 270, 150, 96, a)
        text(im, {"groupchat": "Send them the receipt.", "receipts": "Sources on every claim.", "guess": "Any video. Checked.", "caught": "I checked it with Glowby.",
                  "facebook": "Check before you share.", "friend": "Don't argue. Send the check.", "oneword": "One word. With receipts.", "textfroma": "Check before you share."}.get(style, "Glowby"), 40, 230, (245, 245, 250), alpha=255 * a)
        if u > 0.4:
            k = M._ease((u - 0.4) / 0.3); txt = "Free · glowby.io/get"; f = font(34); tw = d.textlength(txt, font=f)
            d.rounded_rectangle((270 - tw / 2 - 24, 400, 270 + tw / 2 + 24, 468), radius=18, fill=(124, 108, 246, int(255 * k)))
            d.text((270 - tw / 2, 414), txt, font=f, fill=(255, 255, 255, int(255 * k)))
        text(im, "link in bio · App Store", 20, 490, (170, 160, 240), alpha=255 * M._ease((u - 0.8) / 0.3), weight="medium")
    if words:
        captions(im, words, t, col if is_stamp or cur in ("answer", "reply") else (250, 178, 25), y=cap_y)
    im = push(im, kseg, 0.05 if not is_stamp else 0.0)
    return finish(im, t, shake)


def sfx_events(bounds: dict, total: float) -> list:
    ev = []
    order = sorted(bounds.items(), key=lambda kv: kv[1])
    for i, (scene, at) in enumerate(order):
        if i > 0:
            ev.append((at - 0.1, "whoosh", 0.8))
        if scene == "stamp":
            ev.append((at + 0.05, "thud", 1.0))
        if scene == "verdict":
            ev.append((at + 0.62, "thud", 1.0))
        if scene in ("a0", "a1", "a2", "a3", "reveal"):
            ev.append((at + 0.05, "thud", 0.9))
        if scene in ("t1", "t2", "t3"):
            for k in range(8):
                ev.append((at + 0.1 + k * 0.17, "tick", 0.4))
        if scene in ("dm", "bubble"):
            ev.append((at + 0.3, "ding", 0.7))
            for k in range(6):
                ev.append((at + 1.6 + k * 0.18, "tick", 0.5))
        if scene == "reply":
            ev.append((at + 0.25, "sent", 0.8)); ev.append((at + 1.7, "ding", 0.6))
        if scene == "sources":
            for k in range(3):
                ev.append((at + k * 0.45 + 0.05, "pop", 0.7))
        if scene in ("share", "hold"):
            nxt = order[i + 1][1] if i + 1 < len(order) else total
            ev.append((at + (nxt - at) * 0.6, "pop", 0.8))
        if scene == "end":
            ev.append((at + 0.45, "pop", 0.6))
    return ev


def render(result: dict, out_path: str, style: str, speak=None, **kw) -> dict:
    """Voice first, then sound effects, then the picture to match.
    Falls back to the classic silent cut if the voice fails."""
    import tempfile
    style = style if style in STYLES else "caught"
    speak = speak or M.tts
    try:
        with tempfile.TemporaryDirectory() as td:
            audio, bounds, total, segs = M.build_voice_track(result, td, speak=speak, script_=script(result, style),
                                                            gaps=GAPS, lead_in=LEAD_IN[style], scene_style=SCENE_STYLE,
                                                            voice_style=STYLE_VOICE.get(style, VOICE_STYLE), want_segs=True, pre=PRE)
            words = word_times(segs, LEAD_IN[style], GAPS, PRE)
            fx = sfx_track(sfx_events(bounds, total), total, os.path.join(td, "fx.wav"))
            mixed = mix(audio, fx, os.path.join(td, "mix.m4a"))
            M.render_clip(result, out_path, total=total, audio_path=mixed,
                          frame_fn=lambda r, t, tot, b: frame(r, t, style, bounds, tot, words), **kw)
        return {"narrated": True, "total": total, "style": style}
    except Exception as e:
        M.render_clip(result, out_path, **kw)
        return {"narrated": False, "total": M.DEFAULT_BOUNDS["total"], "style": "classic", "voice_error": str(e)[:160]}
