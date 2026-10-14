"""Checks that a song is recognised by its name, not by its playlist number.

Files are numbered by the position a song had in the playlist when it was
downloaded (`001 - song - artist.mp3`), and that position does not hold still.
A playlist gains, drops and reorders tracks between two runs, and the reader
itself can come back with a different order or length depending on whether it
read the playlist page, Apple's API, or the embedded HTML.

Both the "skip songs already downloaded" check and "verify downloads" used to
compare against `{index} - {title} - {artist}.{format}` for the *current* index
only, so after any reorder every song looked missing: a retry re-downloaded the
whole playlist under brand new numbers, leaving duplicates next to the originals,
and verification reported a full folder as empty. The number is now ignored when
asking "do I already have this song".

One more thing followed from that: a song the folder only holds a cut off copy
of gets fetched again under the number this run gives it, so the remains of the
earlier attempt go with it rather than staying beside the fresh file, and a song
a playlist lists twice is fetched once.

Runs fully offline: the playlist reader and the YouTube download are swapped for
fakes. No network, no ffmpeg, no real downloads.

From the repo root:

    python -m unittest discover -s tests -v

Requires yt-dlp to be importable, because core.downloader imports it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import (
    _existing_track,
    _track_name_key,
    _track_output_path,
    _tracks_in_folder,
    download_playlist,
    verify_playlist_downloads,
)

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
TRACKS = [("Test Song", "Test Artist"), ("Test Song Two", "Test Artist")]
COMPLETE = b"audio" * 2048


class FolderIndexTests(unittest.TestCase):
    """What counts as a song already sitting in the folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)

    def _write(self, name: str, payload: bytes = COMPLETE) -> Path:
        path = self.output_dir / name
        path.write_bytes(payload)
        return path

    def _song(self, index: int, title: str = "Test Song", artist: str = "Test Artist") -> Path:
        stem = downloader._track_file_stem(index, title, artist)
        return self._write(f"{stem}.mp3")

    def test_a_song_is_found_under_the_number_of_an_earlier_run(self) -> None:
        song = self._song(7)
        found = _tracks_in_folder(self.output_dir, "mp3")
        self.assertEqual(_existing_track(found, "Test Song", "Test Artist"), song)

    def test_a_song_that_is_not_in_the_folder_is_not_found(self) -> None:
        self._song(7)
        found = _tracks_in_folder(self.output_dir, "mp3")
        self.assertIsNone(_existing_track(found, "Test Song Two", "Test Artist"))
        self.assertIsNone(_existing_track(found, "Test Song", "Another Artist"))

    def test_a_song_of_the_other_format_is_not_a_match(self) -> None:
        self._write("001 - Test Song - Test Artist.wav")
        self.assertEqual(_tracks_in_folder(self.output_dir, "mp3"), {})
        self.assertIsNone(_existing_track(_tracks_in_folder(self.output_dir, "mp3"), "Test Song", "Test Artist"))

    def test_an_unfinished_download_is_not_a_song(self) -> None:
        # What a cancelled download or a failed conversion leaves behind.
        for leftover in ("001 - Test Song - Test Artist.webm.part", "001 - Test Song - Test Artist.m4a.ytdl", "001 - Test Song - Test Artist.webm"):
            self._write(leftover, payload=b"partial bytes")
        self.assertEqual(_tracks_in_folder(self.output_dir, "mp3"), {})

    def test_a_file_the_app_did_not_write_is_ignored(self) -> None:
        # Apple gives "artist - title", not "number - title - artist", and the
        # app only ever sets its own numbered files aside as downloaded.
        self._write("Test Song - Test Artist.mp3")
        self.assertEqual(_tracks_in_folder(self.output_dir, "mp3"), {})

    def test_a_title_that_happens_to_contain_the_separator_still_matches(self) -> None:
        song = self._song(1, title="Test - Song")
        found = _tracks_in_folder(self.output_dir, "mp3")
        self.assertEqual(_existing_track(found, "Test - Song", "Test Artist"), song)

    def test_a_title_that_needs_cleaning_matches_the_cleaned_file(self) -> None:
        song = self._song(2, title='Song: Live/Demo?')
        found = _tracks_in_folder(self.output_dir, "mp3")
        self.assertEqual(_existing_track(found, "Song: Live/Demo?", "Test Artist"), song)

    def test_a_song_keeps_one_file_when_the_folder_holds_two_numbers(self) -> None:
        # Two runs under two different playlists can leave both numbers behind;
        # the lookup must settle on one of them rather than flip between runs.
        oldest = self._song(1)
        self._song(4)
        found = _tracks_in_folder(self.output_dir, "mp3")
        self.assertEqual(_existing_track(found, "Test Song", "Test Artist"), oldest)
        self.assertEqual(_existing_track(_tracks_in_folder(self.output_dir, "mp3"), "Test Song", "Test Artist"), oldest)

    def test_a_folder_that_is_not_there_is_not_an_error(self) -> None:
        self.assertEqual(_tracks_in_folder(self.output_dir / "not-created-yet", "mp3"), {})

    def test_the_key_is_the_sanitized_name(self) -> None:
        self.assertEqual(_track_name_key("Test Song", "Test Artist"), "test song - test artist")


class ReorderedPlaylistTests(unittest.TestCase):
    """What a run does when the playlist no longer matches the folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)
        self.logs: list[str] = []
        self.downloaded: list[str] = []
        self._patch(downloader, "read_playlist_tracks", lambda url, controls=None: list(TRACKS))

    def _patch(self, module: object, name: str, value: object) -> None:
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def _existing_song(self, index: int, track: tuple[str, str]) -> Path:
        stem = downloader._track_file_stem(index, track[0], track[1])
        path = self.output_dir / f"{stem}.mp3"
        path.write_bytes(COMPLETE)
        return path

    def _record_downloads(self, *, payload: bytes = COMPLETE) -> None:
        def fake(
            query,
            title,
            artist,
            index,
            output_dir,
            output_format,
            ffmpeg_path,
            controls=None,
        ):
            self.downloaded.append(f"{title} - {artist}")
            _track_output_path(Path(output_dir), output_format, index, title, artist).write_bytes(payload)

        self._patch(downloader, "_download_track_from_youtube", fake)

    def _run(self, *, skip_existing: bool = True) -> list[tuple[str, str]]:
        return download_playlist(
            url=PLAYLIST_URL,
            output_dir=self.output_dir,
            progress_callback=lambda current, total, name: None,
            log_callback=self.logs.append,
            error_callback=lambda track, reason: None,
            skip_existing=skip_existing,
        )

    def _files(self) -> list[str]:
        return sorted(path.name for path in self.output_dir.iterdir())

    def test_a_reordered_playlist_is_not_downloaded_again(self) -> None:
        # Saved when these two songs sat at 3 and 8 in the playlist.
        first = self._existing_song(3, TRACKS[0])
        second = self._existing_song(8, TRACKS[1])
        self._record_downloads()

        failures = self._run()

        self.assertEqual(failures, [])
        self.assertEqual(self.downloaded, [], "a song the user already has must not be fetched again")
        self.assertEqual(self._files(), sorted([first.name, second.name]))
        self.assertTrue(any("already downloaded as 003 - Test Song - Test Artist.mp3" in line for line in self.logs))

    def test_only_the_song_that_is_actually_new_is_downloaded(self) -> None:
        self._existing_song(3, TRACKS[0])
        self._existing_song(8, TRACKS[1])
        self._record_downloads()
        new_track = ("Brand New Song", "Test Artist")
        self._patch(downloader, "read_playlist_tracks", lambda url, controls=None: [new_track, *TRACKS])

        self._run()

        self.assertEqual(self.downloaded, ["Brand New Song - Test Artist"])
        self.assertIn(f"{downloader._track_file_stem(1, *new_track)}.mp3", self._files())
        self.assertIn(f"{downloader._track_file_stem(3, *TRACKS[0])}.mp3", self._files())

    def test_verification_is_not_fooled_by_the_numbers_from_an_earlier_run(self) -> None:
        self._existing_song(3, TRACKS[0])
        self._existing_song(8, TRACKS[1])

        result = verify_playlist_downloads(PLAYLIST_URL, self.output_dir, "mp3")

        self.assertEqual((result.total, result.present, len(result.missing), len(result.incomplete)), (2, 2, 0, 0))
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("Present: 2", report)
        self.assertNotIn("MISSING", report)

    def test_verification_reports_a_song_that_really_is_missing(self) -> None:
        self._existing_song(3, TRACKS[0])

        result = verify_playlist_downloads(PLAYLIST_URL, self.output_dir, "mp3")

        self.assertEqual((result.present, len(result.missing)), (1, 1))
        self.assertEqual(result.missing[0].index, 2)
        self.assertIn("MISSING | 002 | Test Song Two - Test Artist", result.report_path.read_text(encoding="utf-8"))

    def test_a_half_written_song_is_reported_incomplete_and_fetched_again(self) -> None:
        half = self._existing_song(3, TRACKS[0])
        half.write_bytes(b"only a few bytes")
        self._existing_song(8, TRACKS[1])

        result = verify_playlist_downloads(PLAYLIST_URL, self.output_dir, "mp3")
        self.assertEqual((result.present, len(result.incomplete), len(result.missing)), (1, 1, 0))
        self.assertEqual(result.incomplete[0].expected_path, half)
        self.assertIn("INCOMPLETE | 001 | Test Song - Test Artist", result.report_path.read_text(encoding="utf-8"))

        self._record_downloads()
        self._run()
        self.assertEqual(self.downloaded, ["Test Song - Test Artist"])

        after = verify_playlist_downloads(PLAYLIST_URL, self.output_dir, "mp3")
        self.assertEqual((after.present, len(after.incomplete), len(after.missing)), (2, 0, 0))

    def test_the_broken_copy_of_a_redownloaded_song_is_not_left_beside_it(self) -> None:
        # The run writes the song under the number it has now, so the cut off
        # copy under the old number has to go with it: two files of one song in
        # the folder is what the user sees after every retry otherwise.
        half = self._existing_song(3, TRACKS[0])
        half.write_bytes(b"only a few bytes")
        self._record_downloads()

        self._run()

        self.assertEqual(self.downloaded, ["Test Song - Test Artist", "Test Song Two - Test Artist"])
        self.assertEqual(self._files(), [f"{downloader._track_file_stem(1, *TRACKS[0])}.mp3", f"{downloader._track_file_stem(2, *TRACKS[1])}.mp3"])
        self.assertFalse(half.exists())
        self.assertTrue(any("CLEAN  003 - Test Song - Test Artist.mp3 (incomplete copy removed)" in line for line in self.logs))

    def test_a_song_listed_twice_is_only_downloaded_once(self) -> None:
        self._record_downloads()
        self._patch(downloader, "read_playlist_tracks", lambda url, controls=None: [TRACKS[0], TRACKS[0]])

        self._run()

        self.assertEqual(self.downloaded, ["Test Song - Test Artist"])
        self.assertTrue(any("already downloaded as 001 - Test Song - Test Artist.mp3" in line for line in self.logs))

    def test_a_run_with_skip_turned_off_still_downloads_everything(self) -> None:
        self._existing_song(3, TRACKS[0])
        self._record_downloads()

        self._run(skip_existing=False)

        self.assertEqual(self.downloaded, ["Test Song - Test Artist", "Test Song Two - Test Artist"])


if __name__ == "__main__":
    unittest.main()
