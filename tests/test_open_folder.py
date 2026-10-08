"""Checks that opening the output folder reports failure instead of going quiet.

"Open Downloads Folder" used to call `os.startfile`/`open` and let whatever
came back escape: on a folder that does not exist yet (the default
`~/Music/AppleDownloads` before the first download) it raised
FileNotFoundError inside a Tk callback, so the click did nothing at all and the
traceback went to a log the user never reads. `open_folder` now answers with
True/False, which is what these tests pin down.

Nothing here opens a real file manager: the platform launcher is patched.

From the repo root:

    python -m unittest discover -s tests -v
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

from utils.open_folder import open_folder


class OpenFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        # Pretend to be Windows so os.startfile is used everywhere, and put a
        # mock in its place so no file manager window is ever opened.
        self._system = mock.patch("utils.open_folder.platform.system", return_value="Windows")
        self._system.start()
        self.addCleanup(self._system.stop)
        self.launch = mock.patch("utils.open_folder.os.startfile", create=True)
        self.starter = self.launch.start()
        self.addCleanup(self.launch.stop)

    def test_missing_folder_is_reported_instead_of_raised(self) -> None:
        missing = Path(tempfile.gettempdir()) / "applem-folder-that-is-not-there"
        self.assertFalse(open_folder(missing))
        self.starter.assert_not_called()

    def test_a_file_is_not_a_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.txt"
            target.write_text("hello", encoding="utf-8")
            self.assertFalse(open_folder(target))
        self.starter.assert_not_called()

    def test_existing_folder_opens_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.assertTrue(open_folder(target))
        self.starter.assert_called_once_with(str(target.resolve()))

    def test_a_launcher_that_refuses_is_reported_not_raised(self) -> None:
        self.starter.side_effect = OSError("no file manager here")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(open_folder(Path(tmp)))

    def test_a_home_shortcut_is_expanded_before_opening(self) -> None:
        self.assertTrue(open_folder(Path("~")))
        self.starter.assert_called_once_with(str(Path.home().resolve()))


if __name__ == "__main__":
    unittest.main()
