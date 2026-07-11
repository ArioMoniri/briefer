"""Rich media extraction: X/Twitter tweets and video transcription.

Tweets: if TWITTER_BEARER_TOKEN is set we use the official X API v2 (reliable,
gives replied-to / quoted / retweeted originals + media). Otherwise we fall
back to the public syndication endpoint, then oEmbed — best-effort, no auth.

Video: captions first (cheap), else download audio with yt-dlp and transcribe
locally with faster-whisper. Everything degrades gracefully if a backend or
system dependency (ffmpeg) is missing — we return what we can and note it.

Security: all network egress for tweets goes through the SSRF guard; yt-dlp is
invoked via its Python API (no shell), the initial URL host is validated, and
downloads are bounded by size and duration.
"""
from __future__ import annotations

import logging
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .security import is_safe_url

log = logging.getLogger("briefer.media")

TWEET_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com|nitter\.[^/]+)/([^/]+)/status/(\d+)",
    re.IGNORECASE,
)
VIDEO_HOST_RE = re.compile(
    r"https?://(?:www\.)?("
    r"youtube\.com/watch|youtu\.be/|youtube\.com/shorts/|"
    r"vimeo\.com/|tiktok\.com/|dailymotion\.com/|"
    r"instagram\.com/(?:reel|p|tv)/|"
    r"facebook\.com/\S+/videos/|fb\.watch/|"
    r"linkedin\.com/(?:posts|feed/update)/|"
    r"twitter\.com/\S+/status/|x\.com/\S+/status/)",
    re.IGNORECASE,
)
_HEADERS = {"User-Agent": "BrieferBot/1.0"}


@dataclass
class TweetData:
    url: str
    author: str = ""
    text: str = ""
    created_at: str = ""
    is_reply: bool = False
    reply_to: "TweetData | None" = None
    quoted: "TweetData | None" = None
    retweet_of: "TweetData | None" = None
    photo_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"Tweet by @{self.author} ({self.created_at}):", self.text]
        if self.retweet_of:
            parts.append("↻ Reposted (retweet) of @" + self.retweet_of.author + ":")
            parts.append(self.retweet_of.text)
        if self.quoted:
            parts.append("❝ Quoted tweet by @" + self.quoted.author + ":")
            parts.append(self.quoted.text)
        if self.reply_to:
            parts.append("↳ In reply to @" + self.reply_to.author + ":")
            parts.append(self.reply_to.text)
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Tweets
# ---------------------------------------------------------------------------

class TweetExtractor:
    def __init__(self, bearer_token: str = "", consumer_key: str = "",
                 consumer_secret: str = "") -> None:
        self.bearer = bearer_token.strip()
        self.consumer_key = consumer_key.strip()
        self.consumer_secret = consumer_secret.strip()
        self._bearer_tried = False

    def _ensure_bearer(self) -> str:
        """Return a bearer token, minting one from the consumer key/secret
        (OAuth2 app-only client-credentials grant) if we weren't given one."""
        if self.bearer or self._bearer_tried:
            return self.bearer
        self._bearer_tried = True
        if not (self.consumer_key and self.consumer_secret):
            return ""
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(
                    "https://api.twitter.com/oauth2/token",
                    auth=(self.consumer_key, self.consumer_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded;charset=UTF-8"},
                )
            r.raise_for_status()
            self.bearer = r.json().get("access_token", "")
            if self.bearer:
                log.info("Minted X bearer token from consumer key/secret.")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not mint X bearer from consumer key/secret: %s", exc)
        return self.bearer

    def extract(self, url: str) -> TweetData | None:
        m = TWEET_RE.match(url)
        if not m:
            return None
        tweet_id = m.group(2)
        if self._ensure_bearer():
            try:
                data = self._via_api(tweet_id, url)
                if data:
                    return data
            except Exception as exc:  # noqa: BLE001
                # e.g. 403 on the Free tier (no read access) → fall back.
                log.warning("X API failed (%s); falling back to no-auth", exc)
        return self._via_syndication(tweet_id, url) or self._via_oembed(url)

    # --- official API v2 ---
    def _api_get(self, tweet_id: str) -> dict[str, Any]:
        params = {
            "ids": tweet_id,
            "tweet.fields": "text,created_at,referenced_tweets,author_id",
            "expansions": "referenced_tweets.id,attachments.media_keys,"
                          "author_id,referenced_tweets.id.author_id",
            "media.fields": "url,type,variants,preview_image_url",
            "user.fields": "username",
        }
        with httpx.Client(timeout=15, headers={
                "Authorization": f"Bearer {self.bearer}"}) as c:
            r = c.get("https://api.twitter.com/2/tweets", params=params)
            r.raise_for_status()
            return r.json()

    def _via_api(self, tweet_id: str, url: str) -> TweetData | None:
        raw = self._api_get(tweet_id)
        data = raw.get("data")
        if not data:
            return None
        tweet = data[0]
        inc = raw.get("includes", {})
        users = {u["id"]: u.get("username", "") for u in inc.get("users", [])}
        tweets_by_id = {t["id"]: t for t in inc.get("tweets", [])}
        media = {mm["media_key"]: mm for mm in inc.get("media", [])}

        td = TweetData(url=url, text=tweet.get("text", ""),
                       created_at=tweet.get("created_at", ""),
                       author=users.get(tweet.get("author_id"), ""))
        for mk in tweet.get("attachments", {}).get("media_keys", []):
            mm = media.get(mk, {})
            if mm.get("type") == "photo" and mm.get("url"):
                td.photo_urls.append(mm["url"])
            elif mm.get("type") in {"video", "animated_gif"}:
                best = _best_variant(mm.get("variants", []))
                if best:
                    td.video_urls.append(best)

        for ref in tweet.get("referenced_tweets", []):
            ref_t = tweets_by_id.get(ref.get("id"))
            if not ref_t:
                continue
            child = TweetData(
                url=f"https://x.com/i/status/{ref['id']}",
                text=ref_t.get("text", ""),
                author=users.get(ref_t.get("author_id"), ""))
            if ref["type"] == "replied_to":
                td.is_reply = True
                td.reply_to = child
            elif ref["type"] == "quoted":
                td.quoted = child
            elif ref["type"] == "retweeted":
                td.retweet_of = child
        return td

    # --- public syndication endpoint (no auth) ---
    def _via_syndication(self, tweet_id: str, url: str) -> TweetData | None:
        token = _syndication_token(tweet_id)
        api = ("https://cdn.syndication.twimg.com/tweet-result"
               f"?id={tweet_id}&token={token}&lang=en")
        ok, reason = is_safe_url(api)
        if not ok:
            return None
        try:
            with httpx.Client(timeout=15, headers=_HEADERS) as c:
                r = c.get(api)
            if r.status_code != 200:
                return None
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("syndication fetch failed: %s", exc)
            return None
        return _parse_syndication(j, url)

    # --- oEmbed (text only, last resort) ---
    def _via_oembed(self, url: str) -> TweetData | None:
        api = "https://publish.twitter.com/oembed?omit_script=1&url=" + url
        try:
            with httpx.Client(timeout=15, headers=_HEADERS,
                              follow_redirects=True) as c:
                r = c.get(api)
            if r.status_code != 200:
                return None
            html = r.json().get("html", "")
        except Exception:  # noqa: BLE001
            return None
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        m = re.search(r"—\s*(.+?)\s*\(@(\w+)\)", text)
        author = m.group(2) if m else ""
        return TweetData(url=url, author=author, text=text)


def _best_variant(variants: list[dict[str, Any]]) -> str | None:
    mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4:
        return None
    mp4.sort(key=lambda v: v.get("bit_rate", 0), reverse=True)
    return mp4[0]["url"]


def _syndication_token(tweet_id: str) -> str:
    # Mirrors react-tweet: ((id / 1e15) * pi).toString(36) with 0s and dots removed.
    v = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip = int(v)
    s = ""
    if ip == 0:
        s = "0"
    while ip > 0:
        s = digits[ip % 36] + s
        ip //= 36
    frac = v - int(v)
    if frac > 0:
        s += "."
        for _ in range(24):
            frac *= 36
            d = int(frac)
            s += digits[d]
            frac -= d
    return s.replace("0", "").replace(".", "")


def _parse_syndication(j: dict[str, Any], url: str) -> TweetData:
    def one(node: dict[str, Any], u: str) -> TweetData:
        user = node.get("user", {})
        td = TweetData(
            url=u, text=node.get("text", ""),
            author=user.get("screen_name", ""),
            created_at=node.get("created_at", ""))
        media = node.get("mediaDetails") or []
        for mm in media:
            if mm.get("type") == "photo" and mm.get("media_url_https"):
                td.photo_urls.append(mm["media_url_https"])
            elif mm.get("type") in {"video", "animated_gif"}:
                best = _best_variant([
                    {"content_type": v.get("type"), "url": v.get("src"),
                     "bit_rate": v.get("bitrate", 0)}
                    for v in mm.get("video_info", {}).get("variants", [])
                ])
                if best:
                    td.video_urls.append(best)
        return td

    td = one(j, url)
    if j.get("in_reply_to_screen_name"):
        td.is_reply = True
        parent = j.get("parent")
        if parent:
            td.reply_to = one(parent, url)
        else:
            td.reply_to = TweetData(url=url, author=j["in_reply_to_screen_name"],
                                    text="(parent tweet not available)")
    if j.get("quoted_tweet"):
        td.quoted = one(j["quoted_tweet"], url)
    if j.get("retweeted_status"):
        td.retweet_of = one(j["retweeted_status"], url)
    return td


# ---------------------------------------------------------------------------
# Video transcription
# ---------------------------------------------------------------------------

_WHISPER = None  # lazy singleton


def _get_whisper(model_name: str):
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel  # heavy import
        _WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _WHISPER


class VideoTranscriber:
    def __init__(self, enabled: bool, model: str, max_seconds: int,
                 max_bytes: int) -> None:
        self.enabled = enabled
        self.model = model
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes

    def _transcribe_path(self, path: str) -> str:
        model = _get_whisper(self.model)
        segments, _info = model.transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def transcribe_file(self, path: str) -> str:
        if not self.enabled:
            return "(transcription disabled)"
        try:
            return self._transcribe_path(path) or "(no speech detected)"
        except Exception as exc:  # noqa: BLE001
            log.warning("file transcription failed: %s", exc)
            return "(could not transcribe this media)"

    def transcribe_url(self, url: str) -> dict[str, Any]:
        """Return {title, uploader, duration, transcript, note}."""
        out: dict[str, Any] = {"title": "", "uploader": "", "duration": None,
                               "transcript": "", "note": ""}
        ok, reason = is_safe_url(url)
        if not ok:
            out["note"] = f"refused unsafe URL: {reason}"
            return out
        # 1) captions via youtube-transcript-api (YouTube only, cheap)
        yt = _youtube_id(url)
        if yt:
            cap = _youtube_captions(yt)
            if cap:
                out["transcript"] = cap
                out["note"] = "captions"
        if not self.enabled and not out["transcript"]:
            out["note"] = "transcription disabled; no captions"
            return out
        # 2) fetch metadata + (if needed) audio via yt-dlp, then whisper
        try:
            self._ytdlp(url, out)
        except Exception as exc:  # noqa: BLE001
            log.warning("yt-dlp failed for %s: %s", url, exc)
            if not out["transcript"]:
                out["note"] = "could not fetch/transcribe video"
        return out

    def _ytdlp(self, url: str, out: dict[str, Any]) -> None:
        import yt_dlp

        with tempfile.TemporaryDirectory() as tmp:
            opts = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": bool(out["transcript"]),  # only need meta if we have captions
                "format": "bestaudio/best",
                "max_filesize": self.max_bytes,
                "outtmpl": str(Path(tmp) / "a.%(ext)s"),
                "socket_timeout": 20,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=not out["transcript"])
            out["title"] = info.get("title", "") or out["title"]
            out["uploader"] = info.get("uploader", "") or out["uploader"]
            out["duration"] = info.get("duration")
            if out["transcript"]:
                return
            if out["duration"] and out["duration"] > self.max_seconds:
                out["note"] = (f"video too long to transcribe "
                               f"({out['duration']}s > {self.max_seconds}s)")
                return
            files = list(Path(tmp).glob("a.*"))
            if not files:
                out["note"] = "no audio downloaded"
                return
            out["transcript"] = self.transcribe_file(str(files[0]))
            out["note"] = "whisper"


def _youtube_id(url: str) -> str | None:
    m = re.search(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})", url)
    return m.group(1) if m else None


def _youtube_captions(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        chunks = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(c["text"] for c in chunks).strip()
    except Exception:  # noqa: BLE001
        return ""
