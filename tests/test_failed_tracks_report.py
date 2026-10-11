"""Checks that `failed_downloads.txt` describes the run that just ended.

The report is written into the save folder, the folder is opened automatically
when a run finishes, and the "finished with warnings" dialog tells the user to
read this file. A report from an earlier run used to survive a later clean one,
so the common "retry missing" flow ended with every gap filled and a file next
to them still listing those songs as failed. That file is now rewritten on
every run and removed when a run has nothing to report.

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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.downloader as downloader
from core.downloader import (
    FAILED_TRACKS_FILENAME,
    DownloadCancelled,
    _track_output_path,
    download_playlist,
)
from yt_dlp.utils import DownloadError

PLAYLIST_URL = "https://music.apple.com/us/playlist/test-playlist/pl.123"
TRACK = ("Test Song", "Test Artist")
STALE_LINE = "Old Song - Old Artist | unable to download video data"


class FailedTracksReportTests(unittest.TestCase):
    """What a run leaves in `failed_downloads.txt`."""

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

    def _report(self) -> Path:
        return self.output_dir / FAILED_TRACKS_FILENAME

    def _write_stale_report(self) -> Path:
        report = self._report()
        report.write_text(STALE_LINE, encoding="utf-8")
        return report

    def _patch_download(self, error: Exception | None = None) -> None:
        """Fake yt-dlp: write the song on success, optionally blow up instead."""

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
            if error is not None:
                raise error
            _track_output_path(Path(output_dir), output_format, index, title, artist).write_bytes(b"m" * 2048)

        self._patch(downloader, "_download_track_from_youtube", fake)

    def _run(self) -> list[tuple[str, str]]:
        return download_playlist(
            url=PLAYLIST_URL,
            output_dir=self.output_dir,
            progress_callback=lambda current, total, name: None,
            log_callback=self.logs.append,
            error_callback=lambda track, reason: self.errors.append((track, reason)),
        )

    def test_a_stale_report_is_cleared_when_the_run_finishes_clean(self) -> None:
        stale = self._write_stale_report()
        self._patch_download()

        failures = self._run()

        self.assertEqual(failures, [])
        self.assertFalse(stale.exists(), "a run with no failures must not leave an older report behind")
        self.assertTrue(any(FAILED_TRACKS_FILENAME in line and "CLEAN" in line for line in self.logs))

    def test_a_stale_report_is_replaced_by_the_failures_of_this_run(self) -> None:
        stale = self._write_stale_report()
        self._patch_download(error=DownloadError("unable to download video data"))

        failures = self._run()

        self.assertEqual(len(failures), 1)
        self.assertEqual(
            stale.read_text(encoding="utf-8"),
            f"{TRACK[0]} - {TRACK[1]} | unable to download video data",
        )

    def test_a_clean_run_does_not_create_a_report(self) -> None:
        self._patch_download()

        self._run()

        self.assertFalse(self._report().exists())

    def test_a_cancelled_run_leaves_the_previous_report_alone(self) -> None:
        # The user stopped the run, so nothing about the failures is settled
        # yet and the last report is still the most recent answer.
        stale = self._write_stale_report()
        self._patch_download(error=DownloadCancelled("Download cancelled."))

        with self.assertRaises(DownloadCancelled):
            self._run()

        self.assertEqual(stale.read_text(encoding="utf-8"), STALE_LINE)

    def test_a_report_that_cannot_be_removed_is_reported_not_raised(self) -> None:
        stale = self._write_stale_report()
        self._patch_download()

        with mock.patch.object(Path, "unlink", side_effect=OSError("file is in use")):
            failures = self._run()

        self.assertEqual(failures, [])
        self.assertTrue(stale.exists())
        self.assertTrue(any("WARN" in line and FAILED_TRACKS_FILENAME in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
