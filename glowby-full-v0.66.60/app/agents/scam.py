"""
Scam lens — the fourth thing Glowby looks for.

A scam is not a factual claim, so the truth score cannot catch it. It is
a PATTERN: a promise, pressure, and an ask. "Guaranteed 10x returns" is
the promise; "only 20 spots left" is the pressure; "send 0.1 BTC to the
address below and get 0.2 back" is the ask. The claims lane may score the
promise low, but nobody who was about to send money reads a 3.1 as "this
is a scam". The scam lens says it plainly — and separately, so the truth
score (months of calibration) is never mixed with a risk score.

Two layers, like the content gate:
  1. a free regex pre-screen over caption + transcript + on-screen text,
     one pattern family per known scam shape (guaranteed returns,
     send-first, giveaways, DM-to-invest, urgency, impersonation of a
     government body or bank, pay-to-work jobs, trading gurus, "recovery"
     agents, celebrity endorsement);
  2. a small model that reads the whole thing ONLY when the pre-screen
     saw something (or the video talks money at all) and answers a fixed
     JSON: risk, the promise in one sentence, the ask, the patterns, and
     who it impersonates, if anyone.

Then two things the claims lane already has are folded in:
  - THE DEEPFAKE ENDORSEMENT: media lane says AI (verified/declared/likely)
    AND the pitch is financial -> risk is at least HIGH. A fake Elon or
    MrBeast pushing a "giveaway" is the single biggest video scam shape,
    and it needs both halves of Glowby.
  - WARNINGS ON RECORD: the names the pitch uses (a company, a token, a
    "mentor") are searched against the FTC, SEC, FINRA, CFTC, FBI and BBB
    Scam Tracker. A hit is shown as a link. No hit clears nothing and is
    said that way.

Wording is a rule, not a style: Glowby says a video MATCHES SCAM PATTERNS
and names the patterns. It never says "X is a scam" about a named person
or business. That sentence is a defamation claim; the pattern sentence is
a description of the video.

Risk bands (never a number on the page):
  none   -> nothing shown
  low    -> one quiet line ("1 scam warning sign")
  medium -> the warning card
  high   -> the warning card, red, above the score
"""

import json
import os
import re

MODEL = os.environ.get("GLOWBY_SCAM_MODEL", "claude-haiku-4-5")
RISKS = ("none", "low", "medium", "high")

# ---- pattern families: one compiled regex each; names are what the reader sees ----
PATTERNS = {
    "guaranteed_returns": re.compile(
        r"\b(guaranteed?\s+(daily\s+|weekly\s+|monthly\s+)?(profits?|returns?|income|payouts?|gains?|roi)|"
        r"risk[- ]free\s+(investment|returns?|profits?|trading)|100\s*%\s*(safe|guaranteed|profit)|"
        r"(double|triple|10x|100x|1000x)\s+your\s+(money|investment|bitcoin|btc|crypto|deposit)|"
        r"\d{2,4}\s*%\s+(a|per)\s+(day|week|month)\b|"
        r"(turn|turned|flip|flipped)\s+\$?\d[\d,]*\s+into\s+\$?\d[\d,]*)", re.I),
    "send_first": re.compile(
        r"\b(send\s+(me\s+|us\s+)?(\$\s?\d|\d[\d.,]*\s*(btc|bitcoin|eth|ethereum|usdt|sol|crypto|dollars?))|"
        r"send\s+(btc|bitcoin|eth|crypto|usdt)\s+to|"
        r"(pay|cover|deposit)\s+(a\s+|the\s+)?(small\s+)?(fee|deposit|tax(es)?|shipping|processing|activation|verification)\s+(first|to\s+(receive|unlock|claim|release|get|withdraw))|"
        r"(processing|activation|release|unlock|clearance|withdrawal)\s+fee|"
        r"to\s+(claim|receive|unlock|release)\s+your\s+(prize|winnings?|reward|refund|funds|money|grant)|"
        r"(gift\s*cards?|apple\s+cards?|google\s+play\s+cards?|steam\s+cards?)\s+(as\s+payment|to\s+pay|code)|"
        r"pay(ment)?\s+(only\s+)?(in|with|via)\s+(bitcoin|btc|crypto|gift\s*cards?|wire\s+transfer|zelle|cash\s*app|western\s+union))", re.I),
    "giveaway": re.compile(
        r"\b(giveaway|give[- ]?away|giving\s+away\s+(\$|\d|free|bitcoin|btc|eth|crypto)|"
        r"free\s+(bitcoin|btc|eth|crypto|money|cash|iphones?|tesla|ps5|playstation|xbox)\b|"
        r"claim\s+your\s+(free|prize|reward|bonus|airdrop)|(airdrop|air\s+drop)\b|"
        r"you\s+(have\s+)?(won|been\s+selected|been\s+chosen)\b|congratulations,?\s+you)", re.I),
    "dm_to_invest": re.compile(
        r"\b(dm\s+me|dm\s+(us|now|for|to)|message\s+me\s+(to|for|on|now)|text\s+me\s+(on|at|to)|inbox\s+me|"
        r"(contact|reach|message|add)\s+(me|us|my\s+(broker|manager|account\s+manager|mentor|team))\s+on\s+(whatsapp|telegram|signal|wechat)|"
        r"(whatsapp|telegram)\s*[:\-]?\s*\+?\d{6,}|"
        r"link\s+in\s+(my\s+)?bio\s+to\s+(invest|start|join|register|sign\s+up|get\s+started|claim)|"
        r"(my|the)\s+(account\s+manager|broker|trader|mentor|expert)\s+(will|can)\s+(help|guide|trade|handle|manage)\s+(you|your)|"
        r"(sign\s+up|register|join)\s+(with|using|through)\s+my\s+(link|referral|code)\s+(to|and)\s+(start|earn|invest|trade))", re.I),
    "urgency": re.compile(
        r"\b(only\s+\d+\s+(spots?|slots?|places?|left|remaining)|(spots?|slots?)\s+(are\s+)?(filling|limited)|"
        r"act\s+(now|fast|today|immediately)|(offer|deal|window)\s+(ends|closes|expires)\s+(today|tonight|soon|in\s+\d+)|"
        r"(expires?|closing)\s+in\s+\d+\s+(minutes?|hours?)|before\s+it'?s\s+too\s+late|last\s+chance|"
        r"(hurry|don'?t\s+wait|don'?t\s+miss\s+(out|this))\b|within\s+(the\s+next\s+)?(24|48)\s+hours|"
        r"(immediate|urgent)\s+action\s+(is\s+)?required)", re.I),
    "impersonation": re.compile(
        r"\b(the\s+)?(irs|social\s+security\s+(administration|office)|medicare|medicaid|dmv|"
        r"(federal|government|homeland)\s+(agent|grant|officer)|customs\s+(and\s+border|office|agent)|"
        r"(amazon|paypal|microsoft|apple|netflix|bank\s+of\s+america|chase|wells\s+fargo|citi(bank)?|coinbase|binance)\s+"
        r"(support|security|fraud)\s+(team|department|desk)|"
        r"your\s+(account|package|delivery|payment|subscription|social\s+security\s+number|ssn)\s+"
        r"(has\s+been|will\s+be|is)\s+(suspended|locked|blocked|compromised|on\s+hold|frozen|cancelled|canceled)|"
        r"(verify|confirm|update)\s+your\s+(account|identity|payment|information|details)\s+(now|immediately|within|to\s+avoid)|"
        r"(warrant|arrest)\s+(for|has\s+been\s+issued)|(remote\s+access|anydesk|teamviewer)\s+to\s+(fix|secure|refund))", re.I),
    "pay_to_work": re.compile(
        r"\b(earn\s+\$\s?\d[\d,]*\s*(\+|k)?\s*(a|per|every|/)\s*(day|hour|week|month)\s+(from\s+home|online|working|doing|with\s+no|no\s+experience)|"
        r"(registration|training|starter\s+kit|onboarding|background\s+check)\s+fee\s+(of\s+\$|required|to\s+(start|begin|apply))|"
        r"(no\s+experience|anyone\s+can)\s+[^.]{0,40}\$\s?\d{3,}|"
        r"(mystery\s+shopper|reshipping|package\s+(forwarding|handler))\s+(job|position|opportunity)|"
        r"(task|like|review|rating)\s+(app|jobs?|platform)\s+[^.]{0,40}(earn|paid|commission))", re.I),
    "trading_guru": re.compile(
        r"\b(trading\s+(bot|signals?|robot|algorithm|mentor(ship)?|academy|masterclass)|"
        r"(forex|crypto|binary\s+options?|options?)\s+(signals?|mentor|guru|coach|masterclass)|"
        r"(my|our)\s+(students?|clients?|members?|investors?)\s+(made|earned|profited|are\s+making)\s+\$|"
        r"ai\s+(trading|investment)\s+(bot|platform|app|system)|automated\s+(trading|profits?)|"
        r"(passive|daily)\s+income\s+(of\s+)?\$\s?\d|"
        r"(quantum|immediate|bitcoin)\s+(ai|edge|code|profit|era|revolution|trader|loophole)\b)", re.I),
    "recovery": re.compile(
        r"\b(recover(y)?\s+(agent|expert|service|specialist|hacker|firm)s?|"
        r"(get|recover|retrieve)\s+your\s+(lost|stolen|scammed)\s+(money|crypto|funds|bitcoin|investment)\s+back|"
        r"(ethical\s+)?hackers?\s+for\s+hire|(recovered|got\s+back)\s+(my|all\s+my)\s+(lost|stolen)\s+(funds|crypto|money))", re.I),
    "sextortion": re.compile(
        r"\b(i\s+have\s+(your|ur)\s+(nudes?|photos?|pics?|pictures?|videos?|recordings?)|"
        r"(i'?ll|i\s+will|we'?ll|we\s+will)\s+(send|leak|post|share|release|expose)\s+(them|it|this|these|your\s+(nudes?|photos?|pics?|videos?))\s+to\s+(your|all\s+your|everyone|ur)\s*(friends|family|followers|contacts|school|parents|wife|husband)?|"
        r"(pay|send)\s+(me\s+)?\$?\d[\d,]*\s+(or|otherwise|unless)\s+[^.]{0,40}(post|share|send|leak|expose|release)|"
        r"(recorded|hacked)\s+(you|your\s+(webcam|camera|screen))\s+[^.]{0,60}(bitcoin|btc|pay|send)|"
        r"(your|ur)\s+(nudes?|intimate\s+(photos?|pics?|videos?))\s+[^.]{0,40}(unless|or\s+else|if\s+you\s+don'?t\s+pay))", re.I),
    "celebrity_endorsement": re.compile(
        r"\b(elon\s+musk|musk|mrbeast|mr\s+beast|taylor\s+swift|oprah|bezos|zuckerberg|mark\s+cuban|"
        r"warren\s+buffett|shark\s+tank|dragons'?\s+den|cardone|"
        r"tom\s+hanks|kylie|kardashian|drake|ronaldo|messi|the\s+rock|dwayne\s+johnson|"
        r"prime\s+minister|president|governor|first\s+lady)\b"
        r"[^.]{0,120}\b(invest(ment|ing)?\s+(platform|app|program|opportunity|secret)|crypto\s+(giveaway|platform|app)|"
        r"bitcoin\s+(giveaway|app|platform)|btc\s+giveaway|giveaway|trading\s+(platform|app|bot|program)|passive\s+income|"
        r"doubl(e|ing)\s+your|wealth\s+secret|keto|gummies|cbd|weight\s+loss\s+(secret|pill|gummies))\b", re.I),
}

# a video "talks money" — the cheap trigger for the model read even when
# no pattern family fired
_MONEY_RE = re.compile(
    r"\b(invest\w*|crypto|bitcoin|btc|eth\b|ethereum|usdt|forex|profit\w*|returns?|earn\w*|"
    r"passive\s+income|giveaway|prize|winnings?|refund|grant|loan|\$\s?\d|\d\s?(dollars|usd)\b|"
    r"gift\s*cards?|wire\s+transfer|zelle|cash\s*app|venmo|paypal|bank\s+account|"
    r"account\s+(suspended|locked|compromised)|ssn|social\s+security)", re.I)

# a video that WARNS about scams quotes the same phrases; the fallback
# path (no model) must not flag the warning as the thing it warns about
_WARNING_VIDEO_RE = re.compile(
    r"\b(scammers?|scam\s+alert|beware|don'?t\s+fall\s+for|never\s+(pay|send)|"
    r"red\s+flags?|how\s+to\s+spot|warning\s+signs|is\s+a\s+scam\??|fraud\s+alert)\b", re.I)

# what a financial pitch looks like — used with the media lane
_FINANCIAL_RE = re.compile(
    r"\b(invest\w*|crypto|bitcoin|btc|eth\b|ethereum|token|coin|giveaway|trading|forex|"
    r"passive\s+income|profits?|returns?|doubl(e|ing)\s+your|platform|wealth|"
    r"opportunity|program|earn(ings)?|payout|deposit|withdraw\w*)\b", re.I)

# regulators and trackers whose pages count as "warnings on record"
WARNING_HOSTS = ("ftc.gov", "sec.gov", "finra.org", "cftc.gov", "ic3.gov", "fbi.gov",
                 "bbb.org", "consumerfinance.gov", "fca.org.uk", "asic.gov.au",
                 "actionfraud.police.uk", "scamwatch.gov.au", "canada.ca", "occ.gov",
                 "dfpi.ca.gov", "ag.ny.gov", "texasattorneygeneral.gov", "oag.ca.gov")
_WARN_WORDS = re.compile(
    r"\b(scam\w*|fraud\w*|alert|warning|charged|charges|complaint|indict\w*|cease\s+and\s+desist|"
    r"enforcement|action\s+against|ponzi|pyramid|unregistered|investor\s+alert|do\s+not\s+deal|"
    r"suspend\w*|shut\s+down|arrest\w*|convict\w*|settle\w*|refund\w*)\b", re.I)

# human-readable pattern names (what the card shows)
PATTERN_NAMES = {
    "guaranteed_returns": "promises guaranteed or outsized returns",
    "send_first": "asks for money, crypto or gift cards up front",
    "giveaway": "giveaway / “you won” framing",
    "dm_to_invest": "pushes you to DM, WhatsApp or Telegram to “invest”",
    "urgency": "pressure to act immediately",
    "impersonation": "impersonates a government body, bank or big company",
    "pay_to_work": "job that asks for a fee or promises pay with no work",
    "trading_guru": "trading-bot / signals / mentor pitch",
    "recovery": "“recovery agent” offering to get lost money back",
    "celebrity_endorsement": "public figure attached to a money pitch",
    "sextortion": "threat to share intimate images unless paid",
    "deepfake_endorsement": "AI-generated footage carrying a money pitch",
    "regulator_warning": "a regulator or scam tracker has a warning on record",
}


def prescreen(title: str = "", transcript: str = "", ocr_text: str = "") -> dict:
    """Pure (unit-tested). Which pattern families fire on the text, and
    whether the video talks money at all. Never a verdict by itself."""
    text = "\n".join(t for t in (title, transcript, ocr_text) if t)
    hits = [name for name, rx in PATTERNS.items() if rx.search(text)]
    return {"patterns": hits, "talks_money": bool(_MONEY_RE.search(text)),
            "financial": bool(_FINANCIAL_RE.search(text))}


PROMPT = """You are the scam lens for Glowby, a fact-checking app. You read a \
social-media video's caption, transcript and on-screen text and decide whether \
it MATCHES KNOWN SCAM PATTERNS. You describe the video; you never declare a \
named person or business "a scam". Answer with ONLY a JSON object (no prose, \
no code fences):
{{"risk": "none" | "low" | "medium" | "high",
 "promise": "one plain sentence: what the viewer is promised, or null",
 "ask": "what the viewer is asked to do (send crypto first, DM on Telegram, pay a fee, click the link in bio, give account details), or null",
 "patterns": ["guaranteed_returns", "send_first", "giveaway", "dm_to_invest", "urgency", "impersonation", "pay_to_work", "trading_guru", "recovery", "celebrity_endorsement", "sextortion"],
 "impersonates": "the government body, company or public figure whose identity the pitch borrows, or null",
 "entities": ["company, platform, token, app or person names the pitch relies on — up to 4, exact spelling"],
 "reason": "two short sentences a teenager understands: what makes this look like a scam, or why it does not"}}

Bands:
- "high": a money ask plus a classic shape (send-first, guaranteed returns, \
giveaway needing a deposit, impersonated authority demanding payment or \
details, recovery agent, pay-to-work). This is what a scam looks like. A \
threat to share intimate images unless paid ("sextortion") is always "high".
- "medium": a financial pitch with two or more warning signs but no direct \
ask yet (guru + urgency + DM me), or an impersonation without a payment ask.
- "low": one warning sign in an otherwise ordinary video (a creator saying \
"link in bio", an ad with urgency language, a legitimate giveaway by a known \
brand with no deposit).
- "none": no scam shape — news about scams, education, warnings, a normal \
product ad, a normal creator, a legitimate company's own channel. A video \
that WARNS about scams is "none".

Never rate "high" on tone alone: the ask matters. Selling a course is not a \
scam; selling a course that guarantees profit is a warning sign; a course \
that needs a "release fee" to withdraw your profit is the shape itself. \
Regulated, well-known companies advertising their own products are "none" \
unless the video asks the viewer to move money to a third party.

Pre-screen hits (regex, may be noisy): {hits}

Caption/title: {title}
Uploader: {uploader}
Transcript and on-screen text (may be empty):
\"\"\"{transcript}\"\"\""""


def parse_scam(raw: str) -> dict | None:
    """Pure (unit-tested): model JSON -> validated dict or None."""
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
    risk = str(d.get("risk", "")).strip().lower()
    if risk not in RISKS:
        return None
    pats = d.get("patterns") or []
    if not isinstance(pats, list):
        pats = []
    pats = [str(p).strip().lower() for p in pats if str(p).strip().lower() in PATTERN_NAMES]
    ents = d.get("entities") or []
    if not isinstance(ents, list):
        ents = []
    ents = [str(e).strip()[:60] for e in ents if str(e).strip()][:4]

    def _s(k, n):
        v = d.get(k)
        return (str(v).strip()[:n] or None) if v not in (None, "", "null") else None

    return {"risk": risk, "promise": _s("promise", 240), "ask": _s("ask", 200),
            "patterns": pats, "impersonates": _s("impersonates", 80),
            "entities": ents, "reason": _s("reason", 500) or ""}


def _sanitize(text):
    """The wording rule, enforced in code: 'is a scam' -> 'matches scam
    patterns'. The model is told; this makes sure."""
    if not text:
        return text
    text = re.sub(r"\b(is|are|was|were)\s+(a\s+|an\s+)?(obvious\s+|clear\s+|classic\s+|definite\s+|total\s+)?(scam|fraud|ponzi\s+scheme|con)\b",
                  r"\1 consistent with scam patterns", text, flags=re.I)
    text = re.sub(r"\b(is|are)\s+(a\s+)?(scammer|fraudster|con\s+artist)s?\b",
                  r"\1 using scam patterns", text, flags=re.I)
    return text


AI_ORIGINS = ("verified_ai_provenance", "declared_ai", "likely_synthetic")


def combine_with_media(scam: dict, authenticity: dict | None, text: str = "") -> dict:
    """Pure (unit-tested): the deepfake-endorsement rule. When the media
    lane says the footage is AI and the pitch is financial, the risk is at
    least HIGH and the reason says why. Real AI creators making finance
    education are protected by the 'financial' test — talking about
    crypto is not a pitch; the pitch needs a shape (any pattern) or a
    money ask."""
    out = dict(scam or {})
    out.setdefault("patterns", [])
    au = authenticity or {}
    origin = au.get("origin_result")
    if origin not in AI_ORIGINS:
        return out
    financial = bool(_FINANCIAL_RE.search(text or "")) or bool(out.get("ask"))
    has_shape = bool(out.get("patterns")) or out.get("risk") in ("medium", "high")
    if financial and has_shape:
        out["risk"] = "high"
        if "deepfake_endorsement" not in out["patterns"]:
            out["patterns"] = ["deepfake_endorsement"] + list(out["patterns"])
        why = {"verified_ai_provenance": "carries signed AI provenance",
               "declared_ai": "is labeled AI-generated by its creator or platform",
               "likely_synthetic": "shows strong synthetic signals in the detector"}[origin]
        out["media_note"] = (f"The footage {why}, and it is used to carry a money pitch. "
                             "A fake person promising you money is the most common video scam shape.")
    return out


def filter_warnings(results: list, entities: list) -> list:
    """Pure (unit-tested): keep search results that are (a) on a regulator /
    scam-tracker host and (b) about a warning, and (c) mention one of the
    entities. Returns [{name, url, what}] ready for the card."""
    out, seen = [], set()
    ents = [e.lower() for e in (entities or []) if e and len(e) >= 3]
    for r in results or []:
        url = str(r.get("url", ""))
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
        if not any(host == h or host.endswith("." + h) for h in WARNING_HOSTS):
            continue
        blob = f"{r.get('title', '')} {r.get('snippet', '')}"
        if not _WARN_WORDS.search(blob):
            continue
        if ents and not any(e in blob.lower() for e in ents):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"name": str(r.get("title", ""))[:120] or host, "url": url,
                    "what": host, "snippet": str(r.get("snippet", ""))[:220]})
    return out[:4]


def search_warnings(entities: list, query_fn=None) -> list:
    """Regulator / tracker lookup for the names the pitch relies on. One
    Brave query per entity (max 3), filtered by filter_warnings. Never
    raises; without a search key returns []."""
    ents = [e for e in (entities or []) if e and len(e) >= 3][:3]
    if not ents:
        return []
    if query_fn is None:
        try:
            from app.agents.evidence import brave_available, _brave_query
            if not brave_available():
                return []
            query_fn = _brave_query
        except Exception:
            return []
    results = []
    for e in ents:
        q = (f'"{e}" scam OR fraud OR warning OR alert '
             "(site:ftc.gov OR site:sec.gov OR site:finra.org OR site:cftc.gov OR "
             "site:bbb.org OR site:ic3.gov OR site:consumerfinance.gov)")
        try:
            results.extend(query_fn(q, 8))
        except Exception:
            continue
    return filter_warnings(results, ents)


def band_for(score) -> str:
    """Pure: engine score -> card band."""
    if score is None:
        return "none"
    return "high" if score >= 65 else ("medium" if score >= 40 else ("low" if score >= 20 else "none"))


def assess(title: str = "", transcript: str = "", uploader: str = "",
           authenticity: dict | None = None, ocr_text: str = "",
           client=None, search: bool = True, query_fn=None) -> dict:
    """The whole lens, now on the engine (scamengine.analyze): regex +
    model extraction, deterministic rules with floors, independent
    verification, adjudication, capped score, separate confidence, and
    type-aware safety actions. This function maps the engine's JSON onto
    the card. Never raises."""
    from app.agents import scamengine
    pre = prescreen(title, transcript, ocr_text)
    text = "\n".join(t for t in (title, transcript, ocr_text) if t)
    au = authenticity or {}
    base = {"risk": "none", "score": None, "patterns": [], "pattern_names": [], "promise": None, "ask": None,
            "impersonates": None, "entities": [], "reason": "", "warnings": [], "help": None,
            "source": "prescreen", "ran": True, "report": None}
    # the model read costs ~0.3c: spend it only when the text has a signal
    rx = scamengine.extract_regex(scamengine.normalize(text)) if text.strip() else None
    has_signal = bool(rx and (rx["credentials_requested"] or rx["payment_methods"] or rx["urls"] or rx["emails"]
                              or rx["phones"] or rx["promises"] or rx["threats"] or rx["remote_access"]
                              or rx["release_payment"] or rx["already_done"] or rx["sectors"]))
    if not (pre["patterns"] or pre["talks_money"] or has_signal or (au.get("origin_result") in AI_ORIGINS and pre["financial"])):
        return base
    rep = scamengine.analyze(text, context={"authenticity": au, "uploader": uploader}, client=client,
                             query_fn=query_fn, verify_enabled=search, use_model=True)
    ex = rep.get("extracted") or {}
    out = dict(base)
    out["report"] = rep
    out["score"] = rep.get("scam_risk_score")
    out["risk"] = band_for(out["score"])
    out["verdict"] = rep.get("verdict_label")
    out["confidence"] = rep.get("confidence")
    out["confidence_label"] = rep.get("confidence_label")
    out["source"] = "engine+model" if (rep.get("audit") or {}).get("model_extracted") else "engine"
    # chips: the engine's signals, strongest first (never the weights)
    out["pattern_names"] = [f["signal"] for f in (rep.get("risk_factors") or [])][:6]
    out["patterns"] = list(rep.get("scam_types") or [])
    for p in pre["patterns"]:
        if p not in out["patterns"]:
            out["patterns"].append(p)
    if any("AI-generated footage" in f.get("signal", "") for f in (rep.get("risk_factors") or [])):
        if "deepfake_endorsement" not in out["patterns"]:
            out["patterns"].insert(0, "deepfake_endorsement")
        out["media_note"] = ("The footage is AI-generated by Glowby's media check, and it is used to carry a money pitch. "
                             "A fake person promising you money is the most common video scam shape.")
    out["promise"] = _sanitize(ex.get("one_line"))
    acts = rep.get("requested_actions") or []
    out["ask"] = "; ".join(acts[:2]) if acts else None
    vf = rep.get("verification") or {}
    if ex.get("organization") and (vf.get("domain_matches_official_domain") is False or vf.get("claimed_identity_confirmed") is False
                                   or any(t in (rep.get("scam_types") or []) for t in ("government_bank_impersonation", "phishing_account_takeover"))):
        out["impersonates"] = ex.get("organization")
    out["entities"] = [x for x in (ex.get("organization"), ex.get("investment_entity")) if x]
    out["warnings"] = [{"name": w.get("name"), "url": w.get("url"), "what": w.get("what")} for w in (vf.get("sources") or []) if w.get("url")][:4]
    out["reason"] = _sanitize(rep.get("summary") or "")
    out["verification"] = {k: vf.get(k) for k in ("claimed_identity_confirmed", "domain_matches_official_domain",
                                                   "organization_exists", "official_domain", "external_warning_found",
                                                   "investment_registered", "url_reputation_checked", "notes")}
    out["already_done"] = list(ex.get("already_done") or [])
    if out["risk"] in ("medium", "high"):
        hp = help_for({"patterns": out["patterns"], "ask": out["ask"], "promise": out["promise"], "reason": out["reason"]}, text)
        if "sextortion" not in out["patterns"] and "extortion_blackmail" not in out["patterns"]:
            hp["now"] = list(rep.get("recommended_actions") or [])[:5] or hp["now"]
        hp["by_state"] = rep.get("actions_by_state") or {}
        hp["escalate"] = rep.get("escalate") or []
        hp["helper"] = rep.get("helper")
        out["help"] = hp
    out["ran"] = True
    return out


_PERSON_RE = re.compile(r"\b(a\s+(man|woman|person|guy|girl|soldier|doctor|officer|nurse|pilot)|portrait|selfie|headshot|profile\s+(photo|picture)|smiling|posing|in\s+(military\s+)?uniform|holding\s+(a\s+)?(baby|dog|puppy|fish)|wearing\s+(a\s+)?(suit|scrubs|uniform))\b", re.I)


def wants_photo_check(desc: str, scam_out: dict | None) -> bool:
    """Pure: an uploaded photo of a PERSON (not a screenshot of text) gets
    the 'where else does this photo appear' check — the thing the WSJ
    sisters did by hand to unmask their mother's suitor."""
    if not desc or desc.lstrip().startswith("[MESSAGE TEXT]"):
        return False
    if (scam_out or {}).get("patterns") and "romance" in (scam_out or {}).get("patterns", []):
        return True
    return bool(_PERSON_RE.search(desc))


def apply_photo(out: dict, rs: dict | None) -> dict:
    """Pure (unit-tested): fold a reverse-image result into the lens.
    Matches on other pages are shown as a fact with links; with a romance
    shape, 2+ matches raise the band to at least high (score 65). No
    matches is said honestly and clears nothing."""
    out = dict(out or {})
    if not rs or rs.get("assessment_status") != "completed":
        return out
    pages = list(rs.get("pages") or [])
    n = int(rs.get("match_count") or len(pages))
    photo = {"count": n, "pages": pages[:6], "earliest": rs.get("earliest")}
    if n == 0:
        photo["note"] = "No matching appearances found on the open web. That proves nothing — most private photos never appear anywhere."
    else:
        photo["note"] = (f"This photo appears on {n} other page{'s' if n != 1 else ''}"
                         + (f"; earliest dated match {rs['earliest']['date']} ({rs['earliest']['domain']})" if rs.get("earliest") else "")
                         + ". If it is a stranger's profile picture, check whether those pages show the same name — a photo living under other names is the classic romance-scam sign.")
    out["photo"] = photo
    if n >= 2 and ("romance" in (out.get("patterns") or []) or "romance" in ((out.get("report") or {}).get("scam_types") or [])):
        out["score"] = max(65, out.get("score") or 0)
        out["risk"] = band_for(out["score"])
        out["verdict"] = out.get("verdict") if (out.get("score") or 0) >= 85 else "High scam risk"
        sig = f"The profile photo appears on {n} other pages — it may belong to someone else"
        out["pattern_names"] = [sig] + list(out.get("pattern_names") or [])[:5]
        if "photo_reused" not in (out.get("patterns") or []):
            out["patterns"] = ["photo_reused"] + list(out.get("patterns") or [])
        out["ran"] = True
    return out


def apply_media(out: dict, authenticity: dict | None, text: str = "") -> dict:
    """Pure (unit-tested): the deepfake-endorsement rule applied AFTER the
    engine ran (the media lane finishes later than the text lane). AI
    origin + a financial pitch with a shape -> at least high (score 85),
    a critical factor on top, and the media note."""
    out = dict(out or {})
    au = authenticity or {}
    if au.get("origin_result") not in AI_ORIGINS or not out.get("ran"):
        return out
    rep = out.get("report") or {}
    ex = rep.get("extracted") or {}
    financial = bool(_FINANCIAL_RE.search(text or "")) or bool(ex.get("payment_method")) or bool(
        set(ex.get("promises") or []) & {"investment_return", "prize", "recovered_funds", "job"})
    has_shape = out.get("risk") in ("low", "medium", "high") or bool(rep.get("risk_factors"))
    if not (financial and has_shape):
        return out
    if "deepfake_endorsement" not in out.get("patterns", []):
        out["patterns"] = ["deepfake_endorsement"] + list(out.get("patterns") or [])
    sig = "AI-generated footage carrying a money pitch — a fake person promising you money"
    if not any(sig in n for n in out.get("pattern_names") or []):
        out["pattern_names"] = [sig] + list(out.get("pattern_names") or [])[:5]
    sc = out.get("score")
    out["score"] = max(85, sc or 0)
    out["risk"] = band_for(out["score"])
    out["verdict"] = "Critical scam risk"
    why = {"verified_ai_provenance": "carries signed AI provenance",
           "declared_ai": "is labeled AI-generated by its creator or platform",
           "likely_synthetic": "shows strong synthetic signals in the detector"}[au["origin_result"]]
    out["media_note"] = (f"The footage {why}, and it is used to carry a money pitch. "
                         "A fake person promising you money is the most common video scam shape.")
    if isinstance(rep, dict):
        rep = dict(rep)
        rep["scam_risk_score"] = out["score"]
        rep["verdict"], rep["verdict_label"] = "critical_scam_risk", "Critical scam risk"
        rep["risk_factors"] = [{"signal": sig, "severity": "critical", "evidence_span": au.get("display") or ""}] + list(rep.get("risk_factors") or [])[:9]
        rep["safe_to_proceed"] = False
        out["report"] = rep
    if out.get("help") is None:
        out["help"] = help_for({"patterns": out["patterns"], "ask": out.get("ask"), "promise": out.get("promise"), "reason": out.get("reason")}, text)
        out["help"]["now"] = list(rep.get("recommended_actions") or [])[:5] or out["help"]["now"]
        out["help"]["by_state"] = rep.get("actions_by_state") or {}
        out["help"]["escalate"] = rep.get("escalate") or []
    return out


# ---- help: what to do now, type-aware (pure; unit-tested) ----
# Numbers verified Sept 2026: AARP Fraud Watch Network Helpline (free, any
# age, Mon-Fri 8am-8pm ET); DOJ National Elder Fraud Hotline (Mon-Fri
# 10am-6pm ET); Canadian Anti-Fraud Centre (Mon-Fri 10am-4:45pm ET); UK
# Report Fraud / Action Fraud (24/7 phone). Glowby is not a helpline; these
# are the people who are.
HELPLINES = [
    {"name": "AARP Fraud Watch Network Helpline", "phone": "877-908-3360", "url": "https://www.aarp.org/money/scams-fraud/helpline/",
     "what": "free, any age, no membership needed — trained specialists, Mon–Fri 8am–8pm ET (U.S.)"},
    {"name": "National Elder Fraud Hotline (U.S. DOJ)", "phone": "833-372-8311", "url": "https://ovc.ojp.gov/program/elder-fraud-abuse/national-elder-fraud-hotline",
     "what": "for anyone 60+ or helping someone who is; a case manager walks you through reporting, Mon–Fri 10am–6pm ET"},
    {"name": "Report Fraud (UK, formerly Action Fraud)", "phone": "0300 123 2040", "url": "https://www.reportfraud.police.uk/",
     "what": "the UK's national reporting centre; phone 24/7"},
    {"name": "Canadian Anti-Fraud Centre", "phone": "1-888-495-8501", "url": "https://antifraudcentre-centreantifraude.ca/report-signalez-eng.htm",
     "what": "Mon–Fri 10am–4:45pm ET"},
    {"name": "Scamwatch (Australia)", "phone": None, "url": "https://www.scamwatch.gov.au/report-a-scam",
     "what": "report online"},
]

_HELP_NOW = {
    "default": ["Stop. Don't reply, don't click, don't send anything yet.",
                "Look the company or person up yourself — on a regulator's site, not the link they gave you.",
                "Talk it over with one person you trust before any money moves. Scams depend on you deciding alone and fast."],
    "impersonation": ["Hang up or close the message. Real agencies and banks never demand payment in gift cards, crypto or wire — and never threaten arrest over the phone.",
                      "Call the organisation back yourself on the number printed on your card or on its official site, not the number in the message."],
    "sextortion": ["Do not pay. Paying does not make it stop; it marks you as someone who pays.",
                   "Stop responding, but do NOT delete anything — screenshots and usernames are the evidence.",
                   "Block them and report the account on the platform.",
                   "If you're under 18: tell a trusted adult now, and use Take It Down — it's free and anonymous. This is not your fault."],
    "recovery": ["Anyone who contacts you offering to recover lost money for a fee is running the second scam. Only law enforcement and your own bank can trace funds, and they never charge up front."],
}
_HELP_SENT = {
    "bank": "Money from a bank account or card: call your bank's fraud line NOW (the number on the back of your card). Ask them to reverse the payment or recall the wire; every hour matters.",
    "gift_cards": "Gift cards: contact the card company immediately (Apple, Google Play, Amazon, Steam, Target…) with the card numbers and receipt — they can sometimes freeze the balance.",
    "crypto": "Crypto: tell the exchange or wallet you sent from right away and file with the FBI's IC3 — transfers can sometimes be traced. Ignore anyone who then offers to \"recover\" it for a fee.",
    "identity": "Gave a Social Security number, ID, passwords or account details: go to IdentityTheft.gov for a step-by-step plan, freeze your credit at all three bureaus, and change the passwords you shared.",
    "images": "Sent images: NCMEC's Take It Down (under 18) or StopNCII.org (18+) can stop them being posted on major platforms; the FBI takes sextortion reports at IC3.",
}
_REPORT = [
    {"name": "Report to the FTC", "url": "https://reportfraud.ftc.gov/", "what": "U.S. consumer fraud report, shared with law enforcement"},
    {"name": "FBI IC3", "url": "https://www.ic3.gov/", "what": "internet crime, especially if money already moved"},
    {"name": "IdentityTheft.gov", "url": "https://www.identitytheft.gov/", "what": "if personal information was given"},
    {"name": "Take It Down (NCMEC)", "url": "https://takeitdown.ncmec.org/", "what": "free, anonymous removal of images of anyone under 18"},
    {"name": "Check a broker or adviser", "url": "https://brokercheck.finra.org/", "what": "anyone selling investments in the U.S. must be registered"},
]
_GIFT_RE = re.compile(r"gift\s*cards?|apple\s+cards?|google\s+play|steam\s+cards?", re.I)
_CRYPTO_RE = re.compile(r"\b(crypto|bitcoin|btc|eth|ethereum|usdt|wallet|token|coin)\b", re.I)
_IDENT_RE = re.compile(r"\b(ssn|social\s+security|password|login|account\s+(details|number|information)|identity|id\s+card|passport|bank\s+details|card\s+number)\b", re.I)


def help_for(scam: dict, text: str = "") -> dict:
    """Pure (unit-tested): the card's help block, chosen by what the
    pitch asked for. {now: [...], already_sent: [...], talk: [...],
    report: [...]}. Every list is short; the reader is in a hurry."""
    pats = set(scam.get("patterns") or [])
    if "extortion_blackmail" in pats:
        pats.add("sextortion")
    if "government_bank_impersonation" in pats or "phishing_account_takeover" in pats or "tech_support_remote_access" in pats:
        pats.add("impersonation")
    if "recovery" in pats:
        pats.add("recovery")
    if "investment_crypto" in pats:
        pats.add("trading_guru")
    blob = " ".join(str(scam.get(k) or "") for k in ("ask", "promise", "reason")) + " " + (text or "")[:4000]
    now = []
    if "sextortion" in pats:
        now = list(_HELP_NOW["sextortion"])
    else:
        if "impersonation" in pats:
            now += _HELP_NOW["impersonation"]
        if "recovery" in pats:
            now += _HELP_NOW["recovery"]
        if not now:
            now = list(_HELP_NOW["default"])
        elif "impersonation" not in pats:
            now.append(_HELP_NOW["default"][2])
    sent = []
    if "sextortion" in pats:
        sent.append(_HELP_SENT["images"])
    if _GIFT_RE.search(blob):
        sent.append(_HELP_SENT["gift_cards"])
    if _CRYPTO_RE.search(blob) or "deepfake_endorsement" in pats or "trading_guru" in pats:
        sent.append(_HELP_SENT["crypto"])
    if _IDENT_RE.search(blob) or "impersonation" in pats:
        sent.append(_HELP_SENT["identity"])
    if "sextortion" not in pats:
        sent.insert(0, _HELP_SENT["bank"])
    report = list(_REPORT)
    if "sextortion" not in pats:
        report = [r for r in report if "Take It Down" not in r["name"]]
    if not (_IDENT_RE.search(blob) or "impersonation" in pats):
        report = [r for r in report if "IdentityTheft" not in r["name"]]
    if not any(p in pats for p in ("trading_guru", "guaranteed_returns", "dm_to_invest", "deepfake_endorsement", "celebrity_endorsement")):
        report = [r for r in report if "broker" not in r["name"]]
    talk = list(HELPLINES) if "sextortion" not in pats else [
        {"name": "NCMEC CyberTipline", "phone": "1-800-843-5678", "url": "https://report.cybertip.org/",
         "what": "24/7 — report sextortion of anyone under 18; they will help"},
        {"name": "Cyber Civil Rights Initiative (18+)", "phone": None, "url": "https://cybercivilrights.org/",
         "what": "help and a crisis line for image-based abuse"},
    ] + HELPLINES[:1]
    return {"now": now[:4], "already_sent": sent[:4], "talk": talk, "report": report[:4]}


# ---- what the card tells a reader to do ----
RESOURCES_SCAM = [
    {"name": "Report to the FTC", "url": "https://reportfraud.ftc.gov/",
     "what": "the U.S. consumer fraud report; the FTC shares it with law enforcement"},
    {"name": "FBI IC3", "url": "https://www.ic3.gov/",
     "what": "for internet crime, especially if money was already sent"},
    {"name": "Check an investment adviser or broker", "url": "https://brokercheck.finra.org/",
     "what": "anyone selling investments in the U.S. must be registered; look them up before sending anything"},
    {"name": "Report on the platform", "url": None,
     "what": "every platform has a report option for scams; it helps the next person"},
]
ADVICE = {
    "high": "Never send money, crypto or gift cards to receive money. Real giveaways don't need a deposit, and no agency or bank asks for payment in gift cards or crypto.",
    "sextortion": "Do not pay, do not delete, tell someone you trust.",
    "medium": "Don't move money on the strength of this video. Look the company or person up on a regulator's site first, and be suspicious of anyone who moves the conversation to WhatsApp or Telegram.",
    "low": "One warning sign is not a scam, but it's worth a second look before you act on it.",
}
