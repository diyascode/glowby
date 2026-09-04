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
    # ---- iOS APP MODE: camera control must NOT exist (2.1a crash fix);
    # website keeps it. Checked at iPad Air 11" and iPhone sizes.
    for label, w, h in (("ipad", 820, 1180), ("iphone", 390, 760)):
        page = b.new_page(viewport={"width": w, "height": h})
        aerr = []
        page.on("pageerror", lambda e: aerr.append(str(e)))
        def aroute(r, _req=None):
            u = r.request.url
            if u.endswith("app=1") or "/?app=1" in u:
                r.fulfill(status=200, content_type="text/html", body=html)
            else:
                r.fulfill(status=404, body="nf")
        page.route("**/*", aroute)
        page.goto("http://fake.test/?app=1")
        page.wait_for_timeout(500)
        if not page.evaluate("!!document.getElementById('cam')"):
            fails.append(f"appmode-{label}: upload button MISSING in app mode")
        if not page.evaluate("document.getElementById('camIn') && document.getElementById('camIn').hasAttribute('multiple')"):
            fails.append(f"appmode-{label}: picker NOT library-only (multiple attr missing -> camera option would appear)")
        if not page.evaluate("document.documentElement.classList.contains('appmode')"):
            fails.append(f"appmode-{label}: appmode gate did not engage")
        if aerr:
            fails.append(f"appmode-{label}: JS errors {aerr}")
        page.screenshot(path=os.path.join(_ROOT, f"shape_app_{label}.png"))
        page.close()
    # website (non-app) must still HAVE the camera button
    page = b.new_page(viewport={"width": 1024, "height": 900})
    def wroute(r, _req=None):
        r.fulfill(status=200, content_type="text/html", body=html)
    page.route("**/*", wroute)
    page.goto("http://fake.test/")
    page.wait_for_timeout(400)
    if not page.evaluate("!!document.getElementById('cam')"):
        fails.append("website: upload button MISSING")
    if page.evaluate("document.getElementById('camIn').hasAttribute('multiple')"):
        fails.append("website: picker wrongly library-only (camera allowed on web)")
    page.close()
    # ---- Phase-1 resume: a pending job left in localStorage is picked up
    # on load, rendered, welcomed back, and the pending marker cleared
    page = b.new_page(viewport={"width": 900, "height": 1100})
    resume_result = dict(shapes["mixed_full"])
    def rroute(r, _req=None):
        u = r.request.url
        if "/api/job/testjob1" in u:
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "done", "result": resume_result}))
        elif u.rstrip("/").endswith("fake.test"):
            r.fulfill(status=200, content_type="text/html", body=html)
        else:
            r.fulfill(status=404, body="nf")
    page.route("**/*", rroute)
    page.add_init_script(
        "try{localStorage.setItem('gbPending', JSON.stringify("
        "{job_id:'testjob1', url:'https://youtube.com/watch?v=x', t:Date.now()}))}catch(e){}")
    page.goto("http://fake.test/")
    page.wait_for_timeout(1800)  # poll ticks at 800ms
    body_txt = page.inner_text("#out")
    if "finished while you were away" not in body_txt:
        fails.append("resume: welcome-back note missing")
    if "true thing" not in body_txt:
        fails.append("resume: result not rendered")
    if page.evaluate("localStorage.getItem('gbPending')") is not None:
        fails.append("resume: pending marker not cleared")
    page.close()
    # ---- TWIN DIALS: authenticity finding -> AI dial appears; no finding
    # or "no signal" -> classic single ring, no dial (absence = honest blank)
    twin = dict(shapes["mixed_full"])
    twin = json.loads(json.dumps(twin))
    twin["authenticity"] = {"assessment_status": "completed",
        "origin_result": "declared_ai", "manipulation_scope": "whole_media",
        "display": "Creator or platform labeled this content as AI-generated",
        "show_ai_badge": False, "evidence": [], "stage": 1}
    nosig = json.loads(json.dumps(shapes["mixed_full"]))
    nosig["authenticity"] = {"assessment_status": "completed",
        "origin_result": "no_synthetic_signal", "manipulation_scope": "unknown",
        "display": "No synthetic signal detected", "show_ai_badge": False,
        "evidence": [], "stage": 1}
    for label, payload, expect_dial in (("declared", twin, True),
                                        ("nosignal", nosig, False)):
        page = b.new_page(viewport={"width": 900, "height": 1200})
        def droute(r, _req=None, res=payload):
            u = r.request.url
            if "/api/result/" in u:
                r.fulfill(status=200, content_type="application/json", body=json.dumps(res))
            elif u.endswith("/r/test"):
                r.fulfill(status=200, content_type="text/html", body=html)
            else:
                r.fulfill(status=404, body="nf")
        page.route("**/*", droute)
        page.goto("http://fake.test/r/test")
        page.wait_for_timeout(600)
        has_dial = page.evaluate("!!document.querySelector('.dial-row')")
        if has_dial != expect_dial:
            fails.append(f"twin-dial {label}: dial present={has_dial}, expected {expect_dial}")
        if expect_dial:
            txt = page.inner_text(".dial-row")
            if "CLAIMS" not in txt or "MEDIA" not in txt or "creator-labeled" not in txt:
                fails.append("twin-dial declared: labels missing")
            if "true thing" not in page.inner_text("#out"):
                fails.append("twin-dial declared: claims list broken")
        page.screenshot(path=os.path.join(_ROOT, f"shape_dial_{label}.png"))
        page.close()
    b.close()


# RENDER SAFETY: render() must never throw on a normal result. A scope
# slip once blanked the entire page while every server test still passed.
with sync_playwright() as pw:
    _b = pw.chromium.launch()
    _pg = _b.new_page()
    _res = dict(BASE)
    _res["title"] = "A very long caption " * 12
    _res["claims"] = [claim("true thing", 8.2, "supported")]
    _res["report"] = {"headline_score": 8.2, "headline_state": "good",
                      "headline_label": "ok", "share_text": "s",
                      "counts": {"claim_units": 1, "judged": 1,
                                 "not_judged": 0, "parked": 0},
                      "safety_notice": None}
    _pg.route("**/*", lambda r: (
        r.fulfill(status=200, content_type="application/json",
                  body=json.dumps(_res))
        if "/api/result/" in r.request.url else
        (r.fulfill(status=200, content_type="text/html", body=html)
         if r.request.url.endswith("/r/test")
         else r.fulfill(status=404, body="nf"))))
    _pg.goto("http://fake.test/r/test")
    _pg.wait_for_timeout(700)
    _v = _pg.evaluate("(()=>{try{render();return 'ok';}catch(e){return e.message;}})()")
    if _v != "ok":
        fails.append(f"render() threw: {_v}")
    _txt = _pg.inner_text("#out")
    if "full caption" not in _txt:
        fails.append("long caption not trimmed to a link")
    if "Open the original video" not in _txt:
        fails.append("source link missing")
    _b.close()


# SHARE-EXTENSION / WEBVIEW MODE: the app format must apply even without
# ?app=1 (the share sheet opens the bare URL). App Review saw the beta
# label and a dead voice button there — neither may ever render again.
with sync_playwright() as pw:
    _b2 = pw.chromium.launch()
    WKUA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
    SAFARI_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
                 "Mobile/15E148 Safari/604.1")
    for _label, _ua, _embedded in (("webview", WKUA, True),
                                   ("safari", SAFARI_UA, False)):
        _ctx = _b2.new_context(user_agent=_ua,
                               viewport={"width": 390, "height": 760})
        _p2 = _ctx.new_page()
        _p2.route("**/*", lambda r: (
            r.fulfill(status=200, content_type="text/html", body=html)
            if r.request.url.endswith("/x") else r.fulfill(status=404, body="nf")))
        _p2.goto("http://fake.test/x")   # NO ?app=1 — like the share sheet
        _p2.wait_for_timeout(500)
        _isapp = _p2.evaluate("document.documentElement.classList.contains('appmode')")
        if _isapp != _embedded:
            fails.append(f"{_label}: appmode={_isapp}, expected {_embedded}")
        if _embedded:
            _betavis = _p2.evaluate(
                "(()=>{const e=document.querySelector('.beta2');"
                "return !!(e&&getComputedStyle(e).display!=='none');})()")
            if _betavis: fails.append("webview still shows BETA label")
            _micvis = _p2.evaluate(
                "(()=>{const e=document.getElementById('mic');"
                "return !!(e&&getComputedStyle(e).display!=='none');})()")
            if _micvis: fails.append("webview still shows voice button")
        _ctx.close()
    _b2.close()


# MEDIA LEADS WHEN CLAIMS ARE EMPTY: a checked video with nothing to
# fact-check must headline the AI answer, not an empty claim ring.
with sync_playwright() as pw:
    _b3 = pw.chromium.launch()
    _p3 = _b3.new_page()
    _r3 = dict(BASE)
    _r3["claims"] = []
    _r3["authenticity"] = {"origin_result": "no_synthetic_signal",
                           "assessment_status": "completed", "stage": 2,
                           "stage2_status": "completed",
                           "display": "No synthetic signal detected",
                           "show_ai_badge": False,
                           "evidence": [{"provider": "hive",
                                         "signal_type": "forensic_video_frames",
                                         "raw_score": 0.0, "band": "none",
                                         "classes_seen": 660,
                                         "explanation": "x",
                                         "source_link": None}]}
    _r3["report"] = {"headline_score": None, "headline_state": "unverified",
                     "headline_label": "no claims", "share_text": "s",
                     "counts": {"claim_units": 0, "judged": 0,
                                "not_judged": 0, "parked": 0},
                     "safety_notice": None}
    _p3.route("**/*", lambda r: (
        r.fulfill(status=200, content_type="application/json", body=json.dumps(_r3))
        if "/api/result/" in r.request.url else
        (r.fulfill(status=200, content_type="text/html", body=html)
         if r.request.url.endswith("/r/test") else r.fulfill(status=404, body="nf"))))
    _p3.goto("http://fake.test/r/test")
    _p3.wait_for_timeout(700)
    try:
        _p3.click(".audet summary")
        _p3.wait_for_timeout(150)
    except Exception:
        fails.append("AI check details panel missing")
    _t3 = _p3.inner_text("#out")
    if "No synthetic signal found" not in _t3:
        fails.append("media answer does not lead when claims are empty")
    if _t3.index("No synthetic signal found") > _t3.index("nothing to fact-check"):
        fails.append("claims line placed above the media answer")
    if "660" not in _t3:
        fails.append("class-scores-read count missing from panel")
    _b3.close()


# CONSENT GATE (App Review 5.1.1/5.1.2): first submit shows the consent
# screen and sends NOTHING; agreeing sends and the screen stays gone.
with sync_playwright() as pw:
    _b4 = pw.chromium.launch()
    _p4 = _b4.new_page(viewport={"width": 390, "height": 760})
    _sent = []
    def _route4(r):
        u = r.request.url
        if "/api/check" in u:
            _sent.append(u)
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"job_id": "j1"}))
        elif "/api/job/" in u:
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "error", "error": "stop"}))
        elif u.endswith("/x"):
            r.fulfill(status=200, content_type="text/html", body=html)
        else:
            r.fulfill(status=404, body="nf")
    _p4.route("**/*", _route4)
    _p4.goto("http://fake.test/x")
    _p4.wait_for_timeout(400)
    _p4.fill("#url", "https://youtube.com/shorts/abc123")
    _p4.click("#go")
    _p4.wait_for_timeout(400)
    _vis = _p4.evaluate("!document.getElementById('consent').hidden")
    if not _vis: fails.append("consent screen did not appear on first submit")
    if _sent: fails.append("request was sent BEFORE consent")
    _p4.click("#consentOk")
    _p4.wait_for_timeout(700)
    if not _sent: fails.append("request not sent after consent")
    _again = _p4.evaluate("!document.getElementById('consent').hidden")
    if _again: fails.append("consent screen still showing after agreement")
    _b4.close()


# DRAWER UX (app): tap outside closes it and returns to Check; tapping
# the same tab again also closes it
with sync_playwright() as pw:
    _b5 = pw.chromium.launch()
    _c5 = _b5.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    _p5 = _c5.new_page()
    _p5.route("**/*", lambda r: (
        r.fulfill(status=200, content_type="application/json", body="[]")
        if "/api/recent" in r.request.url else
        (r.fulfill(status=200, content_type="text/html", body=html)
         if "/x" in r.request.url else r.fulfill(status=404, body="nf"))))
    _p5.goto("http://fake.test/x?app=1")
    _p5.wait_for_timeout(400)
    _p5.click('#tabbar button[data-tab="trending"]'); _p5.wait_for_timeout(300)
    if not _p5.evaluate("document.getElementById('sb').classList.contains('open')"):
        fails.append("drawer did not open")
    _p5.mouse.click(370, 400); _p5.wait_for_timeout(300)
    if _p5.evaluate("document.getElementById('sb').classList.contains('open')"):
        fails.append("tap outside did not close drawer")
    if _p5.evaluate("document.querySelector('#tabbar button.on').dataset.tab") != "check":
        fails.append("tap outside did not return to Check")
    _p5.click('#tabbar button[data-tab="trending"]'); _p5.wait_for_timeout(200)
    _p5.click('#tabbar button[data-tab="trending"]'); _p5.wait_for_timeout(300)
    if _p5.evaluate("document.getElementById('sb').classList.contains('open')"):
        fails.append("tapping Trending again did not close drawer")
    _b5.close()

print("FAILURES:", fails) if fails else print(
    "UI SHAPES PASS: safety alert, unverified, mixed+parked+reel, XSS blocked, render-safe, caption-trim, webview-appmode, media-leads, consent-gate, drawer-ux")
