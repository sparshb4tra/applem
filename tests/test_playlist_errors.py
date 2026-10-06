"""Checks for how playlist fetch failures get reported to the user.

Runs fully offline: `urlopen` is swapped for a fake that raises the same errors
the real Apple endpoints raise, so nothing here needs internet access.

From the repo root:

    python -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import PlaylistDownloaderError, read_playlist_tracks

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"

SONG_PAGE = (
    "<html><body><script>"
    '{"resourceType": "song", "id": "1", "title": "Test Song", '
    '"artistName": "Test Artist", "contentDescriptor": {"kind": "song"}}'
    "</script></body></html>"
)


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class PlaylistFetchErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep retryable status checks instant.
        self._restore("FETCH_ATTEMPTS", 2)
        self._restore("FETCH_RETRY_BACKOFF_SECONDS", 0.0)
        self.calls: list[str] = []

    def _restore(self, name: str, value: object) -> None:
        self.addCleanup(setattr, downloader, name, getattr(downloader, name))
        setattr(downloader, name, value)

    def _patch_urlopen(self, *, error: Exception | None = None, body: str = "") -> None:
        def fake_urlopen(req: object, timeout: object = None) -> _FakeResponse:
            self.calls.append(getattr(req, "full_url", str(req)))
            if error is not None:
                raise error
            return _FakeResponse(body)

        self.addCleanup(setattr, downloader, "urlopen", downloader.urlopen)
        downloader.urlopen = fake_urlopen

    def _http_error(self, code: int) -> HTTPError:
        return HTTPError(PLAYLIST_URL, code, "test", None, None)

    def _read_error(self) -> PlaylistDownloaderError:
        with self.assertRaises(PlaylistDownloaderError) as caught:
            read_playlist_tracks(PLAYLIST_URL)
        return caught.exception

    def test_missing_playlist_is_not_blamed_on_the_network(self) -> None:
        self._patch_urlopen(error=self._http_error(404))
        message = str(self._read_error())
        self.assertIn("404", message)
        self.assertNotIn("internet", message.lower())
        self.assertNotIn("wi-fi", message.lower())
        # A dead link will never come back to life on the next attempt.
        self.assertEqual(len(self.calls), 1)

    def test_private_or_region_locked_playlist_explains_itself(self) -> None:
        self._patch_urlopen(error=self._http_error(403))
        message = str(self._read_error())
        self.assertIn("403", message)
        self.assertIn("private", message)
        self.assertNotIn("internet", message.lower())

    def test_rate_limiting_is_retried_then_reported(self) -> None:
        self._patch_urlopen(error=self._http_error(429))
        message = str(self._read_error())
        self.assertIn("rate limiting", message)
        self.assertEqual(len(self.calls), downloader.FETCH_ATTEMPTS)

    def test_server_error_is_retried_then_reported(self) -> None:
        self._patch_urlopen(error=self._http_error(503))
        message = str(self._read_error())
        self.assertIn("server problem", message)
        self.assertEqual(len(self.calls), downloader.FETCH_ATTEMPTS)

    def test_real_offline_still_says_offline(self) -> None:
        self._patch_urlopen(error=URLError(ConnectionRefusedError(10061, "refused")))
        message = str(self._read_error())
        self.assertEqual(message, "No internet connection. Check your Wi-Fi and try again.")

    def test_timeout_gets_its_own_message(self) -> None:
        self._patch_urlopen(error=URLError(TimeoutError("timed out")))
        message = str(self._read_error())
        self.assertIn("took too long", message)
        self.assertNotIn("internet", message.lower())

    def test_unknown_failure_keeps_the_generic_message(self) -> None:
        self._patch_urlopen(error=RuntimeError("weird"))
        message = str(self._read_error())
        self.assertIn("Could not open playlist page", message)

    def test_original_error_is_kept_as_the_cause(self) -> None:
        error = self._http_error(404)
        self._patch_urlopen(error=error)
        self.assertIs(self._read_error().__cause__, error)

    def test_working_playlist_still_reads_tracks(self) -> None:
        self._patch_urlopen(body=SONG_PAGE)
        tracks = read_playlist_tracks(PLAYLIST_URL)
        self.assertEqual(tracks, [("Test Song", "Test Artist")])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
