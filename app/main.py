"""
Glowby — multi-agent misinformation detection & fact-checking.

v1 scope: paste a link, get a fact-check.
Current stage: ingest (URL -> transcript) + claim extraction live.
Next: evidence + verdict agents (Week 2).
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agents.claims import ClaimExtractionError, extract_claims
from app.agents.ingest import IngestError, ingest

app = FastAPI(
    title="Glowby",
    description="Paste a link, get a fact-check.",
    version="0.3.0",
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
            <p style="margin-top:1.5rem; font-size:0.85rem;">v0.3.0 &mdash; transcript + claim extraction live; verdicts coming in Week 2</p>
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
                out.innerHTML = '<span class="meta">Step 1/2: fetching & transcribing... then Step 2/2: extracting checkable claims. Can take a minute.</span>';
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
                        let html = '<div class="meta">' + esc(d.platform) + ' &middot; ' +
                            esc(d.title) + ' &middot; ' + esc(d.uploader) + ' &middot; ' +
                            esc(d.duration_seconds) + 's &middot; transcript: ' + esc(d.transcript_source) + '</div>';
                        if (d.claims.length === 0) {
                            html += '<p>No checkable factual claims found in this video.</p>';
                        } else {
                            html += '<div class="meta"><b>' + d.claims.length + ' checkable claim' + (d.claims.length>1?'s':'') + ' found</b> (score = checkability, 0.0&ndash;10.0)</div>';
                            for (const c of d.claims) {
                                html += '<div class="claim">' +
                                    '<div class="score">' + esc(c.checkability.toFixed(1)) + '</div>' +
                                    '<div class="ctext"><b>' + esc(c.claim) + '</b>' +
                                    '&ldquo;' + esc(c.quote) + '&rdquo;' +
                                    '<br><span class="cat">' + esc(c.category) + '</span> ' +
                                    '<span class="meta">' + esc(c.why_checkable) + '</span></div></div>';
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
        claims = extract_claims(
            result["transcript"], title=result["title"], platform=result["platform"]
        )
    except ClaimExtractionError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error while extracting claims."},
        )

    result["claims"] = claims
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
    return {"status": "ok", "service": "glowby", "version": "0.3.0"}
