"""
Glowby — multi-agent misinformation detection & fact-checking.

v1 scope: paste a link, get a fact-check.
Current stage: ingest -> Sorting Gate/Router (13 buckets) -> evidence ->
CATEGORY JUDGE ENGINE (13 rubrics from app/specs/, truth score 0.0-9.9).
Next: output agent + real UI.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agents.evidence import gather_evidence
from app.agents.ingest import IngestError, ingest
from app.agents.router import (
    MODEL as ROUTER_MODEL,
    TAXONOMY_VERSION,
    RouterError,
    route_claims,
    select_for_verification,
)
from app.agents.judge import judge_with_rubric
from app.storage import canonical_key, get_cached, save_result, save_route_audit

# evidence is gathered for the top N claims by checkability (cost control)
MAX_CLAIMS_WITH_EVIDENCE = 3

app = FastAPI(
    title="Glowby",
    description="Paste a link, get a fact-check.",
    version="0.8.0",
)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    """Homepage — dev test form for the pipeline built so far."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Glowby</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                display: flex; align-items: center; justify-content: center;
                min-height: 100vh; margin: 0;
                background: #0f1117; color: #e8eaf0;
            }
            .card { text-align: center; padding: 2rem; max-width: 760px; width: 100%; }
            h1 { font-size: 2.5rem; margin-bottom: 0.5rem; }
            .status {
                display: inline-block; padding: 0.4rem 1rem; border-radius: 999px;
                background: #16351f; color: #4ade80; font-weight: 600;
            }
            p { color: #9aa0b0; }
            form { margin-top: 2rem; display: flex; gap: 0.5rem; }
            input[type=url] {
                flex: 1; padding: 0.8rem 1rem; border-radius: 10px;
                border: 1px solid #2a2f3d; background: #171b26; color: #e8eaf0;
                font-size: 1rem;
            }
            button {
                padding: 0.8rem 1.4rem; border-radius: 10px; border: none;
                background: #7c6cf6; color: white; font-size: 1rem; font-weight: 600;
                cursor: pointer;
            }
            button:disabled { opacity: 0.5; cursor: wait; }
            #out { margin-top: 1.5rem; text-align: left; display: none; }
            .meta { color: #9aa0b0; font-size: 0.85rem; margin-bottom: 0.6rem; }
            .err { color: #f87171; }
            .claim {
                display: flex; gap: 12px; align-items: flex-start;
                background: #171b26; border: 1px solid #2a2f3d;
                border-radius: 12px; padding: 12px; margin-top: 10px;
            }
            .score {
                flex: 0 0 auto; width: 46px; height: 46px; border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                font-weight: 800; font-size: 0.9rem; border: 3px solid #7c6cf6;
            }
            .ctext { font-size: 0.9rem; line-height: 1.45; color: #c9cdd8; }
            .ctext b { color: #e8eaf0; display: block; margin-bottom: 2px; }
            .src { margin-top: 7px; font-size: 0.82rem; color: #9aa0b0; line-height: 1.4; }
            .src a { color: #7c6cf6; text-decoration: none; font-weight: 600; }
            .verdict {
                display: inline-flex; align-items: center; gap: 6px;
                font-size: 0.8rem; font-weight: 800; padding: 3px 10px;
                border-radius: 999px; margin-top: 7px; border: 1px solid;
            }
            .v-true, .v-mostly_true { color: #4ade80; border-color: #4ade80; }
            .v-misleading { color: #fab219; border-color: #fab219; }
            .v-false { color: #f87171; border-color: #f87171; }
            .v-unverifiable { color: #9aa0b0; border-color: #9aa0b0; }
            .vsum { margin-top: 6px; font-size: 0.84rem; color: #c9cdd8; }
            .cat {
                display: inline-block; font-size: 0.7rem; font-weight: 700;
                padding: 1px 8px; border-radius: 999px; border: 1px solid #2a2f3d;
                color: #9aa0b0; margin-top: 5px; text-transform: uppercase;
            }
            details {
                margin-top: 14px; background: #171b26; border: 1px solid #2a2f3d;
                border-radius: 12px; padding: 10px 14px; font-size: 0.9rem;
                color: #c9cdd8;
            }
            summary { cursor: pointer; color: #9aa0b0; font-weight: 600; }
            .tr { white-space: pre-wrap; margin-top: 8px; max-height: 40vh; overflow-y: auto; line-height: 1.5; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Glowby</h1>
            <p>Paste a link, get a fact-check.</p>
            <div class="status">&#9679; Glowby is alive</div>
            <form id="f">
                <input type="url" id="url" placeholder="Paste a YouTube link..." required>
                <button id="go" type="submit">Check</button>
            </form>
            <div id="out"></div>
            <p style="margin-top:1.5rem; font-size:0.85rem;">v0.8.0 &mdash; 13 category judges live: one number = the TRUTH SCORE (0.0&ndash;9.9)</p>
        </div>
        <script>
            const f = document.getElementById('f');
            const out = document.getElementById('out');
            const go = document.getElementById('go');
            const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
            f.addEventListener('submit', async (e) => {
                e.preventDefault();
                go.disabled = true; go.textContent = 'Working...';
                out.style.display = 'block';
                out.innerHTML = '<span class="meta">Fetching & transcribing &rarr; gating & routing claims &rarr; gathering evidence &rarr; judging. Can take 1-2 minutes.</span>';
                try {
                    const r = await fetch('/api/check', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: document.getElementById('url').value})
                    });
                    const d = await r.json();
                    if (!r.ok) {
                        out.innerHTML = '<span class="err">' + esc(d.detail || 'Something went wrong.') + '</span>';
                    } else {
                        let html = '';
                        if (d.cached) {
                            html += '<div class="meta">&#9889; Instant result &mdash; this video was already checked on ' + esc((d.first_checked_at || '').slice(0,10)) + ', served from Glowby\\'s memory at zero cost.</div>';
                        }
                        html += '<div class="meta">' + esc(d.platform) + ' &middot; ' +
                            esc(d.title) + ' &middot; ' + esc(d.uploader) + ' &middot; ' +
                            esc(d.duration_seconds) + 's &middot; transcript: ' + esc(d.transcript_source) + '</div>';
                        if (d.claims.length === 0) {
                            html += '<p>No checkable factual claims found in this video.</p>';
                        } else {
                            const stanceIcon = {supports:'&#10003;', refutes:'&#10007;', mixed:'&#9888;', context:'&#8505;'};
                            const vLabel = {supported:'&#10003; Supported', partly_supported:'&#9888; Partly supported', provisional:'&#9203; Provisional', insufficient:'&#9888; Insufficient evidence', contradicted:'&#10007; Contradicted', unverifiable:'? Unverifiable', not_scoreable:'&#8709; Not scoreable'};
                            const vClass = {supported:'v-true', partly_supported:'v-misleading', provisional:'v-misleading', insufficient:'v-misleading', contradicted:'v-false', unverifiable:'v-unverifiable', not_scoreable:'v-unverifiable'};
                            const ringColor = (s) => s === null ? '#9aa0b0' : (s >= 7.5 ? '#4ade80' : (s >= 4.0 ? '#fab219' : '#f87171'));
                            const riskColor = {critical:'#f87171', high:'#ec835a'};
                            const fwd = (c) => c.gate_label === 'factual' || c.gate_label === 'prediction';
                            const checked = d.claims.filter(c => c.verdict);
                            const waiting = d.claims.filter(c => !c.verdict && fwd(c));
                            const parked = d.claims.filter(c => !c.verdict && !fwd(c));
                            html += '<div class="meta"><b>' + d.claims.length + ' claim unit' + (d.claims.length>1?'s':'') + '</b> &middot; ' + checked.length + ' verified &middot; ' + parked.length + ' parked by the gate</div>';
                            const chips = (c) => {
                                let h = '<span class="cat">' + esc(c.bucket) + '</span>';
                                if (c.secondary_bucket) h += ' <span class="cat">+ ' + esc(c.secondary_bucket) + '</span>';
                                if (riskColor[c.risk_level]) h += ' <span class="cat" style="color:' + riskColor[c.risk_level] + '; border-color:' + riskColor[c.risk_level] + ';">' + esc(c.risk_level) + ' risk</span>';
                                if (c.developing_story) h += ' <span class="cat">&#9203; developing</span>';
                                if (c.gate_label === 'prediction') h += ' <span class="cat">prediction</span>';
                                if (c.public_safety_risk) h += ' <span class="cat" style="color:#f87171; border-color:#f87171;">&#9888; public safety</span>';
                                return h;
                            };
                            for (const c of [...checked, ...waiting]) {
                                let vd = '';
                                if (c.verdict) {
                                    vd = '<br><span class="verdict ' + (vClass[c.verdict.verdict_state] || 'v-unverifiable') + '">' +
                                        (vLabel[c.verdict.verdict_state] || esc(c.verdict.verdict_state)) + '</span>' +
                                        '<div class="vsum">' + esc(c.verdict.verdict) +
                                        ' <span class="meta">(evidence: ' + esc(c.verdict.evidence_strength) + ')</span></div>';
                                } else {
                                    vd = '<br><span class="meta">Routed; not verified in this pass (highest-risk claims go first).</span>';
                                }
                                let ev = '';
                                const fcs = (c.evidence && c.evidence.fact_checks) || [];
                                const webs = (c.evidence && c.evidence.web_sources) || [];
                                for (const fc of fcs) {
                                    ev += '<div class="src">&#128221; <a href="' + esc(fc.url) + '" target="_blank" rel="noopener">' + esc(fc.publisher) + '</a>: rated &ldquo;' + esc(fc.rating) + '&rdquo;</div>';
                                }
                                for (const w of webs) {
                                    ev += '<div class="src">' + (stanceIcon[w.stance] || '&#8505;') + ' <a href="' + esc(w.url) + '" target="_blank" rel="noopener">' + esc(w.source) + '</a> (' + esc(w.stance) + '): &ldquo;' + esc(w.quote) + '&rdquo;</div>';
                                }
                                if (c.evidence && !ev) ev = '<div class="src meta">No sources found yet.</div>';
                                let circle = '<div class="score">&middot;&middot;&middot;</div>';
                                if (c.verdict) {
                                    const ts = c.verdict.truth_score;
                                    const col = ringColor(ts);
                                    circle = '<div class="score" style="border-color:' + col + '; color:' + col + ';">' + (ts === null ? '&mdash;' : esc(ts.toFixed(1))) + '</div>';
                                }
                                html += '<div class="claim">' + circle +
                                    '<div class="ctext"><b>' + esc(c.claim) + '</b>' +
                                    '&ldquo;' + esc(c.quote) + '&rdquo;' +
                                    '<br>' + chips(c) + ' <span class="meta">' + esc(c.reason || '') + '</span>' +
                                    vd + ev + '</div></div>';
                            }
                            if (parked.length) {
                                html += '<div class="meta" style="margin-top:14px;"><b>Parked by the gate</b> &mdash; opinion, satire, and other non-factual content Glowby deliberately does not judge:</div>';
                                for (const c of parked) {
                                    html += '<div class="claim"><div class="ctext"><b>' + esc(c.claim) + '</b>' +
                                        '<span class="cat">' + esc(c.gate_label) + '</span> ' +
                                        '<span class="meta">' + esc(c.reason || '') + '</span></div></div>';
                                }
                            }
                        }
                        html += '<details><summary>Full transcript</summary><div class="tr">' + esc(d.transcript) + '</div></details>';
                        out.innerHTML = html;
                    }
                } catch (err) {
                    out.innerHTML = '<span class="err">Network error.</span>';
                }
                go.disabled = false; go.textContent = 'Check';
            });
        </script>
    </body>
    </html>
    """


class CheckRequest(BaseModel):
    url: str


@app.post("/api/check")
def api_check(req: CheckRequest):
    """Full pipeline so far: URL -> transcript -> checkable claims.

    Synchronous for Week 1-2 dev testing; moves to a job queue in Week 3.
    """
    # cache first: if anyone already checked this video, serve the stored
    # result instantly — zero API cost.
    url_key = canonical_key(req.url)
    cached = get_cached(url_key)
    if cached is not None:
        return cached

    try:
        result = ingest(req.url)
    except IngestError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while fetching this link."},
        )

    try:
        claims = route_claims(
            result["transcript"],
            title=result["title"],
            platform=result["platform"],
            uploader=result["uploader"],
        )
    except RouterError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while routing claims."},
        )

    # audit record for every routed claim (router spec section 3.10)
    try:
        save_route_audit(url_key, req.url, claims, ROUTER_MODEL, TAXONOMY_VERSION)
    except Exception:
        pass

    # verify the top forward claims: risk level first, then routing
    # confidence (cost control; parked claims are never "debunked")
    for i in select_for_verification(claims, MAX_CLAIMS_WITH_EVIDENCE):
        try:
            claims[i]["evidence"] = gather_evidence(claims[i]["claim"])
        except Exception:
            claims[i]["evidence"] = {"fact_checks": [], "web_sources": []}
        try:
            claims[i]["verdict"] = judge_with_rubric(claims[i], claims[i]["evidence"])
        except Exception:
            claims[i]["verdict"] = {
                "truth_score": None,
                "verdict_state": "unverifiable",
                "verdict": "Judge step failed; try again.",
                "evidence_strength": "none",
                "key_sources": [],
            }

    result["claims"] = claims
    result["cached"] = False
    save_result(url_key, req.url, result)
    return result


@app.post("/api/ingest")
def api_ingest(req: CheckRequest):
    """Transcript only (kept for testing the ingest stage in isolation)."""
    try:
        return ingest(req.url)
    except IngestError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while processing this link."},
        )


@app.get("/health")
def health() -> dict:
    """Health check endpoint — Railway uses this to confirm the app is up."""
    return {"status": "ok", "service": "glowby", "version": "0.8.0"}
