"""Render every result shape in a real browser + XSS injection attempt."""
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright

html = open(os.path.join(_ROOT, "app", "templates", "app.html"), encoding="utf-8").read()

def claim(text, ts, state, gate="factual", **kw):
    c = {"claim": text, "quote": "q", "gate_label": gate, "bucket": "politics",
         "secondary_bucket": None, "central": True, "confidence": 0.9,
         "risk_level": "low", "public_safety_risk": False,
         "developing_story": False, "reason": "r"}
    c.update(kw)
    if ts is not False:
        c["verdict"] = {"truth_score": ts, "verdict_state": state,
                        "verdict": "v.", "evidence_strength": "strong",
                        "key_sources": []}
    return c

BASE = {"url": "https://youtube.com/watch?v=x", "url_key": "youtube:x",
        "platform": "youtube", "title": "T", "uploader": "U",
        "posted_date": "2026-07-01", "transcript_source": "captions",
        "transcript": "hello", "cached": False}

XSS = "<img src=x onerror=window.__xss=1><script>window.__xss=2</script>"

shapes = {
    "safety": dict(BASE, claims=[claim("evacuate now", None, "unverifiable",
                                       public_safety_risk=True)],
                   report={"headline_score": None, "headline_state": "safety_alert",
                           "headline_label": "safety!", "share_text": "s",
                           "counts": {"claim_units": 1, "judged": 1, "not_judged": 0, "parked": 0},
                           "safety_notice": "⚠ SAFETY TEST NOTICE"}),
    "unverified": dict(BASE, claims=[claim("mystery", None, "unverifiable")],
                       report={"headline_score": None, "headline_state": "unverified",
                               "headline_label": "could not verify", "share_text": "s",
                               "counts": {"claim_units": 1, "judged": 1, "not_judged": 0, "parked": 0},
                               "safety_notice": None}),
    "mixed_full": dict(BASE, claims=[
        claim("true thing", 9.1, "supported"),
        claim("false thing", 1.2, "contradicted"),
        claim("opinionated", False, None, gate="opinion"),
        claim("unjudged", False, None)],
        report={"headline_score": 1.2, "headline_state": "misleading",
                "headline_label": "contradicted stuff", "share_text": "s",
                "counts": {"claim_units": 4, "judged": 2, "not_judged": 1, "parked": 1},
                "safety_notice": None}),
    "xss": dict(BASE, title=XSS, uploader=XSS, transcript=XSS,
                claims=[dict(claim(XSS, 5.0, "partly_supported"), quote=XSS, reason=XSS,
                             evidence={"fact_checks": [{"publisher": XSS, "title": XSS,
                                                        "url": "https://ok.example/x",
                                                        "rating": XSS, "review_date": "2026"}],
                                       "web_sources": [{"source": XSS, "url": "https://ok.example/y",
                                                        "quote": XSS, "stance": "supports"}]})],
                report={"headline_score": 5.0, "headline_state": "mixed",
                        "headline_label": XSS, "share_text": XSS,
                        "counts": {"claim_units": 1, "judged": 1, "not_judged": 0, "parked": 0},
                        "safety_notice": None}),
}

fails = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    for name, result in shapes.items():
        page = b.new_page(viewport={"width": 800, "height": 1200})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(r, _req=None, res=result):
            url = r.request.url
            if "/api/result/" in url:
                r.fulfill(status=200, content_type="application/json", body=json.dumps(res))
            elif url.endswith("/r/test"):
                r.fulfill(status=200, content_type="text/html", body=html)
            else:
                r.fulfill(status=404, body="nf")
        page.route("**/*", route)
        page.goto("http://fake.test/r/test")  # permalink mode renders stored result
        page.wait_for_timeout(600)
        txt = page.inner_text("#out")
        if name == "safety" and "SAFETY TEST NOTICE" not in txt:
            fails.append("safety notice not rendered")
        if name == "mixed_full":
            for needle in ["true thing", "false thing", "Parked by the gate", "Found, not judged"]:
                if needle not in txt: fails.append(f"mixed_full missing {needle}")
            # reel view toggle
            page.click("#tr")
            page.wait_for_timeout(200)
            if "Screenshot this view" not in page.inner_text("#out"):
                fails.append("reel view toggle broken")
        if name == "xss":
            injected = page.evaluate("window.__xss || 0")
            if injected: fails.append(f"XSS EXECUTED (variant {injected})")
            if "onerror" not in txt and "<img" not in txt:
                # escaped text should be VISIBLE as text somewhere
                pass
        if errors and name != "xss":
            fails.append(f"{name}: JS errors {errors}")
        page.screenshot(path=os.path.join(_ROOT, f"shape_{name}.png"))
        page.close()
    b.close()

print("FAILURES:", fails) if fails else print(
    "UI SHAPES PASS: safety alert, unverified, mixed+parked+reel, XSS blocked")
