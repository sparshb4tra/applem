"""Checks that the MusicKit API fallback obeys pause and cancel.

The playlist page usually carries the track list already, but when it does not
the reader hunts for a MusicKit token and asks Apple's API instead. That request
pages through a long playlist a few hundred tracks at a time, and it used to be
the one part of the reader that never looked at the stop button: a cancelled
read kept pulling pages until the playlist ran out, and the token loop around it
swallowed the cancel and ended with "Could not read songs from that playlist"
instead of stopping.

Runs fully offline: `urlopen` is replaced with a fake that serves a token-only
playlist page plus a two-page API response.

From the repo root:

    python3 -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import DownloadCancelled, DownloadControls, read_playlist_tracks

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
API_BASE = "https://amp-api.music.apple.com"
API_FIRST_URL = f"{API_BASE}/v1/catalog/us/playlists/pl.123?include=tracks&limit[tracks]=300"
NEXT_PAGE_PATH = "/v1/catalog/us/playlists/pl.123/tracks?offset=300&limit=300"

TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJhcHBsZSJ9.c2lnbmF0dXJl"

# A page with a token but no song objects, so the reader has to fall back to the
# API. No `<script src=...>` either, which keeps the bundle scan out of the way.
PAGE_WITH_TOKEN = (
    "<html><head>"
    f'<script type="application/json">{{"token":"{TOKEN}"}}</script>'
    "</head><body></body></html>"
)

PAGE_ONE = json.dumps(
    {
        "data": [
            {
                "id": "pl.123",
                "type": "playlists",
                "relationships": {"tracks": {"next": NEXT_PAGE_PATH}},
            }
        ],
        "included": [{"type": "songs", "attributes": {"name": "Song One", "artistName": "Artist One"}}],
    }
)
PAGE_TWO = json.dumps(
    {
        "data": [],
        "included": [{"type": "songs", "attributes": {"name": "Song Two", "artistName": "Artist Two"}}],
    }
)
EXPECTED_TRACKS = [("Song One", "Artist One"), ("Song Two", "Artist Two")]


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class ApiPaginationControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetches: list[tuple[str, int | None]] = []
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.on_fetch = None
        self._timers: list[threading.Timer] = []

    def tearDown(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self.pause_event.clear()

    def _is_api(self, url: str) -> bool:
        return "amp-api.music.apple.com" in url

    def _patch_urlopen(self) -> None:
        def fake_urlopen(req: object, timeout: int | None = None) -> _FakeResponse:
            url = getattr(req, "full_url", str(req))
            self.fetches.append((url, timeout))
            if self.on_fetch is not None:
                self.on_fetch(url, len(self.fetches))  # type: ignore[operator]
            if self._is_api(url):
                return _FakeResponse(PAGE_ONE if "offset=300" not in url else PAGE_TWO)
            return _FakeResponse(PAGE_WITH_TOKEN)

        self.addCleanup(setattr, downloader, "urlopen", downloader.urlopen)
        downloader.urlopen = fake_urlopen

    def _controls(self) -> DownloadControls:
        return DownloadControls(pause_event=self.pause_event, cancel_event=self.cancel_event)

    def _api_fetches(self) -> list[tuple[str, int | None]]:
        return [fetch for fetch in self.fetches if self._is_api(fetch[0])]

    def test_cancel_during_a_paged_read_stops_before_the_next_page(self) -> None:
        # The user cancels while the first page of a long playlist is coming in.
        self._patch_urlopen()

        def cancel_after_first_api_page(url: str, count: int) -> None:
            if self._is_api(url):
                self.cancel_event.set()

        self.on_fetch = cancel_after_first_api_page
        started = time.monotonic()
        with self.assertRaises(DownloadCancelled):
            read_playlist_tracks(PLAYLIST_URL, controls=self._controls())

        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(self._api_fetches()), 1, "a cancelled read must not pull another page")

    def test_an_already_cancelled_run_never_asks_the_api(self) -> None:
        self._patch_urlopen()
        self.cancel_event.set()
        with self.assertRaises(DownloadCancelled):
            read_playlist_tracks(PLAYLIST_URL, controls=self._controls())
        self.assertEqual(self._api_fetches(), [])

    def test_pause_holds_the_read_before_the_next_api_page(self) -> None:
        self._patch_urlopen()

        def pause_after_first_api_page(url: str, count: int) -> None:
            if not self._is_api(url):
                return
            self.pause_event.set()
            timer = threading.Timer(0.3, self.pause_event.clear)
            timer.daemon = True
            timer.start()
            self._timers.append(timer)

        self.on_fetch = pause_after_first_api_page
        started = time.monotonic()
        tracks = read_playlist_tracks(PLAYLIST_URL, controls=self._controls())

        self.assertGreaterEqual(time.monotonic() - started, 0.25, "a paused read must wait")
        self.assertEqual(tracks, EXPECTED_TRACKS)

    def test_the_api_fallback_still_reads_every_page(self) -> None:
        self._patch_urlopen()
        tracks = read_playlist_tracks(PLAYLIST_URL, controls=self._controls())

        self.assertEqual(tracks, EXPECTED_TRACKS)
        self.assertEqual([url for url, _ in self._api_fetches()], [API_FIRST_URL, API_BASE + NEXT_PAGE_PATH])
        self.assertEqual(
            [timeout for _, timeout in self._api_fetches()],
            [downloader.API_FETCH_TIMEOUT_SECONDS] * 2,
            "the API fallback must stay on the app's own timeout",
        )


if __name__ == "__main__":
    unittest.main()
