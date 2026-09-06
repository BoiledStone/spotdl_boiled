import argparse
import json
import os
import re
import sys
import time
import unicodedata
from contextlib import suppress
from pathlib import Path
from html import unescape
from urllib.parse import quote, urlparse

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

# Ton bot utilise Firefox pour yt-dlp.
YTDLP_COOKIES_BROWSER = (os.getenv("BOT_YTDLP_COOKIES_BROWSER") or "firefox").strip().lower()
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
YTDLP_COOKIES_BROWSER = (os.getenv("BOT_YTDLP_COOKIES_BROWSER") or "firefox").strip().lower()
OUTPUT_DIR = Path(os.getenv("SPOTDL_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
PLAYLIST_URL = os.getenv("SPOTDL_PLAYLIST_URL") or DEFAULT_PLAYLIST_URL

PRIMARY_SEARCH_SIZE = 3
FALLBACK_SEARCH_SIZE = 2
STRICT_FAST_ACCEPT_SCORE = 160
YOUTUBE_EARLY_ACCEPT_SCORE = 145
YOUTUBE_EARLY_ACCEPT_MARGIN = 16
STRICT_SLOW_SEARCH_SIZE = 3

AUDIO_EXTENSIONS = {".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".webm"}
SPOTIFY_API_PAGE_SIZE = 100
SPOTIFY_API_MAX_RETRIES = 3
SPOTIFY_API_RETRY_BASE_SECONDS = 2
SPOTIFY_API_PAGE_DELAY_SECONDS = 1.0
SPOTIFY_PLAYLIST_CACHE_DIRNAME = ".spotify_cache"

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

# ============================================================
# TEXTE / MATCHING — repris de ton bot
# ============================================================

def clean_spotify_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    return re.sub(r"\s+", " ", text).strip()


def normalize_search_text(value):
    if not value:
        return ""
    text = str(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
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


def coerce_duration_seconds(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return int(round(float(value)))
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
                score -= 35

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
    if entry.get("uploader", "").endswith(" - Topic") or entry.get("channel", "").endswith(" - Topic"):
        score += 20
    if title_text and artist_text:
        if normalize_search_text(entry.get("track")) == title_text:
            score += 25
        if normalize_search_text(entry.get("artist")) == artist_text:
            score += 25

    if source in {"spotify", "deezer"}:
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


def load_spotify_playlist_cache(item_id):
    cache_path = spotify_playlist_cache_path(item_id)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if isinstance(tracks, list) and tracks:
        return tracks
    return None


def save_spotify_playlist_cache(item_id, tracks):
    cache_path = spotify_playlist_cache_path(item_id)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "tracks": tracks,
                    "count": len(tracks),
                    "saved_at": int(time.time()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
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
        cached_tracks = load_spotify_playlist_cache(item_id)
        if cached_tracks and len(cached_tracks) >= len(tracks):
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

    visual = entity.get("visualIdentity")
    if isinstance(visual, dict):
        images = visual.get("image")
        if isinstance(images, list):
            for image in reversed(images):
                if isinstance(image, dict) and image.get("url"):
                    return image["url"]

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
            if isinstance(a, dict) and a.get("name"):
                names.append(clean_spotify_text(a.get("name")))
        if names:
            return ", ".join(names)
    return clean_spotify_text(item.get("subtitle") or item.get("artist"))


def normalize_spotify_track(item):
    title = clean_spotify_text(item.get("title") or item.get("name"))
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
    title = title or "Titre inconnu"
    artist = artist or "Artiste inconnu"
    return {
        "query": f'"{artist}" "{title}" audio',
        "title": title,
        "artist": artist,
        "duration": duration,
        "spotify_id": item.get("id"),
        "source": "spotify",
    }


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

    track_type = clean_spotify_text(track.get("type")).lower()
    if track_type and track_type != "track":
        return None

    if not (track.get("name") or track.get("title")):
        return None

    normalized = normalize_spotify_track(track)
    thumbnail = spotify_api_thumbnail(track)
    if thumbnail:
        normalized["thumbnail"] = thumbnail
    return normalized


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
                if kind == "playlist":
                    try:
                        client_token = fetch_spotify_client_credentials_token()
                    except Exception as exc:
                        print(f"    API Spotify officielle indisponible : {exc}")

                if kind == "playlist":
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

                    cached_tracks = load_spotify_playlist_cache(item_id)
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

def make_ytdlp_search_options():
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "default_search": "ytsearch",
        "proxy": "",
        "extractor_args": {
            "youtube": {
                "player_client": ["default", "web_safari", "android_vr", "ios", "mweb"]
            }
        },
    }
    if YTDLP_COOKIES_BROWSER:
        options["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    return options


def make_ytdlp_download_options(outtmpl):
    options = {
        "format": "bestaudio[protocol^=http][abr<=256]/bestaudio[protocol^=http]/bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": False,
        "proxy": "",
        "extractor_args": {
            "youtube": {
                "player_client": ["default", "web_safari", "android_vr", "ios", "mweb"]
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
    if YTDLP_COOKIES_BROWSER:
        options["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    return options


def make_ytdlp_detail_options():
    options = make_ytdlp_search_options()
    options["extract_flat"] = False
    options["default_search"] = "auto"
    return options


def extract_info_with_fallbacks(candidate, *, search=None):
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return None

    if search is None:
        search = candidate_text.lower().startswith("ytsearch")

    options = make_ytdlp_search_options() if search else make_ytdlp_detail_options()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            data = ydl.extract_info(candidate_text, download=False)
    except Exception:
        return None
    return data


def youtube_search(query, count):
    expression = f"ytsearch{count}:{query}"
    for player_clients in (
        ["default", "web_safari", "android_vr", "ios", "mweb"],
        ["web_safari"],
        ["android_vr"],
        ["ios"],
        ["mweb"],
    ):
        options = make_ytdlp_search_options()
        options["extractor_args"] = {"youtube": {"player_client": player_clients}}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(expression, download=False)
            if data:
                entries = [e for e in (data.get("entries") or []) if e]
                if entries:
                    return entries
        except Exception as exc:
            last_error = exc
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
        options = make_ytdlp_search_options()
        options["extract_flat"] = False
        options["skip_download"] = True
        options["quiet"] = True
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=False)
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
        except Exception:
            pass
    return result


def dedupe_entries(entries):
    result = {}
    for entry in entries:
        key = entry.get("id") or entry.get("url") or entry.get("title")
        if key:
            result[key] = entry
    return list(result.values())


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


def resolve_youtube(artist, title, duration):
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
        print(f"    Recherche principale : {query}" if index == 0 else f"    Recherche complémentaire : {query}")
        count = PRIMARY_SEARCH_SIZE if index == 0 else FALLBACK_SEARCH_SIZE
        all_entries.extend(youtube_search(query, count))
        ranked_now = pick_ranked(dedupe_entries(all_entries), expected_artist, expected_title, duration, query)
        if not ranked_now:
            continue
        top_score = ranked_now[0][0]
        second_score = ranked_now[1][0] if len(ranked_now) > 1 else None
        print(
            f"    Score courant : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        if permissive_best is None or top_score > permissive_score:
            permissive_best = ranked_now[0][1]
            permissive_score = top_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN):
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

    ranked = pick_ranked(
        dedupe_entries(all_entries),
        expected_artist,
        expected_title,
        duration,
        f"{expected_artist} {expected_title}".strip(),
    )
    if ranked:
        top_score = ranked[0][0]
        second_score = ranked[1][0] if len(ranked) > 1 else None
        print(
            f"    Score final : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        if permissive_best is None or top_score > permissive_score:
            permissive_best = ranked[0][1]
            permissive_score = top_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN):
            return ranked[0][1], top_score
        if top_score >= YOUTUBE_EARLY_ACCEPT_SCORE and (second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN):
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
        print(f"    Recherche complémentaire : {search_text}")
        fallback_entries = resolve_text_query_candidates(
            search_text,
            source="spotify",
            limit=max(6, STRICT_SLOW_SEARCH_SIZE + 3),
            enrich_limit=3,
        )
        if not fallback_entries:
            continue
        fallback_ranked = pick_ranked(
            dedupe_entries(fallback_entries),
            expected_artist,
            expected_title,
            duration,
            search_text,
        )
        if not fallback_ranked:
            continue
        top_score = fallback_ranked[0][0]
        second_score = fallback_ranked[1][0] if len(fallback_ranked) > 1 else None
        print(
            f"    Score courant : {top_score}"
            + (f" | suivant : {second_score}" if second_score is not None else "")
        )
        if permissive_best is None or top_score > permissive_score:
            permissive_best = fallback_ranked[0][1]
            permissive_score = top_score
        if top_score >= STRICT_FAST_ACCEPT_SCORE and (second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN):
            return fallback_ranked[0][1], top_score
        if top_score >= YOUTUBE_EARLY_ACCEPT_SCORE and (second_score is None or top_score - second_score >= YOUTUBE_EARLY_ACCEPT_MARGIN):
            return fallback_ranked[0][1], top_score

    if permissive_best:
        print(f"    Fallback bot permissif : score {permissive_score}")
        return permissive_best, permissive_score

    return None, None

# ============================================================
# DOWNLOAD
# ============================================================

def download_audio(candidate, artist, title):
    url = candidate.get("url")
    if not url:
        return False

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
        print(f"    SKIP sécurité : {existing_target.name}")
        return True

    options = make_ytdlp_download_options(outtmpl)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.download([url])
        if result not in (0, None):
            return False
    except Exception as exc:
        print(f"    Téléchargement échoué : {exc}")
        return False

    # Vérification post-téléchargement.
    for ext in (".opus", ".webm", ".m4a", ".mp3"):
        if (OUTPUT_DIR / f"{safe_artist} - {safe_title}{ext}").exists():
            return True
    return False

# ============================================================
# MAIN
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Télécharge les pistes manquantes d'une playlist Spotify.")
    parser.add_argument("--playlist", default=None, help="URL ou URI Spotify de la playlist")
    parser.add_argument("--output", default=None, help="Dossier de sortie")
    return parser.parse_args(argv)


def main(argv=None):
    global OUTPUT_DIR, PLAYLIST_URL

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
        raise RuntimeError("Aucune playlist Spotify fournie. Utilise --playlist ou SPOTDL_PLAYLIST_URL.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print(" SPOTIFY → YOUTUBE → OPUS | RÉSOLVEUR DU BOT")
    print("=" * 64)
    print(f"Dossier   : {OUTPUT_DIR}")
    print(f"Playlist  : {PLAYLIST_URL}")
    print(f"Cookies   : {YTDLP_COOKIES_BROWSER or 'désactivés'}")
    print()

    existing = build_existing_index()
    print(f"Fichiers audio existants : {len(existing)}")
    print()

    try:
        tracks = get_spotify_tracks()
    except Exception as exc:
        print(f"ERREUR Spotify : {exc}")
        return 1

    print(f"Pistes Spotify récupérées : {len(tracks)}")
    print()

    missing = []
    duplicate_playlist_tracks = 0
    skipped_existing = 0
    seen = set()

    for track in tracks:
        artist = track.get("artist") or "Artiste inconnu"
        title = track.get("title") or "Titre inconnu"
        duration = track.get("duration")
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

    if not missing:
        print("\nAucun nouveau morceau à traiter.")
        return 0

    failed = []
    failed_path = OUTPUT_DIR / "_FAILED_BOT_RESOLVER.txt"

    for index, track in enumerate(missing, 1):
        artist = track.get("artist") or "Artiste inconnu"
        title = track.get("title") or "Titre inconnu"
        duration = track.get("duration")

        print("\n" + "-" * 64)
        print(f"[{index}/{len(missing)}] {artist} - {title}")
        if duration:
            print(f"    Durée Spotify : {duration}s")

        candidate, score = resolve_youtube(artist, title, duration)
        if not candidate:
            print("    ❌ Aucune source suffisamment fiable.")
            failed.append(f"{artist} - {title}")
            continue

        print(f"    ✅ Source : {candidate.get('title') or candidate.get('track') or '(sans titre)'}")
        print(f"    ✅ Score  : {score}")

        if not download_audio(candidate, artist, title):
            failed.append(f"{artist} - {title}")
            continue

        # Ajout immédiat à l'index : un éventuel doublon ultérieur sera ignoré.
        expected_name = normalize_search_text(f"{artist} - {title}")
        existing[expected_name] = OUTPUT_DIR / f"{normalize_filename(artist)} - {normalize_filename(title)}.opus"

    print("\n" + "=" * 64)
    print(" FIN")
    print("=" * 64)
    print(f"Traités : {len(missing) - len(failed)} / {len(missing)}")
    print(f"Échecs  : {len(failed)}")

    if failed:
        failed_path.write_text("\n".join(failed), encoding="utf-8")
        print(f"Rapport : {failed_path}")
        return 2

    if failed_path.exists():
        try:
            failed_path.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
