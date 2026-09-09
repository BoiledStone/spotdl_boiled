import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import sys
import struct
import time
import unicodedata
from contextlib import nullcontext, redirect_stderr, redirect_stdout, suppress
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from html import unescape
from urllib.parse import quote, urlparse
from textwrap import indent

import requests
import yt_dlp
from dotenv import load_dotenv

# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("BOT_ENV_FILE") or (PROJECT_DIR / ".env"))
DEFAULT_OUTPUT_DIR = Path(os.getenv("SPOTDL_OUTPUT_DIR") or (PROJECT_DIR / "downloads"))
DEFAULT_PLAYLIST_URL = os.getenv("SPOTDL_PLAYLIST_URL") or ""

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)


def default_ytdlp_cookies_browser():
    configured_browser = os.getenv("BOT_YTDLP_COOKIES_BROWSER")
    if configured_browser is not None:
        return configured_browser.strip().lower()

    appdata = os.getenv("APPDATA")
    if not appdata:
        return ""

    firefox_profiles = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
    with suppress(OSError):
        if firefox_profiles.is_dir() and any(firefox_profiles.iterdir()):
            return "firefox"
    return ""


YTDLP_COOKIES_BROWSER = default_ytdlp_cookies_browser()
OUTPUT_DIR = Path(os.getenv("SPOTDL_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
PLAYLIST_URL = os.getenv("SPOTDL_PLAYLIST_URL") or DEFAULT_PLAYLIST_URL

PRIMARY_SEARCH_SIZE = 3
FALLBACK_SEARCH_SIZE = 2
STRICT_FAST_ACCEPT_SCORE = 160
YOUTUBE_EARLY_ACCEPT_SCORE = 145
YOUTUBE_EARLY_ACCEPT_MARGIN = 16
STRICT_SLOW_SEARCH_SIZE = 3


def read_int_env(name, default, *, minimum=None, maximum=None):
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def read_float_env(name, default, *, minimum=None, maximum=None):
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


MIN_FALLBACK_ACCEPT_SCORE = read_int_env("SPOTDL_MIN_FALLBACK_SCORE", 130, minimum=0)
MAX_DOWNLOAD_CANDIDATE_ATTEMPTS = read_int_env(
    "SPOTDL_MAX_CANDIDATE_ATTEMPTS", 6, minimum=1, maximum=8
)
DEFAULT_CONCURRENT_TRACKS = read_int_env("SPOTDL_WORKERS", 4, minimum=1, maximum=5)

AUDIO_EXTENSIONS = {".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".webm"}
YOUTUBE_YTDL_PLAYER_CLIENTS = ("default", "web_safari")
YOUTUBE_YTDL_FALLBACK_CLIENTS = (("web_safari",),)
YOUTUBE_AUTH_ERROR_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you’re not a bot",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "not a bot",
    "cookies-from-browser or --cookies",
)


def detect_ytdlp_js_runtimes():
    """Return a JavaScript runtime that yt-dlp can actually execute."""
    configured = os.getenv("BOT_YTDLP_JS_RUNTIME", "").strip()
    if configured:
        if os.name == "nt" and len(configured) > 2 and configured[1] == ":":
            runtime_name, runtime_path = configured.split(":", 1)
        elif ":" in configured:
            runtime_name, runtime_path = configured.split(":", 1)
        else:
            runtime_name, runtime_path = configured, ""
        runtime_name = runtime_name.strip().lower()
        if runtime_name in {"deno", "node", "quickjs", "bun"}:
            return {runtime_name: {"path": runtime_path.strip() or None}}

    for executable, runtime_name in (
        ("deno", "deno"),
        ("node", "node"),
        ("qjs", "quickjs"),
        ("quickjs", "quickjs"),
        ("bun", "bun"),
    ):
        runtime_path = shutil.which(executable)
        if runtime_path:
            return {runtime_name: {"path": runtime_path}}
    return {}


YTDLP_JS_RUNTIMES = detect_ytdlp_js_runtimes()
try:
    from importlib import metadata as importlib_metadata
except ImportError:
    importlib_metadata = None

if importlib_metadata is None:
    YTDLP_EJS_PACKAGE_INSTALLED = False
else:
    try:
        importlib_metadata.version("yt-dlp-ejs")
    except importlib_metadata.PackageNotFoundError:
        YTDLP_EJS_PACKAGE_INSTALLED = False
    else:
        YTDLP_EJS_PACKAGE_INSTALLED = True
YTDLP_REMOTE_EJS = (
    os.getenv("BOT_YTDLP_REMOTE_EJS", "").strip().lower() in {"1", "true", "yes", "on"}
    or (not YTDLP_EJS_PACKAGE_INSTALLED and bool(YTDLP_JS_RUNTIMES))
)


class YouTubeAuthenticationRequired(RuntimeError):
    """Raised when YouTube refuses extraction without an authenticated session."""


def is_youtube_auth_error(error):
    error_text = str(error or "").lower()
    return any(marker in error_text for marker in YOUTUBE_AUTH_ERROR_MARKERS)


class YtdlpLogCollector:
    def __init__(self):
        self.authentication_error = None

    def _inspect(self, message):
        if self.authentication_error is None and is_youtube_auth_error(message):
            self.authentication_error = str(message)

    def debug(self, message):
        self._inspect(message)

    def warning(self, message):
        self._inspect(message)

    def error(self, message):
        self._inspect(message)
SPOTIFY_API_PAGE_SIZE = 100
SPOTIFY_API_MAX_RETRIES = 3
SPOTIFY_API_RETRY_BASE_SECONDS = 2
SPOTIFY_API_PAGE_DELAY_SECONDS = 1.0
SPOTIFY_API_MAX_RETRY_AFTER_SECONDS = read_int_env(
    "SPOTIFY_API_MAX_RETRY_AFTER_SECONDS", 30, minimum=0
)
SPOTIFY_WEB_TOKEN_URL = "https://open.spotify.com/api/token"
SPOTIFY_WEB_TOKEN_REFERER = "https://open.spotify.com/"
SPOTIFY_SERVER_TIME_URL = "https://open.spotify.com/"
SPOTIFY_PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v1/query"
SPOTIFY_TOTP_SECRETS_URL = os.getenv("SPOTIFY_TOTP_SECRETS_URL") or "https://github.com/xyloflake/spot-secrets-go/blob/main/secrets/secretDict.json?raw=true"
SPOTIFY_TOTP_TIMEOUT_SECONDS = read_int_env("SPOTIFY_TOTP_TIMEOUT_SECONDS", 10, minimum=1)
SPOTIFY_PATHFINDER_PAGE_SIZE = read_int_env("SPOTIFY_PATHFINDER_PAGE_SIZE", 200, minimum=1)
SPOTIFY_PATHFINDER_PAGE_DELAY_SECONDS = read_float_env(
    "SPOTIFY_PATHFINDER_PAGE_DELAY_SECONDS", 0.2, minimum=0
)
SPOTIFY_SP_DC = os.getenv("SPOTIFY_SP_DC") or ""
SPOTIFY_PLAYLIST_CACHE_DIRNAME = ".spotify_cache"
SPOTIFY_TOTP_SECRET_CIPHER_DICT = {
    59: [123, 105, 79, 70, 110, 59, 52, 125, 60, 49, 80, 70, 89, 75, 80, 86, 63, 53, 123, 37, 117, 49, 52, 93, 77, 62, 47, 86, 48, 104, 68, 72],
    60: [79, 109, 69, 123, 90, 65, 46, 74, 94, 34, 58, 48, 70, 71, 92, 85, 122, 63, 91, 64, 87, 87],
    61: [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78],
}

SPOTIFY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

SPOTIFY_PUBLIC_HEADER_VARIANTS = [
    {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    dict(SPOTIFY_HEADERS, **{"Accept-Language": "en-US,en;q=0.9"}),
]

ANSI_ENABLED = bool(sys.stdout.isatty() and os.getenv("NO_COLOR") is None)
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"
WORKER_CAPTURE_LOGS = True


def style(text, *codes):
    if not ANSI_ENABLED or not codes:
        return text
    return f"{''.join(codes)}{text}{ANSI_RESET}"


def fit_text(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    left = max(1, (width - 3) // 2)
    right = max(1, width - 3 - left)
    return f"{text[:left]}...{text[-right:]}"


def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        stream = kwargs.get("file", sys.stdout)
        separator = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = separator.join(str(arg) for arg in args) + end
        encoding = getattr(stream, "encoding", None) or "utf-8"
        encoded = text.encode(encoding, errors="replace")
        stream_buffer = getattr(stream, "buffer", None)
        if stream_buffer is not None:
            stream_buffer.write(encoded)
            stream_buffer.flush()
        else:
            stream.write(encoded.decode(encoding, errors="replace"))
            stream.flush()


def format_progress(completed, total, width=24):
    total = max(1, int(total))
    completed = max(0, min(int(completed), total))
    filled = int(round(width * (completed / total)))
    bar = "█" * filled + "░" * (width - filled)
    percent = int(round(100 * completed / total))
    return f"[{bar}] {completed}/{total} ({percent:>3}%)"


def print_banner(playlist_url, output_dir, worker_count, total_tracks=None):
    print(style("┌" + "─" * 66 + "┐", ANSI_CYAN))
    print(style(f"│ {fit_text('SPOTIFY -> YOUTUBE -> OPUS | Resolver multi-process', 64):<64} │", ANSI_CYAN))
    print(style("├" + "─" * 66 + "┤", ANSI_CYAN))
    print(style(f"│ Output   : {fit_text(output_dir, 52):<52} │", ANSI_CYAN))
    print(style(f"│ Playlist : {fit_text(playlist_url, 52):<52} │", ANSI_CYAN))
    workers_text = f"{worker_count} process{'es' if worker_count != 1 else ''}"
    print(style(f"│ Workers  : {fit_text(workers_text, 52):<52} │", ANSI_CYAN))
    if total_tracks is not None:
        print(style(f"│ Tracks   : {fit_text(total_tracks, 52):<52} │", ANSI_CYAN))
    print(style("└" + "─" * 66 + "┘", ANSI_CYAN))

# ============================================================
# TEXTE / MATCHING — repris de ton bot
# ============================================================

def clean_spotify_text(value):
    if not value:
        return None
    text = str(value).strip()
    text = re.sub(r"\s*\|\s*Spotify\s*$", "", text, flags=re.I)
    text = text.replace("â€“", "-").replace("Â·", "·")
    return text.strip() or None


def normalize_search_text(value):
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value).lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalized_token_set(value):
    return {token for token in normalize_search_text(value).split() if len(token) > 1}


def token_match_count(expected, actual):
    expected_tokens = normalized_token_set(expected)
    actual_tokens = normalized_token_set(actual)
    if not expected_tokens or not actual_tokens:
        return 0, len(expected_tokens)
    return len(expected_tokens & actual_tokens), len(expected_tokens)


def is_strong_token_match(expected, actual):
    matched, total = token_match_count(expected, actual)
    return total > 0 and matched == total


def has_any_token_match(expected, actual):
    matched, _ = token_match_count(expected, actual)
    return matched > 0


def simplify_track_title(value):
    text = clean_spotify_text(value) or ""
    text = re.sub(r"\s*\((feat|ft|with)[^)]+\)", "", text, flags=re.I)
    text = re.sub(r"\s*\[(feat|ft|with)[^\]]+\]", "", text, flags=re.I)
    text = re.sub(r"\s*-\s*(feat|ft|with)\s+.+$", "", text, flags=re.I)
    return text.strip() or value


def token_overlap_score(expected, haystack):
    expected_tokens = [token for token in normalize_search_text(expected).split() if len(token) > 2]
    if not expected_tokens:
        return 0, 0
    haystack_tokens = normalized_token_set(haystack)
    matched = sum(1 for token in expected_tokens if token in haystack_tokens)
    return matched, len(expected_tokens)


def build_candidate_blob(candidate):
    return " ".join([
        candidate.get("title") or "",
        candidate.get("track") or "",
        candidate.get("artist") or "",
        candidate.get("uploader") or "",
        candidate.get("channel") or "",
        candidate.get("description") or "",
        candidate.get("album") or "",
    ])


def is_fallback_candidate_acceptable(candidate, score, artist, title, duration):
    if not candidate or score is None:
        return False
    if score >= STRICT_FAST_ACCEPT_SCORE:
        return True

    # User-uploaded music often has no artist field. A full title match backed
    # by a close Spotify duration is safer than rejecting it for an unknown
    # uploader, while obvious alternate edits remain excluded.
    if has_reliable_title_duration_anchor(candidate, artist, title, duration):
        return True
    if score < MIN_FALLBACK_ACCEPT_SCORE:
        return False

    blob = build_candidate_blob(candidate)
    title_match, title_total = token_overlap_score(title, blob)
    artist_match, artist_total = token_overlap_score(artist, blob)
    title_threshold = max(1, title_total // 2) if title_total else 0
    artist_threshold = max(1, artist_total // 2) if artist_total else 0

    if title_total and title_match < title_threshold:
        return False
    if artist_total and artist_match < artist_threshold:
        return False

    candidate_duration = coerce_duration_seconds(candidate.get("duration"))
    duration_seconds = coerce_duration_seconds(duration)
    if isinstance(candidate_duration, int) and isinstance(duration_seconds, int):
        if abs(candidate_duration - duration_seconds) > 30 and score < STRICT_FAST_ACCEPT_SCORE:
            return False

    return True


def coerce_duration_seconds(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value <= 0:
            return None
        return int(round(numeric_value))
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        n = float(text)
        return int(round(n)) if n > 0 else None
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return None


def has_reliable_title_duration_anchor(candidate, artist, title, duration):
    candidate_title = normalize_search_text(" ".join([
        candidate.get("title") or "",
        candidate.get("track") or "",
    ]))
    title_match, title_total = token_overlap_score(title, candidate_title)
    if title_total < 2 or title_match < title_total:
        return False

    candidate_duration = coerce_duration_seconds(candidate.get("duration"))
    duration_seconds = coerce_duration_seconds(duration)
    if not isinstance(candidate_duration, int) or not isinstance(duration_seconds, int):
        return False
    if abs(candidate_duration - duration_seconds) > 10:
        return False

    expected_title = normalize_search_text(title)
    quality_terms = (
        "lyrics",
        "lyric video",
        "sped up",
        "slowed",
        "nightcore",
        "8d",
        "live",
        "cover",
        "karaoke",
        "instrumental",
        "muffled",
        "reverb",
        "remix",
        "loop",
        "full album",
        "playlist",
    )
    if any(term in candidate_title and term not in expected_title for term in quality_terms):
        return False

    explicit_artist = clean_spotify_text(candidate.get("artist"))
    return not explicit_artist or has_any_token_match(artist, explicit_artist)


def first_acceptable_ranked(ranked, artist, title, duration):
    for score, candidate in ranked:
        if is_fallback_candidate_acceptable(candidate, score, artist, title, duration):
            return score, candidate
    return None, None

# ============================================================
# SCORE YOUTUBE — repris de ton bot
# ============================================================

def score_youtube_candidate(entry, *, title=None, artist=None, duration=None, query_text=None, source=None):
    raw_entry_artist = " ".join([
        entry.get("artist") or "",
        entry.get("uploader") or "",
        entry.get("channel") or "",
    ])
    entry_title_text = normalize_search_text(" ".join([
        entry.get("title") or "",
        entry.get("track") or "",
    ]))
    entry_artist_text = normalize_search_text(raw_entry_artist)
    haystack = normalize_search_text(" ".join([
        entry.get("title") or "",
        entry.get("uploader") or "",
        entry.get("channel") or "",
        entry.get("description") or "",
        entry.get("track") or "",
        entry.get("artist") or "",
        entry.get("album") or "",
    ]))
    title_text = normalize_search_text(title)
    artist_text = normalize_search_text(artist)
    query_tokens = [token for token in normalize_search_text(query_text).split() if len(token) > 2]
    haystack_tokens = normalized_token_set(haystack)
    explicit_artist = clean_spotify_text(entry.get("artist"))
    explicit_channel = clean_spotify_text(entry.get("channel") or entry.get("uploader"))
    title_duration_anchor = has_reliable_title_duration_anchor(
        entry,
        artist,
        title,
        duration,
    )
    score = 0
    title_matches, title_total = token_match_count(title, haystack)
    artist_matches, artist_total = token_match_count(artist, haystack)
    title_matches_in_title, title_total_in_title = token_match_count(title, entry_title_text)
    artist_matches_in_artist, artist_total_in_artist = token_match_count(artist, entry_artist_text)

    if artist_text:
        artist_parts = [
            part.strip()
            for part in re.split(r",|&|\band\b|\bx\b", artist or "", flags=re.I)
            if part.strip()
        ]
        part_hits = sum(
            1 for part in artist_parts
            if is_strong_token_match(part, raw_entry_artist)
        )
        if part_hits:
            score += min(part_hits * 55, 165)

    if title_text:
        if title_text in haystack:
            score += 50
        if title_text == entry_title_text:
            score += 45
        elif entry_title_text.startswith(title_text):
            score += 25
        score += title_matches * 5
        score += title_matches_in_title * 8

    if artist_text:
        if artist_text in haystack:
            score += 40
        if artist_text in entry_artist_text:
            score += 20
        score += artist_matches * 4
        score += artist_matches_in_artist * 6
        if f"{artist_text} topic" in haystack or f"{artist_text} official" in haystack:
            score += 10
        if haystack.startswith(f"{artist_text} ") or haystack.startswith(f"{artist_text} topic"):
            score += 8
        if explicit_artist:
            if is_strong_token_match(artist, explicit_artist):
                score += 110
            elif has_any_token_match(artist, explicit_artist):
                score += 24
            else:
                score -= 130
        if explicit_channel:
            if is_strong_token_match(artist, explicit_channel):
                score += 60
            elif not has_any_token_match(artist, explicit_channel):
                score -= 10 if title_duration_anchor else 35

    if query_tokens:
        matched = sum(1 for token in query_tokens if token in haystack_tokens)
        score += matched * 3
        if matched == len(query_tokens) and matched > 0:
            score += 15

    bad_terms = [
        "lyrics", "lyric video", "sped up", "slowed", "nightcore", "8d", "live", "cover",
        "karaoke", "instrumental", "fan made", "edit audio", "reverb", "remix", "full album",
        "playlist", "1 hour", "10 hours",
    ]
    for term in bad_terms:
        if term in haystack:
            score -= 12

    good_terms = ["official audio", "audio", "topic", "provided to youtube by"]
    for term in good_terms:
        if term in haystack:
            score += 6

    if "provided to youtube by" in haystack:
        score += 35
    uploader = str(entry.get("uploader") or "")
    channel = str(entry.get("channel") or "")
    if uploader.endswith(" - Topic") or channel.endswith(" - Topic"):
        score += 20
    if title_text and artist_text:
        if normalize_search_text(entry.get("track")) == title_text:
            score += 25
        if normalize_search_text(entry.get("artist")) == artist_text:
            score += 25

    if source in {"spotify", "deezer"}:
        if title_duration_anchor:
            # A user upload can omit the artist while still being an exact
            # audio match. Keep it competitive instead of scoring its
            # uploader as a conflicting artist.
            score += 90
        else:
            if artist_total and artist_matches == 0:
                score -= 120
            elif artist_total and artist_matches < max(1, artist_total // 2):
                score -= 50
            if artist_total_in_artist and artist_matches_in_artist == 0:
                score -= 90
            elif artist_total_in_artist and artist_matches_in_artist < max(1, artist_total_in_artist // 2):
                score -= 35
        if title_total and title_matches == 0:
            score -= 90
        elif title_total and title_matches < max(1, title_total // 2):
            score -= 35
        if title_total_in_title and title_matches_in_title == 0:
            score -= 130
        elif title_total_in_title and title_matches_in_title < max(1, title_total_in_title // 2):
            score -= 55
        if "official video" in haystack and "audio" not in haystack and "topic" not in haystack:
            score -= 10
        if "remix" in entry_title_text or "sped up" in entry_title_text or "slowed" in entry_title_text:
            score -= 80
        if "live" in entry_title_text or "karaoke" in entry_title_text or "cover" in entry_title_text:
            score -= 90
        if not title_duration_anchor:
            if explicit_artist and not is_strong_token_match(artist, explicit_artist):
                score -= 90
            if explicit_channel and not has_any_token_match(artist, explicit_channel):
                score -= 45

    entry_duration = entry.get("duration")
    duration = coerce_duration_seconds(duration)
    entry_duration = coerce_duration_seconds(entry_duration)
    if isinstance(entry_duration, (int, float)) and isinstance(duration, (int, float)):
        delta = abs(int(entry_duration) - int(duration))
        if delta <= 3:
            score += 20
        elif delta <= 10:
            score += 10
        elif delta >= 45:
            score -= 45
        elif delta >= 20:
            score -= 20
        if source in {"spotify", "deezer"}:
            if delta >= 30:
                score -= 70
            elif delta >= 15:
                score -= 35
            elif delta >= 8:
                score -= 12
    return score

# ============================================================
# SPOTIFY HTML
# ============================================================

def normalize_spotify_target(url_or_id):
    raw = str(url_or_id or "").strip()
    if not raw:
        return None, None

    if raw.startswith("spotify:"):
        parts = raw.split(":")
        if len(parts) >= 3 and parts[1] in {"track", "album", "playlist"}:
            return parts[1], parts[2]
        return None, None

    if re.fullmatch(r"[A-Za-z0-9]{22}", raw):
        return "playlist", raw

    if re.match(r"^(?:open\.)?spotify\.com/", raw, flags=re.I) or re.match(r"^spotify\.link/", raw, flags=re.I):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if not segments:
        return None, None

    if segments[0].startswith("intl-") and len(segments) >= 3:
        segments = segments[1:]

    if len(segments) >= 2 and segments[0] in {"track", "album", "playlist"}:
        return segments[0], segments[1]

    if len(segments) >= 3 and segments[0] == "embed" and segments[1] in {"track", "album", "playlist"}:
        return segments[1], segments[2]

    if len(segments) >= 4 and segments[0] == "user" and segments[2] == "playlist":
        return "playlist", segments[3]

    return None, None


def is_spotify_shortlink_url(url):
    raw = str(url or "").strip()
    if not raw:
        return False
    if re.match(r"^spotify\.link/", raw, flags=re.I):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    return host == "spotify.link" or host.endswith(".spotify.link")


def resolve_spotify_shortlink(url):
    if not is_spotify_shortlink_url(url):
        return url

    raw = str(url or "").strip()
    if re.match(r"^spotify\.link/", raw, flags=re.I):
        raw = f"https://{raw}"

    response = requests.get(raw, headers=SPOTIFY_HEADERS, timeout=12, allow_redirects=True)
    response.raise_for_status()
    return response.url or raw


def sanitize_spotify_input(value):
    raw = clean_spotify_text(value)
    if not raw:
        return raw

    raw = raw.strip().strip("`\"'")

    markdown_link = re.match(r"^\[(?P<label>.+?)\]\((?P<target>.+?)\)$", raw)
    if markdown_link:
        target = (clean_spotify_text(markdown_link.group("target")) or "").strip("`\"'")
        label = (clean_spotify_text(markdown_link.group("label")) or "").strip("`\"'")
        for candidate in (target, label):
            if re.search(r"https?://", candidate, flags=re.I):
                return candidate
        return target or label

    match = re.search(r"https?://[^\s)\]]+", raw, flags=re.I)
    if match:
        return match.group(0).rstrip(").,;]")

    return raw


def spotify_public_url(kind, item_id):
    return f"https://open.spotify.com/{kind}/{item_id}"


def spotify_oembed_url(kind, item_id):
    return f"https://open.spotify.com/oembed?url={quote(spotify_public_url(kind, item_id), safe='')}"


def spotify_embed_url(kind, item_id):
    return f"https://open.spotify.com/embed/{kind}/{item_id}?utm_source=oembed"


def spotify_headers():
    return SPOTIFY_HEADERS


def fetch_text(url, *, headers=None, timeout=20):
    response = requests.get(url, headers=headers or spotify_headers(), timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_json(url, *, headers=None, timeout=20):
    response = requests.get(url, headers=headers or spotify_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def spotify_json_headers(access_token):
    headers = dict(SPOTIFY_HEADERS)
    headers["Accept"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def spotify_playlist_cache_path(item_id):
    return OUTPUT_DIR / SPOTIFY_PLAYLIST_CACHE_DIRNAME / f"playlist_{item_id}.json"


def atomic_write_text(path, content):
    path = Path(path)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(path)
    except Exception:
        with suppress(OSError):
            temporary_path.unlink()
        raise


def load_spotify_playlist_cache(item_id, *, minimum_count=None):
    cache_path = spotify_playlist_cache_path(item_id)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if isinstance(tracks, list) and tracks:
        if minimum_count is not None and len(tracks) < minimum_count:
            return None
        return tracks
    return None


def save_spotify_playlist_cache(item_id, tracks):
    cache_path = spotify_playlist_cache_path(item_id)
    try:
        atomic_write_text(
            cache_path,
            json.dumps(
                {
                    "tracks": tracks,
                    "count": len(tracks),
                    "saved_at": int(time.time()),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception:
        pass


def fetch_spotify_client_credentials_token():
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    return token if isinstance(token, str) and token else None


def load_spotify_totp_secret_cipher_dict():
    try:
        response = requests.get(
            SPOTIFY_TOTP_SECRETS_URL,
            headers={**SPOTIFY_HEADERS, "Accept": "application/json"},
            timeout=SPOTIFY_TOTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        payload = None

    secrets = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            try:
                version = int(key)
            except Exception:
                continue
            if isinstance(value, list) and value and all(isinstance(item, int) for item in value):
                secrets[version] = [int(item) for item in value]

    if secrets:
        return secrets

    return dict(SPOTIFY_TOTP_SECRET_CIPHER_DICT)


def extract_spotify_browser_cookie(cookie_name):
    cookie_value = SPOTIFY_SP_DC if cookie_name == "sp_dc" and SPOTIFY_SP_DC else None
    if cookie_value:
        return cookie_value

    try:
        cookie_jar = yt_dlp.cookies.extract_cookies_from_browser(YTDLP_COOKIES_BROWSER)
    except Exception:
        return None

    for cookie in cookie_jar:
        if getattr(cookie, "name", None) != cookie_name:
            continue
        value = getattr(cookie, "value", None)
        if isinstance(value, str) and value:
            return value
    return None


def spotify_server_timestamp():
    for method in ("head", "get"):
        try:
            response = requests.request(
                method.upper(),
                SPOTIFY_SERVER_TIME_URL,
                headers=SPOTIFY_HEADERS,
                timeout=SPOTIFY_TOTP_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            date_header = response.headers.get("Date")
            if not date_header:
                continue
            server_time = parsedate_to_datetime(date_header)
            if server_time.tzinfo is None:
                server_time = server_time.replace(tzinfo=timezone.utc)
            return int(server_time.timestamp())
        except Exception:
            continue
    raise RuntimeError("Impossible de lire l'heure serveur Spotify.")


def spotify_hotp(secret_key, counter, *, digits=6):
    digest = hmac.new(secret_key, struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % (10 ** digits):0{digits}d}"


def spotify_totp_from_cipher(cipher_bytes, server_timestamp, *, interval=30, digits=6):
    cipher_bytes = bytes(cipher_bytes)
    transformed = "".join(str(byte ^ ((index % 33) + 9)) for index, byte in enumerate(cipher_bytes))
    secret_key = transformed.encode("utf-8")
    counter = int(server_timestamp // interval)
    return spotify_hotp(secret_key, counter, digits=digits)


def fetch_spotify_web_access_token():
    sp_dc = extract_spotify_browser_cookie("sp_dc")
    if not sp_dc:
        return None

    server_timestamp = spotify_server_timestamp()
    secret_map = load_spotify_totp_secret_cipher_dict()
    if not secret_map:
        return None

    headers = dict(SPOTIFY_HEADERS)
    headers.update(
        {
            "Accept": "application/json",
            "Referer": SPOTIFY_WEB_TOKEN_REFERER,
            "App-Platform": "WebPlayer",
            "Origin": SPOTIFY_WEB_TOKEN_REFERER,
            "Cookie": f"sp_dc={sp_dc}",
        }
    )

    for version in sorted(secret_map, reverse=True):
        cipher_bytes = secret_map.get(version)
        if not cipher_bytes:
            continue

        totp = spotify_totp_from_cipher(cipher_bytes, server_timestamp)
        try:
            response = requests.get(
                SPOTIFY_WEB_TOKEN_URL,
                headers=headers,
                params={
                    "reason": "transport",
                    "productType": "web-player",
                    "totp": totp,
                    "totpServer": server_timestamp,
                    "totpVer": version,
                },
                timeout=20,
            )
        except Exception:
            continue

        if response.status_code == 200:
            payload = response.json()
            token = payload.get("accessToken") or payload.get("access_token")
            if isinstance(token, str) and token:
                return token
            continue

        if response.status_code in {400, 401}:
            continue

        raise RuntimeError(
            f"Spotify web-token HTTP {response.status_code} : "
            f"{(clean_spotify_text(response.text) or '')[:160] or 'réponse invalide'}"
        )

    return None


def spotify_graphql_hash_cache_path():
    return OUTPUT_DIR / SPOTIFY_PLAYLIST_CACHE_DIRNAME / "graphql_hashes.json"


def load_spotify_graphql_hash_cache():
    cache_path = spotify_graphql_hash_cache_path()
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_spotify_graphql_hash_cache(cache):
    try:
        cache_path = spotify_graphql_hash_cache_path()
        atomic_write_text(cache_path, json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception:
        pass


def invalidate_spotify_graphql_hash_cache(operation):
    cache = load_spotify_graphql_hash_cache()
    if operation in cache:
        cache.pop(operation, None)
        save_spotify_graphql_hash_cache(cache)


def pick_spotify_web_player_bundle(html_text):
    if not html_text:
        return None
    for match in re.finditer(r'<script[^>]+src=["\']([^"\']+\.js)["\']', html_text, flags=re.I):
        src = match.group(1)
        if "/web-player/" not in src and "/mobile-web-player/" not in src:
            continue
        if src.startswith("//"):
            return f"https:{src}"
        if src.startswith("/"):
            return f"https://open.spotify.com{src}"
        return src
    return None


def extract_spotify_graphql_operation_hash(js_text, operation):
    if not js_text or not operation:
        return None

    escaped = re.escape(operation)
    patterns = [
        rf"{escaped}.{{0,500}}?sha256Hash\\?\":\\?\"([a-f0-9]{{64}})\\?\"",
        rf'"{escaped}","(?:query|mutation)","([a-f0-9]{{64}})"',
    ]
    for pattern in patterns:
        match = re.search(pattern, js_text, flags=re.S)
        if match:
            return match.group(1)
    return None


def fetch_spotify_graphql_operation_hash(operation):
    cache = load_spotify_graphql_hash_cache()
    cached = cache.get(operation)
    if isinstance(cached, str) and re.fullmatch(r"[a-f0-9]{64}", cached):
        return cached

    html_text = fetch_text("https://open.spotify.com/", timeout=25)
    bundle_url = pick_spotify_web_player_bundle(html_text)
    if not bundle_url:
        raise RuntimeError("bundle web-player Spotify introuvable.")

    js_text = fetch_text(bundle_url, timeout=30)
    operation_hash = extract_spotify_graphql_operation_hash(js_text, operation)
    if not operation_hash:
        raise RuntimeError(f"hash GraphQL Spotify introuvable pour {operation}.")

    cache[operation] = operation_hash
    save_spotify_graphql_hash_cache(cache)
    return operation_hash


def nested_dict(value, *path):
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def nested_number(value, *path):
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, bool) or current is None:
        return None
    if isinstance(current, (int, float)):
        return current
    try:
        return float(current)
    except Exception:
        return None


def spotify_id_from_uri(uri):
    if not isinstance(uri, str):
        return None
    parts = uri.split(":")
    if len(parts) >= 3:
        return parts[-1]
    return None


def spotify_artist_name_from_value(value):
    if not isinstance(value, dict):
        return None

    profile = value.get("profile")
    if isinstance(profile, dict) and profile.get("name"):
        return clean_spotify_text(profile.get("name"))

    identity = value.get("identityTrait")
    if isinstance(identity, dict):
        contributors = identity.get("contributors")
        if isinstance(contributors, dict):
            items = contributors.get("items")
            if isinstance(items, list):
                names = [clean_spotify_text(item.get("name")) for item in items if isinstance(item, dict) and item.get("name")]
                if names:
                    return ", ".join(name for name in names if name)

    for key in ("node", "artist", "data"):
        child = value.get(key)
        name = spotify_artist_name_from_value(child)
        if name:
            return name

    if value.get("name"):
        return clean_spotify_text(value.get("name"))
    return None


def spotify_artist_names_from_container(container):
    names = []
    if isinstance(container, list):
        iterable = container
    elif isinstance(container, dict):
        iterable = None
        for key in ("items", "nodes", "edges"):
            if isinstance(container.get(key), list):
                iterable = container.get(key)
                break
        if iterable is None:
            name = spotify_artist_name_from_value(container)
            return [name] if name else []
    else:
        return []

    for entry in iterable:
        name = spotify_artist_name_from_value(entry)
        if name and name not in names:
            names.append(name)
    return names


def decode_spotify_next_data(text):
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]+?)</script>', text, flags=re.I)
    if not match:
        return None
    try:
        return json.loads(unescape(match.group(1)))
    except Exception:
        return None


def decode_spotify_state(text):
    patterns = [
        r'<script[^>]+id=["\']initial-state["\'][^>]*>([\s\S]+?)</script>',
        r"Spotify\\.Entity\s*=\s*([\s\S]+?);",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            raw = unescape(match.group(1).strip())
            for candidate in (raw, raw.replace("&quot;", '"').replace("&amp;", "&")):
                try:
                    return json.loads(candidate)
                except Exception:
                    pass
                try:
                    import base64
                    return json.loads(base64.b64decode(candidate).decode("utf-8"))
                except Exception:
                    pass
    return None


def extract_spotify_access_token(next_data):
    if not isinstance(next_data, dict):
        return None

    state = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
    )
    if isinstance(state, dict):
        session = (
            state.get("settings", {})
            .get("session", {})
        )
        token = session.get("accessToken") if isinstance(session, dict) else None
        if isinstance(token, str) and token:
            return token

    for node in walk_json(next_data):
        if isinstance(node, dict):
            token = node.get("accessToken")
            if isinstance(token, str) and token.startswith("BQ"):
                return token
    return None


def extract_spotify_meta(text):
    if not text:
        return {}
    meta = {}
    patterns = {
        "og_title": r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        "og_description": r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
        "twitter_title": r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
        "twitter_description": r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)',
        "meta_description": r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
        "music_musician": r'<meta[^>]+name=["\']music:musician["\'][^>]+content=["\']([^"\']+)',
        "music_song_count": r'<meta[^>]+name=["\']music:song_count["\'][^>]+content=["\']([^"\']+)',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.I)
        if match:
            meta[key] = unescape(match.group(1).strip())

    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>', text, flags=re.I):
        try:
            payload = json.loads(unescape(block.strip()))
        except Exception:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if "ld_title" not in meta and node.get("name"):
                meta["ld_title"] = str(node["name"]).strip()
            artist_value = node.get("byArtist") or node.get("artist")
            if isinstance(artist_value, dict) and artist_value.get("name"):
                meta.setdefault("ld_artist", str(artist_value["name"]).strip())
            elif isinstance(artist_value, list) and artist_value and isinstance(artist_value[0], dict) and artist_value[0].get("name"):
                meta.setdefault("ld_artist", str(artist_value[0]["name"]).strip())
            elif isinstance(artist_value, str):
                meta.setdefault("ld_artist", artist_value.strip())
    return meta


def parse_spotify_count(value):
    text = clean_spotify_text(value)
    if not text:
        return None

    match = re.search(r"\b(?P<count>\d[\d\s.,]*)\s+(?:items?|tracks?|songs?|titres?|morceaux)\b", text, flags=re.I)
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group("count"))
    if not digits:
        return None

    try:
        return int(digits)
    except Exception:
        return None


def extract_spotify_declared_count(text, next_data=None):
    candidates = []

    if text:
        meta = extract_spotify_meta(text)
        candidates.extend(value for value in meta.values() if value)
        candidates.extend(re.findall(r'"description"\s*:\s*"([^"]+)"', text, flags=re.I))
        candidates.extend(re.findall(r'<title[^>]*>([\s\S]+?)</title>', text, flags=re.I))

    if isinstance(next_data, dict):
        for node in walk_json(next_data):
            if not isinstance(node, dict):
                continue
            for key in ("totalCount", "trackCount", "itemCount"):
                value = node.get(key)
                if isinstance(value, int) and value > 0:
                    candidates.append(str(value) + " items")
            for key in ("subtitle", "description"):
                value = node.get(key)
                if isinstance(value, str):
                    candidates.append(value)

    counts = [count for count in (parse_spotify_count(value) for value in candidates) if count]
    return max(counts) if counts else None


def reject_partial_spotify_playlist(item_id, tracks, *, declared_count=None):
    if not tracks:
        return

    if declared_count is None:
        try:
            public_html, _ = fetch_spotify_playlist_html(item_id)
            declared_count = extract_spotify_declared_count(public_html)
        except Exception:
            declared_count = None

    if declared_count and len(tracks) < declared_count:
        cached_tracks = load_spotify_playlist_cache(item_id, minimum_count=declared_count)
        if cached_tracks:
            return cached_tracks

        raise RuntimeError(
            f"Spotify annonce {declared_count} pistes, mais le HTML public n'en expose que {len(tracks)}. "
            "L'API Spotify est temporairement limitée; relance le script dans quelques minutes."
        )

    return None


def parse_artist_and_title(text):
    cleaned = clean_spotify_text(text)
    if not cleaned:
        return None, None

    patterns = [
        r"^(?P<title>.+?)\s*-\s*song and lyrics by\s+(?P<artist>.+)$",
        r"^(?P<title>.+?)\s*-\s*song by\s+(?P<artist>.+)$",
        r"^(?P<title>.+?)\s*[·|]\s*song by\s+(?P<artist>.+)$",
        r"^(?P<title>.+?)\s*[·|]\s*(?P<artist>.+)$",
        r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.I)
        if match:
            return clean_spotify_text(match.group("artist")), clean_spotify_text(match.group("title"))

    match = re.search(r"(?:song by|by)\s+(?P<artist>.+)$", cleaned, flags=re.I)
    if match:
        return clean_spotify_text(match.group("artist")), None

    return None, cleaned


def spotify_track_from_meta(title, artist, thumbnail=None, duration=None, color=None):
    clean_title = clean_spotify_text(title) or "Titre inconnu"
    clean_artist = clean_spotify_text(artist) or "Artiste inconnu"
    return {
        "query": f'"{clean_artist}" "{clean_title}" audio',
        "title": clean_title,
        "artist": clean_artist,
        "thumbnail": thumbnail,
        "duration": duration,
        "color": color,
        "source": "spotify",
    }


def spotify_track_from_embed_item(item, *, fallback_thumbnail=None):
    title = clean_spotify_text(item.get("title") or item.get("name")) or "Titre inconnu"

    artist = None
    artists = item.get("artists")
    if isinstance(artists, list) and artists and isinstance(artists[0], dict):
        artist = clean_spotify_text(artists[0].get("name"))
    if not artist:
        artist = clean_spotify_text(item.get("subtitle"))

    duration = item.get("duration")
    if isinstance(duration, (int, float)):
        duration_value = int(duration // 1000) if duration > 1000 else int(duration)
        if item.get("audioPreview") and item.get("artists") and duration_value <= 60:
            duration_value = None
    else:
        duration_value = None

    return spotify_track_from_meta(
        title,
        artist,
        thumbnail=spotify_thumbnail_from_entity(item, fallback=fallback_thumbnail),
        duration=duration_value,
    )


def spotify_color_from_entity(entity):
    visual = entity.get("visualIdentity")
    if not isinstance(visual, dict):
        return None
    base = visual.get("backgroundBase")
    if not isinstance(base, dict):
        return None
    red = base.get("red")
    green = base.get("green")
    blue = base.get("blue")
    if all(isinstance(channel, int) for channel in (red, green, blue)):
        return (red << 16) + (green << 8) + blue
    return None


def spotify_thumbnail_from_entity(entity, fallback=None):
    cover_art = entity.get("coverArt")
    if isinstance(cover_art, dict):
        sources = cover_art.get("sources")
        if isinstance(sources, list):
            for source in reversed(sources):
                if isinstance(source, dict) and source.get("url"):
                    return source["url"]

    for nested_key in ("album", "albumOfTrack"):
        nested = entity.get(nested_key) if isinstance(entity, dict) else None
        if isinstance(nested, dict):
            candidate = spotify_thumbnail_from_entity(nested, fallback=None)
            if candidate:
                return candidate

    visual = entity.get("visualIdentity")
    if isinstance(visual, dict):
        images = visual.get("image")
        if isinstance(images, list):
            for image in reversed(images):
                if isinstance(image, dict) and image.get("url"):
                    return image["url"]

    visual_trait = entity.get("visualIdentityTrait") if isinstance(entity, dict) else None
    if isinstance(visual_trait, dict):
        for key in ("squareCoverImage", "sixteenByNineCoverImage"):
            image = visual_trait.get(key)
            if not isinstance(image, dict):
                continue
            sources = image.get("sources")
            if isinstance(sources, list):
                for source in reversed(sources):
                    if isinstance(source, dict) and source.get("url"):
                        return source["url"]

    if isinstance(entity, dict):
        direct_url = entity.get("thumbnail") or entity.get("image")
        if isinstance(direct_url, str) and direct_url:
            return direct_url
        images = entity.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and image.get("url"):
                    return image["url"]

    return fallback


def find_entity(state, kind, item_id):
    target = f"spotify:{kind}:{item_id}"
    for node in walk_json(state):
        if not isinstance(node, dict):
            continue
        if node.get("uri") == target or node.get("entityUri") == target or node.get("spotifyUri") == target:
            return node
        if node.get("id") == item_id and node.get("type") == kind:
            return node
        if target in node and isinstance(node[target], dict):
            return node[target]
    return None


def artist_name_from_item(item):
    artists = item.get("artists")
    if isinstance(artists, list):
        names = []
        for a in artists:
            if not isinstance(a, dict):
                continue
            name = clean_spotify_text(a.get("name"))
            if not name:
                name = spotify_artist_name_from_value(a)
            if name:
                names.append(name)
        if names:
            return ", ".join(names)
    if isinstance(artists, dict):
        names = spotify_artist_names_from_container(artists)
        if names:
            return ", ".join(names)
    for key in ("firstArtist", "otherArtists"):
        names = spotify_artist_names_from_container(item.get(key))
        if names:
            return ", ".join(names)
    name = spotify_artist_name_from_value(item)
    if name:
        return name
    return clean_spotify_text(item.get("subtitle") or item.get("artist"))


def normalize_spotify_track(item):
    identity = item.get("identityTrait") if isinstance(item.get("identityTrait"), dict) else {}
    title = clean_spotify_text(item.get("title") or item.get("name") or identity.get("name"))
    artist = artist_name_from_item(item)
    duration_raw = item.get("duration")
    duration = None
    if isinstance(duration_raw, (int, float)):
        duration = int(duration_raw // 1000) if duration_raw > 1000 else int(duration_raw)
        if item.get("audioPreview") and item.get("artists") and duration <= 60:
            duration = None
    elif item.get("duration_ms") is not None:
        try:
            duration = int(round(float(item["duration_ms"]) / 1000))
        except Exception:
            duration = None
    elif item.get("durationMs") is not None:
        try:
            duration = int(round(float(item["durationMs"]) / 1000))
        except Exception:
            duration = None
    else:
        milliseconds = (
            nested_number(item, "duration", "totalMilliseconds")
            or nested_number(item, "trackDuration", "totalMilliseconds")
        )
        if milliseconds:
            duration = int(round(float(milliseconds) / 1000))
        else:
            seconds = nested_number(item, "consumptionExperienceTrait", "duration", "seconds")
            if seconds:
                duration = int(round(float(seconds)))
    title = title or "Titre inconnu"
    artist = artist or "Artiste inconnu"
    track_id = item.get("id") or spotify_id_from_uri(item.get("uri"))
    normalized = {
        "query": f'"{artist}" "{title}" audio',
        "title": title,
        "artist": artist,
        "duration": duration,
        "spotify_id": track_id,
        "source": "spotify",
    }
    thumbnail = spotify_thumbnail_from_entity(item)
    if thumbnail:
        normalized["thumbnail"] = thumbnail
    return normalized


def spotify_api_thumbnail(track):
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    images = album.get("images") if isinstance(album.get("images"), list) else []
    for image in images:
        if isinstance(image, dict) and image.get("url"):
            return image["url"]
    return None


def normalize_spotify_api_item(item):
    if not isinstance(item, dict) or item.get("is_local"):
        return None

    track = item.get("track") if isinstance(item.get("track"), dict) else item
    if not isinstance(track, dict):
        return None

    track_type = (clean_spotify_text(track.get("type")) or "").lower()
    if track_type and track_type != "track":
        return None

    if not (track.get("name") or track.get("title")):
        return None

    normalized = normalize_spotify_track(track)
    thumbnail = spotify_api_thumbnail(track)
    if thumbnail:
        normalized["thumbnail"] = thumbnail
    return normalized


def normalize_spotify_pathfinder_item(item):
    if not isinstance(item, dict):
        return None

    if item.get("isLocal") or item.get("is_local"):
        return None

    candidate = item.get("itemV2")
    if not isinstance(candidate, dict):
        candidate = item.get("itemV3") if isinstance(item.get("itemV3"), dict) else None
    if not isinstance(candidate, dict):
        candidate = item.get("track") if isinstance(item.get("track"), dict) else None
    if not isinstance(candidate, dict):
        candidate = item

    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    if not isinstance(data, dict):
        return None

    if data.get("__typename") in {"NotFound", "Error"}:
        return None

    normalized = normalize_spotify_track(data)
    uri = data.get("uri") or item.get("uri")
    if uri and not normalized.get("spotify_id"):
        normalized["spotify_id"] = spotify_id_from_uri(uri)

    thumbnail = spotify_thumbnail_from_entity(data)
    if thumbnail:
        normalized["thumbnail"] = thumbnail

    return normalized


def fetch_spotify_playlist_tracks_pathfinder(playlist_id, access_token):
    if not access_token:
        return []

    operation_hash = fetch_spotify_graphql_operation_hash("fetchPlaylist")
    tracks = []
    offset = 0
    total = None
    seen_uris = set()
    refreshed_hash = False

    headers = dict(SPOTIFY_HEADERS)
    headers.update(
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "app-platform": "WebPlayer",
            "Origin": SPOTIFY_WEB_TOKEN_REFERER,
            "Referer": SPOTIFY_WEB_TOKEN_REFERER,
        }
    )

    while True:
        variables = {
            "uri": f"spotify:playlist:{playlist_id}",
            "offset": offset,
            "limit": SPOTIFY_PATHFINDER_PAGE_SIZE,
            "enableWatchFeedEntrypoint": False,
        }
        params = {
            "operationName": "fetchPlaylist",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": operation_hash,
                    }
                },
                separators=(",", ":"),
            ),
        }

        response = None
        for attempt in range(SPOTIFY_API_MAX_RETRIES):
            response = requests.post(
                SPOTIFY_PATHFINDER_URL,
                headers=headers,
                params=params,
                timeout=25,
            )

            if response.status_code != 429:
                break

            retry_after = response.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after else min(60, SPOTIFY_API_RETRY_BASE_SECONDS * (attempt + 1) * 2)
            except Exception:
                delay = min(60, SPOTIFY_API_RETRY_BASE_SECONDS * (attempt + 1) * 2)
            if delay > SPOTIFY_API_MAX_RETRY_AFTER_SECONDS:
                raise RuntimeError(
                    f"Spotify Pathfinder rate-limit trop long ({delay}s). "
                    "Le chemin API est abandonné pour éviter une attente excessive."
                )
            delay = max(3, delay)
            print(f"    Spotify Pathfinder rate-limit : pause {delay}s")
            time.sleep(delay)

        if response is None:
            break

        if response.status_code in {400, 404} and not refreshed_hash:
            body = response.text.lower()
            if "persistedquery" in body or "hash" in body or "not found" in body:
                invalidate_spotify_graphql_hash_cache("fetchPlaylist")
                operation_hash = fetch_spotify_graphql_operation_hash("fetchPlaylist")
                refreshed_hash = True
                continue

        if response.status_code == 429:
            raise RuntimeError("Spotify Pathfinder rate-limit persistant.")

        response.raise_for_status()
        payload = response.json()
        content = (
            payload.get("data", {})
            .get("playlistV2", {})
            .get("content", {})
        )
        if not isinstance(content, dict):
            raise RuntimeError("Réponse Spotify Pathfinder invalide.")

        items = content.get("items") or []
        total = content.get("totalCount") if isinstance(content.get("totalCount"), int) else total

        page_count = 0
        for raw_item in items:
            track = normalize_spotify_pathfinder_item(raw_item)
            if not track:
                continue
            uri = track.get("spotify_id")
            if isinstance(uri, str) and uri in seen_uris:
                continue
            if isinstance(uri, str):
                seen_uris.add(uri)
            tracks.append(track)
            page_count += 1

        offset += len(items)
        if total is not None:
            print(f"    Spotify Pathfinder : {min(offset, total)}/{total} pistes")
        if not items:
            break
        if total is not None:
            if offset >= total:
                break
        elif len(items) < SPOTIFY_PATHFINDER_PAGE_SIZE:
            break
        if page_count == 0 and total is None:
            break
        time.sleep(SPOTIFY_PATHFINDER_PAGE_DELAY_SECONDS)

    if total is not None and offset < total:
        raise RuntimeError(f"Spotify Pathfinder a renvoyé une playlist partielle ({offset}/{total}).")

    return tracks


def fetch_spotify_playlist_tracks_api(playlist_id, access_token):
    if not access_token:
        return []

    tracks = []
    offset = 0
    limit = SPOTIFY_API_PAGE_SIZE
    total = None
    fields = (
        "total,limit,offset,next,"
        "items(is_local,track(id,name,type,duration_ms,artists(name),album(images(url))))"
    )

    while True:
        response = None
        for attempt in range(SPOTIFY_API_MAX_RETRIES):
            response = requests.get(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers=spotify_json_headers(access_token),
                params={"limit": limit, "offset": offset, "fields": fields},
                timeout=25,
            )

            if response.status_code != 429:
                break

            retry_after = response.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after else min(60, SPOTIFY_API_RETRY_BASE_SECONDS * (attempt + 1) * 2)
            except Exception:
                delay = min(60, SPOTIFY_API_RETRY_BASE_SECONDS * (attempt + 1) * 2)
            if delay > SPOTIFY_API_MAX_RETRY_AFTER_SECONDS:
                raise RuntimeError(
                    f"Spotify API rate-limit trop long ({delay}s). "
                    "Le chemin API est abandonné pour éviter une attente excessive."
                )
            delay = max(3, delay)
            print(f"    Spotify API rate-limit : pause {delay}s")
            time.sleep(delay)

        if response is None:
            break

        if response.status_code == 429:
            raise RuntimeError("Spotify API rate-limit persistant.")

        response.raise_for_status()
        page = response.json()
        items = page.get("items") or []
        total = page.get("total") if isinstance(page.get("total"), int) else total

        for item in items:
            track = normalize_spotify_api_item(item)
            if not track:
                continue
            tracks.append(track)

        offset += len(items)
        if page.get("next"):
            time.sleep(SPOTIFY_API_PAGE_DELAY_SECONDS)
        if not items:
            break
        if total is not None and offset >= total:
            break
        if not page.get("next"):
            break

    if total is not None and offset < total:
        raise RuntimeError(f"Spotify API a renvoyé une playlist partielle ({offset}/{total}).")

    return tracks


def extract_tracks_from_entity(entity):
    if not isinstance(entity, dict):
        return []

    # Le format __NEXT_DATA__ utilisé par ton bot.
    track_list = entity.get("trackList")
    if isinstance(track_list, list):
        result = []
        for item in track_list:
            if isinstance(item, dict) and (item.get("title") or item.get("name")):
                result.append(normalize_spotify_track(item))
        if result:
            return result

    # Autres variantes vues dans les états Spotify.
    for key in ("tracks", "items"):
        container = entity.get(key)
        if isinstance(container, dict):
            items = container.get("items") or container.get("tracks") or container.get("entities")
        elif isinstance(container, list):
            items = container
        else:
            items = None
        if not items:
            continue
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            track = item.get("track") if isinstance(item.get("track"), dict) else item
            if track.get("name") or track.get("title"):
                result.append(normalize_spotify_track(track))
        if result:
            return result

    # Recherche récursive d'un tableau de pistes dans les données.
    for node in walk_json(entity):
        if not isinstance(node, dict):
            continue
        for key in ("trackList", "tracks", "items"):
            value = node.get(key)
            if isinstance(value, list) and value:
                result = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    track = item.get("track") if isinstance(item.get("track"), dict) else item
                    if track.get("name") or track.get("title"):
                        result.append(normalize_spotify_track(track))
                if len(result) >= 2:
                    return result
    return []


def extract_tracks_from_next_data(next_data, playlist_id):
    if not isinstance(next_data, dict):
        return []
    props = next_data.get("props", {})
    page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
    state = page_props.get("state", {}) if isinstance(page_props, dict) else {}
    data = state.get("data", {}) if isinstance(state, dict) else {}
    entity = data.get("entity", {}) if isinstance(data, dict) else {}
    if isinstance(entity, dict) and entity:
        result = extract_tracks_from_entity(entity)
        if result:
            return result
    entity = find_entity(next_data, "playlist", playlist_id)
    if entity:
        return extract_tracks_from_entity(entity)
    return []


def extract_tracks_from_html(text, playlist_id):
    next_data = decode_spotify_next_data(text)
    if next_data:
        result = extract_tracks_from_next_data(next_data, playlist_id)
        if result:
            return result

    state = decode_spotify_state(text)
    if state:
        entity = find_entity(state, "playlist", playlist_id) or state
        result = extract_tracks_from_entity(entity)
        if result:
            return result

    # Fallback morceau unique depuis les meta tags.
    meta = extract_spotify_meta(text)
    title = meta.get("og_title") or meta.get("twitter_title") or meta.get("ld_title")
    artist = meta.get("music_musician") or meta.get("ld_artist")
    if title and artist:
        return [{"title": clean_spotify_text(title), "artist": clean_spotify_text(artist), "duration": None, "spotify_id": None}]

    return []


def fetch_spotify_playlist_html(playlist_id):
    urls = [
        f"https://open.spotify.com/playlist/{playlist_id}",
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
    ]
    last_error = None
    for url in urls:
        for headers in SPOTIFY_PUBLIC_HEADER_VARIANTS:
            try:
                response = requests.get(url, headers=headers, timeout=20)
                response.raise_for_status()
                text = response.text
                if text:
                    return text, url
            except Exception as exc:
                last_error = exc
    raise RuntimeError(f"Impossible de lire Spotify : {last_error}")


def resolve_spotify_tracks(public_url):
    public_url = sanitize_spotify_input(public_url)
    resolved_url = resolve_spotify_shortlink(public_url)
    kind, item_id = normalize_spotify_target(resolved_url)
    if not kind:
        raise RuntimeError("Lien Spotify non reconnu.")

    public_url = spotify_public_url(kind, item_id)
    embed_url = spotify_embed_url(kind, item_id)
    oembed = None
    embed_html = None

    try:
        oembed = fetch_json(spotify_oembed_url(kind, item_id))
    except Exception:
        oembed = None

    try:
        embed_html = fetch_text(embed_url)
    except Exception:
        embed_html = None

    if embed_html:
        next_data = decode_spotify_next_data(embed_html)
        if next_data:
            declared_count = extract_spotify_declared_count(embed_html, next_data)
            entity = (
                next_data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
            )
            if isinstance(entity, dict) and entity:
                thumbnail = spotify_thumbnail_from_entity(entity, fallback=(oembed.get("thumbnail_url") if oembed else None))
                color = spotify_color_from_entity(entity)
                access_token = extract_spotify_access_token(next_data)
                client_token = None
                web_token = None
                if kind == "playlist":
                    try:
                        client_token = fetch_spotify_client_credentials_token()
                    except Exception as exc:
                        print(f"    API Spotify officielle indisponible : {exc}")
                    try:
                        web_token = fetch_spotify_web_access_token()
                    except Exception as exc:
                        print(f"    API Spotify web-player indisponible : {exc}")

                if kind == "playlist":
                    if web_token:
                        try:
                            api_tracks = fetch_spotify_playlist_tracks_pathfinder(item_id, web_token)
                            if api_tracks:
                                save_spotify_playlist_cache(item_id, api_tracks)
                                return api_tracks
                        except Exception as exc:
                            print(f"    API Spotify Pathfinder indisponible : {exc}")

                    for token_label, token_value in (
                        ("officielle", client_token),
                        ("embed", access_token),
                    ):
                        if not token_value:
                            continue
                        try:
                            api_tracks = fetch_spotify_playlist_tracks_api(item_id, token_value)
                            if api_tracks:
                                save_spotify_playlist_cache(item_id, api_tracks)
                                return api_tracks
                        except Exception as exc:
                            print(f"    API Spotify {token_label} indisponible : {exc}")

                    cached_tracks = load_spotify_playlist_cache(item_id, minimum_count=declared_count)
                    if cached_tracks:
                        return cached_tracks

                if kind == "track":
                    track = spotify_track_from_embed_item(entity, fallback_thumbnail=thumbnail)
                    track["color"] = color
                    return [track]

                track_list = entity.get("trackList")
                if isinstance(track_list, list):
                    tracks = []
                    for item in track_list:
                        if not isinstance(item, dict) or not (item.get("title") or item.get("name")):
                            continue
                        track = spotify_track_from_embed_item(item, fallback_thumbnail=thumbnail)
                        track["color"] = color
                        tracks.append(track)
                    if tracks:
                        if kind == "playlist":
                            cached_tracks = reject_partial_spotify_playlist(
                                item_id,
                                tracks,
                                declared_count=declared_count,
                            )
                            if cached_tracks:
                                return cached_tracks
                        return tracks

    if not public_url:
        raise RuntimeError("Impossible de lire la page Spotify.")

    try:
        html_text = fetch_text(public_url)
    except Exception as exc:
        raise RuntimeError(f"Impossible de lire Spotify : {exc}") from exc

    meta = extract_spotify_meta(html_text)
    thumbnail = oembed.get("thumbnail_url") if oembed else None

    if kind == "track":
        raw_title = oembed.get("title") if oembed else None
        raw_artist = oembed.get("author_name") if oembed else None

        if not raw_title:
            raw_title = meta.get("og_title") or meta.get("twitter_title") or meta.get("ld_title")
        if not raw_artist:
            raw_artist = meta.get("music_musician") or meta.get("ld_artist")

        artist_from_desc, title_from_desc = parse_artist_and_title(meta.get("og_description"))
        artist_from_title, title_from_title = parse_artist_and_title(raw_title)

        title = clean_spotify_text(raw_title) or title_from_desc or title_from_title
        artist = clean_spotify_text(raw_artist) or artist_from_desc or artist_from_title

        if not title:
            raise RuntimeError("Impossible de lire les infos Spotify du morceau.")

        return [spotify_track_from_meta(title, artist, thumbnail=thumbnail, duration=None)]

    state = decode_spotify_state(html_text)
    if state:
        entity = find_entity(state, kind, item_id) or state
        tracks = extract_tracks_from_entity(entity)
        if tracks:
            if kind == "playlist":
                cached_tracks = reject_partial_spotify_playlist(
                    item_id,
                    tracks,
                    declared_count=extract_spotify_declared_count(html_text),
                )
                if cached_tracks:
                    return cached_tracks
            return tracks

    raise RuntimeError("Impossible d'extraire les pistes depuis la page Spotify.")


def get_spotify_tracks():
    tracks = resolve_spotify_tracks(PLAYLIST_URL)
    if not tracks:
        raise RuntimeError("Aucune piste Spotify trouvée.")
    return tracks

# ============================================================
# FICHIERS EXISTANTS
# ============================================================

def normalize_filename(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[<>:\"/\\|?*]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def build_existing_index():
    existing = {}
    for file in OUTPUT_DIR.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            relative_parts = file.relative_to(OUTPUT_DIR).parts
        except ValueError:
            relative_parts = file.parts
        if SPOTIFY_PLAYLIST_CACHE_DIRNAME in relative_parts:
            continue
        existing[normalize_search_text(file.stem)] = file
    return existing


def find_existing_track(artist, title, existing_index):
    target = normalize_search_text(f"{artist} - {title}")
    if target in existing_index:
        return existing_index[target]

    artist_norm = normalize_search_text(artist)
    title_norm = normalize_search_text(title)
    if not artist_norm or not title_norm:
        return None

    for normalized_name, file in existing_index.items():
        if artist_norm in normalized_name and title_norm in normalized_name:
            return file
    return None

# ============================================================
# YOUTUBE RECHERCHE / EXTRACTION
# ============================================================

def make_ytdlp_search_options(logger=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "default_search": "ytsearch",
        "proxy": "",
        "extractor_args": {
            "youtube": {
                "player_client": list(YOUTUBE_YTDL_PLAYER_CLIENTS)
            }
        },
    }
    if YTDLP_JS_RUNTIMES:
        options["js_runtimes"] = dict(YTDLP_JS_RUNTIMES)
    if YTDLP_REMOTE_EJS:
        options["remote_components"] = {"ejs:github"}
    if YTDLP_COOKIES_BROWSER:
        options["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    if logger is not None:
        options["logger"] = logger
    return options


def make_ytdlp_download_options(outtmpl, logger=None):
    options = {
        "format": "bestaudio[protocol^=http][abr<=256]/bestaudio[protocol^=http]/bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": False,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "proxy": "",
        "extractor_args": {
            "youtube": {
                "player_client": list(YOUTUBE_YTDL_PLAYER_CLIENTS)
            }
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "0",
            }
        ],
    }
    if YTDLP_JS_RUNTIMES:
        options["js_runtimes"] = dict(YTDLP_JS_RUNTIMES)
    if YTDLP_REMOTE_EJS:
        options["remote_components"] = {"ejs:github"}
    if YTDLP_COOKIES_BROWSER:
        options["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    if logger is not None:
        options["logger"] = logger
    return options


def make_ytdlp_detail_options():
    options = make_ytdlp_search_options()
    options["extract_flat"] = False
    options["default_search"] = "auto"
    return options


def ytdlp_player_client_variants():
    variants = []
    for player_clients in (YOUTUBE_YTDL_PLAYER_CLIENTS, *YOUTUBE_YTDL_FALLBACK_CLIENTS):
        normalized = tuple(player_clients)
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def configure_ytdlp_player_clients(options, player_clients):
    options["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}
    return options


def extract_info_with_fallbacks(candidate, *, search=None):
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return None

    if search is None:
        search = candidate_text.lower().startswith("ytsearch")

    last_error = None
    for player_clients in ytdlp_player_client_variants():
        logger = YtdlpLogCollector()
        options = make_ytdlp_search_options(logger=logger) if search else make_ytdlp_detail_options()
        options["logger"] = logger
        configure_ytdlp_player_clients(options, player_clients)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(candidate_text, download=False)
        except Exception as exc:
            if logger.authentication_error or is_youtube_auth_error(exc):
                raise YouTubeAuthenticationRequired(logger.authentication_error or str(exc)) from exc
            last_error = exc
            continue
        if logger.authentication_error:
            raise YouTubeAuthenticationRequired(logger.authentication_error)
        if data:
            return data

    if last_error is not None:
        return None
    return None


def youtube_search(query, count):
    expression = f"ytsearch{count}:{query}"
    for player_clients in ytdlp_player_client_variants():
        logger = YtdlpLogCollector()
        options = make_ytdlp_search_options(logger=logger)
        configure_ytdlp_player_clients(options, player_clients)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(expression, download=False)
            if logger.authentication_error:
                raise YouTubeAuthenticationRequired(logger.authentication_error)
            if data:
                entries = [e for e in (data.get("entries") or []) if e]
                if entries:
                    return entries
        except YouTubeAuthenticationRequired:
            raise
        except Exception as exc:
            if is_youtube_auth_error(exc):
                raise YouTubeAuthenticationRequired(str(exc)) from exc
            continue
    return []


def resolve_text_query_candidates(query_text, *, source="youtube", limit=5, enrich_limit=2):
    query_value = str(query_text or "").strip()
    if not query_value:
        return []

    entries = youtube_search(query_value, max(1, int(limit)))

    if not entries:
        direct_result = extract_info_with_fallbacks(query_value, search=False)
        if isinstance(direct_result, dict):
            if isinstance(direct_result.get("entries"), list):
                entries = [entry for entry in direct_result.get("entries", []) if entry]
            else:
                entries = [direct_result]

    if not entries:
        return []

    ranked_entries = [entry for _, entry in pick_ranked(entries, artist=None, title=query_value, duration=None, query=query_value)]
    if not ranked_entries or enrich_limit <= 0:
        return ranked_entries

    enriched_entries = []
    for entry in ranked_entries[:max(1, int(enrich_limit))]:
        enriched_entries.append(enrich_entry(entry))

    merged_entries = []
    seen_keys = set()
    for entry in list(enriched_entries) + ranked_entries[max(1, int(enrich_limit)) :]:
        if not entry:
            continue
        key = entry.get("id") or entry.get("webpage_url") or entry.get("url") or entry.get("title")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        merged_entries.append(entry)

    return [entry for _, entry in pick_ranked(merged_entries, artist=None, title=query_value, duration=None, query=query_value)] or merged_entries


def enrich_entry(entry):
    result = {
        "id": entry.get("id"),
        "url": entry.get("webpage_url") or entry.get("original_url") or entry.get("url"),
        "title": entry.get("title") or "",
        "track": entry.get("track") or "",
        "artist": entry.get("artist") or "",
        "album": entry.get("album") or "",
        "uploader": entry.get("uploader") or "",
        "channel": entry.get("channel") or "",
        "description": entry.get("description") or "",
        "duration": entry.get("duration"),
    }
    url = result["url"]
    if url:
        try:
            data = extract_info_with_fallbacks(url, search=False)
            if data:
                result.update({
                    "url": data.get("webpage_url") or result["url"],
                    "title": data.get("title") or result["title"],
                    "track": data.get("track") or result["track"],
                    "artist": data.get("artist") or result["artist"],
                    "album": data.get("album") or result["album"],
                    "uploader": data.get("uploader") or result["uploader"],
                    "channel": data.get("channel") or result["channel"],
                    "description": data.get("description") or result["description"],
                    "duration": data.get("duration") or result["duration"],
                })
        except Exception as exc:
            if isinstance(exc, YouTubeAuthenticationRequired):
                raise
            if is_youtube_auth_error(exc):
                raise YouTubeAuthenticationRequired(str(exc)) from exc
    return result


def dedupe_entries(entries):
    result = {}
    for entry in entries:
        key = entry.get("id") or entry.get("url") or entry.get("title")
        if key:
            result[key] = entry
    return list(result.values())


def candidate_identity(candidate):
    """Build a stable key so a failed video is not selected again."""
    if not isinstance(candidate, dict):
        return ""
    candidate_id = str(candidate.get("id") or "").strip()
    if candidate_id:
        return f"id:{candidate_id.lower()}"

    url = str(
        candidate.get("webpage_url")
        or candidate.get("original_url")
        or candidate.get("url")
        or ""
    ).strip()
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "youtube.com" in host:
        video_id = (parsed.query and re.search(r"(?:^|&)v=([^&]+)", parsed.query))
        if video_id:
            return f"id:{video_id.group(1).lower()}"
    if "youtu.be" in host:
        video_id = parsed.path.strip("/").split("/", 1)[0]
        if video_id:
            return f"id:{video_id.lower()}"
    return f"url:{url.split('#', 1)[0].rstrip('/').lower()}"


def pick_ranked(entries, artist, title, duration, query):
    scored = []
    for entry in entries:
        candidate = enrich_entry(entry)
        score = score_youtube_candidate(
            candidate,
            title=title,
            artist=artist,
            duration=duration,
            query_text=query,
            source="spotify",
        )
        scored.append((score, candidate))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def resolve_youtube(artist, title, duration, *, excluded_urls=None):
    excluded_ids = {
        str(value).strip().lower()
        for value in (excluded_urls or set())
        if str(value).strip()
    }

    def rank_available(entries, query):
        ranked = pick_ranked(entries, expected_artist, expected_title, duration, query)
        if not excluded_ids:
            return ranked
        return [
            (score, candidate)
            for score, candidate in ranked
            if candidate_identity(candidate) not in excluded_ids
        ]

    expected_artist = simplify_track_title(artist)
    expected_title = simplify_track_title(title)
    simplified_title = simplify_track_title(expected_title)

    if expected_artist and expected_title:
        primary_queries = [
            f'"{expected_artist}" "{expected_title}" official audio',
            f'"{expected_artist}" "{expected_title}" topic',
        ]
        fallback_queries = [
            f'"{expected_artist}" "{expected_title}" audio',
            f'"{expected_artist}" "{expected_title}" "provided to youtube by"',
        ]
        if simplified_title and simplified_title != expected_title:
            fallback_queries.extend([
                f'"{expected_artist}" "{simplified_title}" official audio',
                f'"{expected_artist}" "{simplified_title}" audio',
            ])
    elif expected_title:
        primary_queries = [
            f'"{expected_title}" official audio',
            f'"{expected_title}" audio',
        ]
        fallback_queries = [f'"{expected_title}"']
        if simplified_title and simplified_title != expected_title:
            fallback_queries.append(f'"{simplified_title}" audio')
    else:
        primary_queries = [expected_title or artist]
        fallback_queries = []

    all_entries = []
    permissive_best = None
    permissive_score = None
    for index, query in enumerate(primary_queries + fallback_queries):
        if not query:
            continue
        safe_print(f"    Recherche principale : {query}" if index == 0 else f"    Recherche complémentaire : {query}")
        count = PRIMARY_SEARCH_SIZE if index == 0 else FALLBACK_SEARCH_SIZE
        all_entries.extend(youtube_search(query, count))
        ranked_now = rank_available(
            dedupe_entries(all_entries),
            query,
        )
        if not ranked_now:
            continue
        top_score = ranked_now[0][0]
        second_score = ranked_now[1][0] if len(ranked_now) > 1 else None
        safe_print(
            f"    Score courant : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        acceptable_score, acceptable_candidate = first_acceptable_ranked(
            ranked_now,
            expected_artist,
            expected_title,
            duration,
        )
        if acceptable_candidate is not None and (
            permissive_best is None or acceptable_score > permissive_score
        ):
            permissive_best = acceptable_candidate
            permissive_score = acceptable_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (
            second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN
        ) and acceptable_candidate is ranked_now[0][1]:
            return ranked_now[0][1], top_score
        if len(ranked_now) > 1 and top_score == second_score and top_score >= STRICT_FAST_ACCEPT_SCORE:
            a = ranked_now[0][1]
            b = ranked_now[1][1]
            at = normalize_search_text(a.get("title") or a.get("track"))
            bt = normalize_search_text(b.get("title") or b.get("track"))
            aa = normalize_search_text(" ".join([a.get("artist") or "", a.get("uploader") or "", a.get("channel") or ""]))
            ba = normalize_search_text(" ".join([b.get("artist") or "", b.get("uploader") or "", b.get("channel") or ""]))
            durations_close = (
                isinstance(a.get("duration"), (int, float))
                and isinstance(b.get("duration"), (int, float))
                and abs(int(a["duration"]) - int(b["duration"])) <= 3
            )
            if at and bt and at == bt and has_any_token_match(aa, ba) and durations_close:
                return a, top_score

    ranked = rank_available(
        dedupe_entries(all_entries),
        f"{expected_artist} {expected_title}".strip(),
    )
    if ranked:
        top_score = ranked[0][0]
        second_score = ranked[1][0] if len(ranked) > 1 else None
        safe_print(
            f"    Score final : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        acceptable_score, acceptable_candidate = first_acceptable_ranked(
            ranked,
            expected_artist,
            expected_title,
            duration,
        )
        if acceptable_candidate is not None and (
            permissive_best is None or acceptable_score > permissive_score
        ):
            permissive_best = acceptable_candidate
            permissive_score = acceptable_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (
            second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN
        ) and acceptable_candidate is ranked[0][1]:
            return ranked[0][1], top_score
        if top_score >= YOUTUBE_EARLY_ACCEPT_SCORE and (
            second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN
        ) and acceptable_candidate is ranked[0][1]:
            return ranked[0][1], top_score

    fallback_searches = []
    if expected_artist and expected_title:
        fallback_searches.extend([
            f"{expected_artist} {expected_title}",
            f"{expected_title} {expected_artist}",
        ])
    if expected_title:
        fallback_searches.append(expected_title)
    if artist and title:
        fallback_searches.append(query if (query := f"{artist} {title}".strip()) else "")

    tried_searches = set()
    for search_text in fallback_searches:
        normalized_search = normalize_search_text(search_text)
        if not normalized_search or normalized_search in tried_searches:
            continue
        tried_searches.add(normalized_search)
        safe_print(f"    Recherche complémentaire : {search_text}")
        fallback_entries = resolve_text_query_candidates(
            search_text,
            source="spotify",
            limit=max(6, STRICT_SLOW_SEARCH_SIZE + 3),
            enrich_limit=3,
        )
        if not fallback_entries:
            continue
        fallback_ranked = rank_available(
            dedupe_entries(fallback_entries),
            search_text,
        )
        if not fallback_ranked:
            continue
        top_score = fallback_ranked[0][0]
        second_score = fallback_ranked[1][0] if len(fallback_ranked) > 1 else None
        safe_print(
            f"    Score courant : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        acceptable_score, acceptable_candidate = first_acceptable_ranked(
            fallback_ranked,
            expected_artist,
            expected_title,
            duration,
        )
        if acceptable_candidate is not None and (
            permissive_best is None or acceptable_score > permissive_score
        ):
            permissive_best = acceptable_candidate
            permissive_score = acceptable_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (
            second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN
        ) and acceptable_candidate is fallback_ranked[0][1]:
            return fallback_ranked[0][1], top_score
        if top_score >= YOUTUBE_EARLY_ACCEPT_SCORE and (
            second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN
        ) and acceptable_candidate is fallback_ranked[0][1]:
            return fallback_ranked[0][1], top_score

    if permissive_best:
        safe_print(f"    Fallback bot permissif : score {permissive_score}")
        return permissive_best, permissive_score

    return None, None

# ============================================================
# DOWNLOAD
# ============================================================


def download_audio(candidate, artist, title):
    url = candidate.get("url")
    if not url:
        return False, "missing_url"

    safe_artist = normalize_filename(artist)
    safe_title = normalize_filename(title)
    outtmpl = str(OUTPUT_DIR / f"{safe_artist} - {safe_title}.%(ext)s")

    # Protection supplémentaire : ne pas remplacer un fichier existant.
    existing_target = None
    for ext in AUDIO_EXTENSIONS:
        candidate_path = OUTPUT_DIR / f"{safe_artist} - {safe_title}{ext}"
        if candidate_path.exists():
            existing_target = candidate_path
            break
    if existing_target:
        safe_print(f"    SKIP sécurité : {existing_target.name}")
        return True, None

    last_error = None
    had_nonzero_exit = False
    for player_clients in ytdlp_player_client_variants():
        logger = YtdlpLogCollector()
        options = make_ytdlp_download_options(outtmpl, logger=logger)
        configure_ytdlp_player_clients(options, player_clients)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.download([url])
            if logger.authentication_error:
                return False, "youtube_auth_required"
            if result not in (0, None):
                had_nonzero_exit = True
                continue
        except Exception as exc:
            if logger.authentication_error or is_youtube_auth_error(exc):
                return False, "youtube_auth_required"
            last_error = exc
            continue

        # Vérification post-téléchargement après chaque profil yt-dlp.
        for ext in AUDIO_EXTENSIONS:
            if (OUTPUT_DIR / f"{safe_artist} - {safe_title}{ext}").exists():
                return True, None

    if last_error is not None:
        safe_print(f"    Téléchargement échoué : {last_error}")
        return False, "download_error"
    if had_nonzero_exit:
        return False, "yt_dlp_exit"
    return False, "output_missing"


def initialize_worker(output_dir, capture_logs):
    global OUTPUT_DIR, WORKER_CAPTURE_LOGS
    OUTPUT_DIR = Path(output_dir)
    WORKER_CAPTURE_LOGS = capture_logs


def process_missing_track(track, index, total):
    buffer = io.StringIO()
    artist = track.get("artist") or "Artiste inconnu"
    title = track.get("title") or "Titre inconnu"
    duration = track.get("duration")

    try:
        stdout_context = redirect_stdout(buffer) if WORKER_CAPTURE_LOGS else nullcontext()
        stderr_context = redirect_stderr(buffer) if WORKER_CAPTURE_LOGS else nullcontext()
        with stdout_context, stderr_context:
            safe_print(style("┌" + "─" * 64 + "┐", ANSI_MAGENTA))
            safe_print(f"{style('│', ANSI_MAGENTA)} {style(f'[{index}/{total}]', ANSI_BOLD)} {artist} - {title}")
            if duration:
                safe_print(f"{style('│', ANSI_MAGENTA)} Durée Spotify : {duration}s")
            safe_print(style("├" + "─" * 64 + "┤", ANSI_MAGENTA))

            attempted_ids = set()
            candidate = None
            score = None
            candidate_name = None
            download_error = "no_candidate"

            for attempt in range(1, MAX_DOWNLOAD_CANDIDATE_ATTEMPTS + 1):
                candidate, score = resolve_youtube(
                    artist,
                    title,
                    duration,
                    excluded_urls=attempted_ids,
                )
                if not candidate:
                    break

                candidate_id = candidate_identity(candidate)
                if candidate_id:
                    attempted_ids.add(candidate_id)
                candidate_name = candidate.get("title") or candidate.get("track") or "(sans titre)"
                if attempt == 1:
                    safe_print(style(f"    [OK] Source : {candidate_name}", ANSI_GREEN))
                else:
                    safe_print(style(f"    [RETRY {attempt}] Source suivante : {candidate_name}", ANSI_YELLOW))
                safe_print(style(f"    [OK] Score  : {score}", ANSI_GREEN))

                download_result = download_audio(candidate, artist, title)
                if isinstance(download_result, tuple):
                    download_ok, download_error = download_result
                else:
                    download_ok = bool(download_result)
                    download_error = None
                if download_ok:
                    safe_print(style("    [DONE] Fichier écrit.", ANSI_GREEN))
                    return {
                        "ok": True,
                        "artist": artist,
                        "title": title,
                        "score": score,
                        "candidate": candidate_name,
                        "log": buffer.getvalue(),
                        "error": None,
                    }

                if download_error == "youtube_auth_required":
                    safe_print(style("    [BLOCKED] YouTube exige des cookies authentifiés.", ANSI_RED))
                    return {
                        "ok": False,
                        "artist": artist,
                        "title": title,
                        "score": score,
                        "candidate": candidate_name,
                        "log": buffer.getvalue(),
                        "error": download_error,
                    }

                safe_print(style(f"    [FAIL] Téléchargement invalide ({download_error or 'download_failed'}).", ANSI_RED))
                if attempt < MAX_DOWNLOAD_CANDIDATE_ATTEMPTS:
                    safe_print(style("    [RETRY] Recherche d'une autre source...", ANSI_YELLOW))
                else:
                    break

            if candidate is None:
                safe_print(style("    [FAIL] Aucune source suffisamment fiable.", ANSI_RED))
                download_error = "no_candidate"

            return {
                "ok": False,
                "artist": artist,
                "title": title,
                "score": score,
                "candidate": candidate_name,
                "log": buffer.getvalue(),
                "error": download_error or "download_failed",
            }
    except YouTubeAuthenticationRequired as exc:
        message = str(exc)
        if WORKER_CAPTURE_LOGS:
            buffer.write(style("    [BLOCKED] YouTube exige des cookies authentifiés.", ANSI_RED) + "\n")
            buffer.write(message + "\n")
        else:
            safe_print(style("    [BLOCKED] YouTube exige des cookies authentifiés.", ANSI_RED))
            safe_print(message)
        return {
            "ok": False,
            "artist": artist,
            "title": title,
            "score": None,
            "candidate": None,
            "log": buffer.getvalue(),
            "error": "youtube_auth_required",
        }
    except Exception:
        import traceback
        error_text = traceback.format_exc()
        if WORKER_CAPTURE_LOGS:
            buffer.write(style("    [FAIL] Exception inattendue.", ANSI_RED) + "\n")
            buffer.write(error_text)
        else:
            safe_print(style("    [FAIL] Exception inattendue.", ANSI_RED))
            safe_print(error_text)
        return {
            "ok": False,
            "artist": artist,
            "title": title,
            "score": None,
            "candidate": None,
            "log": buffer.getvalue(),
            "error": "exception",
        }

# ============================================================
# MAIN
# ============================================================

def parse_worker_count(value):
    try:
        worker_count = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("doit être un entier entre 1 et 5") from exc
    if not 1 <= worker_count <= 5:
        raise argparse.ArgumentTypeError("doit être compris entre 1 et 5")
    return worker_count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Télécharge les pistes manquantes d'une playlist Spotify.")
    parser.add_argument("--playlist", default=None, help="URL ou URI Spotify de la playlist")
    parser.add_argument("--output", default=None, help="Dossier de sortie")
    parser.add_argument(
        "--workers",
        type=parse_worker_count,
        default=None,
        help="Nombre de pistes traitées en parallèle (1 à 5)",
    )
    return parser.parse_args(argv)


def consume_track_result(result, failed, completed, total, existing):
    result_error = result.get("error")
    if result_error == "youtube_auth_required":
        status = style("[BLOCKED]", ANSI_RED)
    else:
        status = style("[OK]", ANSI_GREEN) if result.get("ok") else style("[FAIL]", ANSI_RED)
    score = result.get("score")
    candidate = result.get("candidate") or "n/a"
    score_text = f"score {score}" if score is not None else "no score"
    summary = f"{status} {format_progress(completed, total)} {result['artist']} - {result['title']} | {score_text} | {candidate}"
    print(summary)

    log_text = (result.get("log") or "").rstrip()
    if log_text:
        print(indent(log_text, "    "))
        print()

    if result.get("ok"):
        expected_name = normalize_search_text(f"{result['artist']} - {result['title']}")
        existing[expected_name] = OUTPUT_DIR / f"{normalize_filename(result['artist'])} - {normalize_filename(result['title'])}.opus"
        return True, None

    cause = result_error or "download_failed"
    failed.append(f"{result['artist']} - {result['title']} | cause={cause}")
    return False, result_error


def main(argv=None):
    global OUTPUT_DIR, PLAYLIST_URL, WORKER_CAPTURE_LOGS

    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    if args.output:
        OUTPUT_DIR = Path(args.output)
    if args.playlist:
        PLAYLIST_URL = args.playlist
    if not PLAYLIST_URL:
        print(
            style(
                "ERREUR : aucune playlist Spotify fournie. Utilise --playlist ou SPOTDL_PLAYLIST_URL.",
                ANSI_RED,
            )
        )
        return 2

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(style(f"ERREUR sortie : impossible de créer {OUTPUT_DIR} : {exc}", ANSI_RED))
        return 2

    print(style("Initialisation du resolver...", ANSI_DIM))
    print()

    try:
        existing = build_existing_index()
    except OSError as exc:
        print(style(f"ERREUR sortie : impossible de lire {OUTPUT_DIR} : {exc}", ANSI_RED))
        return 2
    print(style(f"Fichiers audio existants : {len(existing)}", ANSI_DIM))
    print()

    try:
        tracks = get_spotify_tracks()
    except Exception as exc:
        print(style(f"ERREUR Spotify : {exc}", ANSI_RED))
        return 1

    print(style(f"Pistes Spotify récupérées : {len(tracks)}", ANSI_DIM))
    print()

    missing = []
    duplicate_playlist_tracks = 0
    skipped_existing = 0
    seen = set()

    for track in tracks:
        artist = track.get("artist") or "Artiste inconnu"
        title = track.get("title") or "Titre inconnu"
        key = normalize_search_text(f"{artist} - {title}")

        if key in seen:
            duplicate_playlist_tracks += 1
            continue
        seen.add(key)

        existing_file = find_existing_track(artist, title, existing)
        if existing_file:
            skipped_existing += 1
            continue

        missing.append(track)

    print(f"Déjà présents et ignorés : {skipped_existing}")
    print(f"Doublons internes ignorés : {duplicate_playlist_tracks}")
    print(f"À résoudre : {len(missing)}")

    failed_path = OUTPUT_DIR / "_FAILED_BOT_RESOLVER.txt"
    if not missing:
        with suppress(OSError):
            failed_path.unlink()
        print("\nAucun nouveau morceau à traiter.")
        return 0

    failed = []
    requested_workers = args.workers if args.workers is not None else DEFAULT_CONCURRENT_TRACKS
    worker_count = requested_workers
    worker_count = min(worker_count, len(missing))
    print_banner(PLAYLIST_URL, OUTPUT_DIR, worker_count, len(missing))
    print(style(f"Travail en parallèle : {worker_count} processus", ANSI_DIM))
    if worker_count == 1:
        print(style("Mode séquentiel : détail de la piste affiché en direct.", ANSI_DIM))
    print()

    start_time = time.time()
    completed = 0
    succeeded = 0
    stopped_for_youtube_auth = False
    cancelled_futures = 0
    if worker_count == 1:
        WORKER_CAPTURE_LOGS = False
        for index, track in enumerate(missing, 1):
            completed += 1
            try:
                result = process_missing_track(track, index, len(missing))
            except Exception as exc:
                failed_track = f"{track.get('artist') or 'Artiste inconnu'} - {track.get('title') or 'Titre inconnu'}"
                failed.append(f"{failed_track} | cause=worker_exception:{exc}")
                print(style(f"[FAIL] {format_progress(completed, len(missing))} {failed_track} | worker exception: {exc}", ANSI_RED))
                continue

            was_successful, result_error = consume_track_result(
                result,
                failed,
                completed,
                len(missing),
                existing,
            )
            if was_successful:
                succeeded += 1
            elif result_error == "youtube_auth_required":
                stopped_for_youtube_auth = True
                print(
                    style(
                        "Arrêt anticipé : YouTube exige une session authentifiée. "
                        "Configure les cookies puis relance.",
                        ANSI_RED,
                    )
                )
                break
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=initialize_worker,
            initargs=(str(OUTPUT_DIR), True),
        ) as executor:
            future_tracks = {
                executor.submit(process_missing_track, track, index, len(missing)): track
                for index, track in enumerate(missing, 1)
            }
            for future in as_completed(future_tracks):
                completed += 1
                try:
                    result = future.result()
                except Exception as exc:
                    track = future_tracks[future]
                    failed_track = f"{track.get('artist') or 'Artiste inconnu'} - {track.get('title') or 'Titre inconnu'}"
                    failed.append(f"{failed_track} | cause=worker_exception:{exc}")
                    print(style(f"[FAIL] {format_progress(completed, len(missing))} {failed_track} | worker exception: {exc}", ANSI_RED))
                    print()
                    continue

                was_successful, result_error = consume_track_result(
                    result,
                    failed,
                    completed,
                    len(missing),
                    existing,
                )
                if was_successful:
                    succeeded += 1
                    continue

                if result_error == "youtube_auth_required":
                    stopped_for_youtube_auth = True
                    for pending_future in future_tracks:
                        if pending_future is not future and pending_future.cancel():
                            cancelled_futures += 1
                    print(
                        style(
                            "Arrêt anticipé : YouTube exige une session authentifiée. "
                            "Configure les cookies puis relance.",
                            ANSI_RED,
                        )
                    )
                    break

    elapsed = time.time() - start_time
    print(style("┌" + "─" * 66 + "┐", ANSI_CYAN))
    print(style(f"│ {fit_text('FINAL SUMMARY', 64):<64} │", ANSI_CYAN))
    print(style("├" + "─" * 66 + "┤", ANSI_CYAN))
    print(style(f"│ Done     : {succeeded:<52} │", ANSI_CYAN))
    print(style(f"│ Failed   : {len(failed):<52} │", ANSI_CYAN))
    if stopped_for_youtube_auth:
        print(style(f"│ Stopped  : {fit_text(f'YouTube authentication ({cancelled_futures} queued jobs canceled)', 52):<52} │", ANSI_CYAN))
    print(style(f"│ Elapsed  : {fit_text(f'{elapsed:.1f}s', 52):<52} │", ANSI_CYAN))
    print(style("└" + "─" * 66 + "┘", ANSI_CYAN))

    if failed:
        report_entries = list(failed)
        if stopped_for_youtube_auth:
            report_entries.append(
                "ARRÊT : YouTube a demandé une session authentifiée. "
                "Configure BOT_YTDLP_COOKIES_BROWSER puis relance."
            )
        try:
            atomic_write_text(failed_path, "\n".join(report_entries))
            print(f"Rapport : {failed_path}")
        except OSError as exc:
            print(style(f"Impossible d'écrire le rapport d'échec : {exc}", ANSI_RED))
        return 3 if stopped_for_youtube_auth else 2

    if failed_path.exists():
        try:
            failed_path.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
