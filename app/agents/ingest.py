"""
Ingest agent — turns a media URL into a transcript.

Strategy (cheapest first):
1. yt-dlp reads the video's metadata + caption tracks (no download).
2. If captions exist (manual preferred, else auto-generated), fetch and
   parse them into plain text. Free and fast.
3. If no captions, download the audio track and transcribe it with
   OpenAI Whisper. Costs a little; needed for most TikTok/X videos.

Returns a dict:
{
  "url": ..., "platform": "youtube"|"tiktok"|"x"|"other",
  "title": ..., "uploader": ..., "duration_seconds": ...,
  "transcript": "full text ...",
  "transcript_source": "captions"|"whisper",
}
Raises IngestError with a human-readable message on failure.
"""

import json
import os
import re
import tempfile
import urllib.request

MAX_DURATION_SECONDS = 20 * 60  # v1 scope: no videos longer than 20 minutes


class IngestError(Exception):
    """Raised when a URL cannot be ingested. Message is user-facing."""


def detect_platform(url: str) -> str:
    host = re.sub(r"^www\.", "", (re.findall(r"https?://([^/]+)", url) or [""])[0]).lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "twitter.com" in host or host == "x.com":
        return "x"
    return "other"


def ingest(url: str) -> dict:
    """Main entry point: URL in, transcript dict out."""
    import yt_dlp  # imported here so the app can boot even if install fails

    platform = detect_platform(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise IngestError(
            f"Could not read this {platform} link. It may be private, deleted, "
            f"age-restricted, or an unsupported URL. (Details: {str(e)[:200]})"
        )

    duration = info.get("duration") or 0
    if duration > MAX_DURATION_SECONDS:
        raise IngestError(
            f"This video is {duration // 60} minutes long — Glowby v1 supports "
            f"videos up to {MAX_DURATION_SECONDS // 60} minutes."
        )

    result = {
        "url": url,
        "platform": platform,
        "title": info.get("title") or "(untitled)",
        "uploader": info.get("uploader") or info.get("channel") or "(unknown)",
        "duration_seconds": duration,
        "transcript": None,
        "transcript_source": None,
    }

    # --- Path 1: caption tracks (free) ---
    transcript = _transcript_from_captions(info)
    if transcript:
        result["transcript"] = transcript
        result["transcript_source"] = "captions"
        return result

    # --- Path 2: Whisper transcription (paid fallback) ---
    transcript = _transcript_from_whisper(url)
    result["transcript"] = transcript
    result["transcript_source"] = "whisper"
    return result


# ---------------------------------------------------------------- captions


def _pick_caption_track(info: dict):
    """Prefer manual subtitles over auto-captions; prefer English; json3 format."""
    for source_key in ("subtitles", "automatic_captions"):
        tracks = info.get(source_key) or {}
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            for fmt in tracks.get(lang, []):
                if fmt.get("ext") == "json3":
                    return fmt
        # fall back to any language's json3 track
        for fmts in tracks.values():
            for fmt in fmts:
                if fmt.get("ext") == "json3":
                    return fmt
    return None


def _transcript_from_captions(info: dict):
    track = _pick_caption_track(info)
    if not track or not track.get("url"):
        return None
    try:
        req = urllib.request.Request(track["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return parse_json3_captions(data)


def parse_json3_captions(data: dict):
    """Parse YouTube's json3 caption format into plain text."""
    lines = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if text and text != "\n":
            lines.append(text)
    if not lines:
        return None
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# ---------------------------------------------------------------- whisper


def _transcript_from_whisper(url: str) -> str:
    """Download the audio track and transcribe it with OpenAI Whisper."""
    import yt_dlp

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise IngestError(
            "This video has no captions, and Whisper transcription is not "
            "configured (missing OPENAI_API_KEY)."
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        outpath = os.path.join(tmpdir, "audio.%(ext)s")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": outpath,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            raise IngestError(
                f"This video has no captions and the audio could not be "
                f"downloaded for transcription. (Details: {str(e)[:200]})"
            )

        audio_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if not audio_files:
            raise IngestError("Audio download produced no file.")
        audio_file = audio_files[0]

        if os.path.getsize(audio_file) > 25 * 1024 * 1024:
            raise IngestError(
                "The audio for this video is too large to transcribe (over 25MB)."
            )

        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        try:
            with open(audio_file, "rb") as f:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1", file=f
                )
        except Exception as e:
            raise IngestError(f"Transcription failed. (Details: {str(e)[:200]})")

    text = (transcription.text or "").strip()
    if not text:
        raise IngestError("Transcription produced no text (silent video?).")
    return text
