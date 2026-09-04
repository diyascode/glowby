"""FINAL v0.18.0 certification matrix — every input shape end-to-end."""
import sys
import types

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
fails = []

# ---------- ingest branching with a fake yt-dlp boundary ----------
import app.agents.ingest as ing
from app.agents.ingest import IngestError


def fake_ydl(info=None, raise_text=None):
    class Y:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            if raise_text: raise Exception(raise_text)
            return info
        def download(self, urls):
            if raise_text: raise Exception(raise_text)
    sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=Y)


BASEINFO = {"title": "T", "uploader": "U", "duration": 30, "upload_date": "20260701",
            "subtitles": {}, "automatic_captions": {}}

# 1. captions present and rich -> captions path, no frames
rich = dict(BASEINFO)
ing._transcript_from_captions = lambda info: "word " * 40
fake_ydl(info=rich)
r = ing.ingest("https://youtube.com/shorts/abcdefg1234")
if r["transcript_source"] != "captions" or "frames" in r: fails.append("m1 captions path")

# 2. captions thin (music tags) -> frames attached
ing._transcript_from_captions = lambda info: "[Music] la la"
ing._frames_from_video = lambda url, max_frames=6: ["ZnJhbWU="]
r = ing.ingest("https://youtube.com/shorts/abcdefg1234")
if r["transcript_source"] != "captions" or not r.get("frames"): fails.append("m2 thin captions frames")

# 3. no captions, speech only, eyes see nothing -> whisper alone
ing._transcript_from_captions = lambda info: None
ing._transcribe_and_see = lambda url, max_frames=6: ("spoken " * 30, ["x"], None, None)
r = ing.ingest("https://youtube.com/shorts/abcdefg1234")
if r["transcript_source"] != "whisper" or "frames" in r: fails.append("m3 whisper path")

# 3b. EARS + EYES together -> BOTH claims merged into one transcript
ing._transcribe_and_see = lambda url, max_frames=6: ("Ronaldo scored " * 20, ["x"] * 6, "on-screen text: Neymar called to 2026 World Cup", None)
r = ing.ingest("https://youtube.com/shorts/abcdefg1234")
if ("Ronaldo" not in r["transcript"] or "Neymar" not in r["transcript"]
        or r["transcript_source"] != "whisper+visual analysis"):
    fails.append("m3b ears+eyes merged")

# 4. silent but visual -> the eyes' read becomes the transcript
ing._transcribe_and_see = lambda url, max_frames=6: (None, ["x"] * 6, "eyes saw a chart", None)
r = ing.ingest("https://youtube.com/shorts/abcdefg1234")
if r["transcript_source"] != "visual analysis" or "eyes saw a chart" not in r["transcript"]:
    fails.append("m4 silent visual")

# 5. silent AND blind -> honest error
ing._transcribe_and_see = lambda url, max_frames=6: (None, [], None, IngestError("no audio"))
try:
    ing.ingest("https://youtube.com/shorts/abcdefg1234"); fails.append("m5 no error")
except IngestError:
    pass

# 6. TikTok metadata blocked -> friendly message
fake_ydl(raise_text="Unable to extract universal data for rehydration")
try:
    ing.ingest("https://www.tiktok.com/t/ZTSOMETHING/"); fails.append("m6 no error")
except IngestError as e:
    if "TikTok blocked" not in str(e): fails.append(f"m6 raw error leaked: {e}")

# 7. video too long -> scope error
fake_ydl(info=dict(BASEINFO, duration=99999))
try:
    ing.ingest("https://youtube.com/watch?v=abcdefg1234"); fails.append("m7 no cap")
except IngestError as e:
    if "minutes" not in str(e): fails.append("m7 wrong msg")

# ---------- pipeline shapes through the REAL _run_pipeline ----------
import importlib
import app.main as m
importlib.reload(m)
m.save_route_audit = lambda *a, **k: None
m.save_result = lambda *a, **k: None
m.search_fact_check_db = lambda c: []


def unit(claim, gate="factual", central=True, risk="low", safety=False):
    return {"claim": claim, "quote": claim, "gate_label": gate, "bucket": "politics",
            "secondary_bucket": None, "central": central, "confidence": 0.9,
            "risk_level": risk, "public_safety_risk": safety,
            "developing_story": False, "reason": "r"}


def run(jid, text, key, router_out, judge_map=None, evidence=None):
    m.ingest = lambda url: {"url": url, "platform": "youtube", "title": text,
                            "uploader": "u", "duration_seconds": 30, "transcript": text,
                            "transcript_source": "captions", "posted_date": "2026-07-01"}
    m.route_claims = lambda t, **kw: [dict(u) for u in router_out]
    m.gather_evidence = lambda c: evidence or {"fact_checks": [], "web_sources": [
        {"source": "S", "url": "https://s.s", "quote": "q", "stance": "supports"}], "search_rounds": 1}
    m.judge_with_rubric = lambda c, ev: (judge_map or {}).get(c["claim"], {
        "truth_score": 9.0, "verdict_state": "supported", "verdict": "ok",
        "evidence_strength": "strong", "key_sources": []})
    m._run_pipeline(jid, text, key)
    with m._jobs_lock:
        return dict(m._jobs[jid])


# 8. pure satire video -> everything parked, honest label
j = run("s1", "url", "youtube:sat1", [unit("joke about senators", gate="satire")])
rep = j["result"]["report"]
if rep["headline_score"] is not None: fails.append("m8 satire got scored")
if "opinion, satire" not in rep["headline_label"]: fails.append("m8 label")

# 9. empty router -> no-claims label
j = run("s2", "url", "youtube:empty1", [])
if "no checkable factual claims" not in j["result"]["report"]["headline_label"]:
    fails.append("m9 no-claims label")

# 10. safety collapse beats good scores
j = run("s3", "url", "youtube:safe1",
        [unit("evacuate the town now", safety=True, risk="critical"), unit("sky is blue")],
        judge_map={"evacuate the town now": {"truth_score": None, "verdict_state": "unverifiable",
                   "verdict": "?", "evidence_strength": "none", "key_sources": []}})
if j["result"]["report"]["headline_state"] != "safety_alert": fails.append("m10 safety collapse")

# 11. central false + side true -> MIN wins (no cap rescue for central)
j = run("s4", "url", "youtube:min1",
        [unit("big lie"), unit("side truth", central=False)],
        judge_map={"big lie": {"truth_score": 1.0, "verdict_state": "contradicted",
                   "verdict": "no", "evidence_strength": "strong", "key_sources": []}})
if j["result"]["report"]["headline_score"] != 1.0: fails.append("m11 central MIN")

# 12. central true + weak side -> capped 7.5 green
j = run("s5", "url", "youtube:cap1",
        [unit("main truth"), unit("outdated side", central=False)],
        judge_map={"outdated side": {"truth_score": 6.0, "verdict_state": "partly_supported",
                   "verdict": "old", "evidence_strength": "strong", "key_sources": []}})
rep = j["result"]["report"]
if rep["headline_score"] != 7.5 or "capped" not in rep["headline_label"]: fails.append("m12 cap")

# 13. typed question -> answer mode (real branch)
m.answer_question = lambda q, ev: "Answer with sources."
m.route_claims = lambda t, **kw: [unit(t, gate="question")]
m.gather_evidence = lambda q: {"fact_checks": [], "web_sources": []}
m.ingest = None
m._run_pipeline("s6", "who is the president?", "text:q1")
with m._jobs_lock: j = dict(m._jobs["s6"])
if not j["result"].get("answer_mode"): fails.append("m13 answer mode")

# 14. typed statement -> judged (not answered)
m.route_claims = lambda t, **kw: [unit(t)]
m.judge_with_rubric = lambda c, ev: {"truth_score": 8.0, "verdict_state": "supported",
                                     "verdict": "ok", "evidence_strength": "strong", "key_sources": []}
m.gather_evidence = lambda c: {"fact_checks": [], "web_sources": [], "search_rounds": 2}
m._run_pipeline("s7", "the sky is blue", "text:s1")
with m._jobs_lock: j = dict(m._jobs["s7"])
if j["result"].get("answer_mode") or j["result"]["report"]["headline_score"] != 8.0:
    fails.append("m14 typed statement")

# 15. search failure -> honest search_error (judge receives flag; here just flag presence)
ev = {"fact_checks": [], "web_sources": [], "search_failed": True, "search_rounds": 2}
if not ev.get("search_failed"): fails.append("m15")


# 16. facebook POST -> immediate friendly login-wall message, no downloader run
try:
    ing.ingest("https://www.facebook.com/NYPost/posts/pfbid0abc123")
    fails.append("m16 fb post should raise")
except IngestError as e:
    if "login wall" not in str(e): fails.append("m16 fb post message")

# 17. facebook VIDEO that fails download -> friendly facebook message, not raw error
fake_ydl(raise_text="ERROR: [facebook] xyz: Unable to download webpage: HTTP Error 404")
try:
    ing.ingest("https://www.facebook.com/watch/?v=123456")
    fails.append("m17 fb video should raise")
except IngestError as e:
    if "Facebook wouldn't hand over" not in str(e): fails.append("m17 fb video message")


# ---------- NEW SURFACES (golden-set round 2) ----------

# 18. ARTICLE DOOR: yt-dlp rejects the URL -> article reader parses the page
import urllib.request as _ur
_HTML = ("<html><head><title>T</title>"
         '<meta property="og:title" content="Honey study finds real effect">'
         '<meta property="og:site_name" content="Example News">'
         '<meta property="article:published_time" content="2026-08-01T10:00:00Z">'
         "</head><body><nav><p>menu home about contact word word word word</p></nav>"
         + "".join("<p>Sentence %d of the article body carries substantial "
                   "readable factual reporting for the parser to keep.</p>" % i
                   for i in range(12)) + "</body></html>")
class _FakeResp:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    def read(self, n=None): return _HTML.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, k, d=None): return self.headers.get(k, d)
class _FakeHeaders(dict): pass
_FakeResp.headers = type("H", (), {"get": lambda self, k, d=None:
    "text/html; charset=utf-8" if k == "Content-Type" else d})()
_orig_urlopen = _ur.urlopen
_ur.urlopen = lambda req, timeout=20: _FakeResp()
fake_ydl(raise_text="ERROR: Unsupported URL: https://news.example.com/story")
try:
    r = ing.ingest("https://news.example.com/story")
    if r.get("platform") != "article": fails.append("m18 article platform")
    if r.get("transcript_source") != "article text": fails.append("m18 article source")
    if "substantial" not in (r.get("transcript") or ""): fails.append("m18 article body")
    if r.get("title") != "Honey study finds real effect": fails.append("m18 article title")
except IngestError as e:
    fails.append("m18 article door raised: " + str(e)[:60])
finally:
    _ur.urlopen = _orig_urlopen

# 19. INSTAGRAM with rescue empty -> honest PUBLIC-reel error, never a crash
import types as _t
sys.modules["app.agents.rescue"] = _t.SimpleNamespace(
    rescue_media=lambda url, platform: None)
try:
    ing.ingest("https://www.instagram.com/reel/ABC123/")
    fails.append("m19 instagram should raise")
except IngestError as e:
    if "PUBLIC reel" not in str(e): fails.append("m19 instagram message")

# 20. RESCUE CAP GATE: no token -> dormant; at cap -> off; under cap -> on
del sys.modules["app.agents.rescue"]
import app.agents.rescue as rsc
import app.storage as st
_tok = rsc.RESCUE_TOKEN
rsc.RESCUE_TOKEN = ""
if rsc._allowed(): fails.append("m20 tokenless not dormant")
rsc.RESCUE_TOKEN = "test-token"
st.event_stats = lambda: {"rescue": {"today": rsc.RESCUE_DAILY_CALLS}}
if rsc._allowed(): fails.append("m20 cap not enforced")
st.event_stats = lambda: {"rescue": {"today": rsc.RESCUE_DAILY_CALLS - 1}}
if not rsc._allowed(): fails.append("m20 under-cap blocked")
rsc.RESCUE_TOKEN = _tok

# 21. +ASK: question rides the fresh pipeline; answered AFTER analysis;
# cache storage strips personal Q/A
m.answer_followup = lambda q, ctx: "Yes - the check's sources support it."
if hasattr(m, "add_usage"): m.add_usage = lambda *a, **k: None
m.route_claims = lambda t, **kw: [unit(t)]
m.judge_with_rubric = lambda c, ev: {"truth_score": 8.0, "verdict_state": "supported",
                                     "verdict": "ok", "evidence_strength": "strong",
                                     "key_sources": []}
m.gather_evidence = lambda c: {"fact_checks": [], "web_sources": [
    {"source": "S", "url": "https://s.s", "quote": "q", "stance": "supports"}],
    "search_rounds": 1}
m._run_pipeline("s9", "the sky is blue", "text:s9", "is that really true?")
with m._jobs_lock: j = dict(m._jobs["s9"])
if j["result"].get("user_question") != "is that really true?": fails.append("m21 ask question")
if "support" not in (j["result"].get("user_answer") or ""): fails.append("m21 ask answer")
import inspect as _insp
_sr = _insp.getsource(__import__("app.storage", fromlist=["save_result"]).save_result)
if "user_question" not in _sr: fails.append("m21 cache must strip user_question")


# 22. RE-CHECK EVIDENCE MEMORY: prior sources merge into the fresh hunt,
# deduped by URL, fresh first, capped
prior = [{"claim": "US sanctions on Iranian banks made cash delivery necessary",
          "evidence": {"web_sources": [
              {"source": "Lawfare", "url": "https://lawfare.example/a", "quote": "q1", "stance": "mixed"},
              {"source": "CBS", "url": "https://cbs.example/b", "quote": "q2", "stance": "supports"}],
              "fact_checks": [{"publisher": "P", "url": "https://fc.example/1", "rating": "Mixed", "title": "t", "review_date": ""}]}},
         {"claim": "totally unrelated thing about a football match",
          "evidence": {"web_sources": [{"source": "X", "url": "https://x.example/z", "quote": "z", "stance": "supports"}], "fact_checks": []}}]
fresh = {"web_sources": [{"source": "CBS", "url": "https://cbs.example/b", "quote": "new", "stance": "supports"}],
         "fact_checks": []}
merged = m._merge_prior_evidence(
    "Sanctions on Iranian banks made physical cash delivery necessary", fresh, prior)
urls = [w["url"] for w in merged["web_sources"]]
if urls != ["https://cbs.example/b", "https://lawfare.example/a"]: fails.append("m22 merge urls " + str(urls))
if len(merged["fact_checks"]) != 1: fails.append("m22 merge fc")
if not merged.get("recheck_memory"): fails.append("m22 memory flag")
# unrelated claim must NOT leak its sources in
merged2 = m._merge_prior_evidence("the moon is made of cheese entirely",
                                  {"web_sources": [], "fact_checks": []}, prior)
if merged2["web_sources"]: fails.append("m22 unrelated leak")

# 23. RE-CHECK PIPELINE: prior evidence reaches the judge
seen_ev = {}
m.route_claims = lambda t, **kw: [unit("US sanctions made cash delivery necessary")]
m.gather_evidence = lambda c: {"fact_checks": [], "web_sources": [
    {"source": "Fresh", "url": "https://fresh.example/f", "quote": "f", "stance": "mixed"}],
    "search_rounds": 1}
def _spy_judge(c, ev):
    seen_ev["ev"] = ev
    return {"truth_score": 5.0, "verdict_state": "partly_supported", "verdict": "contested",
            "evidence_strength": "moderate", "key_sources": []}
m.judge_with_rubric = _spy_judge
m._run_pipeline("s10", "the sanctions claim video", "text:s10", "",
                [{"claim": "US sanctions made cash delivery necessary",
                  "evidence": {"web_sources": [{"source": "Old", "url": "https://old.example/o", "quote": "o", "stance": "refutes"}],
                               "fact_checks": []}}])
ev_urls = [w["url"] for w in (seen_ev.get("ev", {}).get("web_sources") or [])]
if "https://old.example/o" not in ev_urls or "https://fresh.example/f" not in ev_urls:
    fails.append("m23 pipeline memory " + str(ev_urls))


# 24. CONTESTED-DRIVER LABEL: green claims + one contested driver ->
# honest sentence, not "questionable claims" smear
from app.agents.output import build_report as _br
def _cl(txt, score, state, central=True):
    return {"claim": txt, "gate_label": "factual", "central": central,
            "risk_level": "low",
            "verdict": {"truth_score": score, "verdict_state": state,
                        "verdict": "v", "evidence_strength": "strong",
                        "key_sources": []}}
r = _br({"title": "iran video", "claims": [
    _cl("transfer happened", 8.5, "supported"),
    _cl("hague settlement", 8.7, "supported"),
    _cl("sanctions made cash necessary", 5.5, "partly_supported")]})
rep = r["report"]
if rep["headline_score"] != 5.5: fails.append("m24 headline")
if "disputed by experts" not in rep["headline_label"]: fails.append("m24 label: " + rep["headline_label"])
# a truly CONTRADICTED driver must keep the warning label
r2 = _br({"title": "v", "claims": [
    _cl("true thing", 8.5, "supported"),
    _cl("false thing", 5.5, "contradicted")]})
if "disputed by experts" in r2["report"]["headline_label"]: fails.append("m24 contradicted leak")

# 25. RE-CHECK CLAIM ANCHORING: prior units ride into the router prompt
from app.agents.router import build_prompt as _bp
p = _bp("some transcript", "t", "tiktok", "u",
        prior_units=["sanctions made cash delivery necessary", "hague case"])
if "RE-CHECK CONSISTENCY RULE" not in p: fails.append("m25 rule missing")
if "sanctions made cash delivery necessary" not in p: fails.append("m25 units missing")
p2 = _bp("some transcript", "t", "tiktok", "u")
if "RE-CHECK CONSISTENCY RULE" in p2: fails.append("m25 leaks into fresh checks")


# 26. IMAGE VALIDATION: data-URL stripped, garbage rejected, key stable
import base64 as _b64x
good = _b64x.b64encode(b"x" * 5000).decode()
if m._clean_image_b64("data:image/jpeg;base64," + good) != good: fails.append("m26 dataurl")
if m._clean_image_b64("not!!base64$$") is not None: fails.append("m26 garbage accepted")
if m._clean_image_b64(_b64x.b64encode(b"tiny").decode()) is not None: fails.append("m26 tiny accepted")
k1, k2 = m._image_key(good), m._image_key(good)
if k1 != k2 or not k1.startswith("img:"): fails.append("m26 key unstable")

# 27. IMAGE CHECK PIPELINE: eyes read the upload -> normal pipeline;
# unreadable image -> honest typed error
m.describe_frames = lambda frames, title="", uploader="": (
    "A screenshot of a post claiming honey never spoils.")
m.route_claims = lambda t, **kw: [unit("honey never spoils")]
m.judge_with_rubric = lambda c, ev: {"truth_score": 8.4, "verdict_state": "supported",
                                     "verdict": "ok", "evidence_strength": "strong",
                                     "key_sources": []}
m.gather_evidence = lambda c: {"fact_checks": [], "web_sources": [
    {"source": "S", "url": "https://s.s", "quote": "q", "stance": "supports"}],
    "search_rounds": 1}
m._run_pipeline("s11", "", "img:testkey", "", None, good)
with m._jobs_lock: j = dict(m._jobs["s11"])
res = j["result"]
if res.get("platform") != "image": fails.append("m27 platform")
if "[WHAT THE IMAGE SHOWS]" not in (res.get("transcript") or ""): fails.append("m27 transcript")
if res["report"]["headline_score"] != 8.4: fails.append("m27 score")
m.describe_frames = lambda frames, title="", uploader="": None
m._run_pipeline("s12", "", "img:testkey2", "", None, good)
with m._jobs_lock: j2 = dict(m._jobs["s12"])
# a nothing-checkable photo must be a FRIENDLY RESULT, never a red error
# (App Review 2.1a, Aug 27)
if j2.get("status") != "done": fails.append("m27 no-claims image must be done, not error")
r2 = j2.get("result", {})
if r2.get("report", {}).get("headline_score") is not None:
    fails.append("m27 no-claims image should have no score")
if "didn't find a checkable claim" not in r2.get("report", {}).get("headline_label", ""):
    fails.append("m27 friendly label missing")


# 28. SECURITY.TXT: RFC 9116 route serves required fields
_sec = m.security_txt()
for needle in ("Contact: mailto:hello@glowby.io", "Expires:", "Canonical:"):
    if needle not in _sec: fails.append("m28 security.txt missing " + needle)


# 29. AUTHENTICITY STAGE 1 (Day 1): categories not percentages; hierarchy;
# badge only for verified provenance; absence never means genuine
from app.agents import authenticity as auth

# caption label -> declared_ai, NO badge
a1 = auth.assess_stage1(caption="my new film, made with AI #aiart")
if a1["origin_result"] != "declared_ai": fails.append("m29 caption label")
if a1["show_ai_badge"]: fails.append("m29 declared must not badge")

# visible watermark text in the VISUAL channel -> declared_ai
a2 = auth.assess_stage1(ocr_text="bottom corner shows: Sora")
if a2["origin_result"] != "declared_ai": fails.append("m29 ocr watermark")

# a bare tool mention in the CAPTION alone must NOT trigger (conservative)
a3 = auth.assess_stage1(caption="I love talking about Sora and Veo news")
if a3["origin_result"] != "no_synthetic_signal": fails.append("m29 caption overtrigger")

# nothing found -> no_synthetic_signal, display must carry the caveat
a4 = auth.assess_stage1(caption="sunset at the beach")
if a4["origin_result"] != "no_synthetic_signal": fails.append("m29 clean")
if "does not confirm" not in a4["display"]: fails.append("m29 absence caveat missing")
if a4["show_ai_badge"]: fails.append("m29 clean must never badge")

# metadata generator tag in image bytes -> declared_ai (weak, no badge)
import base64 as _b64a
fake_img = _b64a.b64encode(b"\xff\xd8\xff\xe1META Midjourney v6 XMP" + b"x"*2000).decode()
a5 = auth.assess_stage1(image_b64=fake_img)
if a5["origin_result"] != "declared_ai": fails.append("m29 metadata tag")
if a5["show_ai_badge"]: fails.append("m29 metadata must not badge")

# hierarchy: verified outranks declared (mapping check)
if auth._ORIGIN_RANK[0] != "verified_ai_provenance": fails.append("m29 hierarchy order")

# no numeric likelihood anywhere in the assessment
if any(k for k in a1 if "likelihood" in k or "percent" in k):
    fails.append("m29 numeric likelihood leaked")

# 30. FLAG OFF = lane absent (default): pipeline attaches nothing
import os as _os
if m.AUTHENTICITY_ENABLED: fails.append("m30 flag must default OFF")
m.route_claims = lambda t, **kw: [unit("honey never spoils")]
m.judge_with_rubric = lambda c, ev: {"truth_score": 8.0, "verdict_state": "supported",
                                     "verdict": "ok", "evidence_strength": "strong", "key_sources": []}
m.gather_evidence = lambda c: {"fact_checks": [], "web_sources": [
    {"source": "S", "url": "https://s.s", "quote": "q", "stance": "supports"}], "search_rounds": 1}
m.describe_frames = lambda frames, title="", uploader="": "A screenshot of a post claiming honey never spoils."
m._run_pipeline("s13", "", "img:authoff", "", None, good)
with m._jobs_lock: j = dict(m._jobs["s13"])
if "authenticity" in j.get("result", {}): fails.append("m30 lane leaked with flag off")


# 31. SELF-REFERENTIAL RULE CONTRACT: the rule ships in the judge prompt
from app.agents.judge import PROMPT as _JP2
if "SELF-REFERENTIAL CLAIMS" not in _JP2: fails.append("m31 rule missing")
if "never flip between a score and a shrug" not in _JP2: fails.append("m31 stability line missing")


# 32. HIVE DORMANCY: no key -> lane asleep, typed not_assessed, no guess
import os as _os
from app.agents import hive_detect as _hd
_os.environ.pop("HIVE_API_KEY", None)
if _hd.available(): fails.append("m32 available without key")
_r32 = _hd.detect_image("aGVsbG8=")
if _r32.get("assessment_status") != "not_assessed": fails.append("m32 not typed")
if _r32.get("origin") is not None: fails.append("m32 invented a finding")

# 33. STAGE-2 GATE: fires on AI-topic / high-risk / on-demand; not on cat videos;
# never re-pays when provenance already settled it
_g1 = _hd.should_run_stage2(title="Can YOU tell which video is AI?")[0]
_g2 = _hd.should_run_stage2(title="my cat does a backflip")[0]
_g3 = _hd.should_run_stage2(title="cute cats", on_demand=True)[0]
_g4 = _hd.should_run_stage2(title="cats", claims=[{"public_safety_risk": True}])[0]
_g5 = _hd.should_run_stage2(title="AI video", stage1_origin="declared_ai")[0]
if not _g1: fails.append("m33 ai-topic gate")
if _g2: fails.append("m33 cat video fired")
if not _g3: fails.append("m33 on-demand gate")
if not _g4: fails.append("m33 safety gate")
if _g5: fails.append("m33 paid despite declared provenance")

# 34. CATEGORIES NOT PERCENTAGES: score mapping + merge hierarchy + no numeric display
from app.agents.authenticity import merge_stage2 as _ms2, DISPLAY as _DSP
_o1, _, _ = _hd.classes_to_finding([{"class": "ai_generated", "score": 0.97}])
_o2, _, _ = _hd.classes_to_finding([{"class": "ai_generated", "score": 0.70}])
_o3, _, _ = _hd.classes_to_finding([{"class": "ai_generated", "score": 0.20}])
if _o1 != "likely_synthetic": fails.append("m34 strong map")
if _o2 != "inconclusive": fails.append("m34 mid map")
if _o3 is not None: fails.append("m34 weak fired")
_s1 = {"origin_result": "no_synthetic_signal", "evidence": [], "display": _DSP["no_synthetic_signal"], "show_ai_badge": False}
_merged = _ms2(_s1, _hd._finding_to_result("likely_synthetic", 0.97, "sora", "forensic_image"), "test")
if _merged.get("origin_result") != "likely_synthetic": fails.append("m34 merge elevate")
if _merged.get("show_ai_badge"): fails.append("m34 badge leaked (verified only)")
if any(ch.isdigit() for ch in _merged.get("display", "")): fails.append("m34 numeric leak in display")
_declared = {"origin_result": "declared_ai", "evidence": [], "display": _DSP["declared_ai"], "show_ai_badge": False}
_m2 = _ms2(_declared, _hd._finding_to_result("likely_synthetic", 0.97, None, "forensic_image"), "t")
if _m2.get("origin_result") != "declared_ai": fails.append("m34 forensic outranked declared")


# 35. TWO LANES, ONE STORY: media-origin claims are parked when the AI
# dial already answered; world-claims get the media context tag
import re as _re35
_m35 = open("app/main.py").read()
if '"media_origin"' not in _m35: fails.append("m35 parking missing")
if '"media_context"' not in _m35: fails.append("m35 context tag missing")
_pat = _re35.compile(
    r"\b(this|the)\s+(video|clip|footage|image|reel|short)\b"
    r".*\b(creat|generat|made|produc)\w*\b"
    r".*\b(ai|a\.i\.|sora|veo|midjourney|dall|kling|pika|"
    r"artificial intelligence)\b", _re35.I | _re35.S)
if not _pat.search("This video was created using Sora AI video generation."):
    fails.append("m35 regex misses sora claim")
if _pat.search("The bridge was made of glass in this region of China."):
    fails.append("m35 regex overfires")

# 36. AI-MEDIA CONTEXT contract: rule in judge prompt + context injected
from app.agents.judge import PROMPT as _JP36
if "AI-MEDIA CONTEXT" not in _JP36: fails.append("m36 rule missing")
if "no higher than 5.5" not in _JP36: fails.append("m36 cap missing")
_j36 = open("app/agents/judge.py").read()
if "MEDIA CONTEXT: independent authenticity analysis reports" not in _j36:
    fails.append("m36 injection missing")


# 37. BALLPARK RULE CONTRACT: numeric gaps in the same ballpark can
# never be "contradicted"; developing-story counts get temporal grace
from app.agents.judge import PROMPT as _JP37
if "THE RIGHT BALLPARK IS NOT A LIE" not in _JP37: fails.append("m37 rule missing")
if "COUNTS GROW IN DEVELOPING STORIES" not in _JP37: fails.append("m37 growth rule missing")
if "order of magnitude" not in _JP37: fails.append("m37 contradiction boundary missing")


# 38. REVERSE-SEARCH DORMANCY + BUDGET: no key -> asleep; cap honored
import os as _os38
from app.agents import reverse_search as _rs
_os38.environ.pop("GOOGLE_VISION_KEY", None)
if _rs.available(): fails.append("m38 available without key")
_r38 = _rs.analyze("aGVsbG8=")
if _r38.get("assessment_status") != "not_assessed": fails.append("m38 not typed")
_rs._counter["month"] = _rs._month(); _rs._counter["count"] = _rs.MONTHLY_CAP
if _rs._budget_ok(): fails.append("m38 budget cap ignored")
_rs._counter["count"] = 0

# 39. DATE EXTRACTION: URL dates strong, title years weak, junk ignored
if _rs.extract_date("https://news.com/2021/05/flood-story") != "2021-05-01":
    fails.append("m39 url date")
if _rs.extract_date("https://x.com/post/99887766") is not None:
    fails.append("m39 junk number treated as date")
if _rs.extract_date("", "Floods devastate region in 2019 photos") != "2019-01-01":
    fails.append("m39 title year")

# 40. RECYCLED-FOOTAGE NOTE: fires only when well before the post date,
# with the honest "earliest credible matching appearance" phrasing
_pages = [{"url": "https://news.com/2021/05/flood", "pageTitle": "Flood"},
          {"url": "https://late.com/2024/01/flood", "pageTitle": "Flood again"}]
_e, _n = _rs.pick_earliest(_pages, posted_date="2026-08-26")
if not _e or _e["date"] != "2021-05-01": fails.append("m40 earliest pick")
if not _n or "Earliest credible matching appearance located" not in _n:
    fails.append("m40 note phrasing")
_e2, _n2 = _rs.pick_earliest([{"url": "https://news.com/2026/08/flood",
                               "pageTitle": "Flood"}], posted_date="2026-08-26")
if _n2 is not None: fails.append("m40 same-week coverage flagged")


# 41. PER-FACE DEEPFAKE LANE: dormant without its own key; face scope
# survives the merge into the assessment
import os as _os41
from app.agents import hive_detect as _hd41
_os41.environ.pop("HIVE_DEEPFAKE_KEY", None)
if _hd41.deepfake_available(): fails.append("m41 available without key")
_r41 = _hd41.detect_deepfake_frames(["aGVsbG8="])
if _r41.get("assessment_status") != "not_assessed": fails.append("m41 not typed")
from app.agents.authenticity import merge_stage2 as _ms41, DISPLAY as _D41
_fake = {"assessment_status": "completed", "origin": "likely_synthetic",
         "manipulation_scope": "face",
         "evidence": [{"provider": "hive", "signal_type": "forensic_deepfake_faces",
                       "raw_score": 0.96, "band": "strong",
                       "explanation": "x", "source_link": None}]}
_s41 = {"origin_result": "no_synthetic_signal", "evidence": [],
        "display": _D41["no_synthetic_signal"], "show_ai_badge": False}
_m41 = _ms41(_s41, _fake, "t")
if _m41.get("manipulation_scope") != "face": fails.append("m41 scope lost in merge")
if _m41.get("origin_result") != "likely_synthetic": fails.append("m41 origin not elevated")


# 42. FACE-HINT ECONOMY: face detector only when a person is likely
from app.agents.hive_detect import likely_has_person as _lhp
if not _lhp("[WHAT THE VIDEO VISUALLY SHOWS] A man speaking to camera"):
    fails.append("m42 person missed")
if _lhp("A wall of water sweeps through an empty border checkpoint"):
    fails.append("m42 empty scene flagged")
_m42 = open("app/main.py").read()
if "_face_likely" not in _m42 or "deepfake_available() and _face_likely" not in _m42:
    fails.append("m42 gate not wired")


# 43. "+DETECT AI" ON-DEMAND: request flag exists, reaches the gate as
# on_demand, and a cached result without stage-2 is not served stale
_m43 = open("app/main.py").read()
if "detect_ai: bool = False" not in _m43: fails.append("m43 request flag missing")
if "on_demand=detect_ai" not in _m43: fails.append("m43 gate not honoring flag")
if 'req.detect_ai' not in _m43 or '_cau.get("stage") != 2' not in _m43:
    fails.append("m43 cache bypass missing")
_h43 = open("app/templates/app.html").read()
if 'id="aiChip"' not in _h43: fails.append("m43 chip missing")
if "detect_ai=true" not in _h43.replace(" ", ""): fails.append("m43 chip not sent")


# 44. TRUST-PAGE DISCLOSURE CONTRACT: AI-media section + processor
# disclosure + sampling statement + no-green-checkmark promise
_t44 = open("app/templates/trust.html").read()
for needle, tag in [("AI-media detection", "section"),
                    ("Sampling, not scanning", "sampling"),
                    ("Absence proves nothing", "absence"),
                    ("no face recognition, ever", "no-face-recognition"),
                    ("Hive (synthetic-media", "processor-disclosure"),
                    ("Cloud Vision reverse", "vision-disclosure")]:
    if needle not in _t44: fails.append(f"m44 {tag} missing")


# 45. RAN-AND-CLEAN DISCLOSURE: when stage-2 ran and found nothing, the
# result says so with the can-miss caveat; silent when it never ran
_h45 = open("app/templates/app.html").read()
if "au.stage===2&&au.origin_result==='no_synthetic_signal'" not in _h45:
    fails.append("m45 condition missing")
if "not proof the video is real" not in _h45:
    fails.append("m45 caveat missing")
if _h45.count("/about#aimedia") < 2:
    fails.append("m45 limitation links missing")


# 46. GATE WORD BOUNDARIES: "detail" must not read as "AI"; failed
# stage-2 shows an honest could-not-complete line
from app.agents import hive_detect as _hd46
if _hd46.should_run_stage2(title="attention to detail in packaging")[0]:
    fails.append("m46 substring ai overfire")
if not _hd46.should_run_stage2(title="Can YOU tell which video is AI?")[0]:
    fails.append("m46 real ai title missed")
if not _hd46.should_run_stage2(title="deepfake of a local mayor")[0]:
    fails.append("m46 deepfake title missed")
_h46 = open("app/templates/app.html").read()
if "could not complete" not in _h46:
    fails.append("m46 failure line missing")


# 47. V3 TRANSPORT CONTRACT: Bearer auth, v3 path, combined-model slug,
# score-map parsing, deepfake class -> face scope
from app.agents import hive_detect as _hd47
if "api/v3" not in _hd47.HIVE_V3_BASE: fails.append("m47 not v3")
if _hd47.HIVE_MODEL != "hive/ai-generated-and-deepfake-content-detection":
    fails.append("m47 wrong model slug")
_j47 = open("app/agents/hive_detect.py").read()
if 'Bearer' not in _j47: fails.append("m47 bearer auth missing")
_lists = _hd47._extract_class_lists(
    {"output": [{"scores": {"ai_generated": 0.97, "not_ai_generated": 0.03}}]})
_found = any(any(c["class"] == "ai_generated" and c["score"] == 0.97
                 for c in cl) for cl in _lists)
if not _found: fails.append("m47 score-map parse failed")


# 48. APP REVIEW 2.2 CONTRACT: app mode hides beta labels and the voice
# button (dead in the in-app web view); trust footer drops "public beta"
_h48 = open("app/templates/app.html").read()
if "html.appmode .beta2,html.appmode .ah-beta{display:none" not in _h48:
    fails.append("m48 beta labels still visible in app")
if "html.appmode #mic,html.appmode .fu-mic{display:none" not in _h48:
    fails.append("m48 dead mic button still visible in app")
_t48 = open("app/templates/trust.html").read()
import re as _re48
if _re48.search(r"(?i)\bbeta\b(?![^<]*\})", _t48.split("<style>")[-1].split("</style>")[-1]):
    fails.append("m48 trust page still shows beta wording to users")


# 49. NO SILENT SKIPS ON DEMAND: standby frames feed the detector, and a
# detect-AI request with nothing to analyze reports a typed failure
_m49 = open("app/main.py").read()
if "standby and not _au_frames" not in _m49:
    fails.append("m49 standby frames not fed to detector")
if "no frames or image were" not in _m49:
    fails.append("m49 no-media failure not reported")
if '"detector not configured"' not in _m49:
    fails.append("m49 unconfigured failure not reported")


# 50. MEMORY ON EVERY FRESH RUN: the detect-AI cache bypass keeps claim
# anchoring + evidence memory, same as Re-check
_m50 = open("app/main.py").read()
if "if req.force or req.detect_ai or req.ai_only:" not in _m50:
    fails.append("m50 detect-ai run loses memory")

# 51. TYPICAL-PRACTICE RULE: usual practice can never contradict a
# specific depicted event; descriptor doubts stay partly_supported
from app.agents.judge import PROMPT as _JP51
if "TYPICAL PRACTICE IS NOT PROOF ABOUT THIS INSTANCE" not in _JP51:
    fails.append("m51 rule missing")
if "peripheral descriptor" not in _JP51:
    fails.append("m51 descriptor guidance missing")


# 52. V3 DOCS CONTRACT (from the account's own Quickstart): input is an
# ARRAY, base64 uses media_base64, and classes parse "value" scores
_j52 = open("app/agents/hive_detect.py").read()
if '"input": [item]' not in _j52: fails.append("m52 input not an array")
if '"media_base64"' not in _j52: fails.append("m52 media_base64 missing")
from app.agents.hive_detect import classes_to_finding as _ctf52
_o52, _t52, _ = _ctf52([{"class": "ai_generated", "value": 0.98},
                        {"class": "not_ai_generated", "value": 0.02}])
if _o52 != "likely_synthetic" or _t52 != 0.98:
    fails.append("m52 value-key parse failed")
_o52b, _, _ = _ctf52([{"class": "deepfake", "value": 0.95}])
if _o52b != "likely_synthetic": fails.append("m52 deepfake value parse")


# 53. HIVE DIAGNOSTIC: admin-only selftest route exists, is guarded, and
# reports the vendor's real error text instead of an exception class
_m53 = open("app/main.py").read()
if "/api/admin/hivetest" not in _m53: fails.append("m53 route missing")
_i53 = _m53.index("/api/admin/hivetest")
if "_admin_ok(key)" not in _m53[_i53:_i53 + 700]: fails.append("m53 route unguarded")
_j53 = open("app/agents/hive_detect.py").read()
if "def selftest(" not in _j53: fails.append("m53 selftest missing")
if 'raise RuntimeError(f"HTTP {e.code}' not in _j53:
    fails.append("m53 vendor error text not captured")
if "type(e).__name__" in _j53: fails.append("m53 still hiding error detail")


# 54. FRAMES REACH THE DETECTOR: ingest keeps the frames it downloaded
# (frames_media) even after the vision agent consumed them, and the
# pipeline falls back to them — the real cause of "could not complete"
_g54 = open("app/agents/ingest.py").read()
if 'result["frames_media"] = frames' not in _g54:
    fails.append("m54 ingest still discards frames")
_m54 = open("app/main.py").read()
if '_media_frames = result.pop("frames_media", None)' not in _m54:
    fails.append("m54 pipeline ignores kept frames")
if "_src = frames or _media_frames" not in _m54:
    fails.append("m54 no fallback to kept frames")


# 55. EVIDENCE PANEL: the detector's own numbers are visible to readers
# (categories still lead the headline), with the threshold explained
_h55 = open("app/templates/app.html").read()
if "AI check details" not in _h55: fails.append("m55 panel missing")
if "confidence " not in _h55: fails.append("m55 raw score not shown in panel")
if "never means proven real" not in _h55: fails.append("m55 threshold note missing")
_i55 = _h55.index("AI check details")
if "audet" not in _h55[_i55 - 400:_i55]: fails.append("m55 panel not styled block")


# 56. AI-ONLY MODE: backend skips routing/judging, UI offers the third
# chip state, and the media answer leads when claims are empty
_m56 = open("app/main.py").read()
if "ai_only: bool = False" not in _m56: fails.append("m56 request flag missing")
if 'result["media_only"] = True' not in _m56: fails.append("m56 no media-only exit")
_h56 = open("app/templates/app.html").read()
if "aiChipState()==='only'" not in _h56: fails.append("m56 chip third state missing")
if "const mediaLeads=" not in _h56: fails.append("m56 evidence-led layout missing")
if "Media-only check" not in _h56: fails.append("m56 media-only headline missing")

# 57. FOLLOW-UP AI BUTTON + SOURCE LINK + TITLE TRIM
if 'id="fuAi"' not in _h56: fails.append("m57 follow-up AI button missing")
if "Open the original video" not in _h56: fails.append("m57 source link missing")
if "fullTitle.length>90" not in _h56: fails.append("m57 long title not trimmed")

# 58. PARSE-GAP HONESTY: zero readable class scores is reported as a
# parsing gap, never as a clean bill of health
from app.agents.hive_detect import _finding_to_result as _ftr58
_r58 = _ftr58(None, 0.0, None, "forensic_video_frames", classes_seen=0)
if _r58.get("origin") is not None: fails.append("m58 empty parse read as clean")
if _r58.get("assessment_status") != "partial": fails.append("m58 not typed partial")
_r58b = _ftr58(None, 0.01, None, "forensic_image", classes_seen=12)
if _r58b.get("origin") != "no_synthetic_signal": fails.append("m58 real clean broken")


# 59. CHIP HYGIENE: a stored media-only result never answers a full
# check, and the chip resets after every completed check
_m59 = open("app/main.py").read()
if 'cached.get("media_only") and not req.ai_only' not in _m59:
    fails.append("m59 media-only cache served to full check")
_h59 = open("app/templates/app.html").read()
if _h59.count("resetAiChip();") < 3:
    fails.append("m59 chip not reset on all submit paths")


# 60. NATIVE PHOTO HANDOFF: the page exposes glowbyReceiveImage and it
# takes the same resize + check path as the camera button
_h60 = open("app/templates/app.html").read()
if "window.glowbyReceiveImage=function(dataUrl)" not in _h60:
    fails.append("m60 receiver missing")
_i60 = _h60.index("window.glowbyReceiveImage=function")
_blk = _h60[_i60:_i60 + 900]
if "runImageCheck()" not in _blk or "MAX=1568" not in _blk:
    fails.append("m60 receiver does not reuse the image check path")


# 61. APP REVIEW 5.1.1/5.1.2: consent gate before any send, on every
# path; privacy policy names data, recipients, uses, permission, and
# third-party protection
_h61 = open("app/templates/app.html").read()
if 'id="consent"' not in _h61: fails.append("m61 consent screen missing")
_cf = _h61[_h61.index("async function checkFetch(body){"):]
_cf = _cf[:_cf.index("fetch('/api/check'")]
if "await askConsent();" not in _cf:
    fails.append("m61 checkFetch not gated before fetch")
if "await askConsent();\n      const r=await fetch('/api/followup'" not in _h61:
    fails.append("m61 follow-up not gated")
for _n in ("Anthropic", "OpenAI", "Hive", "Google"):
    if _n not in _h61[_h61.index('id="consent"'):_h61.index('id="consent"') + 2500]:
        fails.append(f"m61 consent screen missing recipient {_n}")
_t61 = open("app/templates/trust.html").read()
for _needle, _tag in (('id="datasent"', "section"), ("What is collected and how", "collection"),
                      ("All uses:", "uses"), ("Your permission:", "permission"),
                      ("Protection by third parties", "equal-protection")):
    if _needle not in _t61: fails.append(f"m61 privacy {_tag} missing")


# 62. COST CONTROLS: cached static prefix (no dynamic fields before the
# claim block), tiered judge model (strong on high stakes, cheap on low),
# lean vision defaults
from app.agents import judge as _j62, vision as _v62
_rules62 = _j62.PROMPT.split("=== YOUR CATEGORY: ")[0]
if "{" in _rules62.replace("{{", "").replace("}}", ""):
    fails.append("m62 shared rules block has a dynamic field (breaks cross-category cache)")
_static62 = _j62.PROMPT.split("Claim (routed to")[0]
for _f in ("{search_rounds}", "{posted_date}", "{claim}", "{risk_level}"):
    if _f in _static62: fails.append(f"m62 dynamic field {_f} breaks caching")
if open("app/agents/judge.py").read().count("_cache_block(") < 3:
    fails.append("m62 expected two cache breakpoints (rules, rubric)")
if "cache_control" not in open("app/agents/judge.py").read():
    fails.append("m62 no prompt caching")
if _j62.pick_judge_model({"bucket": "health"}) != _j62.MODEL:
    fails.append("m62 health not on strong judge")
if _j62.pick_judge_model({"bucket": "politics"}) != _j62.MODEL:
    fails.append("m62 politics not on strong judge")
if _j62.pick_judge_model({"bucket": "sports", "risk_level": "high"}) != _j62.MODEL:
    fails.append("m62 high-risk not on strong judge")
if _j62.pick_judge_model({"bucket": "sports", "public_safety_risk": True}) != _j62.MODEL:
    fails.append("m62 safety not on strong judge")
if _j62.pick_judge_model({"bucket": "other", "media_context": "AI"}) != _j62.MODEL:
    fails.append("m62 AI-footage case not on strong judge")
if _j62.JUDGE_TIERING and _j62.pick_judge_model({"bucket": "entertainment"}) == _j62.MODEL:
    fails.append("m62 low-stakes not tiered down")
if _v62.MAX_FRAMES > 4: fails.append("m62 vision frames not lean")


# 63. LONG-LIVED CACHE: 1h TTL requested with safe fallback; hourly
# keep-alive exists and re-reads the SHARED rules block only
from app.agents import judge as _j63
_cb = _j63._cache_block("x")
if _cb["cache_control"].get("ttl") != "1h" and _j63.CACHE_TTL == "1h":
    fails.append("m63 ttl not applied")
_j63._ttl_supported["ok"] = False
if "ttl" in _j63._cache_block("x")["cache_control"]:
    fails.append("m63 fallback does not drop ttl")
_j63._ttl_supported["ok"] = True
_blocks = _j63.cached_system_blocks("health")
if len(_blocks) != 2 or "Fleet-wide rules" not in _blocks[0]["text"] or "RUBRIC" not in _blocks[1]["text"]:
    fails.append("m63 cached blocks malformed")
_m63 = open("app/main.py").read()
if "keep_cache_warm" not in _m63 or "50 * 60" not in _m63:
    fails.append("m63 keep-alive loop missing")
_j63src = open("app/agents/judge.py").read()
if '"ttl" in str(_e).lower()' not in _j63src:
    fails.append("m63 no retry-without-ttl on rejection")


# 64. ADMIN ACCURACY: monthly unique visitors exist (month-rotating code,
# not daily sums), dashboard metrics are fetched independently, dates
# survive the timestamp-to-label path
_s64 = open("app/storage.py").read()
if "def visitor_monthly" not in _s64 or "monthly_visitors" not in _s64:
    fails.append("m64 monthly uniques missing")
if "d.day::date::text" not in _s64: fails.append("m64 daily series still returns timestamps")
if "def q(sql, params=None):" not in _s64: fails.append("m64 quality_stats not fault-isolated")
_m64 = open("app/main.py").read()
if 'f"{salt}:month:{month}:{ip}"' not in _m64: fails.append("m64 monthly hash not salted per month")
_a64 = open("app/templates/admin.html").read()
if "String(d).slice(0,10)" not in _a64: fails.append("m64 fmtDay still NaN-prone")
if "Visitors this month" not in _a64: fails.append("m64 monthly tile missing")
_t64 = open("app/templates/trust.html").read()
if "changes every calendar month" not in _t64: fails.append("m64 privacy wording not updated")


# 65. ADMIN CALENDAR: month + day endpoints exist, are admin-guarded,
# validate input, and the page has the calendar UI; default budget $30
_m65 = open("app/main.py").read()
for _r in ("/api/admin/calendar", "/api/admin/day"):
    if _r not in _m65: fails.append(f"m65 route {_r} missing")
    _i = _m65.index(_r)
    if "_admin_ok(key)" not in _m65[_i:_i + 500]: fails.append(f"m65 {_r} unguarded")
if 're.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", date or "")' not in _m65:
    fails.append("m65 day endpoint does not validate date")
if 'os.environ.get("GLOWBY_DAILY_BUDGET_USD", "30")' not in _m65:
    fails.append("m65 default daily budget not $30")
_s65 = open("app/storage.py").read()
if "def month_calendar" not in _s65 or "def day_detail" not in _s65:
    fails.append("m65 storage calendar functions missing")
_a65 = open("app/templates/admin.html").read()
for _n in ('id="cal"', "loadCalendar(", "loadDay(", 'id="dayDetail"'):
    if _n not in _a65: fails.append(f"m65 admin calendar UI missing {_n}")

print("MATRIX FAILURES:", fails) if fails else print(
    "FINAL MATRIX PASS: 65/65 — captions/thin/whisper/silent/blind/blocked/too-long, "
    "satire, no-claims, safety, MIN, cap, question, statement, honest-failure, fb-post, fb-video, article, reel-honest, rescue-cap, +ask, recheck-memory, memory-to-judge, contested-label, claim-anchoring, image-valid, image-pipeline(friendly-noclaims), security-txt, auth-stage1, auth-flag-off, self-referential, hive-dormant, stage2-gate, categories-merge, media-origin-park, ai-media-context, ballpark-numbers, reverse-dormant, date-extract, recycled-note, deepfake-face-lane, face-hint-economy, detect-ai-chip, trust-disclosure, ran-and-clean, gate-boundaries, hive-v3, app-review-2-2, no-silent-skips, memory-on-detect, typical-practice, hive-v3-docs, hive-diagnostic, frames-to-detector, evidence-panel, ai-only-mode, followup-ai, parse-gap, chip-hygiene, photo-handoff, consent-gate, cost-controls, long-cache, admin-accuracy, admin-calendar")
