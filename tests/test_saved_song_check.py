"""Checks that a track is only reported as downloaded when its file is there.

yt-dlp's download call can return without raising and without leaving a
playable file behind: a YouTube search that matches nothing ends quietly, and a
conversion step that produces no output leaves the folder empty. The run used to
log `OK` for the track anyway, remember it as downloaded for the rest of the
playlist, and finish with "Download complete", so a song that was never saved
only turned up later in a verification report.

A track now counts as done only when its own file is in the folder and holds
audio. When the file is missing, or too small to be a song, the track goes into
`failed_downloads.txt` with a reason the user can act on, and whatever the empty
attempt did write is removed so the folder does not keep a broken copy of a song
the report calls failed.

Runs fully offline: the playlist reader and the YouTube download are swapped for
fakes, so nothing here touches the network or ffmpeg.

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
    FAILED_TRACKS_FILENAME,
    NO_SONG_SAVED_REASON,
    _track_output_path,
    download_playlist,
)

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
SONG = ("Test Song", "Test Artist")
OTHER_SONG = ("Other Song", "Other Artist")
# What a real song file looks like next to what a failed attempt leaves behind.
SONG_BYTES = b"m" * 2048
CRUMB_BYTES = b"only a few bytes"


class SavedSongCheckTests(unittest.TestCase):
    """What a run says about a track whose download wrote nothing usable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)
        self.logs: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self._patch(downloader, "read_playlist_tracks", lambda url, controls=None: [SONG, OTHER_SONG])

    def _patch(self, module: object, name: str, value: object) -> None:
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def _patch_download(self, writes: dict[str, bytes]) -> None:
        """Fake yt-dlp: write exactly what `writes` holds for each song title.

        A title missing from the map is a download that "succeeds" and writes
        nothing at all, which is the case this file is about.
        """

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
            payload = writes.get(title)
            if payload is None:
                return None
            _track_output_path(Path(output_dir), output_format, index, title, artist).write_bytes(payload)
            return None

        self._patch(downloader, "_download_track_from_youtube", fake)

    def _run(self) -> list[tuple[str, str]]:
        return download_playlist(
            url=PLAYLIST_URL,
            output_dir=self.output_dir,
            progress_callback=lambda current, total, name: None,
            log_callback=self.logs.append,
            error_callback=lambda track, reason: self.errors.append((track, reason)),
        )

    def _ok_lines(self) -> list[str]:
        return [line for line in self.logs if line.startswith("OK")]

    def test_a_track_with_nothing_saved_is_reported_as_failed(self) -> None:
        self._patch_download({OTHER_SONG[0]: SONG_BYTES})

        failures = self._run()

        self.assertEqual(
            failures,
            [(f"{SONG[0]} - {SONG[1]}", NO_SONG_SAVED_REASON)],
            "a download that saved no file must not be counted as a downloaded song",
        )
        self.assertEqual(self._ok_lines(), [f"OK  {OTHER_SONG[0]} - {OTHER_SONG[1]}"])

    def test_the_song_that_did_not_land_is_in_the_failure_report(self) -> None:
        self._patch_download({OTHER_SONG[0]: SONG_BYTES})

        self._run()

        report = self.output_dir / FAILED_TRACKS_FILENAME
        self.assertTrue(report.exists())
        self.assertEqual(
            report.read_text(encoding="utf-8"),
            f"{SONG[0]} - {SONG[1]} | {NO_SONG_SAVED_REASON}",
        )

    def test_a_file_too_small_to_be_a_song_is_a_failure_and_is_removed(self) -> None:
        # A conversion that dies partway can leave a file with the right name in
        # the right format and no audio in it. Calling that a downloaded song
        # would hide the failure behind a file the user cannot play.
        leftover = _track_output_path(self.output_dir, "mp3", 1, SONG[0], SONG[1])
        self._patch_download({SONG[0]: CRUMB_BYTES, OTHER_SONG[0]: SONG_BYTES})

        failures = self._run()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], NO_SONG_SAVED_REASON)
        self.assertFalse(
            leftover.exists(),
            "the broken file must not stay next to a report that calls the song failed",
        )

    def test_a_saved_song_is_still_logged_ok_and_kept(self) -> None:
        saved = _track_output_path(self.output_dir, "mp3", 1, SONG[0], SONG[1])
        self._patch_download({SONG[0]: SONG_BYTES, OTHER_SONG[0]: SONG_BYTES})

        failures = self._run()

        self.assertEqual(failures, [])
        self.assertEqual(len(self._ok_lines()), 2)
        self.assertTrue(saved.exists())
        self.assertFalse((self.output_dir / FAILED_TRACKS_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
