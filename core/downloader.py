from __future__ import annotations

import re
import json
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse, urljoin

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]
ErrorCallback = Callable[[str, str], None]


class PlaylistDownloaderError(Exception):
    """User-facing download error."""


class DownloadCancelled(PlaylistDownloaderError):
    """Raised when the user cancels the current download."""


@dataclass
class DownloadControls:
    pause_event: Event | None = None
    cancel_event: Event | None = None


@dataclass
class VerificationItem:
    index: int
    title: str
    artist: str
    expected_path: Path
    status: str


@dataclass
class VerificationResult:
    total: int
    present: int
    missing: list[VerificationItem]
    incomplete: list[VerificationItem]
    report_path: Path


class _ScriptTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._in_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.scripts.append("".join(self._parts).strip())
            self._parts = []
            self._in_script = False


def _browser_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _is_valid_apple_playlist_url(url: str) -> bool:
    clean = url.strip()
    return clean.startswith("https://music.apple.com/") and "/playlist/" in clean


def _fetch_playlist_page(url: str) -> str:
    return _fetch_text(url)


def _clean_track_text(value: str) -> str:
    return unescape(value).replace("\\u0026", "&").strip()


def _dedupe_tracks(tracks: list[tuple[str, str]], dedupe: bool = True) -> list[tuple[str, str]]:
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for title, artist in tracks:
        title = _clean_track_text(title)
        artist = _clean_track_text(artist)
        key = f"{title.lower()}::{artist.lower()}"
        if not title or not artist:
            continue
        if dedupe and key in seen:
            continue
        seen.add(key)
        deduped.append((title, artist))
    return deduped


def _extract_script_texts(html: str) -> list[str]:
    parser = _ScriptTextExtractor()
    parser.feed(html)
    return [script for script in parser.scripts if script]


def _looks_like_song_object(value: dict) -> bool:
    descriptor = value.get("contentDescriptor")
    if isinstance(descriptor, dict) and descriptor.get("kind") == "song":
        return True
    kind = value.get("kind") or value.get("$kind")
    return isinstance(kind, str) and "song" in kind.lower()


def _walk_apple_json_for_tracks(value: object, tracks: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        title = value.get("title") or value.get("name")
        artist = value.get("artistName")
        if isinstance(title, str) and isinstance(artist, str) and _looks_like_song_object(value):
            tracks.append((title, artist))
        for child in value.values():
            _walk_apple_json_for_tracks(child, tracks)
    elif isinstance(value, list):
        for child in value:
            _walk_apple_json_for_tracks(child, tracks)


def _extract_tracks_from_apple_json_scripts(html: str) -> list[tuple[str, str]]:
    tracks: list[tuple[str, str]] = []
    for script in _extract_script_texts(html):
        if ("artistName" not in script and "contentDescriptor" not in script) or not script.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(script)
        except json.JSONDecodeError:
            continue
        _walk_apple_json_for_tracks(payload, tracks)
    return _dedupe_tracks(tracks, dedupe=False)


def _extract_tracks_from_apple_html(html: str) -> list[tuple[str, str]]:
    tracks: list[tuple[str, str]] = []
    # Apple embeds track metadata in JSON where each track has "name" and "byArtist" text.
    pattern = re.compile(r'"name":"(?P<title>[^"]+?)".{0,220}?"byArtist":"(?P<artist>[^"]+?)"', re.DOTALL)
    for match in pattern.finditer(html):
        title = _clean_track_text(match.group("title"))
        artist = _clean_track_text(match.group("artist"))
        if title and artist:
            tracks.append((title, artist))
    return _dedupe_tracks(tracks, dedupe=False)


def _safe_filename_part(value: str, max_length: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "Unknown")[:max_length]


def _track_file_stem(index: int, title: str, artist: str) -> str:
    return f"{index:03d} - {_safe_filename_part(title)} - {_safe_filename_part(artist)}"


def _track_output_path(output_dir: Path, output_format: str, index: int, title: str, artist: str) -> Path:
    return output_dir / f"{_track_file_stem(index, title, artist)}.{output_format}"


def _is_complete_audio_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 1024


def _wait_if_paused(controls: DownloadControls | None) -> None:
    if controls and controls.cancel_event and controls.cancel_event.is_set():
        raise DownloadCancelled("Download cancelled.")
    while controls and controls.pause_event and controls.pause_event.is_set():
        if controls.cancel_event and controls.cancel_event.is_set():
            raise DownloadCancelled("Download cancelled.")
        time.sleep(0.25)


def read_playlist_tracks(url: str) -> list[tuple[str, str]]:
    if not _is_valid_apple_playlist_url(url):
        raise PlaylistDownloaderError(
            "That doesn't look like an Apple Music link. It should start with music.apple.com/..."
        )

    try:
        html = _fetch_playlist_page(url)
    except URLError as exc:
        raise PlaylistDownloaderError("No internet connection. Check your Wi-Fi and try again.") from exc
    except Exception as exc:
        raise PlaylistDownloaderError("Could not open playlist page. Check the link and try again.") from exc

    tracks = _extract_tracks_from_apple_json_scripts(html)
    token_candidates = [] if tracks else _discover_musickit_tokens(html, page_url=url)
    for token in token_candidates:
        try:
            tracks = _fetch_playlist_tracks_via_api(url, token=token)
        except Exception:
            tracks = []
        if tracks:
            break
    if not tracks:
        tracks = _extract_tracks_from_apple_html(html)
    if not tracks:
        raise PlaylistDownloaderError("Could not read songs from that playlist. Please try a different public link.")
    return tracks


def verify_playlist_downloads(url: str, output_dir: Path, output_format: str) -> VerificationResult:
    if output_format not in {"mp3", "wav"}:
        raise PlaylistDownloaderError("Unsupported output format.")

    output_dir.mkdir(parents=True, exist_ok=True)
    tracks = read_playlist_tracks(url)
    missing: list[VerificationItem] = []
    incomplete: list[VerificationItem] = []

    for index, (title, artist) in enumerate(tracks, start=1):
        expected_path = _track_output_path(output_dir, output_format, index, title, artist)
        if not expected_path.exists():
            missing.append(VerificationItem(index, title, artist, expected_path, "missing"))
        elif not _is_complete_audio_file(expected_path):
            incomplete.append(VerificationItem(index, title, artist, expected_path, "incomplete"))

    present = len(tracks) - len(missing) - len(incomplete)
    report_path = output_dir / "verification_report.txt"
    lines = [
        f"Expected songs: {len(tracks)}",
        f"Present: {present}",
        f"Missing: {len(missing)}",
        f"Incomplete: {len(incomplete)}",
        "",
    ]
    for item in missing + incomplete:
        lines.append(f"{item.status.upper()} | {item.index:03d} | {item.title} - {item.artist}")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    return VerificationResult(
        total=len(tracks),
        present=present,
        missing=missing,
        incomplete=incomplete,
        report_path=report_path,
    )


def _extract_musickit_token(html: str) -> str:
    # MusicKit token is usually embedded as a JWT string.
    patterns = [
        r'"token"\s*:\s*"(?P<token>eyJ[^"]+)"',
        r'"musicToken"\s*:\s*"(?P<token>eyJ[^"]+)"',
        r'"developerToken"\s*:\s*"(?P<token>eyJ[^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group("token")
    return ""


def _extract_jwt_candidates(text: str) -> list[str]:
    # Generic JWT matcher; MusicKit tokens are JWT-like strings.
    return re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text)


def _extract_script_urls(html: str, page_url: str) -> list[str]:
    urls = re.findall(r'<script[^>]+src="([^"]+)"', html)
    full: list[str] = []
    for src in urls:
        full.append(urljoin(page_url, src))
    return full


def _fetch_text(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(5):
        req = Request(url, headers=_browser_headers())
        try:
            with urlopen(req, timeout=25) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    if last_error:
        raise last_error
    return ""


def _discover_musickit_tokens(html: str, page_url: str) -> list[str]:
    tokens: list[str] = []
    direct = _extract_musickit_token(html)
    if direct:
        tokens.append(direct)

    # Fallback 1: any JWT-looking strings in initial HTML.
    tokens.extend(_extract_jwt_candidates(html))

    # Fallback 2: scan linked JS bundles for JWT tokens.
    script_urls = _extract_script_urls(html, page_url=page_url)
    for script_url in script_urls[:20]:
        if not script_url.endswith(".js"):
            continue
        try:
            js = _fetch_text(script_url)
        except Exception:
            continue
        tokens.extend(_extract_jwt_candidates(js))

    # De-duplicate while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _storefront_from_url(url: str) -> str:
    # https://music.apple.com/in/playlist/... -> storefront "in"
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return parts[0]
    return "us"


def _playlist_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1] if parts else ""


def _fetch_playlist_tracks_via_api(url: str, token: str) -> list[tuple[str, str]]:
    storefront = _storefront_from_url(url)
    playlist_id = _playlist_id_from_url(url)
    if not playlist_id:
        return []

    base = "https://amp-api.music.apple.com"
    first_url = (
        f"{base}/v1/catalog/{storefront}/playlists/{playlist_id}"
        f"?include=tracks&limit[tracks]=300"
    )

    tracks: list[tuple[str, str]] = []
    next_path: str | None = first_url

    while next_path:
        req = Request(
            next_path,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://music.apple.com",
                "Referer": url,
                "User-Agent": _browser_headers()["User-Agent"],
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(req, timeout=20) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

        # The included tracks are in the "included" list with type "songs".
        included = payload.get("included", []) or []
        for item in included:
            if item.get("type") != "songs":
                continue
            attrs = item.get("attributes") or {}
            title = (attrs.get("name") or "").strip()
            artist = (attrs.get("artistName") or "").strip()
            if title and artist:
                tracks.append((title, artist))

        # Pagination: playlist -> relationships -> tracks -> next
        data = payload.get("data") or []
        rel_next = None
        if data and isinstance(data, list):
            relationships = (data[0].get("relationships") or {}) if isinstance(data[0], dict) else {}
            tracks_rel = relationships.get("tracks") or {}
            rel_next = tracks_rel.get("next")

        if rel_next:
            next_path = base + rel_next
        else:
            next_path = None

        if len(tracks) >= 2000:
            break

    return _dedupe_tracks(tracks)


def _download_track_from_youtube(
    query: str,
    title: str,
    artist: str,
    index: int,
    output_dir: Path,
    output_format: str,
    ffmpeg_path: str,
    controls: DownloadControls | None = None,
) -> None:
    file_stem = _track_file_stem(index, title, artist)

    def progress_hook(_status: dict) -> None:
        _wait_if_paused(controls)

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(output_dir / f"{file_stem}.%(ext)s"),
        "ffmpeg_location": ffmpeg_path,
        "progress_hooks": [progress_hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": output_format,
                "preferredquality": "192" if output_format == "mp3" else None,
            }
        ],
    }
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"ytsearch1:{query}"])


def download_playlist(
    url: str,
    output_dir: Path,
    progress_callback: ProgressCallback,
    log_callback: LogCallback,
    error_callback: ErrorCallback,
    output_format: str = "mp3",
    ffmpeg_path: str = "ffmpeg",
    skip_existing: bool = True,
    controls: DownloadControls | None = None,
) -> list[tuple[str, str]]:
    if not _is_valid_apple_playlist_url(url):
        raise PlaylistDownloaderError(
            "That doesn't look like an Apple Music link. It should start with music.apple.com/..."
        )
    if output_format not in {"mp3", "wav"}:
        raise PlaylistDownloaderError("Unsupported output format.")

    output_dir.mkdir(parents=True, exist_ok=True)

    log_callback("Reading Apple Music playlist...")
    tracks = read_playlist_tracks(url)

    total = len(tracks)
    log_callback(f"Found {total} song(s). Starting downloads...")
    failures: list[tuple[str, str]] = []

    for index, (title, artist) in enumerate(tracks, start=1):
        _wait_if_paused(controls)
        track_name = f"{title} - {artist}"
        progress_callback(index, total, track_name)

        expected_path = _track_output_path(output_dir, output_format, index, title, artist)
        if skip_existing and _is_complete_audio_file(expected_path):
            log_callback(f"SKIP  {title} - {artist} (already downloaded)")
            continue

        log_callback(f"... {title} - {artist} (searching/downloading...)")

        try:
            _download_track_from_youtube(
                query=f"{title} {artist} official audio",
                title=title,
                artist=artist,
                index=index,
                output_dir=output_dir,
                output_format=output_format,
                ffmpeg_path=ffmpeg_path,
                controls=controls,
            )
            log_callback(f"OK  {title} - {artist}")
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            reason = str(exc)
            failures.append((track_name, reason))
            error_callback(track_name, reason)
            log_callback(f"WARN  {title} - {artist} (failed)")
        except Exception as exc:
            reason = str(exc)
            failures.append((track_name, reason))
            error_callback(track_name, reason)
            log_callback(f"WARN  {title} - {artist} (failed)")

    if failures:
        failed_path = output_dir / "failed_downloads.txt"
        lines = [f"{name} | {reason}" for name, reason in failures]
        failed_path.write_text("\n".join(lines), encoding="utf-8")
    return failures


def run_download_sync(
    url: str,
    output_dir: Path,
    progress_callback: ProgressCallback,
    log_callback: LogCallback,
    error_callback: ErrorCallback,
    output_format: str,
    ffmpeg_path: str,
    skip_existing: bool = True,
    controls: DownloadControls | None = None,
) -> list[tuple[str, str]]:
    return download_playlist(
        url=url,
        output_dir=output_dir,
        progress_callback=progress_callback,
        log_callback=log_callback,
        error_callback=error_callback,
        output_format=output_format,
        ffmpeg_path=ffmpeg_path,
        skip_existing=skip_existing,
        controls=controls,
    )
