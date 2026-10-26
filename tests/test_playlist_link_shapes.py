"""Checks that every shape of an Apple Music playlist link reads the playlist.

A link reaches the app in whatever shape it was copied in, and the reader used
to hand it to Apple exactly as pasted. Some of those shapes are answered badly
for a playlist that is public and alive: a trailing slash is a hard 404, and
the embed host serves a stub with no track list. The old check only looked at
the text of the link, so `/us/playlist/name/pl.123/` was accepted and then
reported as "Apple could not find that playlist page (404)", while a legacy
`itunes.apple.com` link or the `geo.` share link was refused outright with
"that doesn't look like an Apple Music link" even though Apple serves the same
playlist for both. A link copied from somewhere that showed it without the
`https://` was refused the same way.

The reader now turns the pasted text into the canonical page URL before it
fetches anything, and the MusicKit fallback walks that same address, so the id
and storefront from the link are the ones asked for.

Runs fully offline: `urlopen` is replaced with a fake that records every fetch
and serves a page with one song in it.

From the repo root:

    python -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import (
    PlaylistDownloaderError,
    _track_output_path,
    download_playlist,
    read_playlist_tracks,
)

PLAYLIST_ID = "pl.123"
CANONICAL_URL = f"https://music.apple.com/us/playlist/{PLAYLIST_ID}"
API_BASE = "https://amp-api.music.apple.com"
API_FIRST_URL = f"{API_BASE}/v1/catalog/us/playlists/{PLAYLIST_ID}?include=tracks&limit[tracks]=300"

PAGE_WITH_SONG = (
    "<html><body><script>"
    '{"resourceType": "song", "id": "1", "title": "Test Song", '
    '"artistName": "Test Artist", "contentDescriptor": {"kind": "song"}}'
    "</script></body></html>"
)

# A page that carries a token but no song objects, so the reader has to fall
# back to Apple's API.
TOKEN = "eyJhbG...dXJl"
PAGE_WITH_TOKEN = (
    "<html><head>"
    f'<script type="application/json">{{"token":"{TOKEN}"}}</script>'
    "</head><body></body></html>"
)
API_PAGE_ONE = json.dumps(
    {
        "data": [{"id": PLAYLIST_ID, "type": "playlists", "relationships": {"tracks": {}}}],
        "included": [{"type": "songs", "attributes": {"name": "Song One", "artistName": "Artist One"}}],
    }
)
EXPECTED_TRACKS = [("Test Song", "Test Artist")]

# The same playlist, copied in every shape a user can end up with.
LINK_SHAPES = {
    "the modern page": CANONICAL_URL,
    "with the name segment": f"https://music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "with a trailing slash": f"https://music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}/",
    "short form with a trailing slash": f"{CANONICAL_URL}/",
    "with tracking parameters": f"https://music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}?l=en-US&at=1000l",
    "over http": f"http://music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "from itunes.apple.com": f"https://itunes.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "from the geo. host": f"https://geo.music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "from the embed host": f"https://embed.music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "with a shouty host": f"https://MUSIC.APPLE.COM/us/playlist/test-playlist/{PLAYLIST_ID}",
    "with a shouty storefront": f"https://music.apple.com/US/playlist/test-playlist/{PLAYLIST_ID}",
    "with no storefront": f"https://music.apple.com/playlist/test-playlist/{PLAYLIST_ID}",
    "with spaces around it": f"  https://music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}  ",
    "without its scheme": f"music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
    "without its scheme or the name": f"music.apple.com/us/playlist/{PLAYLIST_ID}",
    "as a scheme-relative address": f"//music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}",
}


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class PlaylistLinkShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetches: list[str] = []
        self.page_body = PAGE_WITH_SONG

        def fake_urlopen(req: object, timeout: object = None) -> _FakeResponse:
            url = getattr(req, "full_url", str(req))
            self.fetches.append(url)
            if "amp-api.music.apple.com" in url:
                return _FakeResponse(API_PAGE_ONE)
            return _FakeResponse(self.page_body)

        self.addCleanup(setattr, downloader, "urlopen", downloader.urlopen)
        downloader.urlopen = fake_urlopen

    def _page_url(self, link: str) -> str:
        read_playlist_tracks(link)
        return self.fetches[0]

    def test_every_shape_of_a_pasted_link_reads_the_same_page(self) -> None:
        for label, link in LINK_SHAPES.items():
            with self.subTest(shape=label):
                self.fetches.clear()
                self.assertEqual(self._page_url(link), CANONICAL_URL)

    def test_the_songs_come_back_from_that_page(self) -> None:
        self.assertEqual(read_playlist_tracks(f"{CANONICAL_URL}/"), EXPECTED_TRACKS)

    def test_a_trailing_slash_never_reaches_apple(self) -> None:
        # Apple answers a trailing slash with a 404, which used to stop the run
        # with a message about the playlist not existing.
        self._page_url(f"{CANONICAL_URL}/")
        self.assertFalse(any(fetch.endswith("/") for fetch in self.fetches))

    def test_a_storefront_in_the_link_is_kept(self) -> None:
        # The storefront decides which catalogue the tracks come from, so it has
        # to survive the rewrite whether or not the link carried its scheme.
        for link in (
            f"https://music.apple.com/in/playlist/test-playlist/{PLAYLIST_ID}",
            f"music.apple.com/in/playlist/test-playlist/{PLAYLIST_ID}",
        ):
            with self.subTest(link=link):
                self.fetches.clear()
                self.assertEqual(
                    self._page_url(link), f"https://music.apple.com/in/playlist/{PLAYLIST_ID}"
                )

    def test_an_unusable_storefront_falls_back_to_the_us_page(self) -> None:
        fetched = self._page_url(f"https://music.apple.com/playlist-name/playlist/{PLAYLIST_ID}")
        self.assertEqual(fetched, CANONICAL_URL)

    def test_the_api_fallback_walks_the_canonical_link(self) -> None:
        # The token loop and the paged API read used to be handed the pasted
        # text, so the storefront and id had to be recoverable from it.
        self.page_body = PAGE_WITH_TOKEN
        tracks = read_playlist_tracks(f"https://itunes.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}/")

        self.assertEqual(tracks, [("Song One", "Artist One")])
        self.assertEqual(self.fetches[0], CANONICAL_URL)
        self.assertEqual([fetch for fetch in self.fetches if "amp-api" in fetch], [API_FIRST_URL])

    def test_a_name_only_link_asks_for_the_whole_link(self) -> None:
        # `/us/playlist/top-100` is not a playlist page: Apple needs the id, so
        # the user is told to copy the whole link instead of being told Apple
        # could not find a playlist that exists.
        name_only_links = (
            "https://music.apple.com/us/playlist/top-100",
            "https://music.apple.com/us/playlist/top-100/",
            "https://itunes.apple.com/us/playlist/top-100/",
        )
        for link in name_only_links:
            with self.subTest(link=link):
                with self.assertRaises(PlaylistDownloaderError) as caught:
                    read_playlist_tracks(link)
                self.assertIn("playlist link", str(caught.exception))
                self.assertEqual(self.fetches, [])

    def test_a_link_to_something_else_is_refused_without_a_fetch(self) -> None:
        not_playlists = (
            "https://music.apple.com/us/album/test-album/123456",
            "https://music.apple.com/us/playlist/",
            "https://www.youtube.com/playlist?list=PL123",
            "https://music.apple.com.evil.example/us/playlist/pl.123",
            "music.apple.com.evil.example/us/playlist/pl.123",
            "example.com/us/playlist/pl.123",
            "ftp://music.apple.com/us/playlist/pl.123",
            "music.apple.com/us/playlist/top-100",
            "not a link at all",
            "",
        )
        for link in not_playlists:
            with self.subTest(link=link):
                with self.assertRaises(PlaylistDownloaderError):
                    read_playlist_tracks(link)
                self.assertEqual(self.fetches, [])

    def test_a_download_reads_the_canonical_page_too(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(
            setattr, downloader, "_download_track_from_youtube", downloader._download_track_from_youtube
        )

        def fake_download(**kwargs: object) -> None:
            # A run only counts a track as done when its file is there, so the
            # stand-in has to write one: this test is about which URL the
            # download reads, not about what yt-dlp leaves behind.
            _track_output_path(
                Path(kwargs["output_dir"]),
                str(kwargs["output_format"]),
                int(kwargs["index"]),
                str(kwargs["title"]),
                str(kwargs["artist"]),
            ).write_bytes(b"m" * 2048)

        downloader._download_track_from_youtube = fake_download  # type: ignore[assignment]
        failures = download_playlist(
            url=f"https://geo.music.apple.com/us/playlist/test-playlist/{PLAYLIST_ID}/",
            output_dir=Path(tmp.name),
            progress_callback=lambda current, total, name: None,
            log_callback=lambda line: None,
            error_callback=lambda track, reason: None,
        )

        self.assertEqual(failures, [])
        self.assertEqual(self.fetches[0], CANONICAL_URL)


if __name__ == "__main__":
    unittest.main()
