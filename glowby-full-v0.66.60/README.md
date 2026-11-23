# Glowby

**Paste a link, get a fact-check.**

Multi-agent AI system for misinformation detection and fact-checking.
v1 supports user-submitted URLs (YouTube/Shorts first; TikTok and X best-effort).

## Status

- [x] Project skeleton + "Glowby is alive" page
- [ ] Ingest agent (URL → transcript)
- [ ] Claim extraction agent (transcript → checkable claims)
- [ ] Evidence agent (claim → sources) — Week 2
- [ ] Verdict agent (rating + confidence + citations) — Week 2
- [ ] Public UI + shareable result permalinks — Week 3
- [ ] Launch — Aug 13, 2026

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000 — you should see "Glowby is alive".

## Deploy (Railway)

1. Push this repo to GitHub.
2. On [railway.app](https://railway.app): **New Project → Deploy from GitHub repo** → pick this repo.
3. Add the database: **New → Database → PostgreSQL**.
4. Under the service's **Variables** tab, add the keys listed in `.env.example`.
5. Railway redeploys automatically on every push to `main`.

## Project layout

```
app/
  main.py            FastAPI app — homepage + /health
  agents/
    ingest.py        URL → transcript        (Week 1)
    claims.py        transcript → claims     (Week 1)
requirements.txt     Python dependencies
railway.json         Railway deploy config
.env.example         Environment variables the app needs (no real keys!)
```
