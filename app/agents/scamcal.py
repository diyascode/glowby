"""
Scam-engine calibration — run a labelled corpus, measure, learn.

The AI-detector has a calibration tool (ai/real URLs). This is the same
idea for the scam engine: a labelled set of texts — "scam" and "ok" —
runs through the SAME rules readers get (regex + rules + adjudication;
no model, no lookups, so it is free and instant) and reports:

  * detection rate on known scams at the card thresholds (40 = card,
    65 = red), with the misses listed and grouped by what they were
  * false-alarm rate on known-legitimate texts, with each false alarm
    and the codes that fired
  * per-shape counts: which pattern families carry the load

A seed corpus ships with Glowby (app/data/scam_corpus.json — written
from the FTC's, FBI's and WSJ's descriptions of the common shapes, plus
ordinary legitimate messages). Anyone can paste more: one per line,
"scam: <text>" / "ok: <text>", or the UCI SMS Spam Collection's
"spam<TAB>text" / "ham<TAB>text" format (spam is treated as scam).

LEARN: the misses can be sent to the review model (Fable), which
proposes pattern families — a name, a regex, the dimension and points,
a floor if any — in the engine's own shape, ready to paste. It only
proposes; the humans decide, exactly like the weekly flag review.
"""

import json
import os
import re
import time

from app.agents import scamengine as E

CORPUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scam_corpus.json")
MAX_ITEMS = 2000
_LINE = re.compile(r"^\s*\"?(scam|ok|spam|ham|legit|real|fraud|smishing|phishing|smish)\"?\s*[:\t,\-]\s*\"?(.+?)\"?\s*$", re.I)
_SCAM = {"scam", "fraud", "smishing", "phishing", "smish"}
_SPAM = {"spam"}  # marketing spam is not a scam: reported on its own, never counted as a miss


def parse_items(text: str) -> list:
    """Pure (unit-tested): 'scam: ...' / 'ok: ...' or 'spam<TAB>...' /
    'ham<TAB>...' lines -> [{label, text}]."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        k = m.group(1).lower()
        lab = "scam" if k in _SCAM else ("spam" if k in _SPAM else "ok")
        body = m.group(2).strip()
        if len(body) < 8:
            continue
        out.append({"label": lab, "text": body[:2000]})
        if len(out) >= MAX_ITEMS:
            break
    return out


def parse_any(text: str) -> list:
    """'label: text' lines, JSONL cases, or a pasted UCI/Mendeley dataset."""
    items = parse_items(text)
    if items:
        return items
    from app.agents.scamexam import parse_dataset_csv, parse_cases
    cs = parse_cases(text) or parse_dataset_csv(text)
    return [{"label": c["label"], "text": c["text"], "shape": c.get("shape") or ""} for c in cs if c["label"] in ("scam", "ok", "spam")]


def load_verified() -> list:
    """Glowby's own human-verified, redacted samples (storage.scam_samples)
    as corpus items — the third data layer."""
    try:
        from app.storage import list_scam_samples
        out = []
        for r in list_scam_samples(status="verified", limit=2000):
            if r.get("label") in ("scam", "ok") and r.get("redacted_text"):
                out.append({"label": r["label"], "text": r["redacted_text"][:2000],
                            "shape": (r.get("scam_types") or ["verified"])[0], "source": "glowby"})
        return out
    except Exception:
        return []


def load_seed() -> list:
    try:
        with open(CORPUS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        items = []
        for it in d.get("items") or []:
            if it.get("label") in ("scam", "ok") and it.get("text"):
                items.append({"label": it["label"], "text": str(it["text"])[:2000],
                              "shape": it.get("shape") or "", "source": it.get("source") or "seed"})
        return items
    except Exception:
        return []


def run(items: list, thresholds=(40, 65), use_model: bool = False, client=None) -> dict:
    """Score every item with the engine (rules only by default). Pure
    given the engine. Returns the calibration document."""
    t0 = time.time()
    rows = []
    for it in items[:MAX_ITEMS]:
        try:
            r = E.analyze(it["text"], verify_enabled=False, use_model=use_model, client=client)
        except Exception as e:
            r = {"scam_risk_score": None, "verdict": "error", "risk_factors": [], "scam_types": [], "error": str(e)[:100]}
        sc = r.get("scam_risk_score")
        rows.append({"label": it["label"], "shape": it.get("shape") or "", "text": it["text"][:240],
                     "score": sc, "verdict": r.get("verdict"), "codes": [f.get("code") for f in (r.get("risk_factors") or [])][:6],
                     "types": (r.get("scam_types") or [])[:3]})
    scams = [x for x in rows if x["label"] == "scam"]
    oks = [x for x in rows if x["label"] == "ok"]
    spams = [x for x in rows if x["label"] == "spam"]
    at = {}
    for th in thresholds:
        caught = [x for x in scams if (x["score"] or 0) >= th]
        fa = [x for x in oks if (x["score"] or 0) >= th]
        at[str(th)] = {"detection_rate": round(len(caught) / len(scams), 3) if scams else None,
                       "false_alarm_rate": round(len(fa) / len(oks), 3) if oks else None,
                       "caught": len(caught), "false_alarms": len(fa)}
    misses = sorted([x for x in scams if (x["score"] or 0) < thresholds[0]], key=lambda x: (x["score"] or 0))
    weak = [x for x in scams if thresholds[0] <= (x["score"] or 0) < thresholds[1]]
    false_alarms = sorted([x for x in oks if (x["score"] or 0) >= thresholds[0]], key=lambda x: -(x["score"] or 0))
    by_shape = {}
    for x in scams:
        k = x["shape"] or "unlabelled"
        d = by_shape.setdefault(k, {"n": 0, "caught": 0})
        d["n"] += 1
        if (x["score"] or 0) >= thresholds[0]:
            d["caught"] += 1
    code_counts = {}
    for x in rows:
        for c in x["codes"]:
            code_counts[c] = code_counts.get(c, 0) + 1
    null_scams = sum(1 for x in scams if x["score"] is None)
    return {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "n": len(rows), "scams": len(scams), "oks": len(oks), "spams": len(spams),
            "spam_flagged": sum(1 for x in spams if (x["score"] or 0) >= thresholds[0]),
            "thresholds": list(thresholds), "at": at,
            "misses": misses[:60], "weak": weak[:40], "false_alarms": false_alarms[:60],
            "by_shape": by_shape, "code_counts": dict(sorted(code_counts.items(), key=lambda kv: -kv[1])[:30]),
            "null_scams": null_scams, "seconds": round(time.time() - t0, 1), "model": bool(use_model)}


LEARN_PROMPT = """You maintain the pattern rules of Glowby's scam-risk engine. The engine \
scores messages with deterministic regex "shapes" (name, regex, dimension, points, \
optional floor). Below are labelled SCAM messages the engine scored too low \
(misses) and legitimate messages it scored too high (false alarms), with the \
codes that fired. Propose changes — new shapes or narrowing of existing ones — \
in the engine's own form. Rules for you: never propose loosening a floor; every \
new regex must be Python `re` syntax, case-insensitive, word-bounded, and must \
NOT match the false alarms shown; prefer the ask (what the reader must do) over \
tone words; at most 6 proposals. Answer with ONLY a JSON array (no prose):
[{{"name": "SHORT_NAME", "kind": "new_shape" | "narrow_existing", "regex": "...", "dimension": "identity|action|pressure|technical|plausibility", "points": 5-30, "floor": null or 65-95, "explains_misses": [indexes], "why": "one sentence"}}]

Existing dimensions and caps: identity 20, action 30, pressure 15, technical 15, plausibility 10, external 10. \
Existing floors: credential request 90, remote access + banking 95, official body demanding untraceable payment 95, \
advance fee 85, extortion 90, safe account 95, cash courier 90, SSN linked to crime 85, family emergency 75, \
romance money 70, phishing link 65, callback number 65, no-questions transfer 65.

=== MISSES (scam, scored low) ===
{misses}

=== FALSE ALARMS (legitimate, scored high) ===
{false_alarms}"""


def parse_proposals(raw: str) -> list:
    """Pure (unit-tested): model JSON -> validated proposals; every regex
    must compile, or the proposal is dropped."""
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b == -1 or b < a:
        return []
    try:
        arr = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for d in arr if isinstance(arr, list) else []:
        if not isinstance(d, dict):
            continue
        rx = str(d.get("regex") or "")
        try:
            re.compile(rx, re.I)
        except re.error:
            continue
        dim = str(d.get("dimension") or "").lower()
        if dim not in E.CAPS or dim == "external":
            continue
        try:
            pts = int(d.get("points") or 0)
        except (TypeError, ValueError):
            pts = 0
        fl = d.get("floor")
        try:
            fl = int(fl) if fl not in (None, "", "null") else None
        except (TypeError, ValueError):
            fl = None
        if fl is not None and not (65 <= fl <= 95):
            fl = None
        out.append({"name": str(d.get("name") or "")[:40].upper().replace(" ", "_"),
                    "kind": "narrow_existing" if str(d.get("kind")) == "narrow_existing" else "new_shape",
                    "regex": rx[:600], "dimension": dim, "points": max(0, min(pts, E.CAPS[dim])), "floor": fl,
                    "explains_misses": [int(i) for i in (d.get("explains_misses") or []) if str(i).isdigit()][:20],
                    "why": str(d.get("why") or "")[:300]})
    return out[:6]


def check_proposals(props: list, doc: dict) -> list:
    """Pure: does each proposed regex actually hit the misses and miss the
    false alarms? Numbers attached so the human sees the effect."""
    out = []
    for p in props:
        try:
            rx = re.compile(p["regex"], re.I)
        except re.error:
            continue
        hit_m = sum(1 for x in doc.get("misses") or [] if rx.search(x["text"]))
        hit_fa = sum(1 for x in doc.get("false_alarms") or [] if rx.search(x["text"]))
        q = dict(p)
        q["hits_misses"] = hit_m
        q["hits_false_alarms"] = hit_fa
        q["safe"] = hit_fa == 0 and hit_m > 0
        out.append(q)
    return out


def learn(doc: dict, client=None, model=None) -> dict:
    """Send misses + false alarms to the review model; return validated,
    effect-checked proposals. Never raises."""
    from app.agents.review import REVIEW_MODEL, FALLBACK_MODEL, _client
    client = client or _client()
    if client is None:
        return {"error": "no ANTHROPIC_API_KEY", "proposals": []}
    misses = doc.get("misses") or []
    fas = doc.get("false_alarms") or []
    if not misses and not fas:
        return {"proposals": [], "note": "nothing to learn from — no misses or false alarms"}
    mtxt = "\n".join(f"[{i}] (score {x['score']}, codes {x['codes']}) {x['text']}" for i, x in enumerate(misses[:30])) or "(none)"
    ftxt = "\n".join(f"[{i}] (score {x['score']}, codes {x['codes']}) {x['text']}" for i, x in enumerate(fas[:20])) or "(none)"
    prompt = LEARN_PROMPT.format(misses=mtxt, false_alarms=ftxt)
    used = model or REVIEW_MODEL
    for attempt, m in enumerate((used, FALLBACK_MODEL)):
        try:
            msg = client.messages.create(model=m, max_tokens=2000, temperature=0,
                                         messages=[{"role": "user", "content": prompt}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            props = check_proposals(parse_proposals(raw), doc)
            return {"model": m, "proposals": props, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        except Exception as e:
            if attempt == 0:
                continue
            return {"error": str(e)[:200], "proposals": []}
    return {"error": "learn failed", "proposals": []}
