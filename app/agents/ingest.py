"""
Ingest agent — Step 9 of the baby steps (Week 1).

Responsibility: take a URL (YouTube first; TikTok/X best-effort),
download the media, and return a transcript.

Planned tools: yt-dlp for download, Whisper for speech-to-text,
YouTube caption tracks when available (cheaper + faster than Whisper).

Status: NOT BUILT YET. This placeholder defines the contract so the
rest of the app can be wired up around it.
"""


def ingest(url: str) -> dict:
    """Return {"url", "platform", "title", "transcript"} for a media URL."""
    raise NotImplementedError("Ingest agent is built in Week 1 (baby step 9).")
