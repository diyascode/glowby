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
import urllib.parse
import urllib.request

MAX_DURATION_SECONDS = 20 * 60  # v1 scope: no videos longer than 20 minutes


class IngestError(Exception):
    """Raised when a URL cannot be ingested. Message is user-facing."""


# ---------------------------------------------------------------- proxy
# YouTube bot-challenges datacenter IPs. A residential proxy makes our
# requests come from a normal-home address. Two config options:
#   WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD  (recommended:
#     Webshare rotating residential)
#   PROXY_URL  (any provider, e.g. http://user:pass@host:port)
# With neither set, everything works exactly as before (no proxy).


def _proxy_url():
    user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    pw = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if user and pw:
        return f"http://{user}-rotate:{pw}@p.webshare.io:80"
    return os.environ.get("PROXY_URL") or None


def _yta_client():
    """youtube-transcript-api client, proxied when configured."""
    from youtube_transcript_api import YouTubeTranscriptApi

    user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    pw = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    generic = os.environ.get("PROXY_URL")
    try:
        if user and pw:
            from youtube_transcript_api.proxies import WebshareProxyConfig

            return YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(proxy_username=user,
                                                 proxy_password=pw)
            )
        if generic:
            from youtube_transcript_api.proxies import GenericProxyConfig

            return YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(http_url=generic,
                                                https_url=generic)
            )
    except Exception:
        pass  # fall through to unproxied client
    return YouTubeTranscriptApi()


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
        # datacenter IPs get bot-challenged by YouTube's web client; the
        # android/ios player clients are challenged far less often
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
    }
    if _proxy_url():
        ydl_opts["proxy"] = _proxy_url()
    info = None
    bot_blocked = False
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        err_text = str(e)
        bot_blocked = "Sign in to confirm" in err_text or "not a bot" in err_text
        if platform == "youtube":
            # Plan B: a different transcript door that is rarely bot-challenged
            fallback = _youtube_fallback(url)
            if fallback is not None:
                return fallback
        if bot_blocked:
            raise IngestError(
                "YouTube is temporarily challenging our server ('are you a "
                "bot?'). This usually passes in a few minutes — please try "
                "again shortly, or try a different video."
            )
        raise IngestError(
            f"Could not read this {platform} link. It may be private, deleted, "
            f"age-restricted, or an unsupported URL. (Details: {err_text[:200]})"
        )

    duration = info.get("duration") or 0
    if duration > MAX_DURATION_SECONDS:
        raise IngestError(
            f"This video is {duration // 60} minutes long — Glowby v1 supports "
            f"videos up to {MAX_DURATION_SECONDS // 60} minutes."
        )

    raw_date = str(info.get("upload_date") or "")  # yt-dlp: YYYYMMDD
    posted = (
        f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        if len(raw_date) == 8 else None
    )
    result = {
        "url": url,
        "platform": platform,
        "title": info.get("title") or "(untitled)",
        "uploader": info.get("uploader") or info.get("channel") or "(unknown)",
        "duration_seconds": duration,
        "posted_date": posted,
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


# ------------------------------------------------------- youtube fallback


def _youtube_video_id(url: str):
    """Extract the 11-char video id from any YouTube URL spelling."""
    parsed_host = re.findall(r"https?://([^/]+)", url)
    host = (parsed_host[0] if parsed_host else "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        m = re.match(r"https?://[^/]+/([A-Za-z0-9_-]{6,})", url)
        return m.group(1) if m else None
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(shorts|embed|live)/([A-Za-z0-9_-]{6,})", url)
    return m.group(2) if m else None


def _youtube_fallback(url: str):
    """Get transcript via youtube-transcript-api + metadata via oEmbed.

    Used when yt-dlp is bot-challenged. Returns a result dict, or None if
    this door is closed too (then the caller raises a friendly error).
    """
    video_id = _youtube_video_id(url)
    if not video_id:
        return None

    # transcript (works for videos with captions, incl. auto-captions)
    text = None
    try:
        api = _yta_client()
        try:  # v1.x API
            fetched = api.fetch(video_id)
            segments = getattr(fetched, "snippets", fetched)
        except AttributeError:  # pre-1.0 API
            from youtube_transcript_api import YouTubeTranscriptApi

            segments = YouTubeTranscriptApi.get_transcript(video_id)
        parts = []
        for seg in segments:
            t = getattr(seg, "text", None)
            if t is None and isinstance(seg, dict):
                t = seg.get("text")
            if t:
                parts.append(t.strip())
        text = re.sub(r"\s+", " ", " ".join(parts)).strip() or None
    except Exception:
        return None
    if not text:
        return None

    # metadata via oEmbed (public endpoint, rarely challenged)
    title, uploader = "(untitled)", "(unknown)"
    try:
        oembed = (
            "https://www.youtube.com/oembed?format=json&url="
            + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}")
        )
        req = urllib.request.Request(oembed, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        title = meta.get("title") or title
        uploader = meta.get("author_name") or uploader
    except Exception:
        pass  # transcript is what matters; metadata is garnish

    return {
        "url": url,
        "platform": "youtube",
        "title": title,
        "uploader": uploader,
        "duration_seconds": 0,  # unknown via this door
        "posted_date": None,
        "transcript": text,
        "transcript_source": "captions",
    }


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
            **({"proxy": _proxy_url()} if _proxy_url() else {}),
            # convert whatever the site serves (HLS streams, .ts, etc.)
            # into MP3 — Whisper only accepts a fixed list of formats
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "96",
            }],
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
        # prefer a Whisper-supported format if several files were produced
        supported = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga",
                     ".oga", ".ogg", ".wav", ".webm"}
        preferred = [f for f in audio_files
                     if os.path.splitext(f)[1].lower() in supported]
        if not preferred:
            raise IngestError(
                "This site's audio format isn't supported yet. Try a "
                "YouTube link for this story instead."
            )
        audio_file = preferred[0]

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
