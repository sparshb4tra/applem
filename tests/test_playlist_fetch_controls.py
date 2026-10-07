"""Checks that reading a playlist obeys pause/cancel and stays bounded.

Reading the playlist can take a while: the page fetch retries, and when the
page does not carry the track list the reader scans up to 20 linked JS bundles
looking for a MusicKit token. None of that used to look at the pause or cancel
buttons, and every bundle fetch used the full page-level retry budget, so a
slow or blocked CDN could hold a cancelled run for many minutes.

Runs fully offline: `urlopen` is replaced with a fake that records every fetch,
so the scan can be measured without touching Apple.

From the repo root:

    python -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.error import URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import (
    DownloadCancelled,
    DownloadControls,
    PlaylistDownloaderError,
    read_playlist_tracks,
)

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"

# No track JSON and no token anywhere, so the reader has to fall back to
# scanning the linked bundles and then give up with a user-facing error.
PAGE_WITHOUT_TRACKS = (
    "<html><head>"
    '<script src="/bundle-one.js"></script>'
    '<script src="/bundle-two.js"></script>'
    "</head><body></body></html>"
)
BUNDLE_COUNT = 2


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class PlaylistFetchControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetches: list[tuple[str, int | None]] = []
        self.cancel_event = threading.Event()
        self._restore("FETCH_RETRY_BACKOFF_SECONDS", 0.0)

    def _restore(self, name: str, value: object) -> None:
        self.addCleanup(setattr, downloader, name, getattr(downloader, name))
        setattr(downloader, name, value)

    def _patch_urlopen(
        self,
        *,
        body: str = "",
        error: Exception | None = None,
        bundle_error: Exception | None = None,
        after_fetch: object = None,
    ) -> None:
        def fake_urlopen(req: object, timeout: object = None) -> _FakeResponse:
            url = getattr(req, "full_url", str(req))
            self.fetches.append((url, timeout))
            if after_fetch is not None:
                after_fetch(len(self.fetches))  # type: ignore[operator]
            if url.endswith(".js"):
                if bundle_error is not None:
                    raise bundle_error
            elif error is not None:
                raise error
            return _FakeResponse(body)

        self.addCleanup(setattr, downloader, "urlopen", downloader.urlopen)
        downloader.urlopen = fake_urlopen

    def _controls(self) -> DownloadControls:
        return DownloadControls(cancel_event=self.cancel_event)

    def test_already_cancelled_run_never_touches_the_network(self) -> None:
        self._patch_urlopen(body=PAGE_WITHOUT_TRACKS)
        self.cancel_event.set()
        with self.assertRaises(DownloadCancelled):
            read_playlist_tracks(PLAYLIST_URL, controls=self._controls())
        self.assertEqual(self.fetches, [])

    def test_cancel_during_a_retry_wait_stops_without_waiting_it_out(self) -> None:
        # The page fetch drops, and the user cancels while the app would
        # otherwise be backing off for another attempt.
        self._restore("FETCH_RETRY_BACKOFF_SECONDS", 5.0)
        self._patch_urlopen(
            error=URLError(ConnectionRefusedError(10061, "refused")),
            after_fetch=lambda count: self.cancel_event.set() if count == 1 else None,
        )
        started = time.monotonic()
        with self.assertRaises(DownloadCancelled):
            read_playlist_tracks(PLAYLIST_URL, controls=self._controls())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(self.fetches), 1)

    def test_cancel_during_the_token_scan_stops_the_scan(self) -> None:
        self._patch_urlopen(
            body=PAGE_WITHOUT_TRACKS,
            after_fetch=lambda count: self.cancel_event.set() if count == 1 else None,
        )
        with self.assertRaises(DownloadCancelled):
            read_playlist_tracks(PLAYLIST_URL, controls=self._controls())
        self.assertEqual(len(self.fetches), 1)

    def test_token_scan_uses_the_short_best_effort_budget(self) -> None:
        self._patch_urlopen(body=PAGE_WITHOUT_TRACKS)
        with self.assertRaises(PlaylistDownloaderError):
            read_playlist_tracks(PLAYLIST_URL)

        page, *bundles = self.fetches
        self.assertEqual(page[1], downloader.FETCH_TIMEOUT_SECONDS)
        self.assertEqual(len(bundles), BUNDLE_COUNT)
        self.assertEqual(
            [timeout for _, timeout in bundles],
            [downloader.BEST_EFFORT_FETCH_TIMEOUT_SECONDS] * BUNDLE_COUNT,
        )
        # Bundles must not inherit the page-level retry budget.
        self.assertLess(len(self.fetches), 1 + BUNDLE_COUNT * downloader.FETCH_ATTEMPTS)

    def test_a_single_dead_bundle_does_not_retry(self) -> None:
        self._patch_urlopen(
            body=PAGE_WITHOUT_TRACKS,
            bundle_error=URLError(ConnectionRefusedError(10061, "refused")),
        )
        with self.assertRaises(PlaylistDownloaderError) as caught:
            read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(len(self.fetches), 1 + BUNDLE_COUNT)
        self.assertIn("Could not read songs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
