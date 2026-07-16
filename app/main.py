"""
Glowby — multi-agent misinformation detection & fact-checking.

v1 scope: paste a link, get a fact-check.
This is the application skeleton. Agents live in app/agents/ and are
wired into the pipeline as they are built (Week 1-2 of the launch plan).
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="Glowby",
    description="Paste a link, get a fact-check.",
    version="0.1.0",
)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    """Homepage — proves the app is deployed and alive."""
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
            .card { text-align: center; padding: 2rem; }
            h1 { font-size: 2.5rem; margin-bottom: 0.5rem; }
            .status {
                display: inline-block; padding: 0.4rem 1rem; border-radius: 999px;
                background: #16351f; color: #4ade80; font-weight: 600;
            }
            p { color: #9aa0b0; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Glowby</h1>
            <p>Paste a link, get a fact-check.</p>
            <div class="status">&#9679; Glowby is alive</div>
            <p style="margin-top:2rem; font-size:0.85rem;">v0.1.0 &mdash; skeleton deployed, agents coming soon</p>
        </div>
    </body>
    </html>
    """


@app.get("/health")
def health() -> dict:
    """Health check endpoint — Railway uses this to confirm the app is up."""
    return {"status": "ok", "service": "glowby", "version": "0.1.0"}
