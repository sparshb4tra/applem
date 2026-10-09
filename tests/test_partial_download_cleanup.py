"""Checks that a cancelled or failed track leaves no stray files behind.

When a download is stopped mid-flight yt-dlp keeps the bytes it already wrote
next to the song (`001 - song - artist.webm.part`, plus a `.ytdl` sidecar or
`.part-Frag7` for fragmented sources), and when the ffmpeg step fails it leaves
the source audio itself (`001 - song - artist.webm`, a few MB of it). The
output folder is opened automatically when a run ends, so the user ends up
looking at files that are not songs, that never show up in the verification
report, and that a later retry can resume into.

`download_playlist` now clears the leftovers of the track it is about to try
and of the track that just failed, which is what these tests pin down.

Runs fully offline: the playlist reader is swapped for a fixed track list and
the YouTube download is swapped for a fake that writes the same files yt-dlp
does and then fails. No network, no ffmpeg, no real downloads.

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
    DownloadCancelled,
    _remove_unfinished_track_files,
    _track_file_stem,
    _track_output_path,
    _unfinished_track_files,
    download_playlist,
)
from yt_dlp.utils import DownloadError

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
TRACK = ("Test Song", "Test Artist")
STEM = _track_file_stem(1, TRACK[0], TRACK[1])


class UnfinishedFileTests(unittest.TestCase):
    """What counts as an unfinished download, and what must never be deleted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)

    def _make(self, name: str, payload: bytes = b"x" * 4096) -> Path:
        path = self.output_dir / name
        path.write_bytes(payload)
        return path

    def _remove(self, output_format: str = "mp3", index: int = 1, tracks: tuple[str, str] = TRACK):
        return _remove_unfinished_track_files(self.output_dir, output_format, index, tracks[0], tracks[1])

    def test_partial_from_a_cancelled_download_is_removed(self) -> None:
        part = self._make(f"{STEM}.webm.part")
        removed = self._remove()
        self.assertEqual(removed, [part])
        self.assertFalse(part.exists())

    def test_fragment_partial_and_info_sidecar_are_removed(self) -> None:
        fragment = self._make(f"{STEM}.webm.part-Frag7")
        sidecar = self._make(f"{STEM}.m4a.ytdl", payload=b"{}")
        removed = set(self._remove())
        self.assertEqual(removed, {fragment, sidecar})
        self.assertFalse(fragment.exists())
        self.assertFalse(sidecar.exists())

    def test_source_audio_from_a_failed_conversion_is_removed(self) -> None:
        # yt-dlp leaves the downloaded audio behind when ffmpeg cannot convert it.
        source = self._make(f"{STEM}.webm", payload=b"v" * 3_461_773)
        self.assertEqual(self._remove(), [source])
        self.assertFalse(source.exists())

    def test_the_finished_song_is_never_removed(self) -> None:
        song = self._make(f"{STEM}.mp3")
        stale = self._make(f"{STEM}.webm")
        removed = self._remove()
        self.assertEqual(removed, [stale])
        self.assertTrue(song.exists())

    def test_a_wav_from_an_earlier_run_is_not_treated_as_a_partial(self) -> None:
        # The user downloaded this track as wav before switching to mp3.
        earlier = self._make(f"{STEM}.wav")
        self.assertEqual(self._remove(output_format="mp3"), [])
        self.assertTrue(earlier.exists())

    def test_a_title_with_brackets_is_still_cleaned_up(self) -> None:
        # `[` and `]` are legal in filenames and would break a naive glob.
        title = "Song [Remix]"
        stem = _track_file_stem(2, title, TRACK[1])
        part = self._make(f"{stem}.webm.part")
        removed = _remove_unfinished_track_files(self.output_dir, "mp3", 2, title, TRACK[1])
        self.assertEqual(removed, [part])
        self.assertFalse(part.exists())

    def test_another_tracks_partial_is_left_alone(self) -> None:
        other_stem = _track_file_stem(2, "Other Song", TRACK[1])
        other = self._make(f"{other_stem}.webm.part")
        self.assertEqual(self._remove(), [])
        self.assertTrue(other.exists())

    def test_files_that_do_not_carry_the_track_name_are_left_alone(self) -> None:
        keep = [self._make("notes.txt"), self._make("001 - Test Song.mp3")]
        self.assertEqual(self._remove(), [])
        self.assertTrue(all(path.exists() for path in keep))

    def test_a_missing_folder_is_not_an_error(self) -> None:
        missing = self.output_dir / "not-created-yet"
        self.assertEqual(_unfinished_track_files(missing, "mp3", 1, *TRACK), [])

    def test_removals_are_logged(self) -> None:
        self._make(f"{STEM}.webm.part")
        lines: list[str] = []
        _remove_unfinished_track_files(self.output_dir, "mp3", 1, *TRACK, log_callback=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("CLEAN", lines[0])
        self.assertIn(f"{STEM}.webm.part", lines[0])


class DownloadCleanupTests(unittest.TestCase):
    """What `download_playlist` leaves behind after a cancel or a failure."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)
        self.logs: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self._patch(downloader, "read_playlist_tracks", lambda url, controls=None: [TRACK])

    def _patch(self, module: object, name: str, value: object) -> None:
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def _fake_download(self, leftover: str, error: Exception) -> None:
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
            stem = _track_file_stem(index, title, artist)
            (Path(output_dir) / f"{stem}.{leftover}").write_bytes(b"partial bytes")
            raise error

        self._patch(downloader, "_download_track_from_youtube", fake)

    def _run(self) -> list[tuple[str, str]]:
        return download_playlist(
            url=PLAYLIST_URL,
            output_dir=self.output_dir,
            progress_callback=lambda current, total, name: None,
            log_callback=self.logs.append,
            error_callback=lambda track, reason: self.errors.append((track, reason)),
        )

    def _files(self) -> list[str]:
        return sorted(path.name for path in self.output_dir.iterdir())

    def test_cancelled_track_leaves_nothing_behind(self) -> None:
        self._fake_download("webm.part", DownloadCancelled("Download cancelled."))
        with self.assertRaises(DownloadCancelled):
            self._run()
        self.assertEqual(self._files(), [])
        self.assertEqual(self.errors, [])

    def test_failed_track_is_still_reported_without_its_leftovers(self) -> None:
        self._fake_download("webm", DownloadError("unable to download video data"))
        failures = self._run()

        self.assertEqual(len(failures), 1)
        self.assertTrue(self.errors)
        # The failure file stays, the unusable download does not.
        self.assertEqual(self._files(), ["failed_downloads.txt"])
        self.assertTrue(any("CLEAN" in line for line in self.logs))

    def test_a_stale_partial_from_a_killed_run_is_cleared_before_the_retry(self) -> None:
        stale = self.output_dir / f"{STEM}.webm"
        stale.write_bytes(b"half a song from a run that was killed")
        seen_at_start: list[list[str]] = []

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
            seen_at_start.append(sorted(path.name for path in Path(output_dir).iterdir()))
            _track_output_path(Path(output_dir), output_format, index, title, artist).write_bytes(b"m" * 2048)

        self._patch(downloader, "_download_track_from_youtube", fake)
        failures = self._run()

        self.assertEqual(failures, [])
        self.assertEqual(seen_at_start, [[]], "the stale partial should be gone before the attempt")
        self.assertEqual(self._files(), [f"{STEM}.mp3"])

    def test_a_failed_read_still_reports_the_playlist_error(self) -> None:
        # Nothing downloaded yet: the cleanup must not hide playlist errors.
        def offline(url: str, controls: object = None) -> list[tuple[str, str]]:
            raise downloader.PlaylistDownloaderError("No internet connection. Check your Wi-Fi and try again.")

        self._patch(downloader, "read_playlist_tracks", offline)
        with self.assertRaises(downloader.PlaylistDownloaderError):
            self._run()


if __name__ == "__main__":
    unittest.main()
