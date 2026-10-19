"""Checks that the MusicKit API fallback can actually read a playlist's songs.

The playlist page usually carries the track list already. When it does not, the
reader hunts for a MusicKit token and asks Apple's API instead, and that read
only ever looked for the songs in a top level `included` list. Apple does not
answer that way: the playlist resource carries its tracks inline under
`relationships.tracks.data`, and the pages a pagination link leads to are track
collections whose own top level `data` holds the songs. Both shapes came back
with no songs at all, so a playlist whose page did not carry the track list was
answered with "Could not read songs from that playlist" - the one case this read
exists for - and the multi page walk for a long playlist never ran.

Each shape is read from the place it keeps the track list, so a song that turns
up elsewhere in a response is not mistaken for a track of the playlist.

Runs fully offline: `urlopen` is replaced with a fake that serves a token only
playlist page plus the API pages under test.

From the repo root:

    python -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import read_playlist_tracks

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
API_BASE = "https://amp-api.music.apple.com"
API_FIRST_URL = f"{API_BASE}/v1/catalog/us/playlists/pl.123?include=tracks&limit[tracks]=300"

TOKEN = "eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJl"

# A page with a token and no song objects, so the reader has to fall back to the
# API. No `<script src=...>` either, which keeps the bundle scan out of the way.
PAGE_WITH_TOKEN = (
    "<html><head>"
    f'<script type="application/json">{{"token":"{TOKEN}"}}</script>'
    "</head><body></body></html>"
)

# A page that does carry the track list, so no API read is needed at all.
PAGE_WITH_SONGS = (
    "<html><head><script type=\"application/json\">"
    '{"data":[{"type":"songs","title":"Page Song","artistName":"Page Artist",'
    '"contentDescriptor":{"kind":"song"}}]}'
    "</script></head><body></body></html>"
)


def song(name: str, artist: str, kind: str = "songs") -> dict:
    return {"type": kind, "attributes": {"name": name, "artistName": artist}}


def page_first(songs: list[dict], next_path: str | None = None) -> str:
    """The playlist resource, with its tracks inline, as Apple sends it."""
    tracks: dict = {"data": songs}
    if next_path:
        tracks["next"] = next_path
    return json.dumps(
        {"data": [{"id": "pl.123", "type": "playlists", "relationships": {"tracks": tracks}}]}
    )


def page_of_tracks(songs: list[dict], next_path: str | None = None) -> str:
    """A page of the playlist's `tracks` relationship, as Apple sends it."""
    payload: dict = {"data": songs}
    if next_path:
        payload["next"] = next_path
    return json.dumps(payload)


class ApiTrackShapeTests(unittest.TestCase):
    """Where the songs are read from in the API's own responses."""

    def setUp(self) -> None:
        self.fetches: list[str] = []
        self.pages: list[str] = [PAGE_WITH_TOKEN]
        self.api_pages: list[str] = []
        self._patch_urlopen()

    def _patch_urlopen(self) -> None:
        class FakeResponse:
            def __init__(self, body: str) -> None:
                self._body = body.encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc_info: object) -> bool:
                return False

        def fake_urlopen(req: object, timeout: int | None = None) -> FakeResponse:
            url = getattr(req, "full_url", str(req))
            self.fetches.append(url)
            if "amp-api.music.apple.com" in url:
                index = len([fetch for fetch in self.fetches if "amp-api" in fetch]) - 1
                return FakeResponse(self.api_pages[min(index, len(self.api_pages) - 1)])
            return FakeResponse(self.pages[-1])

        self.addCleanup(setattr, downloader, "urlopen", downloader.urlopen)
        downloader.urlopen = fake_urlopen

    def _api_fetches(self) -> list[str]:
        return [fetch for fetch in self.fetches if "amp-api.music.apple.com" in fetch]

    def test_the_songs_come_from_the_playlist_resource(self) -> None:
        # What Apple answers with today: no `included` list at all, the tracks
        # sitting inline under the playlist's own `tracks` relationship.
        self.api_pages = [
            page_first([song("Song One", "Artist One"), song("Song Two", "Artist Two")])
        ]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(tracks, [("Song One", "Artist One"), ("Song Two", "Artist Two")])
        self.assertEqual(self._api_fetches(), [API_FIRST_URL])

    def test_a_long_playlist_is_read_page_by_page(self) -> None:
        next_path = "/v1/catalog/us/playlists/pl.123/tracks?offset=2"
        after_that = f"{API_BASE}/v1/catalog/us/playlists/pl.123/tracks?offset=4"
        self.api_pages = [
            page_first([song("Song One", "Artist One"), song("Song Two", "Artist Two")], next_path),
            # A page of the relationship: the songs are the response itself, and
            # it links on through a `next` of its own, sometimes absolute.
            page_of_tracks([song("Song Three", "Artist Three")], after_that),
            page_of_tracks([song("Song Four", "Artist Four")]),
        ]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(
            tracks,
            [
                ("Song One", "Artist One"),
                ("Song Two", "Artist Two"),
                ("Song Three", "Artist Three"),
                ("Song Four", "Artist Four"),
            ],
        )
        self.assertEqual(self._api_fetches(), [API_FIRST_URL, API_BASE + next_path, after_that])

    def test_a_song_from_another_section_is_not_a_track_of_the_playlist(self) -> None:
        # Only the `tracks` relationship is the playlist, whatever else the
        # response hangs off the playlist resource.
        playlist = {
            "id": "pl.123",
            "type": "playlists",
            "relationships": {
                "tracks": {"data": [song("Real Song", "Real Artist")]},
                "similar": {"data": [song("Some Other Song", "Some Other Artist")]},
            },
        }
        self.api_pages = [json.dumps({"data": [playlist]})]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(tracks, [("Real Song", "Real Artist")])

    def test_the_older_included_shape_is_still_read(self) -> None:
        # A JSON:API shaped response keeps them in `included` instead, and that
        # still has to work.
        self.api_pages = [
            json.dumps(
                {
                    "data": [
                        {
                            "id": "pl.123",
                            "type": "playlists",
                            "relationships": {
                                "tracks": {"next": "/v1/catalog/us/playlists/pl.123/tracks?offset=1"}
                            },
                        }
                    ],
                    "included": [song("Song One", "Artist One")],
                }
            ),
            json.dumps({"data": [], "included": [song("Song Two", "Artist Two")]}),
        ]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(tracks, [("Song One", "Artist One"), ("Song Two", "Artist Two")])

    def test_a_song_without_a_title_or_artist_is_not_a_track(self) -> None:
        self.api_pages = [
            page_first(
                [
                    song("Song One", "Artist One"),
                    {"type": "songs", "attributes": {"name": "No Artist"}},
                    {"type": "songs", "attributes": {"artistName": "No Title"}},
                    {"type": "songs", "attributes": {}},
                ]
            )
        ]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(tracks, [("Song One", "Artist One")])

    def test_a_page_that_carries_the_tracks_is_not_asked_for_over_the_api(self) -> None:
        self.pages = [PAGE_WITH_SONGS]

        tracks = read_playlist_tracks(PLAYLIST_URL)

        self.assertEqual(tracks, [("Page Song", "Page Artist")])
        self.assertEqual(self._api_fetches(), [], "the API is a fallback, not the first read")


if __name__ == "__main__":
    unittest.main()
