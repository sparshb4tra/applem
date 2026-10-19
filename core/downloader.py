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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse, urljoin

from yt_dlp import YoutubeDL

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]
ErrorCallback = Callable[[str, str], None]

FETCH_ATTEMPTS = 5
FETCH_TIMEOUT_SECONDS = 25
FETCH_RETRY_BACKOFF_SECONDS = 0.4

# Hunting for a MusicKit token across linked JS bundles is only a fallback: the
# playlist page usually carries the track list already. Retrying up to 20
# bundles with the page-level budget meant a slow or blocked CDN could hold the
# run for many minutes before it gave up, so those fetches stay short and
# single shot.
BEST_EFFORT_FETCH_ATTEMPTS = 1
BEST_EFFORT_FETCH_TIMEOUT_SECONDS = 10
MAX_SCRIPT_BUNDLES = 20

# The MusicKit API fallback walks a playlist a few hundred tracks at a time, so
# this is the wait for one page of that walk rather than for the whole read.
API_FETCH_TIMEOUT_SECONDS = 20

# Pause and cancel are checked in short steps, and waiting for a retry counts as
# part of the run the user is allowed to stop.
CONTROL_POLL_SECONDS = 0.25

# Apple answers 4xx for dead, private, or rate limited links. Retrying those just
# makes the user wait for an answer that will not change, so only retry 5xx and
# connection level failures (or the couple of 4xx codes that are worth another try).
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}

# The report of the tracks a run could not download. It is written into the
# user's save folder and the "finished with warnings" dialog points straight at
# it, so it has to describe the run that just ended, not an older one.
FAILED_TRACKS_FILENAME = "failed_downloads.txt"


def _fetch_error_message(exc: Exception) -> str:
    """Turn a fetch failure into something a user can actually act on."""
    if isinstance(exc, HTTPError):
        code = exc.code
        if code in (401, 403):
            return (
                f"Apple refused that playlist page ({code}). "
                "The playlist is probably private or not available in that storefront."
            )
        if code == 404:
            return (
                "Apple could not find that playlist page (404). "
                "Open the link in a browser to check it is public and still exists."
            )
        if code == 429:
            return "Apple is rate limiting this app right now. Wait a minute and try again."
        if 500 <= code < 600:
            return f"Apple had a server problem ({code}). Try again in a minute."
        return f"Apple returned an unexpected response ({code}). Check the link and try again."
    if isinstance(exc, TimeoutError):
        return "The playlist page took too long to load. Check your connection and try again."
    if isinstance(exc, URLError):
        if isinstance(exc.reason, TimeoutError):
            return "The playlist page took too long to load. Check your connection and try again."
        return "No internet connection. Check your Wi-Fi and try again."
    return "Could not open playlist page. Check the link and try again."


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


# Apple hands the same playlist out through several link shapes: the modern
# page, a legacy itunes.apple.com address, and the geo./embed. hosts, each with
# or without the playlist's name in front of its id, often with a trailing
# slash or a tracking query left on the end. Only the canonical page answers
# with the track list the app reads: a trailing slash is a hard 404, and the
# same id asked for over an embed link comes back as a stub without the tracks.
# So a pasted link is turned into the canonical page URL before anything is
# fetched, and everything downstream works from that one address.
APPLE_LINK_HOSTS = frozenset(
    {"music.apple.com", "geo.music.apple.com", "itunes.apple.com", "embed.music.apple.com"}
)
PLAYLIST_ID_PREFIX = "pl."
STOREFRONT_PATTERN = re.compile(r"[a-z]{2}")


def _canonical_playlist_url(url: str) -> str:
    """The page URL for a pasted playlist link, whatever shape it arrived in.

    Raises `PlaylistDownloaderError` for a link that is not a playlist link, so
    callers get the user-facing message without knowing the link's shapes.
    """
    parts = _playlist_link_parts(url)
    if parts is None:
        raise PlaylistDownloaderError(
            "That doesn't look like an Apple Music playlist link. Open the playlist in "
            "Apple Music, use Share then Copy Link, and paste the whole link "
            "(it looks like https://music.apple.com/us/playlist/name/pl.xxxxxxxx)."
        )
    storefront, playlist_id = parts
    return f"https://music.apple.com/{storefront}/playlist/{playlist_id}"


def _playlist_link_parts(url: str) -> tuple[str, str] | None:
    """The (storefront, playlist id) a pasted link points at, or None.

    The id is the last path segment. The playlist's name in front of it is
    optional (`/us/playlist/pl.123` and `/us/playlist/top-100/pl.123` are pages
    for the same playlist), so a lone segment only counts as an id when it
    carries the `pl.` prefix Apple gives ids: `/us/playlist/top-100` is a page
    that still needs the rest of the link, and asking Apple for it is a 404.
    """
    text = url.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        # A link copied from somewhere that dropped the scheme (`music.apple.com/...`)
        # or a scheme-relative one (`//music.apple.com/...`) still names the playlist,
        # so it is read as if it had one. Refusing those told the user a link that
        # works is not a link; the host check below is what keeps other hosts out.
        parsed = urlparse(text if text.startswith("//") else f"//{text}")
    if (parsed.hostname or "").casefold() not in APPLE_LINK_HOSTS:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    lowered = [segment.casefold() for segment in segments]
    if "playlist" not in lowered:
        return None
    playlist_at = lowered.index("playlist")
    storefront = segments[playlist_at - 1].casefold() if playlist_at else "us"
    after_playlist = segments[playlist_at + 1 :]

    ids = [
        segment for segment in after_playlist if segment.casefold().startswith(PLAYLIST_ID_PREFIX)
    ]
    if ids:
        playlist_id = ids[-1]
    elif len(after_playlist) > 1:
        # No `pl.` segment to go on, but the id sits after the name.
        playlist_id = after_playlist[-1]
    else:
        return None

    if not STOREFRONT_PATTERN.fullmatch(storefront):
        storefront = "us"
    return storefront, playlist_id


def _fetch_playlist_page(url: str, controls: DownloadControls | None = None) -> str:
    return _fetch_text(url, controls=controls)


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


# Songs are numbered by their position in the playlist, and that position does
# not hold still: the user can add, drop or reorder tracks between two runs, and
# the reader itself can come back with a different order or length depending on
# whether it read the page, Apple's API, or the embedded HTML. The part of the
# filename after the number is the song's own name, so that is what a later run
# matches on when it asks "do I already have this one".
_NUMBERED_TRACK_NAME = re.compile(r"^\d+ - (?P<name>.+)$")


def _track_name_key(title: str, artist: str) -> str:
    """The `title - artist` part of a track's filename, used as a lookup key."""
    return f"{_safe_filename_part(title)} - {_safe_filename_part(artist)}".casefold()


def _tracks_in_folder(output_dir: Path, output_format: str) -> dict[str, Path]:
    """One pass over the save folder: `title - artist` -> the song downloaded for it.

    Only files the app itself writes count: a number, ` - `, then the song's
    name and the chosen format. An unfinished download (`song.webm.part`, a
    `.ytdl` sidecar, the source container ffmpeg never converted) and a file in
    the other format are not songs and stay out of the map, so a caller can
    still tell a partial download from a finished one.
    """
    found: dict[str, Path] = {}
    if not output_dir.is_dir():
        return found

    suffix = f".{output_format}"
    for candidate in sorted(output_dir.iterdir()):
        name = candidate.name
        if not name.casefold().endswith(suffix):
            continue
        match = _NUMBERED_TRACK_NAME.match(name)
        if match is None:
            continue
        song_name = match.group("name")
        if len(song_name) <= len(suffix):
            continue
        if not candidate.is_file():
            continue
        # First number wins, so the map is stable across runs and two entries of
        # the same song in one playlist agree on the file they point at.
        found.setdefault(song_name[: -len(suffix)].casefold(), candidate)
    return found


def _existing_track(
    tracks_in_folder: dict[str, Path],
    title: str,
    artist: str,
) -> Path | None:
    """The finished-looking file this song already has in the folder, if any."""
    return tracks_in_folder.get(_track_name_key(title, artist))


def _is_complete_audio_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 1024


def _remove_superseded_track_file(path: Path, log_callback: LogCallback | None = None) -> bool:
    """Drop a file that carries a song's name but is not a finished download.

    The file has this app's own numbering, this song's name and a size no audio
    file has, so it is the remains of an attempt that was cut off, not something
    the user put there. The run that decided to fetch the song again writes it
    under the number that run gives it, and a broken copy left next to the new
    one would sit in the folder for good.
    """
    try:
        # Missing counts as gone: another entry of the same song in one playlist
        # can have taken this copy's place already.
        path.unlink(missing_ok=True)
    except OSError as exc:
        if log_callback:
            log_callback(f"WARN  could not remove {path.name} ({exc})")
        return False
    if log_callback:
        log_callback(f"CLEAN  {path.name} (incomplete copy removed)")
    return True


# Containers yt-dlp can hand back for `bestaudio/best`. The app itself only
# ever writes mp3 or wav, so a file with a track's own name in one of these is
# an unfinished download, not a song.
SOURCE_CONTAINER_EXTS = frozenset(
    {"3gp", "aac", "flac", "m4a", "mka", "mkv", "mp4", "ogg", "opus", "ts", "weba", "webm"}
)


def _is_unfinished_track_name(rest: str, output_format: str) -> bool:
    """Does the part after `{stem}.` look like leftover download state?

    yt-dlp names its temp files after the target file: `song.webm.part` while
    downloading, `song.webm.part-Frag7` for fragmented sources, and a
    `song.webm.ytdl` info sidecar. When the ffmpeg step fails it leaves the
    source audio itself behind.
    """
    lowered = rest.lower()
    if lowered.endswith(".ytdl") or ".part" in lowered:
        return True
    # A bare source container (`song.webm`) has no further dot; a nested one
    # (`song.webm.part`) was already caught above.
    extension = lowered.rsplit(".", 1)[-1]
    return extension in SOURCE_CONTAINER_EXTS and extension != output_format


def _unfinished_track_files(
    output_dir: Path,
    output_format: str,
    title: str,
    artist: str,
) -> list[Path]:
    """Files left behind by an unfinished attempt at one track.

    The number in front of a filename is the position the song had when the
    attempt was made, so it belongs to that run and not to the song: a playlist
    that gained, lost or reordered tracks since then hands the song a different
    number now. What identifies the track is the `title - artist` part, so that
    is what is matched, the same way a finished song is matched. Only files that
    carry this song's exact name are considered, and the finished song
    (`{stem}.mp3` / `.wav`) is never included, so a stray partial can be cleared
    without touching what the user already has.
    """
    if not output_dir.is_dir():
        return []

    # `{title} - {artist}.` as it appears in a filename, so the rest of the name
    # is where the download state starts (`webm.part`, `m4a.ytdl`, `webm`).
    song_prefix = f"{_track_name_key(title, artist)}."
    leftovers: list[Path] = []
    for candidate in sorted(output_dir.iterdir()):
        if not candidate.is_file():
            continue
        numbered = _NUMBERED_TRACK_NAME.match(candidate.name)
        if numbered is None:
            continue
        song_name = numbered.group("name").casefold()
        if not song_name.startswith(song_prefix):
            continue
        if _is_unfinished_track_name(song_name[len(song_prefix) :], output_format):
            leftovers.append(candidate)
    return leftovers


def _remove_unfinished_track_files(
    output_dir: Path,
    output_format: str,
    title: str,
    artist: str,
    log_callback: LogCallback | None = None,
) -> list[Path]:
    """Delete this track's partial download so the folder only holds songs."""
    removed: list[Path] = []
    for path in _unfinished_track_files(output_dir, output_format, title, artist):
        try:
            path.unlink()
        except OSError as exc:
            if log_callback:
                log_callback(f"WARN  could not remove {path.name} ({exc})")
            continue
        removed.append(path)
        if log_callback:
            log_callback(f"CLEAN  {path.name} (unfinished download removed)")
    return removed


def _wait_if_paused(controls: DownloadControls | None) -> None:
    if controls and controls.cancel_event and controls.cancel_event.is_set():
        raise DownloadCancelled("Download cancelled.")
    while controls and controls.pause_event and controls.pause_event.is_set():
        if controls.cancel_event and controls.cancel_event.is_set():
            raise DownloadCancelled("Download cancelled.")
        time.sleep(CONTROL_POLL_SECONDS)


def _interruptible_sleep(seconds: float, controls: DownloadControls | None) -> None:
    """Wait in small steps so pause and cancel stay responsive during a backoff."""
    deadline = time.monotonic() + max(seconds, 0.0)
    while True:
        _wait_if_paused(controls)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(CONTROL_POLL_SECONDS, remaining))


def read_playlist_tracks(url: str, controls: DownloadControls | None = None) -> list[tuple[str, str]]:
    playlist_url = _canonical_playlist_url(url)

    try:
        html = _fetch_playlist_page(playlist_url, controls=controls)
    except DownloadCancelled:
        raise
    except Exception as exc:
        raise PlaylistDownloaderError(_fetch_error_message(exc)) from exc

    tracks = _extract_tracks_from_apple_json_scripts(html)
    token_candidates = (
        [] if tracks else _discover_musickit_tokens(html, page_url=playlist_url, controls=controls)
    )
    for token in token_candidates:
        _wait_if_paused(controls)
        try:
            tracks = _fetch_playlist_tracks_via_api(playlist_url, token=token, controls=controls)
        except DownloadCancelled:
            # The API walk is the longest part of a read, so a cancel raised
            # inside it has to stop the run, not be swallowed and answered with
            # "Could not read songs from that playlist".
            raise
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
    """Check every song in the playlist is in the folder, and write a report.

    A song counts as present when the folder holds a file with its name in the
    chosen format, whatever number that file carries: the numbers come from the
    position a song had when it was downloaded, and the playlist may well have
    been reordered since.
    """
    if output_format not in {"mp3", "wav"}:
        raise PlaylistDownloaderError("Unsupported output format.")

    output_dir.mkdir(parents=True, exist_ok=True)
    tracks = read_playlist_tracks(url)
    tracks_in_folder = _tracks_in_folder(output_dir, output_format)
    missing: list[VerificationItem] = []
    incomplete: list[VerificationItem] = []

    for index, (title, artist) in enumerate(tracks, start=1):
        expected_path = _track_output_path(output_dir, output_format, index, title, artist)
        downloaded = _existing_track(tracks_in_folder, title, artist)
        if downloaded is None:
            missing.append(VerificationItem(index, title, artist, expected_path, "missing"))
        elif not _is_complete_audio_file(downloaded):
            incomplete.append(VerificationItem(index, title, artist, downloaded, "incomplete"))

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


def _write_failed_tracks(
    output_dir: Path,
    failures: list[tuple[str, str]],
    log_callback: LogCallback | None = None,
) -> Path | None:
    """Keep the failure report equal to the failures of the run that ended.

    The save folder is opened automatically when a run finishes, and the
    "finished with warnings" dialog tells the user to look in this file, so a
    report left over from an earlier run reads as a lie: after "retry missing"
    fills every gap the file still lists the songs that now exist next to it.
    A run with nothing to report clears the stale file instead of leaving it,
    and a file that cannot be removed is reported rather than raised, because
    the run itself did succeed.
    """
    report_path = output_dir / FAILED_TRACKS_FILENAME
    if failures:
        lines = [f"{name} | {reason}" for name, reason in failures]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    if not report_path.exists():
        return None
    try:
        report_path.unlink()
    except OSError as exc:
        if log_callback:
            log_callback(f"WARN  could not remove {FAILED_TRACKS_FILENAME} ({exc})")
        return None
    if log_callback:
        log_callback(f"CLEAN  {FAILED_TRACKS_FILENAME} (no failed songs in this run)")
    return None


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


def _fetch_text(
    url: str,
    *,
    attempts: int | None = None,
    timeout: int | None = None,
    controls: DownloadControls | None = None,
) -> str:
    # Resolved here rather than in the signature so the module constants stay
    # the single source of truth (and can be tuned at runtime).
    attempts = FETCH_ATTEMPTS if attempts is None else attempts
    timeout = FETCH_TIMEOUT_SECONDS if timeout is None else timeout
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        _wait_if_paused(controls)
        req = Request(url, headers=_browser_headers())
        try:
            with urlopen(req, timeout=timeout) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUS:
                raise
            last_error = exc
        except Exception as exc:
            last_error = exc
        _interruptible_sleep(FETCH_RETRY_BACKOFF_SECONDS * (attempt + 1), controls)
    if last_error:
        raise last_error
    return ""


def _discover_musickit_tokens(
    html: str,
    page_url: str,
    controls: DownloadControls | None = None,
) -> list[str]:
    _wait_if_paused(controls)
    tokens: list[str] = []
    direct = _extract_musickit_token(html)
    if direct:
        tokens.append(direct)

    # Fallback 1: any JWT-looking strings in initial HTML.
    tokens.extend(_extract_jwt_candidates(html))

    # Fallback 2: scan linked JS bundles for JWT tokens. Best effort only, so a
    # bundle that is slow or missing must never turn into a long wait.
    script_urls = _extract_script_urls(html, page_url=page_url)
    for script_url in script_urls[:MAX_SCRIPT_BUNDLES]:
        if not script_url.endswith(".js"):
            continue
        _wait_if_paused(controls)
        try:
            js = _fetch_text(
                script_url,
                attempts=BEST_EFFORT_FETCH_ATTEMPTS,
                timeout=BEST_EFFORT_FETCH_TIMEOUT_SECONDS,
                controls=controls,
            )
        except DownloadCancelled:
            raise
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


def _fetch_playlist_tracks_via_api(
    url: str,
    token: str,
    controls: DownloadControls | None = None,
) -> list[tuple[str, str]]:
    """Read the playlist from Apple's API, one page at a time.

    This fallback only runs when the playlist page did not carry the track list,
    and a big playlist can take several pages to walk, so pause and cancel are
    checked before every page instead of once for the whole read.
    """
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
        _wait_if_paused(controls)
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
        with urlopen(req, timeout=API_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310
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
    playlist_url = _canonical_playlist_url(url)
    if output_format not in {"mp3", "wav"}:
        raise PlaylistDownloaderError("Unsupported output format.")

    output_dir.mkdir(parents=True, exist_ok=True)

    log_callback("Reading Apple Music playlist...")
    tracks = read_playlist_tracks(playlist_url, controls=controls)

    total = len(tracks)
    log_callback(f"Found {total} song(s). Starting downloads...")
    failures: list[tuple[str, str]] = []

    # One look at the folder before the run, so "skip songs already downloaded"
    # follows the song's name and not the number it was saved under.
    tracks_in_folder = _tracks_in_folder(output_dir, output_format) if skip_existing else {}

    for index, (title, artist) in enumerate(tracks, start=1):
        _wait_if_paused(controls)
        track_name = f"{title} - {artist}"
        progress_callback(index, total, track_name)

        expected_path = _track_output_path(output_dir, output_format, index, title, artist)
        if skip_existing:
            already_have = _existing_track(tracks_in_folder, title, artist)
            if already_have is not None:
                if _is_complete_audio_file(already_have):
                    if already_have == expected_path:
                        log_callback(f"SKIP  {title} - {artist} (already downloaded)")
                    else:
                        log_callback(f"SKIP  {title} - {artist} (already downloaded as {already_have.name})")
                    continue
                # Something carries this song's name and is not a song, so this
                # run fetches it again. Clear the broken copy first: it was
                # written under an older number, and the new download lands under
                # the number this run gives the song.
                _remove_superseded_track_file(already_have, log_callback)

        log_callback(f"... {title} - {artist} (searching/downloading...)")
        # A run that was killed, or an earlier cancelled attempt, can leave this
        # track's partial download behind. Clearing it first means the retry
        # starts clean instead of resuming a fragment that may belong to a
        # different video than the one this attempt picks. A part written by an
        # earlier run can sit under a different number than the song has now, so
        # this looks the track up by name rather than by number.
        _remove_unfinished_track_files(output_dir, output_format, title, artist, log_callback)

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
            if skip_existing:
                # A playlist can list the same song twice. The second entry must
                # see the copy this run just wrote, not fetch the song again.
                tracks_in_folder[_track_name_key(title, artist)] = expected_path
        except DownloadCancelled:
            _remove_unfinished_track_files(output_dir, output_format, title, artist, log_callback)
            raise
        except Exception as exc:
            # DownloadError included: either way the track did not land, so it
            # is reported to the user and retried later.
            reason = str(exc)
            failures.append((track_name, reason))
            error_callback(track_name, reason)
            log_callback(f"WARN  {title} - {artist} (failed)")
            # Whatever the failed attempt wrote is not a song: drop it.
            _remove_unfinished_track_files(output_dir, output_format, title, artist, log_callback)

    _write_failed_tracks(output_dir, failures, log_callback)
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
