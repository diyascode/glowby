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
import time
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
    err_text = ""
    # blocks are often luck-of-the-draw: a second knock seconds later
    # frequently gets in. Two attempts before giving up.
    for attempt in (1, 2):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            err_text = str(e)
            if attempt == 1:
                time.sleep(1.5)
    if info is None:
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
        if platform == "tiktok":
            # photo slideshows have no video file — but the EYES can still
            # read the cover image via TikTok's public oEmbed door
            photo = _tiktok_photo_result(url)
            if photo is not None:
                return photo
            raise IngestError(
                "TikTok blocked this video even after a retry — that "
                "happens with some TikToks (they fight automated tools "
                "hard). Try again in a few minutes, or try a different link."
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
        if _is_thin(transcript):  # rare: caption track is just music tags
            frames = _frames_from_video(url)
            if frames:
                result["frames"] = frames
        return result

    # --- Path 2+3 combined: no captions -> download the video ONCE and
    # feed both the ears (audio -> Whisper) and the eyes (frames) — in
    # PARALLEL. Silent videos no longer wait for the ears to finish
    # before the eyes start.
    text, frames, visual_desc, whisper_err = _transcribe_and_see(url)
    if text:
        result["transcript"] = text
        result["transcript_source"] = "whisper"
    else:
        result["transcript"] = ""
        result["transcript_source"] = "none"
    if _is_thin(result["transcript"]):
        if frames:
            result["frames"] = frames
            if visual_desc:
                result["visual_desc"] = visual_desc
        elif not result["transcript"]:
            # no speech, no captions, no readable frames — now it's over
            raise whisper_err or IngestError(
                "This video has no captions or speech, and its visuals "
                "could not be read either."
            )
    return result


def _tiktok_photo_result(url: str):
    """TikTok photo slideshows have no video to download — but the EYES
    can still read the COVER image, fetched through TikTok's public
    oEmbed endpoint (which is served to everyone, no bot wall).

    Returns an ingest-shaped result with the cover as a frame, or None
    when even oEmbed won't talk (then the caller shows the blocked error).
    """
    import base64

    try:
        oembed = ("https://www.tiktok.com/oembed?url="
                  + urllib.parse.quote(url, safe=""))
        req = urllib.request.Request(oembed, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
                          "Mac OS X) AppleWebKit/605.1.15"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            meta = json.loads(resp.read().decode())
        thumb = meta.get("thumbnail_url")
        if not thumb:
            return None
        req2 = urllib.request.Request(thumb, headers={
            "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            img = resp2.read()
        if not img or len(img) > 8 * 1024 * 1024:
            return None
        frame = base64.b64encode(img).decode()
        title = (meta.get("title") or "(TikTok photo post)")[:200]
        return {
            "url": url,
            "platform": "tiktok",
            "title": title,
            "uploader": meta.get("author_name") or "(unknown)",
            "duration_seconds": 0,
            "posted_date": None,
            "transcript": "",
            "transcript_source": "none",
            "frames": [frame],
            "photo_post": True,  # cover slide only — said in the output
        }
    except Exception:
        return None


def _is_thin(text) -> bool:
    """A transcript too thin to carry the video's claims by itself."""
    return len((text or "").split()) < 20


# speculative vision: on no-caption videos, run the EYES while Whisper
# is still listening (parallel, not sequential). If the audio turns out
# rich, the visual description is simply discarded (~1 cent). Silent
# videos — which need it anyway — save the whole listening wait.
SPECULATIVE_VISION = os.environ.get(
    "GLOWBY_SPECULATIVE_VISION", "on").lower() in ("on", "true", "1", "yes")


def _transcribe_and_see(url: str, max_frames: int = 6):
    """Download the video ONCE; feed both ears and eyes from it — at
    the same time.

    Returns (transcript, frames, visual_desc, error): transcript is None
    when audio failed or was silent; frames may still be present (silent
    videos make their claims visually); visual_desc is the eyes' report
    when speculative vision ran (None otherwise); error is the
    user-facing IngestError explaining the audio side.
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    import yt_dlp

    with tempfile.TemporaryDirectory() as tmpdir:
        outpath = os.path.join(tmpdir, "vid.%(ext)s")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "best[height<=480]/worst/bestvideo+bestaudio/best",
            "outtmpl": outpath,
            # same bot-challenge dodge as the metadata step (finding #15)
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
            **({"proxy": _proxy_url()} if _proxy_url() else {}),
        }
        dl_err = None
        for attempt in (1, 2):  # blocks are often transient — knock twice
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                dl_err = None
                break
            except Exception as e:
                dl_err = e
                if attempt == 1:
                    time.sleep(2)
        if dl_err is not None:
            if detect_platform(url) == "tiktok":
                return None, [], None, IngestError(
                    "TikTok blocked this video even after a retry — that "
                    "happens with some TikToks (they fight automated tools "
                    "hard). Try again in a few minutes, or try a "
                    "different link.")
            return None, [], None, IngestError(
                "This video has no captions and could not be downloaded "
                f"for analysis. (Details: {str(dl_err)[:200]})")
        vids = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if not vids:
            return None, [], None, IngestError(
                "The video download produced no file.")
        vid = max(vids, key=os.path.getsize)

        frames = _sample_frames(vid, tmpdir, max_frames)

        # audio track -> mp3
        audio = os.path.join(tmpdir, "audio.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-i", vid, "-vn", "-acodec", "libmp3lame",
                 "-q:a", "5", "-y", audio],
                capture_output=True, timeout=120)
        except Exception:
            pass

        # eyes start looking NOW, in parallel with whatever the ears do
        vision_future = None
        if frames and SPECULATIVE_VISION:
            pool = ThreadPoolExecutor(max_workers=1)
            vision_future = pool.submit(_describe_safely, frames)
            pool.shutdown(wait=False)

        def _vision_result():
            if vision_future is None:
                return None
            try:
                return vision_future.result(timeout=60)
            except Exception:
                return None

        if not os.path.exists(audio) or os.path.getsize(audio) == 0:
            return None, frames, _vision_result(), IngestError(
                "This video's audio could not be extracted (it may be silent).")
        if os.path.getsize(audio) > 25 * 1024 * 1024:
            return None, frames, _vision_result(), IngestError(
                "The audio for this video is too large to transcribe (over 25MB).")
        try:
            text = _whisper_file(audio)
        except IngestError as e:
            return None, frames, _vision_result(), e
        # rich speech -> the speculative description is discarded unread
        desc = _vision_result() if _is_thin(text) else None
        return text, frames, desc, None


def _describe_safely(frames: list):
    """Vision call that never raises (runs on a worker thread)."""
    try:
        from app.agents.vision import describe_frames

        return describe_frames(frames)
    except Exception:
        return None


def _whisper_file(audio_file: str):
    """Transcribe one audio file with Whisper. None when it hears nothing."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise IngestError(
            "This video has no captions, and Whisper transcription is not "
            "configured (missing OPENAI_API_KEY).")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    try:
        with open(audio_file, "rb") as f:
            transcription = client.audio.transcriptions.create(
                model="whisper-1", file=f)
    except Exception as e:
        raise IngestError(f"Transcription failed. (Details: {str(e)[:200]})")
    return (transcription.text or "").strip() or None


def _sample_frames(vid: str, tmpdir: str, max_frames: int = 6) -> list:
    """Sample frames evenly from a downloaded video file -> base64 JPEGs."""
    import base64
    import subprocess

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", vid],
            capture_output=True, text=True, timeout=30)
        duration = max(1.0, float(probe.stdout.strip()))
    except Exception:
        duration = 30.0
    frames = []
    for i in range(max_frames):
        t = duration * (i + 0.5) / max_frames
        fp = os.path.join(tmpdir, f"frame{i}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-ss", f"{t:.2f}", "-i", vid, "-frames:v", "1",
                 "-vf", "scale=640:-2", "-q:v", "5", "-y", fp],
                capture_output=True, timeout=30)
        except Exception:
            continue
        if os.path.exists(fp) and os.path.getsize(fp) > 0:
            with open(fp, "rb") as fh:
                frames.append(base64.b64encode(fh.read()).decode())
    return frames


def _frames_from_video(url: str, max_frames: int = 6) -> list:
    """Download the video (lowest usable quality) and sample frames.

    Returns a list of base64 JPEG strings, [] on any failure — vision is
    an enhancement, never a reason to kill a check.
    """
    import base64
    import subprocess

    import yt_dlp

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            outpath = os.path.join(tmpdir, "vid.%(ext)s")
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "worst[height>=240]/worst/bestvideo+bestaudio/best",
                "outtmpl": outpath,
                "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
                **({"proxy": _proxy_url()} if _proxy_url() else {}),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            vids = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
            if not vids:
                return []
            vid = max(vids, key=os.path.getsize)
            return _sample_frames(vid, tmpdir, max_frames)
    except Exception:
        return []


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


